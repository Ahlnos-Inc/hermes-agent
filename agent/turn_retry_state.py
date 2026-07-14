"""Per-attempt recovery bookkeeping for the conversation turn loop.

The inner retry loop in ``run_conversation`` (``while retry_count <
max_retries``) makes several distinct recovery attempts on a single model API
call: a credential-pool 429 retry, a per-provider OAuth refresh (codex,
anthropic, nous, copilot), a long-context compression restart, a length-
continuation restart, and a handful of format-recovery branches (thinking-
signature stripping, multimodal-tool-content stripping, llama.cpp grammar
fallback, image shrink, invalid-encrypted-content, 1M-beta header).

Each of those branches is guarded by a one-shot boolean so it fires at most
once per attempt. They used to be ~16 bare ``*_attempted`` / ``has_retried_*``
/ ``restart_with_*`` locals declared inline before the loop and threaded
through its 2,400-line body. ``TurnRetryState`` collapses them into one object
the loop mutates in place (``state.codex_auth_retry_attempted = True``), giving
the recovery bookkeeping a single named, testable home.

Loop-control variables (``retry_count``, ``max_retries``,
``max_compression_attempts``) intentionally stay as plain locals — they are the
``while`` mechanics, not recovery bookkeeping, and putting them on the object
would add indirection without clarifying anything.

This module is dependency-free so it can be unit-tested in isolation and
imported by the turn loop without an import cycle.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover — type-only, keeps this module dependency-free
    from agent.error_classifier import ClassifiedError, FailoverReason


@dataclass
class RouteFailureLedger:
    """Availability failures accumulated across one logical user turn.

    Unlike ``TurnRetryState`` this object survives provider switches and the
    native/external runtime recursion boundary.  The route key is optional for
    unit callers; production supplies it so repeated bookkeeping for the same
    abandoned route is idempotent.
    """

    reasons: list[str] = field(default_factory=list)
    _recorded_routes: set[tuple[str, str, str]] = field(
        default_factory=set, repr=False
    )

    def record(
        self,
        reason: "FailoverReason | str",
        *,
        route: dict[str, Any] | None = None,
    ) -> None:
        value = str(getattr(reason, "value", reason) or "").strip()
        if not value:
            return
        if route is not None:
            route_key = (
                str(route.get("runtime") or "hermes").strip().lower(),
                str(route.get("provider") or "").strip().lower(),
                str(route.get("model") or "").strip(),
            )
            if route_key in self._recorded_routes:
                return
            self._recorded_routes.add(route_key)
        self.reasons.append(value)

    def resolve(self, current: "FailoverReason | str") -> str:
        """Return the turn-level terminal class for the attempted routes."""
        from agent.error_classifier import (
            FailoverReason,
            is_provider_availability_reason,
        )

        current_value = str(getattr(current, "value", current) or "").strip()
        attempted = [*self.reasons, current_value]
        if attempted and all(is_provider_availability_reason(item) for item in attempted):
            # Preserve a concrete quota wall for the dispatcher when one was
            # observed.  Otherwise collapse transport/auth/capacity mixtures
            # to the typed provider-unavailable outcome.
            quota_values = {
                FailoverReason.billing.value,
                FailoverReason.rate_limit.value,
                FailoverReason.upstream_rate_limit.value,
            }
            for item in attempted:
                if item in quota_values:
                    return item
            return FailoverReason.provider_unavailable.value
        return current_value


@dataclass
class TurnRetryState:
    """One-shot recovery guards + restart signals for a single API-call attempt.

    A fresh instance is created for each iteration of the outer turn loop
    (once per ``api_call_count``). Each guard fires its recovery branch at most
    once; the ``restart_with_*`` signals are read by the loop after the attempt
    to decide whether to rebuild the request and retry.
    """

    # ── Per-provider OAuth / credential refresh guards ───────────────────
    codex_auth_retry_attempted: bool = False
    anthropic_auth_retry_attempted: bool = False
    nous_auth_retry_attempted: bool = False
    nous_paid_entitlement_refresh_attempted: bool = False
    copilot_auth_retry_attempted: bool = False
    vertex_auth_retry_attempted: bool = False

    # ── Format / payload recovery guards ─────────────────────────────────
    thinking_sig_retry_attempted: bool = False
    invalid_encrypted_content_retry_attempted: bool = False
    image_shrink_retry_attempted: bool = False
    multimodal_tool_content_retry_attempted: bool = False
    oauth_1m_beta_retry_attempted: bool = False
    llama_cpp_grammar_retry_attempted: bool = False

    # ── Transport / rate-limit recovery ──────────────────────────────────
    primary_recovery_attempted: bool = False
    has_retried_429: bool = False

    # ── Auth-failure provider failover ───────────────────────────────────
    # Set once we've escalated a persistent 401/403 (after the per-provider
    # credential-refresh attempt above failed) to the fallback chain, so we
    # don't loop on the same auth failover within one attempt.
    auth_failover_attempted: bool = False

    # ── Restart signals (read by the outer loop after the attempt) ───────
    restart_with_compressed_messages: bool = False
    restart_with_length_continuation: bool = False
    # Set when a content-filter stream stall (e.g. MiniMax "new_sensitive")
    # has been escalated to the fallback chain: the partial-stream content
    # was rolled back off ``messages`` and the loop should re-issue the API
    # call against the newly-activated provider (#32421).
    restart_with_rebuilt_messages: bool = False

    # Shared across all API-attempt state objects created for one logical
    # user turn, including native/external runtime hand-offs.
    route_failures: RouteFailureLedger = field(default_factory=RouteFailureLedger)

    def resolve_failure_reason(
        self, classified: ClassifiedError | None = None, *, reason: str | None = None
    ) -> str:
        """Resolve the terminal ``failure_reason`` value for a turn result.

        Delegates to the turn-wide route ledger.  Availability-only chains
        preserve a concrete quota reason when present, otherwise collapse to
        ``provider_unavailable``.  Any request-specific failure remains the
        terminal reason so an unchanged bad request is not parked forever.

        Exactly one of the two calling conventions applies:
          - ``classified``: pass the current attempt's ``ClassifiedError``
            — used by every site that has one.
          - ``reason``: a raw ``FailoverReason.value`` string for sites
            that die before any ``ClassifiedError`` exists (e.g. the Nous
            Portal preemptive rate-limit guard, which returns before an
            API call is even attempted). The caller vouches this reason is
            itself quota-class, so no origin lookup is needed.
        """
        if classified is not None and reason is not None:
            raise ValueError(
                "resolve_failure_reason: pass classified or reason, not both"
            )
        if classified is not None:
            return self.route_failures.resolve(classified.reason)
        if reason is None:
            raise ValueError("resolve_failure_reason requires classified or reason")
        return self.route_failures.resolve(reason)

    def __iter__(self):
        # Convenience for debugging / tests: iterate (name, value) pairs.
        for f in fields(self):
            yield f.name, getattr(self, f.name)
