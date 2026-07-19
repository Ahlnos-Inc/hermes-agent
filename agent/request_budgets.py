"""Absolute request-attempt budgets shared by native and external runtimes."""

from __future__ import annotations

from dataclasses import dataclass
import math
import threading
from typing import Any
from urllib.parse import urlsplit

from hermes_cli.timeouts import (
    get_provider_first_event_timeout,
    get_provider_total_attempt_timeout,
)
from agent.model_metadata import is_local_endpoint


# Local inference can legitimately spend several minutes loading weights,
# prefilling a large context, and generating a response.  It still must not be
# allowed to run forever when no route policy was supplied.  Fifteen minutes is
# deliberately generous and remains overrideable through the existing typed
# ``providers.<id>[.models.<model>].total_attempt_timeout_seconds`` contract.
DEFAULT_LOCAL_TOTAL_ATTEMPT_TIMEOUT_SECONDS = 15 * 60.0
DEFAULT_BEDROCK_TOTAL_ATTEMPT_TIMEOUT_SECONDS = 15 * 60.0


# A timed-out Python worker cannot be killed safely.  Keep exact routes whose
# transport thread did not unwind quarantined in this process so retries and
# new turns fail fast instead of compounding the hung request.  Local routes
# additionally retain their durable capacity lease in the worker until actual
# unwind, providing the cross-process containment that this in-memory registry
# intentionally cannot provide for cloud routes.
_orphaned_route_threads: dict[tuple[str, ...], set[threading.Thread]] = {}
_orphaned_route_threads_lock = threading.Lock()


@dataclass(frozen=True)
class AttemptBudgets:
    """Monotonic wall-clock limits for one provider attempt.

    ``first_event_seconds`` means the first observable provider protocol event.
    Streaming transports (including Bedrock ConverseStream) satisfy it on any
    decoded provider event, not only a text token.  Non-streaming APIs expose
    no intermediate event, so their complete response is necessarily their
    first observable event and this budget bounds the whole non-stream call.
    """

    total_seconds: float | None
    first_event_seconds: float | None


class ProviderRouteQuarantined(TimeoutError):
    """An earlier timed-out request is still running on the exact route."""


def _positive_timeout(raw: object) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0 or not math.isfinite(value):
        return None
    return value


def _credential_free_endpoint(raw: object) -> tuple[str, str, str, str]:
    """Return a stable endpoint identity without userinfo/query/fragment."""
    value = str(raw or "").strip()
    if not value:
        return ("", "", "", "")
    try:
        parsed = urlsplit(value if "://" in value else f"https://{value}")
        return (
            (parsed.scheme or "").lower(),
            (parsed.hostname or "").lower(),
            str(parsed.port or ""),
            parsed.path.rstrip("/"),
        )
    except (TypeError, ValueError):
        # Do not retain or report a malformed raw URL because it may contain
        # embedded credentials.  Provider/mode/model still scope the route.
        return ("invalid", "", "", "")


def provider_route_key(agent: Any, api_payload: dict[str, Any]) -> tuple[str, ...]:
    """Build the credential-free identity used for orphan containment."""
    model = (
        api_payload.get("model")
        or api_payload.get("modelId")
        or getattr(agent, "model", "")
        or ""
    )
    return (
        str(getattr(agent, "provider", "") or "").strip().lower(),
        str(getattr(agent, "api_mode", "") or "").strip().lower(),
        str(model),
        *_credential_free_endpoint(getattr(agent, "base_url", None)),
    )


def _live_orphan_threads_locked(
    route_key: tuple[str, ...],
) -> set[threading.Thread]:
    threads = _orphaned_route_threads.get(route_key, set())
    live = {thread for thread in threads if thread.is_alive()}
    if live:
        _orphaned_route_threads[route_key] = live
    else:
        _orphaned_route_threads.pop(route_key, None)
    return live


def ensure_provider_route_available(agent: Any, api_payload: dict[str, Any]) -> None:
    """Fail fast while a timed-out worker still owns the exact route."""
    route_key = provider_route_key(agent, api_payload)
    with _orphaned_route_threads_lock:
        live = _live_orphan_threads_locked(route_key)
    if live:
        raise ProviderRouteQuarantined(
            "Provider route is quarantined while a prior timed-out request "
            "is still unwinding"
        )


def quarantine_provider_route(
    agent: Any,
    api_payload: dict[str, Any],
    worker: threading.Thread,
) -> bool:
    """Quarantine ``agent``'s exact route until ``worker`` really exits.

    Returns ``True`` only when a live worker was registered.  Dead workers are
    never quarantined, so cooperative transports recover immediately.
    """
    if not worker.is_alive():
        return False
    route_key = provider_route_key(agent, api_payload)
    with _orphaned_route_threads_lock:
        live = _live_orphan_threads_locked(route_key)
        live.add(worker)
        _orphaned_route_threads[route_key] = live
    return True


def provider_route_is_quarantined(agent: Any) -> bool:
    """Non-raising probe: is ``agent``'s current route still quarantined?

    Lets retry backoff wait on the actual release condition (the orphaned
    worker thread exiting) instead of sleeping a fixed jitter and slamming
    into ``ensure_provider_route_available`` again — the 2026-07-18 cascade
    burned all three retries in ~8s against a quarantine that releases on
    thread exit, then failed the turn terminally.
    """
    route_key = provider_route_key(agent, {})
    with _orphaned_route_threads_lock:
        return bool(_live_orphan_threads_locked(route_key))


def _reset_provider_route_quarantine_for_tests() -> None:
    with _orphaned_route_threads_lock:
        _orphaned_route_threads.clear()


def resolve_attempt_budgets(agent: Any) -> AttemptBudgets:
    """Resolve route overrides, then provider policy, with safe clamping.

    Existing ``request_timeout_seconds`` is intentionally treated as the
    total-attempt fallback. Historically it only configured socket operations,
    which allowed a chunking stream to run forever and violated the operator's
    expectation of a request timeout.
    """
    total = getattr(agent, "_route_total_attempt_timeout_seconds", None)
    if total is None:
        total = getattr(agent, "_route_request_timeout_seconds", None)
    if total is None:
        total = get_provider_total_attempt_timeout(
            str(getattr(agent, "provider", "") or ""),
            str(getattr(agent, "model", "") or "") or None,
        )
    if total is None:
        base_url = getattr(agent, "base_url", None)
        if isinstance(base_url, str) and base_url and is_local_endpoint(base_url):
            total = DEFAULT_LOCAL_TOTAL_ATTEMPT_TIMEOUT_SECONDS
        elif getattr(agent, "api_mode", None) == "bedrock_converse":
            # Native Bedrock bypasses the OpenAI/httpx request-timeout path.
            # Give it an absolute default so its request-scoped botocore
            # connect/read timeouts can always be derived from finite policy.
            total = DEFAULT_BEDROCK_TOTAL_ATTEMPT_TIMEOUT_SECONDS

    first_event = getattr(agent, "_route_first_event_timeout_seconds", None)
    if first_event is None:
        first_event = get_provider_first_event_timeout(
            str(getattr(agent, "provider", "") or ""),
            str(getattr(agent, "model", "") or "") or None,
        )
    if first_event is None and total is not None:
        first_event = min(float(total), 120.0)
    if total is not None and first_event is not None:
        first_event = min(float(first_event), float(total))
    return AttemptBudgets(
        total_seconds=_positive_timeout(total),
        first_event_seconds=_positive_timeout(first_event),
    )


class AttemptDeadlineExceeded(TimeoutError):
    """A provider attempt exceeded an explicit absolute budget."""


__all__ = [
    "AttemptBudgets",
    "AttemptDeadlineExceeded",
    "DEFAULT_BEDROCK_TOTAL_ATTEMPT_TIMEOUT_SECONDS",
    "DEFAULT_LOCAL_TOTAL_ATTEMPT_TIMEOUT_SECONDS",
    "ProviderRouteQuarantined",
    "ensure_provider_route_available",
    "provider_route_key",
    "quarantine_provider_route",
    "resolve_attempt_budgets",
]
