from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from pathlib import Path
import time

import pytest

from agent.error_classifier import FailoverReason
from agent.runtime_circuit import open_runtime_circuit, runtime_circuit_status
from run_agent import AIAgent


def _make_agent(*, runtime="claude_agent_sdk", fallback_model=None):
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
    ):
        return AIAgent(
            api_key="test-key" if runtime == "hermes" else None,
            base_url="https://example.invalid/v1" if runtime == "hermes" else None,
            provider="anthropic",
            model="claude-sonnet-4-6",
            runtime=runtime,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=fallback_model,
        )


def _mock_response(content: str):
    message = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(message=message, finish_reason="stop")
    return SimpleNamespace(choices=[choice], model="fallback/model", usage=None)


def _mock_native_fallback_client():
    client = MagicMock()
    client.base_url = "http://localhost:8080/v1"
    client.api_key = "local"
    client._custom_headers = None
    client.default_headers = None
    return client


def test_subscription_runtime_needs_no_api_key_and_is_snapshotted():
    agent = _make_agent()

    assert agent.runtime == "claude_agent_sdk"
    assert agent.api_key == ""
    assert agent.client is None
    assert agent._primary_runtime["runtime"] == "claude_agent_sdk"
    assert agent._current_main_runtime()["runtime"] == "claude_agent_sdk"


def test_each_fallback_keeps_its_own_runtime_configuration():
    agent = _make_agent(
        fallback_model=[
            {
                "provider": "anthropic",
                "model": "claude-opus-4-6",
                "runtime": "claude_agent_sdk",
            },
            {
                "provider": "openai-codex",
                "model": "gpt-5.4",
                "openai_runtime": "codex_app_server",
            },
        ]
    )

    assert [entry["runtime"] for entry in agent._fallback_chain] == [
        "claude_agent_sdk",
        "codex_app_server",
    ]


def test_external_fallback_activates_without_provider_credentials():
    agent = _make_agent(
        runtime="hermes",
        fallback_model={
            "provider": "anthropic",
            "model": "claude-opus-4-6",
            "runtime": "claude_agent_sdk",
        },
    )

    assert agent._try_activate_fallback() is True
    assert agent.runtime == "claude_agent_sdk"
    assert agent.model == "claude-opus-4-6"
    assert agent.client is None


def test_open_target_circuit_skips_to_next_fallback_without_activation(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    unavailable = {
        "provider": "anthropic",
        "model": "claude-opus-4-6",
        "runtime": "claude_agent_sdk",
    }
    agent = _make_agent(
        runtime="hermes",
        fallback_model=[
            unavailable,
            {
                "provider": "openai-codex",
                "model": "gpt-5.4",
                "runtime": "codex_app_server",
            },
        ],
    )
    agent._claude_max_attestation = type(
        "Auth", (), {"account_key": "account-a"}
    )()
    open_runtime_circuit(
        agent,
        target=unavailable,
        reset_at=time.time() + 3600,
        reason="auth_permanent",
    )

    assert agent._try_activate_fallback() is True
    assert agent.provider == "openai-codex"
    assert agent.model == "gpt-5.4"
    assert agent.runtime == "codex_app_server"


def test_classified_failure_opens_reason_aware_circuit_for_native_route(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    agent = _make_agent(
        runtime="hermes",
        fallback_model={
            "provider": "openai-codex",
            "model": "gpt-5.4",
            "runtime": "codex_app_server",
        },
    )
    started = time.time()

    assert agent._try_activate_fallback(reason=FailoverReason.timeout) is True

    status = runtime_circuit_status(agent, target=agent._primary_runtime)
    assert status is not None
    assert status.reason == "timeout"
    assert status.until >= started + 299


def test_request_specific_failure_does_not_poison_shared_route_circuit(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    agent = _make_agent(runtime="hermes")
    primary_route = dict(agent._primary_runtime)

    assert agent._try_activate_fallback(reason=FailoverReason.format_error) is False
    assert runtime_circuit_status(agent, target=primary_route) is None


def test_native_primary_preflights_shared_circuit_before_first_network_call(
    tmp_path, monkeypatch
):
    """A fresh native worker must skip a primary route another worker opened.

    The immutable primary snapshot is launch intent, so circuit preflight may
    activate a fallback but must not rewrite that requested route in place.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    fallback = {
        "provider": "omlx-local",
        "model": "local-model",
        "base_url": "http://localhost:8080/v1",
    }
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI", return_value=MagicMock()),
    ):
        agent = AIAgent(
            api_key="primary-key",
            base_url="https://api.deepseek.com/v1",
            provider="deepseek",
            model="deepseek-chat",
            runtime="hermes",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=fallback,
        )
    requested_route = dict(agent._primary_runtime)
    open_runtime_circuit(
        agent,
        target=requested_route,
        reset_at=time.time() + 3600,
        reason=FailoverReason.billing,
    )
    attempts = []
    observations = []
    agent._runtime_observer = lambda **payload: observations.append(payload)

    def api_call(_api_kwargs):
        attempts.append((agent.provider, agent.model))
        return _mock_response("fallback succeeded")

    with (
        patch.object(agent, "_interruptible_api_call", side_effect=api_call),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(_mock_native_fallback_client(), "local-model"),
        ),
        patch(
            "hermes_cli.model_normalize.normalize_model_for_provider",
            side_effect=lambda model, _provider: model,
        ),
        patch("agent.model_metadata.get_model_context_length", return_value=200000),
    ):
        result = agent.run_conversation("do the card")

    assert result["completed"] is True
    assert attempts == [("omlx-local", "local-model")]
    assert agent._primary_runtime == requested_route
    assert observations == [
        {
            "phase": "fallback",
            "reason": FailoverReason.billing,
            "from_route": {
                "provider": "deepseek",
                "model": "deepseek-chat",
                "runtime": "hermes",
                "api_mode": "chat_completions",
            },
        }
    ]


def test_native_open_circuit_without_fallback_parks_without_network_call(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI", return_value=MagicMock()),
    ):
        agent = AIAgent(
            api_key="primary-key",
            base_url="https://api.deepseek.com/v1",
            provider="deepseek",
            model="deepseek-chat",
            runtime="hermes",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    open_runtime_circuit(
        agent,
        target=agent._primary_runtime,
        reset_at=time.time() + 3600,
        reason=FailoverReason.billing,
    )

    with (
        patch.object(agent, "_interruptible_api_call") as api_call,
        patch.object(agent, "_interruptible_streaming_api_call") as streaming_call,
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("do the card")

    api_call.assert_not_called()
    streaming_call.assert_not_called()
    assert result["failed"] is True
    assert result["completed"] is False
    assert result["failure_reason"] == "billing"
    assert "circuit" in result["error"].lower()


def test_native_chain_records_primary_and_anthropic_entitlement_failures(
    tmp_path, monkeypatch
):
    """Every unavailable route is circuit-opened before chain exhaustion."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    fallback = {
        "provider": "anthropic",
        "model": "claude-opus-4-6",
        "base_url": "https://api.anthropic.com/v1",
    }
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI", return_value=MagicMock()),
    ):
        agent = AIAgent(
            api_key="primary-key",
            base_url="https://api.deepseek.com/v1",
            provider="deepseek",
            model="deepseek-chat",
            runtime="hermes",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=fallback,
        )
    agent._disable_streaming = True
    agent._api_max_retries = 1
    primary_route = dict(agent._primary_runtime)
    attempts = []

    def api_call(_api_kwargs):
        attempts.append((agent.provider, agent.model))
        if agent.provider == "deepseek":
            error = Exception("402 Insufficient balance, please add funds")
            error.status_code = 402
            raise error
        error = Exception(
            "400 invalid_request_error: Third-party apps now draw from extra "
            "usage, not plan limits"
        )
        error.status_code = 400
        raise error

    anthropic_client = _mock_native_fallback_client()
    anthropic_client.base_url = "https://api.anthropic.com/v1"
    anthropic_client.api_key = "anthropic-key"

    with (
        patch.object(agent, "_interruptible_api_call", side_effect=api_call),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(anthropic_client, "claude-opus-4-6"),
        ),
        patch("agent.anthropic_adapter.build_anthropic_client", return_value=MagicMock()),
        patch("agent.anthropic_adapter._is_oauth_token", return_value=True),
        patch(
            "hermes_cli.model_normalize.normalize_model_for_provider",
            side_effect=lambda model, _provider: model,
        ),
        patch("agent.model_metadata.get_model_context_length", return_value=200000),
    ):
        result = agent.run_conversation("do the card")

    assert attempts == [
        ("deepseek", "deepseek-chat"),
        ("anthropic", "claude-opus-4-6"),
    ]
    assert result["failed"] is True
    assert result["failure_reason"] == "billing"
    primary_status = runtime_circuit_status(agent, target=primary_route)
    fallback_status = runtime_circuit_status(agent, target={**fallback, "runtime": "hermes"})
    assert primary_status is not None
    assert primary_status.reason == "billing"
    assert fallback_status is not None
    assert fallback_status.reason == "billing"


def test_exhausted_credential_pool_skips_fallback_before_activation(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    exhausted_pool = type(
        "Pool",
        (),
        {
            "has_credentials": lambda self: True,
            "has_available": lambda self: False,
        },
    )()
    empty_pool = type(
        "Pool",
        (),
        {
            "has_credentials": lambda self: False,
            "has_available": lambda self: False,
        },
    )()
    agent = _make_agent(
        runtime="hermes",
        fallback_model=[
            {
                "provider": "anthropic",
                "model": "claude-opus-4-6",
                "runtime": "claude_agent_sdk",
            },
            {
                "provider": "openai-codex",
                "model": "gpt-5.4",
                "runtime": "codex_app_server",
            },
        ],
    )

    with patch(
        "agent.credential_pool.load_pool",
        side_effect=lambda provider: (
            exhausted_pool if provider == "anthropic" else empty_pool
        ),
    ):
        assert agent._try_activate_fallback() is True

    assert agent.provider == "openai-codex"
    assert agent.model == "gpt-5.4"
    assert agent.runtime == "codex_app_server"


def test_successful_external_fallback_attests_before_returning():
    agent = _make_agent(
        runtime="hermes",
        fallback_model={
            "provider": "anthropic",
            "model": "claude-opus-4-6",
            "runtime": "claude_agent_sdk",
        },
    )
    observations = []
    agent._runtime_observer = lambda **payload: observations.append(payload)

    assert agent._try_activate_fallback() is True

    assert observations == [
        {
            "phase": "fallback",
            "reason": None,
            "from_route": {
                "provider": "anthropic",
                "model": "claude-sonnet-4-6",
                "runtime": "hermes",
                "api_mode": "anthropic_messages",
            },
        }
    ]


def test_fallback_observer_failure_is_not_treated_as_another_provider_failure():
    from hermes_cli.kanban_runtime_contract import RuntimeObservationError

    agent = _make_agent(
        runtime="hermes",
        fallback_model={
            "provider": "anthropic",
            "model": "claude-opus-4-6",
            "runtime": "claude_agent_sdk",
        },
    )

    def fail_observation(**_payload):
        raise RuntimeObservationError("database unavailable")

    agent._runtime_observer = fail_observation

    with pytest.raises(RuntimeObservationError, match="database unavailable"):
        agent._try_activate_fallback()


def test_restore_primary_restores_agent_loop_runtime():
    agent = _make_agent(
        fallback_model={
            "provider": "anthropic",
            "model": "claude-opus-4-6",
            "runtime": "claude_agent_sdk",
        }
    )
    agent.runtime = "hermes"
    agent._fallback_activated = True

    assert agent._restore_primary_runtime() is True
    assert agent.runtime == "claude_agent_sdk"


def test_restore_primary_attests_route_mutation():
    agent = _make_agent(
        fallback_model={
            "provider": "openai-codex",
            "model": "gpt-5.4",
            "runtime": "codex_app_server",
        }
    )
    agent.model = "gpt-5.4"
    agent.provider = "openai-codex"
    agent.api_mode = "codex_app_server"
    agent.runtime = "codex_app_server"
    agent._fallback_activated = True
    agent._rate_limited_until = 0
    observed = []
    agent._runtime_observer = lambda **payload: observed.append(payload)

    assert agent._restore_primary_runtime() is True
    assert observed == [
        {
            "phase": "primary",
            "reason": "turn_restore",
            "from_route": {
                "provider": "openai-codex",
                "model": "gpt-5.4",
                "runtime": "codex_app_server",
                "api_mode": "codex_app_server",
            },
        }
    ]


def test_reset_aware_primary_circuit_prevents_early_restore():
    agent = _make_agent(
        fallback_model={
            "provider": "openai-codex",
            "model": "gpt-5.4",
            "runtime": "codex_app_server",
        }
    )
    open_runtime_circuit(agent, reset_at=time.time() + 3600)
    assert agent._try_activate_fallback() is True
    agent._rate_limited_until = 0

    assert agent._restore_primary_runtime() is False
    assert agent.runtime == "codex_app_server"


def test_runtime_circuit_survives_a_fresh_agent_process_state(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    first = _make_agent()
    first._claude_max_attestation = type("Auth", (), {"account_key": "account-a"})()
    expected = open_runtime_circuit(first, reset_at=time.time() + 3600)

    second = _make_agent()
    second._claude_max_attestation = type("Auth", (), {"account_key": "account-a"})()

    from agent.runtime_circuit import runtime_circuit_open_until

    assert runtime_circuit_open_until(second) == expected


def test_runtime_circuit_is_shared_across_profiles_and_retains_reason(
    monkeypatch, tmp_path
):
    root = tmp_path / ".hermes"
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "coder"))
    first = _make_agent()
    first._claude_max_attestation = type(
        "Auth", (), {"account_key": "account-a"}
    )()

    expected = open_runtime_circuit(
        first,
        reset_at=time.time() + 3600,
        reason="auth_permanent",
    )

    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "verifier"))
    second = _make_agent()
    second._claude_max_attestation = type(
        "Auth", (), {"account_key": "account-a"}
    )()

    from agent.runtime_circuit import runtime_circuit_status

    status = runtime_circuit_status(second)
    assert status is not None
    assert status.until == expected
    assert status.reason == "auth_permanent"
    assert (root / "shared" / "runtime-circuits.json").exists()


def test_fresh_worker_attests_before_account_scoped_circuit_lookup(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    first = _make_agent()
    first._claude_max_attestation = type("Auth", (), {"account_key": "account-a"})()
    open_runtime_circuit(first, reset_at=time.time() + 3600)
    second = _make_agent()

    def attest(agent):
        agent._claude_max_attestation = type(
            "Auth", (), {"account_key": "account-a"}
        )()
        return None

    with (
        patch("agent.external_runtime.prepare_claude_agent_sdk_runtime", side_effect=attest),
        patch("agent.external_runtime.run_claude_agent_sdk_attempt") as attempt,
    ):
        result = second.run_conversation("do the card")

    assert result["completed"] is False
    assert "circuit open" in result["error"]
    attempt.assert_not_called()
