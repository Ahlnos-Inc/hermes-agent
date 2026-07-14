"""Reset-aware circuit state for whole-agent subscription runtimes."""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hermes_constants import get_default_hermes_root, get_hermes_home
from utils import atomic_json_write


@dataclass(frozen=True)
class RuntimeCircuitState:
    """Persisted health state for one account-scoped runtime target."""

    until: float
    reason: str | None = None


def runtime_target_key(agent: Any, target: dict[str, Any] | None = None) -> tuple[str, str, str]:
    source = target or {}
    return (
        str(source.get("runtime", getattr(agent, "runtime", "hermes")) or "hermes"),
        str(source.get("provider", getattr(agent, "provider", "")) or "").strip().lower(),
        str(source.get("model", getattr(agent, "model", "")) or "").strip(),
    )


def _account_key(agent: Any) -> str:
    attestation = getattr(agent, "_claude_max_attestation", None)
    return str(getattr(attestation, "account_key", "") or "profile-default")


def _state_path() -> Path:
    return get_default_hermes_root() / "shared" / "runtime-circuits.json"


def _legacy_state_path() -> Path:
    """Pre-shared location retained as a read-only migration source."""
    return get_hermes_home() / "state" / "runtime-circuits.json"


def _persistent_key(agent: Any, target: dict[str, Any] | None = None) -> str:
    return json.dumps([_account_key(agent), *runtime_target_key(agent, target)], separators=(",", ":"))


def _coerce_state(value: Any) -> RuntimeCircuitState | None:
    if isinstance(value, RuntimeCircuitState):
        return value
    if isinstance(value, dict):
        until_value = value.get("until")
        reason_value = value.get("reason")
    else:
        # Backward compatibility with the original ``{key: reset_at}`` file.
        until_value = value
        reason_value = None
    try:
        until = float(until_value)
    except (TypeError, ValueError):
        return None
    reason = (
        str(reason_value).strip()
        if isinstance(reason_value, str) and reason_value.strip()
        else None
    )
    return RuntimeCircuitState(until=until, reason=reason)


def _reason_value(reason: Any) -> str | None:
    value = getattr(reason, "value", reason)
    normalized = str(value).strip() if value is not None else ""
    return normalized or None


def runtime_circuit_ttl_seconds(reason: Any) -> int:
    """Return the conservative retry delay for a classified route failure."""
    normalized = _reason_value(reason)
    return {
        "auth": 15 * 60,
        "auth_permanent": 60 * 60,
        "billing": 60 * 60,
        "rate_limit": 60,
        "upstream_rate_limit": 60,
        "overloaded": 2 * 60,
        "server_error": 2 * 60,
        "timeout": 5 * 60,
        "model_not_found": 15 * 60,
        "provider_policy_blocked": 15 * 60,
        "content_policy_blocked": 15 * 60,
    }.get(normalized, 60)


def _load_persistent(path: Path | None = None) -> dict[str, RuntimeCircuitState]:
    path = path or _state_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    result: dict[str, RuntimeCircuitState] = {}
    for key, value in payload.items():
        state = _coerce_state(value)
        if state is not None:
            result[str(key)] = state
    return result


@contextmanager
def _persistent_lock():
    path = _state_path().with_suffix(".lock")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        yield
    finally:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
        handle.close()


def _store_persistent(circuits: dict[str, RuntimeCircuitState]) -> None:
    path = _state_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    now = time.time()
    live = {
        key: {"until": state.until, "reason": state.reason}
        for key, state in circuits.items()
        if state.until > now
    }
    atomic_json_write(path, live, mode=0o600, indent=2, sort_keys=True)


def open_runtime_circuit(
    agent: Any,
    *,
    reset_at: int | float | None,
    fallback_seconds: int | float | None = None,
    reason: Any = None,
    target: dict[str, Any] | None = None,
) -> float:
    normalized_reason = _reason_value(reason)
    ttl = (
        float(fallback_seconds)
        if fallback_seconds is not None
        else float(runtime_circuit_ttl_seconds(normalized_reason))
    )
    until = float(reset_at) if reset_at else time.time() + ttl
    state = RuntimeCircuitState(
        until=until,
        reason=normalized_reason,
    )
    circuits = getattr(agent, "_runtime_circuits", None)
    if circuits is None:
        circuits = {}
        agent._runtime_circuits = circuits
    runtime_key = runtime_target_key(agent, target)
    existing = _coerce_state(circuits.get(runtime_key))
    if existing is not None and existing.until > state.until:
        state = RuntimeCircuitState(
            until=existing.until,
            reason=normalized_reason or existing.reason,
        )
    with _persistent_lock():
        persistent = _load_persistent()
        persistent_key = _persistent_key(agent, target)
        existing = persistent.get(persistent_key)
        if existing is not None and existing.until > state.until:
            state = RuntimeCircuitState(
                until=existing.until,
                reason=normalized_reason or existing.reason,
            )
        persistent[persistent_key] = state
        _store_persistent(persistent)
    circuits[runtime_key] = state
    return state.until


def runtime_circuit_status(
    agent: Any, target: dict[str, Any] | None = None
) -> RuntimeCircuitState | None:
    circuits = getattr(agent, "_runtime_circuits", None) or {}
    key = runtime_target_key(agent, target)
    state = _coerce_state(circuits.get(key))
    if state is None or state.until <= time.time():
        persistent = _load_persistent()
        state = persistent.get(_persistent_key(agent, target))
        if state is None:
            state = _load_persistent(_legacy_state_path()).get(
                _persistent_key(agent, target)
            )
        if state is not None and state.until > time.time():
            circuits = getattr(agent, "_runtime_circuits", None)
            if circuits is None:
                circuits = {}
                agent._runtime_circuits = circuits
            circuits[key] = state
    if state is None or state.until <= time.time():
        circuits.pop(key, None)
        return None
    return state


def runtime_circuit_open_until(
    agent: Any, target: dict[str, Any] | None = None
) -> float | None:
    state = runtime_circuit_status(agent, target)
    return state.until if state is not None else None


def preflight_native_runtime_circuit(agent: Any) -> RuntimeCircuitState | None:
    """Skip a known-unavailable native route before its first network call.

    The primary runtime snapshot is immutable launch intent.  This helper only
    advances the existing fallback state machine; it never rewrites that
    snapshot.  Whole-agent runtimes perform their own attestation before
    circuit lookup and therefore do not use this native preflight.

    Returns the open state when no fallback can be activated, otherwise
    ``None`` after either a clear circuit or a successful fallback switch.
    """
    if str(getattr(agent, "runtime", "hermes") or "hermes") != "hermes":
        return None
    state = runtime_circuit_status(agent)
    if state is None:
        return None

    from agent.error_classifier import FailoverReason

    try:
        reason = FailoverReason(state.reason) if state.reason else FailoverReason.rate_limit
    except ValueError:
        reason = FailoverReason.rate_limit
    if agent._try_activate_fallback(
        reason=reason,
        _record_failed_route=False,
    ):
        return None
    return state


__all__ = [
    "RuntimeCircuitState",
    "open_runtime_circuit",
    "preflight_native_runtime_circuit",
    "runtime_circuit_open_until",
    "runtime_circuit_status",
    "runtime_circuit_ttl_seconds",
    "runtime_target_key",
]
