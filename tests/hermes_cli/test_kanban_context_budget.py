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
