"""Tests for the Kanban tool surface (tools/kanban_tools.py).

Verifies:
  - Tools are gated on HERMES_KANBAN_TASK: a normal chat session sees
    zero kanban tools in its schema; a worker session sees the kanban set.
  - Each handler's happy path.
  - Error paths (missing required args, bad metadata type, etc).
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


_REAL_HERMES_HOME = (Path.home() / ".hermes").resolve()


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------

def test_kanban_tools_hidden_without_env_var(monkeypatch, tmp_path):
    """Normal `hermes chat` sessions (no HERMES_KANBAN_TASK) must have
    zero kanban_* tools in their schema."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    import tools.kanban_tools  # ensure registered
    from tools.registry import invalidate_check_fn_cache, registry
    from toolsets import resolve_toolset

    invalidate_check_fn_cache()
    schema = registry.get_definitions(set(resolve_toolset("hermes-cli")), quiet=True)
    names = {s["function"].get("name") for s in schema if "function" in s}
    kanban = {n for n in names if n and n.startswith("kanban_")}
    assert kanban == set(), (
        f"kanban tools leaked into normal chat schema: {kanban}"
    )


def test_kanban_tools_visible_with_env_var(monkeypatch, tmp_path):
    """Worker sessions get task lifecycle tools, not board-routing tools."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_fake")
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    import tools.kanban_tools  # ensure registered
    from tools.registry import invalidate_check_fn_cache, registry
    from toolsets import resolve_toolset

    invalidate_check_fn_cache()
    schema = registry.get_definitions(set(resolve_toolset("hermes-cli")), quiet=True)
    names = {s["function"].get("name") for s in schema if "function" in s}
    kanban = {n for n in names if n and n.startswith("kanban_")}
    expected = {
        "kanban_show", "kanban_complete", "kanban_block", "kanban_request_rework",
        "kanban_request_publication",
        "kanban_heartbeat",
        "kanban_comment", "kanban_create", "kanban_link",
        "kanban_attach", "kanban_attach_url", "kanban_attachments",
    }
    assert kanban == expected, f"expected {expected}, got {kanban}"


def test_kanban_worker_env_overrides_profile_toolset_filter(monkeypatch, tmp_path):
    """Dispatcher-spawned workers must get lifecycle tools even when the
    assignee profile restricts enabled toolsets and does not list kanban.
    """
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_fake")
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    import tools.kanban_tools  # ensure registered
    from model_tools import _clear_tool_defs_cache, get_tool_definitions
    from tools.registry import invalidate_check_fn_cache

    invalidate_check_fn_cache()
    _clear_tool_defs_cache()
    schema = get_tool_definitions(
        enabled_toolsets=["terminal"],
        quiet_mode=True,
    )
    names = {s["function"].get("name") for s in schema if "function" in s}
    assert "kanban_show" in names
    assert "kanban_complete" in names
    assert "kanban_block" in names
    assert "kanban_list" not in names


def test_worker_with_kanban_toolset_still_hides_board_routing(monkeypatch, tmp_path):
    """Task scope wins over profile config for board-routing tools.

    Even if a worker process happens to also have ``toolsets: [kanban]``
    in its config, the HERMES_KANBAN_TASK env var means it's a focused
    worker and must not see kanban_list / kanban_unblock.
    """
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_fake")
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text("toolsets:\n  - kanban\n")
    monkeypatch.setenv("HERMES_HOME", str(home))

    import tools.kanban_tools  # ensure registered
    from tools.registry import invalidate_check_fn_cache, registry
    from toolsets import resolve_toolset

    invalidate_check_fn_cache()
    schema = registry.get_definitions(set(resolve_toolset("hermes-cli")), quiet=True)
    names = {s["function"].get("name") for s in schema if "function" in s}
    kanban = {n for n in names if n and n.startswith("kanban_")}
    assert {
        "kanban_list",
        "kanban_unblock",
    }.isdisjoint(kanban), (
        f"Board-routing tools leaked into worker schema: "
        f"{kanban & {'kanban_list', 'kanban_unblock'}}"
    )


def test_kanban_tools_visible_with_toolset_config(monkeypatch, tmp_path):
    """Orchestrator profiles with toolsets: [kanban] see all kanban tools."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text("toolsets:\n  - kanban\n")
    monkeypatch.setenv("HERMES_HOME", str(home))

    import tools.kanban_tools  # ensure registered
    from tools.registry import invalidate_check_fn_cache, registry
    from toolsets import resolve_toolset

    invalidate_check_fn_cache()
    schema = registry.get_definitions(set(resolve_toolset("hermes-cli")), quiet=True)
    names = {s["function"].get("name") for s in schema if "function" in s}
    kanban = {n for n in names if n and n.startswith("kanban_")}
    expected = {
        "kanban_list",
        "kanban_show", "kanban_complete", "kanban_block", "kanban_heartbeat",
        "kanban_request_rework",
        "kanban_request_publication",
        "kanban_comment", "kanban_create", "kanban_compile_workflow", "kanban_link",
        "kanban_unblock",
        "kanban_attach", "kanban_attach_url", "kanban_attachments",
    }
    assert kanban == expected, f"expected {expected}, got {kanban}"


def _compiled_workflow_steps():
    return [
        {
            "key": "implement",
            "title": "Implement the change",
            "assignee": "coder",
            "model_routing": "code_implementation",
        },
        {
            "key": "verify",
            "title": "Verify the result",
            "assignee": "verifier",
            "parents": ["implement"],
            "role": "reporter",
            "terminal": True,
            "model_routing": "verification_leaf",
        },
    ]


def _install_compiler_test_profiles():
    profiles = Path(os.environ["HERMES_HOME"]) / "profiles"
    for name in ("coder", "verifier"):
        profile = profiles / name
        profile.mkdir(parents=True, exist_ok=True)
        (profile / "config.yaml").write_text("toolsets: []\n")
    policy = Path(os.environ["HERMES_HOME"]) / "rules" / "architecture-gate-policy.json"
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_text(json.dumps({"schema_version": 1, "mode": "off"}))


def test_compile_workflow_is_orchestrator_only_and_hidden_from_workers(
    monkeypatch, worker_env,
):
    from tools import kanban_tools as kt

    refused = json.loads(kt._handle_compile_workflow({
        "workflow_key": "worker-bypass",
        "idempotency_key": "worker-bypass-v1",
        "steps": _compiled_workflow_steps(),
    }))
    assert "orchestrator-only" in refused["error"]


def test_compile_workflow_atomically_subscribes_gateway_target_on_every_step(
    monkeypatch, worker_env,
):
    from gateway.session_context import reset_session_vars
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setenv("HERMES_PROFILE", "orchestrator")
    monkeypatch.setenv("HERMES_SESSION_ID", "session-compile")
    reset_session_vars()
    _install_compiler_test_profiles()
    args = {
        "workflow_key": "BUILD-472:tool-contract",
        "idempotency_key": "BUILD-472:tool-contract:v1",
        "workspace_kind": "dir",
        "workspace_path": "/tmp/project",
        "steps": _compiled_workflow_steps(),
    }
    gateway_source = {
        "platform": "telegram",
        "chat_id": "chat-472",
        "thread_id": "thread-1",
        "user_id": "nicholas",
    }

    first = json.loads(kt._handle_compile_workflow(
        args, gateway_source=gateway_source,
    ))
    second = json.loads(kt._handle_compile_workflow(
        args, gateway_source=gateway_source,
    ))

    assert first == second
    assert first["ok"] is True
    assert first["workflow_key"] == args["workflow_key"]
    assert first["terminal_task_id"] == first["task_ids"]["verify"]
    # BUILD-503: terminal gets an all-kinds sub, every step a failure-kinds
    # sub (BUILD-508), all bound to the originating gateway target.
    assert first["notification"] == {
        "subscribed": True,
        "count": 2,
        "terminal_count": 1,
        "terminal_only": False,
    }
    with kb.connect() as conn:
        implementation = kb.get_task(conn, first["task_ids"]["implement"])
        verification = kb.get_task(conn, first["task_ids"]["verify"])
        assert implementation.model_provider_override == "deepseek"
        assert implementation.model_override == "deepseek-v4-pro"
        assert implementation.model_reasoning_effort == "xhigh"
        assert verification.model_provider_override == "nous"
        assert verification.model_override == "deepseek/deepseek-v4-flash:free"
        assert verification.model_reasoning_effort == "low"
        subscriptions = kb.list_notify_subs(conn)
        assert len(subscriptions) == 2
        by_task = {row["task_id"]: row for row in subscriptions}
        assert set(by_task) == set(first["task_ids"].values())
        assert all(row["platform"] == "telegram" for row in subscriptions)
        assert all(row["chat_id"] == "chat-472" for row in subscriptions)
        assert by_task[first["terminal_task_id"]]["kinds_json"] is None
        step_kinds = json.loads(by_task[first["task_ids"]["implement"]]["kinds_json"])
        assert step_kinds == sorted(kb.FAILURE_KINDS)


def test_compile_workflow_invalid_graph_rolls_back_without_partial_cards(
    monkeypatch, worker_env,
):
    from gateway.session_context import reset_session_vars
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setenv("HERMES_PROFILE", "orchestrator")
    reset_session_vars()
    _install_compiler_test_profiles()
    bad_steps = _compiled_workflow_steps()
    bad_steps[1]["parents"] = ["missing"]

    result = json.loads(kt._handle_compile_workflow({
        "workflow_key": "invalid-tool-graph",
        "idempotency_key": "invalid-tool-graph-v1",
        "steps": bad_steps,
    }))

    assert "unknown parent" in result["error"]
    with kb.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE workflow_key = ?",
            ("invalid-tool-graph",),
        ).fetchone()[0] == 0


def test_compile_workflow_rejects_unknown_assignee_before_creating_cards(
    monkeypatch, worker_env,
):
    from gateway.session_context import reset_session_vars
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setenv("HERMES_PROFILE", "orchestrator")
    reset_session_vars()
    _install_compiler_test_profiles()
    steps = _compiled_workflow_steps()
    steps[0]["assignee"] = "profile-that-does-not-exist"

    result = json.loads(kt._handle_compile_workflow({
        "workflow_key": "unknown-assignee",
        "idempotency_key": "unknown-assignee-v1",
        "steps": steps,
    }))

    assert "unknown assignee profile" in result["error"]
    with kb.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE workflow_key = 'unknown-assignee'"
        ).fetchone()[0] == 0


def test_compile_workflow_refuses_to_bypass_staged_architecture_gate(
    monkeypatch, worker_env,
):
    from gateway.session_context import reset_session_vars
    from tools import kanban_tools as kt

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setenv("HERMES_PROFILE", "orchestrator")
    reset_session_vars()
    policy_path = Path(os.environ["HERMES_HOME"]) / "rules" / "architecture-gate-policy.json"
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy_path.write_text(json.dumps({"schema_version": 1, "mode": "shadow"}))

    assert kt._managed_architecture_gate_mode() == "shadow"
    result = json.loads(kt._handle_compile_workflow({
        "workflow_key": "must-use-gated-issuance",
        "idempotency_key": "must-use-gated-issuance-v1",
        "steps": _compiled_workflow_steps(),
    }))
    assert "architecture graph issuance" in result["error"]


def test_compile_workflow_retry_ignores_new_session_route_and_delivery_context(
    monkeypatch, worker_env,
):
    from gateway.session_context import reset_session_vars
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setenv("HERMES_PROFILE", "orchestrator")
    monkeypatch.setenv("HERMES_SESSION_ID", "first-session")
    reset_session_vars()
    _install_compiler_test_profiles()
    args = {
        "workflow_key": "stable-request-retry",
        "idempotency_key": "stable-request-retry-v1",
        "steps": _compiled_workflow_steps(),
    }

    first = json.loads(kt._handle_compile_workflow(
        args,
        gateway_source={"platform": "telegram", "chat_id": "first-chat"},
    ))
    monkeypatch.setenv("HERMES_SESSION_ID", "continued-session")
    reset_session_vars()
    policy = Path(os.environ["HERMES_HOME"]) / "rules" / "architecture-gate-policy.json"
    policy.write_text("invalid-after-first-compile")
    monkeypatch.setattr(kt, "_resolve_model_routing", lambda *_: (
        "different-model",
        {"provider": "different-provider", "reasoning_effort": "medium"},
    ))
    retry = json.loads(kt._handle_compile_workflow(
        args,
        gateway_source={"platform": "discord", "chat_id": "second-chat"},
    ))

    assert retry["ok"] is True
    assert retry["task_ids"] == first["task_ids"]
    with kb.connect() as conn:
        subscriptions = kb.list_notify_subs(conn)
        # terminal + step sub from the FIRST compile only (BUILD-503 fan-out);
        # the retry's new session/delivery context adds nothing.
        assert len(subscriptions) == 2
        assert all(row["platform"] == "telegram" for row in subscriptions)
        assert all(row["chat_id"] == "first-chat" for row in subscriptions)


def test_compile_workflow_atomically_subscribes_configured_home_channels(
    monkeypatch, worker_env,
):
    from gateway.session_context import reset_session_vars
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    monkeypatch.delenv("HERMES_SESSION_CHAT_ID", raising=False)
    monkeypatch.setenv("HERMES_PROFILE", "orchestrator")
    reset_session_vars()
    _install_compiler_test_profiles()
    (Path(os.environ["HERMES_HOME"]) / "config.yaml").write_text(
        "toolsets:\n  - kanban\nkanban:\n"
        "  auto_subscribe_on_create: true\n"
        "  auto_subscribe_home_on_create: true\n"
    )
    monkeypatch.setattr(kt, "_configured_home_channels", lambda: [
        {"platform": "telegram", "chat_id": "home-a", "thread_id": "1"},
        {"platform": "discord", "chat_id": "home-b", "thread_id": ""},
    ])

    result = json.loads(kt._handle_compile_workflow({
        "workflow_key": "home-terminal-delivery",
        "idempotency_key": "home-terminal-delivery-v1",
        "steps": _compiled_workflow_steps(),
    }))

    assert result["ok"] is True
    # 2 home channels × (terminal all-kinds + 1 step failure-kinds) = 4 subs
    assert result["notification"] == {
        "subscribed": True,
        "count": 4,
        "terminal_count": 2,
        "terminal_only": False,
    }
    with kb.connect() as conn:
        subscriptions = kb.list_notify_subs(conn)
        assert len(subscriptions) == 4
        assert {row["task_id"] for row in subscriptions} == set(
            result["task_ids"].values()
        )
        assert {row["platform"] for row in subscriptions} == {
            "telegram", "discord"
        }


def test_compile_workflow_denies_new_work_when_managed_policy_is_invalid(
    monkeypatch, worker_env,
):
    from gateway.session_context import reset_session_vars
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setenv("HERMES_PROFILE", "orchestrator")
    reset_session_vars()
    _install_compiler_test_profiles()
    policy = Path(os.environ["HERMES_HOME"]) / "rules" / "architecture-gate-policy.json"
    policy.write_text("not-json")

    result = json.loads(kt._handle_compile_workflow({
        "workflow_key": "invalid-policy-must-deny",
        "idempotency_key": "invalid-policy-must-deny-v1",
        "steps": _compiled_workflow_steps(),
    }))

    assert "managed architecture-gate policy" in result["error"]
    with kb.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE workflow_key = 'invalid-policy-must-deny'"
        ).fetchone()[0] == 0


# ---------------------------------------------------------------------------
# Handler happy paths
# ---------------------------------------------------------------------------

@pytest.fixture
def worker_env(monkeypatch, tmp_path):
    """Simulate being a worker: HERMES_HOME isolated, HERMES_KANBAN_TASK set
    after we've created the task."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="worker-test", assignee="test-worker")
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None and claimed.current_run_id is not None
        kb.record_runtime_observation(
            conn,
            tid,
            claimed.current_run_id,
            {
                "version": 1,
                "phase": "initial",
                "provider": "",
                "model": "",
                "reasoning_effort": "",
                "runtime": "hermes",
                "api_mode": "chat_completions",
            },
        )
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))
    return tid


def test_show_defaults_to_env_task_id(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_show({})
    d = json.loads(out)
    assert "task" in d
    assert d["task"]["id"] == worker_env
    assert d["task"]["status"] == "running"
    assert "worker_context" in d
    assert "runs" not in d
    assert "comments" not in d
    assert "events" not in d
    assert "body" not in d["task"]


def test_show_detail_full_retains_diagnostic_history(worker_env):
    from tools import kanban_tools as kt

    d = json.loads(kt._handle_show({"detail": "full"}))

    assert d["task"]["id"] == worker_env
    assert "body" in d["task"]
    assert "runs" in d
    assert "comments" in d
    assert "events" in d


def test_orchestrator_show_defaults_to_compact_status(monkeypatch, worker_env):
    from tools import kanban_tools as kt

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    d = json.loads(kt._handle_show({"task_id": worker_env}))

    assert d["task"] == {
        "id": worker_env,
        "title": "worker-test",
        "assignee": "test-worker",
        "status": "running",
        "current_run_id": d["task"]["current_run_id"],
    }
    assert d["dependencies"] == {
        "parent_count": 0,
        "child_count": 0,
        "unfinished_parent_count": 0,
    }
    assert d["latest_run"]["status"] == "running"
    assert "latest_event" in d
    assert "worker_context" not in d
    assert "runs" not in d
    assert "comments" not in d
    assert "events" not in d


def test_orchestrator_compact_show_bounds_latest_run_and_event(monkeypatch, worker_env):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    with kb.connect() as conn:
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_runs SET summary = ?, error = ?, metadata = ? "
                "WHERE task_id = ?",
                ("s" * 100_000, "e" * 100_000, json.dumps({"blob": "m" * 100_000}), worker_env),
            )
            kb._append_event(conn, worker_env, "diagnostic", {"blob": "p" * 100_000})

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    raw = kt._handle_show({"task_id": worker_env})
    d = json.loads(raw)

    assert len(raw.encode("utf-8")) < 8 * 1024
    assert "metadata" not in d["latest_run"]
    assert d["latest_event"]["payload"]["truncated"] is True


def test_show_explicit_task_id(worker_env):
    """Peek at a different task than the one in env."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        other = kb.create_task(conn, title="other task", assignee="peer")
    finally:
        conn.close()
    from tools import kanban_tools as kt
    out = kt._handle_show({"task_id": other})
    d = json.loads(out)
    assert d["task"]["id"] == other


def test_list_filters_tasks(monkeypatch, worker_env):
    """kanban_list gives orchestrators filtered board discovery."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        a = kb.create_task(conn, title="alpha", assignee="factory", priority=5)
        b = kb.create_task(conn, title="beta", assignee="reviewer")
        c = kb.create_task(conn, title="gamma", assignee="factory", tenant="other")
    finally:
        conn.close()

    from tools import kanban_tools as kt
    out = kt._handle_list({"assignee": "factory", "status": "ready", "limit": 10})
    d = json.loads(out)
    ids = [t["id"] for t in d["tasks"]]
    assert ids == [a, c]
    assert d["count"] == 2
    assert d["tasks"][0]["title"] == "alpha"
    assert b not in ids

    tenant_out = kt._handle_list({
        "assignee": "factory",
        "status": "ready",
        "tenant": "other",
    })
    tenant_ids = [t["id"] for t in json.loads(tenant_out)["tasks"]]
    assert tenant_ids == [c]


def test_list_filters_one_workflow_without_dumping_the_board(monkeypatch, worker_env):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    with kb.connect() as conn:
        wanted = kb.create_task(
            conn, title="wanted", assignee="factory", workflow_key="wf-one",
        )
        kb.create_task(
            conn, title="unrelated", assignee="factory", workflow_key="wf-two",
        )

    d = json.loads(kt._handle_list({"workflow_key": "wf-one"}))

    assert [task["id"] for task in d["tasks"]] == [wanted]
    assert d["tasks"][0]["parent_count"] == 0


def test_list_rejects_invalid_status(monkeypatch, worker_env):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from tools import kanban_tools as kt
    out = kt._handle_list({"status": "not-a-state"})
    assert "status must be one of" in json.loads(out).get("error", "")


def test_list_rejects_bad_limit(monkeypatch, worker_env):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from tools import kanban_tools as kt
    assert json.loads(kt._handle_list({"limit": "nope"})).get("error")
    assert json.loads(kt._handle_list({"limit": 0})).get("error")


def test_list_parses_include_archived_string_false(monkeypatch, worker_env):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        live = kb.create_task(conn, title="live task", assignee="factory")
        archived = kb.create_task(conn, title="archived task", assignee="factory")
        assert kb.archive_task(conn, archived)
    finally:
        conn.close()

    from tools import kanban_tools as kt
    out = kt._handle_list({
        "assignee": "factory",
        "include_archived": "false",
    })
    ids = [t["id"] for t in json.loads(out)["tasks"]]
    assert live in ids
    assert archived not in ids


def test_list_parses_include_archived_string_true(monkeypatch, worker_env):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        live = kb.create_task(conn, title="live task", assignee="factory")
        archived = kb.create_task(conn, title="archived task", assignee="factory")
        assert kb.archive_task(conn, archived)
    finally:
        conn.close()

    from tools import kanban_tools as kt
    out = kt._handle_list({
        "assignee": "factory",
        "include_archived": "true",
    })
    ids = [t["id"] for t in json.loads(out)["tasks"]]
    assert live in ids
    assert archived in ids


def test_list_rejects_bad_include_archived(monkeypatch, worker_env):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from tools import kanban_tools as kt
    out = kt._handle_list({"include_archived": "sometimes"})
    assert "include_archived must be" in json.loads(out).get("error", "")


def test_complete_happy_path(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_complete({
        "summary": "got the thing done",
        "metadata": {"files": 2},
    })
    d = json.loads(out)
    assert d["ok"] is True
    assert d["task_id"] == worker_env
    # Verify via kernel
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        run = kb.latest_run(conn, worker_env)
        assert run.outcome == "completed"
        assert run.summary == "got the thing done"
        assert run.metadata == {"files": 2}
    finally:
        conn.close()


def test_worker_complete_returns_typed_terminal_control(worker_env):
    from tools import kanban_tools as kt

    out = kt._handle_complete({"summary": "finished the worker task"})

    assert isinstance(out, kt.KanbanTerminalControl)
    assert out.action is kt.KanbanTerminalAction.COMPLETE
    assert json.loads(out)["ok"] is True


def test_complete_metadata_round_trips_through_show(worker_env):
    """Structured completion metadata should be visible to downstream agents."""
    from tools import kanban_tools as kt

    handoff = {
        "changed_files": ["hermes_cli/kanban.py"],
        "verification": ["pytest tests/tools/test_kanban_tools.py -q"],
        "dependencies": [],
        "blocked_reason": None,
        "retry_notes": "none",
        "residual_risk": ["dashboard rendering not exercised"],
    }

    complete_out = kt._handle_complete({
        "summary": "finished with structured evidence",
        "metadata": handoff,
    })
    assert json.loads(complete_out)["ok"] is True

    show_out = kt._handle_show({"task_id": worker_env, "detail": "full"})
    shown = json.loads(show_out)
    assert shown["task"]["status"] == "done"
    assert shown["runs"][-1]["summary"] == "finished with structured evidence"
    assert shown["runs"][-1]["metadata"] == handoff


def test_complete_stamps_worker_session_id_from_env(monkeypatch, worker_env):
    from tools import kanban_tools as kt

    monkeypatch.setenv("HERMES_SESSION_ID", "session-trusted")
    metadata = {"files": 2, "worker_session_id": "user-spoof"}

    out = kt._handle_complete({
        "summary": "done by scoped worker",
        "metadata": metadata,
    })
    assert json.loads(out)["ok"] is True
    assert metadata["worker_session_id"] == "user-spoof"

    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        run = kb.latest_run(conn, worker_env)
        assert run.metadata == {
            "files": 2,
            "worker_session_id": "session-trusted",
        }
    finally:
        conn.close()


def test_complete_does_not_stamp_worker_session_id_without_scoped_task(
    monkeypatch, worker_env
):
    from tools import kanban_tools as kt

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setenv("HERMES_SESSION_ID", "session-trusted")

    out = kt._handle_complete({
        "task_id": worker_env,
        "summary": "done outside worker scope",
        "metadata": {"files": 2, "worker_session_id": "user-provided"},
    })
    assert json.loads(out)["ok"] is True

    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        run = kb.latest_run(conn, worker_env)
        assert run.metadata == {
            "files": 2,
            "worker_session_id": "user-provided",
        }
    finally:
        conn.close()


def test_complete_with_result_only(worker_env):
    """`result` alone (without summary) is accepted for legacy compat."""
    from tools import kanban_tools as kt
    out = kt._handle_complete({"result": "legacy result"})
    d = json.loads(out)
    assert d["ok"] is True


def test_complete_review_outputs_schema_and_validation(worker_env):
    from tools import kanban_tools as kt

    properties = kt.KANBAN_COMPLETE_SCHEMA["parameters"]["properties"]
    assert properties["review_outputs"]["type"] == "array"
    assert properties["review_outputs"]["items"]["required"] == [
        "review_task_id",
        "attachment_id",
    ]

    rejected = json.loads(
        kt._handle_complete(
            {"summary": "bad selection shape", "review_outputs": {"x": 1}}
        )
    )
    assert "review_outputs must be a list" in rejected["error"]


def test_complete_with_artifacts_lands_in_event_payload(worker_env):
    """``artifacts=[...]`` rides into the completed event payload so the
    gateway notifier can upload them as native attachments. See the
    kanban notifier in gateway/run.py for the consumer side."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    out = kt._handle_complete({
        "summary": "rendered the chart",
        "artifacts": ["/tmp/q3-revenue.png", "/tmp/q3-report.pdf"],
    })
    assert json.loads(out)["ok"] is True

    conn = kb.connect()
    try:
        events = kb.list_events(conn, worker_env)
        # Find the completion event
        completed = [e for e in events if e.kind == "completed"]
        assert len(completed) == 1
        payload = completed[0].payload or {}
        assert payload.get("artifacts") == [
            "/tmp/q3-revenue.png",
            "/tmp/q3-report.pdf",
        ]
        # And the artifacts also live on metadata for downstream workers
        run = kb.latest_run(conn, worker_env)
        assert run.metadata.get("artifacts") == [
            "/tmp/q3-revenue.png",
            "/tmp/q3-report.pdf",
        ]
    finally:
        conn.close()


def test_complete_artifacts_accepts_single_string(worker_env):
    """A bare string is auto-promoted to a single-element list for convenience."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    out = kt._handle_complete({
        "summary": "one chart",
        "artifacts": "/tmp/chart.png",
    })
    assert json.loads(out)["ok"] is True

    conn = kb.connect()
    try:
        run = kb.latest_run(conn, worker_env)
        assert run.metadata.get("artifacts") == ["/tmp/chart.png"]
    finally:
        conn.close()


def test_complete_artifacts_merges_with_explicit_metadata_field(worker_env):
    """If the worker passes metadata.artifacts AND the top-level artifacts
    param, merge the two without duplicates."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    out = kt._handle_complete({
        "summary": "merged",
        "metadata": {"artifacts": ["/tmp/a.png"], "other": "fact"},
        "artifacts": ["/tmp/b.pdf", "/tmp/a.png"],
    })
    assert json.loads(out)["ok"] is True

    conn = kb.connect()
    try:
        run = kb.latest_run(conn, worker_env)
        # Order: existing entries first, then new ones, deduplicated.
        assert run.metadata.get("artifacts") == ["/tmp/a.png", "/tmp/b.pdf"]
        assert run.metadata.get("other") == "fact"
    finally:
        conn.close()


def test_complete_rejects_non_list_artifacts(worker_env):
    """Non-list, non-string artifacts should be rejected with a clear error."""
    from tools import kanban_tools as kt
    out = kt._handle_complete({
        "summary": "bad shape",
        "artifacts": {"not": "a list"},
    })
    err = json.loads(out).get("error", "")
    assert "artifacts must be a list" in err


def test_complete_missing_scratch_artifact_stays_in_flight(worker_env):
    """A false deliverable claim must return retry guidance, not mark Done."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    with kb.connect() as conn:
        task = kb.get_task(conn, worker_env)
        assert task is not None
        workspace = kb.resolve_workspace(task)
        kb.set_workspace_path(conn, worker_env, workspace)

    output = kt._handle_complete({
        "summary": "report complete",
        "artifacts": [str(workspace / "missing-report.md")],
    })
    error = json.loads(output).get("error", "")

    assert "could not preserve" in error
    assert "still in-flight" in error
    assert "retry kanban_complete" in error
    with kb.connect() as conn:
        assert kb.get_task(conn, worker_env).status == "running"
    assert workspace.exists()


def test_complete_rejects_no_handoff(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_complete({})
    assert json.loads(out).get("error"), "should have errored"
    assert not isinstance(out, kt.KanbanTerminalControl)


def test_complete_rejects_non_dict_metadata(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_complete({"summary": "x", "metadata": [1, 2, 3]})
    assert json.loads(out).get("error")


def test_complete_phantom_card_message_advertises_retry(worker_env):
    """A phantom-card rejection must surface a tool_error that explicitly
    tells the worker the task is still in-flight and how to retry — the
    worker has no other channel to discover that. Regression for #22923,
    where the previous wording read like a terminal failure and workers
    routinely abandoned the run instead of trying again.
    """
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    out = kt._handle_complete({
        "summary": "oops claimed a phantom",
        "created_cards": ["t_phantomdeadbeef"],
    })
    err = json.loads(out).get("error", "")
    assert err, f"expected an error, got {out!r}"
    # Phantom id surfaced verbatim.
    assert "t_phantomdeadbeef" in err
    # The retry-is-supported phrasing — these are the literal cues a
    # worker reads to decide whether to retry vs block/abandon. If a
    # future change rewords the message, these checks will catch the
    # regression. See #22923 for the failure mode.
    assert "still in-flight" in err
    assert "Retry kanban_complete" in err
    assert "created_cards=[]" in err

    # Critically: the task is genuinely still in-flight — the gate
    # rejection did not mutate state, so the worker's retry can land.
    conn = kb.connect()
    try:
        assert kb.get_task(conn, worker_env).status == "running"
    finally:
        conn.close()


def test_complete_retry_with_empty_created_cards_succeeds(worker_env):
    """After a phantom rejection, retrying kanban_complete with
    created_cards=[] (the documented escape hatch) must complete the
    task. Regression for #22923."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    # Hit the gate first.
    rejected = json.loads(kt._handle_complete({
        "summary": "oops",
        "created_cards": ["t_phantomdeadbeef"],
    }))
    assert rejected.get("error")

    # Retry with the escape hatch.
    ok = json.loads(kt._handle_complete({
        "summary": "retry without claims",
        "created_cards": [],
    }))
    assert ok.get("ok") is True

    conn = kb.connect()
    try:
        assert kb.get_task(conn, worker_env).status == "done"
    finally:
        conn.close()


def test_complete_retry_with_corrected_created_cards_succeeds(worker_env):
    """After a phantom rejection, retrying kanban_complete with a
    corrected created_cards list (phantom ids removed) must complete the
    task. Regression for #22923."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    # Create a real child via the tool so it gets the worker-profile
    # attribution the gate trusts.
    child = json.loads(kt._handle_create({
        "title": "real child", "assignee": "peer",
    }))
    assert child["ok"]
    real_id = child["task_id"]

    # First attempt mixes real + phantom — gate rejects.
    rejected = json.loads(kt._handle_complete({
        "summary": "oops",
        "created_cards": [real_id, "t_phantomdeadbeef"],
    }))
    assert rejected.get("error")
    assert "t_phantomdeadbeef" in rejected["error"]

    # Retry with corrected list.
    ok = json.loads(kt._handle_complete({
        "summary": "retry with corrected list",
        "created_cards": [real_id],
    }))
    assert ok.get("ok") is True


def _attest_test_worker(kb, conn, task, monkeypatch) -> None:
    assert task is not None and task.current_run_id is not None
    assert kb.record_runtime_observation(
        conn,
        task.id,
        task.current_run_id,
        {
            "version": 1,
            "phase": "initial",
            "provider": "",
            "model": "",
            "reasoning_effort": "",
            "runtime": "hermes",
            "api_mode": "chat_completions",
        },
    )
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(task.current_run_id))


def test_complete_goal_mode_rejected_by_judge(monkeypatch, tmp_path):
    """Goal-mode tasks must pass the auxiliary judge before completion.
    Regression for #38367: workers bypassing the judge via early kanban_complete."""
    from pathlib import Path as _Path
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    # Set up isolated HERMES_HOME
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        goal_task_id = kb.create_task(
            conn, title="goal-mode-test", assignee="test-worker",
            body="Must achieve X with verified evidence.", goal_mode=True
        )
        claimed = kb.claim_task(conn, goal_task_id)
        _attest_test_worker(kb, conn, claimed, monkeypatch)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", goal_task_id)

    # Mock the judge to reject the completion. The gate only runs when a
    # judge is reachable, so force the availability probe True as well.
    def mock_judge_goal(goal, last_response, *, timeout=30.0, subgoals=None):
        return "continue", "missing verification evidence", False

    monkeypatch.setattr("tools.kanban_tools.judge_goal", mock_judge_goal)
    monkeypatch.setattr("tools.kanban_tools._goal_judge_available", lambda: True)

    # Attempt to complete should be rejected
    out = kt._handle_complete({"summary": "I did some stuff but not X"})
    d = json.loads(out)
    assert "error" in d
    assert "Goal completion rejected by judge" in d["error"]
    assert "missing verification evidence" in d["error"]
    assert f"parents=[{goal_task_id}]" in d["error"]

    # Verify the task is NOT completed in the DB
    conn2 = kb.connect()
    try:
        task = kb.get_task(conn2, goal_task_id)
        assert task.status == "running"  # Should still be running, not done
    finally:
        conn2.close()


def test_complete_goal_mode_allows_when_judge_unavailable(monkeypatch, tmp_path):
    """Fail-open: an unreachable judge must not wedge a goal_mode worker.

    judge_goal returns a "continue" verdict when no auxiliary model is
    configured, which is indistinguishable from a real "not done" judgment.
    The gate probes availability first, so completion proceeds rather than
    being rejected forever when no judge can be reached."""
    from pathlib import Path as _Path
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        goal_task_id = kb.create_task(
            conn, title="goal-mode-test", assignee="test-worker",
            body="Must achieve X with verified evidence.", goal_mode=True
        )
        claimed = kb.claim_task(conn, goal_task_id)
        _attest_test_worker(kb, conn, claimed, monkeypatch)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", goal_task_id)

    # No judge reachable. judge_goal must not even be consulted; if it were,
    # this stub would reject — so reaching "done" proves the probe short-circuit.
    def fail_if_called(goal, last_response, *, timeout=30.0, subgoals=None):
        raise AssertionError("judge_goal must not run when no judge is available")

    monkeypatch.setattr("tools.kanban_tools.judge_goal", fail_if_called)
    monkeypatch.setattr("tools.kanban_tools._goal_judge_available", lambda: False)

    out = kt._handle_complete({"summary": "done enough"})
    d = json.loads(out)
    assert d.get("ok") is True

    conn2 = kb.connect()
    try:
        assert kb.get_task(conn2, goal_task_id).status == "done"
    finally:
        conn2.close()


def test_block_happy_path(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_block({"reason": "need clarification"})
    d = json.loads(out)
    assert d["ok"] is True
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        assert kb.get_task(conn, worker_env).status == "blocked"
    finally:
        conn.close()


def test_worker_block_returns_typed_terminal_control(worker_env):
    from tools import kanban_tools as kt

    out = kt._handle_block({"reason": "need a human decision"})

    assert isinstance(out, kt.KanbanTerminalControl)
    assert out.action is kt.KanbanTerminalAction.BLOCK
    assert json.loads(out)["ok"] is True


def test_worker_request_rework_adopts_fix_and_stops_loop(worker_env):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    with kb.connect_closing() as conn:
        fix_id = kb.create_task(conn, title="fix finding", assignee="coder")

    out = kt._handle_request_rework({
        "finding": "review caught a P1 defect",
        "fix_task_id": fix_id,
        "request_key": "review-run-1",
    })
    assert isinstance(out, kt.KanbanTerminalControl)
    assert out.action is kt.KanbanTerminalAction.REWORK
    result = json.loads(out)
    assert result["fix_task_id"] == fix_id
    assert result["fix_action"] == "adopted"

    with kb.connect_closing() as conn:
        review = kb.get_task(conn, worker_env)
        assert review is not None
        assert review.status == "todo"
        assert review.block_kind is None
        assert conn.execute(
            "SELECT 1 FROM task_links WHERE parent_id = ? AND child_id = ?",
            (fix_id, worker_env),
        ).fetchone() is not None


def test_worker_request_rework_escalation_is_terminal_control(worker_env, monkeypatch):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    with kb.connect_closing() as conn:
        gate_id = kb.create_task(
            conn, title="human approval", assignee="nicholas"
        )
        kb.link_tasks(conn, worker_env, gate_id)

    for round_number in range(1, 5):
        out = kt._handle_request_rework({
            "finding": f"finding {round_number}",
            "request_key": f"tool-round-{round_number}",
            "fix": {
                "title": f"tool fix {round_number}",
                "body": "apply the correction",
                "assignee": "coder",
            },
            "human_gate_task_id": gate_id,
        })
        assert isinstance(out, kt.KanbanTerminalControl)
        payload = json.loads(out)
        assert payload["escalated"] is False
        fix_id = payload["fix_task_id"]
        with kb.connect_closing() as conn:
            assert kb.complete_task(conn, fix_id, result="fixed")
            claimed = kb.claim_task(conn, worker_env, claimer="test-worker")
            assert claimed is not None and claimed.current_run_id is not None
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))

    out = kt._handle_request_rework({
        "finding": "finding 5",
        "request_key": "tool-round-5",
        "fix": {
            "title": "tool fix 5 must not exist",
            "body": "apply the correction",
            "assignee": "coder",
        },
        "human_gate_task_id": gate_id,
    })
    assert isinstance(out, kt.KanbanTerminalControl)
    assert out.action is kt.KanbanTerminalAction.REWORK
    payload = json.loads(out)
    assert payload["escalated"] is True
    assert payload["fix_task_id"] is None
    assert payload["escalation_target_task_id"] == gate_id

    with kb.connect_closing() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE title = 'tool fix 5 must not exist'"
        ).fetchone()[0] == 0


def test_worker_request_publication_adopts_releaser_card_and_stops_loop(
    worker_env, tmp_path,
):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    workspace = tmp_path / "coder-workspace"
    workspace.mkdir()
    with kb.connect_closing() as conn:
        publication_id = kb.create_task(
            conn,
            title="publish committed change",
            assignee="releaser",
            workspace_kind="dir",
            workspace_path=str(workspace),
            publication_expected_sha="a" * 40,
            publication_remote="origin",
            publication_ref="refs/heads/main",
        )

    out = kt._handle_request_publication({
        "publication_task_id": publication_id,
        "request_key": "publication-run-1",
    })
    assert isinstance(out, kt.KanbanTerminalControl)
    assert out.action is kt.KanbanTerminalAction.PUBLICATION
    result = json.loads(out)
    assert result["publication_task_id"] == publication_id
    assert result["publication_action"] == "adopted"

    with kb.connect_closing() as conn:
        requester = kb.get_task(conn, worker_env)
        assert requester is not None
        assert requester.status == "todo"
        assert conn.execute(
            "SELECT 1 FROM task_links WHERE parent_id = ? AND child_id = ?",
            (publication_id, worker_env),
        ).fetchone() is not None


def test_later_run_replayed_rework_is_not_terminal_control(worker_env, monkeypatch):
    """A stable key from an older run must not strand the current run."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    with kb.connect_closing() as conn:
        fix_id = kb.create_task(conn, title="fix finding", assignee="coder")

    first = kt._handle_request_rework({
        "finding": "review caught a P1 defect",
        "fix_task_id": fix_id,
        "request_key": "stable-finding-key",
    })
    assert isinstance(first, kt.KanbanTerminalControl)

    with kb.connect_closing() as conn:
        assert kb.complete_task(conn, fix_id, result="fixed")
        claimed = kb.claim_task(conn, worker_env, claimer="test-worker")
        assert claimed is not None and claimed.current_run_id is not None
        run_id = int(claimed.current_run_id)

    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    replay = kt._handle_request_rework({
        "finding": "review caught a P1 defect",
        "fix_task_id": fix_id,
        "request_key": "stable-finding-key",
    })

    assert not isinstance(replay, kt.KanbanTerminalControl)
    replay_payload = json.loads(replay)
    assert replay_payload["fix_action"] == "replayed"
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, worker_env)
        assert task is not None
        assert task.status == "running"
        assert task.current_run_id == run_id
        run = conn.execute(
            "SELECT ended_at, outcome FROM task_runs WHERE id = ?", (run_id,)
        ).fetchone()
        assert run is not None
        assert run["ended_at"] is None
        assert run["outcome"] is None


def test_request_rework_requires_exactly_one_fix_form(worker_env):
    from tools import kanban_tools as kt

    both = json.loads(kt._handle_request_rework({
        "finding": "bad",
        "request_key": "rk",
        "fix_task_id": "t_existing",
        "fix": {"title": "new", "assignee": "coder"},
    }))
    assert "exactly one" in both["error"]


def test_block_rejects_empty_reason(worker_env):
    from tools import kanban_tools as kt
    for bad in ["", "   ", None]:
        out = kt._handle_block({"reason": bad})
        assert json.loads(out).get("error")


def _make_goal_mode_worker_env(monkeypatch, tmp_path, *, unfinished_parent=False):
    """Set up an isolated HERMES_HOME with one claimed goal_mode task,
    matching the pattern used by the kanban_complete judge gate tests."""
    from pathlib import Path as _Path
    from hermes_cli import kanban_db as kb

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        goal_task_id = kb.create_task(
            conn, title="goal-mode-block-test", assignee="test-worker",
            body="Must achieve X.", goal_mode=True,
        )
        claimed = kb.claim_task(conn, goal_task_id)
        _attest_test_worker(kb, conn, claimed, monkeypatch)
        if unfinished_parent:
            parent_id = kb.create_task(
                conn, title="unfinished dependency", assignee="test-worker",
            )
            # Link after claiming so the test models a worker that discovers
            # the dependency during its active attempt.
            kb.link_tasks(conn, parent_id, goal_task_id)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", goal_task_id)
    return goal_task_id


def test_block_goal_mode_rejects_missing_kind(monkeypatch, tmp_path):
    """A goal_mode worker calling kanban_block with no kind must not be able
    to use it as an unguarded escape from the goal loop (Issue #38696,
    sibling of the kanban_complete judge gate / Issue #38367)."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb

    tid = _make_goal_mode_worker_env(monkeypatch, tmp_path)
    out = kt._handle_block({"reason": "giving up"})
    d = json.loads(out)
    assert "error" in d
    assert "goal_mode" in d["error"]

    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid).status == "running"
    finally:
        conn.close()


def test_block_goal_mode_rejects_disallowed_kind(monkeypatch, tmp_path):
    """`capability` / `transient` are valid kinds in general but must not
    let a goal_mode worker exit the loop without going through the judge."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb

    tid = _make_goal_mode_worker_env(monkeypatch, tmp_path)
    for kind in ("capability", "transient"):
        out = kt._handle_block({"reason": "blocked", "kind": kind})
        d = json.loads(out)
        assert "error" in d, f"kind={kind} should be rejected for goal_mode"

    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid).status == "running"
    finally:
        conn.close()


def test_block_goal_mode_allows_dependency_kind(monkeypatch, tmp_path):
    """`dependency` and `needs_input` represent a genuine external blocker
    the worker cannot resolve itself — these remain ungated.

    `dependency` routes to status='todo' (not 'blocked') per block_task's
    own kind-routing — the goal loop still treats anything outside
    running/ready/done/blocked as a stop, so this is still a legitimate,
    judge-free exit; it's just not the literal 'blocked' status."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb

    tid = _make_goal_mode_worker_env(
        monkeypatch, tmp_path, unfinished_parent=True,
    )
    out = kt._handle_block({"reason": "waiting on another task", "kind": "dependency"})
    d = json.loads(out)
    assert d.get("ok") is True

    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid).status == "todo"
    finally:
        conn.close()


def test_block_goal_mode_allows_needs_input_kind(monkeypatch, tmp_path):
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb

    tid = _make_goal_mode_worker_env(monkeypatch, tmp_path)
    out = kt._handle_block({"reason": "need a decision from the user", "kind": "needs_input"})
    d = json.loads(out)
    assert d.get("ok") is True

    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid).status == "blocked"
    finally:
        conn.close()


def test_block_non_goal_mode_task_unaffected_by_new_gate(worker_env):
    """The new gate only applies to goal_mode tasks — plain tasks must keep
    blocking freely with no kind, exactly as before this fix."""
    from tools import kanban_tools as kt
    out = kt._handle_block({"reason": "need clarification"})
    assert json.loads(out).get("ok") is True


def test_heartbeat_happy_path(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_heartbeat({"note": "progress"})
    d = json.loads(out)
    assert d["ok"] is True


def test_heartbeat_without_note(worker_env):
    """note is optional."""
    from tools import kanban_tools as kt
    out = kt._handle_heartbeat({})
    d = json.loads(out)
    assert d["ok"] is True


def test_heartbeat_extends_claim_expires(worker_env):
    """The kanban_heartbeat tool MUST extend claim_expires, not just
    update last_heartbeat_at — otherwise long-running workers loop the
    heartbeat tool diligently and still get reclaimed by
    release_stale_claims at DEFAULT_CLAIM_TTL_SECONDS.

    Regression test for the bug where _handle_heartbeat called
    heartbeat_worker but never heartbeat_claim, so claim_expires sat
    static while last_heartbeat_at advanced.
    """
    import time as _time
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    # Rewind claim_expires into the past so any forward movement is
    # unambiguous (avoids time.sleep flakiness).
    conn = kb.connect()
    try:
        conn.execute(
            "UPDATE tasks SET claim_expires = ? WHERE id = ?",
            (1, worker_env),
        )
        conn.commit()
        before = conn.execute(
            "SELECT claim_expires FROM tasks WHERE id = ?", (worker_env,)
        ).fetchone()["claim_expires"]
    finally:
        conn.close()
    assert before == 1

    out = kt._handle_heartbeat({"note": "still alive"})
    assert json.loads(out).get("ok") is True

    conn = kb.connect()
    try:
        after = conn.execute(
            "SELECT claim_expires FROM tasks WHERE id = ?", (worker_env,)
        ).fetchone()["claim_expires"]
    finally:
        conn.close()

    now = int(_time.time())
    # claim_expires should be roughly now + DEFAULT_CLAIM_TTL_SECONDS.
    # We assert a generous floor (now + half the default TTL) to keep the
    # test stable against future TTL changes.
    assert after > before, (
        f"claim_expires did not advance ({before} -> {after}); workers "
        f"would be reclaimed at TTL despite heartbeating"
    )
    assert after >= now + (kb.DEFAULT_CLAIM_TTL_SECONDS // 2), (
        f"claim_expires={after} is suspiciously close to now={now}; "
        f"expected at least now + {kb.DEFAULT_CLAIM_TTL_SECONDS // 2}"
    )


def test_process_keepalive_does_not_advance_semantic_progress(worker_env):
    """A live wrapper waiting on a provider is not evidence of task progress."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    conn = kb.connect()
    try:
        initial_semantic = conn.execute(
            "SELECT last_semantic_progress_at FROM task_runs WHERE id = "
            "(SELECT current_run_id FROM tasks WHERE id = ?)",
            (worker_env,),
        ).fetchone()["last_semantic_progress_at"]
    finally:
        conn.close()

    kt._auto_heartbeat_last_attempt = 0.0
    assert kt.heartbeat_current_worker_from_env(activity_kind="process") is True

    conn = kb.connect()
    try:
        row = conn.execute(
            "SELECT last_heartbeat_at, last_semantic_progress_at "
            "FROM task_runs WHERE id = "
            "(SELECT current_run_id FROM tasks WHERE id = ?)",
            (worker_env,),
        ).fetchone()
    finally:
        conn.close()

    assert row["last_heartbeat_at"] is not None
    assert row["last_semantic_progress_at"] == initial_semantic


def test_typed_activity_advances_distinct_run_clocks(worker_env):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    def record(kind):
        assert kt.heartbeat_current_worker_from_env(activity_kind=kind) is True

    def clocks():
        conn = kb.connect()
        try:
            return conn.execute(
                "SELECT last_transport_activity_at, last_semantic_progress_at, "
                "last_durable_progress_at FROM task_runs WHERE id = "
                "(SELECT current_run_id FROM tasks WHERE id = ?)",
                (worker_env,),
            ).fetchone()
        finally:
            conn.close()

    kt._auto_heartbeat_last_attempt = 0.0
    initial = clocks()
    record("transport")
    row = clocks()
    assert row["last_transport_activity_at"] is not None
    assert row["last_semantic_progress_at"] == initial["last_semantic_progress_at"]
    assert row["last_durable_progress_at"] == initial["last_durable_progress_at"]

    record("semantic")
    row = clocks()
    assert row["last_semantic_progress_at"] is not None
    assert row["last_durable_progress_at"] == initial["last_durable_progress_at"]

    record("durable")
    assert clocks()["last_durable_progress_at"] is not None
    conn = kb.connect()
    try:
        event = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? "
            "AND kind = 'heartbeat' ORDER BY id DESC LIMIT 1",
            (worker_env,),
        ).fetchone()
    finally:
        conn.close()
    assert json.loads(event["payload"])["activity_kind"] == "durable"


def test_comment_happy_path(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_comment({
        "task_id": worker_env,
        "body": "hello thread",
    })
    d = json.loads(out)
    assert d["ok"] is True
    assert d["comment_id"]
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        comments = kb.list_comments(conn, worker_env)
        assert len(comments) == 1
        # Author defaults to HERMES_PROFILE env we set in the fixture
        assert comments[0].author == "test-worker"
        assert comments[0].body == "hello thread"
    finally:
        conn.close()


def test_comment_rejects_empty_body(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_comment({"task_id": worker_env, "body": "   "})
    assert json.loads(out).get("error")


def test_comment_ignores_caller_supplied_author(worker_env):
    """``args["author"]`` is no longer honored — the author is always
    derived from ``HERMES_PROFILE`` so a worker can't forge a comment
    under an authoritative-looking name like ``hermes-system`` and
    poison the next worker's prompt context. Cross-task commenting
    itself remains unrestricted (see #19713); only the author override
    is removed.
    """
    from tools import kanban_tools as kt
    out = kt._handle_comment({
        "task_id": worker_env, "body": "hi", "author": "hermes-system",
    })
    assert json.loads(out)["ok"]
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        comments = kb.list_comments(conn, worker_env)
        # Author comes from HERMES_PROFILE in the fixture, not the
        # caller-supplied "hermes-system" override.
        assert comments[0].author == "test-worker"
    finally:
        conn.close()


def test_comment_schema_omits_author_override():
    """The ``author`` property must not appear on KANBAN_COMMENT_SCHEMA;
    exposing it to the LLM would re-introduce the forgery surface this
    handler is hardened against.
    """
    from tools.kanban_tools import KANBAN_COMMENT_SCHEMA
    props = KANBAN_COMMENT_SCHEMA["parameters"]["properties"]
    assert "author" not in props


def test_create_happy_path(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_create({
        "title": "child task",
        "assignee": "peer",
        "parents": [worker_env],
    })
    d = json.loads(out)
    assert d["ok"] is True
    assert d["task_id"]
    assert d["status"] == "todo"  # parent isn't done yet
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        child = kb.get_task(conn, d["task_id"])
        assert child.title == "child task"
        assert child.assignee == "peer"
    finally:
        conn.close()


def test_create_schema_exposes_model_override():
    from tools.kanban_tools import KANBAN_CREATE_SCHEMA

    props = KANBAN_CREATE_SCHEMA["parameters"]["properties"]
    assert "model_override" in props
    assert props["model_override"]["type"] == "string"


def test_create_persists_model_override(worker_env):
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb

    out = kt._handle_create({
        "title": "cheap worker",
        "assignee": "peer",
        "model_override": "deepseek/deepseek-v4-flash:free",
    })
    d = json.loads(out)
    assert d["ok"] is True
    with kb.connect() as conn:
        task = kb.get_task(conn, d["task_id"])
    assert task is not None
    assert task.model_override == "deepseek/deepseek-v4-flash:free"


def test_create_blank_model_override_uses_profile_default(worker_env):
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb

    out = kt._handle_create({
        "title": "default model",
        "assignee": "peer",
        "model_override": "   ",
    })
    d = json.loads(out)
    assert d["ok"] is True
    with kb.connect() as conn:
        task = kb.get_task(conn, d["task_id"])
    assert task is not None
    assert task.model_override is None



def _write_model_routing_table(home: str | os.PathLike[str], *, weak_flash: bool = False) -> Path:
    state = Path(home) / "state"
    state.mkdir(parents=True, exist_ok=True)
    path = state / "model-routing-table.json"
    flash_coding = 0.58 if weak_flash else 0.68
    payload = {
        "version": 1,
        "updated_at": "2026-05-26T00:00:00Z",
        "policy": {
            "uncertainty_bias": "over_resource",
            "front_door_route": {
                "provider": "openai-codex",
                "model": "gpt-5.5",
                "reasoning_effort": "high",
                "route_key": "openai-codex/gpt-5.5",
            },
            "default_uncertain_route": {
                "provider": "openai-codex",
                "model": "gpt-5.5",
                "reasoning_effort": "xhigh",
                "route_key": "openai-codex/gpt-5.5",
            },
            "privacy_rule": "sensitive work must not route to public/free providers",
            "stale_after_days": 45,
        },
        "task_lanes": {
            "front_door": {
                "provider": "openai-codex",
                "model": "gpt-5.5",
                "reasoning_effort": "high",
                "route_key": "openai-codex/gpt-5.5",
                "min_scores": {"reasoning": 0.75, "writing": 0.70},
                "quota_mode": "always_premium",
            },
            "front_door_or_uncertain": {
                "provider": "openai-codex",
                "model": "gpt-5.5",
                "reasoning_effort": "high",
                "route_key": "openai-codex/gpt-5.5",
                "min_scores": {"reasoning": 0.75, "writing": 0.70},
                "quota_mode": "always_premium",
            },
            "uncertainty_escalation": {
                "provider": "openai-codex",
                "model": "gpt-5.5",
                "reasoning_effort": "xhigh",
                "route_key": "openai-codex/gpt-5.5",
                "min_scores": {"reasoning": 0.85, "writing": 0.80},
                "quota_mode": "always_premium",
            },
            "verification_leaf": {
                "provider": "nous",
                "model": "deepseek/deepseek-v4-flash:free",
                "reasoning_effort": "low",
                "route_key": "nous/deepseek/deepseek-v4-flash:free",
                "min_scores": {"coding": 0.60, "data_scan": 0.50},
                "quota_mode": "normal",
                "fallback": ["front_door"],
                "data_classes_allowed": ["public", "trivial"],
            },
            "code_implementation": {
                "provider": "deepseek",
                "model": "deepseek-v4-pro",
                "reasoning_effort": "xhigh",
                "route_key": "deepseek/deepseek-v4-pro",
                "min_scores": {"coding": 0.75, "reasoning": 0.70},
                "quota_mode": "budget_ok",
                "budget_route": {
                    "provider": "nous",
                    "model": "deepseek/deepseek-v4-flash:free",
                    "reasoning_effort": "low",
                    "route_key": "nous/deepseek/deepseek-v4-flash:free",
                },
                "fallback": ["uncertainty_escalation"],
            },
            "security_review": {
                "provider": "openai-codex",
                "model": "gpt-5.5",
                "reasoning_effort": "xhigh",
                "route_key": "openai-codex/gpt-5.5",
                "min_scores": {"reasoning": 0.85, "coding": 0.75},
                "quota_mode": "always_premium",
            },
        },
        "models": [
            {
                "provider": "openai-codex",
                "model": "gpt-5.5",
                "route_key": "openai-codex/gpt-5.5",
                "benchmark_profile": {"reasoning": 0.93, "coding": 0.86, "data_scan": 0.80, "writing": 0.89},
                "benchmark_metadata": {"composite_score": 0.91, "confidence": "high"},
            },
            {
                "provider": "nous",
                "model": "deepseek/deepseek-v4-flash:free",
                "route_key": "nous/deepseek/deepseek-v4-flash:free",
                "benchmark_profile": {"reasoning": 0.61, "coding": flash_coding, "data_scan": 0.59, "writing": 0.82},
                "benchmark_metadata": {"composite_score": 0.70, "confidence": "medium"},
            },
            {
                "provider": "deepseek",
                "model": "deepseek-v4-pro",
                "route_key": "deepseek/deepseek-v4-pro",
                "benchmark_profile": {"reasoning": 0.72, "coding": 0.80, "data_scan": 0.67, "writing": 0.76},
                "benchmark_metadata": {"composite_score": 0.78, "confidence": "medium"},
            },
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _write_architecture_gate_policy(
    home: str | os.PathLike[str], *, version: str = "v1", mode: str = "off",
) -> Path:
    rules = Path(home) / "rules"
    rules.mkdir(parents=True, exist_ok=True)
    path = rules / "architecture-gate-policy.json"
    path.write_text(json.dumps({"version": version, "mode": mode}), encoding="utf-8")
    return path


def _write_quota_usage(
    home: str | os.PathLike[str],
    *,
    pct: float,
    resets_at: datetime | str | None = None,
) -> Path:
    state = Path(home) / "state"
    state.mkdir(parents=True, exist_ok=True)
    path = state / "quota-usage.json"
    payload = {
        "usage_pct": pct,
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": "test-estimate",
    }
    if resets_at is not None:
        payload["resets_at"] = resets_at if isinstance(resets_at, str) else resets_at.isoformat().replace("+00:00", "Z")
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _write_native_quota_snapshot(
    state_dir: str | os.PathLike[str],
    *,
    pct: float | None = None,
    provider: str = "openai-codex",
    suffix: str = "test",
    fetched_at: datetime | str | None = None,
    windows: dict | None = None,
    additional_limits: list[dict] | None = None,
) -> Path:
    path = Path(state_dir) / f"provider-native-quota-{provider}-{suffix}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    reset = datetime.now(timezone.utc) + timedelta(hours=1)
    fetched = fetched_at or datetime.now(timezone.utc)
    payload = {
        "provider": provider,
        "fetched_at": fetched if isinstance(fetched, str) else fetched.isoformat().replace("+00:00", "Z"),
        "windows": windows if windows is not None else {
            "five_hour": {
                "used_percentage": pct,
                "resets_at": reset.isoformat().replace("+00:00", "Z"),
            }
        },
    }
    if additional_limits is not None:
        payload["additional_limits"] = additional_limits
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_create_schema_exposes_model_routing_classification_fields():
    from tools.kanban_tools import KANBAN_CREATE_SCHEMA

    props = KANBAN_CREATE_SCHEMA["parameters"]["properties"]
    assert "model_routing" in props
    assert props["model_routing"]["type"] == "string"
    assert "task_category" in props
    assert "data_sensitivity" in props
    assert "verifiability" in props
    assert "quota_policy" in props


def test_create_model_routing_preset_persists_override_and_logs_decision(monkeypatch, worker_env):
    home = os.environ["HERMES_HOME"]
    _write_model_routing_table(home)
    monkeypatch.setenv("HERMES_ORCHESTRATION_TELEMETRY", "1")

    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    out = kt._handle_create({
        "title": "run tests",
        "assignee": "qa",
        "model_routing": "verification_leaf",
        "data_sensitivity": "public",
    })
    d = json.loads(out)
    assert d["ok"] is True
    assert d["model_override"] == "deepseek/deepseek-v4-flash:free"
    assert d["model_provider_override"] == "nous"
    assert d["model_reasoning_effort"] == "low"
    assert d["model_routing"]["preset"] == "verification_leaf"

    with kb.connect() as conn:
        task = kb.get_task(conn, d["task_id"])
    assert task is not None
    assert task.model_override == "deepseek/deepseek-v4-flash:free"
    assert task.model_provider_override == "nous"
    assert task.model_reasoning_effort == "low"

    from orchestration_telemetry import read_events, telemetry_path
    events = read_events(path=telemetry_path())
    event = events[-1]
    event_text = json.dumps(event, sort_keys=True)
    assert event["event_type"] == "route.selected"
    assert event["route"]["model_routing_preset"] == "verification_leaf"
    assert event["route"]["provider"] == "nous"
    assert event["route"]["model"] == "deepseek/deepseek-v4-flash:free"
    assert event["route"]["model_override"] == "deepseek/deepseek-v4-flash:free"
    assert event["routing_decision"]["benchmark_source"] == "model-routing-table.json"
    assert event["routing_decision"]["benchmark_composite"] == 0.70
    assert event["gates"]["privacy"] == "title/body/idempotency_key excluded from telemetry"
    assert "run tests" not in event_text


def test_create_model_routing_privacy_escalates_sensitive_public_lane(worker_env):
    _write_model_routing_table(os.environ["HERMES_HOME"])

    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    out = kt._handle_create({
        "title": "security review",
        "assignee": "analyst",
        "model_routing": "verification_leaf",
        "data_sensitivity": "security",
    })
    d = json.loads(out)
    assert d["ok"] is True
    assert d["model_override"] == "gpt-5.5"
    assert d["model_routing"]["privacy_escalated"] is True
    assert d["model_routing"]["provider"] == "openai-codex"
    assert d["model_routing"]["reasoning_effort"] == "high"

    with kb.connect() as conn:
        task = kb.get_task(conn, d["task_id"])
    assert task is not None
    assert task.model_override == "gpt-5.5"
    assert task.model_reasoning_effort == "high"


def test_legacy_front_door_or_uncertain_alias_resolves_to_front_door_high(worker_env):
    _write_model_routing_table(os.environ["HERMES_HOME"])

    from tools import kanban_tools as kt
    out = kt._handle_create({
        "title": "publish and verify",
        "assignee": "orchestrator",
        "model_routing": "front_door_or_uncertain",
        "data_sensitivity": "credential-adjacent",
    })
    d = json.loads(out)
    assert d["ok"] is True
    assert d["model_override"] == "gpt-5.5"
    assert d["model_provider_override"] == "openai-codex"
    assert d["model_reasoning_effort"] == "high"
    assert d["model_routing"]["resolved_preset"] == "front_door"


def test_create_model_routing_persists_xhigh_route_tuple(worker_env):
    _write_model_routing_table(os.environ["HERMES_HOME"])

    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb

    out = kt._handle_create({
        "title": "security review",
        "assignee": "reviewer",
        "model_routing": "security_review",
        "data_sensitivity": "security",
    })
    d = json.loads(out)

    assert d["ok"] is True
    assert d["model_override"] == "gpt-5.5"
    assert d["model_provider_override"] == "openai-codex"
    assert d["model_reasoning_effort"] == "xhigh"
    with kb.connect() as conn:
        task = kb.get_task(conn, d["task_id"])
    assert task is not None
    assert task.model_override == "gpt-5.5"
    assert task.model_provider_override == "openai-codex"
    assert task.model_reasoning_effort == "xhigh"


def test_create_model_routing_quota_uses_budget_route_for_budget_ok_lane(monkeypatch, worker_env):
    home = os.environ["HERMES_HOME"]
    _write_model_routing_table(home)
    _write_quota_usage(home, pct=91.0)
    monkeypatch.setenv("HERMES_NATIVE_QUOTA_STATE_DIR", str(Path(home) / "state"))

    from tools import kanban_tools as kt
    out = kt._handle_create({
        "title": "straightforward implementation",
        "assignee": "engineer",
        "model_routing": "code_implementation",
        "task_category": "implementation/routine",
        "data_sensitivity": "public",
        "verifiability": "high",
        "quota_policy": "downgrade_verifiable",
    })
    d = json.loads(out)
    assert d["ok"] is True
    assert d["model_override"] == "deepseek/deepseek-v4-flash:free"
    assert d["model_routing"]["quota_gate_triggered"] is True
    assert d["model_routing"]["quota_usage_pct"] == 91.0
    assert d["model_routing"]["provider"] == "nous"


def test_create_model_routing_reads_native_quota_snapshot(monkeypatch, worker_env):
    home = os.environ["HERMES_HOME"]
    state = Path(home) / "state"
    _write_native_quota_snapshot(state, pct=88.0)
    monkeypatch.setenv("HERMES_NATIVE_QUOTA_STATE_DIR", str(state))

    from tools.kanban_tools import _estimate_quota_usage
    quota = _estimate_quota_usage()
    assert quota is not None
    assert quota["usage_pct"] == 88.0
    assert quota["source"].startswith("provider-native-quota")


def test_quota_prefers_profile_native_over_expired_legacy_with_env_unset(monkeypatch, worker_env):
    home = os.environ["HERMES_HOME"]
    state = Path(home) / "state"
    _write_model_routing_table(home)
    _write_native_quota_snapshot(state, pct=17.0)
    _write_quota_usage(home, pct=99.0, resets_at=datetime.now(timezone.utc) - timedelta(minutes=5))
    monkeypatch.delenv("HERMES_NATIVE_QUOTA_STATE_DIR", raising=False)

    from tools import kanban_tools as kt

    quota = kt._estimate_quota_usage()
    assert quota is not None
    assert quota["usage_pct"] == 17.0
    assert quota["source"].startswith("provider-native-quota-openai-codex")

    model, decision = kt._resolve_model_routing({
        "model_routing": "code_implementation",
        "task_category": "implementation/routine",
        "data_sensitivity": "public",
        "verifiability": "tests",
        "quota_policy": "auto",
    }, None)
    assert model == "deepseek-v4-pro"
    assert decision is not None
    assert decision["quota_gate_triggered"] is False
    assert decision["quota_usage_pct"] == 17.0
    assert decision["provider"] == "deepseek"


def test_native_quota_state_dir_splits_pathsep_and_ignores_other_providers(monkeypatch, tmp_path, worker_env):
    first = tmp_path / "native-one"
    second = tmp_path / "native-two"
    _write_native_quota_snapshot(first, pct=93.0, provider="anthropic", suffix="claude")
    _write_native_quota_snapshot(second, pct=23.0, provider="openai-codex", suffix="codex")
    monkeypatch.setenv("HERMES_NATIVE_QUOTA_STATE_DIR", os.pathsep.join([str(first), str(second)]))

    from tools.kanban_tools import _estimate_quota_usage

    quota = _estimate_quota_usage()
    assert quota is not None
    assert quota["usage_pct"] == 23.0
    assert quota["source"].startswith("provider-native-quota-openai-codex")


def test_expired_legacy_quota_is_ignored(monkeypatch, worker_env):
    home = os.environ["HERMES_HOME"]
    legacy = _write_quota_usage(home, pct=98.0, resets_at=datetime.now(timezone.utc) - timedelta(minutes=1))
    monkeypatch.delenv("HERMES_NATIVE_QUOTA_STATE_DIR", raising=False)

    from tools.kanban_tools import _estimate_quota_usage

    assert _estimate_quota_usage() is None
    assert not legacy.exists()


def test_native_additional_limits_high_usage_beats_low_top_level_window(monkeypatch, worker_env):
    home = os.environ["HERMES_HOME"]
    state = Path(home) / "state"
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    _write_native_quota_snapshot(
        state,
        windows={"five_hour": {"used_percentage": 10.0, "resets_at": future}},
        additional_limits=[
            {"name": "weekly", "windows": {"week": {"usage_pct": 86.5, "resets_at": future}}},
        ],
    )
    monkeypatch.setenv("HERMES_NATIVE_QUOTA_STATE_DIR", str(state))

    from tools.kanban_tools import _estimate_quota_usage

    quota = _estimate_quota_usage()
    assert quota is not None
    assert quota["usage_pct"] == 86.5
    assert quota["window"] == "week"


def test_fresh_legacy_quota_still_used_when_native_absent(monkeypatch, worker_env):
    home = os.environ["HERMES_HOME"]
    _write_quota_usage(home, pct=72.0, resets_at=datetime.now(timezone.utc) + timedelta(minutes=30))
    monkeypatch.delenv("HERMES_NATIVE_QUOTA_STATE_DIR", raising=False)

    from tools.kanban_tools import _estimate_quota_usage

    quota = _estimate_quota_usage()
    assert quota is not None
    assert quota["usage_pct"] == 72.0
    assert quota["source"] == "quota-usage.json"


def test_create_model_routing_benchmark_failure_prefers_same_effort(worker_env):
    """When a lane fails its benchmark gate, same-effort alternatives are preferred
    over escalating to a higher-effort fallback. With weak_flash (coding=0.58),
    verification_leaf fails its coding gate. A same-effort (low) alternative like
    vault_curation should be selected instead of escalating to front_door (high)."""
    _write_model_routing_table(os.environ["HERMES_HOME"], weak_flash=True)

    from tools import kanban_tools as kt
    out = kt._handle_create({
        "title": "run tests",
        "assignee": "qa",
        "model_routing": "verification_leaf",
        "data_sensitivity": "public",
    })
    d = json.loads(out)
    assert d["ok"] is True
    # Should prefer a same-effort (low) alternative over escalating to front_door (high)
    assert d["model_routing"]["fallback_applied"] is True
    reason = d["model_routing"]["fallback_reason"]
    assert "benchmark_failed" in reason
    # The same-effort fallback should remain at low (not escalate to high/xhigh)
    assert d["model_reasoning_effort"] != "xhigh"
def test_create_inherits_worker_dir_workspace(monkeypatch, worker_env):
    """A worker scoped to a dir: task that spawns a child without a
    workspace arg inherits the dir, not scratch (so follow-up code-gen
    lands in the same project)."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb

    proj = "/home/teknium/myproject"
    conn = kb.connect()
    try:
        self_tid = kb.create_task(
            conn, title="dir worker", assignee="test-worker",
            workspace_kind="dir", workspace_path=proj,
        )
        kb.claim_task(conn, self_tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", self_tid)

    d = json.loads(kt._handle_create({"title": "follow-up", "assignee": "peer"}))
    assert d["ok"] is True
    conn = kb.connect()
    try:
        child = kb.get_task(conn, d["task_id"])
        assert child.workspace_kind == "dir"
        assert child.workspace_path == proj
    finally:
        conn.close()


def test_create_explicit_workspace_beats_inheritance(monkeypatch, worker_env):
    """An explicit workspace arg overrides worker-task inheritance."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb

    conn = kb.connect()
    try:
        self_tid = kb.create_task(
            conn, title="dir worker", assignee="test-worker",
            workspace_kind="dir", workspace_path="/home/teknium/proj",
        )
        kb.claim_task(conn, self_tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", self_tid)

    d = json.loads(kt._handle_create({
        "title": "scratch child", "assignee": "peer",
        "workspace_kind": "scratch",
    }))
    assert d["ok"] is True
    conn = kb.connect()
    try:
        child = kb.get_task(conn, d["task_id"])
        assert child.workspace_kind == "scratch"
    finally:
        conn.close()


def test_create_no_worker_task_stays_scratch(monkeypatch, worker_env):
    """Orchestrator/CLI callers (no HERMES_KANBAN_TASK) still default to
    scratch — inheritance only applies to task-scoped workers."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    d = json.loads(kt._handle_create({"title": "orch child", "assignee": "peer"}))
    assert d["ok"] is True
    conn = kb.connect()
    try:
        child = kb.get_task(conn, d["task_id"])
        assert child.workspace_kind == "scratch"
        assert child.workspace_path is None
    finally:
        conn.close()


def test_create_stamps_session_id_from_env(monkeypatch, worker_env):
    """When the agent loop runs under ACP, the server propagates the
    originating chat session id via HERMES_SESSION_ID. ``kanban_create``
    reads it and stamps the new task so clients can render a per-session
    board (issue: ACP session linkage on kanban tasks)."""
    monkeypatch.setenv("HERMES_SESSION_ID", "acp-sess-abc")
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    out = kt._handle_create({
        "title": "from chat",
        "assignee": "peer",
        "parents": [worker_env],
    })
    d = json.loads(out)
    assert d["ok"] is True
    conn = kb.connect()
    try:
        new_task = kb.get_task(conn, d["task_id"])
        assert new_task.session_id == "acp-sess-abc"
    finally:
        conn.close()


def test_create_session_id_arg_overrides_env(monkeypatch, worker_env):
    """An explicit ``session_id`` arg from the model wins over the env
    propagation. Edge case but exercised: a tool call could carry a
    different session id (e.g. cross-session linking) and the explicit
    arg should not be silently overwritten."""
    monkeypatch.setenv("HERMES_SESSION_ID", "from-env")
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    out = kt._handle_create({
        "title": "explicit override",
        "assignee": "peer",
        "parents": [worker_env],
        "session_id": "explicit-arg",
    })
    d = json.loads(out)
    assert d["ok"] is True
    conn = kb.connect()
    try:
        new_task = kb.get_task(conn, d["task_id"])
        assert new_task.session_id == "explicit-arg"
    finally:
        conn.close()


def test_create_session_id_absent_when_env_unset(monkeypatch, worker_env):
    """No env var, no arg → session_id stays NULL. Important for backwards
    compatibility: pre-ACP-propagation hosts and CLI-driven creates must
    not accidentally inherit a stale id."""
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    out = kt._handle_create({
        "title": "no session",
        "assignee": "peer",
        "parents": [worker_env],
    })
    d = json.loads(out)
    assert d["ok"] is True
    conn = kb.connect()
    try:
        new_task = kb.get_task(conn, d["task_id"])
        assert new_task.session_id is None
    finally:
        conn.close()


@pytest.mark.parametrize("mode", ["shadow", "orchestrator_only"])
@pytest.mark.parametrize("model_routing", [None, "verification_leaf"])
def test_front_door_architecture_create_opens_or_reuses_trusted_gate_without_routing(
    monkeypatch, worker_env, mode, model_routing,
):
    """Trusted front-door identity, not model routing, activates staged gates."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setenv("HERMES_PROFILE", "orchestrator")
    monkeypatch.setenv("HERMES_SESSION_ID", "trusted-session")
    # Earlier tests may bind a process-local session ContextVar.  This case
    # models an unattached front-door invocation whose authority comes from
    # its environment, so restore the unbound resolution state explicitly.
    from gateway.session_context import reset_session_vars
    reset_session_vars()
    args = {
        "title": "Design the workflow", "assignee": "architect",
        "session_id": "forged-model-session",
    }
    if model_routing is not None:
        args["model_routing"] = model_routing
    _write_architecture_gate_policy(os.environ["HERMES_HOME"], mode=mode)

    first = json.loads(kt._handle_create(args, turn_id="trusted-turn"))
    second = json.loads(kt._handle_create(args, turn_id="trusted-turn"))
    assert first["ok"] and second["ok"]
    assert first["task_id"] == second["task_id"]
    with kb.connect() as conn:
        gate = kb.get_architecture_gate_for_task(conn, first["task_id"])
        assert gate is not None and gate.state == "open"
        assert gate.enforcement_mode == mode
        assert gate.session_id == "trusted-session"
        assert gate.session_id != args["session_id"]
        assert conn.execute("SELECT COUNT(*) FROM architecture_gates").fetchone()[0] == 1


def test_front_door_open_gate_denies_unparented_coder_in_same_turn(
    monkeypatch, worker_env,
):
    """Every managed front-door mutation receives trusted gate context."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setenv("HERMES_PROFILE", "orchestrator")
    monkeypatch.setenv("HERMES_SESSION_ID", "trusted-session")
    from gateway.session_context import reset_session_vars
    reset_session_vars()
    _write_architecture_gate_policy(
        os.environ["HERMES_HOME"], mode="orchestrator_only"
    )

    architect = json.loads(
        kt._handle_create(
            {"title": "Design workflow", "assignee": "architect"},
            turn_id="trusted-turn",
        )
    )
    assert architect["ok"]
    with kb.connect() as conn:
        before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]

    denied = json.loads(
        kt._handle_create(
            {"title": "Bypass implementation", "assignee": "coder"},
            turn_id="trusted-turn",
        )
    )
    assert "architecture_gate_open" in denied["error"]
    with kb.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == before


@pytest.mark.parametrize(
    ("profile", "assignee"), [("worker", "architect"), ("orchestrator", "coder")],
)
def test_front_door_architecture_gate_does_not_expand_to_ordinary_creates(
    monkeypatch, worker_env, profile, assignee,
):
    """Staged policy changes only the trusted orchestrator-to-architect boundary."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setenv("HERMES_PROFILE", profile)
    monkeypatch.setenv("HERMES_SESSION_ID", "trusted-session")
    _write_architecture_gate_policy(os.environ["HERMES_HOME"], mode="shadow")

    created = json.loads(kt._handle_create(
        {"title": "Ordinary task", "assignee": assignee}, turn_id="trusted-turn",
    ))
    assert created["ok"]
    with kb.connect() as conn:
        assert kb.get_architecture_gate_for_task(conn, created["task_id"]) is None


def test_front_door_architecture_create_uses_managed_mode_and_turn_scope(monkeypatch, worker_env):
    """The trusted boundary reads managed policy and never scopes to model data."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.config import DEFAULT_CONFIG
    from tools import kanban_tools as kt

    assert "architecture_gate" not in DEFAULT_CONFIG["kanban"]
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setenv("HERMES_PROFILE", "orchestrator")
    monkeypatch.setenv("HERMES_SESSION_ID", "trusted-session")
    (Path(os.environ["HERMES_HOME"]) / "config.yaml").write_text(
        "kanban:\n  architecture_gate:\n    version: v1\n    mode: orchestrator_only\n"
    )
    assert kt._managed_architecture_gate_mode() == "off"
    assert kt._managed_architecture_gate_policy() == ("off", True)
    policy_path = _write_architecture_gate_policy(os.environ["HERMES_HOME"])
    assert kt._managed_architecture_gate_mode() == "off"
    args = {
        "title": "Design the workflow",
        "assignee": "architect",
        "request_scope_id": "forged-model-scope",
    }

    policy_path.write_text(json.dumps({"version": "v1", "mode": "off"}))
    off = json.loads(kt._handle_create(args, turn_id="trusted-turn-off"))
    assert off["ok"]
    with kb.connect() as conn:
        assert kb.get_architecture_gate_for_task(conn, off["task_id"]) is None

    policy_path.write_text(json.dumps({"version": "v1", "mode": "shadow"}))
    shadow_first = json.loads(kt._handle_create(args, turn_id="trusted-turn-shadow"))
    shadow_retry = json.loads(kt._handle_create(args, turn_id="trusted-turn-shadow"))
    shadow_next_turn = json.loads(kt._handle_create(args, turn_id="trusted-turn-shadow-next"))
    assert shadow_first["task_id"] == shadow_retry["task_id"]
    assert shadow_next_turn["task_id"] != shadow_first["task_id"]
    with kb.connect() as conn:
        shadow_gate = kb.get_architecture_gate_for_task(conn, shadow_first["task_id"])
        assert shadow_gate is not None
        assert shadow_gate.enforcement_mode == "shadow"
        assert shadow_gate.request_scope_id == "front-door:trusted-turn-shadow"
        assert shadow_gate.request_scope_id != args["request_scope_id"]

    policy_path.write_text(json.dumps({"version": "v1", "mode": "orchestrator_only"}))
    enforcing = json.loads(kt._handle_create(args, turn_id="trusted-turn-enforcing"))
    with kb.connect() as conn:
        enforcing_gate = kb.get_architecture_gate_for_task(conn, enforcing["task_id"])
        assert enforcing_gate is not None
        assert enforcing_gate.enforcement_mode == "orchestrator_only"

    policy_path.write_text(json.dumps({"version": "v1", "mode": "enforce"}))
    legacy = json.loads(kt._handle_create(args, turn_id="trusted-turn-legacy"))
    policy_path.write_text("not valid json")
    malformed = json.loads(kt._handle_create(args, turn_id="trusted-turn-malformed"))
    with kb.connect() as conn:
        assert kb.get_architecture_gate_for_task(conn, legacy["task_id"]) is None
        assert kb.get_architecture_gate_for_task(conn, malformed["task_id"]) is None


def test_create_rejects_no_title(worker_env):
    from tools import kanban_tools as kt
    assert json.loads(kt._handle_create({"assignee": "x"})).get("error")
    assert json.loads(kt._handle_create({"title": "   ", "assignee": "x"})).get("error")


def test_create_rejects_no_assignee(worker_env):
    from tools import kanban_tools as kt
    assert json.loads(kt._handle_create({"title": "t"})).get("error")


def test_create_rejects_non_list_parents(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_create({"title": "t", "assignee": "a", "parents": 42})
    assert json.loads(out).get("error")


def test_create_parses_triage_string_false(worker_env):
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    out = kt._handle_create({
        "title": "not triage",
        "assignee": "peer",
        "triage": "false",
    })
    d = json.loads(out)
    assert d["ok"] is True
    conn = kb.connect()
    try:
        task = kb.get_task(conn, d["task_id"])
        assert task.status == "ready"
    finally:
        conn.close()


def test_create_parses_triage_string_true(worker_env):
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    out = kt._handle_create({
        "title": "needs triage",
        "assignee": "peer",
        "triage": "true",
    })
    d = json.loads(out)
    assert d["ok"] is True
    conn = kb.connect()
    try:
        task = kb.get_task(conn, d["task_id"])
        assert task.status == "triage"
    finally:
        conn.close()


def test_create_rejects_bad_triage(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_create({
        "title": "bad triage",
        "assignee": "peer",
        "triage": "sometimes",
    })
    assert "triage must be" in json.loads(out).get("error", "")


def test_create_accepts_string_parent(worker_env):
    """Convenience: a single parent id as string is coerced to [id]."""
    from tools import kanban_tools as kt
    out = kt._handle_create({
        "title": "t", "assignee": "a", "parents": worker_env,
    })
    assert json.loads(out)["ok"]


def test_create_accepts_skills_list(worker_env):
    """Tool writes the per-task skills through to the kernel."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    out = kt._handle_create({
        "title": "skilled",
        "assignee": "linguist",
        "skills": ["translation", "github-code-review"],
    })
    d = json.loads(out)
    assert d["ok"] is True
    with kb.connect() as conn:
        task = kb.get_task(conn, d["task_id"])
    assert task.skills == ["translation", "github-code-review"]


def test_create_accepts_skills_string(worker_env):
    """Convenience: a single skill name as string is coerced to [name]."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    out = kt._handle_create({
        "title": "one-skill",
        "assignee": "a",
        "skills": "translation",
    })
    d = json.loads(out)
    assert d["ok"] is True
    with kb.connect() as conn:
        task = kb.get_task(conn, d["task_id"])
    assert task.skills == ["translation"]


def test_create_rejects_non_list_skills(worker_env):
    """skills: 42 must be rejected, not silently dropped."""
    from tools import kanban_tools as kt
    out = kt._handle_create({
        "title": "t", "assignee": "a", "skills": 42,
    })
    assert json.loads(out).get("error")


def test_link_happy_path(worker_env):
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        a = kb.create_task(conn, title="A", assignee="x")
        b = kb.create_task(conn, title="B", assignee="x")
    finally:
        conn.close()
    from tools import kanban_tools as kt
    out = kt._handle_link({"parent_id": a, "child_id": b})
    d = json.loads(out)
    assert d["ok"] is True


def test_link_rejects_self_reference(worker_env):
    from tools import kanban_tools as kt
    out = kt._handle_link({"parent_id": worker_env, "child_id": worker_env})
    assert json.loads(out).get("error")


def test_link_rejects_missing_args(worker_env):
    from tools import kanban_tools as kt
    assert json.loads(kt._handle_link({"parent_id": "x"})).get("error")
    assert json.loads(kt._handle_link({"child_id": "y"})).get("error")


def test_link_rejects_cycle(worker_env):
    """A → B, then try to link B → A."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        a = kb.create_task(conn, title="A", assignee="x")
        b = kb.create_task(conn, title="B", assignee="x", parents=[a])
    finally:
        conn.close()
    from tools import kanban_tools as kt
    out = kt._handle_link({"parent_id": b, "child_id": a})
    assert json.loads(out).get("error")


def test_unblock_happy_path(monkeypatch, worker_env):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="blocked", assignee="worker")
        kb.block_task(conn, tid, reason="waiting")
    finally:
        conn.close()

    from tools import kanban_tools as kt
    out = kt._handle_unblock({"task_id": tid})
    d = json.loads(out)
    assert d["ok"] is True
    assert d["status"] == "ready"

    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid).status == "ready"
    finally:
        conn.close()


def test_unblock_rejects_non_blocked_task(monkeypatch, worker_env):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from tools import kanban_tools as kt
    out = kt._handle_unblock({"task_id": worker_env})
    assert json.loads(out).get("error")


def test_worker_lifecycle_through_tools(worker_env):
    """Drive the full claim -> heartbeat -> comment -> complete lifecycle
    exclusively through the tools, then verify the DB state matches what
    the dispatcher/notifier expect."""
    from tools import kanban_tools as kt

    # 1. show — worker orientation
    show = json.loads(kt._handle_show({}))
    assert show["task"]["id"] == worker_env

    # 2. heartbeat during long op
    assert json.loads(kt._handle_heartbeat({"note": "warming up"}))["ok"]

    # 3. comment for a future peer
    assert json.loads(kt._handle_comment({
        "task_id": worker_env,
        "body": "note: using stdlib sqlite3 bindings",
    }))["ok"]

    # 4. spawn a child task for follow-up
    child_out = json.loads(kt._handle_create({
        "title": "write integration test",
        "assignee": "qa",
        "parents": [worker_env],
    }))
    assert child_out["ok"]

    # 5. complete with structured handoff
    comp = json.loads(kt._handle_complete({
        "summary": "implemented + spawned QA follow-up",
        "metadata": {"child_task": child_out["task_id"]},
    }))
    assert comp["ok"]

    # Verify final state
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        parent = kb.get_task(conn, worker_env)
        assert parent.status == "done"
        assert parent.current_run_id is None
        run = kb.latest_run(conn, worker_env)
        assert run.outcome == "completed"
        assert run.metadata == {"child_task": child_out["task_id"]}
        # Child is todo (parent just finished, but recompute_ready may
        # have promoted it — complete_task runs recompute internally).
        child = kb.get_task(conn, child_out["task_id"])
        assert child.status == "ready", (
            f"child should be ready after parent done, got {child.status}"
        )
        # Comment is visible
        assert len(kb.list_comments(conn, worker_env)) == 1
        # Heartbeat event recorded
        hb = [e for e in kb.list_events(conn, worker_env) if e.kind == "heartbeat"]
        assert len(hb) == 1
    finally:
        conn.close()



def test_complete_stamps_model_used_on_run_and_completed_event(monkeypatch, worker_env):
    monkeypatch.setenv("HERMES_PROVIDER", "openai-codex")
    monkeypatch.setenv("HERMES_MODEL", "gpt-5.5")
    monkeypatch.setenv("HERMES_REASONING_EFFORT", "xhigh")
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb

    with kb.connect() as conn:
        run_id = kb.get_task(conn, worker_env).current_run_id
        assert kb.record_runtime_observation(conn, worker_env, run_id, {
            "version": 1,
            "phase": "switch",
            "provider": "openai-codex",
            "model": "gpt-5.5",
            "reasoning_effort": "xhigh",
            "runtime": "hermes",
            "api_mode": "codex_responses",
        })

    comp = json.loads(kt._handle_complete({
        "summary": "done with model metadata",
        "metadata": {"tests_run": 1},
    }))
    assert comp["ok"]

    with kb.connect() as conn:
        run = kb.latest_run(conn, worker_env)
        completed = [e for e in kb.list_events(conn, worker_env) if e.kind == "completed"][-1]

    assert run.metadata["tests_run"] == 1
    assert run.metadata["model_used"] == {
        "provider": "openai-codex",
        "model": "gpt-5.5",
        "reasoning_effort": "xhigh",
    }
    assert completed.payload["model_used"] == run.metadata["model_used"]


def test_complete_logs_route_completed_telemetry_without_summary(monkeypatch, worker_env):
    monkeypatch.setenv("HERMES_ORCHESTRATION_TELEMETRY", "1")
    monkeypatch.setenv("HERMES_PROVIDER", "nous")
    monkeypatch.setenv("HERMES_MODEL", "deepseek/deepseek-v4-flash:free")
    monkeypatch.setenv("HERMES_REASONING_EFFORT", "low")
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb

    with kb.connect() as conn:
        run_id = kb.get_task(conn, worker_env).current_run_id
        assert kb.record_runtime_observation(conn, worker_env, run_id, {
            "version": 1,
            "phase": "switch",
            "provider": "nous",
            "model": "deepseek/deepseek-v4-flash:free",
            "reasoning_effort": "low",
            "runtime": "hermes",
            "api_mode": "chat_completions",
        })

    comp = json.loads(kt._handle_complete({
        "summary": "secret-ish implementation details should not be logged",
    }))
    assert comp["ok"]

    from orchestration_telemetry import read_events, telemetry_path
    events = read_events(path=telemetry_path())
    event = events[-1]
    event_text = json.dumps(event, sort_keys=True)
    assert event["event_type"] == "route.completed"
    assert event["surface"] == "kanban_complete"
    assert event["route"]["model"] == "deepseek/deepseek-v4-flash:free"
    assert event["route"]["provider"] == "nous"
    assert "secret-ish" not in event_text
    assert event["privacy"]["content_fields_excluded"] == ["metadata", "result", "summary"]


# ---------------------------------------------------------------------------
# System-prompt guidance injection
# ---------------------------------------------------------------------------

def test_kanban_guidance_not_in_normal_prompt(monkeypatch, tmp_path):
    """A normal chat session (no HERMES_KANBAN_TASK) must NOT have
    KANBAN_GUIDANCE in its system prompt."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from pathlib import Path as _P
    monkeypatch.setattr(_P, "home", lambda: tmp_path)

    from tools.registry import invalidate_check_fn_cache
    from model_tools import _clear_tool_defs_cache
    invalidate_check_fn_cache()
    _clear_tool_defs_cache()

    from run_agent import AIAgent
    a = AIAgent(
        api_key="test",
        base_url="https://openrouter.ai/api/v1",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    prompt = a._build_system_prompt()
    assert "You are a Kanban worker" not in prompt
    assert "kanban_show()" not in prompt


def test_kanban_guidance_in_worker_prompt(monkeypatch, tmp_path):
    """A worker session (HERMES_KANBAN_TASK set) MUST have the full
    lifecycle guidance in its system prompt."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_fake")
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from pathlib import Path as _P
    monkeypatch.setattr(_P, "home", lambda: tmp_path)

    from tools.registry import invalidate_check_fn_cache
    from model_tools import _clear_tool_defs_cache
    invalidate_check_fn_cache()
    _clear_tool_defs_cache()

    from run_agent import AIAgent
    a = AIAgent(
        api_key="test",
        base_url="https://openrouter.ai/api/v1",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    prompt = a._build_system_prompt()
    # Header phrase (identity-free — SOUL.md owns identity, layer 3 is protocol)
    assert "Kanban task execution protocol" in prompt
    # Lifecycle signals
    assert "kanban_show()" in prompt
    assert "kanban_complete" in prompt
    assert "kanban_block" in prompt
    assert "kanban_create" in prompt
    # Anti-shell guidance
    assert "Do not shell out" in prompt or "tools — they work" in prompt


def test_kanban_guidance_prompt_size_bounded(monkeypatch, tmp_path):
    """Sanity: the guidance block stays lean so it doesn't blow up the
    cached prompt.

    The ceiling guards against unbounded growth, not against any growth.
    The block absorbed the load-bearing worker/orchestrator reference
    details (workspace kinds, deliverable artifacts, created-card claims,
    profile discovery) when the standalone kanban-worker / kanban-orchestrator
    skills were removed and folded into this always-injected guidance, so the
    ceiling is sized to fit that content with a little headroom.
    """
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_fake")
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from pathlib import Path as _P
    monkeypatch.setattr(_P, "home", lambda: tmp_path)

    from agent.prompt_builder import KANBAN_GUIDANCE
    assert 1_500 < len(KANBAN_GUIDANCE) < 5_500, (
        f"KANBAN_GUIDANCE is {len(KANBAN_GUIDANCE)} chars — too short (missing?) or too long"
    )


# ---------------------------------------------------------------------------
# Worker task-ownership enforcement (regression tests for #19534)
# ---------------------------------------------------------------------------
#
# A worker process has HERMES_KANBAN_TASK set to its own task id. The
# destructive tools (kanban_complete, kanban_block, kanban_heartbeat,
# kanban_unblock) must refuse to operate
# on any OTHER task id, even if the caller supplies an explicit `task_id`
# argument. Workers legitimately call kanban_show / kanban_list /
# kanban_comment / kanban_create / kanban_link on other tasks, so those
# are unrestricted.
#
# Orchestrator profiles (no HERMES_KANBAN_TASK in env) are intentionally
# exempt — their job is routing, and they sometimes close out child
# tasks on behalf of the child.


def test_worker_complete_rejects_foreign_task_id(worker_env):
    """A worker cannot complete a task that isn't its own (#19534)."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        other = kb.create_task(conn, title="sibling")
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (other,))
        conn.commit()
    finally:
        conn.close()

    from tools import kanban_tools as kt
    out = kt._handle_complete({"task_id": other, "summary": "HIJACK"})
    d = json.loads(out)
    assert d.get("ok") is not True
    assert "refusing to mutate" in d.get("error", "")

    # Sibling task must be untouched.
    conn = kb.connect()
    try:
        assert kb.get_task(conn, other).status == "ready"
    finally:
        conn.close()


def test_worker_block_rejects_foreign_task_id(worker_env):
    """A worker cannot block a task that isn't its own (#19534)."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        other = kb.create_task(conn, title="sibling")
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (other,))
        conn.commit()
    finally:
        conn.close()

    from tools import kanban_tools as kt
    out = kt._handle_block({"task_id": other, "reason": "evil"})
    d = json.loads(out)
    assert "refusing to mutate" in d.get("error", "")

    conn = kb.connect()
    try:
        assert kb.get_task(conn, other).status == "ready"
    finally:
        conn.close()


def test_worker_heartbeat_rejects_foreign_task_id(worker_env):
    """A worker cannot heartbeat a task that isn't its own (#19534)."""
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        other = kb.create_task(conn, title="sibling")
        # Put sibling in running state so heartbeat would otherwise succeed.
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (other,))
        conn.commit()
    finally:
        conn.close()

    from tools import kanban_tools as kt
    out = kt._handle_heartbeat({"task_id": other})
    d = json.loads(out)
    assert "refusing to mutate" in d.get("error", "")


def test_worker_can_comment_on_foreign_task(worker_env):
    """Cross-task commenting must remain unrestricted (#19713 policy).

    The author-forgery hardening removed args['author'] but deliberately
    did NOT add an ownership gate to kanban_comment — comments are the
    documented handoff channel between tasks. This test pins that policy
    so a future change accidentally adding ``_enforce_worker_task_ownership``
    to ``_handle_comment`` would fail CI immediately.
    """
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        other = kb.create_task(conn, title="sibling")
    finally:
        conn.close()

    from tools import kanban_tools as kt
    out = kt._handle_comment({
        "task_id": other,
        "body": "handoff: see prior findings before starting",
    })
    d = json.loads(out)
    assert d.get("ok") is True, f"cross-task comment must succeed: {d}"

    # The comment lands on the foreign task, attributed to the worker's
    # HERMES_PROFILE — never to a caller-controlled string.
    conn = kb.connect()
    try:
        comments = kb.list_comments(conn, other)
        assert len(comments) == 1
        assert comments[0].author == "test-worker"
        assert comments[0].body.startswith("handoff:")
    finally:
        conn.close()


def test_worker_unblock_rejects_foreign_task_id(worker_env):
    """A worker cannot unblock any task — kanban_unblock is orchestrator-only.

    The check fires before the per-task ownership check, so the error
    surface is the orchestrator-only refusal rather than the
    cross-task-ownership refusal. Either is fine — the property we're
    pinning is "worker cannot mutate foreign task via kanban_unblock".
    """
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        other = kb.create_task(conn, title="blocked sibling", assignee="peer")
        kb.block_task(conn, other, reason="waiting")
    finally:
        conn.close()

    from tools import kanban_tools as kt
    out = kt._handle_unblock({"task_id": other})
    d = json.loads(out)
    err = d.get("error", "")
    assert "orchestrator-only" in err or "refusing to mutate" in err, (
        f"expected worker-rejection error, got {err}"
    )

    conn = kb.connect()
    try:
        assert kb.get_task(conn, other).status == "blocked"
    finally:
        conn.close()


def test_worker_complete_own_task_still_works(worker_env):
    """The ownership check doesn't break the normal own-task happy path."""
    from tools import kanban_tools as kt
    # Both implicit (no task_id arg) and explicit (matching env) must work.
    out = kt._handle_complete({"task_id": worker_env, "summary": "explicit own"})
    d = json.loads(out)
    assert d.get("ok") is True and d.get("task_id") == worker_env


def test_worker_complete_rejects_stale_run_id(worker_env, monkeypatch):
    """A retried worker cannot complete the task using an old run token."""
    from hermes_cli import kanban_db as kb
    import hermes_cli.kanban_db as _kb

    # detect_crashed_workers now gates each running task behind a
    # launch-window grace period (c002668ff) so a freshly-spawned worker
    # whose PID isn't yet visible on /proc isn't reclaimed. The fixture
    # creates the task moments before this assertion, so the grace
    # period (default 30s) would skip the liveness check. Zero it out
    # for this test — we WANT immediate reclamation here.
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")

    conn = kb.connect()
    try:
        run1 = kb.latest_run(conn, worker_env)
        kb._set_worker_pid(conn, worker_env, 98765)
        monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
        monkeypatch.setattr(_kb, "_pid_alive", lambda pid: False)
        assert kb.detect_crashed_workers(conn) == [worker_env]

        kb.claim_task(conn, worker_env)
        run2 = kb.latest_run(conn, worker_env)
        assert run2.id != run1.id
        assert kb.record_runtime_observation(conn, worker_env, run2.id, {
            "version": 1,
            "phase": "initial",
            "provider": "",
            "model": "",
            "reasoning_effort": "",
            "runtime": "hermes",
            "api_mode": "chat_completions",
        })
    finally:
        conn.close()

    from tools import kanban_tools as kt
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run1.id))
    out = kt._handle_complete({"summary": "late stale completion"})
    d = json.loads(out)
    assert d.get("ok") is not True

    conn = kb.connect()
    try:
        task = kb.get_task(conn, worker_env)
        assert task.status == "running"
        assert task.current_run_id == run2.id
    finally:
        conn.close()

    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run2.id))
    out = kt._handle_complete({"summary": "current completion"})
    d = json.loads(out)
    assert d.get("ok") is True


def test_orchestrator_complete_any_task_allowed(monkeypatch, tmp_path):
    """Orchestrator profiles (no HERMES_KANBAN_TASK) can still complete
    any task via explicit task_id. The check only applies to workers."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from pathlib import Path as _P
    monkeypatch.setattr(_P, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="child to close out")
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        conn.commit()
    finally:
        conn.close()

    from tools import kanban_tools as kt
    out = kt._handle_complete({"task_id": tid, "summary": "orchestrator close"})
    d = json.loads(out)
    assert d.get("ok") is True and d.get("task_id") == tid
    assert not isinstance(out, kt.KanbanTerminalControl)


# ---------------------------------------------------------------------------
# Optional ``board`` parameter — per-call DB override
# ---------------------------------------------------------------------------
#
# The dispatcher pins the active board via HERMES_KANBAN_BOARD env var,
# but a Telegram-side orchestrator handling multiple boards needs to be
# able to route a single tool call to a specific board's DB without
# restarting Hermes. These tests pin that ``board=<slug>`` argument
# routes each handler to that board's sqlite file, and that omitting
# ``board`` preserves the legacy env-driven resolution.


@pytest.fixture
def multi_board_env(monkeypatch, tmp_path):
    """Isolated Hermes home with two distinct kanban boards seeded.

    Returns ``("default", "alt")`` slugs. The default board has one
    pre-existing task ``seed_default``; ``alt`` has ``seed_alt``. No
    HERMES_KANBAN_TASK is pinned (orchestrator context) — workers test
    the env-task case via the existing ``worker_env`` fixture.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    # Make sure neither HERMES_KANBAN_DB nor HERMES_KANBAN_BOARD pin a
    # board — the test is specifically about the per-call override.
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setenv("HERMES_PROFILE", "test-orchestrator")
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    # Default board — implicit
    conn = kb.connect()
    try:
        seed_default = kb.create_task(
            conn, title="seed-default", assignee="worker-d"
        )
    finally:
        conn.close()
    # Alt board — explicit slug routes the connection to a separate DB
    conn = kb.connect(board="alt")
    try:
        seed_alt = kb.create_task(
            conn, title="seed-alt", assignee="worker-a"
        )
    finally:
        conn.close()
    return {
        "default_seed": seed_default,
        "alt_seed": seed_alt,
        "default_db": kb.kanban_db_path(),
        "alt_db": kb.kanban_db_path(board="alt"),
    }


def test_board_param_routes_create_to_alt_board(multi_board_env):
    """kanban_create with ``board="alt"`` must write into the alt board's DB,
    not the default one."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    out = kt._handle_create({
        "title": "alt-only",
        "assignee": "worker",
        "board": "alt",
    })
    d = json.loads(out)
    assert d["ok"] is True, d
    new_tid = d["task_id"]

    # Lands on alt board.
    with kb.connect(board="alt") as conn:
        assert kb.get_task(conn, new_tid).title == "alt-only"
    # Does NOT land on default board.
    with kb.connect() as conn:
        assert kb.get_task(conn, new_tid) is None


def test_board_param_routes_list_to_alt_board(multi_board_env):
    """kanban_list filters by the board parameter, not env-active."""
    from tools import kanban_tools as kt

    # Default — sees seed-default, not seed-alt.
    default_out = json.loads(kt._handle_list({}))
    default_titles = {t["title"] for t in default_out["tasks"]}
    assert "seed-default" in default_titles
    assert "seed-alt" not in default_titles

    # Alt — sees seed-alt, not seed-default.
    alt_out = json.loads(kt._handle_list({"board": "alt"}))
    alt_titles = {t["title"] for t in alt_out["tasks"]}
    assert "seed-alt" in alt_titles
    assert "seed-default" not in alt_titles


def test_board_param_routes_show_to_alt_board(multi_board_env):
    """kanban_show reads from the board parameter, not env-active.

    Tasks across boards may share ids (the id space is per-DB) but the
    seed task ids in this fixture are distinct, so a cross-board show
    must return the matching task only when board is correct.
    """
    from tools import kanban_tools as kt

    alt_seed = multi_board_env["alt_seed"]
    # Without board override, the alt task is invisible.
    bad = json.loads(kt._handle_show({"task_id": alt_seed}))
    assert "not found" in bad.get("error", "")

    # With board override, it's readable.
    good = json.loads(kt._handle_show({"task_id": alt_seed, "board": "alt"}))
    assert good["task"]["id"] == alt_seed
    assert good["task"]["title"] == "seed-alt"


def test_board_param_routes_assign_via_create_to_alt(multi_board_env):
    """Workflow test for the 'assign' UX — create with assignee on a
    specific board. (The CLI has a separate ``kanban assign`` verb; the
    MCP surface assigns at task creation time.)"""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    out = kt._handle_create({
        "title": "alt-assigned",
        "assignee": "linguist",
        "board": "alt",
    })
    d = json.loads(out)
    assert d["ok"] is True
    with kb.connect(board="alt") as conn:
        task = kb.get_task(conn, d["task_id"])
        assert task is not None
        assert task.assignee == "linguist"


def test_board_param_routes_comment_to_alt_board(multi_board_env):
    """kanban_comment routes the insert to the alt board's DB."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    alt_seed = multi_board_env["alt_seed"]
    out = kt._handle_comment({
        "task_id": alt_seed,
        "body": "alt comment",
        "board": "alt",
    })
    d = json.loads(out)
    assert d["ok"] is True

    with kb.connect(board="alt") as conn:
        comments = kb.list_comments(conn, alt_seed)
        assert len(comments) == 1
        assert comments[0].body == "alt comment"
    # Default board does not have this task at all, so no rogue comment.
    with kb.connect() as conn:
        assert kb.get_task(conn, alt_seed) is None


def test_board_param_routes_complete_to_alt_board(multi_board_env):
    """kanban_complete on the alt board closes the alt task, leaving
    the default seed untouched."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    alt_seed = multi_board_env["alt_seed"]
    # Make alt task running so complete is valid.
    with kb.connect(board="alt") as conn:
        kb.claim_task(conn, alt_seed)

    out = kt._handle_complete({
        "task_id": alt_seed,
        "summary": "alt close",
        "board": "alt",
    })
    d = json.loads(out)
    assert d["ok"] is True

    with kb.connect(board="alt") as conn:
        assert kb.get_task(conn, alt_seed).status == "done"
    # Default seed is unchanged.
    with kb.connect() as conn:
        default_seed = multi_board_env["default_seed"]
        assert kb.get_task(conn, default_seed).status == "ready"


def test_board_param_routes_block_to_alt_board(multi_board_env):
    """kanban_block targets the alt board's DB."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    alt_seed = multi_board_env["alt_seed"]
    with kb.connect(board="alt") as conn:
        kb.claim_task(conn, alt_seed)

    out = kt._handle_block({
        "task_id": alt_seed,
        "reason": "need input on alt board",
        "board": "alt",
    })
    d = json.loads(out)
    assert d["ok"] is True

    with kb.connect(board="alt") as conn:
        assert kb.get_task(conn, alt_seed).status == "blocked"


def test_board_param_routes_unblock_to_alt_board(multi_board_env):
    """kanban_unblock targets the alt board's DB."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    alt_seed = multi_board_env["alt_seed"]
    with kb.connect(board="alt") as conn:
        kb.block_task(conn, alt_seed, reason="waiting")
        assert kb.get_task(conn, alt_seed).status == "blocked"

    out = kt._handle_unblock({"task_id": alt_seed, "board": "alt"})
    d = json.loads(out)
    assert d["ok"] is True
    assert d["status"] == "ready"

    with kb.connect(board="alt") as conn:
        assert kb.get_task(conn, alt_seed).status == "ready"


def test_board_param_routes_heartbeat_to_alt_board(monkeypatch, tmp_path):
    """kanban_heartbeat targets the alt board's DB. Worker-scoped, so we
    use the worker-env style fixture inline (pinning HERMES_KANBAN_TASK
    to a task that exists in the alt board)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "alt-worker")
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    # Seed the alt board with a claimed task.
    with kb.connect(board="alt") as conn:
        tid = kb.create_task(conn, title="alt hb", assignee="alt-worker")
        kb.claim_task(conn, tid)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)

    from tools import kanban_tools as kt
    out = kt._handle_heartbeat({"note": "alive on alt", "board": "alt"})
    d = json.loads(out)
    assert d["ok"] is True

    # Heartbeat event landed in the alt DB.
    with kb.connect(board="alt") as conn:
        events = [e for e in kb.list_events(conn, tid) if e.kind == "heartbeat"]
        assert len(events) == 1


def test_board_param_routes_link_to_alt_board(multi_board_env):
    """kanban_link operates on the alt board's DB."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    with kb.connect(board="alt") as conn:
        a = kb.create_task(conn, title="A-alt", assignee="x")
        b = kb.create_task(conn, title="B-alt", assignee="x")

    out = kt._handle_link({
        "parent_id": a,
        "child_id": b,
        "board": "alt",
    })
    d = json.loads(out)
    assert d["ok"] is True

    with kb.connect(board="alt") as conn:
        assert b in kb.child_ids(conn, a)


def test_board_param_none_falls_back_to_env(worker_env):
    """When ``board`` is omitted or None, behaviour is unchanged from
    before this feature — calls land on whatever the env resolves to.
    Regression guard against accidentally rewiring default resolution."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    out = kt._handle_show({})  # no board, no task_id
    d = json.loads(out)
    assert d["task"]["id"] == worker_env

    out = kt._handle_show({"task_id": worker_env, "board": None})
    d = json.loads(out)
    assert d["task"]["id"] == worker_env

    # Sanity: the env-resolved path is the legacy default DB, NOT an
    # 'alt' board path. Confirms the override path was not silently
    # forced.
    assert kb.kanban_db_path() == kb.kanban_db_path(board="default")


def test_board_param_rejects_invalid_slug(multi_board_env):
    """A board slug that fails ``_normalize_board_slug`` surfaces as a
    structured tool_error rather than a 500 / unhandled exception."""
    from tools import kanban_tools as kt

    out = kt._handle_list({"board": "Has Spaces"})
    err = json.loads(out).get("error", "")
    assert "invalid board slug" in err, f"got {err!r}"


def test_block_with_summary_metadata_persists_to_db(worker_env):
    """block_task with summary and metadata must persist both on the run
    row so a retrying worker sees them in retry context."""
    from hermes_cli import kanban_db as kb

    tid = worker_env
    conn = kb.connect()
    try:
        meta = {
            "failure_code": "iteration_budget_exhausted",
            "iterations": {"used": 42, "max": 90},
            "changed_files": ["a.py", "b.py"],
            "tests_run": 3,
        }
        summary = "Did half the work before exhausting budget."
        ok = kb.block_task(
            conn, tid,
            reason="iteration exhaustion",
            summary=summary,
            metadata=meta,
        )
        assert ok is True

        # Verify the run row has the handoff data.
        run = kb.latest_run(conn, tid)
        assert run is not None
        assert run.outcome == "blocked"
        assert run.summary is not None
        assert "Did half the work" in run.summary
        assert run.metadata is not None
        # metadata is stored as JSON
        import json
        stored_meta = json.loads(run.metadata) if isinstance(run.metadata, str) else run.metadata
        assert stored_meta["failure_code"] == "iteration_budget_exhausted"
        assert stored_meta["iterations"]["used"] == 42
        assert stored_meta["changed_files"] == ["a.py", "b.py"]
        assert stored_meta["tests_run"] == 3
    finally:
        conn.close()


def test_block_schema_includes_summary_and_metadata():
    """KANBAN_BLOCK_SCHEMA must include optional summary and metadata
    properties so the LLM knows it can pass recovery handoff."""
    from tools import kanban_tools as kt

    schema = kt.KANBAN_BLOCK_SCHEMA
    props = schema["parameters"]["properties"]

    assert "summary" in props, (
        "KANBAN_BLOCK_SCHEMA must include 'summary' property"
    )
    assert props["summary"]["type"] == "string"

    assert "metadata" in props, (
        "KANBAN_BLOCK_SCHEMA must include 'metadata' property"
    )
    assert props["metadata"]["type"] == "object"

    # summary and metadata must always remain optional — never required.
    required = schema["parameters"].get("required", [])
    assert "summary" not in required
    assert "metadata" not in required
    # reason is still required.
    assert "reason" in required


def test_board_param_in_all_schemas():
    """Every kanban_* tool schema must expose an optional ``board``
    parameter. This pins the contract surfaced to the LLM — adding a
    new kanban tool without ``board`` will fail CI immediately."""
    from tools import kanban_tools as kt

    schemas = [
        kt.KANBAN_SHOW_SCHEMA,
        kt.KANBAN_LIST_SCHEMA,
        kt.KANBAN_COMPLETE_SCHEMA,
        kt.KANBAN_BLOCK_SCHEMA,
        kt.KANBAN_HEARTBEAT_SCHEMA,
        kt.KANBAN_COMMENT_SCHEMA,
        kt.KANBAN_CREATE_SCHEMA,
        kt.KANBAN_UNBLOCK_SCHEMA,
        kt.KANBAN_LINK_SCHEMA,
        kt.KANBAN_ATTACH_SCHEMA,
        kt.KANBAN_ATTACH_URL_SCHEMA,
        kt.KANBAN_ATTACHMENTS_SCHEMA,
    ]
    for schema in schemas:
        props = schema["parameters"]["properties"]
        assert "board" in props, (
            f"{schema['name']} is missing the 'board' property"
        )
        assert props["board"]["type"] == "string"
        # board is optional everywhere — never in required.
        assert "board" not in schema["parameters"].get("required", []), (
            f"{schema['name']} marks board as required; must be optional"
        )


# ---------------------------------------------------------------------------
# kanban_create auto-subscribe behaviour
#
# When a worker calls kanban_create from inside a session that has a
# persistent delivery channel, the originating session should be
# subscribed to the new task's completion/block events automatically.
# - Gateway sessions: HERMES_SESSION_PLATFORM + HERMES_SESSION_CHAT_ID set.
# - TUI sessions: HERMES_SESSION_KEY (or HERMES_SESSION_ID) set, with
#   the platform/chat_id ContextVars intentionally empty.
# - CLI / cron / test sessions: no delivery channel -> no subscription.
# - Config gate kanban.auto_subscribe_on_create: false -> no subscription
#   even when the session has a delivery channel.
# ---------------------------------------------------------------------------

def _list_subs_for_task(task_id):
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        return list(kb.list_notify_subs(conn, task_id))
    finally:
        conn.close()


def _sub_index(subs):
    """Normalise a list of notify-subs (dicts or objects) into dicts
    keyed by platform+chat_id, so assertions work regardless of the
    return shape."""
    out = []
    for s in subs:
        if isinstance(s, dict):
            out.append(s)
        else:
            out.append({
                "platform": getattr(s, "platform", None),
                "chat_id": getattr(s, "chat_id", None),
                "thread_id": getattr(s, "thread_id", None),
                "user_id": getattr(s, "user_id", None),
            })
    return out


def test_create_subscribes_gateway_session(monkeypatch, worker_env):
    """A gateway session (platform + chat_id set) gets auto-subscribed
    to its own kanban_create result, and the response surfaces the
    ``subscribed`` flag so the orchestrator can react."""
    from tools import kanban_tools as kt
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "chat-42")
    monkeypatch.setenv("HERMES_SESSION_THREAD_ID", "thread-7")
    monkeypatch.setenv("HERMES_SESSION_USER_ID", "user-9")

    out = kt._handle_create({
        "title": "auto-sub gateway",
        "assignee": "peer",
    })
    d = json.loads(out)
    assert d["ok"] is True
    new_tid = d["task_id"]
    assert d["subscribed"] is True, d

    subs = _sub_index(_list_subs_for_task(new_tid))
    assert len(subs) == 1
    s = subs[0]
    assert s["platform"] == "telegram"
    assert s["chat_id"] == "chat-42"
    assert s["thread_id"] == "thread-7"
    assert s["user_id"] == "user-9"


def test_create_subscribes_tui_session_via_session_key(monkeypatch, worker_env):
    """TUI / desktop sessions don't have a platform/chat_id (single
    local channel), but the parent process exports HERMES_SESSION_KEY.
    We should still auto-subscribe, with platform='tui' and
    chat_id=<key>."""
    from tools import kanban_tools as kt
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    monkeypatch.delenv("HERMES_SESSION_CHAT_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_THREAD_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_USER_ID", raising=False)
    monkeypatch.setenv("HERMES_SESSION_KEY", "tui-session-abc")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)

    out = kt._handle_create({
        "title": "auto-sub tui",
        "assignee": "peer",
    })
    d = json.loads(out)
    assert d["ok"] is True
    new_tid = d["task_id"]
    assert d["subscribed"] is True, d

    subs = _sub_index(_list_subs_for_task(new_tid))
    assert len(subs) == 1
    assert subs[0]["platform"] == "tui"
    assert subs[0]["chat_id"] == "tui-session-abc"


def test_create_tui_origin_suppresses_enabled_home_fallback(monkeypatch, worker_env):
    """One originating TUI subscription wins over configured home copies."""
    from tools import kanban_tools as kt

    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    monkeypatch.delenv("HERMES_SESSION_CHAT_ID", raising=False)
    monkeypatch.setenv("HERMES_SESSION_KEY", "tui-session-primary")
    monkeypatch.setattr(
        kt,
        "load_config",
        lambda: {
            "kanban": {
                "auto_subscribe_on_create": True,
                "auto_subscribe_home_on_create": True,
                "auto_subscribe_home_platforms": ["telegram"],
            }
        },
    )
    monkeypatch.setattr(
        kt,
        "_configured_home_channels",
        lambda: [{"platform": "telegram", "chat_id": "home-chat", "thread_id": ""}],
    )

    result = json.loads(kt._handle_create({"title": "one origin", "assignee": "peer"}))
    subs = _sub_index(_list_subs_for_task(result["task_id"]))

    assert [(sub["platform"], sub["chat_id"]) for sub in subs] == [
        ("tui", "tui-session-primary")
    ]


def test_create_does_not_subscribe_in_cli_session(monkeypatch, worker_env):
    """CLI / cron / test sessions have no persistent delivery channel.
    _maybe_auto_subscribe returns False and no row is written."""
    from tools import kanban_tools as kt
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    monkeypatch.delenv("HERMES_SESSION_CHAT_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)

    out = kt._handle_create({
        "title": "no sub cli",
        "assignee": "peer",
    })
    d = json.loads(out)
    assert d["ok"] is True
    assert d["subscribed"] is False, d

    assert _list_subs_for_task(d["task_id"]) == []


def test_create_respects_auto_subscribe_on_create_false(monkeypatch, worker_env, tmp_path):
    """The config gate kanban.auto_subscribe_on_create=false must
    suppress auto-subscription even when the session has a delivery
    channel. This is the knob that addresses the upstream design
    concern from PR #19718 (reverted in #19721) — users who want
    explicit kanban_notify-subscribe calls per task get that."""
    # worker_env already created <tmp>/.hermes; use a fresh sibling
    # home to avoid mkdir() colliding with the worker's directory.
    home = tmp_path / "gate-home" / ".hermes"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(
        "kanban:\n  auto_subscribe_on_create: false\n"
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "discord")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "channel-1")

    from tools import kanban_tools as kt
    out = kt._handle_create({
        "title": "no sub gated",
        "assignee": "peer",
    })
    d = json.loads(out)
    assert d["ok"] is True
    assert d["subscribed"] is False, d

    assert _list_subs_for_task(d["task_id"]) == []


def test_create_partial_session_context_no_subscribe(monkeypatch, worker_env):
    """Only one of (platform, chat_id) set -> no implicit subscribe.
    Either both are set (gateway) or neither (TUI / CLI); partial is
    ambiguous and the safe default is to skip."""
    from tools import kanban_tools as kt
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "slack")
    monkeypatch.delenv("HERMES_SESSION_CHAT_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)

    out = kt._handle_create({
        "title": "no sub partial",
        "assignee": "peer",
    })
    d = json.loads(out)
    assert d["ok"] is True
    assert d["subscribed"] is False, d


def test_maybe_auto_subscribe_swallows_add_notify_sub_failure(monkeypatch, worker_env):
    """If add_notify_sub itself raises (e.g. DB locked, schema drift),
    _maybe_auto_subscribe must NOT bubble that up and fail the parent
    kanban_create. The function returns False and the parent create
    still succeeds with subscribed=False."""
    from tools import kanban_tools as kt
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "chat-42")

    from hermes_cli import kanban_db as kb

    def _boom(*a, **kw):
        raise RuntimeError("simulated DB failure")

    monkeypatch.setattr(kb, "add_notify_sub", _boom)

    out = kt._handle_create({
        "title": "auto-sub tolerates add_notify_sub failure",
        "assignee": "peer",
    })
    d = json.loads(out)
    assert d["ok"] is True, d
    assert d["subscribed"] is False, d


# ---------------------------------------------------------------------------
# Attachments — kanban_attach / kanban_attach_url / kanban_attachments
# ---------------------------------------------------------------------------


@pytest.fixture
def allow_private_urls(monkeypatch):
    """Opt the SSRF guard into private/loopback targets for local fixtures.

    Mirrors a user setting HERMES_ALLOW_PRIVATE_URLS on a private network.
    Resets the url_safety process-lifetime cache on both sides so the
    override neither leaks in nor out of the test.
    """
    from tools import url_safety

    monkeypatch.setenv("HERMES_ALLOW_PRIVATE_URLS", "true")
    url_safety._reset_allow_private_cache()
    yield
    url_safety._reset_allow_private_cache()


def test_attach_roundtrips_bytes_to_row_and_disk(worker_env):
    """kanban_attach decodes base64, writes the blob, and records the row."""
    import base64
    from pathlib import Path

    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    content = b"hello attachment from a tool"
    out = kt._handle_attach({
        "filename": "notes.txt",
        "content_base64": base64.b64encode(content).decode(),
        "content_type": "text/plain",
    })
    d = json.loads(out)
    assert d.get("ok") is True, out
    assert d["size"] == len(content)
    att_id = d["attachment_id"]

    conn = kb.connect()
    try:
        atts = kb.list_attachments(conn, worker_env)
        assert [a.filename for a in atts] == ["notes.txt"]
        a = atts[0]
        assert a.id == att_id
        assert a.content_type == "text/plain"
        assert a.uploaded_by == "agent"
        # Blob is on disk under the task's attachments dir with the bytes.
        assert Path(a.stored_path).read_bytes() == content
        assert Path(a.stored_path).resolve().is_relative_to(
            kb.task_attachments_dir(worker_env).resolve()
        )
    finally:
        conn.close()


def _worker_workspace(worker_env):
    from hermes_cli import kanban_db as kb

    conn = kb.connect()
    try:
        task = kb.get_task(conn, worker_env)
        assert task is not None
        if task.workspace_path:
            workspace = Path(task.workspace_path).resolve()
        else:
            # ``worker_env`` creates a claimed task but deliberately does not
            # run the dispatcher workspace-materialization step. Exercise the
            # handler's existing board workspaces fallback explicitly.
            workspace = (kb.workspaces_root() / worker_env).resolve()
            workspace.mkdir(parents=True, exist_ok=True)
    finally:
        conn.close()
    assert not workspace.is_relative_to(_REAL_HERMES_HOME)
    return workspace


def _assert_attachment_not_under_live_home(path):
    assert not Path(path).resolve().is_relative_to(_REAL_HERMES_HOME)


def test_attach_path_roundtrips_large_file_with_verified_digest(worker_env):
    """Path attach reads a multi-MB artifact without model-side base64."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    workspace = _worker_workspace(worker_env)
    source = workspace / "large-artifact.bin"
    payload = (b"hermes-kanban-artifact\x00" * 120_000) + b"tail"
    source.write_bytes(payload)
    expected_sha256 = hashlib.sha256(payload).hexdigest()

    out = kt._handle_attach({"path": str(source)})
    result = json.loads(out)
    assert result.get("ok") is True, out
    assert result["size"] == len(payload)
    assert result["sha256"] == expected_sha256

    conn = kb.connect()
    try:
        attachments = kb.list_attachments(conn, worker_env)
        assert len(attachments) == 1
        attachment = attachments[0]
        assert attachment.size == len(payload)
        assert Path(attachment.stored_path).read_bytes() == payload
        assert hashlib.sha256(Path(attachment.stored_path).read_bytes()).hexdigest() == expected_sha256
        _assert_attachment_not_under_live_home(attachment.stored_path)
    finally:
        conn.close()


def _run_git(*args, cwd):
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def test_attach_path_git_bundle_can_be_cloned_from_stored_attachment(worker_env, tmp_path):
    """The durable artifact regression: a real bundle survives attach and clone."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    workspace = _worker_workspace(worker_env)
    repository = workspace / "repo"
    repository.mkdir()
    _run_git("init", "-q", "-b", "main", cwd=repository)
    _run_git("config", "user.email", "worker@example.test", cwd=repository)
    _run_git("config", "user.name", "Kanban Worker", cwd=repository)
    (repository / "README.md").write_text("durable bundle\n", encoding="utf-8")
    _run_git("add", "README.md", cwd=repository)
    _run_git("commit", "-q", "-m", "initial", cwd=repository)

    bundle = workspace / "repository.bundle"
    _run_git("bundle", "create", str(bundle), "--all", cwd=repository)

    result = json.loads(kt._handle_attach({"path": str(bundle)}))
    assert result.get("ok") is True, result

    conn = kb.connect()
    try:
        attachment = kb.list_attachments(conn, worker_env)[0]
        stored_path = Path(attachment.stored_path)
        _assert_attachment_not_under_live_home(stored_path)
    finally:
        conn.close()

    clone = tmp_path / "cloned-bundle"
    _run_git("clone", "-q", str(stored_path), str(clone), cwd=tmp_path)
    assert (clone / "README.md").read_text(encoding="utf-8") == "durable bundle\n"


def test_attach_path_rejects_workspace_escape_and_symlink(worker_env):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    workspace = _worker_workspace(worker_env)
    outside = workspace.parent.parent / "outside-artifact.bin"
    outside.write_bytes(b"outside")
    traversal = workspace / ".." / ".." / outside.name

    for candidate in (outside, traversal):
        result = json.loads(kt._handle_attach({"path": str(candidate)}))
        assert "error" in result, result

    link = workspace / "outside-link.bin"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this platform")
    result = json.loads(kt._handle_attach({"path": str(link)}))
    assert "error" in result, result

    conn = kb.connect()
    try:
        assert kb.list_attachments(conn, worker_env) == []
    finally:
        conn.close()


def test_attach_path_fails_closed_without_resolvable_workspace(worker_env, tmp_path):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    missing_workspace = tmp_path / "not-materialized"
    source = tmp_path / "candidate.bin"
    source.write_bytes(b"candidate")
    conn = kb.connect()
    try:
        kb.set_workspace_path(conn, worker_env, missing_workspace)
    finally:
        conn.close()

    result = json.loads(kt._handle_attach({"path": str(source)}))
    assert "error" in result
    assert "no resolvable owned workspace" in result["error"]


def test_attach_path_and_inline_payload_are_mutually_exclusive(worker_env):
    from tools import kanban_tools as kt

    source = _worker_workspace(worker_env) / "artifact.bin"
    source.write_bytes(b"artifact")
    result = json.loads(kt._handle_attach({
        "path": str(source),
        "content_base64": "YXJ0aWZhY3Q=",
    }))
    assert "error" in result
    assert "exactly one" in result["error"]


def test_attach_path_rejects_directory_fifo_and_missing_file(worker_env):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    workspace = _worker_workspace(worker_env)
    directory = workspace / "directory"
    directory.mkdir()
    missing = workspace / "missing.bin"
    fifo = workspace / "pipe"
    try:
        os.mkfifo(fifo)
    except (AttributeError, NotImplementedError, OSError):
        fifo = None

    candidates = [directory, missing]
    if fifo is not None:
        candidates.append(fifo)
    for candidate in candidates:
        result = json.loads(kt._handle_attach({"path": str(candidate)}))
        assert "error" in result, result

    conn = kb.connect()
    try:
        assert kb.list_attachments(conn, worker_env) == []
    finally:
        conn.close()


def test_attach_path_rejects_oversize_without_storing(worker_env, monkeypatch):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    monkeypatch.setattr(kb, "KANBAN_ATTACHMENT_MAX_BYTES", 1024)
    source = _worker_workspace(worker_env) / "too-large.bin"
    source.write_bytes(b"x" * 1025)

    result = json.loads(kt._handle_attach({"path": str(source)}))
    assert "error" in result
    assert "limit" in result["error"]

    conn = kb.connect()
    try:
        assert kb.list_attachments(conn, worker_env) == []
    finally:
        conn.close()


def test_attach_path_rejects_truncated_store(worker_env, monkeypatch):
    """A partial write cannot be reported as a successful attachment."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    source = _worker_workspace(worker_env) / "complete-artifact.bin"
    source.write_bytes(b"complete artifact bytes\n")
    real_store = kb.store_attachment_bytes

    def truncated_store(conn, task_id, filename, data, **kwargs):
        attachment_id = real_store(conn, task_id, filename, data, **kwargs)
        attachment = kb.get_attachment(conn, attachment_id)
        assert attachment is not None
        stored = Path(attachment.stored_path)
        stored.write_bytes(stored.read_bytes()[:-1])
        return attachment_id

    monkeypatch.setattr(kb, "store_attachment_bytes", truncated_store)
    result = json.loads(kt._handle_attach({"path": str(source)}))
    assert "error" in result
    assert "integrity" in result["error"]

    conn = kb.connect()
    try:
        assert kb.list_attachments(conn, worker_env) == []
    finally:
        conn.close()


def test_attach_rejects_oversize(worker_env, monkeypatch):
    """A decoded payload over the cap returns a clean tool error, no row."""
    import base64

    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    # Shrink the cap so we don't have to build a 25 MB payload.
    monkeypatch.setattr(kb, "KANBAN_ATTACHMENT_MAX_BYTES", 8)
    out = kt._handle_attach({
        "filename": "big.bin",
        "content_base64": base64.b64encode(b"0123456789").decode(),
    })
    d = json.loads(out)
    assert "error" in d
    assert "MB limit" in d["error"]

    conn = kb.connect()
    try:
        assert kb.list_attachments(conn, worker_env) == []
    finally:
        conn.close()


def test_attach_rejects_bad_base64(worker_env):
    from tools import kanban_tools as kt

    out = kt._handle_attach({"filename": "x.txt", "content_base64": "not base64!!!"})
    d = json.loads(out)
    assert "error" in d and "base64" in d["error"]


def test_attach_requires_filename_and_content(worker_env):
    from tools import kanban_tools as kt

    assert "error" in json.loads(kt._handle_attach({"content_base64": "QQ=="}))
    assert "error" in json.loads(kt._handle_attach({"filename": "x.txt"}))


def test_attach_enforces_worker_task_ownership(worker_env):
    """A worker scoped to its own task can't attach to a foreign task."""
    import base64

    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    conn = kb.connect()
    try:
        other = kb.create_task(conn, title="someone else's task", assignee="peer")
    finally:
        conn.close()

    out = kt._handle_attach({
        "task_id": other,
        "filename": "x.txt",
        "content_base64": base64.b64encode(b"x").decode(),
    })
    d = json.loads(out)
    assert "error" in d
    assert "scoped to task" in d["error"]


def test_attachments_lists_uploaded_files(worker_env):
    import base64

    from tools import kanban_tools as kt

    kt._handle_attach({
        "filename": "a.txt",
        "content_base64": base64.b64encode(b"aaa").decode(),
    })
    kt._handle_attach({
        "filename": "b.txt",
        "content_base64": base64.b64encode(b"bbbb").decode(),
    })
    out = kt._handle_attachments({})
    d = json.loads(out)
    assert d.get("ok") is True
    names = sorted(a["filename"] for a in d["attachments"])
    assert names == ["a.txt", "b.txt"]
    sizes = {a["filename"]: a["size"] for a in d["attachments"]}
    assert sizes == {"a.txt": 3, "b.txt": 4}


def test_attachments_unknown_task_errors(worker_env):
    from tools import kanban_tools as kt

    out = kt._handle_attachments({"task_id": "t_nope"})
    assert "error" in json.loads(out)


def test_attach_url_fetches_local_fixture(worker_env, allow_private_urls):
    """kanban_attach_url downloads from an http(s) URL and stores the bytes.

    The fixture server lives on loopback, which the SSRF guard blocks by
    default — opted in via the allow_private_urls fixture exactly like a
    user on a private network would.
    """
    import http.server
    import threading
    from pathlib import Path

    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    payload = b"downloaded-by-url body"

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):  # silence
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_address[1]
        out = kt._handle_attach_url({
            "url": f"http://127.0.0.1:{port}/files/report.bin",
        })
    finally:
        srv.shutdown()
    d = json.loads(out)
    assert d.get("ok") is True, out
    assert d["size"] == len(payload)

    conn = kb.connect()
    try:
        atts = kb.list_attachments(conn, worker_env)
        # Filename derived from the URL path leaf.
        assert atts[0].filename == "report.bin"
        assert Path(atts[0].stored_path).read_bytes() == payload
    finally:
        conn.close()


def test_attach_url_rejects_oversize_stream(worker_env, monkeypatch, allow_private_urls):
    """An oversize response body is rejected during download, no row written."""
    import http.server
    import threading

    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    big = b"x" * (64 * 1024)

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(big)))
            self.end_headers()
            self.wfile.write(big)

        def log_message(self, *a):
            pass

    monkeypatch.setattr(kb, "KANBAN_ATTACHMENT_MAX_BYTES", 1024)
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_address[1]
        out = kt._handle_attach_url({"url": f"http://127.0.0.1:{port}/big.bin"})
    finally:
        srv.shutdown()
    d = json.loads(out)
    assert "error" in d
    assert "MB limit" in d["error"]

    conn = kb.connect()
    try:
        assert kb.list_attachments(conn, worker_env) == []
    finally:
        conn.close()


def test_attach_url_rejects_non_http_scheme(worker_env):
    from tools import kanban_tools as kt

    out = kt._handle_attach_url({"url": "file:///etc/passwd"})
    d = json.loads(out)
    assert "error" in d
    assert "scheme" in d["error"]


# ---------------------------------------------------------------------------
# kanban_attach_url — SSRF guard (tools/url_safety.is_safe_url per hop)
# ---------------------------------------------------------------------------


@pytest.fixture
def default_url_guard(monkeypatch):
    """Force the SSRF guard to its secure default for this test.

    Clears HERMES_ALLOW_PRIVATE_URLS and resets url_safety's process-lifetime
    cache on both sides so a prior test's opt-in can't leak in.
    """
    from tools import url_safety

    monkeypatch.delenv("HERMES_ALLOW_PRIVATE_URLS", raising=False)
    url_safety._reset_allow_private_cache()
    yield
    url_safety._reset_allow_private_cache()


def _assert_attach_url_blocked(worker_env, url):
    """Call kanban_attach_url with ``url`` and assert the SSRF guard fired
    (clean tool error, no attachment row, no network fetch needed)."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    out = kt._handle_attach_url({"url": url})
    d = json.loads(out)
    assert "error" in d, out
    assert "SSRF" in d["error"] or "blocked" in d["error"].lower(), out
    conn = kb.connect()
    try:
        assert kb.list_attachments(conn, worker_env) == []
    finally:
        conn.close()


def test_attach_url_blocks_loopback(worker_env, default_url_guard):
    """http://127.0.0.1/ is rejected before any connection is made."""
    _assert_attach_url_blocked(worker_env, "http://127.0.0.1/")


def test_attach_url_blocks_cloud_metadata(worker_env, default_url_guard):
    """The cloud metadata endpoint is rejected — the #1 SSRF target."""
    _assert_attach_url_blocked(
        worker_env, "http://169.254.169.254/latest/meta-data/"
    )


def test_attach_url_blocks_private_range(worker_env, default_url_guard):
    """RFC1918 addresses (http://10.0.0.1/) are rejected."""
    _assert_attach_url_blocked(worker_env, "http://10.0.0.1/")


def _fake_public_dns(monkeypatch, mapping):
    """Patch url_safety's getaddrinfo so hostnames in ``mapping`` resolve to
    the given (public) IPs and literal IPs resolve to themselves — no real
    DNS or network traffic."""
    import ipaddress
    import socket as _socket

    real_af, real_sock = _socket.AF_INET, _socket.SOCK_STREAM

    def fake_getaddrinfo(host, *args, **kwargs):
        ip = mapping.get(host)
        if ip is None:
            # Literal IPs pass through; unknown hostnames fail like NXDOMAIN.
            try:
                ipaddress.ip_address(host)
            except ValueError:
                raise _socket.gaierror(f"fake DNS: unknown host {host!r}")
            ip = host
        return [(real_af, real_sock, 6, "", (ip, 0))]

    from tools import url_safety
    monkeypatch.setattr(url_safety.socket, "getaddrinfo", fake_getaddrinfo)


class _FakeStreamResponse:
    def __init__(self, *, status_code=200, headers=None, body=b""):
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body

    @property
    def is_redirect(self):
        return 300 <= self.status_code < 400 and "location" in {
            k.lower() for k in self.headers
        }

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_bytes(self, chunk_size):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_attach_url_blocks_redirect_to_loopback(worker_env, default_url_guard, monkeypatch):
    """A public host 302ing to loopback is caught on the redirect hop.

    The pre-flight check passes (public IP), then the mocked response
    redirects to http://127.0.0.1/ — the guard must re-validate the
    Location target and refuse to follow it.
    """
    import httpx

    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    _fake_public_dns(monkeypatch, {"files.example.com": "93.184.216.34"})

    requested = []

    def fake_stream(method, url, **kwargs):
        requested.append(url)
        assert kwargs.get("follow_redirects") is False
        return _FakeStreamResponse(
            status_code=302,
            headers={"location": "http://127.0.0.1/latest/secrets"},
        )

    monkeypatch.setattr(httpx, "stream", fake_stream)

    out = kt._handle_attach_url({"url": "http://files.example.com/report.pdf"})
    d = json.loads(out)
    assert "error" in d, out
    assert "127.0.0.1" in d["error"], out
    # Only the public hop was ever fetched; the loopback target never was.
    assert requested == ["http://files.example.com/report.pdf"]

    conn = kb.connect()
    try:
        assert kb.list_attachments(conn, worker_env) == []
    finally:
        conn.close()


def test_attach_url_happy_path_public_host(worker_env, default_url_guard, monkeypatch):
    """A public URL passes the guard and the bytes are stored (mocked fetch)."""
    from pathlib import Path

    import httpx

    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    _fake_public_dns(monkeypatch, {"files.example.com": "93.184.216.34"})

    payload = b"public fetch body"

    def fake_stream(method, url, **kwargs):
        assert url == "http://files.example.com/docs/spec.pdf"
        return _FakeStreamResponse(
            status_code=200,
            headers={"content-type": "application/pdf; charset=binary"},
            body=payload,
        )

    monkeypatch.setattr(httpx, "stream", fake_stream)

    out = kt._handle_attach_url({"url": "http://files.example.com/docs/spec.pdf"})
    d = json.loads(out)
    assert d.get("ok") is True, out
    assert d["size"] == len(payload)

    conn = kb.connect()
    try:
        atts = kb.list_attachments(conn, worker_env)
        assert [a.filename for a in atts] == ["spec.pdf"]
        assert atts[0].content_type == "application/pdf"
        assert Path(atts[0].stored_path).read_bytes() == payload
    finally:
        conn.close()


def _jira_scoped_steps(implement_key, verify_key):
    steps = _compiled_workflow_steps()
    steps[0]["jira_key"] = implement_key
    steps[1]["jira_key"] = verify_key
    return steps


def _compile_workflow_env(monkeypatch):
    from gateway.session_context import reset_session_vars

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setenv("HERMES_PROFILE", "orchestrator")
    monkeypatch.setenv("HERMES_SESSION_ID", "session-jira-scope")
    reset_session_vars()
    _install_compiler_test_profiles()


def test_compile_workflow_rejects_divergent_jira_scope_without_mapping(
    monkeypatch, worker_env,
):
    """BUILD-580: verifier scoped to BUILD-567 while the fix landed under
    BUILD-575 left the remediation unverifiable. Divergent step jira_key
    values without an explicit jira_scope_mapping must be refused."""
    from tools import kanban_tools as kt

    _compile_workflow_env(monkeypatch)
    result = json.loads(kt._handle_compile_workflow({
        "workflow_key": "sig-mismatch-test",
        "idempotency_key": "sig-mismatch-test:v1",
        "steps": _jira_scoped_steps("BUILD-575", "BUILD-567"),
    }))
    assert result.get("ok") is not True
    assert "jira" in json.dumps(result).lower()


def test_compile_workflow_accepts_divergent_jira_scope_with_mapping(
    monkeypatch, worker_env,
):
    from tools import kanban_tools as kt

    _compile_workflow_env(monkeypatch)
    result = json.loads(kt._handle_compile_workflow({
        "workflow_key": "sig-mapped-test",
        "idempotency_key": "sig-mapped-test:v1",
        "steps": _jira_scoped_steps("BUILD-575", "BUILD-567"),
        "jira_scope_mapping": {"BUILD-567": "BUILD-575"},
    }))
    assert result.get("ok") is True


def test_compile_workflow_uniform_jira_scope_needs_no_mapping(
    monkeypatch, worker_env,
):
    from tools import kanban_tools as kt

    _compile_workflow_env(monkeypatch)
    result = json.loads(kt._handle_compile_workflow({
        "workflow_key": "sig-uniform-test",
        "idempotency_key": "sig-uniform-test:v1",
        "steps": _jira_scoped_steps("BUILD-575", "BUILD-575"),
    }))
    assert result.get("ok") is True


def test_compile_workflow_signature_retry_is_idempotent(
    monkeypatch, worker_env,
):
    """BUILD-580 AC: signature-keyed remediation graphs never duplicate on
    retry — the exact sentinel signature from the incident."""
    from tools import kanban_tools as kt

    _compile_workflow_env(monkeypatch)
    args = {
        "workflow_key": "sig-55f8324523b0a573",
        "idempotency_key": "sig-55f8324523b0a573:v1",
        "steps": _jira_scoped_steps("BUILD-575", "BUILD-575"),
    }
    first = json.loads(kt._handle_compile_workflow(args))
    second = json.loads(kt._handle_compile_workflow(args))
    assert first["ok"] is True
    assert first == second
