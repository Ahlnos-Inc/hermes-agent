import asyncio
import time
from pathlib import Path
from types import SimpleNamespace


from gateway.config import HomeChannel, Platform
from gateway.run import GatewayRunner
from gateway import kanban_watchers as kw
from hermes_cli import kanban_db as kb


class RecordingAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})


class DisconnectedAdapters(dict):
    """Expose a platform during collection, then simulate disconnect on get()."""

    def get(self, key, default=None):
        return None


async def _run_one_notifier_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _make_runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    return runner


def _create_completed_subscription(summary="done once"):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="notify once", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb.complete_task(conn, tid, summary=summary)
        return tid
    finally:
        conn.close()


def _create_blocked_subscription(reason="blocked once"):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="blocked notify once", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb.block_task(conn, tid, reason=reason)
        return tid
    finally:
        conn.close()


def _unseen_terminal_events(tid):
    conn = kb.connect()
    try:
        _, events = kb.unseen_events_for_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            kinds=["completed", "blocked", "gave_up", "crashed", "timed_out"],
        )
        return events
    finally:
        conn.close()


def test_kanban_notifier_dedupes_board_slugs_pointing_to_same_db(tmp_path, monkeypatch):
    db_path = tmp_path / "shared-kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    kb.write_board_metadata("alias-a", name="Alias A")
    kb.write_board_metadata("alias-b", name="Alias B")

    tid = _create_completed_subscription()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert "Kanban" in adapter.sent[0]["text"]
    assert tid in adapter.sent[0]["text"]


def test_kanban_notifier_claim_prevents_second_watcher_send(tmp_path, monkeypatch):
    db_path = tmp_path / "single-owner.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    tid = _create_completed_subscription()

    adapter1 = RecordingAdapter()
    adapter2 = RecordingAdapter()

    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter1)))
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter2)))

    assert len(adapter1.sent) == 1
    assert adapter2.sent == []


def test_kanban_notifier_rewinds_claim_if_adapter_disconnects(tmp_path, monkeypatch):
    db_path = tmp_path / "adapter-disconnect.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    tid = _create_completed_subscription()

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = DisconnectedAdapters({Platform.TELEGRAM: RecordingAdapter()})
    runner._kanban_sub_fail_counts = {}

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert [ev.kind for ev in _unseen_terminal_events(tid)] == ["completed"]


def test_kanban_notifier_sends_full_completed_run_summary(tmp_path, monkeypatch):
    """Completion pings must not silently cut the worker handoff.

    The completed event payload intentionally stores only a compact first-line
    summary for event-log/dashboard reads. The notifier should hydrate the run
    row and send the full summary, leaving platform adapters to split long
    messages for transport limits.
    """
    db_path = tmp_path / "full-summary.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    tail = "TAIL_SENTINEL after old two-hundred and four-hundred char caps"
    summary = (
        "Accepted prior implementation. "
        + "root cause and verification details " * 20
        + tail
    )
    tid = _create_completed_subscription(summary=summary)

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert len(adapter.sent) == 1
    text = adapter.sent[0]["text"]
    assert tid in text
    assert tail in text
    assert "root cause and verification details" in text


def test_kanban_notifier_sends_full_blocked_reason(tmp_path, monkeypatch):
    """Blocked pings should not truncate the operator-visible decision point."""
    db_path = tmp_path / "full-blocked-reason.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    tail = "BLOCKED_TAIL_SENTINEL after old one-sixty char cap"
    reason = "review-required: " + ("context detail " * 20) + tail
    tid = _create_blocked_subscription(reason=reason)

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert len(adapter.sent) == 1
    text = adapter.sent[0]["text"]
    assert tid in text
    assert tail in text
    assert reason in text


def test_workflow_nonterminal_step_blocked_notifies_origin_with_recorded_delivery(
    tmp_path, monkeypatch
):
    """BUILD-503 regression: the exact 2026-07-16 silent failure.

    A workflow-compiled graph subscribed only its terminal task, so a
    NONTERMINAL step that went blocked(review-required) fired no origin
    notification and the workflow sat silent. Assert the origin subscriber now
    receives the rendered blocked event for the STEP task, that the delivery is
    recorded in the ledger (subscription-exists is NOT proof of delivery), and
    that a watcher restart does not re-deliver (cursor dedup).
    """
    db_path = tmp_path / "workflow-silent-block.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    reason = "review-required: needs human sign-off on the migration plan"
    conn = kb.connect()
    try:
        compiled = kb.compile_workflow_graph(
            conn,
            workflow_key="silent-block-2026-07-16",
            idempotency_key="req-503",
            created_by="orchestrator",
            steps=[
                {"key": "step", "title": "Implement", "assignee": "coder",
                 "parents": []},
                {"key": "final", "title": "Report", "assignee": "writer",
                 "parents": ["step"], "role": "reporter", "terminal": True},
            ],
            notification={"platform": "telegram", "chat_id": "chat-1"},
        )
        step_id = compiled.task_ids["step"]
        # The nonterminal step blocks for review — the stranding transition.
        kb.block_task(conn, step_id, reason=reason)
    finally:
        conn.close()

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert len(adapter.sent) == 1, adapter.sent
    text = adapter.sent[0]["text"]
    assert step_id in text
    assert "blocked" in text.lower()
    assert reason in text

    conn = kb.connect()
    try:
        deliveries = kb.list_notify_deliveries(conn, task_id=step_id)
        assert len(deliveries) == 1
        assert deliveries[0]["status"] == "delivered"
        assert deliveries[0]["platform"] == "telegram"
    finally:
        conn.close()

    # Watcher restart: a fresh runner/tick must not re-deliver (cursor dedup).
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(RecordingAdapter())))
    restarted = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(restarted)))
    assert restarted.sent == []


# --- BUILD-508: per-subscription event-kind filter --------------------------


def test_workflow_step_completion_is_silent_only_terminal_notifies(tmp_path, monkeypatch):
    """BUILD-508: the named upgrade path from BUILD-503's ponytail comment.

    A non-terminal step's own `completed` event is progress noise, not the
    "workflow finished" signal — its subscription is narrowed to
    FAILURE_KINDS by compile_workflow_graph, so it must NOT ping the origin.
    The terminal task's subscription stays NULL (all kinds) and DOES notify
    on completion, unchanged from pre-BUILD-508 behavior.
    """
    db_path = tmp_path / "workflow-step-progress.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        compiled = kb.compile_workflow_graph(
            conn,
            workflow_key="progress-2026-07-17",
            idempotency_key="req-508",
            created_by="orchestrator",
            steps=[
                {"key": "step", "title": "Implement", "assignee": "coder",
                 "parents": []},
                {"key": "final", "title": "Report", "assignee": "writer",
                 "parents": ["step"], "role": "reporter", "terminal": True},
            ],
            notification={"platform": "telegram", "chat_id": "chat-1"},
        )
        step_id = compiled.task_ids["step"]
        terminal_id = compiled.terminal_task_id
        # A raw `completed`-kind event on the step task, independent of its
        # actual status transition (which would unsub it anyway once truly
        # done) — isolates the kind filter from the separate done/archived
        # unsub mechanism so the cursor assertion below is meaningful.
        with kb.write_txn(conn):
            kb._append_event(conn, step_id, kind="completed", payload={"summary": "noise"})
    finally:
        conn.close()

    # Tick 1: the step's completed event is claimed-and-skipped.
    tick1 = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(tick1)))
    assert tick1.sent == [], "a non-terminal step's completion must not ping the origin"

    # The cursor must have advanced past the filtered event anyway — the
    # claim (not the per-kind filter) is what owns exactly-once delivery.
    conn = kb.connect()
    try:
        step_sub = next(
            s for s in kb.list_notify_subs(conn, task_id=step_id)
            if s["chat_id"] == "chat-1"
        )
        assert int(step_sub["last_event_id"]) > 0
    finally:
        conn.close()

    # Tick 2 (restart): nothing replays.
    tick2 = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(tick2)))
    assert tick2.sent == []

    # The terminal task's own completion (kinds_json=NULL) DOES notify.
    conn = kb.connect()
    try:
        assert kb.claim_task(conn, step_id) is not None
        assert kb.complete_task(conn, step_id, summary="step done")
        assert kb.complete_task(conn, terminal_id, summary="workflow done")
    finally:
        conn.close()

    tick3 = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(tick3)))
    assert len(tick3.sent) == 1
    assert terminal_id in tick3.sent[0]["text"]


def test_kanban_db_path_is_test_isolated_from_real_home():
    hermes_home = Path(kb.kanban_home())
    production_db = Path.home() / ".hermes" / "kanban.db"
    assert kb.kanban_db_path().resolve() != production_db.resolve()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
    finally:
        conn.close()

    assert kb.kanban_db_path().resolve().is_relative_to(hermes_home.resolve())
    assert kb.kanban_db_path().resolve() != production_db.resolve()


def test_shared_kanban_renderer_preserves_completed_handoff():
    from gateway.kanban_notifications import render_kanban_event

    task = type("Task", (), {"title": "Ship it", "assignee": "coder", "result": None})()
    event = type("Event", (), {"kind": "completed", "payload": {"summary": "compact"}})()
    run = type("Run", (), {"summary": "full handoff"})()

    assert render_kanban_event(
        task_id="t_123", task=task, event=event, run=run, board_slug="hermes"
    ) == "✔ [hermes] @coder Kanban t_123 done — Ship it\nfull handoff"

class FailingAdapter:
    """Adapter whose send() always raises, simulating a transient send error."""

    def __init__(self):
        self.attempts = 0

    async def send(self, chat_id, text, metadata=None):
        self.attempts += 1
        raise RuntimeError("simulated send failure")


def test_kanban_notifier_rewinds_claim_on_send_exception(tmp_path, monkeypatch):
    """A raising adapter rewinds the claim so the next tick can retry.

    This is the second rewind path (distinct from the adapter-disconnect path
    in test_kanban_notifier_rewinds_claim_if_adapter_disconnects). Here the
    adapter is connected and the send call actually fires; the claim must
    still rewind so the event isn't lost when send() raises mid-tick.
    """
    db_path = tmp_path / "send-failure.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    tid = _create_completed_subscription()

    adapter = FailingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # Send was attempted (so we exercised the failure path, not just the
    # disconnect path) and the claim was rewound — the unseen-events query
    # still returns the event for retry on the next tick.
    assert adapter.attempts >= 1, "send should have been attempted at least once"
    assert [ev.kind for ev in _unseen_terminal_events(tid)] == ["completed"]


def test_notifier_redelivers_same_kind_on_dispatch_cycle(tmp_path, monkeypatch):
    """A retry cycle (crashed → reclaimed → crashed) notifies the user twice.

    Before #21398 the notifier auto-unsubscribed on any terminal event kind
    (gave_up / crashed / timed_out), so the second crash in a respawn cycle
    silently dropped — the subscription was already gone. This test pins the
    new contract: subscription survives non-final terminal events; the
    cursor handles dedup.

    Two crashes ten seconds apart on the same task — both should land on
    the adapter.
    """
    db_path = tmp_path / "redeliver-cycle.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="cycle test", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        # First crash — fired by the dispatcher when the worker PID dies.
        kb._append_event(conn, tid, kind="crashed")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # First crash delivered.
    assert len(adapter.sent) == 1
    assert "crashed" in adapter.sent[0]["text"].lower()

    # Subscription survives — the cursor advanced past event #1, but the
    # row is still there.
    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, tid)
        assert len(subs) == 1, (
            "Subscription must survive a crashed event so a respawn-cycle "
            "second crash also notifies the user (issue #21398)."
        )

        # Second crash — same task, same dispatcher (or a respawn). Append
        # another event to simulate the dispatcher firing crashed a second
        # time during retry.
        kb._append_event(conn, tid, kind="crashed")
    finally:
        conn.close()

    # New tick: the second event has a fresh id past the cursor advance,
    # so it gets claimed and delivered.
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 2, (
        f"Second crashed event should also notify; got {len(adapter.sent)} "
        f"deliveries (texts: {[d['text'] for d in adapter.sent]})"
    )
    assert "crashed" in adapter.sent[1]["text"].lower()


def test_crashed_message_includes_exit_code_and_kind_when_known(tmp_path, monkeypatch):
    """BUILD-343: a crash with a known exit classification (captured by
    ``_classify_worker_exit`` in the reap registry — see hermes_cli/
    kanban_db.py's ``event_payload["exit_kind"] / ["exit_code"]``) must
    surface that detail instead of the fixed generic "(pid gone)" text, so
    an operator can tell a real crash (exit 1, killed by signal) apart from
    a quota death at a glance.
    """
    db_path = tmp_path / "crashed-exit-code.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="real crash", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        # Matches the payload shape _classify_worker_exit's reap path
        # actually writes for a nonzero_exit (kanban_db.py:7326-7330).
        kb._append_event(
            conn, tid, kind="crashed",
            payload={"pid": 4242, "claimer": "host:1", "exit_kind": "nonzero_exit", "exit_code": 1},
        )
    finally:
        conn.close()

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert len(adapter.sent) == 1
    text = adapter.sent[0]["text"]
    assert "1" in text
    assert "pid gone" not in text.lower()


def test_crashed_message_includes_signal_when_signaled(tmp_path, monkeypatch):
    db_path = tmp_path / "crashed-signal.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="oom killed", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb._append_event(
            conn, tid, kind="crashed",
            payload={"pid": 555, "claimer": "host:1", "exit_kind": "signaled", "exit_code": 9},
        )
    finally:
        conn.close()

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert len(adapter.sent) == 1
    text = adapter.sent[0]["text"]
    assert "9" in text
    assert "signal" in text.lower()
    assert "pid gone" not in text.lower()


def test_crashed_message_falls_back_to_pid_gone_when_classification_unknown(tmp_path, monkeypatch):
    """No exit_kind/exit_code captured (the pre-existing ``"unknown"``
    reap-classifier case, e.g. reaped by something else) — keep the
    original generic wording rather than inventing detail that isn't
    there."""
    db_path = tmp_path / "crashed-unknown.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="unknown crash", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb._append_event(
            conn, tid, kind="crashed", payload={"pid": 777, "claimer": "host:1"},
        )
    finally:
        conn.close()

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert len(adapter.sent) == 1
    assert "pid gone" in adapter.sent[0]["text"].lower()


def test_kanban_notifier_delivers_block_loop_detected(tmp_path, monkeypatch):
    """BUILD-443: block_loop_detected must reach messaging subscribers.

    Before this fix the kind was absent from TERMINAL_KINDS, so a task that
    hit the block-recurrence limit and dropped to triage never notified the
    operator — exactly the stall a human needs to hear about.
    """
    db_path = tmp_path / "block-loop-detected.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="loopy task", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb._append_event(
            conn, tid, kind="block_loop_detected",
            payload={"reason": "needs decision", "kind": "needs_input", "recurrences": 2, "limit": 2},
        )
    finally:
        conn.close()

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert len(adapter.sent) == 1
    text = adapter.sent[0]["text"]
    assert tid in text
    assert "triage" in text.lower()
    assert "needs decision" in text


def test_notifier_owning_profile_adapter_no_default_fallback(tmp_path, monkeypatch):
    """A subscription owned by a secondary profile whose profile-adapter
    registry entry EXISTS but lacks this platform must NOT fall back to the
    default profile's same-platform adapter — the notifier must route through
    the shared ``_authorization_adapter`` chokepoint, which forbids that
    fallback (gateway/authz_mixin.py). Delivering via the default profile's bot
    is the exact cross-profile mis-delivery this whole change exists to fix
    (`[230002] Bot can NOT be out of the chat`).

    Mutation check: reverting kanban_watchers.py's adapter selection to the old
    inline ``if adapter is None: adapter = self.adapters.get(plat)`` fallback
    makes this test FAIL (the default adapter receives the delivery).
    """
    db_path = tmp_path / "profile-no-fallback.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="owned by beta", assignee="worker")
        # Subscription is owned by profile "beta".
        kb.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-beta",
            notifier_profile="beta",
        )
        kb.complete_task(conn, tid, summary="done")
    finally:
        conn.close()

    default_adapter = RecordingAdapter()
    other_adapter = RecordingAdapter()
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    # Default profile has a telegram adapter …
    runner.adapters = {Platform.TELEGRAM: default_adapter}
    # … and profile "beta" HAS a non-empty registry entry (so it passes the
    # notifier's upstream skip-filter, which only skips owning profiles with NO
    # adapter at all), but that entry does NOT contain a telegram adapter — beta
    # connected a different platform (discord). The telegram sub owned by beta
    # must therefore resolve to NO adapter, not silently borrow the default
    # profile's telegram bot.
    runner._profile_adapters = {"beta": {Platform.DISCORD: other_adapter}}
    runner._kanban_sub_fail_counts = {}

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # The default profile's adapter must never receive beta's notification.
    assert default_adapter.sent == [], (
        "Owning-profile subscription must not fall back to the default "
        f"profile's adapter; got {default_adapter.sent!r}"
    )
    assert other_adapter.sent == [], (
        f"beta's discord adapter must not receive a telegram sub; got {other_adapter.sent!r}"
    )
    # The claim is rewound (adapter resolved to None → treated as disconnected),
    # so the event is still unseen and will deliver once beta's adapter connects.
    assert [ev.kind for ev in _unseen_terminal_events_for(tid, "chat-beta")] == ["completed"]


def _unseen_terminal_events_for(tid, chat_id):
    conn = kb.connect()
    try:
        _, events = kb.unseen_events_for_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id=chat_id,
            kinds=["completed", "blocked", "gave_up", "crashed", "timed_out"],
        )
        return events
    finally:
        conn.close()


# --- BUILD-506: orphaned tui-subscription sweep, end-to-end through a full
# notifier tick. Unit-level coverage of the sweep primitives themselves
# (age gate, liveness snapshot, CAS race) lives in
# tests/gateway/test_kanban_tui_orphan_sweep.py; the tui-poller-vs-sweep
# race lives in tests/tui_gateway/test_kanban_notify_poller.py.


def _make_runner_with_home(adapter, home_chat_id="home-1"):
    """A runner wired for the Telegram home fallback (BUILD-503/506): the
    same adapter is both the (unused, in these tests) messaging-platform
    adapter and the home-channel destination, mirroring how one Telegram
    adapter instance serves both roles in the real gateway."""
    runner = _make_runner(adapter)
    home = HomeChannel(platform=Platform.TELEGRAM, chat_id=home_chat_id, name="Home")
    runner.config = SimpleNamespace(
        get_home_channel=lambda p: home if p is Platform.TELEGRAM else None
    )
    return runner


def _create_orphaned_tui_sub(chat_id="dead-session", kind="crashed", age_seconds=None):
    """Seed a tui subscription with one failure-kind event, optionally
    backdated so it clears the default age gate without a real 15-minute
    wait."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="desktop worker task", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="tui", chat_id=chat_id)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, kind=kind, payload={"error": "boom"})
        if age_seconds is not None:
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE task_events SET created_at = ? WHERE task_id = ?",
                    (int(time.time()) - age_seconds, tid),
                )
        return tid
    finally:
        conn.close()


def test_kanban_notifier_sweeps_orphaned_tui_sub_to_home_channel(tmp_path, monkeypatch):
    """Regression (BUILD-506): a tui subscription whose desktop is gone
    used to sit unclaimed forever (the exact silent-failure shape BUILD-503
    fixed for reachable-but-dead chats, reopened for tui). Past the age
    gate with no live session, the failure event must reach the Telegram
    home channel with a notify_deliveries row."""
    db_path = tmp_path / "tui-orphan-swept.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    monkeypatch.setattr(
        "hermes_cli.active_sessions.active_session_registry_snapshot", lambda: [],
    )

    tid = _create_orphaned_tui_sub(
        chat_id="dead-session", kind="crashed",
        age_seconds=kw.DEFAULT_TUI_ORPHAN_AGE_SECONDS + 60,
    )

    adapter = RecordingAdapter()
    runner = _make_runner_with_home(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1, adapter.sent
    assert adapter.sent[0]["chat_id"] == "home-1"
    text = adapter.sent[0]["text"]
    assert tid in text
    assert "crashed" in text.lower()

    conn = kb.connect()
    try:
        deliveries = kb.list_notify_deliveries(conn, task_id=tid)
        assert len(deliveries) == 1
        assert deliveries[0]["status"] == "delivered"
        assert deliveries[0]["platform"] == "tui"
        # The subscription itself must survive — the desktop may come back.
        assert len(kb.list_notify_subs(conn, task_id=tid)) == 1
    finally:
        conn.close()


def test_kanban_notifier_skips_tui_sub_with_live_session(tmp_path, monkeypatch):
    """THE RACE THAT MATTERS: an active desktop poller must never have its
    deliveries stolen. A live registry entry for the sub's session must
    make the sweep skip it — even though the event is well past the age
    gate."""
    db_path = tmp_path / "tui-live-session.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    monkeypatch.setattr(
        "hermes_cli.active_sessions.active_session_registry_snapshot",
        lambda: [{"session_id": "live-session", "pid": 999,
                   "metadata": {"live_session_id": "sid-1"}}],
    )

    tid = _create_orphaned_tui_sub(
        chat_id="live-session", kind="blocked",
        age_seconds=kw.DEFAULT_TUI_ORPHAN_AGE_SECONDS + 3600,
    )

    adapter = RecordingAdapter()
    runner = _make_runner_with_home(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert adapter.sent == [], (
        "an active desktop session's failure event must not be swept to "
        f"the home channel; got {adapter.sent!r}"
    )
    conn = kb.connect()
    try:
        assert kb.list_notify_deliveries(conn, task_id=tid) == []
        # Cursor untouched — the (still-live, per the registry) desktop
        # poller owns this event.
        subs = kb.list_notify_subs(conn, task_id=tid)
        assert int(subs[0]["last_event_id"]) == 0
    finally:
        conn.close()


def test_kanban_notifier_skips_fresh_tui_orphan_under_age_gate(tmp_path, monkeypatch):
    """A tui sub with no live session AND a fresh failure event (well under
    the age gate) must not be swept yet — this is the "normal desktop
    restart in progress" window the age gate exists to protect."""
    db_path = tmp_path / "tui-fresh-orphan.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    monkeypatch.setattr(
        "hermes_cli.active_sessions.active_session_registry_snapshot", lambda: [],
    )

    tid = _create_orphaned_tui_sub(chat_id="restarting-session", kind="spawn_failed")

    adapter = RecordingAdapter()
    runner = _make_runner_with_home(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert adapter.sent == []
    conn = kb.connect()
    try:
        assert kb.list_notify_deliveries(conn, task_id=tid) == []
        subs = kb.list_notify_subs(conn, task_id=tid)
        assert int(subs[0]["last_event_id"]) == 0
    finally:
        conn.close()


def test_kanban_notifier_tui_sweep_idempotent_across_restarts(tmp_path, monkeypatch):
    """A fresh runner/tick (simulating a gateway restart) must not
    re-deliver an already-swept orphaned tui sub — the cursor, not any
    in-memory watcher state, is the dedup authority (BUILD-503 pattern)."""
    db_path = tmp_path / "tui-orphan-restart-dedup.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    monkeypatch.setattr(
        "hermes_cli.active_sessions.active_session_registry_snapshot", lambda: [],
    )

    tid = _create_orphaned_tui_sub(
        chat_id="dead-session-2", kind="gave_up",
        age_seconds=kw.DEFAULT_TUI_ORPHAN_AGE_SECONDS + 60,
    )

    first = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner_with_home(first)))
    assert len(first.sent) == 1

    # A brand-new runner instance (nothing carried over in memory) ticking
    # again must find nothing left to claim.
    second = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner_with_home(second)))
    assert second.sent == []

    conn = kb.connect()
    try:
        deliveries = kb.list_notify_deliveries(conn, task_id=tid)
        assert len(deliveries) == 1, (
            "restart must not create a second delivery ledger row"
        )
    finally:
        conn.close()
