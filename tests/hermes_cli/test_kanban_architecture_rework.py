"""BUILD-862: bounded autonomous rework cycles inside issued architecture graphs.

The budget is declared once at issuance; a reviewer changes_requested verdict
(`request_rework` naming the issued fix node) re-arms the existing fix node
iff every same-graph precondition holds. Everything else stays denied with
the same fail-closed behavior as before.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb

GOOD_SHA = "a" * 40
GOOD_SHA_2 = "b" * 40


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _architect_context(scope: str = "turn-1") -> "kb.MutationContext":
    return kb.MutationContext(
        board_key="default",
        principal="orchestrator-session",
        actor_type="orchestrator_agent",
        session_id="session-1",
        request_scope_id=scope,
        mode="enforce",
        phase="architecture",
    )


def _human_context(scope: str = "turn-1") -> "kb.MutationContext":
    return kb.MutationContext(
        board_key="default",
        principal="human-1",
        actor_type="human",
        session_id="session-1",
        request_scope_id=scope,
        surface="cli",
        mode="enforce",
        phase="approval",
    )


def _formal_handoff() -> dict:
    return {
        "role": "architect",
        "design_depth": "formal",
        "chosen_approach": "Bounded rework loop declared at issuance.",
        "alternatives_rejected": ["post-issuance re-arm authority"],
        "slices": [{"name": "core", "verification": ["focused test"]}],
        "acceptance_criteria": ["re-arm only inside the issued graph"],
        "verification_plan": ["run focused tests"],
        "human_approval_required": True,
        "rollout": {"mode": "shadow"},
        "rollback": {"mode": "off"},
    }


def _issued_loop(conn, *, max_rework_cycles=None, scope: str = "turn-1"):
    """Approve a gate and issue a coder→reviewer→verifier graph.

    Returns (gate, coder_id, reviewer_id, verifier_id).
    """
    architect = kb.create_task(
        conn,
        title=f"Design workflow {scope}",
        assignee="architect",
        mutation_context=_architect_context(scope),
    )
    claimed = kb.claim_task(conn, architect)
    assert claimed is not None and claimed.current_run_id is not None
    assert kb.complete_task(
        conn, architect, metadata=_formal_handoff(),
        expected_run_id=claimed.current_run_id,
    )
    gate = kb.get_architecture_gate_for_task(conn, architect)
    assert gate is not None and gate.state == "validated_awaiting_approval"
    kb.accept_architecture_handoff(conn, gate.gate_id)
    approved = kb.approve_architecture_gate(
        conn, gate.gate_id, _human_context(scope), gate.design_digest or "",
    )
    issuer = kb.MutationContext(
        board_key="default",
        principal="orchestrator-session",
        actor_type="orchestrator_agent",
        session_id="session-1",
        request_scope_id=scope,
        gate_id=approved.gate_id,
        profile="orchestrator",
        mode="enforce",
        phase="graph_issuance",
    )
    issued = kb.issue_architecture_graph(
        conn,
        approved.gate_id,
        issuer,
        [
            {"title": "implement", "assignee": "coder", "parents": []},
            {"title": "review", "assignee": "reviewer", "parents": [0]},
            {"title": "verify", "assignee": "verifier", "parents": [1]},
        ],
        idempotency_key=f"graph-{scope}",
        max_rework_cycles=max_rework_cycles,
    )
    assert len(issued) == 3
    return approved, issued[0], issued[1], issued[2]


def _complete_coder(conn, coder: str, sha: str = GOOD_SHA, metadata=...):
    claimed = kb.claim_task(conn, coder)
    assert claimed is not None and claimed.current_run_id is not None
    if metadata is ...:
        metadata = {"artifact_delivery": {"commit_sha": sha}}
    assert kb.complete_task(
        conn, coder, metadata=metadata, expected_run_id=claimed.current_run_id,
    )


def _claim_reviewer(conn, reviewer: str):
    current = kb.get_task(conn, reviewer)
    if current.status == "running" and current.current_run_id is not None:
        # A denied rework rolls back only its own transaction; the reviewer
        # keeps its active run, exactly like a live worker after a tool error.
        return current
    assert current.status == "ready"
    claimed = kb.claim_task(conn, reviewer)
    assert claimed is not None and claimed.current_run_id is not None
    return claimed


def _request_rework(conn, reviewer: str, fix_task_id: str, key: str):
    claimed = _claim_reviewer(conn, reviewer)
    return kb.request_rework(
        conn,
        reviewer,
        finding="reviewer found a defect",
        fix=kb.ExistingFixTask(task_id=fix_task_id),
        request_key=key,
        actor="reviewer",
        expected_run_id=claimed.current_run_id,
    )


def _cycles_row(conn, gate_id, coder, reviewer):
    return conn.execute(
        "SELECT cycles_used FROM architecture_graph_rework_cycles "
        "WHERE gate_id = ? AND fix_task_id = ? AND review_task_id = ?",
        (gate_id, coder, reviewer),
    ).fetchone()


# ---------------------------------------------------------------------------
# Issuance contract
# ---------------------------------------------------------------------------

def test_issuance_stores_default_and_explicit_budget(kanban_home):
    with kb.connect() as conn:
        gate, *_ = _issued_loop(conn)
        policy = json.loads(conn.execute(
            "SELECT rework_policy FROM architecture_graph_issuances WHERE gate_id = ?",
            (gate.gate_id,),
        ).fetchone()[0])
        assert policy["max_rework_cycles"] == kb.ARCHITECTURE_REWORK_DEFAULT_BUDGET
        assert set(policy["assignees"].values()) == {"coder", "reviewer", "verifier"}

        gate_b, *_ = _issued_loop(conn, max_rework_cycles=1, scope="turn-2")
        policy_b = json.loads(conn.execute(
            "SELECT rework_policy FROM architecture_graph_issuances WHERE gate_id = ?",
            (gate_b.gate_id,),
        ).fetchone()[0])
        assert policy_b["max_rework_cycles"] == 1


def test_issuance_rejects_invalid_budget(kanban_home):
    with kb.connect() as conn:
        for bad in (-1, kb.ARCHITECTURE_REWORK_MAX_BUDGET + 1, "2", 2.0, True):
            with pytest.raises(ValueError, match="max_rework_cycles"):
                kb.issue_architecture_graph(
                    conn, "g_nonexistent",
                    kb.MutationContext(
                        board_key="default", principal="orchestrator-session",
                        actor_type="orchestrator_agent", profile="orchestrator",
                        mode="enforce", phase="graph_issuance",
                    ),
                    [{"title": "x", "assignee": "coder", "parents": []}],
                    idempotency_key="bad-budget",
                    max_rework_cycles=bad,
                )


# ---------------------------------------------------------------------------
# Integration: the full bounded loop
# ---------------------------------------------------------------------------

def test_full_rework_loop_rearm_then_pass_promotes_verifier(kanban_home):
    with kb.connect() as conn:
        gate, coder, reviewer, verifier = _issued_loop(conn)
        _complete_coder(conn, coder)
        result = _request_rework(conn, reviewer, coder, "rework-1")

        assert result.fix_action == "rearmed"
        assert result.fix_task_id == coder
        assert kb.get_task(conn, coder).status == "ready"
        assert kb.get_task(conn, reviewer).status == "todo"
        assert kb.get_task(conn, verifier).status == "todo"
        assert _cycles_row(conn, gate.gate_id, coder, reviewer)[0] == 1

        rearm_events = [
            e for e in kb.list_events(conn, coder)
            if e.kind == kb.ARCHITECTURE_REWORK_REARMED_EVENT
        ]
        assert len(rearm_events) == 1
        payload = rearm_events[0].payload
        assert payload["cycle"] == 1
        assert payload["max_rework_cycles"] == kb.ARCHITECTURE_REWORK_DEFAULT_BUDGET
        assert payload["reviewed_sha"] == GOOD_SHA
        assert payload["review_task_id"] == reviewer
        assert payload["fix_task_id"] == coder
        assert payload["open_blockers"] == []
        audit = [
            e for e in kb.list_events(conn, gate.architect_task_id)
            if e.kind == kb.ARCHITECTURE_REWORK_REARMED_EVENT
        ]
        assert len(audit) == 1 and audit[0].payload["gate_id"] == gate.gate_id

        # Second cycle of real work: coder fixes, reviewer passes, verifier arms.
        _complete_coder(conn, coder, sha=GOOD_SHA_2)
        claimed_review = _claim_reviewer(conn, reviewer)
        assert kb.complete_task(
            conn, reviewer, result="PASS",
            expected_run_id=claimed_review.current_run_id,
        )
        assert kb.get_task(conn, verifier).status == "ready"


def test_rearm_replay_is_idempotent(kanban_home):
    with kb.connect() as conn:
        gate, coder, reviewer, _ = _issued_loop(conn)
        _complete_coder(conn, coder)
        first = _request_rework(conn, reviewer, coder, "rework-1")
        assert first.fix_action == "rearmed"

        replay = kb.request_rework(
            conn,
            reviewer,
            finding="reviewer found a defect",
            fix=kb.ExistingFixTask(task_id=coder),
            request_key="rework-1",
            actor="reviewer",
        )
        assert replay.fix_action == "replayed"
        assert _cycles_row(conn, gate.gate_id, coder, reviewer)[0] == 1
        assert kb.get_task(conn, coder).status == "ready"


# ---------------------------------------------------------------------------
# Adversarial: every precondition fails closed
# ---------------------------------------------------------------------------

def _assert_denied(conn, reviewer, fix_task_id, key, code):
    before = {
        row["id"]: (row["status"], row["completed_at"])
        for row in conn.execute("SELECT id, status, completed_at FROM tasks")
    }
    with pytest.raises(kb.ArchitectureGateError, match=code):
        _request_rework(conn, reviewer, fix_task_id, key)
    after = {
        row["id"]: (row["status"], row["completed_at"])
        for row in conn.execute("SELECT id, status, completed_at FROM tasks")
    }
    # The denial rolls back every task mutation, including the reviewer's
    # claim-and-rework transition attempted inside the same call.
    for task_id, snapshot in before.items():
        if snapshot[0] in {"done", "archived", "todo"}:
            assert after[task_id] == snapshot


def test_budget_exhaustion_fails_closed(kanban_home):
    with kb.connect() as conn:
        gate, coder, reviewer, verifier = _issued_loop(conn, max_rework_cycles=1)
        _complete_coder(conn, coder)
        assert _request_rework(conn, reviewer, coder, "rework-1").fix_action == "rearmed"
        _complete_coder(conn, coder, sha=GOOD_SHA_2)

        with pytest.raises(
            kb.ArchitectureGateError, match="architecture_rework_budget_exhausted"
        ):
            _request_rework(conn, reviewer, coder, "rework-2")
        assert kb.get_task(conn, coder).status == "done"
        assert kb.get_task(conn, verifier).status == "todo"
        assert _cycles_row(conn, gate.gate_id, coder, reviewer)[0] == 1
        denials = [
            e for e in kb.list_events(conn, gate.architect_task_id)
            if e.kind == "architecture_gate_denied"
            and e.payload.get("reason") == "architecture_rework_budget_exhausted"
        ]
        assert denials, "denial must be audited on the gate"


def test_budget_zero_never_rearms(kanban_home):
    with kb.connect() as conn:
        _, coder, reviewer, _ = _issued_loop(conn, max_rework_cycles=0)
        _complete_coder(conn, coder)
        with pytest.raises(
            kb.ArchitectureGateError, match="architecture_rework_budget_exhausted"
        ):
            _request_rework(conn, reviewer, coder, "rework-1")


def test_double_fire_race_fails_closed(kanban_home):
    with kb.connect() as conn:
        gate, coder, reviewer, _ = _issued_loop(conn)
        _complete_coder(conn, coder)
        assert _request_rework(conn, reviewer, coder, "rework-1").fix_action == "rearmed"
        # The fix is re-armed (ready, not terminal): a second verdict with a
        # fresh request key cannot double-fire the cycle.
        with pytest.raises(
            kb.ArchitectureGateError, match="architecture_rework_fix_not_terminal"
        ):
            kb.request_rework(
                conn,
                reviewer,
                finding="second fire",
                fix=kb.ExistingFixTask(task_id=coder),
                request_key="rework-2",
                actor="reviewer",
            )
        assert _cycles_row(conn, gate.gate_id, coder, reviewer)[0] == 1


def test_legacy_issuance_without_policy_keeps_todays_denial(kanban_home):
    with kb.connect() as conn:
        gate, coder, reviewer, _ = _issued_loop(conn)
        conn.execute(
            "UPDATE architecture_graph_issuances SET rework_policy = NULL "
            "WHERE gate_id = ?",
            (gate.gate_id,),
        )
        conn.commit()
        _complete_coder(conn, coder)
        _assert_denied(conn, reviewer, coder, "rework-1", "architecture_graph_issued")


def test_cross_gate_fix_node_fails_closed(kanban_home):
    with kb.connect() as conn:
        _, coder_a, reviewer_a, _ = _issued_loop(conn, scope="turn-1")
        _, coder_b, _, _ = _issued_loop(conn, scope="turn-2")
        _complete_coder(conn, coder_a)
        _complete_coder(conn, coder_b)
        _assert_denied(
            conn, reviewer_a, coder_b, "rework-1", "architecture_graph_issued"
        )


def test_off_graph_fix_node_fails_closed(kanban_home):
    with kb.connect() as conn:
        _, coder, reviewer, _ = _issued_loop(conn)
        _complete_coder(conn, coder)
        stray = kb.create_task(conn, title="stray", assignee="coder")
        _assert_denied(conn, reviewer, stray, "rework-1", "architecture_graph_issued")


def test_in_graph_node_without_edge_fails_closed(kanban_home):
    with kb.connect() as conn:
        _, coder, reviewer, verifier = _issued_loop(conn)
        _complete_coder(conn, coder)
        # verifier is issued but has no verifier→reviewer edge.
        _assert_denied(
            conn, reviewer, verifier, "rework-1", "architecture_rework_edge_missing"
        )


def test_quarantined_fix_node_fails_closed(kanban_home):
    with kb.connect() as conn:
        gate, coder, reviewer, _ = _issued_loop(conn)
        _complete_coder(conn, coder)
        # The realistic race: the reviewer is already running when the fix
        # node gets quarantined, then returns changes_requested.
        claimed = _claim_reviewer(conn, reviewer)
        conn.execute(
            "UPDATE tasks SET policy_quarantined = 1 WHERE id = ?", (coder,)
        )
        conn.commit()
        with pytest.raises(ValueError, match="quarantined"):
            kb.request_rework(
                conn,
                reviewer,
                finding="defect",
                fix=kb.ExistingFixTask(task_id=coder),
                request_key="rework-1",
                actor="reviewer",
                expected_run_id=claimed.current_run_id,
            )
        assert kb.get_task(conn, coder).status == "done"
        assert _cycles_row(conn, gate.gate_id, coder, reviewer) is None


def test_changed_workspace_fails_closed(kanban_home):
    with kb.connect() as conn:
        _, coder, reviewer, _ = _issued_loop(conn)
        _complete_coder(conn, coder)
        conn.execute(
            "UPDATE tasks SET workspace_path = '/tmp/attacker' WHERE id = ?",
            (coder,),
        )
        conn.commit()
        _assert_denied(
            conn, reviewer, coder, "rework-1", "architecture_rework_workspace_changed"
        )


def test_changed_workspace_kind_fails_closed(kanban_home):
    with kb.connect() as conn:
        _, coder, reviewer, _ = _issued_loop(conn)
        _complete_coder(conn, coder)
        conn.execute(
            "UPDATE tasks SET workspace_kind = 'tampered-kind' WHERE id = ?",
            (coder,),
        )
        conn.commit()
        _assert_denied(
            conn, reviewer, coder, "rework-1", "architecture_rework_workspace_changed"
        )


def test_role_mismatch_fails_closed(kanban_home):
    with kb.connect() as conn:
        _, coder, reviewer, _ = _issued_loop(conn)
        _complete_coder(conn, coder)
        conn.execute(
            "UPDATE tasks SET assignee = 'releaser' WHERE id = ?", (coder,)
        )
        conn.commit()
        _assert_denied(
            conn, reviewer, coder, "rework-1", "architecture_rework_role_mismatch"
        )


def test_tampered_cycle_counter_fails_closed(kanban_home):
    with kb.connect() as conn:
        gate, coder, reviewer, _ = _issued_loop(conn, max_rework_cycles=1)
        _complete_coder(conn, coder)
        assert _request_rework(conn, reviewer, coder, "rework-1").fix_action == "rearmed"
        _complete_coder(conn, coder, sha=GOOD_SHA_2)

        # Deleting the counter row cannot reset the budget: the append-only
        # re-arm events are the accounting floor.
        conn.execute(
            "DELETE FROM architecture_graph_rework_cycles WHERE gate_id = ?",
            (gate.gate_id,),
        )
        conn.commit()
        with pytest.raises(
            kb.ArchitectureGateError, match="architecture_rework_budget_exhausted"
        ):
            _request_rework(conn, reviewer, coder, "rework-2")

        # A malformed counter value is tampering, not state.
        conn.execute(
            "INSERT OR REPLACE INTO architecture_graph_rework_cycles "
            "(gate_id, fix_task_id, review_task_id, cycles_used, updated_at) "
            "VALUES (?, ?, ?, -5, 0)",
            (gate.gate_id, coder, reviewer),
        )
        conn.commit()
        with pytest.raises(
            kb.ArchitectureGateError, match="architecture_rework_cycle_counter_invalid"
        ):
            _request_rework(conn, reviewer, coder, "rework-3")


def test_unattested_reviewed_sha_fails_closed(kanban_home):
    with kb.connect() as conn:
        _, coder, reviewer, _ = _issued_loop(conn)
        _complete_coder(conn, coder, metadata={"notes": "no delivery block"})
        _assert_denied(
            conn, reviewer, coder, "rework-1",
            "architecture_rework_reviewed_sha_unattested",
        )


def test_new_fix_card_stays_denied_after_issuance(kanban_home):
    with kb.connect() as conn:
        _, coder, reviewer, _ = _issued_loop(conn)
        _complete_coder(conn, coder)
        task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        with pytest.raises(
            kb.ArchitectureGateError, match="architecture_graph_issued"
        ):
            _request_rework_new_fix(conn, reviewer)
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == task_count


def _request_rework_new_fix(conn, reviewer: str):
    claimed = _claim_reviewer(conn, reviewer)
    return kb.request_rework(
        conn,
        reviewer,
        finding="wants a brand-new card",
        fix=kb.NewFixTask(title="new fix", body=None, assignee="coder"),
        request_key="new-fix-1",
        actor="reviewer",
        expected_run_id=claimed.current_run_id,
    )
