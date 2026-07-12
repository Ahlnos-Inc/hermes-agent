"""Tests for kb.decompose_triage_task — the DB-layer atomic fan-out
from the triage column. LLM-free by design.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _create_triage(conn, title="rough idea", body=None, assignee=None, tenant=None):
    return kb.create_task(
        conn,
        title=title,
        body=body,
        assignee=assignee,
        tenant=tenant,
        triage=True,
    )


def test_decompose_creates_children_and_promotes_root(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn, title="ship a feature")
        assert kb.get_task(conn, tid).status == "triage"

    children = [
        {"title": "research", "body": "look at prior art", "assignee": "researcher", "parents": []},
        {"title": "build it", "body": "write code", "assignee": "engineer", "parents": [0]},
    ]
    with kb.connect() as conn:
        child_ids = kb.decompose_triage_task(
            conn,
            tid,
            root_assignee="orchestrator",
            children=children,
            author="decomposer",
        )
    assert child_ids is not None
    assert len(child_ids) == 2

    with kb.connect() as conn:
        root = kb.get_task(conn, tid)
        c0 = kb.get_task(conn, child_ids[0])
        c1 = kb.get_task(conn, child_ids[1])

    # Root flipped to todo with orchestrator assignee, gated by children.
    assert root.status == "todo"
    assert root.assignee == "orchestrator"
    # First child has no internal parents → ready on recompute_ready.
    assert c0.status == "ready"
    assert c0.assignee == "researcher"
    # Second child has parents=[0] → stays in todo until c0 completes.
    assert c1.status == "todo"
    assert c1.assignee == "engineer"


def test_decompose_returns_none_when_task_missing(kanban_home):
    with kb.connect() as conn:
        result = kb.decompose_triage_task(
            conn,
            "nonexistent",
            root_assignee="orch",
            children=[{"title": "x"}],
            author="me",
        )
    assert result is None


def test_decompose_returns_none_when_task_not_in_triage(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="already a real task")  # not triage
        result = kb.decompose_triage_task(
            conn,
            tid,
            root_assignee="orch",
            children=[{"title": "x"}],
            author="me",
        )
    assert result is None


def test_decompose_empty_children_returns_none(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn)
        result = kb.decompose_triage_task(
            conn,
            tid,
            root_assignee="orch",
            children=[],
            author="me",
        )
    assert result is None


def test_decompose_rejects_self_parent(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn)
        with pytest.raises(ValueError, match="cannot list itself"):
            kb.decompose_triage_task(
                conn,
                tid,
                root_assignee="orch",
                children=[{"title": "x", "parents": [0]}],
                author="me",
            )


def test_decompose_rejects_out_of_range_parent(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn)
        with pytest.raises(ValueError, match="not a valid index"):
            kb.decompose_triage_task(
                conn,
                tid,
                root_assignee="orch",
                children=[{"title": "x", "parents": [5]}],
                author="me",
            )


def test_decompose_rejects_cyclic_parents(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn)
        with pytest.raises(ValueError, match="cyclic dependency"):
            kb.decompose_triage_task(
                conn,
                tid,
                root_assignee="orch",
                children=[
                    {"title": "A", "parents": [1]},
                    {"title": "B", "parents": [0]},
                ],
                author="me",
            )


def test_decompose_records_audit_comment_and_event(kanban_home):
    with kb.connect() as conn:
        tid = _create_triage(conn)
        child_ids = kb.decompose_triage_task(
            conn,
            tid,
            root_assignee="orch",
            children=[{"title": "task A", "assignee": "researcher"}],
            author="alice",
        )
    assert child_ids is not None

    with kb.connect() as conn:
        comments = kb.list_comments(conn, tid)
        events = kb.list_events(conn, tid)

    assert any("Decomposed into" in (c.body or "") for c in comments)
    assert any(ev.kind == "decomposed" for ev in events)


def test_decompose_children_inherit_dir_workspace(kanban_home):
    """Fan-out children inherit the root's dir workspace, not scratch."""
    proj = "/home/teknium/myproject"
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="codegen root", assignee="worker",
            workspace_kind="dir", workspace_path=proj, triage=True,
        )
        child_ids = kb.decompose_triage_task(
            conn, tid, root_assignee="orchestrator",
            children=[{"title": "part A"}, {"title": "part B", "parents": [0]}],
            author="decomposer",
        )
    assert child_ids and len(child_ids) == 2
    with kb.connect() as conn:
        for cid in child_ids:
            t = kb.get_task(conn, cid)
            assert t.workspace_kind == "dir"
            assert t.workspace_path == proj


def test_decompose_children_stay_scratch_when_root_scratch(kanban_home):
    """No regression: a scratch root still fans out into scratch children."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="scratch root", assignee="worker",
            workspace_kind="scratch", triage=True,
        )
        child_ids = kb.decompose_triage_task(
            conn, tid, root_assignee="orchestrator",
            children=[{"title": "s1"}], author="decomposer",
        )
    with kb.connect() as conn:
        t = kb.get_task(conn, child_ids[0])
    assert t.workspace_kind == "scratch"
    assert t.workspace_path is None


def test_decompose_per_child_workspace_override(kanban_home):
    """An explicit per-child workspace beats inheritance."""
    proj = "/home/teknium/myproject"
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="root", assignee="worker",
            workspace_kind="dir", workspace_path=proj, triage=True,
        )
        child_ids = kb.decompose_triage_task(
            conn, tid, root_assignee="orchestrator",
            children=[
                {"title": "override", "workspace_kind": "dir",
                 "workspace_path": "/other/repo"},
                {"title": "inherit"},
            ],
            author="decomposer",
        )
    with kb.connect() as conn:
        over = kb.get_task(conn, child_ids[0])
        inh = kb.get_task(conn, child_ids[1])
    assert over.workspace_path == "/other/repo"
    assert inh.workspace_path == proj

# ---------------------------------------------------------------------------
# Architecture-gate rejection tests (BUILD-382 backstop)
# ---------------------------------------------------------------------------

def _insert_gate(conn, architect_task_id, *, enforcement_mode, state="open"):
    """Directly insert an architecture_gate row for testing without going
    through the full accept/approve handshake.  Lets us freeze any gate
    state cheaply."""
    now = int(time.time())
    conn.execute(
        """INSERT INTO architecture_gates (
            gate_id, board_key, creator_principal, creator_actor_type,
            request_scope_id, architect_task_id, state,
            policy_version, canonicalization_version,
            enforcement_mode, row_version, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)""",
        (
            "g_test_" + architect_task_id[:8],
            "default",
            "test-principal",
            "orchestrator_agent",
            "scope-1",
            architect_task_id,
            state,
            "v1",
            "v1",
            enforcement_mode,
            now,
            now,
        ),
    )


def _count_tasks(conn):
    return conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]


def test_decompose_rejects_orchestrator_only_gate_open_state(kanban_home):
    """orchestrator_only mode + open gate must raise architecture_gate_open
    and must not insert any child tasks."""
    with kb.connect() as conn:
        architect_id = kb.create_task(conn, title="architect task")
        _insert_gate(conn, architect_id, enforcement_mode="orchestrator_only", state="open")
        # Create triage task; link it under the architect task so the gate traversal hits it.
        triage_id = kb.create_task(conn, title="triage work", triage=True)
        conn.execute(
            "INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
            (architect_id, triage_id),
        )
        before = _count_tasks(conn)

    with kb.connect() as conn:
        with pytest.raises(kb.ArchitectureGateError) as exc_info:
            kb.decompose_triage_task(
                conn, triage_id, root_assignee="orch",
                children=[{"title": "child A"}], author="tester",
            )
        assert exc_info.value.code == "architecture_gate_open"

    # Zero children inserted.
    with kb.connect() as conn:
        assert _count_tasks(conn) == before


def test_decompose_rejects_orchestrator_only_gate_human_approved_without_issuance(kanban_home):
    """orchestrator_only mode + human_approved gate without issuance must raise
    architecture_graph_issuance_required and must not insert any child tasks."""
    with kb.connect() as conn:
        architect_id = kb.create_task(conn, title="architect task 2")
        _insert_gate(conn, architect_id, enforcement_mode="orchestrator_only", state="human_approved")
        triage_id = kb.create_task(conn, title="triage work 2", triage=True)
        conn.execute(
            "INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
            (architect_id, triage_id),
        )
        before = _count_tasks(conn)

    with kb.connect() as conn:
        with pytest.raises(kb.ArchitectureGateError) as exc_info:
            kb.decompose_triage_task(
                conn, triage_id, root_assignee="orch",
                children=[{"title": "child B"}], author="tester",
            )
        assert exc_info.value.code == "architecture_graph_issuance_required"

    with kb.connect() as conn:
        assert _count_tasks(conn) == before


def test_decompose_rejects_orchestrator_only_gate_after_graph_issuance(kanban_home):
    """A second graph path stays closed after the canonical graph is issued."""
    with kb.connect() as conn:
        architect_id = kb.create_task(conn, title="architect task issued")
        _insert_gate(
            conn,
            architect_id,
            enforcement_mode="orchestrator_only",
            state="human_approved",
        )
        gate = kb.get_architecture_gate_for_task(conn, architect_id)
        assert gate is not None
        conn.execute(
            """INSERT INTO architecture_graph_issuances
               (gate_id, idempotency_key, task_ids, issued_by, issued_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                gate.gate_id,
                "issued-once",
                json.dumps(["t_existing"]),
                "orchestrator",
                int(time.time()),
            ),
        )
        triage_id = kb.create_task(conn, title="triage issued", triage=True)
        conn.execute(
            "INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
            (architect_id, triage_id),
        )
        before = _count_tasks(conn)

    with kb.connect() as conn:
        with pytest.raises(kb.ArchitectureGateError) as exc_info:
            kb.decompose_triage_task(
                conn,
                triage_id,
                root_assignee="orch",
                children=[{"title": "second graph child"}],
                author="tester",
            )
        assert exc_info.value.code == "architecture_graph_issued"
        assert _count_tasks(conn) == before


def test_decompose_rejects_legacy_enforce_gate_open_state(kanban_home):
    """Legacy enforce mode + open gate must now raise architecture_gate_open
    (previously slipped through; this is the new backstop behavior)."""
    with kb.connect() as conn:
        architect_id = kb.create_task(conn, title="architect task 3")
        _insert_gate(conn, architect_id, enforcement_mode="enforce", state="open")
        triage_id = kb.create_task(conn, title="triage work 3", triage=True)
        conn.execute(
            "INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
            (architect_id, triage_id),
        )
        before = _count_tasks(conn)

    with kb.connect() as conn:
        with pytest.raises(kb.ArchitectureGateError) as exc_info:
            kb.decompose_triage_task(
                conn, triage_id, root_assignee="orch",
                children=[{"title": "child C"}], author="tester",
            )
        assert exc_info.value.code == "architecture_gate_open"

    with kb.connect() as conn:
        assert _count_tasks(conn) == before


def test_decompose_rejects_legacy_enforce_gate_human_approved_without_issuance(kanban_home):
    """Legacy enforce mode + human_approved without issuance still raises
    architecture_graph_issuance_required (unchanged legacy behavior)."""
    with kb.connect() as conn:
        architect_id = kb.create_task(conn, title="architect task 4")
        _insert_gate(conn, architect_id, enforcement_mode="enforce", state="human_approved")
        triage_id = kb.create_task(conn, title="triage work 4", triage=True)
        conn.execute(
            "INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
            (architect_id, triage_id),
        )
        before = _count_tasks(conn)

    with kb.connect() as conn:
        with pytest.raises(kb.ArchitectureGateError) as exc_info:
            kb.decompose_triage_task(
                conn, triage_id, root_assignee="orch",
                children=[{"title": "child D"}], author="tester",
            )
        assert exc_info.value.code == "architecture_graph_issuance_required"

    with kb.connect() as conn:
        assert _count_tasks(conn) == before


def test_decompose_allows_shadow_mode_gate(kanban_home):
    """shadow mode must NOT enforce — decomposition must succeed."""
    with kb.connect() as conn:
        architect_id = kb.create_task(conn, title="architect task shadow")
        _insert_gate(conn, architect_id, enforcement_mode="shadow", state="open")
        triage_id = kb.create_task(conn, title="triage shadow", triage=True)
        conn.execute(
            "INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
            (architect_id, triage_id),
        )

    with kb.connect() as conn:
        child_ids = kb.decompose_triage_task(
            conn, triage_id, root_assignee="orch",
            children=[{"title": "shadow child"}], author="tester",
        )
    assert child_ids is not None and len(child_ids) == 1


def test_decompose_allows_off_mode_gate(kanban_home):
    """off mode must NOT enforce — decomposition must succeed."""
    with kb.connect() as conn:
        architect_id = kb.create_task(conn, title="architect task off")
        _insert_gate(conn, architect_id, enforcement_mode="off", state="human_approved")
        triage_id = kb.create_task(conn, title="triage off", triage=True)
        conn.execute(
            "INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
            (architect_id, triage_id),
        )

    with kb.connect() as conn:
        child_ids = kb.decompose_triage_task(
            conn, triage_id, root_assignee="orch",
            children=[{"title": "off child"}], author="tester",
        )
    assert child_ids is not None and len(child_ids) == 1
