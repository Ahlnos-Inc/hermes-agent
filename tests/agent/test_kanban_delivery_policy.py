import json
from types import SimpleNamespace

import pytest

from agent.kanban_delivery_policy import (
    ArchitectureDeliveryPolicy,
    policy_for_current_kanban_task,
)


def test_dynamic_policy_allows_architect_to_produce_and_surface_handoff(
    monkeypatch, tmp_path,
):
    from hermes_cli import kanban_db as kb

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    kb.init_db()
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="Design workflow",
            assignee="architect",
            mutation_context=kb.MutationContext(
                board_key="default", principal="orchestrator-session",
                actor_type="orchestrator_agent", profile="orchestrator",
                session_id="session-1", request_scope_id="turn-1",
                mode="orchestrator_only", phase="architecture",
            ),
        )
        gate = kb.get_architecture_gate_for_task(conn, task_id)
        assert gate is not None
        policy = ArchitectureDeliveryPolicy(task_id=task_id)
        for state in (
            "open",
            "validated_awaiting_approval",
            "policy_accepted",
            "human_approved",
            "invalidated",
            "rejected",
        ):
            conn.execute("UPDATE architecture_gates SET state = ? WHERE gate_id = ?", (state, gate.gate_id))
            assert policy.final(state) == state


def test_unresolved_gate_withholds_all_delivery_shapes():
    policy = ArchitectureDeliveryPolicy(gate_id="gate-1", state="validated_awaiting_approval")

    assert policy.stream_delta("secret streamed prose") is None
    assert policy.interim("secret interim prose") is None
    receipt = str(policy.final("secret final prose"))
    assert receipt.startswith("Architecture approval pending; output withheld (gate gate-1;")
    assert "state validated_awaiting_approval" in receipt
    assert "next action: await exact-digest human approval" in receipt
    assert "secret" not in receipt


def test_human_approved_gate_preserves_delivery():
    policy = ArchitectureDeliveryPolicy(gate_id="gate-1", state="human_approved")

    assert policy.stream_delta("visible") == "visible"
    assert policy.interim("visible") == "visible"
    assert policy.final("visible") == "visible"


def test_dynamic_policy_closes_every_authoritative_lookup(monkeypatch):
    from hermes_cli import kanban_db as kb

    opened = 0
    closed = 0

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def close(self):
            nonlocal closed
            closed += 1

    def fake_connect(**_kwargs):
        nonlocal opened
        opened += 1
        return FakeConnection()

    monkeypatch.setattr(kb, "connect", fake_connect)
    monkeypatch.setattr(
        kb, "get_delivery_architecture_gate", lambda _conn, _task_id: None,
    )

    policy = ArchitectureDeliveryPolicy(task_id="t_worker")
    for _ in range(300):
        assert policy.tool_result("visible") == "visible"

    assert opened == 300
    assert closed == opened


def test_dynamic_policy_closes_connection_when_lookup_fails(monkeypatch):
    from hermes_cli import kanban_db as kb

    closed = 0

    class FakeConnection:
        def close(self):
            nonlocal closed
            closed += 1

    monkeypatch.setattr(kb, "connect", lambda **_kwargs: FakeConnection())

    def fail_lookup(_conn, _task_id):
        raise OSError("too many open files")

    monkeypatch.setattr(kb, "get_delivery_architecture_gate", fail_lookup)

    policy = ArchitectureDeliveryPolicy(task_id="t_worker")
    result = policy.tool_result("private")

    assert "output withheld" in str(result)
    assert policy.lookup_failed is True
    # tool_result checks withholding, then receipt deliberately refreshes
    # again so a just-approved gate cannot return a stale denial.
    assert closed == 2


def test_authoritatively_ungated_worker_never_depends_on_runtime_lookup(
    monkeypatch,
):
    from hermes_cli import kanban_db as kb

    monkeypatch.setattr(kb, "connect", lambda **_kwargs: FakeConnection())
    monkeypatch.setattr(
        kb,
        "get_delivery_architecture_gate",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("ungated run performed a live lookup")
        ),
    )

    policy = ArchitectureDeliveryPolicy(
        task_id="t_ungated",
        attestation_loaded=True,
        attested_disposition="none",
    )
    assert policy.tool_result("still visible") == "still visible"
    assert policy.lookup_failed is False


def test_known_gate_remains_fail_closed_when_later_lookup_fails(monkeypatch):
    from hermes_cli import kanban_db as kb

    monkeypatch.setattr(kb, "connect", lambda **_kwargs: FakeConnection())
    gate = SimpleNamespace(
        gate_id="gate-1", architect_task_id="t_arch", state="human_approved",
    )
    lookups = iter([OSError("transient board lookup failure"), gate])

    def lookup(_conn, _task_id):
        result = next(lookups)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(kb, "get_delivery_architecture_gate", lookup)

    policy = ArchitectureDeliveryPolicy(
        task_id="t_protected",
        gate_id="gate-1",
        architect_task_id="t_arch",
        state="human_approved",
        attestation_loaded=True,
        attested_disposition="enforcing_approved",
        attested_gate_id="gate-1",
        attested_row_version=1,
    )
    assert "output withheld" in str(policy.tool_result("private"))


def test_previously_seen_gate_cannot_disappear_open(monkeypatch):
    from hermes_cli import kanban_db as kb

    monkeypatch.setattr(kb, "connect", lambda **_kwargs: FakeConnection())
    lookups = iter([None, None])
    monkeypatch.setattr(
        kb, "get_delivery_architecture_gate",
        lambda _conn, _task_id: next(lookups),
    )

    policy = ArchitectureDeliveryPolicy(
        task_id="t_protected",
        gate_id="gate-1",
        architect_task_id="t_arch",
        state="human_approved",
        attestation_loaded=True,
        attested_disposition="enforcing_approved",
        attested_gate_id="gate-1",
        attested_row_version=1,
    )
    assert "output withheld" in str(policy.tool_result("private"))


def test_current_run_loads_authoritative_ungated_attestation(monkeypatch, tmp_path):
    from hermes_cli import kanban_db as kb

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    kb.init_db()
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="Ordinary worker", assignee="coder")
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None and claimed.current_run_id is not None
        spec = kb.get_run_spec(conn, claimed.current_run_id)
        assert spec is not None

    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))
    monkeypatch.setenv(
        "HERMES_KANBAN_DELIVERY_POLICY",
        json.dumps(spec["delivery_policy"]),
    )
    policy = policy_for_current_kanban_task()

    assert policy is not None
    assert policy.attested_disposition == "none"
    assert policy.tool_result("visible") == "visible"


def test_spawn_attestation_is_cached_without_reloading_runspec(monkeypatch):
    from hermes_cli import kanban_db as kb

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_cached_attestation")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "991")
    monkeypatch.setenv(
        "HERMES_KANBAN_DELIVERY_POLICY",
        json.dumps(
            {
                "version": 1,
                "disposition": "none",
                "gate_id": None,
                "architect_task_id": None,
                "state": None,
                "row_version": None,
                "accepted_run_id": None,
                "design_digest": None,
            }
        ),
    )
    monkeypatch.setattr(
        kb,
        "get_run_spec",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("runtime must not reload RunSpec")
        ),
    )

    first = policy_for_current_kanban_task()
    second = policy_for_current_kanban_task()

    assert first is second
    assert first is not None and first.attested_disposition == "none"


@pytest.mark.parametrize(
    "delivery_policy",
    [
        None,
        {},
        {"version": 2, "disposition": "none"},
        {
            "version": 1,
            "disposition": "enforcing_approved",
            "gate_id": "gate-1",
            "architect_task_id": "t_arch",
            "state": "policy_accepted",
            "row_version": True,
            "accepted_run_id": 1,
            "design_digest": "a" * 64,
        },
        {
            "version": 1,
            "disposition": "none",
            "gate_id": "forged-gate",
            "state": None,
            "row_version": None,
        },
    ],
)
def test_current_run_missing_or_malformed_attestation_fails_closed(
    monkeypatch, tmp_path, delivery_policy,
):
    from hermes_cli import kanban_db as kb

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    kb.init_db()
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="Ordinary worker", assignee="coder")
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None and claimed.current_run_id is not None
        spec = kb.get_run_spec(conn, claimed.current_run_id)
        assert spec is not None
        if delivery_policy is None:
            spec.pop("delivery_policy")
        else:
            spec["delivery_policy"] = delivery_policy
        conn.execute(
            "UPDATE task_runs SET run_spec_json = ? WHERE id = ?",
            (json.dumps(spec), claimed.current_run_id),
        )

    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))
    if delivery_policy is None:
        monkeypatch.delenv("HERMES_KANBAN_DELIVERY_POLICY", raising=False)
    else:
        monkeypatch.setenv(
            "HERMES_KANBAN_DELIVERY_POLICY",
            json.dumps(delivery_policy),
        )
    policy = policy_for_current_kanban_task()

    assert policy is not None
    receipt = str(policy.tool_result("private"))
    assert receipt.startswith(
        "Architecture authorization unavailable; output withheld"
    )
    assert "private" not in receipt


class FakeConnection:
    def close(self):
        pass
