"""Contracts for ``hermes kanban gate`` -- the architecture-gate operator surface.

Before BUILD-791 the gate could be OPENED but never resolved: approve, reject,
invalidate, reopen and graph issuance had zero callers outside ``kanban_db``.
An ``orchestrator_only`` gate was therefore a one-way trap, and the documented
"roll the policy back to shadow" escape does not work because
``enforcement_mode`` is persisted per gate ROW.
"""

from __future__ import annotations

import argparse
import json

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    from pathlib import Path

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # A gate action must never be mistaken for a worker action by default.
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    kb.init_db()
    return home


def _architect_context() -> "kb.MutationContext":
    return kb.MutationContext(
        board_key="default",
        principal="orchestrator-session",
        actor_type="orchestrator_agent",
        session_id="session-1",
        request_scope_id="turn-1",
        mode="enforce",
        phase="architecture",
    )


def _implementation_context() -> "kb.MutationContext":
    return kb.MutationContext(
        **{**_architect_context().__dict__, "phase": "implementation"}
    )


def _formal_handoff(*, human_approval_required: bool) -> dict:
    return {
        "role": "architect",
        "design_depth": "formal",
        "chosen_approach": "Use one transactional gate projection.",
        "alternatives_rejected": ["prompt-only guard"],
        "slices": [{"name": "core", "verification": ["focused test"]}],
        "acceptance_criteria": ["protected work is denied before approval"],
        "verification_plan": ["run focused tests"],
        "human_approval_required": human_approval_required,
        "rollout": {"mode": "shadow"},
        "rollback": {"mode": "off"},
    }


def _open_awaiting_approval(conn) -> "kb.ArchitectureGate":
    architect = kb.create_task(
        conn, title="Design workflow", assignee="architect",
        mutation_context=_architect_context(),
    )
    claimed = kb.claim_task(conn, architect)
    assert kb.complete_task(
        conn, architect,
        metadata=_formal_handoff(human_approval_required=True),
        expected_run_id=claimed.current_run_id,
    )
    gate = kb.get_architecture_gate_for_task(conn, architect)
    assert gate is not None and gate.state == "validated_awaiting_approval"
    return gate


def _args(**kw) -> argparse.Namespace:
    base = {
        "json": False, "operator": None, "state": None,
        "review_task_id": None, "review_completion_event_id": None,
        "artifact_generation": None, "artifact_sha256": None,
    }
    return argparse.Namespace(**{**base, **kw})


def _run(action: str, **kw) -> int:
    """Drive the dispatcher the way argparse would, capturing its exit code."""
    with pytest.raises(SystemExit) as excinfo:
        kc._dispatch_gate(_args(gate_action=action, **kw))
    return int(excinfo.value.code or 0)


# ---------------------------------------------------------------------------
# AC1 -- approval releases a write that the open gate denied
# ---------------------------------------------------------------------------

def test_approve_then_issue_releases_the_work_the_open_gate_denied(
    kanban_home, tmp_path, capsys,
):
    """Approval alone does not release free-form writes -- issuance does.

    An approved gate deliberately still refuses an ad-hoc implementation card
    with ``architecture_graph_issuance_required``; the authorized work must
    arrive as the one issued graph. Both halves are asserted so a future change
    that lets approval alone unlock ``create_task`` fails here.
    """
    with kb.connect_closing() as conn:
        gate = _open_awaiting_approval(conn)
        digest = gate.design_digest

        # The exact denial this ticket exists to escape.
        with pytest.raises(kb.ArchitectureGateError, match="architecture_gate_open"):
            kb.create_task(
                conn, title="implementation", assignee="coder",
                mutation_context=_implementation_context(),
            )

    assert _run("approve", gate_id=gate.gate_id, digest=digest) == 0
    assert "approved by" in capsys.readouterr().out

    with kb.connect_closing() as conn:
        assert kb.get_architecture_gate(conn, gate.gate_id).state == "human_approved"
        # Still not a free-for-all: the release path is graph issuance.
        with pytest.raises(
            kb.ArchitectureGateError, match="architecture_graph_issuance_required"
        ):
            kb.create_task(
                conn, title="implementation", assignee="coder",
                mutation_context=_implementation_context(),
            )

    assert _run("issue-graph", gate_id=gate.gate_id,
                graph_file=_graph_file(tmp_path), idempotency_key="release-v1") == 0
    assert "issued 2 task(s)" in capsys.readouterr().out


def test_approve_records_the_operator_principal_and_cli_surface(kanban_home):
    with kb.connect_closing() as conn:
        gate = _open_awaiting_approval(conn)

    assert _run("approve", gate_id=gate.gate_id, digest=gate.design_digest,
                operator="nicholas") == 0

    with kb.connect_closing() as conn:
        approved = kb.get_architecture_gate(conn, gate.gate_id)
    assert approved.approval_actor_id == "nicholas"
    assert approved.approval_surface == "cli"


def test_approve_with_a_wrong_digest_is_refused_and_exits_non_zero(kanban_home, capsys):
    with kb.connect_closing() as conn:
        gate = _open_awaiting_approval(conn)

    assert _run("approve", gate_id=gate.gate_id, digest="0" * 64) == 1
    assert "approval_digest_mismatch" in capsys.readouterr().err

    with kb.connect_closing() as conn:
        assert kb.get_architecture_gate(conn, gate.gate_id).state == (
            "validated_awaiting_approval"
        )


# ---------------------------------------------------------------------------
# AC2 -- reject, and invalidate/reopen recovery without DB surgery
# ---------------------------------------------------------------------------

def test_reject_moves_the_gate_out_of_awaiting_approval(kanban_home):
    with kb.connect_closing() as conn:
        gate = _open_awaiting_approval(conn)

    assert _run("reject", gate_id=gate.gate_id, digest=gate.design_digest) == 0

    with kb.connect_closing() as conn:
        assert kb.get_architecture_gate(conn, gate.gate_id).state == "rejected"


def test_invalidate_then_reopen_recovers_a_mistaken_gate(kanban_home):
    """The recovery path: reopen rebuilds every persisted owner binding."""
    with kb.connect_closing() as conn:
        gate = _open_awaiting_approval(conn)

    assert _run("invalidate", gate_id=gate.gate_id, reason="opened by mistake") == 0
    with kb.connect_closing() as conn:
        assert kb.get_architecture_gate(conn, gate.gate_id).state == "invalidated"

    assert _run("reopen", gate_id=gate.gate_id) == 0
    with kb.connect_closing() as conn:
        assert kb.get_architecture_gate(conn, gate.gate_id).state == "open"


def test_unknown_gate_id_exits_non_zero(kanban_home, capsys):
    assert _run("show", gate_id="g_does_not_exist") == 1
    assert "no such architecture gate" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# AC3 -- exactly one authorized graph per approved gate
# ---------------------------------------------------------------------------

def _graph_file(tmp_path) -> str:
    path = tmp_path / "graph.json"
    path.write_text(json.dumps([
        {"title": "ingestion", "assignee": "coder", "parents": []},
        {"title": "review", "assignee": "reviewer", "parents": [0]},
    ]), encoding="utf-8")
    return str(path)


def test_issue_graph_is_single_shot(kanban_home, tmp_path, capsys):
    with kb.connect_closing() as conn:
        gate = _open_awaiting_approval(conn)
    assert _run("approve", gate_id=gate.gate_id, digest=gate.design_digest) == 0
    capsys.readouterr()

    graph = _graph_file(tmp_path)
    assert _run("issue-graph", gate_id=gate.gate_id, graph_file=graph,
                idempotency_key="graph-v1") == 0
    assert "issued 2 task(s)" in capsys.readouterr().out

    # A second issuance must be refused, not silently duplicated.
    assert _run("issue-graph", gate_id=gate.gate_id, graph_file=graph,
                idempotency_key="graph-v2") == 1
    assert "architecture_graph_issued" in capsys.readouterr().err


def test_issue_graph_rejects_an_unreadable_or_empty_graph_file(kanban_home, tmp_path):
    with kb.connect_closing() as conn:
        gate = _open_awaiting_approval(conn)
    empty = tmp_path / "empty.json"
    empty.write_text("[]", encoding="utf-8")

    assert _run("issue-graph", gate_id=gate.gate_id, graph_file=str(empty),
                idempotency_key="k") == 2
    assert _run("issue-graph", gate_id=gate.gate_id,
                graph_file=str(tmp_path / "missing.json"), idempotency_key="k") == 2


# ---------------------------------------------------------------------------
# AC4 -- the surface refuses a dispatcher worker
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "action,extra",
    [
        ("approve", {"digest": "x"}),
        ("reject", {"digest": "x"}),
        ("invalidate", {"reason": "r"}),
        ("reopen", {}),
        ("issue-graph", {"graph_file": "g.json", "idempotency_key": "k"}),
    ],
)
def test_every_mutating_gate_action_refuses_a_dispatcher_worker(
    kanban_home, monkeypatch, capsys, action, extra,
):
    with kb.connect_closing() as conn:
        gate = _open_awaiting_approval(conn)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_worker")

    assert _run(action, gate_id=gate.gate_id, **extra) == 3
    assert "dispatcher worker" in capsys.readouterr().err

    with kb.connect_closing() as conn:
        assert kb.get_architecture_gate(conn, gate.gate_id).state == (
            "validated_awaiting_approval"
        )


def test_read_only_gate_actions_still_work_for_a_worker(kanban_home, monkeypatch):
    """The refusal is scoped to mutations; inspection is not privileged."""
    with kb.connect_closing() as conn:
        _open_awaiting_approval(conn)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_worker")
    assert _run("list") == 0


# ---------------------------------------------------------------------------
# AC5 -- the regression that makes this ticket necessary
# ---------------------------------------------------------------------------

def test_rolling_policy_back_to_shadow_does_not_un_enforce_an_open_gate(kanban_home):
    """``enforcement_mode`` is persisted per gate ROW, not read from policy.

    This is why "flip it back to shadow" is not a rollback, and why the CLI
    has to exist: once a gate is open in an enforcing mode, only resolving it
    releases the protected write.
    """
    with kb.connect_closing() as conn:
        gate = _open_awaiting_approval(conn)
        assert gate.enforcement_mode in kb.ARCHITECTURE_GATE_ENFORCING_MODES

        # Whatever the live policy now says, the ROW still enforces.
        with pytest.raises(kb.ArchitectureGateError, match="architecture_gate_open"):
            kb.create_task(
                conn, title="blocked by the row", assignee="coder",
                mutation_context=_implementation_context(),
            )

    # Resolving it -- the only real escape -- is what releases the work.
    assert _run("approve", gate_id=gate.gate_id, digest=gate.design_digest) == 0
    with kb.connect_closing() as conn:
        assert kb.get_architecture_gate(conn, gate.gate_id).state == "human_approved"


def test_list_reports_gates_and_filters_by_state(kanban_home, capsys):
    with kb.connect_closing() as conn:
        gate = _open_awaiting_approval(conn)

    assert _run("list", json=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [g["gate_id"] for g in payload] == [gate.gate_id]

    assert _run("list", state="human_approved", json=True) == 0
    assert json.loads(capsys.readouterr().out) == []
