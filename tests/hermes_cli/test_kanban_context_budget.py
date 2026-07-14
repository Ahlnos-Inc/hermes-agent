"""Aggregate prompt-budget contracts for Kanban worker handoffs."""

from __future__ import annotations

from pathlib import Path


def test_worker_context_has_hard_aggregate_budget_and_names_omissions(
    tmp_path, monkeypatch,
):
    from hermes_cli import kanban_db as kb

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()

    with kb.connect() as conn:
        parents = []
        for index in range(100):
            parent = kb.create_task(conn, title=f"parent-{index:03d}")
            assert kb.complete_task(
                conn,
                parent,
                result=f"PARENT-{index:03d}:" + ("x" * 4096),
            )
            parents.append(parent)
        child = kb.create_task(
            conn,
            title="bounded aggregate handoff",
            body="body:" + ("b" * 8192),
            parents=parents,
        )

        context = kb.build_worker_context(conn, child)

    assert len(context.encode("utf-8")) <= 48 * 1024
    assert "parent handoff" in context.lower()
    assert "omitted" in context.lower()
    assert parents[-1] in context


def test_downstream_worker_context_is_deterministic_bounded_and_identity_only(
    tmp_path, monkeypatch,
):
    from hermes_cli import kanban_db as kb

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()

    with kb.connect() as conn:
        parent = kb.create_task(
            conn,
            title="implementation",
            assignee="coder",
            workflow_key="bounded-workflow",
            current_step_key="implement",
        )
        for index in reversed(range(80)):
            kb.create_task(
                conn,
                title=f"downstream-{index:03d}",
                body=f"PRIVATE-CHILD-BODY-{index:03d}:" + ("x" * 4096),
                assignee="worker",
                parents=[parent],
                workflow_key="bounded-workflow",
                current_step_key=f"step-{index:03d}",
            )

        first = kb.build_worker_context(conn, parent)
        second = kb.build_worker_context(conn, parent)

    assert first == second
    assert len(first.encode("utf-8")) <= kb._CTX_MAX_TOTAL_BYTES
    downstream = "## Planned downstream workflow steps" + first.split(
        "## Planned downstream workflow steps", 1
    )[1]
    assert len((downstream + "\n").encode("utf-8")) <= kb._CTX_MAX_DOWNSTREAM_BYTES
    entries = [line for line in downstream.splitlines() if line.startswith("- `")]
    assert len(entries) <= kb._CTX_MAX_DOWNSTREAM_TASKS
    rendered_steps = [
        line.split("step: ", 1)[1].split(" |", 1)[0]
        for line in entries
    ]
    assert rendered_steps == sorted(rendered_steps)
    assert "additional direct child tasks omitted by section budget" in downstream
    assert "PRIVATE-CHILD-BODY" not in first
