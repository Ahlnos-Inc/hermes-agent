"""BUILD-343 regression: a quota-class failover origin must survive to the
terminal ``failure_reason`` even when the LAST hop in the fallback chain
fails with a transport-class error.

Incident shape: a kanban worker's fallback chain ends at a local model
(``omlx-local``) with no billing/rate-limit concept. When the primary
provider fails on billing/rate-limit and the chain walks down to that local
tier, a connection failure there is transport-class (``FailoverReason.
timeout``). Before this fix, the terminal ``result["failure_reason"]`` was
whatever the LAST hop classified as — the transport reason — so
``cli.py``'s kanban exit-code gate (``rate_limit`` / ``billing`` /
``upstream_rate_limit`` -> exit 75) never fired. The worker exited 1,
``_classify_worker_exit`` called it ``"crashed"``, and it counted toward
the circuit breaker instead of the graceful ``rate_limited`` requeue.

This violates the documented contract in
``~/.hermes/profiles/orchestrator/skills/devops/
hermes-runtime-routing-debugging/SKILL.md``: "when the fallback chain
exhausts, Hermes surfaces the original primary error, not the last
fallback error."

Fix: a turn-wide ``RouteFailureLedger`` records each abandoned route.  When
the complete chain contains only provider-availability failures, the terminal
result preserves a concrete quota reason when one exists; otherwise it emits
``provider_unavailable``.  Request-specific failures are never masked.

Harness mirrors test_32646_fallback_429_after_timeout.py and
test_build342_pool_exhausted_fallback.py: drive the real
``run_conversation()`` loop, mock only the two true I/O boundaries (the
outer API call and the fallback provider's client construction).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.runtime_circuit import runtime_circuit_status
from run_agent import AIAgent


def _make_agent(fallback_model=None):
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI", return_value=MagicMock()),
    ):
        agent = AIAgent(
            api_key="primary-key-abcdef12",
            base_url="https://api.deepseek.com",
            provider="deepseek",
            model="deepseek-chat",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=fallback_model,
        )
        agent.client = MagicMock()
        return agent


def _mock_response(content: str):
    msg = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    return SimpleNamespace(choices=[choice], model="fallback/model", usage=None)


def _billing_error():
    err = Exception(
        "Error code: 402 - Insufficient balance, please add funds to your account."
    )
    err.status_code = 402
    return err


_LOCAL_FALLBACK_CHAIN = [
    {
        "provider": "omlx-local",
        "model": "local-model",
        "base_url": "http://localhost:8080/v1",
    }
]


def _mock_fallback_client():
    mock = MagicMock()
    mock.base_url = "http://localhost:8080/v1"
    mock.api_key = "no-key-needed"
    mock._custom_headers = None
    mock.default_headers = None
    return mock


class TestQuotaOriginSurvivesTransportTailFailure:
    def test_billing_then_transport_exhaustion_reports_billing_as_failure_reason(self):
        agent = _make_agent(fallback_model=_LOCAL_FALLBACK_CHAIN)
        agent._api_max_retries = 2

        calls = []

        def fake_api_call(api_kwargs):
            calls.append((agent.provider, agent.model))
            if len(calls) == 1:
                raise _billing_error()
            # Every subsequent attempt is on the exhausted local tail of
            # the chain: pure transport failure, no billing/rate-limit
            # concept for a local model server.
            raise ConnectionError("connection refused: omlx-local unreachable")

        mock_fb_client = _mock_fallback_client()

        with (
            patch.object(agent, "_interruptible_api_call", side_effect=fake_api_call),
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
            patch("agent.agent_runtime_helpers.time.sleep"),
            patch(
                "agent.auxiliary_client.resolve_provider_client",
                return_value=(mock_fb_client, "local-model"),
            ) as mock_resolve,
            patch(
                "hermes_cli.model_normalize.normalize_model_for_provider",
                side_effect=lambda m, p: m,
            ),
            patch("agent.model_metadata.get_model_context_length", return_value=200000),
        ):
            result = agent.run_conversation("hello")

        # Guard against a vacuous pass: billing fired once, then the chain's
        # local tail was actually exercised (and exhausted — no third
        # fallback hop exists).
        assert calls[0] == ("deepseek", "deepseek-chat")
        assert all(c == ("omlx-local", "local-model") for c in calls[1:])
        assert len(calls) >= 2
        mock_resolve.assert_called_once()
        assert agent._fallback_activated is True

        assert result["failed"] is True
        assert result["completed"] is False
        # The whole point of the fix: the terminal classification is the
        # quota-class ORIGIN (billing), not the last hop's transport error.
        assert result["failure_reason"] == "billing"
        # The last error's own text must still be present in the summary —
        # only the failure_reason classification prefers the origin.
        assert "omlx-local" in result["error"] or "connection refused" in result["error"].lower()

    def test_transport_only_exhaustion_is_typed_provider_unavailable(
        self, tmp_path, monkeypatch
    ):
        """A route exhausted only by infrastructure faults is parkable.

        The terminal type describes the turn-wide availability outcome rather
        than exposing whichever transport subtype happened to be last.
        """
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
        agent = _make_agent(fallback_model=None)
        agent._api_max_retries = 2
        primary_route = dict(agent._primary_runtime)

        def fake_api_call(api_kwargs):
            raise ConnectionError("connection refused")

        with (
            patch.object(agent, "_interruptible_api_call", side_effect=fake_api_call),
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
            patch("agent.agent_runtime_helpers.time.sleep"),
        ):
            result = agent.run_conversation("hello")

        assert result["failed"] is True
        assert result["completed"] is False
        assert result["failure_reason"] == "provider_unavailable"
        status = runtime_circuit_status(agent, target=primary_route)
        assert status is not None
        assert status.reason == "timeout"
