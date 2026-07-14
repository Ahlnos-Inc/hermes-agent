"""Absolute request-attempt budgets shared by native and external runtimes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hermes_cli.timeouts import (
    get_provider_first_event_timeout,
    get_provider_total_attempt_timeout,
)


@dataclass(frozen=True)
class AttemptBudgets:
    """Monotonic wall-clock limits for one provider attempt."""

    total_seconds: float | None
    first_event_seconds: float | None


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
        total_seconds=float(total) if total is not None else None,
        first_event_seconds=(float(first_event) if first_event is not None else None),
    )


class AttemptDeadlineExceeded(TimeoutError):
    """A provider attempt exceeded an explicit absolute budget."""


__all__ = ["AttemptBudgets", "AttemptDeadlineExceeded", "resolve_attempt_budgets"]
