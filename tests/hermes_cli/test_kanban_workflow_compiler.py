"""Public behavior contract for atomic Kanban workflow compilation."""

from __future__ import annotations

import sqlite3

import pytest

from hermes_cli import kanban_db as kb


def test_compile_workflow_graph_creates_one_terminal_subscription(tmp_path):
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        compiled = kb.compile_workflow_graph(
            conn,
            workflow_key="market-brief-2026-07-13",
            idempotency_key="request-42",
            created_by="orchestrator",
            steps=[
                {
                    "key": "market",
                    "title": "Map the market",
                    "assignee": "researcher",
                    "parents": [],
                },
                {
                    "key": "customer",
                    "title": "Map customer needs",
                    "assignee": "researcher",
                    "parents": [],
                },
                {
                    "key": "final",
                    "title": "Write the decision brief",
                    "assignee": "writer",
                    "parents": ["market", "customer"],
                    "role": "reporter",
                    "terminal": True,
                },
            ],
            notification={
                "platform": "telegram",
                "chat_id": "chat-123",
                "thread_id": "thread-7",
                "user_id": "user-9",
                "notifier_profile": "orchestrator",
            },
        )

        market = kb.get_task(conn, compiled.task_ids["market"])
        customer = kb.get_task(conn, compiled.task_ids["customer"])
        terminal = kb.get_task(conn, compiled.terminal_task_id)

        assert market is not None and market.status == "ready"
        assert customer is not None and customer.status == "ready"
        assert terminal is not None and terminal.status == "todo"
        assert terminal.current_step_key == "final"
        assert set(kb.parent_ids(conn, terminal.id)) == {market.id, customer.id}
        assert {task.workflow_key for task in (market, customer, terminal)} == {
            "market-brief-2026-07-13"
        }

        subscriptions = kb.list_notify_subs(conn)
        assert [(sub["task_id"], sub["platform"], sub["chat_id"]) for sub in subscriptions] == [
            (terminal.id, "telegram", "chat-123")
        ]
    finally:
        conn.close()


def test_compile_workflow_graph_retry_is_idempotent_by_step_identity(tmp_path):
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        first_steps = [
            {"key": "a", "title": "A", "assignee": "worker", "parents": []},
            {"key": "b", "title": "B", "assignee": "worker", "parents": []},
            {
                "key": "final",
                "title": "Final",
                "assignee": "writer",
                "parents": ["a", "b"],
                "role": "synthesizer",
                "terminal": True,
            },
        ]
        first = kb.compile_workflow_graph(
            conn,
            workflow_key="stable-workflow",
            idempotency_key="stable-request",
            created_by="orchestrator",
            steps=first_steps,
        )

        retry = kb.compile_workflow_graph(
            conn,
            workflow_key="stable-workflow",
            idempotency_key="stable-request",
            created_by="orchestrator",
            steps=[
                {
                    "key": "final",
                    "title": "Final",
                    "assignee": "writer",
                    "parents": ["b", "a"],
                    "role": "synthesizer",
                    "terminal": True,
                },
                first_steps[1],
                first_steps[0],
            ],
        )

        assert retry == first
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM task_links").fetchone()[0] == 2

        with pytest.raises(kb.WorkflowGraphError, match="identity conflict"):
            kb.compile_workflow_graph(
                conn,
                workflow_key="stable-workflow",
                idempotency_key="stable-request",
                created_by="orchestrator",
                steps=[
                    {"key": "a", "title": "Changed A", "assignee": "worker"},
                    first_steps[1],
                    first_steps[2],
                ],
            )

        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 3
    finally:
        conn.close()


def test_compile_workflow_graph_refuses_a_partially_written_workflow(tmp_path):
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        existing = kb.create_task(
            conn,
            title="Legacy incremental card",
            assignee="worker",
            workflow_key="partially-written",
        )

        try:
            kb.compile_workflow_graph(
                conn,
                workflow_key="partially-written",
                idempotency_key="replacement-request",
                created_by="orchestrator",
                steps=[
                    {
                        "key": "final",
                        "title": "Replacement final",
                        "assignee": "writer",
                        "parents": [],
                        "role": "finalizer",
                        "terminal": True,
                    }
                ],
                notification={"platform": "telegram", "chat_id": "chat-1"},
            )
        except kb.WorkflowGraphError as exc:
            assert str(exc) == "workflow graph identity conflict"
        else:
            raise AssertionError("partial workflow was silently duplicated")

        rows = conn.execute(
            "SELECT id FROM tasks WHERE workflow_key = ?", ("partially-written",)
        ).fetchall()
        assert [row["id"] for row in rows] == [existing]
        assert kb.list_notify_subs(conn) == []
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("steps", "message"),
    [
        (
            [{"key": "only", "title": "Only", "assignee": "worker"}],
            "exactly one terminal",
        ),
        (
            [
                {
                    "key": "one",
                    "title": "One",
                    "assignee": "worker",
                    "role": "reporter",
                    "terminal": True,
                },
                {
                    "key": "two",
                    "title": "Two",
                    "assignee": "worker",
                    "role": "reporter",
                    "terminal": True,
                },
            ],
            "exactly one terminal",
        ),
        (
            [
                {"key": "orphan", "title": "Orphan", "assignee": "worker"},
                {
                    "key": "final",
                    "title": "Final",
                    "assignee": "writer",
                    "role": "finalizer",
                    "terminal": True,
                },
            ],
            "every workflow step must reach the terminal",
        ),
        (
            [
                {
                    "key": "a",
                    "title": "A",
                    "assignee": "worker",
                    "parents": ["b"],
                },
                {
                    "key": "b",
                    "title": "B",
                    "assignee": "worker",
                    "parents": ["a"],
                    "role": "finalizer",
                    "terminal": True,
                },
            ],
            "contains a cycle",
        ),
        (
            [
                {
                    "key": "final",
                    "title": "Final",
                    "assignee": "worker",
                    "role": "worker",
                    "terminal": True,
                },
            ],
            "terminal role",
        ),
    ],
)
def test_compile_workflow_graph_rejects_nonconvergent_topologies(
    tmp_path, steps, message
):
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        with pytest.raises(kb.WorkflowGraphError, match=message):
            kb.compile_workflow_graph(
                conn,
                workflow_key="invalid-workflow",
                idempotency_key="invalid-request",
                created_by="orchestrator",
                steps=steps,
            )

        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM task_links").fetchone()[0] == 0
    finally:
        conn.close()


def test_compile_workflow_graph_rolls_back_every_row_on_late_failure(tmp_path):
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        conn.execute(
            """CREATE TRIGGER reject_workflow_compilation
               BEFORE INSERT ON workflow_graph_compilations
               BEGIN SELECT RAISE(ABORT, 'injected late failure'); END"""
        )

        with pytest.raises(sqlite3.IntegrityError, match="injected late failure"):
            kb.compile_workflow_graph(
                conn,
                workflow_key="must-rollback",
                idempotency_key="rollback-request",
                created_by="orchestrator",
                steps=[
                    {
                        "key": "work",
                        "title": "Work",
                        "assignee": "worker",
                    },
                    {
                        "key": "final",
                        "title": "Final",
                        "assignee": "writer",
                        "parents": ["work"],
                        "role": "finalizer",
                        "terminal": True,
                    },
                ],
                notification={"platform": "telegram", "chat_id": "chat-1"},
            )

        for table in (
            "tasks",
            "task_links",
            "task_events",
            "kanban_notify_subs",
            "workflow_graph_compilations",
        ):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    finally:
        conn.close()


def test_compile_workflow_graph_supports_precompleted_root_and_step_limits(tmp_path):
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        compiled = kb.compile_workflow_graph(
            conn,
            workflow_key="swarm-rooted",
            idempotency_key="swarm-request",
            created_by="orchestrator",
            steps=[
                {
                    "key": "root",
                    "title": "Planning root",
                    "assignee": "orchestrator",
                    "role": "root",
                    "initial_status": "done",
                    "result": "Topology compiled atomically.",
                },
                {
                    "key": "worker",
                    "title": "Do work",
                    "assignee": "researcher",
                    "role": "worker",
                    "parents": ["root"],
                    "skills": ["research"],
                    "max_runtime_seconds": 900,
                    "priority": 7,
                },
                {
                    "key": "final",
                    "title": "Synthesize",
                    "assignee": "writer",
                    "role": "synthesizer",
                    "parents": ["worker"],
                    "terminal": True,
                },
            ],
        )

        root = kb.get_task(conn, compiled.task_ids["root"])
        worker = kb.get_task(conn, compiled.task_ids["worker"])
        assert root.status == "done"
        assert root.result == "Topology compiled atomically."
        assert root.completed_at is not None
        assert worker.status == "ready"
        assert worker.skills == ["research"]
        assert worker.max_runtime_seconds == 900
        assert worker.priority == 7
    finally:
        conn.close()
