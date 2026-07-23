import os
import stat
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import agent.external_runtime as external_runtime
from agent.external_runtime import (
    _load_persisted_claude_session_id,
    _persist_claude_session_id,
    prepare_claude_agent_sdk_runtime,
    prepare_claude_sdk_temp_dir,
    run_claude_agent_sdk_attempt,
)
from agent.claude_agent_runtime import ClaudeProjection, RuntimeFailure
from agent.claude_cli_boundary import (
    ClaudeAttestationRejectedError,
    ClaudeAttestationTransientError,
)
from agent.error_classifier import FailoverReason
from agent.claude_workspace_terminal import WorkspaceBoundaryProvisioningError


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


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            ClaudeAttestationTransientError(
                "probe unavailable", diagnostic={"kind": "timeout"}
            ),
            # A transient attestation is a retryable provider-availability
            # outage, not an unclassifiable error — routed as ``auth`` so a
            # token-refresh blip requeues instead of fail-closing the card.
            FailoverReason.auth,
        ),
        (
            ClaudeAttestationRejectedError(
                "wrong subscription", diagnostic={"kind": "subscription_mismatch"}
            ),
            FailoverReason.auth_permanent,
        ),
    ],
)
def test_preflight_uses_typed_attestation_classification(
    monkeypatch, tmp_path, error, expected
):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "BUILD-472")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(external_runtime.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(external_runtime, "load_claude_agent_sdk", lambda: SimpleNamespace(__file__=__file__))
    monkeypatch.setattr(external_runtime, "get_host_user_home", lambda: tmp_path)
    monkeypatch.setattr(external_runtime, "build_claude_subscription_env", lambda *a, **k: {})
    monkeypatch.setattr(external_runtime, "create_exact_env_cli_wrapper", lambda *a, **k: tmp_path / "wrapper")
    monkeypatch.setattr(external_runtime, "attest_claude_max_auth", lambda *a, **k: (_ for _ in ()).throw(error))

    failure = prepare_claude_agent_sdk_runtime(
        SimpleNamespace(provider="anthropic", model="claude-haiku")
    )

    assert failure is not None
    assert failure.reason is expected


def test_workspace_boundary_failure_is_typed_and_precedes_auth_attestation(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "BUILD-708")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(external_runtime.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        external_runtime,
        "load_claude_agent_sdk",
        lambda: SimpleNamespace(__file__=__file__),
    )
    monkeypatch.setattr(external_runtime, "get_host_user_home", lambda: tmp_path)
    monkeypatch.setattr(
        external_runtime,
        "build_claude_subscription_env",
        lambda *a, **k: {"PATH": "/usr/bin:/bin"},
    )
    monkeypatch.setattr(
        external_runtime,
        "prepare_workspace_terminal_boundary",
        lambda *a, **k: (_ for _ in ()).throw(
            WorkspaceBoundaryProvisioningError("external alias")
        ),
    )
    attestation_called = []
    monkeypatch.setattr(
        external_runtime,
        "attest_claude_max_auth",
        lambda *a, **k: attestation_called.append(True),
    )

    failure = prepare_claude_agent_sdk_runtime(
        SimpleNamespace(provider="anthropic", model="claude-haiku")
    )

    assert failure is not None
    assert failure.provisioning is True
    assert failure.reason is FailoverReason.unknown
    assert attestation_called == []


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
            "attested_route": ("anthropic", "claude-fable-5"),
            "attested_at": time.monotonic(),
        },
        _claude_max_attestation=SimpleNamespace(included_usage=True),
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


def test_sandbox_extra_read_paths_unset_by_default(monkeypatch, tmp_path):
    profile_home = tmp_path / ".hermes" / "profiles" / "build581-unset"
    profile_home.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.delenv("HERMES_SANDBOX_EXTRA_READ_PATHS", raising=False)

    assert external_runtime._sandbox_extra_read_paths() == ()


def test_sandbox_extra_read_paths_reads_profile_config_and_env(monkeypatch, tmp_path):
    profile_home = tmp_path / ".hermes" / "profiles" / "build581-set"
    profile_home.mkdir(parents=True)
    (profile_home / "config.yaml").write_text(
        "model:\n"
        "  runtime: claude_agent_sdk\n"
        "  sandbox_extra_read_paths:\n"
        "  - /Users/nicholas/.hermes/hermes-agent\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.setenv(
        "HERMES_SANDBOX_EXTRA_READ_PATHS", "/extra/one:/extra/two"
    )

    assert external_runtime._sandbox_extra_read_paths() == (
        "/Users/nicholas/.hermes/hermes-agent",
        "/extra/one",
        "/extra/two",
    )


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


def test_each_new_sdk_route_requires_fresh_billing_attestation(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "BUILD-472")
    calls = []

    class FakeSession:
        def __init__(self, **_kwargs):
            pass

        def run_turn(self, _prompt):
            return ClaudeProjection(final_text="ok")

    def attest(wrapper, **kwargs):
        calls.append((wrapper, kwargs))
        return SimpleNamespace(included_usage=True, marker=len(calls))

    monkeypatch.setattr(external_runtime, "ClaudeAgentSdkSession", FakeSession)
    monkeypatch.setattr(external_runtime, "attest_claude_max_auth", attest)
    key = ("anthropic", "claude-sonnet-4-6")
    agent = SimpleNamespace(
        provider=key[0],
        model=key[1],
        tools=[],
        _cached_system_prompt="",
        max_iterations=2,
        stream_delta_callback=None,
        tool_progress_callback=None,
        _claude_runtime_context={
            "sdk": object(),
            "host_home": tmp_path,
            "workspace": tmp_path,
            "cli_wrapper": tmp_path / "wrapper",
            "kanban_task_id": "BUILD-472",
            "attested_route": key,
            "attested_at": 0.0,
        },
    )

    assert run_claude_agent_sdk_attempt(
        agent, user_message="first", effective_task_id="task"
    ).final_text == "ok"
    assert calls == [(tmp_path / "wrapper", {"cache_ttl_seconds": 0})]

    # The existing session keeps its own already-attested lifecycle.
    assert run_claude_agent_sdk_attempt(
        agent, user_message="second", effective_task_id="task"
    ).final_text == "ok"
    assert len(calls) == 1

    # A new route means a new SDK session and fresh auth/billing evidence.
    agent.model = "claude-opus-4-8"
    assert run_claude_agent_sdk_attempt(
        agent, user_message="third", effective_task_id="task"
    ).final_text == "ok"
    assert len(calls) == 2
    assert calls[-1][1] == {"cache_ttl_seconds": 0}

    agent.model = "claude-sonnet-4-6"
    assert run_claude_agent_sdk_attempt(
        agent, user_message="fourth", effective_task_id="task"
    ).final_text == "ok"
    assert len(calls) == 2
    assert agent._claude_max_attestation.marker == 1


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


def test_claude_sdk_temp_dir_is_exact_owner_only_private_directory(tmp_path):
    temp_root = tmp_path / "tmp"
    temp_root.mkdir()

    temp_dir = prepare_claude_sdk_temp_dir(temp_root=temp_root)

    assert temp_dir == temp_root / f"claude-{os.geteuid()}"
    info = temp_dir.lstat()
    assert stat.S_ISDIR(info.st_mode)
    assert info.st_uid == os.geteuid()
    assert stat.S_IMODE(info.st_mode) == 0o700


def test_claude_sdk_temp_dir_rejects_symlinked_owner_path(tmp_path):
    temp_root = tmp_path / "tmp"
    temp_root.mkdir()
    expected = temp_root / f"claude-{os.geteuid()}"
    expected.symlink_to(tmp_path / "outside", target_is_directory=True)

    with pytest.raises(RuntimeError, match="owner-only"):
        prepare_claude_sdk_temp_dir(temp_root=temp_root)


def test_preflight_uses_per_worker_tmpdir_and_narrow_sdk_compatibility_grant(
    monkeypatch, tmp_path
):
    workspace = tmp_path / "workspace"
    host_home = tmp_path / "host"
    temp_root = tmp_path / "tmp"
    workspace.mkdir()
    host_home.mkdir()
    temp_root.mkdir()
    captured = {"envs": [], "profiles": []}

    class FakeSdk:
        __file__ = str(tmp_path / "sdk" / "__init__.py")

    def create_wrapper(real_cli, exact_env, wrapper_dir, *, sandbox_profile):
        captured["envs"].append(dict(exact_env))
        captured["profiles"].append(sandbox_profile)
        return tmp_path / "claude-wrapper"

    monkeypatch.setenv("HERMES_KANBAN_TASK", "BUILD-425")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(workspace))
    monkeypatch.setattr(external_runtime.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(external_runtime, "_CLAUDE_SDK_TEMP_ROOT", temp_root)
    monkeypatch.setattr(external_runtime, "load_claude_agent_sdk", lambda: FakeSdk)
    monkeypatch.setattr(external_runtime, "get_host_user_home", lambda: host_home)
    monkeypatch.setattr(
        external_runtime,
        "build_claude_subscription_env",
        lambda *_args, **_kwargs: {"PATH": "/usr/bin:/bin"},
    )
    monkeypatch.setattr(external_runtime, "create_exact_env_cli_wrapper", create_wrapper)
    monkeypatch.setattr(
        external_runtime,
        "attest_claude_max_auth",
        lambda _wrapper, **_kwargs: SimpleNamespace(included_usage=True),
    )
    agent = SimpleNamespace(provider="anthropic", model="claude-sonnet-4-6")

    assert prepare_claude_agent_sdk_runtime(agent) is None
    second_workspace = tmp_path / "second-workspace"
    second_workspace.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(second_workspace))
    second_agent = SimpleNamespace(provider="anthropic", model="claude-sonnet-4-6")
    assert prepare_claude_agent_sdk_runtime(second_agent) is None

    claude_tmp = temp_root / f"claude-{os.geteuid()}"
    worker_tmp = workspace / ".hermes-claude-runtime" / "tmp"
    second_worker_tmp = second_workspace / ".hermes-claude-runtime" / "tmp"
    assert captured["envs"][0]["TMPDIR"] == str(worker_tmp)
    assert captured["envs"][1]["TMPDIR"] == str(second_worker_tmp)
    assert worker_tmp != second_worker_tmp
    assert worker_tmp != claude_tmp
    for path in (worker_tmp, second_worker_tmp, claude_tmp):
        info = path.lstat()
        assert stat.S_ISDIR(info.st_mode)
        assert info.st_uid == os.geteuid()
        assert stat.S_IMODE(info.st_mode) == 0o700
    write_rules = [
        line
        for line in captured["profiles"][0].splitlines()
        if line.startswith("(allow file-write")
    ]
    assert f'(allow file-write* (subpath "{claude_tmp.resolve()}"))' in write_rules
    assert f'(allow file-write* (subpath "{temp_root.resolve()}"))' not in write_rules
    assert not any('subpath "/tmp"' in rule for rule in write_rules)


@pytest.mark.parametrize("mode", [0o750, 0o777])
def test_claude_sdk_temp_dir_rejects_existing_nonprivate_mode(tmp_path, mode):
    temp_root = tmp_path / "tmp"
    temp_root.mkdir()
    temp_dir = temp_root / f"claude-{os.geteuid()}"
    temp_dir.mkdir(mode=0o700)
    temp_dir.chmod(mode)

    with pytest.raises(RuntimeError, match="owner-only"):
        prepare_claude_sdk_temp_dir(temp_root=temp_root)


def test_claude_sdk_temp_dir_rejects_existing_wrong_owner(monkeypatch, tmp_path):
    temp_root = tmp_path / "tmp"
    temp_root.mkdir()
    temp_dir = temp_root / f"claude-{os.geteuid()}"
    temp_dir.mkdir(mode=0o700)
    real_lstat = Path.lstat

    def wrong_owner_lstat(path):
        info = real_lstat(path)
        if path == temp_dir:
            return SimpleNamespace(st_mode=info.st_mode, st_uid=os.geteuid() + 1)
        return info

    monkeypatch.setattr(Path, "lstat", wrong_owner_lstat)

    with pytest.raises(RuntimeError, match="owner-only"):
        prepare_claude_sdk_temp_dir(temp_root=temp_root)
