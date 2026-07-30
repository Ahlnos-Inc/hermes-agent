"""BUILD-862 issued-rearm contract tests."""

from __future__ import annotations
import sqlite3
from pathlib import Path
import pytest
from hermes_cli import kanban_db as kb

@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"; home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db(); return home

def _mc(**kw): return kb.MutationContext(board_key="default", session_id="s1", request_scope_id="t1", **kw)
def _arch(mode="orchestrator_only"): return _mc(principal="orch", actor_type="orchestrator_agent", mode=mode, phase="architecture")
def _human(): return _mc(principal="h1", actor_type="human", surface="cli", mode="enforce", phase="approval")

def _handoff():
    return {"role":"architect","design_depth":"formal","chosen_approach":"CAS rearm.",
            "alternatives_rejected":["links"],"slices":[{"name":"core","verification":["test"]}],
            "acceptance_criteria":["rearm works"],"verification_plan":["tests"],
            "human_approval_required":True,"rollout":{"mode":"shadow"},"rollback":{"mode":"off"}}

def _gate(conn, mode="orchestrator_only"):
    t = kb.create_task(conn, title="arch", assignee="architect", mutation_context=_arch(mode))
    c = kb.claim_task(conn, t); assert c is not None
    assert kb.complete_task(conn, t, metadata=_handoff(), expected_run_id=c.current_run_id)
    g = kb.get_architecture_gate_for_task(conn, t); assert g is not None
    return kb.accept_architecture_handoff(conn, g.gate_id)

def _setup(conn, ikey="v1", mode="orchestrator_only"):
    """Return (coder_id, reviewer_id, verifier_id, gate) after approve+issue."""
    gate = _gate(conn, mode)
    appr = kb.approve_architecture_gate(conn, gate.gate_id, _human(), gate.design_digest or "")
    fg = kb.get_architecture_gate(conn, appr.gate_id); assert fg is not None
    iss = _mc(principal="orch", actor_type="orchestrator_agent", gate_id=appr.gate_id,
             profile="orchestrator", mode=mode, phase="graph_issuance")
    graph = [{"title":"code","assignee":"coder","parents":[]},
             {"title":"review","assignee":"reviewer","parents":[0]},
             {"title":"verify","assignee":"verifier","parents":[1]}]
    ids = kb.issue_architecture_graph(conn, appr.gate_id, iss, graph, idempotency_key=ikey)
    assert len(ids) == 3
    return ids[0], ids[1], ids[2], fg


# ---- schema ----

def test_schema_has_contracts_table(kanban_home):
    with kb.connect() as conn:
        names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "architecture_graph_issuance_contracts" in names

def test_schema_has_no_delete_trigger(kanban_home):
    with kb.connect() as conn:
        names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert "continuation_blockers_no_delete" in names

def test_contract_columns(kanban_home):
    with kb.connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(architecture_graph_issuance_contracts)")}
    assert {"id","gate_id","fix_task_id","review_task_id","fix_assignee","review_assignee","issued_at"} <= cols


# ---- contract population ----

def test_contract_populated_for_coder_reviewer_edge(kanban_home):
    with kb.connect() as conn:
        coder, reviewer, verifier, gate = _setup(conn)
        c = kb.get_issuance_contract(conn, coder, reviewer)
        assert c is not None and c.gate_id == gate.gate_id
        assert c.fix_task_id == coder and c.review_task_id == reviewer
        assert c.fix_assignee == "coder" and c.review_assignee == "reviewer"
        assert c.issued_at > 0
        assert kb.get_issuance_contract(conn, reviewer, verifier) is None
        assert kb.get_issuance_contract(conn, reviewer, coder) is None

def test_no_contract_for_coder_coder_edge(kanban_home):
    with kb.connect() as conn:
        g = _gate(conn); appr = kb.approve_architecture_gate(conn, g.gate_id, _human(), g.design_digest or "")
        iss = _mc(principal="orch", actor_type="orchestrator_agent", gate_id=appr.gate_id,
                  profile="orchestrator", mode="orchestrator_only", phase="graph_issuance")
        graph = [{"title":"c1","assignee":"coder","parents":[]},{"title":"c2","assignee":"coder","parents":[0]}]
        ids = kb.issue_architecture_graph(conn, appr.gate_id, iss, graph, idempotency_key="cc")
        assert kb.get_issuance_contract(conn, ids[0], ids[1]) is None


# ---- blocker no-delete trigger ----

def test_blocker_no_delete_trigger(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="r", assignee="reviewer")
        b = kb.record_continuation_blocker(conn, task_id=tid, severity="P1", title="Bug", discovered_by="reviewer")
        with pytest.raises((sqlite3.OperationalError, sqlite3.IntegrityError), match="continuation_blocker_delete_denied"):
            conn.execute("DELETE FROM continuation_blockers WHERE id = ?", (b.id,))
        kb.resolve_continuation_blocker(conn, task_id=tid, blocker_id=b.id, resolved_by="r", resolution_evidence_ref="c1")
        resolved = kb.list_continuation_blockers(conn, tid)
        assert resolved[0].status == "resolved"


# ---- issued rearm success ----

def test_issued_rearm_success_path(kanban_home):
    with kb.connect() as conn:
        coder, reviewer, verifier, gate = _setup(conn)
        cr = kb.claim_task(conn, coder, claimer="coder"); assert cr is not None
        assert kb.complete_task(conn, coder, result="done", metadata={"sha": "a"*40}, expected_run_id=cr.current_run_id)
        rr = kb.claim_task(conn, reviewer, claimer="reviewer"); assert rr is not None
        rr_id = int(rr.current_run_id)
        kb.block_task(conn, reviewer, kind="needs_input", reason="P1")
        res = kb.request_rework(conn, reviewer, finding="P1", fix=kb.ExistingFixTask(coder),
            request_key="rr1", actor="reviewer",
            metadata={"rework":{"open_blockers":[{"key":"B1","summary":"s"}]}},
            reviewed_sha="a"*40, expected_run_id=rr_id)
        assert res.fix_action == "issued_rearm"
        assert kb.get_task(conn, coder).status == "ready"
        assert kb.get_task(conn, reviewer).block_kind is None
        assert kb.get_task(conn, verifier).status == "todo"
        rev_evts = kb.list_events(conn, reviewer)
        req = [e for e in rev_evts if e.kind == "rework_requested"]
        assert len(req) == 1 and req[0].payload.get("request_key") == "rr1"
        assert req[0].payload.get("reviewed_sha") == "a"*40
        fix_evts = [e for e in kb.list_events(conn, coder) if e.kind == "rework_for"]
        assert len(fix_evts) == 1
        gate_evts = [e for e in kb.list_events(conn, gate.architect_task_id) if e.kind == "issued_rearm_activated"]
        assert len(gate_evts) == 1 and gate_evts[0].payload.get("fix_task_id") == coder


# ---- idempotency ----

def test_issued_rearm_replay(kanban_home):
    with kb.connect() as conn:
        coder, reviewer, _, _ = _setup(conn, ikey="replay-v1")
        kb.claim_task(conn, coder, claimer="c1"); kb.complete_task(conn, coder, result="d")
        rr1 = kb.claim_task(conn, reviewer, claimer="r1"); rr1_id = int(rr1.current_run_id)
        kb.block_task(conn, reviewer, kind="needs_input", reason="f")
        first = kb.request_rework(conn, reviewer, finding="A", fix=kb.ExistingFixTask(coder),
            request_key="stable-key", actor="reviewer",
            metadata={"rework":{"open_blockers":[{"key":"B1","summary":"A"}]}},
            expected_run_id=rr1_id)
        assert first.fix_action == "issued_rearm"
        kb.claim_task(conn, coder, claimer="c2"); kb.complete_task(conn, coder, result="d2")
        rr2 = kb.claim_task(conn, reviewer, claimer="r2")
        replay = kb.request_rework(conn, reviewer, finding="A", fix=kb.ExistingFixTask(coder),
            request_key="stable-key", actor="reviewer",
            metadata={"rework":{"open_blockers":[{"key":"B1","summary":"A"}]}},
            expected_run_id=int(rr2.current_run_id))
        assert replay.fix_action == "replayed"
        assert replay.request_event_id == first.request_event_id


# ---- denial guards ----

def test_denied_fix_not_done(kanban_home):
    with kb.connect() as conn:
        coder, reviewer, _, _ = _setup(conn, ikey="nd-v1")
        kb.claim_task(conn, coder, claimer="c"); kb.complete_task(conn, coder, result="done")
        rr = kb.claim_task(conn, reviewer, claimer="r")
        kb.block_task(conn, reviewer, kind="needs_input", reason="b")
        conn.execute("UPDATE tasks SET status=? WHERE id=?", ("running", coder))
        with pytest.raises(ValueError, match="fix task must be done"):
            kb.request_rework(conn, reviewer, finding="f", fix=kb.ExistingFixTask(coder),
                request_key="k", actor="reviewer", metadata={"rework":{"open_blockers":[]}},
                expected_run_id=int(rr.current_run_id))

def test_denied_review_not_blocked(kanban_home):
    with kb.connect() as conn:
        coder, reviewer, _, _ = _setup(conn, ikey="nb-v1")
        kb.claim_task(conn, coder, claimer="c"); kb.complete_task(conn, coder, result="done")
        rr = kb.claim_task(conn, reviewer, claimer="r")
        with pytest.raises(ValueError, match="review task must be blocked"):
            kb.request_rework(conn, reviewer, finding="f", fix=kb.ExistingFixTask(coder),
                request_key="k", actor="reviewer", metadata={"rework":{"open_blockers":[]}},
                expected_run_id=int(rr.current_run_id))

def test_denied_quarantined_fix(kanban_home):
    with kb.connect() as conn:
        coder, reviewer, _, _ = _setup(conn, ikey="q-v1")
        kb.claim_task(conn, coder, claimer="c"); kb.complete_task(conn, coder, result="done")
        # Claim the reviewer BEFORE quarantining the coder; otherwise claim_task
        # sees an unsatisfied (quarantined) parent and returns None.
        rr = kb.claim_task(conn, reviewer, claimer="r")
        assert rr is not None
        conn.execute("UPDATE tasks SET policy_quarantined=1 WHERE id=?", (coder,))
        kb.block_task(conn, reviewer, kind="needs_input", reason="b")
        with pytest.raises(ValueError, match="quarantined or invalidated"):
            kb.request_rework(conn, reviewer, finding="f", fix=kb.ExistingFixTask(coder),
                request_key="k", actor="reviewer", metadata={"rework":{"open_blockers":[]}},
                expected_run_id=int(rr.current_run_id))

def test_denied_dominance_failure(kanban_home):
    with kb.connect() as conn:
        g = _gate(conn); appr = kb.approve_architecture_gate(conn, g.gate_id, _human(), g.design_digest or "")
        iss = _mc(principal="orch", actor_type="orchestrator_agent", gate_id=appr.gate_id,
                  profile="orchestrator", mode="orchestrator_only", phase="graph_issuance")
        graph = [{"title":"code","assignee":"coder","parents":[]},
                 {"title":"review","assignee":"reviewer","parents":[0]},
                 {"title":"verify","assignee":"verifier","parents":[0,1]}]
        ids = kb.issue_architecture_graph(conn, appr.gate_id, iss, graph, idempotency_key="dom-v1")
        coder, reviewer = ids[0], ids[1]
        assert kb.get_issuance_contract(conn, coder, reviewer) is not None
        kb.claim_task(conn, coder, claimer="c"); kb.complete_task(conn, coder, result="done")
        rr = kb.claim_task(conn, reviewer, claimer="r")
        kb.block_task(conn, reviewer, kind="needs_input", reason="P1")
        with pytest.raises(ValueError, match="dominance"):
            kb.request_rework(conn, reviewer, finding="f", fix=kb.ExistingFixTask(coder),
                request_key="k", actor="reviewer", metadata={"rework":{"open_blockers":[]}},
                expected_run_id=int(rr.current_run_id))

def test_denied_bound_exceeded(kanban_home):
    with kb.connect() as conn:
        coder, reviewer, _, _ = _setup(conn, ikey="bound-v1")
        for i in range(kb.ISSUED_REARM_BOUND):
            kb.claim_task(conn, coder, claimer=f"c{i}"); kb.complete_task(conn, coder, result=f"a{i}")
            rr = kb.claim_task(conn, reviewer, claimer=f"r{i}"); assert rr is not None
            kb.block_task(conn, reviewer, kind="needs_input", reason="b")
            res = kb.request_rework(conn, reviewer, finding=f"d{i}", fix=kb.ExistingFixTask(coder),
                request_key=f"k{i}", actor="reviewer", metadata={"rework":{"open_blockers":[{"key":f"B{i}","summary":"s"}]}},
                expected_run_id=int(rr.current_run_id))
            assert res.fix_action == "issued_rearm", f"round {i} should succeed"
        kb.claim_task(conn, coder, claimer="cx"); kb.complete_task(conn, coder, result="ax")
        rfinal = kb.claim_task(conn, reviewer, claimer="rx"); assert rfinal is not None
        kb.block_task(conn, reviewer, kind="needs_input", reason="b")
        with pytest.raises(ValueError, match="bound exceeded"):
            kb.request_rework(conn, reviewer, finding="over", fix=kb.ExistingFixTask(coder),
                request_key="kx", actor="reviewer", metadata={"rework":{"open_blockers":[]}},
                expected_run_id=int(rfinal.current_run_id))

def test_denied_missing_run_id(kanban_home):
    with kb.connect() as conn:
        coder, reviewer, _, _ = _setup(conn, ikey="nri-v1")
        kb.claim_task(conn, coder, claimer="c"); kb.complete_task(conn, coder, result="done")
        kb.claim_task(conn, reviewer, claimer="r")
        kb.block_task(conn, reviewer, kind="needs_input", reason="b")
        with pytest.raises(ValueError, match="expected_run_id"):
            kb.request_rework(conn, reviewer, finding="f", fix=kb.ExistingFixTask(coder),
                request_key="k", actor="reviewer", metadata={"rework":{"open_blockers":[]}})

def test_denied_stale_run_id(kanban_home):
    with kb.connect() as conn:
        coder, reviewer, _, _ = _setup(conn, ikey="sri-v1")
        kb.claim_task(conn, coder, claimer="c"); kb.complete_task(conn, coder, result="done")
        rr = kb.claim_task(conn, reviewer, claimer="r"); rr_id = int(rr.current_run_id)
        kb.block_task(conn, reviewer, kind="needs_input", reason="b")
        with pytest.raises(ValueError, match="stale expected_run_id|expected_run_id"):
            kb.request_rework(conn, reviewer, finding="f", fix=kb.ExistingFixTask(coder),
                request_key="k", actor="reviewer", metadata={"rework":{"open_blockers":[]}},
                expected_run_id=rr_id + 99999)

def test_denied_bad_reviewed_sha(kanban_home):
    with kb.connect() as conn:
        coder, reviewer, _, _ = _setup(conn, ikey="sha-v1")
        kb.claim_task(conn, coder, claimer="c"); kb.complete_task(conn, coder, result="done")
        rr = kb.claim_task(conn, reviewer, claimer="r")
        kb.block_task(conn, reviewer, kind="needs_input", reason="b")
        with pytest.raises(ValueError, match="reviewed_sha"):
            kb.request_rework(conn, reviewer, finding="f", fix=kb.ExistingFixTask(coder),
                request_key="k", actor="reviewer", metadata={"rework":{"open_blockers":[]}},
                reviewed_sha="not-hex", expected_run_id=int(rr.current_run_id))


# ---- fallthrough ----

def test_external_fix_falls_through_to_gate_denial(kanban_home):
    with kb.connect() as conn:
        coder, reviewer, _, _ = _setup(conn, ikey="ext-v1")
        ext = kb.create_task(conn, title="ext", assignee="coder")
        kb.claim_task(conn, ext, claimer="ec"); kb.complete_task(conn, ext, result="done")
        assert kb.get_issuance_contract(conn, ext, reviewer) is None
        kb.claim_task(conn, coder, claimer="c"); kb.complete_task(conn, coder, result="done")
        rr = kb.claim_task(conn, reviewer, claimer="r")
        kb.block_task(conn, reviewer, kind="needs_input", reason="f")
        with pytest.raises((ValueError, kb.ArchitectureGateError)):
            kb.request_rework(conn, reviewer, finding="f", fix=kb.ExistingFixTask(ext),
                request_key="k", actor="reviewer", expected_run_id=int(rr.current_run_id))


# ---- end-to-end ----

def test_e2e_lifecycle(kanban_home):
    SHA1 = "a" * 40; SHA2 = "b" * 40
    with kb.connect() as conn:
        coder, reviewer, verifier, gate = _setup(conn, ikey="e2e-v1")
        # coder round 1
        cr1 = kb.claim_task(conn, coder, claimer="coder"); assert cr1 is not None
        assert kb.complete_task(conn, coder, result="r1", metadata={"sha": SHA1}, expected_run_id=cr1.current_run_id)
        # reviewer round 1: P1
        rr1 = kb.claim_task(conn, reviewer, claimer="reviewer"); assert rr1 is not None
        rr1_id = int(rr1.current_run_id)
        p1 = kb.record_continuation_blocker(conn, task_id=reviewer, severity="P1",
            title="Assertion inverted", discovered_by="reviewer", discovered_run_id=rr1_id)
        assert len(kb.open_critical_continuation_blockers(conn, reviewer)) == 1
        kb.block_task(conn, reviewer, kind="needs_input", reason="P1")
        rearm = kb.request_rework(conn, reviewer, finding="P1: inverted",
            fix=kb.ExistingFixTask(coder), request_key="r1", actor="reviewer",
            metadata={"rework":{"open_blockers":[{"key":"B1","summary":"inv"}]}},
            reviewed_sha=SHA1, expected_run_id=rr1_id)
        assert rearm.fix_action == "issued_rearm"
        assert kb.get_task(conn, coder).status == "ready"
        assert kb.get_task(conn, verifier).status == "todo"
        # coder round 2
        cr2 = kb.claim_task(conn, coder, claimer="coder"); assert cr2 is not None
        assert kb.complete_task(conn, coder, result="r2", metadata={"sha": SHA2}, expected_run_id=cr2.current_run_id)
        # reviewer round 2: resolve P1, pass
        rr2 = kb.claim_task(conn, reviewer, claimer="reviewer"); assert rr2 is not None
        rr2_id = int(rr2.current_run_id)
        kb.resolve_continuation_blocker(conn, task_id=reviewer, blocker_id=p1.id,
            resolved_by="reviewer", resolution_evidence_ref=f"commit {SHA2}", resolved_run_id=rr2_id)
        assert len(kb.open_critical_continuation_blockers(conn, reviewer)) == 0
        assert kb.complete_task(conn, reviewer, result="LGTM", expected_run_id=rr2_id)
        assert kb.get_task(conn, reviewer).status == "done"
        v = kb.get_task(conn, verifier); assert v is not None and v.status in {"ready","todo"}
        # audit
        rr_evts = [e for e in kb.list_events(conn, reviewer) if e.kind == "rework_requested"]
        assert len(rr_evts) == 1 and rr_evts[0].payload.get("reviewed_sha") == SHA1
        rf_evts = [e for e in kb.list_events(conn, coder) if e.kind == "rework_for"]
        assert len(rf_evts) == 1 and rf_evts[0].payload.get("fix_action") == "issued_rearm"
        ia_evts = [e for e in kb.list_events(conn, gate.architect_task_id) if e.kind == "issued_rearm_activated"]
        assert len(ia_evts) == 1 and ia_evts[0].payload.get("round_count_after") == 1
        blk = kb.list_continuation_blockers(conn, reviewer)
        assert len(blk) == 1 and blk[0].status == "resolved" and SHA2 in (blk[0].resolution_evidence_ref or "")
