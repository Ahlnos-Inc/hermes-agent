"""Unit tests for TurnRetryState (god-file Phase 1b).

The dataclass holds the inner-retry-loop's one-shot recovery guards + restart
signals. These tests pin its shape and default semantics — the behavioral
guarantee for the loop itself is the existing recovery-branch tests in
tests/run_agent/ which now exercise these fields via `_retry.<flag>`.
"""

from __future__ import annotations

from dataclasses import fields

import pytest

from agent.error_classifier import ClassifiedError, FailoverReason
from agent.turn_retry_state import RouteFailureLedger, TurnRetryState


EXPECTED_FIELDS = {
    "codex_auth_retry_attempted",
    "anthropic_auth_retry_attempted",
    "nous_auth_retry_attempted",
    "nous_paid_entitlement_refresh_attempted",
    "copilot_auth_retry_attempted",
    "vertex_auth_retry_attempted",
    "thinking_sig_retry_attempted",
    "invalid_encrypted_content_retry_attempted",
    "image_shrink_retry_attempted",
    "multimodal_tool_content_retry_attempted",
    "oauth_1m_beta_retry_attempted",
    "llama_cpp_grammar_retry_attempted",
    "primary_recovery_attempted",
    "has_retried_429",
    "auth_failover_attempted",
    "restart_with_compressed_messages",
    "restart_with_length_continuation",
    "restart_with_rebuilt_messages",
    "route_failures",
}

# Fields that aren't one-shot boolean guards — checked separately below.
_NON_BOOL_FIELDS = {"route_failures"}


def test_all_guards_default_false():
    s = TurnRetryState()
    for name, value in s:
        if name in _NON_BOOL_FIELDS:
            continue
        assert value is False, f"{name} should default to False"


def test_field_set_matches_contract():
    names = {f.name for f in fields(TurnRetryState)}
    assert names == EXPECTED_FIELDS, (
        f"unexpected drift: missing={EXPECTED_FIELDS - names} extra={names - EXPECTED_FIELDS}"
    )


def test_loop_control_vars_are_not_on_state():
    # retry_count / max_retries / max_compression_attempts stay as loop locals,
    # NOT on the state object (they are while-mechanics, not recovery bookkeeping).
    names = {f.name for f in fields(TurnRetryState)}
    for loop_local in ("retry_count", "max_retries", "max_compression_attempts"):
        assert loop_local not in names


def test_guards_are_independently_mutable():
    s = TurnRetryState()
    s.codex_auth_retry_attempted = True
    s.restart_with_compressed_messages = True
    assert s.codex_auth_retry_attempted is True
    assert s.restart_with_compressed_messages is True
    # untouched guards stay False
    assert s.has_retried_429 is False
    assert s.anthropic_auth_retry_attempted is False


class TestResolveFailureReason:
    """BUILD-472: one availability aggregate serves every terminal site."""

    def test_quota_class_classified_reports_its_own_reason(self):
        s = TurnRetryState()
        classified = ClassifiedError(reason=FailoverReason.billing)
        assert s.resolve_failure_reason(classified) == "billing"

    def test_single_availability_failure_reports_provider_unavailable(self):
        s = TurnRetryState()
        classified = ClassifiedError(reason=FailoverReason.timeout)
        assert s.resolve_failure_reason(classified) == "provider_unavailable"

    def test_availability_chain_with_quota_preserves_quota_reason(self):
        s = TurnRetryState()
        s.route_failures.record(FailoverReason.billing)
        classified = ClassifiedError(reason=FailoverReason.timeout)
        assert s.resolve_failure_reason(classified) == "billing"

    def test_request_specific_tail_is_not_masked_by_earlier_quota(self):
        s = TurnRetryState()
        s.route_failures.record(FailoverReason.billing)
        classified = ClassifiedError(reason=FailoverReason.format_error)
        assert s.resolve_failure_reason(classified) == "format_error"

    def test_raw_reason_string_with_no_classified_error(self):
        # Sites that die before any ClassifiedError exists this turn (the
        # Nous Portal preemptive rate-limit guard) pass the reason directly.
        s = TurnRetryState()
        assert s.resolve_failure_reason(reason="rate_limit") == "rate_limit"

    def test_raises_when_neither_classified_nor_reason_given(self):
        s = TurnRetryState()
        with pytest.raises(ValueError):
            s.resolve_failure_reason()

    def test_raises_when_both_classified_and_reason_given(self):
        # Exactly one calling convention applies (docstring contract) —
        # passing both is caller confusion, not a case to silently resolve.
        s = TurnRetryState()
        classified = ClassifiedError(reason=FailoverReason.billing)
        with pytest.raises(ValueError):
            s.resolve_failure_reason(classified, reason="rate_limit")


class TestRouteFailureLedger:
    def test_all_availability_failures_resolve_to_provider_unavailable(self):
        ledger = RouteFailureLedger()
        ledger.record(FailoverReason.timeout)

        assert (
            ledger.resolve(FailoverReason.overloaded)
            == FailoverReason.provider_unavailable.value
        )
