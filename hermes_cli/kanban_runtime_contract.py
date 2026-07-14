"""Immutable requested routes and durable observed-runtime attestations.

Environment variables locate the active Kanban run; they are never treated as
evidence of which provider/model is actually active.
"""

from __future__ import annotations

import os
from typing import Any, Optional


class RunRouteMismatch(RuntimeError):
    """The parsed CLI route no longer matches the run's immutable request."""


class RuntimeObservationError(RuntimeError):
    """The active runtime could not be durably attested."""


def _active_identity() -> tuple[Optional[str], Optional[int]]:
    task_id = (os.environ.get("HERMES_KANBAN_TASK") or "").strip() or None
    raw_run_id = (os.environ.get("HERMES_KANBAN_RUN_ID") or "").strip()
    try:
        run_id = int(raw_run_id) if raw_run_id else None
    except (TypeError, ValueError):
        run_id = None
    return task_id, run_id


def _kanban_identity_declared() -> bool:
    """Return whether this process claims to belong to a Kanban run."""
    return bool(
        (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
        or (os.environ.get("HERMES_KANBAN_RUN_ID") or "").strip()
        or (os.environ.get("HERMES_KANBAN_CLAIM_LOCK") or "").strip()
    )


def load_active_run_spec() -> Optional[dict]:
    task_id, run_id = _active_identity()
    if not task_id or run_id is None:
        return None
    from hermes_cli import kanban_db

    with kanban_db.connect() as conn:
        return kanban_db.get_run_spec(
            conn, run_id, task_id=task_id, require_current=True,
        )


def _canonical_provider(value: Any) -> str:
    return str(value or "").strip().lower().replace("_", "-")


def _canonical_model(value: Any, provider: Any) -> str:
    model = str(value or "").strip().lower()
    provider_norm = _canonical_provider(provider)
    if "/" in model:
        prefix, suffix = model.split("/", 1)
        if _canonical_provider(prefix) == provider_norm:
            model = suffix
    return model


def _reasoning_effort(reasoning_config: Any) -> str:
    if not isinstance(reasoning_config, dict):
        return ""
    if reasoning_config.get("enabled") is False:
        return "none"
    return str(reasoning_config.get("effort") or "").strip().lower()


def _canonical_toolsets(value: Any) -> Optional[list[str]]:
    """Return a stable set representation, or ``None`` for invalid input."""
    if not isinstance(value, (list, tuple)):
        return None
    if any(
        not isinstance(item, str)
        or not item.strip()
        or "," in item
        for item in value
    ):
        return None
    return sorted({item.strip().casefold() for item in value})


def preflight_kanban_cli_route(
    *,
    model: Any,
    provider: Any,
    reasoning_config: Any,
    toolsets: Any = None,
) -> Optional[dict]:
    """Fail before agent/provider construction when parser/config drifted."""
    spec = load_active_run_spec()
    if spec is None:
        if _kanban_identity_declared():
            raise RunRouteMismatch(
                "process declares Kanban identity but has no active run contract"
            )
        return None  # explicit manual or non-Kanban process
    requested = spec.get("requested_route")
    version = spec.get("version")
    if version not in {1, 2} or not isinstance(requested, dict):
        raise RunRouteMismatch("active run has an unsupported route contract")

    if version == 2:
        expected_toolsets = spec.get("toolsets")
        expected_normalized = _canonical_toolsets(expected_toolsets)
        if not expected_normalized:
            raise RunRouteMismatch("active run has invalid toolset contract")
        actual_toolsets = _canonical_toolsets(toolsets)
        if actual_toolsets != expected_normalized:
            raise RunRouteMismatch(
                f"requested toolsets {expected_toolsets!r}, parsed {actual_toolsets!r}"
            )

    expected_provider = requested.get("provider")
    if expected_provider and _canonical_provider(provider) != _canonical_provider(
        expected_provider
    ):
        raise RunRouteMismatch(
            f"requested provider {expected_provider!r}, parsed {provider!r}"
        )

    expected_model = requested.get("model")
    if expected_model and _canonical_model(
        model, expected_provider or provider
    ) != _canonical_model(expected_model, expected_provider or provider):
        raise RunRouteMismatch(
            f"requested model {expected_model!r}, parsed {model!r}"
        )

    expected_effort = requested.get("reasoning_effort")
    if expected_effort and _reasoning_effort(reasoning_config) != str(
        expected_effort
    ).strip().lower():
        raise RunRouteMismatch(
            f"requested reasoning effort {expected_effort!r}, parsed "
            f"{_reasoning_effort(reasoning_config)!r}"
        )
    return spec


def runtime_observation(agent: Any, *, phase: str, reason: Any = None) -> dict:
    """Build the secret-free observed route payload for one activation."""
    payload = {
        "version": 1,
        "phase": str(phase),
        "provider": str(getattr(agent, "provider", "") or ""),
        "model": str(getattr(agent, "model", "") or ""),
        "reasoning_effort": _reasoning_effort(
            getattr(agent, "reasoning_config", None)
        ),
        "runtime": str(getattr(agent, "runtime", "hermes") or "hermes"),
        "api_mode": str(getattr(agent, "api_mode", "") or ""),
    }
    if reason is not None:
        payload["reason"] = str(getattr(reason, "value", reason))
    return payload


def _safe_route_snapshot(value: Any) -> Optional[dict]:
    if not isinstance(value, dict):
        return None
    return {
        key: str(value.get(key) or "")
        for key in ("provider", "model", "runtime", "api_mode")
    }


def attach_kanban_runtime_observer(agent: Any) -> bool:
    """Attach a synchronous durable observer and attest the initial route."""
    spec = load_active_run_spec()
    if spec is None:
        return False
    task_id, run_id = _active_identity()
    if not task_id or run_id is None:
        return False

    def observer(
        *, phase: str, reason: Any = None, from_route: Any = None,
    ) -> None:
        from hermes_cli import kanban_db

        payload = runtime_observation(agent, phase=phase, reason=reason)
        safe_from = _safe_route_snapshot(from_route)
        if safe_from is not None:
            payload["from"] = safe_from
        with kanban_db.connect() as conn:
            persisted = kanban_db.record_runtime_observation(
                conn, task_id, run_id, payload,
            )
        if not persisted:
            raise RuntimeObservationError(
                f"could not attest active route for {task_id} run {run_id}"
            )

    agent._runtime_observer = observer
    observer(phase="initial")
    return True


def notify_runtime_observer(
    agent: Any,
    *,
    phase: str,
    reason: Any = None,
    from_route: Any = None,
) -> bool:
    """Synchronously persist a route mutation when the agent is contracted."""
    observer = getattr(agent, "_runtime_observer", None)
    if not callable(observer):
        return False
    observer(phase=phase, reason=reason, from_route=from_route)
    return True
