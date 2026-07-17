"""Public behavior contract for atomic Kanban workflow compilation."""

from __future__ import annotations

import sqlite3

import pytest

from hermes_cli import kanban_db as kb


def test_compile_workflow_graph_subscribes_every_step(tmp_path):
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

        # BUILD-503: every step (not just the terminal) is subscribed so a
        # nonterminal step that blocks/gives up/fails-to-spawn still notifies
        # the origin instead of stranding the workflow silently.
        subscriptions = kb.list_notify_subs(conn)
        assert {
            (sub["task_id"], sub["platform"], sub["chat_id"]) for sub in subscriptions
        } == {
            (market.id, "telegram", "chat-123"),
            (customer.id, "telegram", "chat-123"),
            (terminal.id, "telegram", "chat-123"),
        }

        # BUILD-508: non-terminal step subs are narrowed to FAILURE_KINDS —
        # the terminal task's own subscription stays NULL (all kinds), the
        # only one that ever notified before BUILD-503/508.
        by_task = {sub["task_id"]: sub for sub in subscriptions}
        assert kb.notify_sub_kinds(by_task[market.id]) == kb.FAILURE_KINDS
        assert kb.notify_sub_kinds(by_task[customer.id]) == kb.FAILURE_KINDS
        assert kb.notify_sub_kinds(by_task[terminal.id]) is None
    finally:
        conn.close()


def test_compiled_worker_context_names_declared_direct_downstream_step(tmp_path):
    """A workflow worker sees its already-compiled child before delegating."""
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        compiled = kb.compile_workflow_graph(
            conn,
            workflow_key="peptimate-build",
            idempotency_key="peptimate-build-v1",
            created_by="orchestrator",
            steps=[
                {
                    "key": "implement",
                    "title": "Implement the deterministic renderer",
                    "assignee": "coder",
                },
                {
                    "key": "curate-vault",
                    "title": "Curate durable Peptimate findings",
                    "body": "FULL-DOWNSTREAM-BODY-MUST-STAY-HIDDEN",
                    "assignee": "vault-v2-curator",
                    "parents": ["implement"],
                },
                {
                    "key": "verify",
                    "title": "Verify the completed workflow",
                    "assignee": "verifier",
                    "parents": ["curate-vault"],
                    "role": "reporter",
                    "terminal": True,
                },
            ],
        )

        context = kb.build_worker_context(conn, compiled.task_ids["implement"])

        downstream_id = compiled.task_ids["curate-vault"]
        assert "## Planned downstream workflow steps" in context
        assert downstream_id in context
        assert "Curate durable Peptimate findings" in context
        assert "vault-v2-curator" in context
        assert "status: todo" in context
        assert "step: curate-vault" in context
        assert "workflow: peptimate-build" in context
        assert "reuse" in context.lower()
        assert "do not create duplicate" in context.lower()
        assert "FULL-DOWNSTREAM-BODY-MUST-STAY-HIDDEN" not in context
        assert compiled.task_ids["verify"] not in context
    finally:
        conn.close()


def test_worker_context_shows_only_direct_dynamic_children_with_workflow_identity(
    tmp_path,
):
    """Ad-hoc remediation children stay visible without implying ancestry."""
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        parent = kb.create_task(conn, title="Ordinary implementation", assignee="coder")
        ad_hoc_child = kb.create_task(
            conn,
            title="Ad-hoc documentation follow-up",
            assignee="writer",
            parents=[parent],
        )
        remediation_child = kb.create_task(
            conn,
            title="Repair release diagnostics",
            assignee="releaser",
            parents=[parent],
            workflow_key="release-remediation-1",
            current_step_key="repair",
        )
        completed_child = kb.create_task(
            conn,
            title="Previously completed follow-up",
            assignee="writer",
        )
        assert kb.claim_task(conn, completed_child)
        assert kb.complete_task(
            conn,
            completed_child,
            result="PRIVATE-CHILD-RESULT-MUST-STAY-HIDDEN",
        )
        kb.link_tasks(conn, parent, completed_child)
        nested = kb.create_task(
            conn,
            title="Nested verification",
            assignee="verifier",
            parents=[remediation_child],
            workflow_key="release-remediation-1",
            current_step_key="verify",
        )

        context = kb.build_worker_context(conn, parent)

        assert ad_hoc_child in context
        assert remediation_child in context
        assert completed_child in context
        assert "workflow: (none)" in context
        assert "step: (none)" in context
        assert "workflow: release-remediation-1" in context
        assert "step: repair" in context
        assert "PRIVATE-CHILD-RESULT-MUST-STAY-HIDDEN" not in context
        assert nested not in context
    finally:
        conn.close()


def test_worker_context_without_children_keeps_the_ordinary_rendering(tmp_path):
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        task_id = kb.create_task(
            conn,
            title="Ordinary standalone task",
            assignee="coder",
        )

        context = kb.build_worker_context(conn, task_id)

        assert context == (
            f"# Kanban task {task_id}: Ordinary standalone task\n"
            "\n"
            "Assignee: coder\n"
            "Status:   ready\n"
            "Workspace: scratch @ (unresolved)\n"
        )
        assert "Planned downstream workflow steps" not in context
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


def test_compile_workflow_graph_persists_runtime_route_per_step(tmp_path):
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        compiled = kb.compile_workflow_graph(
            conn,
            workflow_key="routed-workflow",
            idempotency_key="routed-workflow-v1",
            created_by="orchestrator",
            steps=[
                {
                    "key": "work",
                    "title": "Do work",
                    "assignee": "coder",
                    "model_override": "gpt-5.6-terra",
                    "model_provider_override": "openai-codex",
                    "model_reasoning_effort": "high",
                },
                {
                    "key": "final",
                    "title": "Verify",
                    "assignee": "verifier",
                    "parents": ["work"],
                    "role": "reporter",
                    "terminal": True,
                    "model_override": "gpt-5.6-sol",
                    "model_provider_override": "openai-codex",
                    "model_reasoning_effort": "xhigh",
                },
            ],
        )

        work = kb.get_task(conn, compiled.task_ids["work"])
        final = kb.get_task(conn, compiled.task_ids["final"])
        assert (
            work.model_provider_override,
            work.model_override,
            work.model_reasoning_effort,
        ) == ("openai-codex", "gpt-5.6-terra", "high")
        assert (
            final.model_provider_override,
            final.model_override,
            final.model_reasoning_effort,
        ) == ("openai-codex", "gpt-5.6-sol", "xhigh")

        with pytest.raises(kb.WorkflowGraphError, match="model_reasoning_effort"):
            kb.compile_workflow_graph(
                conn,
                workflow_key="bad-route",
                idempotency_key="bad-route-v1",
                created_by="orchestrator",
                steps=[
                    {
                        "key": "work",
                        "title": "Do work",
                        "assignee": "coder",
                        "model_reasoning_effort": "maximum",
                    },
                    {
                        "key": "final",
                        "title": "Verify",
                        "assignee": "verifier",
                        "parents": ["work"],
                        "role": "reporter",
                        "terminal": True,
                    },
                ],
            )
        assert conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE workflow_key = 'bad-route'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_compile_workflow_graph_rejects_invalid_forced_skill_before_write(
    monkeypatch, tmp_path,
):
    conn = kb.connect(tmp_path / "kanban.db")
    monkeypatch.setattr(
        kb,
        "_forced_skill_validation_error",
        lambda assignee, skills: (
            "forced skill validation failed" if skills else None
        ),
    )
    try:
        with pytest.raises(kb.WorkflowGraphError, match="forced skill validation"):
            kb.compile_workflow_graph(
                conn,
                workflow_key="bad-forced-skill",
                idempotency_key="bad-forced-skill-v1",
                created_by="orchestrator",
                steps=[
                    {
                        "key": "work",
                        "title": "Work",
                        "assignee": "coder",
                        "skills": ["not-installed"],
                    },
                    {
                        "key": "final",
                        "title": "Final",
                        "assignee": "verifier",
                        "parents": ["work"],
                        "role": "reporter",
                        "terminal": True,
                    },
                ],
            )
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    finally:
        conn.close()


def test_compile_workflow_graph_denies_active_architecture_session_in_transaction(
    tmp_path,
):
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        session_id = "architecture-session"
        context = kb.MutationContext(
            board_key="default",
            principal=f"orchestrator:{session_id}",
            actor_type="orchestrator_agent",
            profile="orchestrator",
            session_id=session_id,
            request_scope_id="front-door:turn-1",
            mode="shadow",
            phase="architecture",
        )
        kb.create_task(
            conn,
            title="Design first",
            assignee="architect",
            session_id=session_id,
            mutation_context=context,
        )

        with pytest.raises(
            kb.WorkflowGraphError, match="architecture_graph_issuance_required"
        ):
            kb.compile_workflow_graph(
                conn,
                workflow_key="must-not-bypass-gate",
                idempotency_key="must-not-bypass-gate-v1",
                created_by="orchestrator",
                session_id=session_id,
                request_scope_id="front-door:turn-2",
                deny_active_architecture_session=True,
                steps=[
                    {"key": "work", "title": "Work", "assignee": "coder"},
                    {
                        "key": "final",
                        "title": "Final",
                        "assignee": "verifier",
                        "parents": ["work"],
                        "role": "reporter",
                        "terminal": True,
                    },
                ],
            )
        assert conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE workflow_key = 'must-not-bypass-gate'"
        ).fetchone()[0] == 0
    finally:
        conn.close()
