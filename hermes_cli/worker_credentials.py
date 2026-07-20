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
import secrets
import shutil
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping

from hermes_constants import get_default_hermes_root

_log = logging.getLogger(__name__)

CONTRACT_VERSION = 1
MANIFEST_FILENAME = "worker-credential-contract.yaml"

BWS_BOOTSTRAP_ENV = "BWS_ACCESS_TOKEN"
# Ambient name that must always be STRIPPED from workers (kept in
# UNCONDITIONAL_STRIP_ENV below). It is NOT the resolve source anymore.
GITHUB_WRITE_SOURCE_KEY = "GH_TOKEN_SECRET_WRITE"
# The BWS secret github_write actually RESOLVES from. The fine-grained
# GH_TOKEN_SECRET_WRITE PAT is not authorized for the release-target repos
# (contents + pull_requests both 403 on Ahlnos-Inc/aldnoah), so github_write
# is sourced from the classic GITHUB_TOKEN (repo scope) per Nicholas's call
# 2026-07-20. Tradeoff: classic token is broad-scoped; migrate back to a
# properly-scoped fine-grained PAT under BUILD-603 to restore least-privilege.
GITHUB_WRITE_RESOLVE_KEY = "GITHUB_TOKEN"

# These names are intentionally explicit.  The list is also used when a
# caller provides an environment mapping rather than os.environ, so a worker
# cannot accidentally inherit the controller's bootstrap or GitHub aliases.
PRIVATE_HANDOFF_PREFIX = "HERMES_WORKER_CREDENTIAL_"
GITHUB_WRITE_HANDOFF_ENV = f"{PRIVATE_HANDOFF_PREFIX}GITHUB_WRITE"
BWS_BOOTSTRAP_HANDOFF_ENV = f"{PRIVATE_HANDOFF_PREFIX}BWS_BOOTSTRAP"
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
    "host=; "
    "while IFS= read -r line; do "
    "case \"$line\" in host=*) host=\"${line#host=}\";; esac; "
    "done; "
    "[ \"$host\" = github.com ] || return 0; "
    "printf 'username=x-access-token\\npassword=%s\\n' \"$GH_TOKEN\"; "
    "}; f"
)

UNCONDITIONAL_STRIP_ENV = frozenset(
    {
        BWS_BOOTSTRAP_ENV,
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
    source_key: str
    handoff_env: str
    projection_env: tuple[str, ...] = ()


# ``bws_bootstrap`` is transitional.  It exists only for the current
# marketing-operator migration and is intentionally not a general vault
# capability.
CAPABILITIES: Mapping[str, CapabilityDefinition] = {
    "github_write": CapabilityDefinition(
        name="github_write",
        source_key=GITHUB_WRITE_RESOLVE_KEY,
        handoff_env=GITHUB_WRITE_HANDOFF_ENV,
        projection_env=("GH_TOKEN",),
    ),
    "bws_bootstrap": CapabilityDefinition(
        name="bws_bootstrap",
        source_key=BWS_BOOTSTRAP_ENV,
        handoff_env=BWS_BOOTSTRAP_HANDOFF_ENV,
        projection_env=(BWS_BOOTSTRAP_ENV,),
    ),
}


def _canonical_manifest_payload(
    profiles: Mapping[str, tuple[str, ...]],
) -> dict[str, Any]:
    return {
        "version": CONTRACT_VERSION,
        "profiles": {
            profile: {"actions": list(actions)}
            for profile, actions in sorted(profiles.items())
        },
    }


def _manifest_digest(profiles: Mapping[str, tuple[str, ...]]) -> str:
    encoded = json.dumps(
        _canonical_manifest_payload(profiles),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class WorkerCredentialManifest:
    """Normalized, digested v1 worker credential manifest."""

    version: int
    profiles: Mapping[str, tuple[str, ...]]
    digest: str
    path: Path | None = field(default=None, repr=False, compare=False)

    def actions_for(self, profile: str) -> tuple[str, ...]:
        """Return grants for *profile*; absent profiles have no grants."""
        return self.profiles.get(_normalize_profile(profile), ())


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
    capabilities: tuple[str, ...]
    _values: tuple[tuple[str, str], ...] = field(
        default=(), repr=False, compare=False
    )

    def value_for(self, capability: str) -> str | None:
        return dict(self._values).get(capability)


_TRUSTED_WORKER_RUNTIME: TrustedWorkerCredentialRuntime | None = None
_TERMINAL_ARTIFACT_DIR: Path | None = None


def _normalize_profile(profile: str) -> str:
    try:
        from hermes_cli.profile_contract import normalize_and_validate_profile_name

        return normalize_and_validate_profile_name(profile)
    except (TypeError, ValueError):
        raise WorkerCredentialError("worker credential profile is invalid") from None


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
    if raw.get("version") != CONTRACT_VERSION or isinstance(raw.get("version"), bool):
        raise WorkerCredentialError("worker credential manifest version is unsupported")

    raw_profiles = raw.get("profiles")
    if not isinstance(raw_profiles, dict):
        raise WorkerCredentialError("worker credential manifest profiles are malformed")

    profiles: dict[str, tuple[str, ...]] = {}
    for raw_name, raw_grant in raw_profiles.items():
        try:
            profile = _normalize_profile(raw_name)
        except WorkerCredentialError:
            raise WorkerCredentialError("worker credential profile grant is malformed") from None
        if profile in profiles:
            raise WorkerCredentialError("worker credential manifest has duplicate profiles")
        if not isinstance(raw_grant, dict) or set(raw_grant) != {"actions"}:
            raise WorkerCredentialError("worker credential profile grant is malformed")
        actions = raw_grant.get("actions")
        if not isinstance(actions, list) or any(
            not isinstance(action, str) or action not in CAPABILITIES
            for action in actions
        ):
            raise WorkerCredentialError("worker credential capability is unsupported")
        if len(set(actions)) != len(actions):
            raise WorkerCredentialError("worker credential capability is duplicated")
        profiles[profile] = tuple(sorted(actions))

    return WorkerCredentialManifest(
        version=CONTRACT_VERSION,
        profiles=profiles,
        digest=_manifest_digest(profiles),
        path=path,
    )


def load_manifest(root: Path | str | None = None) -> WorkerCredentialManifest:
    """Load and normalize the machine-global v1 worker manifest.

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
            digest=_manifest_digest({}),
            path=None,
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
        handoff = {
            name: target.get(name)
            for name in (GITHUB_WRITE_HANDOFF_ENV, BWS_BOOTSTRAP_HANDOFF_ENV)
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
                digest=_manifest_digest({}),
                path=None,
            )

        if expected_digest != manifest.digest:
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
        if "bws_bootstrap" in admitted:
            target[BWS_BOOTSTRAP_ENV] = admitted["bws_bootstrap"]

        runtime = TrustedWorkerCredentialRuntime(
            profile=profile,
            task_id=task_id,
            run_id=run_id,
            manifest_digest=manifest.digest,
            capabilities=tuple(sorted(admitted)),
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
    """Isolate worker terminals and project ``github_write`` when trusted."""
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
        _TRUSTED_WORKER_RUNTIME = None
        _TERMINAL_ARTIFACT_DIR = None
        _TRUSTED_WORKER_CREDENTIALS.clear()


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
        if (
            isinstance(configured_access_token_env, str)
            and configured_access_token_env
        ):
            strip_env.add(configured_access_token_env)
        return frozenset(strip_env)
    except Exception:
        return frozenset(UNCONDITIONAL_STRIP_ENV)


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


def resolve_worker_credentials(
    profile: str,
    *,
    root: Path | str | None = None,
    base_env: Mapping[str, str] | None = None,
    run_id: int | str | None = None,
    manifest: WorkerCredentialManifest | None = None,
) -> WorkerCredentialPlan:
    """Resolve the closed grants for one worker without leaking secrets.

    Ambient ``GH_TOKEN``, ``GITHUB_TOKEN``, and ``GH_TOKEN_SECRET_WRITE`` are
    never considered valid action sources.  ``github_write`` is satisfied only
    by the selected value returned by the Bitwarden source adapter.
    """
    cleanup_stale_worker_credential_artifacts()
    normalized_profile = _normalize_profile(profile)
    loaded_manifest = manifest or load_manifest(root)
    capabilities = loaded_manifest.actions_for(normalized_profile)
    env = dict(os.environ if base_env is None else base_env)
    source_config = _bitwarden_config(Path(root) if root is not None else get_default_hermes_root())
    access_token_env = str((source_config or {}).get("access_token_env") or BWS_BOOTSTRAP_ENV)
    bootstrap = str(env.get(access_token_env) or "").strip()
    needs_bitwarden = "github_write" in capabilities
    needs_bootstrap = needs_bitwarden or "bws_bootstrap" in capabilities
    statuses: list[str] = []
    handoff: dict[str, str] = {}
    source = "bitwarden" if needs_bitwarden else None
    strip_env = set(UNCONDITIONAL_STRIP_ENV)
    strip_env.add(access_token_env)

    if needs_bootstrap:
        statuses.append(f"bws_bootstrap={'present' if bootstrap else 'missing'}")
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

    if "bws_bootstrap" in capabilities:
        handoff[BWS_BOOTSTRAP_HANDOFF_ENV] = bootstrap

    if needs_bitwarden:
        if not isinstance(source_config, dict) or not source_config.get("enabled"):
            plan = WorkerCredentialPlan(
                profile=normalized_profile,
                manifest_digest=loaded_manifest.digest,
                capabilities=capabilities,
                diagnostics=tuple([*statuses, "github_write=missing"]),
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
        github_value, failure = _safe_source_result(result, GITHUB_WRITE_RESOLVE_KEY)
        if github_value is None:
            statuses.append("github_write=missing")
            plan = WorkerCredentialPlan(
                profile=normalized_profile,
                manifest_digest=loaded_manifest.digest,
                capabilities=capabilities,
                diagnostics=tuple(statuses),
                error=(
                    "worker credential preflight Bitwarden source failed"
                    if failure != "secret_missing"
                    else "worker credential preflight GitHub write secret is missing"
                ),
                source="bitwarden",
                _strip_env=frozenset(strip_env),
            )
            _log_preflight(plan, run_id)
            return plan
        statuses.append("github_write=present")
        handoff[GITHUB_WRITE_HANDOFF_ENV] = github_value

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
) -> WorkerCredentialPlan:
    """Resolve grants and raise a safe error when the worker cannot start."""
    return resolve_worker_credentials(
        profile,
        root=root,
        base_env=base_env,
        run_id=run_id,
        manifest=manifest,
    ).require_ok()


def build_worker_environment(
    base_env: Mapping[str, str], plan: WorkerCredentialPlan
) -> dict[str, str]:
    """Sanitize a worker environment, then add only authorized handoffs."""
    env = {
        key: value
        for key, value in base_env.items()
        if key not in plan._strip_env and not key.startswith(PRIVATE_HANDOFF_PREFIX)
    }
    for key, value in plan._handoff:
        env[key] = value
    env[MANIFEST_DIGEST_ENV] = plan.manifest_digest
    if "bws_bootstrap" in plan.capabilities:
        for key, value in plan._handoff:
            if key == BWS_BOOTSTRAP_HANDOFF_ENV:
                env[BWS_BOOTSTRAP_ENV] = value
                break
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
        capability = {
            GITHUB_WRITE_HANDOFF_ENV: "github_write",
            BWS_BOOTSTRAP_HANDOFF_ENV: "bws_bootstrap",
        }.get(key)
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


# Descriptive aliases keep call sites readable while preserving one contract.
load_worker_credential_manifest = load_manifest
preflight_worker_credentials = resolve_worker_credentials
sanitize_worker_environment = build_worker_environment
