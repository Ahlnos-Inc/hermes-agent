"""Closed credential contract for dispatcher-launched workers.

The dispatcher is the credential authority for Kanban workers.  This module
keeps that authority deliberately small:

* the machine-global manifest can grant only names in ``CAPABILITIES``;
* Bitwarden is consulted through the existing secret-source adapter;
* resolved values are kept out of public result representations and logs; and
* the child environment is stripped before the authorized handoff is added.

The manifest is policy, not a vault.  Capability definitions (including the
source key and the private handoff name) live in code so a YAML edit cannot
turn this into an arbitrary environment-variable injector.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import signal
import sqlite3
import stat
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator, Mapping
from urllib.parse import unquote, urlsplit

from hermes_constants import get_default_hermes_root

_log = logging.getLogger(__name__)

LEGACY_CONTRACT_VERSION = 1
CONTRACT_VERSION = 2
SUPPORTED_CONTRACT_VERSIONS = frozenset({LEGACY_CONTRACT_VERSION, CONTRACT_VERSION})
MANIFEST_FILENAME = "worker-credential-contract.yaml"
GOOGLE_ADS_ACTIVATION_FILENAME = "google-ads-campaign-status.activation.json"

BWS_BOOTSTRAP_ENV = "BWS_ACCESS_TOKEN"
# Dedicated controller-only source authority for the Google Ads one-shot
# helper.  This value is intentionally not a capability source_key/handoff and
# therefore has no worker accessor or projection path.
GOOGLE_ADS_CONTROLLER_BWS_TOKEN_ENV = "HERMES_GOOGLE_ADS_CONTROLLER_BWS_TOKEN"
# Ambient name that must always be STRIPPED from workers (kept in
# UNCONDITIONAL_STRIP_ENV below). It is NOT the resolve source anymore.
GITHUB_WRITE_SOURCE_KEY = "GH_TOKEN_SECRET_WRITE"
# The BWS secrets github_write RESOLVES from, keyed by the GitHub owner of the
# repository the worker will publish to (BUILD-603).
#
# A fine-grained PAT has exactly one resource owner, and the release targets
# span two (``rules/pr-target-repo-allowlist.json`` allows ``Ahlnos-Inc`` and
# ``nlachica``), so one token cannot cover both and there is no single resolve
# key to point at. The dispatcher selects per task from the owner of the
# worker's own workspace remote; an owner with no entry here gets no
# ``github_write`` handoff at all.
#
# This replaces the classic broad-scoped ``GITHUB_TOKEN`` (all repos, workflow
# scope, no expiry) that was the accepted interim from 2026-07-20 until both
# fine-grained PATs were minted on 2026-07-26. Keys are casefolded owners.
GITHUB_WRITE_RESOLVE_KEYS: Mapping[str, str] = {
    "ahlnos-inc": "HERMES_RELEASER_AHLNOS_INC",
    "nlachica": "HERMES_RELEASER_NLACHICA",
}

# These names are intentionally explicit.  The list is also used when a
# caller provides an environment mapping rather than os.environ, so a worker
# cannot accidentally inherit the controller's bootstrap or GitHub aliases.
PRIVATE_HANDOFF_PREFIX = "HERMES_WORKER_CREDENTIAL_"
GITHUB_WRITE_HANDOFF_ENV = f"{PRIVATE_HANDOFF_PREFIX}GITHUB_WRITE"
BWS_BOOTSTRAP_HANDOFF_ENV = f"{PRIVATE_HANDOFF_PREFIX}BWS_BOOTSTRAP"
META_MARKETING_HANDOFF_ENV = f"{PRIVATE_HANDOFF_PREFIX}META_MARKETING_READ"
INSTAGRAM_GRAPH_HANDOFF_ENV = f"{PRIVATE_HANDOFF_PREFIX}INSTAGRAM_GRAPH_READ"
POSTHOG_READ_HANDOFF_ENV = f"{PRIVATE_HANDOFF_PREFIX}POSTHOG_READ"
MANIFEST_DIGEST_ENV = f"{PRIVATE_HANDOFF_PREFIX}MANIFEST_DIGEST"

# Terminal-only artifacts live below the machine-global Kanban runtime root,
# never in a profile home.  The directory names are generated locally; task
# and profile values are not interpolated into paths.
WORKER_CREDENTIAL_RUNTIME_RELATIVE = Path(
    "kanban", "runtime", "worker-credentials"
)
WORKER_CREDENTIAL_ARTIFACT_MAX_AGE_SECONDS = 24 * 60 * 60

# This helper is installed at Git's command-scope so repository-local helpers
# cannot re-add Keychain or a hosts-file-backed helper.  It is deliberately
# token-free; the only secret it reads is the already-projected GH_TOKEN.
GIT_ENV_TOKEN_HELPER = (
    "!f() { "
    "protocol=; host=; "
    "while IFS= read -r line; do "
    "case \"$line\" in "
    "protocol=*) protocol=\"${line#protocol=}\";; "
    "host=*) host=\"${line#host=}\";; esac; "
    "done; "
    "[ \"$protocol\" = https ] || return 0; "
    "[ \"$host\" = github.com ] || return 0; "
    "printf 'username=x-access-token\\npassword=%s\\n' \"$GH_TOKEN\"; "
    "}; f"
)

UNCONDITIONAL_STRIP_ENV = frozenset(
    {
        BWS_BOOTSTRAP_ENV,
        GOOGLE_ADS_CONTROLLER_BWS_TOKEN_ENV,
        "GH_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "GITHUB_TOKEN",
        "GITHUB_PERSONAL_ACCESS_TOKEN",
        "COPILOT_GITHUB_TOKEN",
        "GITHUB_APP_ID",
        "GITHUB_APP_PRIVATE_KEY_PATH",
        "GITHUB_APP_INSTALLATION_ID",
        GITHUB_WRITE_SOURCE_KEY,
        GITHUB_WRITE_HANDOFF_ENV,
        BWS_BOOTSTRAP_HANDOFF_ENV,
    }
)


class WorkerCredentialError(RuntimeError):
    """A deterministic, secret-free worker credential preflight failure."""


@dataclass(frozen=True)
class CapabilityDefinition:
    """Code-owned definition of one closed worker capability."""

    name: str
    source_key: str | None = None
    handoff_env: str | None = None
    projection_env: tuple[str, ...] = ()
    projection_kind: str = "worker_environment"
    source_keys: tuple[str, ...] = ()
    ambient_strip_env: tuple[str, ...] = ()
    operation: str | None = None
    api_major: str | None = None
    # What picks ONE of several ``source_keys`` per task, when the keys are
    # alternatives rather than a bundle. Display-only, but load-bearing for the
    # operator: without it the audit lists every candidate key on one row and
    # reads as "this profile receives all of them".
    selected_by: str | None = None


# No capability here hands a worker the vault access token itself. The last one
# that did (``bws_bootstrap``, marketing-operator) was retired by BUILD-601 in
# favour of the three scoped marketing reads below; a worker now receives only
# resolved values, projected at the terminal boundary.
CAPABILITIES: Mapping[str, CapabilityDefinition] = {
    "github_write": CapabilityDefinition(
        name="github_write",
        # Owner-selected: exactly one of these is resolved per task, never
        # both. Listing both here is what keeps the unselected one in
        # CAPABILITY_SENSITIVE_ENV, so a releaser worker cannot inherit the
        # other owner's token ambiently.
        source_keys=tuple(sorted(set(GITHUB_WRITE_RESOLVE_KEYS.values()))),
        handoff_env=GITHUB_WRITE_HANDOFF_ENV,
        projection_env=("GH_TOKEN",),
        selected_by="repository owner",
    ),
    # BUILD-601: the three credentials marketing-operator actually needs, which
    # replaced its ``bws_bootstrap`` grant -- the last full-vault-token grant on
    # the system. Deliberately three single-secret capabilities
    # rather than one bundle: the existing CapabilityDefinition shape already
    # fits, each grant is independently revocable, and the audit renders one
    # row per credential instead of one row naming three.
    #
    # ``source_key`` is a Bitwarden RECORD name, but it must ALSO be a legal
    # environment-variable name: the controller fetches through
    # ``_run_bws_list``, which drops any record whose key fails
    # ``is_valid_env_name`` (agent/secret_sources/bitwarden.py:481) BEFORE the
    # ``secrets[key]`` assignment five lines later. The hyphenated
    # ``meta-system-user-token`` therefore resolved to nothing; the record was
    # recreated as ``META_SYSTEM_USER_TOKEN`` in the controller's own project
    # (BUILD-601). Every capability here must also live in the project named by
    # ``secrets.bitwarden.project_id`` -- the adapter fetches exactly one
    # project, so a record in a sibling project is invisible to the controller
    # even though an unscoped ``bws`` CLI can see it.
    "meta_marketing_read": CapabilityDefinition(
        name="meta_marketing_read",
        source_key="META_SYSTEM_USER_TOKEN",
        handoff_env=META_MARKETING_HANDOFF_ENV,
        projection_env=("META_SYSTEM_USER_TOKEN",),
    ),
    "instagram_graph_read": CapabilityDefinition(
        name="instagram_graph_read",
        source_key="INSTAGRAM_GRAPH_TOKEN",
        handoff_env=INSTAGRAM_GRAPH_HANDOFF_ENV,
        projection_env=("INSTAGRAM_GRAPH_TOKEN",),
    ),
    "posthog_read": CapabilityDefinition(
        name="posthog_read",
        source_key="POSTHOG_PERSONAL_KEY",
        handoff_env=POSTHOG_READ_HANDOFF_ENV,
        projection_env=("POSTHOG_PERSONAL_KEY",),
    ),
    "google_ads_campaign_status_read": CapabilityDefinition(
        name="google_ads_campaign_status_read",
        projection_kind="controller_action_receipt",
        source_keys=(
            "google-ads-developer-token",
            "google-ads-manager-customer-id",
            "vitatide-marketing-oauth-client-id",
            "vitatide-marketing-oauth-client-secret",
            "vitatide-marketing-oauth-refresh-token",
        ),
        # Source keys are Bitwarden record names, not necessarily environment
        # variable names. Strip conventional aliases too so a stale controller
        # environment can never become an ambient worker handoff.
        ambient_strip_env=(
            GOOGLE_ADS_CONTROLLER_BWS_TOKEN_ENV,
            "GOOGLE_ADS_DEVELOPER_TOKEN",
            "GOOGLE_ADS_MANAGER_CUSTOMER_ID",
            "GOOGLE_ADS_LOGIN_CUSTOMER_ID",
            "GOOGLE_ADS_CUSTOMER_ID",
            "GOOGLE_ADS_CLIENT_ID",
            "GOOGLE_ADS_CLIENT_SECRET",
            "GOOGLE_ADS_REFRESH_TOKEN",
            "VITATIDE_MARKETING_OAUTH_CLIENT_ID",
            "VITATIDE_MARKETING_OAUTH_CLIENT_SECRET",
            "VITATIDE_MARKETING_OAUTH_REFRESH_TOKEN",
        ),
        operation="campaign_status_read_v1",
        api_major="v24",
    ),
}

# Every name a capability sources from, plus the aliases those sources are
# conventionally exported under. Stripped from EVERY worker, including one
# granted the capability: a grant is delivered through the private handoff and
# projected at the terminal boundary, never inherited ambiently from the
# controller's own environment.
#
# ``source_key`` (singular) belongs here as much as ``source_keys``. It was
# absent until BUILD-601, which did not matter while the only singular source
# was ``bws_bootstrap``'s BWS_ACCESS_TOKEN (stripped separately as
# ``access_token_env``) -- but the marketing read capabilities source from
# INSTAGRAM_GRAPH_TOKEN and POSTHOG_PERSONAL_KEY, which are real vault names
# already present in the controller environment, so omitting them would hand a
# granted worker the ambient copy and quietly defeat the terminal-only
# boundary.
CAPABILITY_SENSITIVE_ENV = frozenset(
    name
    for definition in CAPABILITIES.values()
    for name in (
        *((definition.source_key,) if definition.source_key else ()),
        *definition.source_keys,
        *definition.ambient_strip_env,
        # The names a grant is PROJECTED under belong here too: an ambient
        # copy under the same name would be indistinguishable from the
        # projected credential to anything downstream. GH_TOKEN and
        # BWS_ACCESS_TOKEN were already covered by name; this makes it a
        # property of the registry instead of two hand-kept lists.
        *definition.projection_env,
    )
)

# Reverse index of the private handoff names, derived so that adding a
# capability cannot leave its handoff without a consumer.
_HANDOFF_ENV_TO_CAPABILITY: Mapping[str, str] = {
    definition.handoff_env: name
    for name, definition in CAPABILITIES.items()
    if definition.handoff_env
}


def _canonical_manifest_payload(
    profiles: Mapping[str, tuple[str, ...]],
    *,
    version: int = LEGACY_CONTRACT_VERSION,
    action_configs: Mapping[str, Mapping[str, Mapping[str, str]]] | None = None,
) -> dict[str, Any]:
    if version == LEGACY_CONTRACT_VERSION:
        normalized_profiles: dict[str, Any] = {
            profile: {"actions": list(actions)}
            for profile, actions in sorted(profiles.items())
        }
    else:
        configs = action_configs or {}
        normalized_profiles = {
            profile: {
                "actions": {
                    action: dict(sorted((configs.get(profile, {}).get(action) or {}).items()))
                    for action in actions
                }
            }
            for profile, actions in sorted(profiles.items())
        }
    return {
        "version": version,
        "profiles": normalized_profiles,
    }


def _manifest_digest(
    profiles: Mapping[str, tuple[str, ...]],
    *,
    version: int = LEGACY_CONTRACT_VERSION,
    action_configs: Mapping[str, Mapping[str, Mapping[str, str]]] | None = None,
) -> str:
    encoded = json.dumps(
        _canonical_manifest_payload(
            profiles,
            version=version,
            action_configs=action_configs,
        ),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class WorkerCredentialManifest:
    """Normalized, digested worker credential/action manifest."""

    version: int
    profiles: Mapping[str, tuple[str, ...]]
    digest: str
    path: Path | None = field(default=None, repr=False, compare=False)
    action_configs: Mapping[str, Mapping[str, Mapping[str, str]]] = field(
        default_factory=dict, repr=False, compare=False
    )

    def actions_for(self, profile: str) -> tuple[str, ...]:
        """Return grants for *profile*; absent profiles have no grants."""
        return self.profiles.get(_normalize_profile(profile), ())

    def config_for(self, profile: str, capability: str) -> Mapping[str, str]:
        """Return the closed, non-secret v2 config for one granted action."""
        normalized = _normalize_profile(profile)
        return self.action_configs.get(normalized, {}).get(capability, {})


@dataclass(frozen=True)
class WorkerCredentialPlan:
    """Secret-free public result of worker credential preflight.

    The resolved handoff values are deliberately stored in a private,
    ``repr=False`` field.  The dispatcher passes this object to
    :func:`build_worker_environment`; callers should not serialize it.
    """

    profile: str
    manifest_digest: str
    capabilities: tuple[str, ...]
    diagnostics: tuple[str, ...] = ()
    error: str | None = None
    source: str | None = None
    _handoff: tuple[tuple[str, str], ...] = field(
        default=(), repr=False, compare=False
    )
    _strip_env: frozenset[str] = field(
        default=UNCONDITIONAL_STRIP_ENV, repr=False, compare=False
    )

    @property
    def ok(self) -> bool:
        return self.error is None

    def require_ok(self) -> "WorkerCredentialPlan":
        if not self.ok:
            raise WorkerCredentialError(
                self.error or "worker credential preflight failed"
            ) from None
        return self


@dataclass(frozen=True)
class ConsumedWorkerCredentials:
    """Secret-free view of credentials consumed by the worker bootstrap."""

    capabilities: tuple[str, ...]
    _values: tuple[tuple[str, str], ...] = field(
        default=(), repr=False, compare=False
    )

    @property
    def ok(self) -> bool:
        return bool(self.capabilities)


_WORKER_CREDENTIAL_LOCK = threading.Lock()
_TRUSTED_WORKER_CREDENTIALS: dict[str, str] = {}


@dataclass(frozen=True)
class _FileSeal:
    """Identity captured for one immutable bootstrap-owned executable."""

    path: str
    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int
    sha256: str


@dataclass(frozen=True)
class _DirectorySeal:
    """Identity captured for one bootstrap-owned directory."""

    path: str
    device: int
    inode: int
    mode: int


@dataclass(frozen=True)
class SealedGitRuntime:
    """Bootstrap-sealed executable, helper runtime, PATH, and temp root."""

    git: _FileSeal
    version: str
    exec_path: _DirectorySeal
    shell: _FileSeal
    path: str
    path_dirs: tuple[_DirectorySeal, ...]
    temp_root: _DirectorySeal


@dataclass(frozen=True)
class TrustedWorkerCredentialRuntime:
    """Process-local identity and values admitted at worker bootstrap.

    The private handoff is consumed once.  Terminal projection consults this
    object rather than re-reading an environment marker, then checks the
    current Kanban identity before every projection.  This prevents a later
    shell export from becoming an authorization grant.
    """

    profile: str
    task_id: str
    run_id: str
    manifest_digest: str
    manifest_verified: bool
    kanban_db_path: str
    capabilities: tuple[str, ...]
    git_runtime: SealedGitRuntime | None = field(
        default=None, repr=False, compare=False
    )
    _values: tuple[tuple[str, str], ...] = field(
        default=(), repr=False, compare=False
    )

    def value_for(self, capability: str) -> str | None:
        return dict(self._values).get(capability)


_TRUSTED_WORKER_RUNTIME: TrustedWorkerCredentialRuntime | None = None
_TERMINAL_ARTIFACT_DIR: Path | None = None
_PUBLICATION_READBACK_CALLS: dict[tuple[str, int], int] = {}
_PUBLICATION_CREDENTIAL_ATTEMPTS: set[tuple[str, int]] = set()


def _normalize_profile(profile: str) -> str:
    try:
        from hermes_cli.profile_contract import normalize_and_validate_profile_name

        return normalize_and_validate_profile_name(profile)
    except (TypeError, ValueError):
        raise WorkerCredentialError("worker credential profile is invalid") from None


_GIT_BOOTSTRAP_CANDIDATES = (
    ("/usr/bin/git", "/opt/homebrew/bin/git", "/usr/local/bin/git")
    if sys.platform == "darwin"
    else ("/usr/bin/git", "/bin/git", "/usr/local/bin/git")
)
_GIT_BOOTSTRAP_TIMEOUT_SECONDS = 10
_TRUSTED_TEMP_ROOT_PREFIX = "hermes-publication-runtime-"
_DIRECTORY_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)


def _worker_can_write(st: os.stat_result) -> bool:
    """Return whether the current uid's normal Unix mode grants write access."""
    uid = os.geteuid() if hasattr(os, "geteuid") else os.getuid()
    groups = set(os.getgroups()) if hasattr(os, "getgroups") else set()
    groups.add(os.getegid() if hasattr(os, "getegid") else os.getgid())
    if st.st_uid == uid:
        return bool(st.st_mode & stat.S_IWUSR)
    if st.st_gid in groups:
        return bool(st.st_mode & stat.S_IWGRP)
    return bool(st.st_mode & stat.S_IWOTH)


def _immutable_real_path(path: Path, *, directory: bool) -> Path | None:
    """Resolve an OS path and reject every worker-steerable ancestor."""
    try:
        real = path.resolve(strict=True)
        item_stat = real.stat()
    except (OSError, RuntimeError):
        return None
    if directory:
        if not stat.S_ISDIR(item_stat.st_mode):
            return None
    elif not stat.S_ISREG(item_stat.st_mode) or not os.access(real, os.X_OK):
        return None

    candidates = [real, *real.parents]
    for candidate in candidates:
        try:
            candidate_stat = candidate.stat()
        except OSError:
            return None
        if candidate != real and not stat.S_ISDIR(candidate_stat.st_mode):
            return None
        if _worker_can_write(candidate_stat):
            return None
        if stat.S_ISDIR(candidate_stat.st_mode) and candidate_stat.st_mode & (
            stat.S_IWGRP | stat.S_IWOTH
        ):
            return None
    return real


def _sha256_file(path: Path) -> str | None:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    return digest.hexdigest()
                digest.update(chunk)
    except OSError:
        return None


def _seal_file(path: Path) -> _FileSeal | None:
    real = _immutable_real_path(path, directory=False)
    if real is None:
        return None
    try:
        st = real.stat()
    except OSError:
        return None
    digest = _sha256_file(real)
    if digest is None:
        return None
    return _FileSeal(
        path=str(real),
        device=st.st_dev,
        inode=st.st_ino,
        mode=stat.S_IMODE(st.st_mode),
        size=st.st_size,
        mtime_ns=st.st_mtime_ns,
        sha256=digest,
    )


def _seal_directory(path: Path, *, immutable: bool = True) -> _DirectorySeal | None:
    if immutable:
        real = _immutable_real_path(path, directory=True)
        if real is None:
            return None
    else:
        try:
            real = path.resolve(strict=True)
            if real.is_symlink() or not real.is_dir():
                return None
        except (OSError, RuntimeError):
            return None
    try:
        st = real.stat()
    except OSError:
        return None
    return _DirectorySeal(
        path=str(real),
        device=st.st_dev,
        inode=st.st_ino,
        mode=stat.S_IMODE(st.st_mode),
    )


def _seal_matches_file(seal: _FileSeal, *, rehash: bool) -> bool:
    try:
        path = Path(seal.path)
        st = path.stat()
    except OSError:
        return False
    if (
        st.st_dev,
        st.st_ino,
        stat.S_IMODE(st.st_mode),
        st.st_size,
        st.st_mtime_ns,
    ) != (seal.device, seal.inode, seal.mode, seal.size, seal.mtime_ns):
        return False
    return not rehash or _sha256_file(path) == seal.sha256


def _seal_matches_directory(seal: _DirectorySeal) -> bool:
    try:
        path = Path(seal.path)
        st = path.stat()
    except OSError:
        return False
    return (
        stat.S_ISDIR(st.st_mode)
        and not path.is_symlink()
        and (st.st_dev, st.st_ino, stat.S_IMODE(st.st_mode))
        == (seal.device, seal.inode, seal.mode)
    )


def _bootstrap_git_query(git: str, argument: str) -> str | None:
    try:
        completed = subprocess.run(
            [git, argument],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            stdin=subprocess.DEVNULL,
            env={
                "HOME": os.devnull,
                "PATH": "/usr/bin:/bin",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0",
                "LANG": "C",
                "LC_ALL": "C",
            },
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = (completed.stdout or "").strip()
    return value if completed.returncode == 0 and value else None


def _seal_git_runtime() -> SealedGitRuntime | None:
    """Seal the fixed Git runtime before any model/tool execution begins."""
    for candidate in _GIT_BOOTSTRAP_CANDIDATES:
        git = _seal_file(Path(candidate))
        if git is None:
            continue
        version = _bootstrap_git_query(git.path, "--version")
        exec_path_value = _bootstrap_git_query(git.path, "--exec-path")
        if version is None or not version.startswith("git version ") or not exec_path_value:
            continue
        exec_path = _seal_directory(Path(exec_path_value))
        shell = _seal_file(Path("/bin/sh"))
        if exec_path is None or shell is None:
            continue

        path_seals: list[_DirectorySeal] = []
        for value in dict.fromkeys((str(Path(git.path).parent), "/usr/bin", "/bin")):
            sealed = _seal_directory(Path(value))
            if sealed is None:
                path_seals = []
                break
            path_seals.append(sealed)
        if not path_seals:
            continue

        try:
            temp_root_value = tempfile.mkdtemp(prefix=_TRUSTED_TEMP_ROOT_PREFIX)
            temp_root_path = Path(temp_root_value)
            parent = temp_root_path.parent.resolve(strict=True)
            created = temp_root_path.lstat()
            if (
                temp_root_path.name.startswith(_TRUSTED_TEMP_ROOT_PREFIX) is False
                or temp_root_path.is_symlink()
                or not stat.S_ISDIR(created.st_mode)
                or temp_root_path.parent.resolve(strict=True) != parent
            ):
                continue
            directory_fd = os.open(temp_root_path, _DIRECTORY_OPEN_FLAGS)
            try:
                opened = os.fstat(directory_fd)
                if (opened.st_dev, opened.st_ino) != (created.st_dev, created.st_ino):
                    continue
                os.fchmod(directory_fd, 0o700)
                secured = os.fstat(directory_fd)
            finally:
                os.close(directory_fd)
            if (
                (secured.st_dev, secured.st_ino) != (created.st_dev, created.st_ino)
                or stat.S_IMODE(secured.st_mode) != 0o700
            ):
                continue
            temp_root = _seal_directory(temp_root_path, immutable=False)
        except (OSError, RuntimeError):
            temp_root = None
        if temp_root is None or temp_root.mode != 0o700:
            continue

        return SealedGitRuntime(
            git=git,
            version=version,
            exec_path=exec_path,
            shell=shell,
            path=os.pathsep.join(item.path for item in path_seals),
            path_dirs=tuple(path_seals),
            temp_root=temp_root,
        )
    return None


def _git_runtime_is_current(runtime: SealedGitRuntime, *, rehash_git: bool) -> bool:
    return (
        _seal_matches_file(runtime.git, rehash=rehash_git)
        and _seal_matches_file(runtime.shell, rehash=False)
        and _seal_matches_directory(runtime.exec_path)
        and _seal_matches_directory(runtime.temp_root)
        and all(_seal_matches_directory(item) for item in runtime.path_dirs)
    )


def _safe_remove_directory(
    path: Path,
    *,
    device: int,
    inode: int,
    parent_device: int | None = None,
    parent_inode: int | None = None,
) -> None:
    """Best-effort removal anchored to the directory's current parent fd."""
    if not getattr(shutil.rmtree, "avoids_symlink_attacks", False):
        return
    parent_fd: int | None = None
    try:
        parent_fd = os.open(path.parent, _DIRECTORY_OPEN_FLAGS)
        opened_parent = os.fstat(parent_fd)
        if (
            parent_device is not None
            and parent_inode is not None
            and (opened_parent.st_dev, opened_parent.st_ino)
            != (parent_device, parent_inode)
        ):
            return
        current = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISDIR(current.st_mode)
            or (current.st_dev, current.st_ino) != (device, inode)
        ):
            return
        shutil.rmtree(path.name, dir_fd=parent_fd)
    except (OSError, RuntimeError):
        return
    finally:
        if parent_fd is not None:
            try:
                os.close(parent_fd)
            except OSError:
                pass


def _load_yaml(path: Path) -> Any:
    try:
        import yaml  # type: ignore

        with path.open("r", encoding="utf-8") as stream:
            return yaml.safe_load(stream)
    except Exception:
        raise WorkerCredentialError("worker credential manifest is malformed") from None


def _normalize_manifest(raw: Any, path: Path | None) -> WorkerCredentialManifest:
    if not isinstance(raw, dict):
        raise WorkerCredentialError("worker credential manifest is malformed")
    if set(raw) != {"version", "profiles"}:
        raise WorkerCredentialError("worker credential manifest has unsupported fields")
    version = raw.get("version")
    if version not in SUPPORTED_CONTRACT_VERSIONS or isinstance(version, bool):
        raise WorkerCredentialError("worker credential manifest version is unsupported")

    raw_profiles = raw.get("profiles")
    if not isinstance(raw_profiles, dict):
        raise WorkerCredentialError("worker credential manifest profiles are malformed")

    profiles: dict[str, tuple[str, ...]] = {}
    action_configs: dict[str, dict[str, dict[str, str]]] = {}
    for raw_name, raw_grant in raw_profiles.items():
        try:
            profile = _normalize_profile(raw_name)
        except WorkerCredentialError:
            raise WorkerCredentialError("worker credential profile grant is malformed") from None
        if profile in profiles:
            raise WorkerCredentialError("worker credential manifest has duplicate profiles")
        if not isinstance(raw_grant, dict) or set(raw_grant) != {"actions"}:
            raise WorkerCredentialError("worker credential profile grant is malformed")
        raw_actions = raw_grant.get("actions")
        normalized_configs: dict[str, dict[str, str]] = {}
        if version == LEGACY_CONTRACT_VERSION:
            if not isinstance(raw_actions, list) or any(
                not isinstance(action, str) or action not in CAPABILITIES
                for action in raw_actions
            ):
                raise WorkerCredentialError("worker credential capability is unsupported")
            if len(set(raw_actions)) != len(raw_actions):
                raise WorkerCredentialError("worker credential capability is duplicated")
            if any(
                CAPABILITIES[action].projection_kind == "controller_action_receipt"
                for action in raw_actions
            ):
                raise WorkerCredentialError(
                    "worker credential controller action requires contract version 2"
                )
            actions = list(raw_actions)
        else:
            if not isinstance(raw_actions, dict):
                raise WorkerCredentialError("worker credential v2 actions are malformed")
            actions = []
            for action, raw_config in raw_actions.items():
                if not isinstance(action, str) or action not in CAPABILITIES:
                    raise WorkerCredentialError(
                        "worker credential capability is unsupported"
                    )
                if not isinstance(raw_config, dict):
                    raise WorkerCredentialError(
                        "worker credential capability config is malformed"
                    )
                definition = CAPABILITIES[action]
                if definition.projection_kind == "controller_action_receipt":
                    if set(raw_config) != {"activation_sha256"}:
                        raise WorkerCredentialError(
                            "worker credential controller action config is malformed"
                        )
                    activation_sha256 = raw_config.get("activation_sha256")
                    if not isinstance(activation_sha256, str) or not re.fullmatch(
                        r"[0-9a-f]{64}", activation_sha256
                    ):
                        raise WorkerCredentialError(
                            "worker credential activation digest is malformed"
                        )
                    normalized_configs[action] = {
                        "activation_sha256": activation_sha256
                    }
                elif raw_config:
                    raise WorkerCredentialError(
                        "worker credential environment action config is unsupported"
                    )
                else:
                    normalized_configs[action] = {}
                actions.append(action)
        profiles[profile] = tuple(sorted(actions))
        action_configs[profile] = normalized_configs

    return WorkerCredentialManifest(
        version=version,
        profiles=profiles,
        digest=_manifest_digest(
            profiles,
            version=version,
            action_configs=action_configs,
        ),
        path=path,
        action_configs=action_configs,
    )


def load_manifest(root: Path | str | None = None) -> WorkerCredentialManifest:
    """Load and normalize the machine-global worker action manifest.

    A missing manifest is the migration-safe empty contract.  Once present,
    the file is closed-schema and fail-closed: malformed versions, fields,
    profiles, or capabilities never produce a partial grant.
    """
    manifest_root = Path(root) if root is not None else get_default_hermes_root()
    path = manifest_root / MANIFEST_FILENAME
    if not path.exists():
        return WorkerCredentialManifest(
            version=CONTRACT_VERSION,
            profiles={},
            digest=_manifest_digest({}, version=CONTRACT_VERSION),
            path=None,
            action_configs={},
        )
    return _normalize_manifest(_load_yaml(path), path)


def worker_credential_runtime_root(root: Path | str | None = None) -> Path:
    """Return the machine-global runtime directory for terminal artifacts."""
    base = Path(root) if root is not None else get_default_hermes_root()
    return base / WORKER_CREDENTIAL_RUNTIME_RELATIVE


def cleanup_stale_worker_credential_artifacts(
    root: Path | str | None = None,
    *,
    max_age_seconds: int = WORKER_CREDENTIAL_ARTIFACT_MAX_AGE_SECONDS,
) -> int:
    """Remove old run directories without failing worker startup.

    Terminal cleanup removes the current directory eagerly.  This bounded
    sweep handles crashes and dispatcher restarts.  Only generated ``run-``
    children below the exact runtime root are eligible.
    """
    runtime_root = worker_credential_runtime_root(root)
    if not runtime_root.is_dir() or runtime_root.is_symlink():
        return 0
    cutoff = time.time() - max(0, int(max_age_seconds))
    removed = 0
    try:
        children = list(runtime_root.iterdir())
    except OSError:
        return 0
    for child in children:
        if not child.name.startswith("run-"):
            continue
        try:
            if child.stat().st_mtime >= cutoff:
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def _worker_identity(environ: Mapping[str, str]) -> tuple[str, str, str] | None:
    """Extract the immutable worker identity needed for action projection."""
    raw_profile = str(environ.get("HERMES_PROFILE") or "").strip()
    task_id = str(environ.get("HERMES_KANBAN_TASK") or "").strip()
    run_id = str(environ.get("HERMES_KANBAN_RUN_ID") or "").strip()
    if not raw_profile or not task_id or not run_id:
        return None
    try:
        profile = _normalize_profile(raw_profile)
    except WorkerCredentialError:
        return None
    return profile, task_id, run_id


def bootstrap_worker_credential_context(
    environ: dict[str, str] | None = None,
) -> TrustedWorkerCredentialRuntime | None:
    """Consume the initial handoff and lock the worker's trusted state.

    A worker identity is required before any action value is admitted.  Once
    a Kanban worker has bootstrapped, later mutations of ``os.environ`` cannot
    create a new grant because this context is never rebuilt in-process.
    Non-worker processes remain uninitialized and therefore cannot project an
    action credential.
    """
    global _TRUSTED_WORKER_RUNTIME

    with _WORKER_CREDENTIAL_LOCK:
        if _TRUSTED_WORKER_RUNTIME is not None:
            return _TRUSTED_WORKER_RUNTIME

        target = os.environ if environ is None else environ
        identity = _worker_identity(target)
        # Derived from the registry, like the admission below and like
        # consume_worker_credential_handoff. As a literal pair this collected
        # github_write and the retired bws_bootstrap only, so BUILD-601's three
        # marketing handoffs were written by the controller, dropped here, and
        # then scrubbed — preflight logged them present while the worker's
        # terminal got nothing.
        handoff = {
            name: target.get(name)
            for name in _HANDOFF_ENV_TO_CAPABILITY
            if isinstance(target.get(name), str) and target.get(name)
        }
        if identity is None:
            # No Kanban worker identity means this is an ordinary Hermes
            # process. Do not consume a marker here; the unconditional child
            # scrubbing still removes it, and ordinary processes can never
            # project it.
            return None

        expected_digest = target.get(MANIFEST_DIGEST_ENV)

        # Consume every private handoff name, including an unknown future
        # name, before any terminal or hook subprocess can inherit it.
        for key in list(target):
            if key.startswith(PRIVATE_HANDOFF_PREFIX):
                target.pop(key, None)

        profile, task_id, run_id = identity
        try:
            manifest = load_manifest()
        except WorkerCredentialError:
            manifest = WorkerCredentialManifest(
                version=CONTRACT_VERSION,
                profiles={},
                digest=_manifest_digest({}, version=CONTRACT_VERSION),
                path=None,
                action_configs={},
            )

        manifest_verified = expected_digest == manifest.digest
        if not manifest_verified:
            _log.warning(
                "worker credential manifest digest mismatch; no capabilities admitted"
            )
            admitted: dict[str, str] = {}
        else:
            granted = manifest.actions_for(profile)
            admitted = {
                capability: handoff[definition.handoff_env]
                for capability in granted
                if capability in CAPABILITIES
                for definition in (CAPABILITIES[capability],)
                if definition.handoff_env in handoff
            }

        strip_env = worker_credential_strip_env()

        for key in strip_env:
            target.pop(key, None)

        raw_db_path = str(target.get("HERMES_KANBAN_DB") or "").strip()
        try:
            sealed_db = Path(raw_db_path).expanduser().resolve(strict=True)
            sealed_db_path = str(sealed_db) if sealed_db.is_file() else ""
        except (OSError, RuntimeError):
            sealed_db_path = ""

        runtime = TrustedWorkerCredentialRuntime(
            profile=profile,
            task_id=task_id,
            run_id=run_id,
            manifest_digest=manifest.digest,
            manifest_verified=manifest_verified,
            kanban_db_path=sealed_db_path,
            capabilities=tuple(sorted(admitted)),
            git_runtime=(
                _seal_git_runtime() if profile == "releaser" else None
            ),
            _values=tuple(sorted(admitted.items())),
        )
        _TRUSTED_WORKER_RUNTIME = runtime
        return runtime


def _runtime_matches_current_worker(
    runtime: TrustedWorkerCredentialRuntime,
    environ: Mapping[str, str],
) -> bool:
    current = _worker_identity(environ)
    if current != (runtime.profile, runtime.task_id, runtime.run_id):
        return False
    try:
        return load_manifest().digest == runtime.manifest_digest
    except WorkerCredentialError:
        return False


def trusted_worker_identity(
    *, environ: Mapping[str, str] | None = None
) -> tuple[str, int] | None:
    """Return the bootstrap-sealed task/run identity for this worker.

    Environment variables locate the initial worker, but later mutation cannot
    switch this identity to another task or run. Receipt-reader tools use this
    boundary instead of trusting mutable environment markers directly.
    """
    runtime = bootstrap_worker_credential_context()
    if runtime is None or not runtime.manifest_verified:
        return None
    current_env = os.environ if environ is None else environ
    if not _runtime_matches_current_worker(runtime, current_env):
        return None
    try:
        run_id = int(runtime.run_id)
    except ValueError:
        return None
    return (runtime.task_id, run_id) if run_id > 0 else None


def trusted_worker_receipt_context(
    *, environ: Mapping[str, str] | None = None
) -> tuple[str, int, str] | None:
    """Return task/run plus the bootstrap-sealed Kanban database path."""
    identity = trusted_worker_identity(environ=environ)
    runtime = _TRUSTED_WORKER_RUNTIME
    if identity is None or runtime is None or not runtime.kanban_db_path:
        return None
    return identity[0], identity[1], runtime.kanban_db_path


def get_trusted_worker_credential(
    capability: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """Return an action value only at the matching trusted worker boundary."""
    runtime = bootstrap_worker_credential_context()
    if runtime is None:
        return None
    current_env = os.environ if environ is None else environ
    if not _runtime_matches_current_worker(runtime, current_env):
        return None
    return runtime.value_for(capability)


def has_trusted_worker_action(
    capability: str, *, environ: Mapping[str, str] | None = None
) -> bool:
    """Return whether *capability* is currently trusted for this worker."""
    return get_trusted_worker_credential(capability, environ=environ) is not None


def _terminal_artifacts() -> tuple[Path, Path, Path]:
    """Create (run_dir, gh_config_dir, global_git_config) atomically enough."""
    global _TERMINAL_ARTIFACT_DIR

    with _WORKER_CREDENTIAL_LOCK:
        existing = _TERMINAL_ARTIFACT_DIR
        if existing is not None:
            gh_dir = existing / "gh-config"
            git_config = existing / "gitconfig"
            if existing.is_dir() and gh_dir.is_dir() and git_config.is_file():
                return existing, gh_dir, git_config

        runtime_root = worker_credential_runtime_root()
        runtime_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        runtime_root.chmod(0o700)
        run_dir = runtime_root / f"run-{secrets.token_hex(16)}"
        run_dir.mkdir(mode=0o700)
        run_dir.chmod(0o700)
        gh_dir = run_dir / "gh-config"
        gh_dir.mkdir(mode=0o700)
        gh_dir.chmod(0o700)
        git_config = run_dir / "gitconfig"
        git_config.write_text("[credential]\n\thelper =\n", encoding="utf-8")
        git_config.chmod(0o600)
        _TERMINAL_ARTIFACT_DIR = run_dir
        return run_dir, gh_dir, git_config


def project_worker_terminal_environment(
    environ: dict[str, str],
) -> bool:
    """Isolate worker terminals and project trusted action credentials.

    ``github_write`` is projected as ``GH_TOKEN`` plus a token-free git helper.
    Every other worker_environment capability is projected into the names its
    registry entry declares, after the sanitizer has stripped every ambient
    copy — so a granted worker uses the terminal projection and never the
    controller's own environment. Only a trusted worker receives any value.

    Nothing here projects the vault access token itself: BUILD-601 retired the
    transitional ``bws_bootstrap`` grant in favour of the three resolved
    marketing reads, so a worker can no longer re-derive secrets the manifest
    did not grant it.
    """
    runtime = bootstrap_worker_credential_context()
    if runtime is None:
        return False

    token = get_trusted_worker_credential("github_write", environ=os.environ)
    _run_dir, gh_config_dir, git_config = _terminal_artifacts()
    # Remove ambient command-scope config knobs before installing the exact
    # two entries below.  This is configuration isolation, not a credential
    # source, so it belongs after the general environment sanitization.
    for key in list(environ):
        if (
            key in {"GIT_CONFIG_PARAMETERS", "GIT_CONFIG_SYSTEM"}
            or key == "GIT_CONFIG_COUNT"
            or key.startswith("GIT_CONFIG_KEY_")
            or key.startswith("GIT_CONFIG_VALUE_")
        ):
            environ.pop(key, None)
    environ.pop("GH_TOKEN", None)
    environ["GH_CONFIG_DIR"] = str(gh_config_dir)
    environ["GIT_CONFIG_NOSYSTEM"] = "1"
    environ["GIT_CONFIG_GLOBAL"] = str(git_config)
    environ["GIT_CONFIG_COUNT"] = "2" if token is not None else "1"
    environ["GIT_CONFIG_KEY_0"] = "credential.helper"
    environ["GIT_CONFIG_VALUE_0"] = ""
    if token is not None:
        environ["GH_TOKEN"] = token
        environ["GIT_CONFIG_KEY_1"] = "credential.helper"
        environ["GIT_CONFIG_VALUE_1"] = GIT_ENV_TOKEN_HELPER
    # Every other worker_environment capability projects its resolved value
    # into the names the registry declares. ``github_write`` is handled above
    # instead of here because its projection is paired with the git
    # credential-helper config written into GIT_CONFIG_KEY_1.
    for name, definition in CAPABILITIES.items():
        if name == "github_write" or definition.projection_kind != "worker_environment":
            continue
        value = get_trusted_worker_credential(name, environ=os.environ)
        if value is None:
            continue
        for projected in definition.projection_env:
            environ[projected] = value
    return True


# Kept as a compatibility alias for callers that used the old, grant-specific
# name. Worker isolation now applies to denied workers too.
project_github_write_terminal_environment = project_worker_terminal_environment


def cleanup_worker_terminal_artifacts() -> bool:
    """Remove the current terminal run directory; safe to call repeatedly."""
    global _TERMINAL_ARTIFACT_DIR

    with _WORKER_CREDENTIAL_LOCK:
        run_dir = _TERMINAL_ARTIFACT_DIR
        _TERMINAL_ARTIFACT_DIR = None
    if run_dir is None:
        return False
    try:
        if run_dir.is_dir() and not run_dir.is_symlink():
            shutil.rmtree(run_dir)
            return True
    except OSError:
        pass
    return False


def reset_worker_credential_context_for_tests() -> None:
    """Reset process-local state for isolated unit tests."""
    global _TRUSTED_WORKER_RUNTIME, _TERMINAL_ARTIFACT_DIR

    with _WORKER_CREDENTIAL_LOCK:
        runtime = _TRUSTED_WORKER_RUNTIME
        _TRUSTED_WORKER_RUNTIME = None
        _TERMINAL_ARTIFACT_DIR = None
        _TRUSTED_WORKER_CREDENTIALS.clear()
        _PUBLICATION_READBACK_CALLS.clear()
        _PUBLICATION_CREDENTIAL_ATTEMPTS.clear()
    git_runtime = runtime.git_runtime if runtime is not None else None
    if git_runtime is not None and _seal_matches_directory(git_runtime.temp_root):
        temp_root = Path(git_runtime.temp_root.path)
        if temp_root.name.startswith(_TRUSTED_TEMP_ROOT_PREFIX):
            _safe_remove_directory(
                temp_root,
                device=git_runtime.temp_root.device,
                inode=git_runtime.temp_root.inode,
            )


@contextmanager
def _controller_bootstrap_environment(
    access_token_env: str, access_token: str
) -> Iterator[None]:
    """Make the controller bootstrap visible only while the adapter fetches."""
    previous = os.environ.get(access_token_env)
    os.environ[access_token_env] = access_token
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(access_token_env, None)
        else:
            os.environ[access_token_env] = previous


def _bitwarden_config(home_path: Path) -> dict[str, Any] | None:
    try:
        from hermes_cli.env_loader import _load_secrets_config

        secrets_cfg = _load_secrets_config(home_path)
    except Exception:
        return None
    if not isinstance(secrets_cfg, dict):
        return None
    config = secrets_cfg.get("bitwarden")
    return config if isinstance(config, dict) else None


def worker_credential_strip_env(
    root: Path | str | None = None,
) -> frozenset[str]:
    """Return environment names that must be stripped from workers."""
    try:
        source_config = _bitwarden_config(
            get_default_hermes_root() if root is None else Path(root)
        ) or {}
        configured_access_token_env = (
            source_config.get("access_token_env") or BWS_BOOTSTRAP_ENV
        )
        strip_env = set(UNCONDITIONAL_STRIP_ENV)
        strip_env.update(CAPABILITY_SENSITIVE_ENV)
        if (
            isinstance(configured_access_token_env, str)
            and configured_access_token_env
        ):
            strip_env.add(configured_access_token_env)
        return frozenset(strip_env)
    except Exception:
        return frozenset(
            {
                *UNCONDITIONAL_STRIP_ENV,
                *CAPABILITY_SENSITIVE_ENV,
            }
        )


def _fetch_bitwarden_result(
    *,
    home_path: Path,
    access_token_env: str,
    access_token: str,
    source_config: Mapping[str, Any],
) -> Any:
    """Fetch through the registered Bitwarden adapter without exposing values."""
    try:
        from agent.secret_sources.base import FetchResult
        from agent.secret_sources.registry import get_source

        source = get_source("bitwarden")
        if source is None or getattr(source, "name", "") != "bitwarden":
            return FetchResult(error="source unavailable")
        with _controller_bootstrap_environment(access_token_env, access_token):
            result = source.fetch(dict(source_config), home_path)
        return result
    except Exception:
        # Adapter implementations must not raise, but a plugin/test double
        # must not be able to turn a secret into an exception or traceback.
        try:
            from agent.secret_sources.base import FetchResult

            return FetchResult(error="source fetch failed")
        except Exception:
            return None


def _safe_source_result(result: Any, required_key: str) -> tuple[str | None, str | None]:
    """Return one selected source value and a fixed diagnostic category."""
    if result is None:
        return None, "source_unavailable"
    if getattr(result, "error", None):
        return None, "source_failed"
    secrets = getattr(result, "secrets", None)
    if not isinstance(secrets, dict):
        return None, "source_failed"
    value = secrets.get(required_key)
    if not isinstance(value, str) or not value:
        return None, "secret_missing"
    return value, None


# Owner and repository of a GitHub remote, in the URL shapes git accepts:
# ``scheme://[user[:pass]@]github.com[:port]/owner/repo[.git]`` and the
# scp-style ``[user@]github.com:owner/repo[.git]``.
#
# Anchored at BOTH ends, and ``github.com`` must be the whole host -- matching
# it anywhere in the string would accept ``https://evil.example/github.com/o/r``
# and ``https://notgithub.com/o/r`` and hand a real GitHub credential to a
# worker whose remote points somewhere else entirely. The path must be exactly
# ``owner/repo``; a deeper path is not a repository URL and yields no owner.
_GITHUB_REMOTE_OWNER_RE = re.compile(
    r"(?:[A-Za-z][A-Za-z0-9+.-]*://)?"      # optional scheme
    r"(?:[^/@\s]*@)?"                       # optional user[:password]@
    r"github\.com"                          # the host, and only this host
    r"(?::\d+)?"                            # optional port
    r"[:/]"                                 # scp-style ':' or a path '/'
    r"(?P<owner>[A-Za-z0-9][A-Za-z0-9-]{0,38})/"
    r"(?P<repo>[^/\s]+?)(?:\.git)?/?"
)


def github_write_resolve_key(owner: str | None) -> str | None:
    """Return the BWS secret backing ``github_write`` for ``owner``."""
    if not owner:
        return None
    return GITHUB_WRITE_RESOLVE_KEYS.get(owner.strip().casefold())


@dataclass(frozen=True)
class _PublicationPolicy:
    status: str
    allowed_owners: frozenset[str] = frozenset()
    denied_repos: Mapping[str, str] = field(default_factory=dict)


_POLICY_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_POLICY_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")


def _canonical_repo_slug(value: Any) -> tuple[str, str, str] | None:
    if not isinstance(value, str) or value != value.strip() or value.count("/") != 1:
        return None
    owner, repo = value.split("/", 1)
    if not _POLICY_OWNER_RE.fullmatch(owner) or not _POLICY_REPO_RE.fullmatch(repo):
        return None
    if repo in {".", ".."} or repo.casefold().endswith(".git"):
        return None
    return owner, repo, f"{owner}/{repo}"


def _load_publication_policy(
    profile: str, root: Path | str | None = None
) -> _PublicationPolicy:
    """Parse the versioned publication policy once, preserving tri-state."""
    base = Path(root) if root is not None else get_default_hermes_root()
    rules = base / "profiles" / _normalize_profile(profile) / "rules"
    try:
        payload = json.loads(
            (rules / "pr-target-repo-allowlist.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return _PublicationPolicy("unavailable")
    if not isinstance(payload, dict) or set(payload) != {
        "version",
        "allowed_owners",
        "denied_repos",
    }:
        return _PublicationPolicy("unavailable")
    if payload.get("version") != 1 or isinstance(payload.get("version"), bool):
        return _PublicationPolicy("unavailable")
    owners = payload.get("allowed_owners")
    denied = payload.get("denied_repos")
    if (
        not isinstance(owners, list)
        or not owners
        or not isinstance(denied, dict)
        or any(not isinstance(item, str) or not _POLICY_OWNER_RE.fullmatch(item) for item in owners)
        or len({item.casefold() for item in owners}) != len(owners)
    ):
        return _PublicationPolicy("unavailable")
    normalized_denied: dict[str, str] = {}
    for slug, reason in denied.items():
        parsed = _canonical_repo_slug(slug)
        if parsed is None or not isinstance(reason, str) or not reason.strip():
            return _PublicationPolicy("unavailable")
        normalized_denied[parsed[2].casefold()] = reason
    return _PublicationPolicy(
        "allowed",
        allowed_owners=frozenset(item.casefold() for item in owners),
        denied_repos=normalized_denied,
    )


def denied_publication_repos(
    profile: str, root: Path | str | None = None
) -> dict[str, str]:
    """Compatibility fail-open view over the canonical strict policy parser."""
    policy = _load_publication_policy(profile, root)
    return dict(policy.denied_repos) if policy.status == "allowed" else {}


def publication_policy_decision_for_repo(
    profile: str,
    repo: str | None,
    root: Path | str | None = None,
) -> str:
    """Return the canonical allowed/denied/unavailable policy decision."""
    policy = _load_publication_policy(profile, root)
    parsed = _canonical_repo_slug(repo)
    if policy.status != "allowed" or parsed is None:
        return "unavailable" if policy.status != "allowed" else "denied"
    if (
        parsed[0].casefold() not in policy.allowed_owners
        or parsed[2].casefold() in policy.denied_repos
    ):
        return "denied"
    return "allowed"


def vault_sourced_capabilities(capabilities: tuple[str, ...]) -> tuple[str, ...]:
    """Granted capabilities whose value must be fetched from the secret vault.

    Derived from the code-owned registry rather than a hand-kept list, so
    adding a capability does not create a second place to update (BUILD-601 --
    before it, this was the literal ``"github_write" in capabilities``).

    Controller-action capabilities are excluded -- they never project into a
    worker at all.
    """
    resolved: list[str] = []
    for capability in capabilities:
        definition = CAPABILITIES.get(capability)
        if definition is None or definition.projection_kind != "worker_environment":
            continue
        keys = tuple(
            key for key in (definition.source_key, *definition.source_keys) if key
        )
        if not keys or keys == (BWS_BOOTSTRAP_ENV,):
            continue
        resolved.append(capability)
    return tuple(resolved)


def _github_remote_match(
    workspace: Path | str | None, *, remote: str = "origin"
) -> "re.Match[str] | None":
    """Match ``remote``'s push url in ``workspace`` against the GitHub pattern.

    One probe behind both public helpers, so the owner and the repo can never
    be read from two different urls.
    """
    if not workspace:
        return None
    remote_name = (remote or "origin").strip() or "origin"
    if remote_name.startswith("-"):
        # A remote name is task-controlled; never let one become a git flag.
        return None
    # An ambient GIT_DIR / GIT_WORK_TREE / GIT_CONFIG_* in the dispatcher's own
    # environment would silently point this probe at a different repository or
    # inject config into it, so the probe answers about a repo the worker will
    # never touch. Scrub the whole namespace rather than enumerate it.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    # Re-add the two safety vars after the scrub: GIT_CONFIG_NOSYSTEM prevents
    # the system gitconfig from being read (blocked in sandbox environments),
    # GIT_TERMINAL_PROMPT prevents interactive credential prompts.
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    # Prevent git from walking above the workspace directory when searching
    # for a .git entry; without this, a non-repo workspace inside a larger
    # git checkout silently resolves the parent repo's remote instead of
    # returning "not a git repository".
    env["GIT_CEILING_DIRECTORIES"] = str(Path(str(workspace)).parent)
    try:
        completed = subprocess.run(
            ["git", "-C", str(workspace), "remote", "get-url", "--push", remote_name],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return _GITHUB_REMOTE_OWNER_RE.fullmatch((completed.stdout or "").strip())


def github_owner_for_workspace(
    workspace: Path | str | None, *, remote: str = "origin"
) -> str | None:
    """GitHub owner the worker in ``workspace`` would publish to, or ``None``.

    BUILD-603: ``github_write`` is owner-scoped, so the dispatcher has to know
    which repository a task publishes to before it can pick a token. The
    workspace's own remote URL is the only per-task repo context available at
    spawn time -- ``tasks.publication_remote`` is a git *remote name*
    (``origin`` is its only live value), not an owner.

    Never raises: a workspace that is not a git repository, has no such remote,
    or points at a non-GitHub host simply has no owner, and the caller fails
    closed. Never logs the URL either -- a remote can carry an embedded
    credential (``https://x-access-token:<token>@github.com/...``).

    Reads the PUSH url. A remote may carry a ``pushurl`` distinct from its
    fetch url (the fetch-upstream / push-fork setup), and it is where the
    worker's push actually lands that decides which token is the right one.
    ``--push`` falls back to the fetch url when no ``pushurl`` is configured.
    """
    match = _github_remote_match(workspace, remote=remote)
    return match.group("owner") if match else None


def github_release_target_for_workspace(
    workspace: Path | str | None, *, remote: str = "origin"
) -> tuple[str | None, str | None]:
    """``(owner, "owner/repo")`` for ``workspace``'s push url, from ONE probe.

    The dispatcher needs both: the owner selects the token, the full slug is
    checked against the deny list. Reading them with two separate `git` calls
    would leave a window in which the workspace's own `.git/config` — which the
    previous worker in that worktree can write — changes between them, so the
    deny check and the token selection could describe different repositories.
    That is the exact class of steering BUILD-795 exists to close, so both come
    off a single invocation.
    """
    match = _github_remote_match(workspace, remote=remote)
    if match is None:
        return None, None
    return match.group("owner"), f"{match.group('owner')}/{match.group('repo')}"


def github_repo_for_workspace(
    workspace: Path | str | None, *, remote: str = "origin"
) -> str | None:
    """``owner/repo`` the worker in ``workspace`` would push to, or ``None``.

    Same probe and same guarantees as :func:`github_owner_for_workspace` — it
    just keeps the repository half, which the deny list needs and an owner
    cannot express (BUILD-795: `Ahlnos-Inc/hermes-config` is abandoned while
    other `Ahlnos-Inc` repositories are live).
    """
    match = _github_remote_match(workspace, remote=remote)
    return f"{match.group('owner')}/{match.group('repo')}" if match else None


def resolve_worker_credentials(
    profile: str,
    *,
    root: Path | str | None = None,
    base_env: Mapping[str, str] | None = None,
    run_id: int | str | None = None,
    manifest: WorkerCredentialManifest | None = None,
    github_owner: str | None = None,
    github_repo: str | None = None,
    expected_github_repo: str | None = None,
) -> WorkerCredentialPlan:
    """Resolve the closed grants for one worker without leaking secrets.

    Ambient ``GH_TOKEN``, ``GITHUB_TOKEN``, and ``GH_TOKEN_SECRET_WRITE`` are
    never considered valid action sources.  ``github_write`` is satisfied only
    by the selected value returned by the Bitwarden source adapter, and only
    for a ``github_owner`` that has a registered release-target token.
    """
    cleanup_stale_worker_credential_artifacts()
    normalized_profile = _normalize_profile(profile)
    loaded_manifest = manifest or load_manifest(root)
    capabilities = loaded_manifest.actions_for(normalized_profile)
    env = dict(os.environ if base_env is None else base_env)
    source_config = _bitwarden_config(Path(root) if root is not None else get_default_hermes_root())
    access_token_env = str((source_config or {}).get("access_token_env") or BWS_BOOTSTRAP_ENV)
    bootstrap = str(env.get(access_token_env) or "").strip()
    vault_capabilities = vault_sourced_capabilities(capabilities)
    needs_bitwarden = bool(vault_capabilities)
    # The bootstrap token is the CONTROLLER's key to the vault, needed only to
    # perform the fetch. No worker receives it (BUILD-601 retired the last
    # grant that did), so needing it is now exactly needing a fetch.
    needs_bootstrap = needs_bitwarden
    statuses: list[str] = []
    handoff: dict[str, str] = {}
    source = "bitwarden" if needs_bitwarden else None
    strip_env = set(UNCONDITIONAL_STRIP_ENV)
    strip_env.update(CAPABILITY_SENSITIVE_ENV)
    strip_env.add(access_token_env)

    github_resolve_key: str | None = None
    if "github_write" in vault_capabilities:
        # BUILD-795: the owner-level token is coarser than the repo policy.
        # `rules/pr-target-repo-allowlist.json` denies a repository by name,
        # but that rule is enforced by a `gh` hook and a plain `git push`
        # through the projected credential helper never reaches it. Check the
        # same list here, before a token is selected, so a denied repository
        # gets none. Denial precedes owner resolution: a denied repo must not
        # be reportable as a mere owner problem.
        denied = denied_publication_repos(normalized_profile, root)
        denied_reason = denied.get((github_repo or "").strip().casefold())
        if denied_reason:
            statuses.append(f"github_write_repo={github_repo} denied")
            return WorkerCredentialPlan(
                profile=normalized_profile,
                manifest_digest=loaded_manifest.digest,
                capabilities=capabilities,
                diagnostics=tuple([*statuses, "github_write=denied_repo"]),
                error=(
                    "worker credential preflight refuses a GitHub write token: "
                    f"the workspace publishes to {github_repo!r}, which is "
                    f"denied as a release target ({denied_reason})"
                ),
            )
        # BUILD-795 AC2: the task row states which repository this card may
        # publish to. The workspace's push url comes out of its own
        # .git/config, which every worker that ran in that worktree could
        # write, so the two must AGREE before a token is chosen. Fail closed:
        # a recorded target with no resolvable workspace repo is a mismatch,
        # not a pass. This can only ever REFUSE — the token is still selected
        # from the probed owner, never from the recorded target — so recording
        # a target can narrow a card's reach and never widen it.
        expected_slug = str(expected_github_repo or "").strip().casefold()
        if expected_slug and expected_slug != (github_repo or "").strip().casefold():
            statuses.append(f"github_write_target={expected_github_repo}")
            return WorkerCredentialPlan(
                profile=normalized_profile,
                manifest_digest=loaded_manifest.digest,
                capabilities=capabilities,
                diagnostics=tuple([*statuses, "github_write=target_mismatch"]),
                error=(
                    "worker credential preflight refuses a GitHub write token: "
                    f"the task records release target {expected_github_repo!r} "
                    "but the workspace publishes to "
                    f"{github_repo or 'no github.com repository'!r}"
                ),
            )
        # Owner selection is pure policy and needs no vault, so it runs before
        # the bootstrap and source checks: an unresolvable owner is reported as
        # itself rather than as a missing secret.
        github_resolve_key = github_write_resolve_key(github_owner)
        statuses.append(f"github_write_owner={github_owner or 'unresolved'}")
        if github_resolve_key is None:
            plan = WorkerCredentialPlan(
                profile=normalized_profile,
                manifest_digest=loaded_manifest.digest,
                capabilities=capabilities,
                diagnostics=tuple([*statuses, "github_write=missing"]),
                error=(
                    "worker credential preflight cannot select a GitHub write "
                    "token: "
                    + (
                        f"no release-target token is registered for owner "
                        f"{github_owner!r}"
                        if github_owner
                        # Deliberately enumerated: owner=None collapses four
                        # distinct causes, and naming only one of them sends
                        # the operator to check the wrong thing.
                        else "the task workspace yielded no GitHub release "
                        "target -- it is not a git repository, has no such "
                        "remote, its push url is not a github.com repository, "
                        "or the git probe failed (give the task a "
                        "worktree/dir workspace on the repository it "
                        "publishes to)"
                    )
                ),
                source="bitwarden",
                _strip_env=frozenset(strip_env),
            )
            _log_preflight(plan, run_id)
            return plan

    if needs_bootstrap:
        # The CONTROLLER's key to the vault, not a worker grant -- naming it
        # after the retired capability made a controller-side fetch failure
        # read as a worker holding a vault token.
        statuses.append(f"vault_bootstrap={'present' if bootstrap else 'missing'}")
        if not bootstrap:
            plan = WorkerCredentialPlan(
                profile=normalized_profile,
                manifest_digest=loaded_manifest.digest,
                capabilities=capabilities,
                diagnostics=tuple(statuses),
                error="worker credential preflight missing BWS bootstrap",
                source=source,
                _strip_env=frozenset(strip_env),
            )
            _log_preflight(plan, run_id)
            return plan

    if needs_bitwarden:
        if not isinstance(source_config, dict) or not source_config.get("enabled"):
            plan = WorkerCredentialPlan(
                profile=normalized_profile,
                manifest_digest=loaded_manifest.digest,
                capabilities=capabilities,
                diagnostics=tuple(
                    [*statuses, *(f"{name}=missing" for name in vault_capabilities)]
                ),
                error="worker credential preflight Bitwarden source is unavailable",
                source="bitwarden",
                _strip_env=frozenset(strip_env),
            )
            _log_preflight(plan, run_id)
            return plan
        result = _fetch_bitwarden_result(
            home_path=Path(root) if root is not None else get_default_hermes_root(),
            access_token_env=access_token_env,
            access_token=bootstrap,
            source_config=source_config,
        )
        # One fetch, then one selection per granted capability. Every grant
        # must resolve or the worker does not start: a partially-credentialed
        # worker fails later, further from the cause, and with a live task
        # already claimed.
        for capability in vault_capabilities:
            definition = CAPABILITIES[capability]
            if capability == "github_write":
                assert github_resolve_key is not None  # set above for this grant
                resolve_key = github_resolve_key
            else:
                resolve_key = definition.source_key or ""
            value, failure = _safe_source_result(result, resolve_key)
            if value is None:
                statuses.append(f"{capability}=missing")
                plan = WorkerCredentialPlan(
                    profile=normalized_profile,
                    manifest_digest=loaded_manifest.digest,
                    capabilities=capabilities,
                    diagnostics=tuple(statuses),
                    error=(
                        "worker credential preflight Bitwarden source failed"
                        if failure != "secret_missing"
                        else (
                            "worker credential preflight GitHub write secret "
                            "is missing"
                            if capability == "github_write"
                            else f"worker credential preflight secret for "
                            f"{capability} is missing"
                        )
                    ),
                    source="bitwarden",
                    _strip_env=frozenset(strip_env),
                )
                _log_preflight(plan, run_id)
                return plan
            statuses.append(f"{capability}=present")
            if definition.handoff_env:
                handoff[definition.handoff_env] = value

    plan = WorkerCredentialPlan(
        profile=normalized_profile,
        manifest_digest=loaded_manifest.digest,
        capabilities=capabilities,
        diagnostics=tuple(statuses),
        source=source,
        _handoff=tuple(sorted(handoff.items())),
        _strip_env=frozenset(strip_env),
    )
    _log_preflight(plan, run_id)
    return plan


def _log_preflight(plan: WorkerCredentialPlan, run_id: int | str | None) -> None:
    statuses = ",".join(plan.diagnostics) if plan.diagnostics else "none"
    _log.info(
        "worker credential preflight profile=%s run_id=%s manifest_digest=%s "
        "source=%s status=%s",
        plan.profile,
        run_id if run_id is not None else "-",
        plan.manifest_digest,
        plan.source or "none",
        statuses,
    )


def prepare_worker_credentials(
    profile: str,
    *,
    root: Path | str | None = None,
    base_env: Mapping[str, str] | None = None,
    run_id: int | str | None = None,
    manifest: WorkerCredentialManifest | None = None,
    github_owner: str | None = None,
    github_repo: str | None = None,
    expected_github_repo: str | None = None,
) -> WorkerCredentialPlan:
    """Resolve grants and raise a safe error when the worker cannot start."""
    return resolve_worker_credentials(
        profile,
        root=root,
        base_env=base_env,
        run_id=run_id,
        manifest=manifest,
        github_owner=github_owner,
        github_repo=github_repo,
        expected_github_repo=expected_github_repo,
    ).require_ok()


@lru_cache(maxsize=1)
def model_provider_control_plane_env() -> frozenset[str]:
    """Env names carrying a model provider's OWN API credential.

    BUILD-681 strips vault-sourced variables from workers, but a worker still
    has to authenticate to the model it runs on, so this is the one class that
    survives. It is derived from two code-owned catalogs rather than a
    hand-maintained list, so adding a provider does not silently create a
    fourth place to update:

    * ``hermes_cli.auth.PROVIDER_REGISTRY`` — ``api_key_env_vars`` and
      ``base_url_env_var`` for every provider with an auth flow.
    * ``hermes_cli.config.OPTIONAL_ENV_VARS`` entries whose ``category`` is
      ``"provider"``.

    Plus the three Anthropic aliases that ``credential_pool`` special-cases
    (the registry entry lists only one of them).

    Names absent from both catalogs are deliberately NOT rescued. Two that
    look like they should be, and are not: ``oMLX_API_KEY`` (omlx-local is
    configured ``api_key: no-key-required`` and never reads it) and
    ``GROQ_API_KEY`` (speech-to-text, not a dispatcher model provider).
    """
    names = {"ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"}
    try:
        from hermes_cli.auth import PROVIDER_REGISTRY

        for definition in PROVIDER_REGISTRY.values():
            names.update(getattr(definition, "api_key_env_vars", ()) or ())
            base_url_env = getattr(definition, "base_url_env_var", None)
            if base_url_env:
                names.add(base_url_env)
    except Exception:  # noqa: BLE001 — a catalog import must never block a spawn
        pass
    try:
        from hermes_cli.config import OPTIONAL_ENV_VARS

        names.update(
            name
            for name, meta in OPTIONAL_ENV_VARS.items()
            if isinstance(meta, Mapping) and meta.get("category") == "provider"
        )
    except Exception:  # noqa: BLE001
        pass
    return frozenset(names)


def ambient_vault_strip_env(plan: WorkerCredentialPlan) -> frozenset[str]:
    """Vault-sourced names this worker must not inherit ambiently.

    Everything the process pulled from the secret vault, minus the model
    provider control plane, minus whatever this plan's capabilities grant.
    Capability grants are projected explicitly elsewhere in this module; they
    are excluded here only so a grant and this strip cannot contradict.
    """
    try:
        from hermes_cli.env_loader import externally_sourced_env_names

        vault = externally_sourced_env_names()
    except Exception:  # noqa: BLE001 — never block a spawn on provenance
        return frozenset()
    if not vault:
        return frozenset()
    granted: set[str] = set()
    for capability in plan.capabilities:
        definition = CAPABILITIES.get(capability)
        if definition is None:
            continue
        if definition.source_key:
            granted.add(definition.source_key)
        granted.update(definition.source_keys)
        granted.update(definition.projection_env)
    return frozenset(vault - model_provider_control_plane_env() - granted)


def build_worker_environment(
    base_env: Mapping[str, str], plan: WorkerCredentialPlan
) -> dict[str, str]:
    """Sanitize a worker environment, then add only authorized handoffs.

    BUILD-681: the base environment is the *gateway's* ``os.environ``, which
    carries every secret the vault applied at startup (142 on this install).
    Keeping everything except a 13-name blocklist meant ~129 controller
    secrets — AWS keys, database URLs, Jira tokens, R2 keys, VPS SSH keys —
    reached every worker regardless of its manifest, which made the manifest
    a description rather than a control. Vault-sourced names are now dropped
    by default and re-added only by capability or provider class.
    """
    stripped_vault = ambient_vault_strip_env(plan)
    env = {
        key: value
        for key, value in base_env.items()
        if key not in plan._strip_env
        and key not in stripped_vault
        and not key.startswith(PRIVATE_HANDOFF_PREFIX)
    }
    if stripped_vault:
        # Names only, never values. A worker that silently loses a variable it
        # was relying on is undiagnosable; this line is what makes the
        # resulting failure attributable to the tightening rather than to the
        # worker's own code.
        _log.info(
            "worker credentials: profile=%s manifest=%s withheld %d "
            "vault-sourced variable(s) not granted by the manifest: %s",
            plan.profile,
            plan.manifest_digest,
            len(stripped_vault),
            ", ".join(sorted(stripped_vault)),
        )
    for key, value in plan._handoff:
        env[key] = value
    env[MANIFEST_DIGEST_ENV] = plan.manifest_digest
    return env


def consume_worker_credential_handoff(
    environ: dict[str, str] | None = None,
) -> ConsumedWorkerCredentials:
    """Consume private handoffs into process-local state and delete them.

    The default target is the worker's actual ``os.environ``.  Tests and
    bootstrap wrappers may pass a mapping explicitly.  Unknown private names
    are stripped too, so adding a handoff name without adding a consumer never
    leaks it to a later shell hook or subprocess.
    """
    target = os.environ if environ is None else environ
    consumed: dict[str, str] = {}
    for key in list(target):
        if not key.startswith(PRIVATE_HANDOFF_PREFIX):
            continue
        value = target.pop(key, None)
        # Derived from the registry, so a capability can never be added with a
        # handoff name that has no consumer -- the case this function's
        # docstring warns about, previously prevented only by remembering to
        # edit a literal here too.
        capability = _HANDOFF_ENV_TO_CAPABILITY.get(key)
        if capability and isinstance(value, str) and value:
            consumed[capability] = value

    if consumed:
        with _WORKER_CREDENTIAL_LOCK:
            _TRUSTED_WORKER_CREDENTIALS.update(consumed)
    return ConsumedWorkerCredentials(
        capabilities=tuple(sorted(consumed)),
        _values=tuple(sorted(consumed.items())),
    )


def get_consumed_worker_credential(capability: str) -> str | None:
    """Return a consumed value for an internal authorized boundary."""
    with _WORKER_CREDENTIAL_LOCK:
        return _TRUSTED_WORKER_CREDENTIALS.get(capability)


def render_worker_credential_audit(root: Path | str | None = None) -> str:
    """Render the read-only credential audit. Names only, never values.

    BUILD-681 AC5. Joins the code-owned capability registry with the live
    manifest so "which profile can reach which secret" is answerable from one
    view instead of inferred from two files that can disagree. Deliberately
    an operator command, not a model tool.
    """
    manifest = load_manifest(root=root)
    header = (
        "profile", "capability", "source", "source key", "projection",
        "scope", "manifest digest",
    )
    rows: list[tuple[str, ...]] = []
    for profile in sorted(manifest.profiles):
        capabilities = manifest.actions_for(profile)
        if not capabilities:
            rows.append(
                (profile, "(none)", "-", "-", "-", "no action plane", manifest.digest)
            )
            continue
        for capability in capabilities:
            # No unknown-capability branch: load_manifest already rejects a
            # grant naming a capability the code registry does not define, in
            # both v1 and v2, so a loaded manifest cannot carry one.
            definition = CAPABILITIES[capability]
            source_keys = tuple(
                key for key in (definition.source_key, *definition.source_keys) if key
            )
            projection = tuple(
                name
                for name in (definition.projection_env or (definition.handoff_env,))
                if name
            )
            scope = definition.operation or definition.projection_kind
            if definition.api_major:
                scope = f"{scope} ({definition.api_major})"
            if definition.selected_by:
                # Otherwise the row lists every candidate key and reads as
                # though the worker receives all of them at once.
                scope = f"{scope}, 1 of {len(source_keys)} by {definition.selected_by}"
            rows.append(
                (
                    profile,
                    capability,
                    "controller-resolved",
                    ", ".join(source_keys) or "-",
                    ", ".join(projection) or "-",
                    scope,
                    manifest.digest,
                )
            )

    widths = [
        max(len(str(row[index])) for row in (header, *rows))
        for index in range(len(header))
    ]

    def _row(cells: tuple[str, ...]) -> str:
        return " | ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(cells))

    lines = [_row(header), "-+-".join("-" * width for width in widths)]
    lines.extend(_row(row) for row in rows)

    try:
        from hermes_cli.env_loader import externally_sourced_env_names

        vault = externally_sourced_env_names()
    except Exception:  # noqa: BLE001
        vault = frozenset()
    # UNCONDITIONAL_STRIP_ENV outranks the provider allowlist: GH_TOKEN and
    # GITHUB_TOKEN are provider credentials for `copilot` AND the GitHub write
    # path, and the capability projection is the only way they may reach a
    # worker. Reporting them as ambient would be a false clean bill.
    provider = sorted(
        vault & model_provider_control_plane_env() - UNCONDITIONAL_STRIP_ENV
    )
    withheld = sorted(
        vault - model_provider_control_plane_env() - UNCONDITIONAL_STRIP_ENV
    )
    capability_only = sorted(vault & UNCONDITIONAL_STRIP_ENV)
    lines += [
        "",
        f"Vault-sourced variables visible to this controller: {len(vault)}",
        f"  reaching every worker (model provider control plane, {len(provider)}): "
        + (", ".join(provider) or "(none)"),
        f"  stripped always, reachable only by capability projection "
        f"({len(capability_only)}): " + (", ".join(capability_only) or "(none)"),
        f"  withheld from workers ({len(withheld)}): "
        + (", ".join(withheld) or "(none)"),
        "  Profile-local .env is a separate, profile-owned control plane that "
        "this boundary does not cover — enumerated below (BUILD-789).",
        "",
    ]
    lines += render_skipped_secret_audit()
    lines.append("")
    lines += render_profile_local_env_audit(root=root)
    return "\n".join(lines)


def render_skipped_secret_audit() -> list[str]:
    """List vault secrets a source declined to apply, with the reason.

    A secret that never became an environment variable is invisible in every
    other view: it is present in the vault, the startup banner reports a
    healthy "applied N", and a capability that resolves by env-var name keeps
    using whatever it used before (BUILD-793).
    """
    try:
        from hermes_cli.env_loader import skipped_secret_names

        skips = skipped_secret_names()
    except Exception:  # noqa: BLE001
        skips = {}
    if not skips:
        return ["Vault secrets not applied to the environment: none"]
    out = [f"Vault secrets NOT applied to the environment ({len(skips)}):"]
    by_reason: dict[str, list[str]] = {}
    for name, reason in skips.items():
        by_reason.setdefault(reason, []).append(name)
    for reason, names in sorted(by_reason.items()):
        out.append(f"  {reason}: " + ", ".join(sorted(names)))
    return out


def render_profile_local_env_audit(root: Path | str | None = None) -> list[str]:
    """List credential NAMES each profile's own ``.env`` carries. Never values.

    The strip boundary only governs what a worker INHERITS. A profile whose own
    ``.env`` holds a credential hands it to every worker spawned for that
    profile regardless, and if that credential is a vault access token the
    worker can re-pull the entire vault (BUILD-789 — five profiles did, four of
    them because their ``.env`` was a symlink to the machine-global secrets
    file, which no audit surface showed).
    """
    from hermes_cli.env_loader import _CREDENTIAL_SUFFIXES

    profiles_dir = (
        Path(root) if root is not None else get_default_hermes_root()
    ) / "profiles"
    out = ["Profile-local .env credential names (profile-owned, names only):"]
    if not profiles_dir.is_dir():
        return out + ["  (no profiles directory)"]

    for profile_dir in sorted(p for p in profiles_dir.iterdir() if p.is_dir()):
        env_file = profile_dir / ".env"
        if not env_file.exists():
            continue
        names = []
        try:
            for line in env_file.read_text(errors="replace").splitlines():
                name = line.split("=", 1)[0].strip()
                if not name or line.lstrip().startswith("#") or "=" not in line:
                    continue
                if name.endswith(_CREDENTIAL_SUFFIXES):
                    names.append(name)
        except OSError:
            out.append(f"  {profile_dir.name:26s} (unreadable)")
            continue
        # A symlinked .env is the finding that hid for months: the profile is
        # not holding its own small credential set, it is reading the
        # controller's entire secrets file.
        shared = ""
        if env_file.is_symlink():
            shared = f" [symlink -> {os.readlink(env_file)}]"
        vault = " ** HOLDS A VAULT ACCESS TOKEN **" if BWS_BOOTSTRAP_ENV in names else ""
        out.append(
            f"  {profile_dir.name:26s} {len(names):2d}{shared}{vault}"
            + (": " + ", ".join(sorted(dict.fromkeys(names))) if names else "")
        )
    return out



# ---------------------------------------------------------------------------
# Trusted hermetic publication readback (BUILD-841)
# ---------------------------------------------------------------------------

PUBLICATION_READBACK_TIMEOUT_SECONDS = 20
PUBLICATION_READBACK_CODES: frozenset[str] = frozenset(
    {
        "contract_incomplete",
        "workspace_missing",
        "target_unbound",
        "target_mismatch",
        "target_denied",
        "policy_unavailable",
        "identity_mismatch",
        "identity_unavailable",
        "auth_missing",
        "remote_rejected",
        "transport",
        "timeout",
        "ref_absent",
        "malformed_response",
        "sha_mismatch",
        "git_unavailable",
    }
)
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_REMOTE_NAME_RE = re.compile(r"^[^\x00-\x20\x7f]{1,255}$")
_MAX_REMOTE_URL_BYTES = 4096
_MAX_GIT_OUTPUT_BYTES = 64 * 1024
_PRIVATE_ATTEMPT_PREFIX = "attempt-"
_HERMETIC_GIT_ENV_KEYS = frozenset(
    {
        "HOME",
        "XDG_CONFIG_HOME",
        "PATH",
        "GIT_EXEC_PATH",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_GLOBAL",
        "GIT_TERMINAL_PROMPT",
        "LANG",
        "LC_ALL",
    }
)


@dataclass(frozen=True)
class _GitProcessResult:
    returncode: int
    stdout: str
    stderr: str
    failure: str | None = None


@dataclass(frozen=True)
class _PrivateDirectory:
    path: str
    device: int
    inode: int


@dataclass(frozen=True)
class _ReadbackContractState:
    expected_sha: str
    remote: str
    ref: str
    workspace_path: str
    publication_repo: str | None


@dataclass(frozen=True)
class _TargetPlan:
    target: str
    protocol: str
    github_slug: str | None
    bound: bool


def _readback_result(
    reason: str | None = None,
    *,
    observed_sha: str | None = None,
    verified: bool = False,
) -> dict[str, Any]:
    if reason is not None and reason not in PUBLICATION_READBACK_CODES:
        reason = "transport"
    return {
        "verified": bool(verified),
        "observed_sha": observed_sha if _FULL_SHA_RE.fullmatch(observed_sha or "") else None,
        "reason": reason,
    }


def safe_publication_readback_reason(value: Any) -> str:
    """Collapse untrusted/exceptional failure labels into the fixed taxonomy."""
    return value if isinstance(value, str) and value in PUBLICATION_READBACK_CODES else "transport"


def sanitize_publication_readback(
    value: Any,
    *,
    expected_sha: str | None = None,
) -> dict[str, Any]:
    """Return only controller-owned, secret-safe publication evidence keys."""
    source = value if isinstance(value, Mapping) else {}
    observed = source.get("observed_sha")
    observed_sha = (
        observed
        if isinstance(observed, str) and _FULL_SHA_RE.fullmatch(observed)
        else None
    )
    verified = (
        source.get("verified") is True
        and observed_sha is not None
        and (expected_sha is None or observed_sha == expected_sha)
    )
    reason = source.get("reason")
    if source.get("verified") is True and observed_sha is not None and not verified:
        reason = "sha_mismatch"
    return {
        "verified": verified,
        "observed_sha": observed_sha,
        "reason": None if verified else safe_publication_readback_reason(reason),
    }


def _run_git_process(
    command: list[str],
    *,
    env: Mapping[str, str],
    cwd: str,
    timeout: int,
) -> _GitProcessResult:
    """Run one bounded Git process group and discard output above the cap."""
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=dict(env),
            start_new_session=True,
        )
    except OSError:
        return _GitProcessResult(-1, "", "", "git_unavailable")

    captured = {"stdout": bytearray(), "stderr": bytearray()}

    def drain(name: str, stream: Any) -> None:
        try:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    return
                remaining = _MAX_GIT_OUTPUT_BYTES - len(captured[name])
                if remaining > 0:
                    captured[name].extend(chunk[:remaining])
        except OSError:
            return

    readers = [
        threading.Thread(target=drain, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=drain, args=("stderr", process.stderr), daemon=True),
    ]
    for reader in readers:
        reader.start()

    def terminate_process_group() -> None:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass

    failure: str | None = None
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        failure = "timeout"
        terminate_process_group()
    except OSError:
        failure = "transport"
        terminate_process_group()
    finally:
        for reader in readers:
            reader.join(timeout=5)
        for stream in (process.stdout, process.stderr):
            if stream is None:
                continue
            try:
                stream.close()
            except (AttributeError, OSError):
                pass
    return _GitProcessResult(
        process.returncode if process.returncode is not None else -1,
        bytes(captured["stdout"]).decode("utf-8", errors="replace"),
        bytes(captured["stderr"]).decode("utf-8", errors="replace"),
        failure,
    )


def _private_directory(runtime: SealedGitRuntime) -> _PrivateDirectory | None:
    """Create one validated child below the immutable bootstrap temp root."""
    if not _seal_matches_directory(runtime.temp_root):
        return None
    root = Path(runtime.temp_root.path)
    try:
        value = tempfile.mkdtemp(prefix=_PRIVATE_ATTEMPT_PREFIX, dir=str(root))
        child = Path(value)
        child_lstat = child.lstat()
        if (
            child.is_symlink()
            or not stat.S_ISDIR(child_lstat.st_mode)
            or child.parent.resolve(strict=True) != root
            or not child.name.startswith(_PRIVATE_ATTEMPT_PREFIX)
            or not _seal_matches_directory(runtime.temp_root)
        ):
            return None
        identity = _PrivateDirectory(str(child), child_lstat.st_dev, child_lstat.st_ino)
        directory_fd = os.open(child, _DIRECTORY_OPEN_FLAGS)
        try:
            opened = os.fstat(directory_fd)
            if (opened.st_dev, opened.st_ino) != (identity.device, identity.inode):
                return None
            os.fchmod(directory_fd, 0o700)
            current = os.fstat(directory_fd)
        finally:
            os.close(directory_fd)
        if (
            current.st_dev != identity.device
            or current.st_ino != identity.inode
            or stat.S_IMODE(current.st_mode) != 0o700
        ):
            return None
        return identity
    except (OSError, RuntimeError):
        return None


def _cleanup_private_directory(
    runtime: SealedGitRuntime, private: _PrivateDirectory | None
) -> None:
    if private is None or not _seal_matches_directory(runtime.temp_root):
        return
    path = Path(private.path)
    if (
        path.parent != Path(runtime.temp_root.path)
        or not path.name.startswith(_PRIVATE_ATTEMPT_PREFIX)
    ):
        return
    _safe_remove_directory(
        path,
        device=private.device,
        inode=private.inode,
        parent_device=runtime.temp_root.device,
        parent_inode=runtime.temp_root.inode,
    )


def _hermetic_git_env(
    runtime: SealedGitRuntime,
    private: _PrivateDirectory,
    *,
    token: str | None = None,
) -> dict[str, str]:
    env = {
        "HOME": private.path,
        "XDG_CONFIG_HOME": private.path,
        "PATH": runtime.path,
        "GIT_EXEC_PATH": runtime.exec_path.path,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "LANG": "C",
        "LC_ALL": "C",
    }
    if token is not None:
        env["GH_TOKEN"] = token
    return env


def _caller_contract_tuple(contract: Any) -> tuple[Any, Any, Any, Any, Any]:
    return (
        getattr(contract, "expected_sha", None),
        getattr(contract, "remote", None),
        getattr(contract, "ref", None),
        getattr(contract, "workspace_path", None),
        getattr(contract, "publication_repo", None),
    )


def _read_current_publication_contract(
    contract: Any,
    *,
    task_id: str,
    run_id: int,
) -> tuple[_ReadbackContractState | None, TrustedWorkerCredentialRuntime | None, str | None]:
    context = trusted_worker_receipt_context()
    runtime = _TRUSTED_WORKER_RUNTIME
    if (
        context is None
        or runtime is None
        or run_id <= 0
        or context[0] != task_id
        or context[1] != run_id
    ):
        return None, runtime, "identity_mismatch"
    connection: sqlite3.Connection | None = None
    try:
        uri = Path(context[2]).absolute().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=0.1)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA busy_timeout = 100")
        connection.execute("BEGIN DEFERRED")
        row = connection.execute(
            "SELECT t.status AS task_status, t.current_run_id, "
            "t.publication_expected_sha, t.publication_remote, t.publication_ref, "
            "t.workspace_path, t.publication_repo, r.task_id AS run_task_id, "
            "r.profile AS run_profile, r.status AS run_status, r.ended_at "
            "FROM tasks t JOIN task_runs r ON r.id = t.current_run_id "
            "WHERE t.id = ? AND r.id = ?",
            (task_id, run_id),
        ).fetchone()
        connection.rollback()
        connection.close()
    except (OSError, sqlite3.Error, ValueError):
        try:
            if connection is not None:
                connection.close()
        except sqlite3.Error:
            pass
        return None, runtime, "identity_unavailable"
    if (
        row is None
        or row["task_status"] != "running"
        or row["current_run_id"] != run_id
        or row["run_task_id"] != task_id
        or str(row["run_profile"] or "").casefold() != runtime.profile.casefold()
        or row["run_status"] != "running"
        or row["ended_at"] is not None
    ):
        return None, runtime, "identity_mismatch"
    db_tuple = (
        row["publication_expected_sha"],
        row["publication_remote"],
        row["publication_ref"],
        row["workspace_path"],
        row["publication_repo"],
    )
    if db_tuple != _caller_contract_tuple(contract):
        return None, runtime, "identity_mismatch"
    return (
        _ReadbackContractState(
            expected_sha=str(db_tuple[0] or ""),
            remote=str(db_tuple[1] or ""),
            ref=str(db_tuple[2] or ""),
            workspace_path=str(db_tuple[3] or ""),
            publication_repo=(str(db_tuple[4]) if db_tuple[4] is not None else None),
        ),
        runtime,
        None,
    )


def _valid_literal_ref(ref: str) -> bool:
    if not ref.startswith("refs/") or len(ref.encode("utf-8")) > 1024:
        return False
    if ref.endswith((".", "/")) or ".." in ref or "@{" in ref or "//" in ref:
        return False
    if ref.endswith("^{}") or ref == "@":
        return False
    forbidden = set(" ~^:?*[\\")
    if any(ord(char) < 32 or ord(char) == 127 or char in forbidden for char in ref):
        return False
    return all(
        component and not component.startswith(".") and not component.endswith(".lock")
        for component in ref.split("/")
    )


def _parse_github_target(value: str) -> tuple[str, str, str] | None:
    if not value or len(value.encode("utf-8")) > _MAX_REMOTE_URL_BYTES:
        return None
    scp = re.fullmatch(
        r"(?:(?P<user>git)@)?github\.com:(?P<owner>[A-Za-z0-9][A-Za-z0-9-]{0,38})/"
        r"(?P<repo>[A-Za-z0-9_.-]{1,100})(?:\.git)?",
        value,
        re.IGNORECASE,
    )
    if scp is not None:
        owner, repo = scp.group("owner"), scp.group("repo")
        if repo.casefold().endswith(".git"):
            repo = repo[:-4]
        return owner, repo, f"{owner}/{repo}"
    try:
        parsed = urlsplit(value)
        if parsed.scheme.casefold() not in {"https", "ssh"}:
            return None
        if parsed.hostname is None or parsed.hostname.casefold() != "github.com":
            return None
        if parsed.password is not None or parsed.query or parsed.fragment or "%" in parsed.path:
            return None
        if parsed.scheme.casefold() == "https":
            if parsed.username is not None or parsed.port not in {None, 443}:
                return None
        elif parsed.username not in {None, "git"} or parsed.port not in {None, 22}:
            return None
    except ValueError:
        return None
    pieces = parsed.path.strip("/").split("/")
    if len(pieces) != 2:
        return None
    owner, repo = pieces
    if repo.casefold().endswith(".git"):
        repo = repo[:-4]
    canonical = _canonical_repo_slug(f"{owner}/{repo}")
    return canonical


def _parse_local_target(value: str, workspace: Path) -> str | None:
    if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    if value.startswith("file://"):
        try:
            parsed = urlsplit(value)
        except ValueError:
            return None
        if (
            parsed.scheme != "file"
            or parsed.netloc not in {"", "localhost"}
            or parsed.query
            or parsed.fragment
        ):
            return None
        local = Path(unquote(parsed.path))
        if not local.is_absolute():
            return None
    else:
        if not value.startswith(("/", "./", "../")):
            return None
        local = Path(value)
        if not local.is_absolute():
            local = workspace / local
    try:
        return str(local.resolve(strict=False))
    except (OSError, RuntimeError):
        return None


def _probe_publication_target(
    state: _ReadbackContractState,
    runtime: SealedGitRuntime,
    workspace: Path,
) -> tuple[str | None, str | None]:
    private = _private_directory(runtime)
    if private is None:
        return None, "transport"
    try:
        command = [
            runtime.git.path,
            "-c",
            "credential.helper=",
            "-c",
            "protocol.allow=never",
            "-c",
            "protocol.ext.allow=never",
            "-C",
            str(workspace),
            "remote",
            "get-url",
            "--push",
            state.remote,
        ]
        completed = _run_git_process(
            command,
            env=_hermetic_git_env(runtime, private),
            cwd=private.path,
            timeout=10,
        )
    finally:
        _cleanup_private_directory(runtime, private)
    if completed.failure is not None:
        return None, completed.failure
    if completed.returncode != 0:
        return None, "target_mismatch" if state.publication_repo else "target_unbound"
    lines = completed.stdout.splitlines()
    if len(lines) != 1 or not lines[0] or len(lines[0].encode("utf-8")) > _MAX_REMOTE_URL_BYTES:
        return None, "target_mismatch" if state.publication_repo else "target_unbound"
    return lines[0], None


def _target_plan(
    remote_url: str,
    *,
    state: _ReadbackContractState,
    workspace: Path,
) -> tuple[_TargetPlan | None, str | None]:
    bound_slug = _canonical_repo_slug(state.publication_repo) if state.publication_repo else None
    if state.publication_repo and bound_slug is None:
        return None, "contract_incomplete"
    github = _parse_github_target(remote_url)
    if bound_slug is not None:
        if github is None or github[2].casefold() != bound_slug[2].casefold():
            return None, "target_mismatch"
        return (
            _TargetPlan(
                target=f"https://github.com/{bound_slug[0]}/{bound_slug[1]}.git",
                protocol="https",
                github_slug=bound_slug[2],
                bound=True,
            ),
            None,
        )
    local = _parse_local_target(remote_url, workspace)
    if local is not None and github is None:
        return _TargetPlan(local, "file", None, False), None
    if github is not None:
        return (
            _TargetPlan(
                f"https://github.com/{github[0]}/{github[1]}.git",
                "https",
                github[2],
                False,
            ),
            None,
        )
    try:
        unknown = urlsplit(remote_url)
    except ValueError:
        return None, "target_unbound"
    try:
        invalid_unknown = (
            unknown.scheme.casefold() != "https"
            or unknown.hostname is None
            or unknown.username is not None
            or unknown.password is not None
            or unknown.port not in {None, 443}
            or bool(unknown.query)
            or bool(unknown.fragment)
        )
    except ValueError:
        return None, "target_unbound"
    if invalid_unknown:
        return None, "target_unbound"
    return _TargetPlan(remote_url, "https", None, False), None


def _ls_remote_command(
    runtime: SealedGitRuntime,
    plan: _TargetPlan,
    ref: str,
    *,
    credentialed: bool,
) -> list[str]:
    command = [
        runtime.git.path,
        "-c",
        "credential.helper=",
        "-c",
        "http.followRedirects=false",
        "-c",
        "protocol.allow=never",
        "-c",
        "protocol.ext.allow=never",
        "-c",
        f"protocol.{plan.protocol}.allow=always",
    ]
    if credentialed:
        command += ["-c", f"credential.helper={GIT_ENV_TOKEN_HELPER}"]
    return command + ["ls-remote", "--exit-code", "--refs", "--", plan.target, ref]


def _parse_ls_remote(stdout: str, *, ref: str, expected: str) -> dict[str, Any]:
    records: list[str] = []
    malformed = False
    for line in stdout.splitlines():
        parts = line.split("	")
        if len(parts) != 2:
            malformed = True
            continue
        sha, returned_ref = parts
        if returned_ref != ref or returned_ref.endswith("^{}"):
            malformed = True
            continue
        records.append(sha)
    if malformed or len(records) != 1 or not _FULL_SHA_RE.fullmatch(records[0]):
        return _readback_result("malformed_response")
    observed = records[0]
    if observed != expected:
        return _readback_result("sha_mismatch", observed_sha=observed)
    return _readback_result(None, observed_sha=observed, verified=True)


_PUBLIC_AUTH_DENIAL_PATTERNS = (
    re.compile(r"(?mi)^(?:remote:\s*)?Repository not found\.?\s*$"),
    re.compile(r"(?mi)^fatal: Authentication failed for 'https://github\.com/[^']+'\.?\s*$"),
    re.compile(r"(?mi)^fatal: could not read Username for 'https://github\.com': terminal prompts disabled\s*$"),
    re.compile(r"(?mi)^remote: Invalid username or token\.\s*$"),
    re.compile(
        r"(?mi)^fatal: unable to access 'https://github\.com/[^']+': "
        r"The requested URL returned error: (?:401|403|404)\s*$"
    ),
)


def _is_public_access_denial(stderr: str) -> bool:
    return any(pattern.search(stderr) is not None for pattern in _PUBLIC_AUTH_DENIAL_PATTERNS)


def _run_ls_remote(
    runtime: SealedGitRuntime,
    private: _PrivateDirectory,
    plan: _TargetPlan,
    state: _ReadbackContractState,
    *,
    token: str | None,
) -> _GitProcessResult:
    return _run_git_process(
        _ls_remote_command(runtime, plan, state.ref, credentialed=token is not None),
        env=_hermetic_git_env(runtime, private, token=token),
        cwd=private.path,
        timeout=PUBLICATION_READBACK_TIMEOUT_SECONDS,
    )


def trusted_publication_readback(
    contract: Any,
    *,
    task_id: str = "",
    run_id: int | None = None,
) -> dict[str, Any]:
    """Perform the sole task/run-bound, public-first publication proof action."""
    caller = _caller_contract_tuple(contract)
    expected = str(caller[0] or "")
    remote = str(caller[1] or "")
    ref = str(caller[2] or "")
    workspace_value = str(caller[3] or "")
    if (
        not _FULL_SHA_RE.fullmatch(expected)
        or not _REMOTE_NAME_RE.fullmatch(remote)
        or remote.startswith("-")
        or not _valid_literal_ref(ref)
    ):
        return _readback_result("contract_incomplete")
    workspace = Path(workspace_value)
    if not workspace_value or not workspace.is_absolute() or not workspace.is_dir():
        return _readback_result("workspace_missing")
    exact_run_id = run_id if type(run_id) is int else 0

    state, trusted_runtime, identity_reason = _read_current_publication_contract(
        contract,
        task_id=task_id,
        run_id=exact_run_id,
    )
    if identity_reason is not None or state is None or trusted_runtime is None:
        return _readback_result(identity_reason or "identity_mismatch")
    git_runtime = trusted_runtime.git_runtime
    if git_runtime is None or not _git_runtime_is_current(git_runtime, rehash_git=False):
        return _readback_result("git_unavailable")

    budget_key = (task_id, exact_run_id)
    with _WORKER_CREDENTIAL_LOCK:
        calls = _PUBLICATION_READBACK_CALLS.get(budget_key, 0) + 1
        _PUBLICATION_READBACK_CALLS[budget_key] = calls
    if calls > 2:
        return _readback_result("transport")

    remote_url, probe_reason = _probe_publication_target(state, git_runtime, workspace)
    if probe_reason is not None or remote_url is None:
        return _readback_result(probe_reason or "target_unbound")
    plan, plan_reason = _target_plan(remote_url, state=state, workspace=workspace)
    if plan_reason is not None or plan is None:
        return _readback_result(plan_reason or "target_unbound")

    if plan.bound:
        policy_decision = publication_policy_decision_for_repo(
            trusted_runtime.profile,
            plan.github_slug,
        )
        if policy_decision == "unavailable":
            return _readback_result("policy_unavailable")
        if policy_decision != "allowed":
            return _readback_result("target_denied")

    public_dir = _private_directory(git_runtime)
    if public_dir is None:
        return _readback_result("transport")
    try:
        public = _run_ls_remote(
            git_runtime,
            public_dir,
            plan,
            state,
            token=None,
        )
    finally:
        _cleanup_private_directory(git_runtime, public_dir)
    if public.failure is not None:
        return _readback_result(public.failure)
    if public.returncode == 0:
        return _parse_ls_remote(public.stdout, ref=state.ref, expected=state.expected_sha)
    if public.returncode == 2:
        return _readback_result("ref_absent")
    if not _is_public_access_denial(public.stderr):
        return _readback_result("transport")
    if not plan.bound:
        return _readback_result("target_unbound")

    credential_dir = _private_directory(git_runtime)
    if credential_dir is None:
        return _readback_result("transport")
    try:
        current, current_runtime, current_reason = _read_current_publication_contract(
            contract,
            task_id=task_id,
            run_id=exact_run_id,
        )
        if current_reason is not None or current is None or current_runtime is None:
            return _readback_result(current_reason or "identity_mismatch")
        with _WORKER_CREDENTIAL_LOCK:
            if budget_key in _PUBLICATION_CREDENTIAL_ATTEMPTS:
                return _readback_result("transport")
            _PUBLICATION_CREDENTIAL_ATTEMPTS.add(budget_key)
        if not _git_runtime_is_current(git_runtime, rehash_git=True):
            return _readback_result("git_unavailable")
        token = get_trusted_worker_credential("github_write")
        if token is None:
            return _readback_result("auth_missing")
        credentialed = _run_ls_remote(
            git_runtime,
            credential_dir,
            plan,
            current,
            token=token,
        )
    finally:
        _cleanup_private_directory(git_runtime, credential_dir)
    if credentialed.failure is not None:
        return _readback_result(credentialed.failure)
    if credentialed.returncode == 0:
        return _parse_ls_remote(
            credentialed.stdout,
            ref=state.ref,
            expected=state.expected_sha,
        )
    if credentialed.returncode == 2:
        return _readback_result("ref_absent")
    return _readback_result("remote_rejected")


# Descriptive aliases keep call sites readable while preserving one contract.
load_worker_credential_manifest = load_manifest
preflight_worker_credentials = resolve_worker_credentials
sanitize_worker_environment = build_worker_environment
