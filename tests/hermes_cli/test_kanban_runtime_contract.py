"""Run-scoped requested-vs-observed runtime contracts."""

from __future__ import annotations

import pytest
from types import SimpleNamespace


def _claimed_routed_task(monkeypatch):
    from hermes_cli import kanban_db as kb

    conn = kb.connect()
    tid = kb.create_task(
        conn,
        title="contracted route",
        assignee="coder",
        model_override="gpt-5.6-sol",
        model_provider_override="openai-codex",
        model_reasoning_effort="xhigh",
        toolsets=["terminal", "file"],
    )
    task = kb.claim_task(conn, tid)
    assert task is not None and task.current_run_id is not None
    conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(task.current_run_id))
    return tid, task.current_run_id


def test_preflight_rejects_clobbered_cli_route_before_provider_work(
    monkeypatch,
):
    _claimed_routed_task(monkeypatch)
    from hermes_cli.kanban_runtime_contract import (
        RunRouteMismatch,
        preflight_kanban_cli_route,
    )

    with pytest.raises(RunRouteMismatch, match="requested provider"):
        preflight_kanban_cli_route(
            model="claude-opus-4-8",
            provider="anthropic",
            reasoning_config={"effort": "max"},
            toolsets=["FILE", "terminal"],
        )


def test_preflight_accepts_canonical_provider_prefixed_model(
    monkeypatch,
):
    _claimed_routed_task(monkeypatch)
    from hermes_cli.kanban_runtime_contract import preflight_kanban_cli_route

    spec = preflight_kanban_cli_route(
        model="openai-codex/gpt-5.6-sol",
        provider="OPENAI-CODEX",
        reasoning_config={"effort": "xhigh"},
        toolsets=["terminal", "file"],
    )

    assert spec["version"] == 2


def test_preflight_rejects_clobbered_task_toolsets(monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_runtime_contract import (
        RunRouteMismatch,
        preflight_kanban_cli_route,
    )

    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="scoped tools",
            assignee="verifier",
            toolsets=["terminal", "file"],
        )
        task = kb.claim_task(conn, task_id)
        assert task is not None
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(task.current_run_id))

    with pytest.raises(RunRouteMismatch, match="requested toolsets"):
        preflight_kanban_cli_route(
            model="anything",
            provider="anything",
            reasoning_config=None,
            toolsets=["browser", "web"],
        )


def test_preflight_compares_toolsets_as_a_normalized_set(monkeypatch):
    _claimed_routed_task(monkeypatch)
    from hermes_cli.kanban_runtime_contract import preflight_kanban_cli_route

    spec = preflight_kanban_cli_route(
        model="gpt-5.6-sol",
        provider="openai-codex",
        reasoning_config={"effort": "xhigh"},
        toolsets=["TERMINAL", "file", "terminal"],
    )

    assert spec["toolsets"] == ["file", "terminal"]


def test_preflight_fails_closed_when_kanban_identity_has_no_current_run(
    monkeypatch,
):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_runtime_contract import (
        RunRouteMismatch,
        preflight_kanban_cli_route,
    )

    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="stale worker identity",
            assignee="verifier",
            toolsets=["terminal", "file"],
        )
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "999999")

    with pytest.raises(RunRouteMismatch, match="no active run contract"):
        preflight_kanban_cli_route(
            model="gpt-5.6-sol",
            provider="openai-codex",
            reasoning_config={"effort": "xhigh"},
            toolsets=["terminal", "file"],
        )


def test_preflight_allows_explicit_non_kanban_manual_process(monkeypatch):
    from hermes_cli.kanban_runtime_contract import preflight_kanban_cli_route

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)

    assert (
        preflight_kanban_cli_route(
            model="manual-model",
            provider="manual-provider",
            reasoning_config=None,
            toolsets=["terminal"],
        )
        is None
    )


def test_preflight_rejects_partial_kanban_identity_as_manual(monkeypatch):
    from hermes_cli.kanban_runtime_contract import (
        RunRouteMismatch,
        preflight_kanban_cli_route,
    )

    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-1")
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_CLAIM_LOCK", raising=False)

    with pytest.raises(RunRouteMismatch, match="process declares Kanban identity"):
        preflight_kanban_cli_route(
            model="manual-model",
            provider="manual-provider",
            reasoning_config=None,
            toolsets=["terminal"],
        )


def test_claim_rejects_corrupt_empty_task_toolsets():
    from hermes_cli import kanban_db as kb

    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="corrupt empty tools",
            assignee="verifier",
        )
        conn.execute("UPDATE tasks SET toolsets = '[]' WHERE id = ?", (task_id,))

        with pytest.raises(ValueError, match="unsupported values"):
            kb.claim_task(conn, task_id)

        assert kb.get_task(conn, task_id).status == "ready"


def test_cli_preflight_runs_before_deferred_startup_or_agent_construction(monkeypatch):
    import cli
    from hermes_cli.cli_agent_setup_mixin import CLIAgentSetupMixin
    from hermes_cli import kanban_runtime_contract as contract

    touched: list[str] = []

    def reject(**_kwargs):
        touched.append("preflight")
        raise contract.RunRouteMismatch("clobbered")

    monkeypatch.setattr(contract, "preflight_kanban_cli_route", reject)
    monkeypatch.setattr(
        cli,
        "_prepare_deferred_agent_startup",
        lambda: touched.append("deferred-startup"),
    )
    dummy = SimpleNamespace(
        agent=None,
        model="claude-opus-4-8",
        requested_provider="anthropic",
        reasoning_config={"effort": "max"},
    )

    assert CLIAgentSetupMixin._init_agent(dummy) is False
    assert touched == ["preflight"]


def test_runtime_observation_is_append_only_and_stale_run_guarded(monkeypatch):
    from hermes_cli import kanban_db as kb

    tid, run_id = _claimed_routed_task(monkeypatch)
    observed = {
        "version": 1,
        "phase": "initial",
        "provider": "openai-codex",
        "model": "gpt-5.6-sol",
        "reasoning_effort": "xhigh",
        "runtime": "hermes",
        "api_mode": "codex_responses",
    }
    with kb.connect() as conn:
        before = kb.latest_run(conn, tid)
        monkeypatch.setattr(kb.time, "time", lambda: before.started_at + 60)
        assert kb.record_runtime_observation(
            conn, tid, run_id, observed,
        ) is True
        assert kb.latest_runtime_observation(conn, run_id) == observed
        after = kb.latest_run(conn, tid)
        assert after.last_semantic_progress_at == before.last_semantic_progress_at
        assert after.last_durable_progress_at == before.last_durable_progress_at
        conn.execute(
            "UPDATE tasks SET current_run_id = NULL WHERE id = ?", (tid,),
        )
        assert kb.record_runtime_observation(
            conn, tid, run_id, {**observed, "phase": "fallback"},
        ) is False
        assert kb.latest_runtime_observation(conn, run_id) == observed


def test_attach_observer_attests_actual_agent_route(monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_runtime_contract import attach_kanban_runtime_observer

    _tid, run_id = _claimed_routed_task(monkeypatch)
    agent = SimpleNamespace(
        provider="openai-codex",
        model="gpt-5.6-sol",
        reasoning_config={"effort": "xhigh"},
        runtime="hermes",
        api_mode="codex_responses",
    )

    assert attach_kanban_runtime_observer(agent) is True

    with kb.connect() as conn:
        assert kb.latest_runtime_observation(conn, run_id) == {
            "version": 1,
            "phase": "initial",
            "provider": "openai-codex",
            "model": "gpt-5.6-sol",
            "reasoning_effort": "xhigh",
            "runtime": "hermes",
            "api_mode": "codex_responses",
        }


def test_terminal_metadata_uses_latest_observation_not_env_or_caller(monkeypatch):
    import json
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools

    tid, run_id = _claimed_routed_task(monkeypatch)
    actual = {
        "version": 1,
        "phase": "fallback",
        "provider": "anthropic",
        "model": "claude-opus-4-8",
        "reasoning_effort": "max",
        "runtime": "claude_agent_sdk",
        "api_mode": "anthropic_messages",
    }
    with kb.connect() as conn:
        assert kb.record_runtime_observation(conn, tid, run_id, actual)

    monkeypatch.setenv("HERMES_PROVIDER", "lying-provider")
    monkeypatch.setenv("HERMES_MODEL", "lying-model")
    result = json.loads(kanban_tools._handle_complete({
        "summary": "done",
        "metadata": {
            "model_used": {"provider": "caller", "model": "caller"},
        },
    }))

    assert result["ok"] is True
    with kb.connect() as conn:
        assert kb.latest_run(conn, tid).metadata["model_used"] == {
            "provider": "anthropic",
            "model": "claude-opus-4-8",
            "reasoning_effort": "max",
        }


def test_contracted_run_cannot_finish_without_runtime_attestation(monkeypatch):
    import json
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools

    tid, _run_id = _claimed_routed_task(monkeypatch)
    result = json.loads(kanban_tools._handle_complete({"summary": "done"}))

    assert "no runtime observation" in result["error"]
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).status == "running"
