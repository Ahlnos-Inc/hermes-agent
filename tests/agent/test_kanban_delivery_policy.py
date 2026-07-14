from agent.kanban_delivery_policy import ArchitectureDeliveryPolicy


def test_dynamic_policy_delivers_for_each_authoritative_approved_state(monkeypatch, tmp_path):
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
        for state in ("policy_accepted", "human_approved"):
            conn.execute("UPDATE architecture_gates SET state = ? WHERE gate_id = ?", (state, gate.gate_id))
            assert policy.final(state) == state
        for state in ("open", "validated_awaiting_approval", "invalidated", "rejected"):
            conn.execute("UPDATE architecture_gates SET state = ? WHERE gate_id = ?", (state, gate.gate_id))
            assert "output withheld" in str(policy.final("secret"))


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
