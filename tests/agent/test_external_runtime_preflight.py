from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent.external_runtime import (
    _load_persisted_claude_session_id,
    _persist_claude_session_id,
    run_claude_agent_sdk_attempt,
)
from agent.claude_agent_runtime import ClaudeProjection, RuntimeFailure
from agent.error_classifier import FailoverReason


def _tool(name):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": name,
            "parameters": {"type": "object", "properties": {}},
        },
    }


def test_missing_lazy_sdk_becomes_replay_safe_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "BUILD-392")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path))
    agent = SimpleNamespace(provider="anthropic", model="claude-sonnet-4-6")
    with patch(
        "agent.external_runtime.load_claude_agent_sdk",
        side_effect=ImportError("offline and SDK is not installed"),
    ):
        projection = run_claude_agent_sdk_attempt(
            agent, user_message="work", effective_task_id="task"
        )

    assert projection.failure is not None
    assert projection.failure.replay_safe is True
    assert "SDK is not installed" in projection.failure.message


def test_external_runtime_uses_active_profile_capability_policy(
    monkeypatch, tmp_path
):
    profile_home = tmp_path / ".hermes" / "profiles" / "reviewer"
    profile_home.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "BUILD-425")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    seen_options = []

    class FakeOptions:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class FakeHookMatcher:
        def __init__(self, *, matcher, hooks, timeout=None):
            self.matcher = matcher
            self.hooks = hooks
            self.timeout = timeout

    class ResultMessage:
        session_id = "sdk-session"
        result = "reviewed"
        usage = {"input_tokens": 1, "output_tokens": 1}
        is_error = False
        errors = None
        api_error_status = None

    class FakeClient:
        def __init__(self, options):
            seen_options.append(options)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def query(self, prompt):
            return None

        async def receive_response(self):
            yield ResultMessage()

    class FakeSdk:
        ClaudeAgentOptions = FakeOptions
        ClaudeSDKClient = FakeClient
        HookMatcher = FakeHookMatcher

        @staticmethod
        def tool(name, description, input_schema):
            def decorate(handler):
                handler.sdk_name = name
                return handler

            return decorate

        @staticmethod
        def create_sdk_mcp_server(*, name, version, tools):
            return {"name": name, "version": version, "tools": tools}

    agent = SimpleNamespace(
        provider="anthropic",
        model="claude-fable-5",
        tools=[
            _tool("terminal"),
            _tool("process"),
            _tool("read_file"),
            _tool("write_file"),
            _tool("kanban_complete"),
        ],
        _cached_system_prompt="review only",
        max_iterations=10,
        stream_delta_callback=None,
        tool_progress_callback=None,
        _claude_runtime_context={
            "sdk": FakeSdk,
            "host_home": tmp_path,
            "workspace": workspace,
            "cli_wrapper": tmp_path / "wrapper",
            "kanban_task_id": "BUILD-425",
        },
    )

    projection = run_claude_agent_sdk_attempt(
        agent, user_message="review", effective_task_id="worker-task"
    )

    assert projection.final_text == "reviewed"
    assert seen_options[0].tools == [
        "mcp__hermes__kanban_complete",
        "mcp__hermes__read_file",
        "mcp__hermes__terminal",
    ]


def test_claude_session_id_round_trips_through_hermes_session_metadata():
    class FakeDb:
        row = {"model_config": '{"runtime": "claude_agent_sdk"}'}

        def get_session(self, session_id):
            return dict(self.row)

        def update_session_meta(self, session_id, model_config, model):
            self.row = {"model_config": model_config, "model": model}

    db = FakeDb()
    first = SimpleNamespace(
        _session_db=db, session_id="hermes-session", model="claude-sonnet-4-6"
    )
    _persist_claude_session_id(first, "claude-session-123")
    restarted = SimpleNamespace(_session_db=db, session_id="hermes-session")

    assert _load_persisted_claude_session_id(restarted) == "claude-session-123"


def test_auth_failure_clears_preflight_and_session_for_reattestation(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "BUILD-392")

    class FailedSession:
        closed = False

        def run_turn(self, prompt):
            return ClaudeProjection(
                failure=RuntimeFailure(FailoverReason.auth, "expired")
            )

        def close(self):
            self.closed = True

    session = FailedSession()
    key = ("anthropic", "claude-sonnet-4-6")
    agent = SimpleNamespace(
        provider=key[0],
        model=key[1],
        _claude_runtime_context={
            "sdk": object(),
            "host_home": tmp_path,
            "workspace": tmp_path,
            "cli_wrapper": tmp_path / "wrapper",
            "kanban_task_id": "BUILD-392",
        },
        _claude_sdk_sessions={key: session},
        stream_delta_callback=None,
        _claude_max_attestation=SimpleNamespace(included_usage=True),
    )

    projection = run_claude_agent_sdk_attempt(
        agent, user_message="work", effective_task_id="task"
    )

    assert projection.failure.reason is FailoverReason.auth
    assert agent._claude_runtime_context is None
    assert agent._claude_max_attestation is None
    assert agent._claude_sdk_sessions == {}
    assert session.closed is True


def test_raised_auth_failure_also_clears_preflight_and_session(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "BUILD-392")

    class AuthError(Exception):
        status_code = 401

    class FailedSession:
        closed = False

        def run_turn(self, prompt):
            raise AuthError("expired")

        def close(self):
            self.closed = True

    session = FailedSession()
    key = ("anthropic", "claude-sonnet-4-6")
    agent = SimpleNamespace(
        provider=key[0],
        model=key[1],
        _claude_runtime_context={
            "sdk": object(),
            "host_home": tmp_path,
            "workspace": tmp_path,
            "cli_wrapper": tmp_path / "wrapper",
            "kanban_task_id": "BUILD-392",
        },
        _claude_sdk_sessions={key: session},
        stream_delta_callback=None,
        _claude_max_attestation=SimpleNamespace(included_usage=True),
    )

    projection = run_claude_agent_sdk_attempt(
        agent, user_message="work", effective_task_id="task"
    )

    assert projection.failure.reason is FailoverReason.auth
    assert agent._claude_runtime_context is None
    assert agent._claude_max_attestation is None
    assert agent._claude_sdk_sessions == {}
    assert session.closed is True
