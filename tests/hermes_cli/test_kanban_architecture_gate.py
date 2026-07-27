"""Focused contracts for the Architecture-First Kanban gate."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from agent.kanban_delivery_policy import (
    policy_for_current_kanban_task,
    requeue_current_task_for_delivery_authorization,
)


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _architect_context(mode: str = "enforce") -> "kb.MutationContext":
    return kb.MutationContext(
        board_key="default",
        principal="orchestrator-session",
        actor_type="orchestrator_agent",
        session_id="session-1",
        request_scope_id="turn-1",
        mode=mode,
        phase="architecture",
    )


def _implementation_context() -> "kb.MutationContext":
    return kb.MutationContext(
        board_key="default",
        principal="orchestrator-session",
        actor_type="orchestrator_agent",
        session_id="session-1",
        request_scope_id="turn-1",
        mode="enforce",
        phase="implementation",
    )


def _formal_handoff() -> dict:
    return {
        "role": "architect",
        "design_depth": "formal",
        "chosen_approach": "Use one transactional gate projection.",
        "alternatives_rejected": ["prompt-only guard"],
        "slices": [{"name": "core", "verification": ["focused test"]}],
        "acceptance_criteria": ["protected work is denied before approval"],
        "verification_plan": ["run focused tests"],
        "human_approval_required": False,
        "rollout": {"mode": "shadow"},
        "rollback": {"mode": "off"},
    }


def test_schema_migrates_architecture_gates_projection(kanban_home):
    with kb.connect() as conn:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(architecture_gates)")
        }

    assert "architecture_gates" in tables
    assert {
        "gate_id",
        "board_key",
        "creator_actor_type",
        "creator_profile",
        "architect_task_id",
        "state",
        "design_digest",
        "accepted_snapshot",
        "row_version",
    } <= columns


def test_handoff_digest_is_stable_and_domain_separated():
    handoff = _formal_handoff()
    canonical = kb.canonicalize_architecture_handoff(handoff)
    first = kb.architecture_handoff_digest(
        policy_version="v1",
        canonicalization_version="v1",
        trusted_scope={"board_key": "default", "request_scope_id": "turn-1"},
        architect_task_id="t_architect",
        accepted_run_id=7,
        canonical_handoff_json=canonical,
    )
    second = kb.architecture_handoff_digest(
        policy_version="v1",
        canonicalization_version="v1",
        trusted_scope={"request_scope_id": "turn-1", "board_key": "default"},
        architect_task_id="t_architect",
        accepted_run_id=7,
        canonical_handoff_json=canonical,
    )
    different_scope = kb.architecture_handoff_digest(
        policy_version="v1",
        canonicalization_version="v1",
        trusted_scope={"board_key": "default", "request_scope_id": "turn-2"},
        architect_task_id="t_architect",
        accepted_run_id=7,
        canonical_handoff_json=canonical,
    )

    assert first == second
    assert first != different_scope
    with pytest.raises(ValueError, match="unknown top-level"):
        kb.canonicalize_architecture_handoff({**handoff, "forged": True})


def test_enforce_gate_blocks_protected_create_link_and_direct_claim(kanban_home):
    with kb.connect() as conn:
        architect = kb.create_task(
            conn,
            title="Design workflow",
            assignee="architect",
            mutation_context=_architect_context(),
        )
        gate = kb.get_architecture_gate_for_task(conn, architect)

        assert gate is not None
        assert gate.state == "open"
        assert gate.architect_task_id == architect

        with pytest.raises(kb.ArchitectureGateError, match="architecture_gate_open"):
            kb.create_task(
                conn,
                title="Implement workflow",
                assignee="coder",
                parents=[architect],
                mutation_context=_implementation_context(),
            )

        bypass = kb.create_task(conn, title="unsupported direct mutation", assignee="coder")
        conn.execute(
            "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)",
            (architect, bypass),
        )
        with pytest.raises(kb.ArchitectureGateError, match="architecture_gate_open"):
            kb.link_tasks(
                conn,
                architect,
                bypass,
                mutation_context=_implementation_context(),
            )
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (bypass,))

        assert kb.claim_task(conn, bypass) is None
        assert kb.get_task(conn, bypass).status == "todo"
        assert any(
            event.kind == "claim_blocked"
            and event.payload == {"reason": "architecture_gate_open", "gate_id": gate.gate_id}
            for event in kb.list_events(conn, bypass)
        )


def test_valid_completed_architect_handoff_accepts_exact_snapshot_and_allows_create(kanban_home):
    with kb.connect() as conn:
        architect = kb.create_task(
            conn,
            title="Design workflow",
            assignee="architect",
            mutation_context=_architect_context(),
        )
        claimed = kb.claim_task(conn, architect)
        assert claimed is not None and claimed.current_run_id is not None
        claimed_gate = kb.get_architecture_gate_for_task(conn, architect)
        assert claimed_gate is not None
        architect_policy = kb.latest_run(conn, architect).run_spec["delivery_policy"]
        assert architect_policy == {
            "version": 1,
            "disposition": "enforcing_unresolved",
            "gate_id": claimed_gate.gate_id,
            "architect_task_id": architect,
            "state": "open",
            "row_version": 0,
            "accepted_run_id": None,
            "design_digest": None,
        }
        assert kb.complete_task(
            conn,
            architect,
            metadata=_formal_handoff(),
            expected_run_id=claimed.current_run_id,
        )

        gate = kb.get_architecture_gate_for_task(conn, architect)
        assert gate is not None
        assert gate.state == "policy_accepted"
        assert gate.authorization_event_id is not None
        accepted = kb.accept_architecture_handoff(conn, gate.gate_id)
        assert accepted.state == "policy_accepted"
        assert accepted.design_digest
        assert accepted.accepted_snapshot == kb.canonicalize_architecture_handoff(_formal_handoff())

        implementation = kb.create_task(
            conn,
            title="Implement workflow",
            assignee="coder",
            parents=[architect],
            mutation_context=_implementation_context(),
        )
        claimed_implementation = kb.claim_task(conn, implementation)
        assert claimed_implementation is not None and claimed_implementation.current_run_id is not None
        implementation_policy = kb.latest_run(conn, implementation).run_spec["delivery_policy"]
        assert implementation_policy["disposition"] == "enforcing_approved"
        assert implementation_policy["gate_id"] == accepted.gate_id
        assert implementation_policy["state"] == "policy_accepted"
        assert implementation_policy["row_version"] == accepted.row_version
        assert not kb.complete_task(conn, implementation)
        assert kb.complete_task(
            conn,
            implementation,
            expected_run_id=claimed_implementation.current_run_id,
        )
        completed = kb.get_task(conn, implementation)
        assert completed is not None
        assert completed.status == "done"


def test_architect_invalidation_requires_fresh_acceptance_before_claim(kanban_home):
    with kb.connect() as conn:
        architect = kb.create_task(
            conn,
            title="Design workflow",
            assignee="architect",
            mutation_context=_architect_context(),
        )
        claimed = kb.claim_task(conn, architect)
        assert claimed is not None
        assert kb.complete_task(
            conn,
            architect,
            metadata=_formal_handoff(),
            expected_run_id=claimed.current_run_id,
        )
        gate = kb.get_architecture_gate_for_task(conn, architect)
        kb.accept_architecture_handoff(conn, gate.gate_id)
        kb.invalidate_architecture_gate(conn, gate.gate_id, reason="architect_retry")

        with pytest.raises(kb.ArchitectureGateError, match="architecture_gate_open"):
            kb.create_task(
                conn,
                title="attempt after invalidation",
                assignee="coder",
                mutation_context=_implementation_context(),
            )

        direct = kb.create_task(conn, title="direct child", assignee="coder")
        conn.execute(
            "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)",
            (architect, direct),
        )
        assert kb.claim_task(conn, direct) is None
        assert kb.get_architecture_gate(conn, gate.gate_id).state == "invalidated"


def _human_context(*, surface: str = "cli") -> "kb.MutationContext":
    return kb.MutationContext(
        board_key="default",
        principal="human-1",
        actor_type="human",
        session_id="session-1",
        request_scope_id="turn-1",
        surface=surface,
        mode="enforce",
        phase="approval",
    )


def _awaiting_human_approval(conn, mode: str = "enforce") -> "kb.ArchitectureGate":
    architect = kb.create_task(
        conn,
        title="Design workflow",
        assignee="architect",
        mutation_context=_architect_context(mode),
    )
    claimed = kb.claim_task(conn, architect)
    assert claimed is not None and claimed.current_run_id is not None
    handoff = {**_formal_handoff(), "human_approval_required": True}
    assert kb.complete_task(conn, architect, metadata=handoff, expected_run_id=claimed.current_run_id)
    gate = kb.get_architecture_gate_for_task(conn, architect)
    assert gate is not None
    assert gate.state == "validated_awaiting_approval"
    completed = [event for event in kb.list_events(conn, architect) if event.kind == "completed"][-1]
    assert completed.payload["delivery_withheld"] is True
    assert completed.payload["gate_id"] == gate.gate_id
    assert completed.payload["design_digest"] == gate.design_digest
    assert "chosen_approach" not in json.dumps(completed.payload)
    accepted = kb.accept_architecture_handoff(conn, gate.gate_id)
    assert accepted.state == "validated_awaiting_approval"
    return accepted


def test_malformed_architect_handoff_rolls_back_completion(kanban_home):
    with kb.connect() as conn:
        architect = kb.create_task(
            conn,
            title="Design workflow",
            assignee="architect",
            mutation_context=_architect_context(),
        )
        claimed = kb.claim_task(conn, architect)
        assert claimed is not None and claimed.current_run_id is not None

        with pytest.raises(ValueError, match="missing architecture handoff fields"):
            kb.complete_task(
                conn,
                architect,
                metadata={"role": "architect"},
                expected_run_id=claimed.current_run_id,
            )

        assert kb.get_task(conn, architect).status == "running"
        assert kb.get_architecture_gate_for_task(conn, architect).state == "open"


def test_architect_handoff_ignores_only_trusted_operational_metadata(kanban_home):
    with kb.connect() as conn:
        architect = kb.create_task(
            conn,
            title="Design workflow",
            assignee="architect",
            mutation_context=_architect_context(),
        )
        claimed = kb.claim_task(conn, architect)
        assert claimed is not None and claimed.current_run_id is not None
        metadata = {
            **_formal_handoff(),
            "worker_session_id": "worker-session",
            "model_used": {"provider": "anthropic", "model": "opus"},
            "artifacts": ["/tmp/design.md"],
        }

        assert kb.complete_task(
            conn,
            architect,
            metadata=metadata,
            expected_run_id=claimed.current_run_id,
        )
        gate = kb.get_architecture_gate_for_task(conn, architect)
        assert gate is not None and gate.state == "policy_accepted"
        assert json.loads(gate.accepted_snapshot) == _formal_handoff()


def test_human_approval_requires_authenticated_exact_digest_and_is_idempotent(kanban_home):
    with kb.connect() as conn:
        gate = _awaiting_human_approval(conn)

        with pytest.raises(kb.ArchitectureGateError, match="approval_requires_human"):
            kb.approve_architecture_gate(conn, gate.gate_id, _implementation_context(), gate.design_digest)
        with pytest.raises(kb.ArchitectureGateError, match="approval_surface_not_authenticated"):
            kb.approve_architecture_gate(
                conn, gate.gate_id, _human_context(surface="model"), gate.design_digest,
            )
        with pytest.raises(kb.ArchitectureGateError, match="approval_digest_mismatch"):
            kb.approve_architecture_gate(conn, gate.gate_id, _human_context(), "forged")

        approved = kb.approve_architecture_gate(conn, gate.gate_id, _human_context(), gate.design_digest)
        replay = kb.approve_architecture_gate(conn, gate.gate_id, _human_context(), gate.design_digest)

        assert approved.state == replay.state == "human_approved"
        assert approved.approved_digest == gate.design_digest
        assert replay.row_version == approved.row_version
        assert approved.approval_actor_id == "human-1"


def test_accepted_edit_invalidation_and_wrong_state_deny_human_approval(kanban_home):
    with kb.connect() as conn:
        gate = _awaiting_human_approval(conn)
        kb.invalidate_architecture_gate(conn, gate.gate_id, reason="accepted_edit")

        with pytest.raises(kb.ArchitectureGateError, match="approval_invalidated"):
            kb.approve_architecture_gate(conn, gate.gate_id, _human_context(), gate.design_digest)


def test_human_approved_gate_issues_one_atomic_five_card_graph(kanban_home):
    """The Strava incident cannot create a second implementation graph."""
    with kb.connect() as conn:
        gate = _awaiting_human_approval(conn)
        approved = kb.approve_architecture_gate(
            conn, gate.gate_id, _human_context(), gate.design_digest or "",
        )
        issuer = kb.MutationContext(
            board_key="default",
            principal="orchestrator-session",
            actor_type="orchestrator_agent",
            session_id="session-1",
            request_scope_id="turn-1",
            gate_id=approved.gate_id,
            profile="orchestrator",
            mode="enforce",
            phase="graph_issuance",
        )
        graph = [
            {"title": "Strava ingestion", "assignee": "coder", "parents": []},
            {"title": "Google Drive export", "assignee": "reviewer", "parents": [0]},
            {"title": "verification", "assignee": "verifier", "parents": [1]},
            {"title": "publisher", "assignee": "releaser", "parents": [2]},
        ]

        issued = kb.issue_architecture_graph(
            conn, approved.gate_id, issuer, graph, idempotency_key="strava-incident-v1",
        )
        assert len(issued) == 4
        assert len(kb.child_ids(conn, approved.architect_task_id)) == 1
        task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        link_count = conn.execute("SELECT COUNT(*) FROM task_links").fetchone()[0]

        with pytest.raises(kb.ArchitectureGateError, match="architecture_graph_issued"):
            kb.create_task(
                conn,
                title="direct duplicate",
                assignee="coder",
                mutation_context=kb.MutationContext(
                    **{**issuer.__dict__, "phase": "implementation"}
                ),
            )
        with pytest.raises(kb.ArchitectureGateError, match="architecture_graph_issued"):
            kb.issue_architecture_graph(
                conn, approved.gate_id, issuer, graph, idempotency_key="retry-graph",
            )

        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == task_count
        assert conn.execute("SELECT COUNT(*) FROM task_links").fetchone()[0] == link_count


def test_worker_kanban_create_cannot_append_to_an_issued_graph(kanban_home, monkeypatch):
    with kb.connect() as conn:
        gate = _awaiting_human_approval(conn)
        approved = kb.approve_architecture_gate(
            conn, gate.gate_id, _human_context(), gate.design_digest or "",
        )
        issued = kb.issue_architecture_graph(
            conn,
            approved.gate_id,
            kb.MutationContext(
                board_key="default",
                principal="orchestrator-session",
                actor_type="orchestrator_agent",
                session_id="session-1",
                request_scope_id="turn-1",
                gate_id=approved.gate_id,
                profile="orchestrator",
                mode="enforce",
                phase="graph_issuance",
            ),
            [{"title": "implementation", "assignee": "coder", "parents": []}],
            idempotency_key="issued-worker-graph",
        )
        task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]

    monkeypatch.setenv("HERMES_KANBAN_TASK", issued[0])
    monkeypatch.setenv("HERMES_PROFILE", "coder")
    from tools import kanban_tools as kt

    result = json.loads(kt._handle_create({"title": "duplicate", "assignee": "coder"}))
    assert result["error"].endswith("architecture_graph_issued")
    with kb.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == task_count


def test_auto_decompose_cannot_append_to_an_issued_graph(kanban_home):
    with kb.connect() as conn:
        gate = _awaiting_human_approval(conn)
        approved = kb.approve_architecture_gate(
            conn, gate.gate_id, _human_context(), gate.design_digest or "",
        )
        issued = kb.issue_architecture_graph(
            conn,
            approved.gate_id,
            kb.MutationContext(
                board_key="default", principal="orchestrator-session",
                actor_type="orchestrator_agent", session_id="session-1",
                request_scope_id="turn-1", gate_id=approved.gate_id,
                profile="orchestrator", mode="enforce", phase="graph_issuance",
            ),
            [{"title": "implementation", "assignee": "coder", "parents": []}],
            idempotency_key="issued-auto-decompose-graph",
        )
        triage = kb.create_task(conn, title="late decomposition", triage=True)
        conn.execute(
            "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)", (issued[0], triage),
        )
        task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]

        with pytest.raises(kb.ArchitectureGateError, match="architecture_graph_issued"):
            kb.decompose_triage_task(
                conn, triage, root_assignee="orchestrator",
                children=[{"title": "duplicate", "assignee": "coder", "parents": []}],
            )

        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == task_count


def test_discovery_capability_is_bound_single_use_and_never_allows_protected_work(kanban_home):
    with kb.connect() as conn:
        gate = _awaiting_human_approval(conn)
        capability = kb.issue_discovery_capability(
            conn,
            gate.gate_id,
            _human_context(),
            principal="orchestrator-session",
            session_id="session-1",
            request_scope_id="turn-1",
            profile="scout",
        )
        discovery_context = kb.MutationContext(
            board_key="default",
            principal="orchestrator-session",
            actor_type="orchestrator_agent",
            session_id="session-1",
            request_scope_id="turn-1",
            gate_id=gate.gate_id,
            profile="scout",
            discovery_capability=capability.token,
            mode="enforce",
            phase="discovery",
        )
        discovery = kb.create_task(
            conn, title="Read-only research", assignee="scout", mutation_context=discovery_context,
        )
        assert kb.get_task(conn, discovery).status == "ready"

        with pytest.raises(kb.ArchitectureGateError, match="discovery_capability_used"):
            kb.create_task(
                conn, title="Replay discovery", assignee="scout", mutation_context=discovery_context,
            )

        expired = kb.issue_discovery_capability(
            conn, gate.gate_id, _human_context(), principal="orchestrator-session",
            session_id="session-1", request_scope_id="turn-1", profile="scout",
        )
        conn.execute("UPDATE discovery_capabilities SET expires_at = 0 WHERE token = ?", (expired.token,))
        expired_context = kb.MutationContext(
            **{**discovery_context.__dict__, "discovery_capability": expired.token}
        )
        with pytest.raises(kb.ArchitectureGateError, match="discovery_capability_expired"):
            kb.create_task(conn, title="Expired discovery", assignee="scout", mutation_context=expired_context)

        fresh = kb.issue_discovery_capability(
            conn, gate.gate_id, _human_context(), principal="orchestrator-session",
            session_id="session-1", request_scope_id="turn-1", profile="scout",
        )
        protected = kb.MutationContext(
            **{
                **discovery_context.__dict__,
                "discovery_capability": fresh.token,
                "phase": "implementation",
            }
        )
        with pytest.raises(kb.ArchitectureGateError, match="architecture_gate_open"):
            kb.create_task(conn, title="Forbidden implementation", assignee="coder", mutation_context=protected)


def test_five_card_strava_incident_is_classified_without_any_external_action(kanban_home):
    with kb.connect() as conn:
        cards = [
            kb.create_task(conn, title=title, assignee="coder")
            for title in (
                "Strava ingestion", "Google Drive export", "delivery worker",
                "retry worker", "finalizer",
            )
        ]
        for parent, child in zip(cards, cards[1:]):
            kb.link_tasks(conn, parent, child)
        architect = kb.create_task(
            conn, title="Architect remediation", assignee="architect", mutation_context=_architect_context(),
        )
        kb.link_tasks(conn, architect, cards[0], mutation_context=_architect_context())
        gate = kb.get_architecture_gate_for_task(conn, architect)
        assert gate is not None

        events_before = conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]
        assert {item.task_id for item in kb.classify_policy_quarantine(conn, gate.gate_id)} == set(cards)
        assert conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == events_before
        assert kb.apply_policy_quarantine(
            conn, gate.gate_id, context=_human_context(), signal_fn=lambda *_: None,
        ) == set(cards)
        for card in cards:
            task = kb.get_task(conn, card)
            assert task is not None and task.policy_quarantined


def test_policy_quarantine_dominates_readiness_claim_dependencies_and_stale_completion(kanban_home):
    with kb.connect() as conn:
        premature = kb.create_task(conn, title="Premature implementation", assignee="coder")
        claimed = kb.claim_task(conn, premature)
        assert claimed is not None and claimed.current_run_id is not None
        descendant = kb.create_task(conn, title="Premature finalizer", assignee="reviewer", parents=[premature])
        architect = kb.create_task(
            conn, title="Design workflow", assignee="architect", mutation_context=_architect_context(),
        )
        kb.link_tasks(conn, architect, premature, mutation_context=_architect_context())

        gate = kb.get_architecture_gate_for_task(conn, architect)
        assert gate is not None
        classified = kb.classify_policy_quarantine(conn, gate.gate_id)
        assert {item.task_id for item in classified} == {premature, descendant}
        assert kb.apply_policy_quarantine(
            conn, gate.gate_id, context=_human_context(), signal_fn=lambda *_: None,
        ) == {premature, descendant}

        assert kb.claim_task(conn, descendant) is None
        assert not kb.complete_task(conn, premature, expected_run_id=claimed.current_run_id)
        assert kb.get_task(conn, premature).policy_quarantined
        assert kb.get_task(conn, descendant).policy_quarantined

        row_count_before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        with pytest.raises(kb.ArchitectureGateError, match="architecture_gate_open"):
            kb.create_task(
                conn,
                title="Depends on invalid work",
                assignee="coder",
                parents=[premature],
            )
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == row_count_before
        assert kb.recompute_ready(conn) == 0


def test_gate_open_rejects_matching_running_ungated_run(kanban_home, monkeypatch):
    """A new gate cannot retroactively replace an active RunSpec contract."""
    with kb.connect() as conn:
        dispatcher_task = kb.create_task(
            conn,
            title="Orchestrate five-card remediation",
            assignee="orchestrator",
            session_id="session-1",
            workflow_key="incident-5",
        )
        claimed = kb.claim_task(conn, dispatcher_task)
        assert claimed is not None and claimed.current_run_id is not None
        monkeypatch.setenv("HERMES_KANBAN_TASK", dispatcher_task)
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))
        monkeypatch.setenv(
            "HERMES_KANBAN_DELIVERY_POLICY",
            json.dumps(kb.latest_run(conn, dispatcher_task).run_spec["delivery_policy"]),
        )
        policy = policy_for_current_kanban_task()
        assert policy is not None and not policy.withholding

        with pytest.raises(
            kb.ArchitectureGateError,
            match="architecture_gate_running_ungated_run",
        ):
            kb.create_task(
                conn,
                title="Architect the incident",
                assignee="architect",
                session_id="session-1",
                workflow_key="incident-5",
                mutation_context=kb.MutationContext(
                    board_key="default",
                    principal="orchestrator-session",
                    actor_type="orchestrator_agent",
                    session_id="session-1",
                    request_scope_id="incident-turn",
                    workflow_key="incident-5",
                    mode="orchestrator_only",
                    phase="architecture",
                ),
            )

        assert policy.stream_delta("still authorized") == "still authorized"
        assert not policy.withholding


def test_scope_gate_opened_before_claim_blocks_ready_and_review_workers(
    kanban_home,
):
    """Claim enforcement and RunSpec snapshot share the scope resolver."""
    with kb.connect() as conn:
        ready = kb.create_task(
            conn,
            title="Scoped implementation",
            assignee="coder",
            session_id="session-claim-race",
            workflow_key="workflow-claim-race",
        )
        review = kb.create_task(
            conn,
            title="Scoped review",
            assignee="reviewer",
            session_id="session-claim-race",
            workflow_key="workflow-claim-race",
        )
        conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (review,))
        architect = kb.create_task(
            conn,
            title="Authorize scoped workflow",
            assignee="architect",
            session_id="session-claim-race",
            workflow_key="workflow-claim-race",
            mutation_context=kb.MutationContext(
                board_key="default",
                principal="orchestrator:claim-race",
                actor_type="orchestrator_agent",
                profile="orchestrator",
                session_id="session-claim-race",
                request_scope_id="front-door:claim-race",
                workflow_key="workflow-claim-race",
                mode="orchestrator_only",
                phase="architecture",
            ),
        )
        gate = kb.get_architecture_gate_for_task(conn, architect)
        assert gate is not None and gate.state == "open"

        assert kb.claim_task(conn, ready) is None
        assert kb.get_task(conn, ready).status == "todo"
        assert kb.latest_run(conn, ready) is None
        assert kb.claim_review_task(conn, review) is None
        assert kb.get_task(conn, review).status == "review"
        assert kb.latest_run(conn, review) is None


def test_terminal_workflow_gate_does_not_poison_other_workflow_in_session(
    kanban_home,
):
    with kb.connect() as conn:
        architect = kb.create_task(
            conn,
            title="Authorize workflow A",
            assignee="architect",
            session_id="long-lived-session",
            workflow_key="workflow-a",
            mutation_context=kb.MutationContext(
                board_key="default",
                principal="orchestrator:workflow-a",
                actor_type="orchestrator_agent",
                profile="orchestrator",
                session_id="long-lived-session",
                request_scope_id="front-door:workflow-a",
                workflow_key="workflow-a",
                mode="orchestrator_only",
                phase="architecture",
            ),
        )
        gate = kb.get_architecture_gate_for_task(conn, architect)
        kb.invalidate_architecture_gate(conn, gate.gate_id, reason="replan A")

        same_workflow = kb.create_task(
            conn,
            title="Still belongs to A",
            assignee="coder",
            session_id="long-lived-session",
            workflow_key="workflow-a",
        )
        other_workflow = kb.create_task(
            conn,
            title="Independent workflow B",
            assignee="coder",
            session_id="long-lived-session",
            workflow_key="workflow-b",
        )

        assert kb.claim_task(conn, same_workflow) is None
        claimed_b = kb.claim_task(conn, other_workflow)
        assert claimed_b is not None and claimed_b.current_run_id is not None
        assert (
            kb.latest_run(conn, other_workflow)
            .run_spec["delivery_policy"]["disposition"]
            == "none"
        )


def test_approved_run_latches_authority_epoch_change(kanban_home, monkeypatch):
    with kb.connect() as conn:
        architect = kb.create_task(
            conn,
            title="Design workflow",
            assignee="architect",
            mutation_context=_architect_context(),
        )
        architect_run = kb.claim_task(conn, architect)
        assert architect_run is not None and architect_run.current_run_id is not None
        assert kb.complete_task(
            conn,
            architect,
            metadata=_formal_handoff(),
            expected_run_id=architect_run.current_run_id,
        )
        implementation = kb.create_task(
            conn,
            title="Implement approved workflow",
            assignee="coder",
            parents=[architect],
            mutation_context=_implementation_context(),
        )
        claimed = kb.claim_task(conn, implementation)
        assert claimed is not None and claimed.current_run_id is not None
        delivery = kb.latest_run(conn, implementation).run_spec["delivery_policy"]
        monkeypatch.setenv("HERMES_KANBAN_TASK", implementation)
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))
        monkeypatch.setenv("HERMES_KANBAN_DELIVERY_POLICY", json.dumps(delivery))
        policy = policy_for_current_kanban_task()
        assert policy is not None and not policy.withholding

        gate = kb.get_architecture_gate_for_task(conn, implementation)
        conn.execute(
            "UPDATE architecture_gates SET row_version = row_version + 1 "
            "WHERE gate_id = ?",
            (gate.gate_id,),
        )
        assert "output withheld" in str(policy.tool_result("private"))
        assert policy.authorization_conflict

        # A later projection change cannot reauthorize the same immutable run.
        conn.execute(
            "UPDATE architecture_gates SET row_version = ? WHERE gate_id = ?",
            (delivery["row_version"], gate.gate_id),
        )
        assert "output withheld" in str(policy.tool_result("still private"))


def test_old_approved_epoch_cannot_complete_after_reacceptance(kanban_home):
    with kb.connect() as conn:
        architect = kb.create_task(
            conn,
            title="Design workflow",
            assignee="architect",
            mutation_context=_architect_context(),
        )
        first_architect = kb.claim_task(conn, architect)
        assert first_architect is not None
        first_handoff = _formal_handoff()
        assert kb.complete_task(
            conn,
            architect,
            metadata=first_handoff,
            expected_run_id=first_architect.current_run_id,
        )
        first_gate = kb.get_architecture_gate_for_task(conn, architect)
        first_digest = first_gate.design_digest

        implementation = kb.create_task(
            conn,
            title="Implement epoch A",
            assignee="coder",
            parents=[architect],
            mutation_context=_implementation_context(),
        )
        old_run = kb.claim_task(conn, implementation)
        assert old_run is not None and old_run.current_run_id is not None

        kb.invalidate_architecture_gate(conn, first_gate.gate_id, reason="new design")
        kb.reopen_architecture_gate(conn, first_gate.gate_id, _architect_context())
        second_architect = kb.claim_task(conn, architect)
        assert second_architect is not None
        second_handoff = {
            **first_handoff,
            "chosen_approach": "Use a materially different epoch B design.",
        }
        assert kb.complete_task(
            conn,
            architect,
            metadata=second_handoff,
            expected_run_id=second_architect.current_run_id,
        )
        second_gate = kb.get_architecture_gate_for_task(conn, architect)
        assert second_gate.design_digest != first_digest

        assert not kb.complete_task(
            conn,
            implementation,
            summary="OLD A OUTPUT",
            expected_run_id=old_run.current_run_id,
        )
        assert kb.get_task(conn, implementation).status == "running"
        assert not any(
            event.kind == "completed" and "OLD A OUTPUT" in json.dumps(event.payload)
            for event in kb.list_events(conn, implementation)
        )
        assert any(
            event.kind == "completion_blocked"
            and event.payload.get("reason") == "delivery_authority_epoch_mismatch"
            for event in kb.list_events(conn, implementation)
        )


def test_authorization_lookup_outage_requeues_without_failure_count(
    kanban_home, monkeypatch,
):
    with kb.connect() as conn:
        architect = kb.create_task(
            conn,
            title="Design workflow",
            assignee="architect",
            mutation_context=_architect_context(),
        )
        architect_run = kb.claim_task(conn, architect)
        assert architect_run is not None and architect_run.current_run_id is not None
        assert kb.complete_task(
            conn,
            architect,
            metadata=_formal_handoff(),
            expected_run_id=architect_run.current_run_id,
        )
        implementation = kb.create_task(
            conn,
            title="Implement approved workflow",
            assignee="coder",
            parents=[architect],
            mutation_context=_implementation_context(),
        )
        claimed = kb.claim_task(conn, implementation)
        assert claimed is not None and claimed.current_run_id is not None
        run_id = claimed.current_run_id
        delivery = kb.latest_run(conn, implementation).run_spec["delivery_policy"]

    monkeypatch.setenv("HERMES_KANBAN_TASK", implementation)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    monkeypatch.setenv("HERMES_KANBAN_DELIVERY_POLICY", json.dumps(delivery))
    monkeypatch.setattr(
        kb,
        "get_delivery_architecture_gate",
        lambda *_args: (_ for _ in ()).throw(OSError("resolver unavailable")),
    )
    policy = policy_for_current_kanban_task()
    assert policy is not None and policy.requires_kernel_requeue
    assert requeue_current_task_for_delivery_authorization(policy)

    with kb.connect() as conn:
        task = kb.get_task(conn, implementation)
        run = kb.get_run(conn, run_id)
        assert task.status == "ready"
        assert task.consecutive_failures == 0
        assert run.outcome == "delivery_authorization_unavailable"
        assert kb.check_respawn_guard(conn, implementation) == (
            "delivery_authorization_cooldown"
        )
        assert any(
            event.kind == "delivery_authorization_unavailable"
            for event in kb.list_events(conn, implementation)
        )


def test_authorized_graph_and_post_approval_descendants_are_not_quarantined(kanban_home):
    with kb.connect() as conn:
        gate = _awaiting_human_approval(conn)
        approved = kb.approve_architecture_gate(
            conn, gate.gate_id, _human_context(), gate.design_digest or "",
        )
        issued = kb.issue_architecture_graph(
            conn,
            approved.gate_id,
            kb.MutationContext(
                board_key="default", principal="orchestrator-session",
                actor_type="orchestrator_agent", session_id="session-1",
                request_scope_id="turn-1", gate_id=approved.gate_id,
                profile="orchestrator", mode="orchestrator_only", phase="graph_issuance",
            ),
            [
                {"title": "implementation", "assignee": "coder", "parents": []},
                {"title": "Google configuration", "assignee": "coder", "parents": [0]},
                {"title": "verification", "assignee": "verifier", "parents": [1]},
                {"title": "finalizer", "assignee": "releaser", "parents": [2]},
            ],
            idempotency_key="incident-five-card-v1",
        )
        assert len(issued) == 4
        assert kb.classify_policy_quarantine(conn, approved.gate_id) == []


def test_invalidated_handoff_reopens_clears_authority_and_revalidates_new_run(kanban_home):
    with kb.connect() as conn:
        architect = kb.create_task(conn, title="Design workflow", assignee="architect", mutation_context=_architect_context())
        first = kb.claim_task(conn, architect)
        assert first is not None and first.current_run_id is not None
        assert kb.complete_task(conn, architect, metadata=_formal_handoff(), expected_run_id=first.current_run_id)
        gate = kb.get_architecture_gate_for_task(conn, architect)
        assert gate is not None
        accepted = kb.accept_architecture_handoff(conn, gate.gate_id)
        kb.invalidate_architecture_gate(conn, accepted.gate_id, reason="retry")
        reopened = kb.reopen_architecture_gate(conn, accepted.gate_id, _architect_context())
        assert reopened.state == "open"
        assert reopened.accepted_run_id is None and reopened.accepted_snapshot is None
        assert reopened.design_digest is None and reopened.approval_actor_id is None
        assert reopened.authorization_event_id is None
        assert kb.get_task(conn, architect).status == "ready"
        second = kb.claim_task(conn, architect)
        assert second is not None and second.current_run_id != first.current_run_id
        updated = {**_formal_handoff(), "chosen_approach": "Use a fresh retry snapshot."}
        assert kb.complete_task(conn, architect, metadata=updated, expected_run_id=second.current_run_id)
        revalidated = kb.accept_architecture_handoff(conn, accepted.gate_id)
        assert revalidated.accepted_run_id == second.current_run_id


def test_invalidated_handoff_reopen_requires_owning_architecture_context(kanban_home):
    with kb.connect() as conn:
        architect = kb.create_task(conn, title="Design workflow", assignee="architect", mutation_context=_architect_context())
        gate = kb.get_architecture_gate_for_task(conn, architect)
        assert gate is not None
        kb.invalidate_architecture_gate(conn, gate.gate_id, reason="retry")
        with pytest.raises(kb.ArchitectureGateError, match="architecture_gate_reopen_requires_owner"):
            kb.reopen_architecture_gate(conn, gate.gate_id, _implementation_context())
        reopened = kb.reopen_architecture_gate(conn, gate.gate_id, _architect_context())
        assert reopened.state == "open" and reopened.row_version == gate.row_version + 2
        assert kb.reopen_architecture_gate(conn, gate.gate_id, _architect_context()).row_version == reopened.row_version


def test_invalidated_handoff_reopen_requires_all_persisted_owner_bindings(kanban_home):
    owner = kb.MutationContext(
        **{
            **_architect_context().__dict__,
            "workflow_key": "workflow-1",
            "profile": "architect",
        }
    )
    with kb.connect() as conn:
        architect = kb.create_task(
            conn, title="Design workflow", assignee="architect", mutation_context=owner,
        )
        gate = kb.get_architecture_gate_for_task(conn, architect)
        assert gate is not None
        kb.invalidate_architecture_gate(conn, gate.gate_id, reason="retry")

        for binding, forged in (
            ("actor_type", "other_actor"),
            ("profile", "other_profile"),
            ("session_id", "other_session"),
            ("workflow_key", "other_workflow"),
            ("request_scope_id", "other_turn"),
        ):
            forged_owner = kb.MutationContext(**{**owner.__dict__, binding: forged})
            with pytest.raises(kb.ArchitectureGateError, match="architecture_gate_reopen_requires_owner"):
                kb.reopen_architecture_gate(conn, gate.gate_id, forged_owner)

        assert kb.reopen_architecture_gate(conn, gate.gate_id, owner).state == "open"


def test_accepted_architect_edit_invalidates_within_owning_mutation(kanban_home):
    with kb.connect() as conn:
        architect = kb.create_task(
            conn, title="Design workflow", assignee="architect", mutation_context=_architect_context(),
        )
        claimed = kb.claim_task(conn, architect)
        assert claimed is not None and claimed.current_run_id is not None
        assert kb.complete_task(
            conn, architect, metadata=_formal_handoff(), expected_run_id=claimed.current_run_id,
        )
        gate = kb.get_architecture_gate_for_task(conn, architect)
        assert gate is not None
        accepted = kb.accept_architecture_handoff(conn, gate.gate_id)
        assert accepted.state == "policy_accepted"

        assert kb.edit_completed_task_result(conn, architect, result="corrected handoff")
        reopened = kb.get_architecture_gate(conn, gate.gate_id)
        assert reopened is not None and reopened.state == "open"
        assert kb.get_task(conn, architect).status == "ready"


# ---------------------------------------------------------------------------
# BUILD-800 — a dispatcher worker must not be able to forge a human approval.
# ---------------------------------------------------------------------------


def test_dispatcher_worker_cannot_forge_human_gate_approval(kanban_home, monkeypatch):
    """The BUILD-800 reproduction, verbatim.

    A process marked as a dispatcher worker holds a writable board handle, so it
    can read ``gate.design_digest`` straight off the board and hand in a
    ``MutationContext`` claiming ``actor_type="human"`` on an authenticated
    surface. Every field the old check consulted was therefore either
    self-asserted or board-readable.
    """
    with kb.connect() as conn:
        gate = _awaiting_human_approval(conn)

        # Exactly what the dispatcher sets on a worker (`_default_spawn`).
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_pretend_worker")
        monkeypatch.setenv("HERMES_PROFILE", "coder")

        # The digest is an integrity binding, not a secret: the worker reads it.
        stolen_digest = kb.get_architecture_gate(conn, gate.gate_id).design_digest
        assert stolen_digest == gate.design_digest

        for forged in (_human_context(), _human_context(surface="dashboard")):
            with pytest.raises(
                kb.ArchitectureGateError, match="approval_surface_is_dispatcher_worker"
            ):
                kb.approve_architecture_gate(conn, gate.gate_id, forged, stolen_digest)
            with pytest.raises(
                kb.ArchitectureGateError, match="approval_surface_is_dispatcher_worker"
            ):
                kb.reject_architecture_gate(conn, gate.gate_id, forged, stolen_digest)
            with pytest.raises(
                kb.ArchitectureGateError, match="approval_surface_is_dispatcher_worker"
            ):
                kb.issue_discovery_capability(
                    conn,
                    gate.gate_id,
                    forged,
                    principal="human-1",
                    session_id="session-1",
                    request_scope_id="turn-1",
                    profile="researcher",
                )
            with pytest.raises(
                kb.ArchitectureGateError, match="approval_surface_is_dispatcher_worker"
            ):
                kb.apply_policy_quarantine(
                    conn, gate.gate_id, context=forged, signal_fn=lambda *_: None,
                )

        # Graph issuance is authorized on a different triple, and is forgeable
        # the same way, so it carries the same board-side check.
        with pytest.raises(
            kb.ArchitectureGateError, match="approval_surface_is_dispatcher_worker"
        ):
            kb.issue_architecture_graph(
                conn,
                gate.gate_id,
                kb.MutationContext(
                    board_key="default",
                    principal="orchestrator-session",
                    actor_type="orchestrator_agent",
                    profile="orchestrator",
                    session_id="session-1",
                    request_scope_id="turn-1",
                    mode="enforce",
                    phase="graph_issuance",
                ),
                [{"title": "implement", "assignee": "coder"}],
                idempotency_key="forged",
            )

        assert kb.get_architecture_gate(conn, gate.gate_id).state == (
            "validated_awaiting_approval"
        )

        # AC3: the genuine human path is untouched — same context, same digest,
        # from a process with no worker provenance.
        monkeypatch.delenv("HERMES_KANBAN_TASK")
        approved = kb.approve_architecture_gate(
            conn, gate.gate_id, _human_context(), stolen_digest,
        )
        assert approved.state == "human_approved"
        replay = kb.approve_architecture_gate(
            conn, gate.gate_id, _human_context(), stolen_digest,
        )
        assert replay.row_version == approved.row_version


def test_gate_approval_refused_from_a_live_worker_session_without_the_env_marker(
    kanban_home, monkeypatch,
):
    """Scrubbing ``HERMES_KANBAN_TASK`` is not enough to shed worker provenance.

    Workers are spawned session leaders, so every process a worker starts shares
    its session id, and the board records that id as ``tasks.worker_sid``. The
    check is therefore derived from the board's own record of live workers
    rather than from the environment alone.
    """
    import os

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    with kb.connect() as conn:
        gate = _awaiting_human_approval(conn)
        digest = gate.design_digest

        # A running worker whose session id is this process's session id.
        victim = kb.create_task(conn, title="worker work", assignee="coder")
        claimed = kb.claim_task(conn, victim)
        assert claimed is not None
        conn.execute(
            "UPDATE tasks SET worker_pid = ?, worker_sid = ? WHERE id = ?",
            (os.getpid(), os.getsid(0), victim),
        )
        conn.commit()

        with pytest.raises(
            kb.ArchitectureGateError, match="approval_surface_is_dispatcher_worker"
        ):
            kb.approve_architecture_gate(conn, gate.gate_id, _human_context(), digest)

        # The binding is to a *live* worker: once the run is no longer running,
        # the same process is an ordinary operator shell again.
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (victim,))
        conn.commit()
        assert kb.approve_architecture_gate(
            conn, gate.gate_id, _human_context(), digest,
        ).state == "human_approved"


# ---------------------------------------------------------------------------
# BUILD-818 — the two paths a shadow soak cannot exercise
#
# `_link_tasks_in_txn` is the one deliberate exception to "no MutationContext
# => no gate evaluation": a context-less caller is *refused* on a gated
# subtree.  It cannot fire in `shadow`, because `enforcement_mode` is stamped
# on the gate row when the gate opens and shadow rows are not in
# ARCHITECTURE_GATE_ENFORCING_MODES.  So the BUILD-382 flip is the first
# production execution of every assertion below.
# ---------------------------------------------------------------------------


def _gated_subtree(conn, mode: str = "orchestrator_only", scope: str = "turn-1"):
    """An architect card with a live gate plus one card below it.

    The descendant is wired with raw SQL on purpose: `create_task(parents=…)`
    is itself gate-checked, so building the fixture through it would test the
    create path rather than the link path.

    `scope` must differ between two gates in one test: an architecture-phase
    create inside an existing (board_key, principal, request_scope_id) scope
    returns that gate's architect card rather than opening a second gate.
    """
    context = kb.MutationContext(
        **{**_architect_context(mode).__dict__, "request_scope_id": scope}
    )
    architect = kb.create_task(
        conn,
        title="Design workflow",
        assignee="architect",
        mutation_context=context,
    )
    gate = kb.get_architecture_gate_for_task(conn, architect)
    assert gate is not None and gate.enforcement_mode == mode
    descendant = kb.create_task(conn, title="already under the gate", assignee="coder")
    conn.execute(
        "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)",
        (architect, descendant),
    )
    return architect, descendant, gate


def _counts(conn) -> tuple[int, int]:
    return (
        conn.execute("SELECT COUNT(*) FROM task_links").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0],
    )


def test_context_less_link_into_a_gated_subtree_is_refused_under_orchestrator_only(
    kanban_home,
):
    """The decision is: refuse, in both directions, through the ancestry walk.

    A caller that cannot present a `MutationContext` cannot prove it is the
    front door, and a link is the one mutation that attaches arbitrary
    existing work to a gated subtree without creating anything.  Fail closed.
    """
    with kb.connect() as conn:
        architect, descendant, _gate = _gated_subtree(conn)
        outsider = kb.create_task(conn, title="unrelated work", assignee="coder")
        links_before, events_before = _counts(conn)

        # gated task as parent
        with pytest.raises(kb.ArchitectureGateError, match="architecture_gate_open"):
            kb.link_tasks(conn, architect, outsider)
        # gated task as child — the lookup checks both endpoints
        with pytest.raises(kb.ArchitectureGateError, match="architecture_gate_open"):
            kb.link_tasks(conn, outsider, architect)
        # a descendant of the gated card resolves the same gate by ancestry
        with pytest.raises(kb.ArchitectureGateError, match="architecture_gate_open"):
            kb.link_tasks(conn, descendant, outsider)

        # Nothing was written, and — measured, not assumed — the refusal
        # leaves no audit trail: the caller owns the write_txn, so an audit
        # row appended before the raise would roll back with it.  During the
        # flip window a denial is visible at the calling surface only.
        assert _counts(conn) == (links_before, events_before)

        # An ungated pair is untouched.
        kb.link_tasks(conn, outsider, kb.create_task(conn, title="sibling", assignee="coder"))


def test_a_shadow_gate_cannot_exercise_the_context_less_link_refusal(kanban_home):
    """Why a clean shadow soak says nothing about the path above.

    `enforcement_mode` is written once, at gate creation, from the mode in
    force at that moment, and no code path updates it.  A gate opened while
    the policy said `shadow` therefore never enforces — not after the flip,
    not ever — and a shadow soak cannot produce the denial.
    """
    with kb.connect() as conn:
        architect, descendant, gate = _gated_subtree(conn, mode="shadow")
        outsider = kb.create_task(conn, title="unrelated work", assignee="coder")
        _links_before, events_before = _counts(conn)

        kb.link_tasks(conn, architect, outsider)
        kb.link_tasks(conn, descendant, outsider)

        # set(): parent_ids orders by the random task id, not insertion order.
        assert set(kb.parent_ids(conn, outsider)) == {architect, descendant}
        # The shadow *audit* is also silent here: `create_allowed` is only
        # written on the context-carrying branch.
        assert _counts(conn)[1] == events_before + 2  # two `linked` events, no audit
        assert not [
            event
            for event in kb.list_events(conn, architect)
            if event.kind == "create_allowed"
        ]

        # The sibling fail-closed rule on the create path is mode-gated the
        # same way: a context-less create with a gated parent is permitted
        # under shadow and refused under `orchestrator_only`.
        child = kb.create_task(
            conn, title="context-less child", assignee="coder", parents=[architect],
        )
        assert kb.parent_ids(conn, child) == [architect]

        # Opening an enforcing gate elsewhere on the same board does not
        # retroactively arm the shadow row.
        _gated_subtree(conn, mode="orchestrator_only", scope="turn-2")
        assert kb.get_architecture_gate(conn, gate.gate_id).enforcement_mode == "shadow"
        kb.link_tasks(conn, architect, kb.create_task(conn, title="still fine", assignee="coder"))


def test_a_shadow_gate_allows_a_context_less_link_even_after_graph_issuance(kanban_home):
    """The second half of "shadow proves nothing here".

    Both refusals on the context-less branch are mode-gated, not just the
    open-gate one: a shadow gate whose graph has already been issued still
    permits the link that `orchestrator_only` refuses as
    `architecture_graph_issued`.
    """
    with kb.connect() as conn:
        architect = kb.create_task(
            conn, title="Design workflow", assignee="architect",
            mutation_context=_architect_context("shadow"),
        )
        claimed = kb.claim_task(conn, architect)
        assert kb.complete_task(
            conn, architect,
            metadata={**_formal_handoff(), "human_approval_required": True},
            expected_run_id=claimed.current_run_id,
        )
        # Under `shadow` completion leaves the gate `open`; acceptance is what
        # moves it on. Under `enforce` completion moves it directly.
        gate = kb.accept_architecture_handoff(
            conn, kb.get_architecture_gate_for_task(conn, architect).gate_id,
        )
        assert gate.state == "validated_awaiting_approval"
        approved = kb.approve_architecture_gate(
            conn, gate.gate_id, _human_context(), gate.design_digest or "",
        )
        issued = kb.issue_architecture_graph(
            conn,
            approved.gate_id,
            kb.MutationContext(
                board_key="default", principal="orchestrator-session",
                actor_type="orchestrator_agent", session_id="session-1",
                request_scope_id="turn-1", gate_id=approved.gate_id,
                profile="orchestrator", mode="shadow", phase="graph_issuance",
            ),
            [{"title": "implementation", "assignee": "coder", "parents": []}],
            idempotency_key="issued-shadow-graph",
        )
        outsider = kb.create_task(conn, title="late addition", assignee="coder")

        kb.link_tasks(conn, issued[0], outsider)

        assert kb.parent_ids(conn, outsider) == [issued[0]]

        # And the general contract behind it: `shadow` never denies. The same
        # protected, context-carrying create that `orchestrator_only` refuses
        # is permitted here — only the audit row records the would-deny.
        permitted = kb.create_task(
            conn,
            title="protected work",
            assignee="coder",
            mutation_context=kb.MutationContext(
                **{**_architect_context("shadow").__dict__, "phase": "implementation"}
            ),
        )
        assert kb.get_task(conn, permitted) is not None


def test_dispatcher_worker_kanban_link_is_refused_under_orchestrator_only(
    kanban_home, monkeypatch,
):
    """BUILD-818 AC2, at the real surface.

    `kanban_link` builds no `MutationContext` for anyone — not for a worker
    and not for the front door — so every call it makes lands on the
    context-less branch.  A dispatcher worker attaching its own follow-up work
    to a gated subtree is therefore refused.
    """
    with kb.connect() as conn:
        architect, descendant, _gate = _gated_subtree(conn)
        follow_up = kb.create_task(conn, title="worker follow-up", assignee="coder")
        links_before, _events_before = _counts(conn)

    monkeypatch.setenv("HERMES_KANBAN_TASK", descendant)
    monkeypatch.setenv("HERMES_PROFILE", "coder")
    from tools import kanban_tools as kt

    result = json.loads(
        kt._handle_link({"parent_id": descendant, "child_id": follow_up})
    )
    assert result["error"].endswith("architecture_gate_open")

    with kb.connect() as conn:
        assert _counts(conn)[0] == links_before
        assert kb.parent_ids(conn, follow_up) == []


def test_context_less_link_after_graph_issuance_is_refused_as_issued(kanban_home):
    """The second context-less refusal: an issued graph is closed to edits."""
    with kb.connect() as conn:
        gate = _awaiting_human_approval(conn, mode="orchestrator_only")
        approved = kb.approve_architecture_gate(
            conn, gate.gate_id, _human_context(), gate.design_digest or "",
        )
        issued = kb.issue_architecture_graph(
            conn,
            approved.gate_id,
            kb.MutationContext(
                board_key="default", principal="orchestrator-session",
                actor_type="orchestrator_agent", session_id="session-1",
                request_scope_id="turn-1", gate_id=approved.gate_id,
                profile="orchestrator", mode="orchestrator_only",
                phase="graph_issuance",
            ),
            [{"title": "implementation", "assignee": "coder", "parents": []}],
            idempotency_key="issued-link-graph",
        )
        outsider = kb.create_task(conn, title="late addition", assignee="coder")
        links_before, _events_before = _counts(conn)

        with pytest.raises(kb.ArchitectureGateError, match="architecture_graph_issued"):
            kb.link_tasks(conn, issued[0], outsider)

        assert _counts(conn)[0] == links_before


def test_vault_doc_impact_rewire_survives_a_gate_refusal_without_orphaning(kanban_home):
    """The rewire adds before it removes, so a refused link changes nothing.

    `_rewire_parents_to_gate` used to delete every parent edge of the
    finalizer first.  A refusal on the following link — which an enforcing
    architecture gate produces for this context-less caller — left the
    finalizer with no parents at all.  Nothing would have reported it:
    `ArchitectureGateError` subclasses `ValueError`, so the rewire's own
    `except ValueError` swallows it, and `kanban_create` swallows whatever
    escapes at debug level.
    """
    from hermes_cli import kanban_vault_doc_impact as vdi

    with kb.connect() as conn:
        upstream = kb.create_task(conn, title="upstream", assignee="coder")
        finalizer = kb.create_task(
            conn, title="finalizer", assignee="reviewer", parents=[upstream],
        )
        curator = kb.create_task(conn, title="doc impact", assignee="vault-v2-curator")

        calls: list[tuple[str, str]] = []
        real_link = kb.link_tasks

        def refusing_link(conn_, parent_id, child_id, **kw):
            calls.append((parent_id, child_id))
            if child_id == finalizer:
                raise kb.ArchitectureGateError("architecture_gate_open")
            return real_link(conn_, parent_id, child_id, **kw)

        vdi.kb.link_tasks = refusing_link
        try:
            vdi._rewire_parents_to_gate(conn, kb.get_task(conn, finalizer), curator)
        finally:
            vdi.kb.link_tasks = real_link

        # The finalizer keeps its original parent: the refusal happened before
        # anything was removed.  With the old unlink-first order this is [].
        assert kb.parent_ids(conn, finalizer) == [upstream]
        assert (curator, finalizer) in calls
