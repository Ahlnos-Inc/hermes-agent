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

import contextvars
import hashlib
import json
import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping

from hermes_constants import get_default_hermes_root

_log = logging.getLogger(__name__)

CONTRACT_VERSION = 1
MANIFEST_FILENAME = "worker-credential-contract.yaml"

BWS_BOOTSTRAP_ENV = "BWS_ACCESS_TOKEN"
GITHUB_WRITE_SOURCE_KEY = "GH_TOKEN_SECRET_WRITE"

# These names are intentionally explicit.  The list is also used when a
# caller provides an environment mapping rather than os.environ, so a worker
# cannot accidentally inherit the controller's bootstrap or GitHub aliases.
PRIVATE_HANDOFF_PREFIX = "HERMES_WORKER_CREDENTIAL_"
GITHUB_WRITE_HANDOFF_ENV = f"{PRIVATE_HANDOFF_PREFIX}GITHUB_WRITE"
BWS_BOOTSTRAP_HANDOFF_ENV = f"{PRIVATE_HANDOFF_PREFIX}BWS_BOOTSTRAP"

UNCONDITIONAL_STRIP_ENV = frozenset(
    {
        BWS_BOOTSTRAP_ENV,
        "GH_TOKEN",
        "GITHUB_TOKEN",
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
        source_key=GITHUB_WRITE_SOURCE_KEY,
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


_TRUSTED_WORKER_CREDENTIALS: contextvars.ContextVar[dict[str, str]] = (
    contextvars.ContextVar("_TRUSTED_WORKER_CREDENTIALS", default={})
)


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
        github_value, failure = _safe_source_result(result, GITHUB_WRITE_SOURCE_KEY)
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
        trusted = dict(_TRUSTED_WORKER_CREDENTIALS.get())
        trusted.update(consumed)
        _TRUSTED_WORKER_CREDENTIALS.set(trusted)
    return ConsumedWorkerCredentials(
        capabilities=tuple(sorted(consumed)),
        _values=tuple(sorted(consumed.items())),
    )


def get_consumed_worker_credential(capability: str) -> str | None:
    """Return a consumed value for an internal authorized boundary."""
    return _TRUSTED_WORKER_CREDENTIALS.get().get(capability)


# Descriptive aliases keep call sites readable while preserving one contract.
load_worker_credential_manifest = load_manifest
preflight_worker_credentials = resolve_worker_credentials
sanitize_worker_environment = build_worker_environment
