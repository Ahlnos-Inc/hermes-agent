import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock


from gateway.config import HomeChannel, Platform
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.run import GatewayRunner
from gateway import kanban_watchers as kw
from hermes_cli import kanban_db as kb


class RecordingAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})


class OwnerWakeAdapter(RecordingAdapter):
    def __init__(self, *, fail_sends=False):
        super().__init__()
        self.handled = []
        self.send_attempts = []
        self.fail_sends = fail_sends

    async def send(self, chat_id, text, metadata=None):
        self.send_attempts.append(chat_id)
        if self.fail_sends:
            raise RuntimeError("simulated human notification failure")
        await super().send(chat_id, text, metadata=metadata)

    async def handle_message(self, event):
        self.handled.append(event)


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


def test_owner_wake_reenters_exact_marketing_session_without_raw_duplicate(
    tmp_path, monkeypatch,
):
    """BUILD-695: delegated completion is a new owner-agent turn.

    The durable internal subscription is independent from the human send.  A
    failing secondary notification must neither prevent nor replay the owner
    wake, and the worker handoff must be context for the Marketing agent rather
    than a raw Telegram message presented as final judgment.
    """
    db_path = tmp_path / "owner-wake.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    owner_context = {
        "profile": "marketing",
        "session_id": "marketing-session-695",
        "session_key": "agent:marketing:telegram:dm:chat-695:topic-695",
        "platform": "telegram",
        "chat_id": "chat-695",
        "chat_type": "dm",
        "thread_id": "topic-695",
        "user_id": "nicholas",
    }
    summary = "Worker evidence only. TAIL_OWNER_HANDOFF"

    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="Prepare campaign brief",
            assignee="writer",
            workflow_key="BUILD-695:marketing",
            current_step_key="brief",
            session_id=owner_context["session_id"],
            owner_context=owner_context,
        )
        # A secondary human target deliberately fails. This must be independent
        # from the owner-agent cursor and must not cause an owner replay.
        kb.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id="ops-console",
            thread_id="kanban-console", notifier_profile="marketing",
        )
        # Even an old/manual transport subscription matching the owner lane
        # must not race the agent turn with a raw worker handoff.
        kb.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-695",
            thread_id="topic-695", notifier_profile="marketing",
        )
        kb.complete_task(conn, tid, summary=summary)
    finally:
        conn.close()

    default_adapter = OwnerWakeAdapter()
    marketing_adapter = OwnerWakeAdapter(fail_sends=True)
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: default_adapter}
    runner._profile_adapters = {
        "marketing": {Platform.TELEGRAM: marketing_adapter},
    }
    runner._kanban_sub_fail_counts = {}
    runner._kanban_notifier_profile = "default"
    setattr(runner, "_kanban_notification_sources", {"marketing"})

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert default_adapter.handled == []
    assert default_adapter.sent == []
    assert len(marketing_adapter.handled) == 1
    wake = marketing_adapter.handled[0]
    assert wake.internal is True
    assert wake.source.profile == "marketing"
    assert wake.source.chat_id == "chat-695"
    assert wake.source.chat_type == "dm"
    assert wake.source.thread_id == "topic-695"
    assert wake.source.user_id == "nicholas"
    assert wake.metadata["gateway_session_id"] == "marketing-session-695"
    assert wake.metadata["gateway_session_key"] == owner_context["session_key"]
    assert wake.metadata["kanban_owner_wake"] is True
    assert tid in wake.text
    assert "BUILD-695:marketing" in wake.text
    assert "brief" in wake.text
    assert "completed" in wake.text
    assert "TAIL_OWNER_HANDOFF" in wake.text
    assert "inspect the current Kanban state" in wake.text
    # No raw worker completion was sent to the owner chat. The eventual user
    # message comes from the Marketing agent turn recorded above.
    assert marketing_adapter.sent == []
    assert marketing_adapter.send_attempts == ["ops-console"]

    # Fresh runner = gateway restart. The durable owner cursor suppresses the
    # already-injected event even though the human send failed and rewound its
    # separate cursor.
    restarted_marketing = OwnerWakeAdapter(fail_sends=True)
    restarted = GatewayRunner.__new__(GatewayRunner)
    restarted._running = True
    restarted.adapters = {Platform.TELEGRAM: OwnerWakeAdapter()}
    restarted._profile_adapters = {
        "marketing": {Platform.TELEGRAM: restarted_marketing},
    }
    restarted._kanban_sub_fail_counts = {}
    asyncio.run(_run_one_notifier_tick(monkeypatch, restarted))
    assert restarted_marketing.handled == []


def test_owner_wakes_keep_blocked_forum_and_concurrent_dm_workflows_isolated(
    tmp_path, monkeypatch,
):
    """Marketing blocks and a concurrent DM completion return to their owners."""
    db_path = tmp_path / "concurrent-owner-wakes.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    marketing_owner = {
        "profile": "marketing",
        "session_id": "marketing-forum-session",
        "session_key": "agent:marketing:telegram:group:marketing-chat:topic-17",
        "platform": "telegram",
        "chat_id": "marketing-chat",
        "chat_type": "group",
        "thread_id": "topic-17",
        "user_id": "nicholas",
    }
    orchestrator_owner = {
        "profile": "orchestrator",
        "session_id": "orchestrator-dm-session",
        "session_key": "agent:orchestrator:telegram:dm:orchestrator-chat",
        "platform": "telegram",
        "chat_id": "orchestrator-chat",
        "chat_type": "dm",
        "user_id": "nicholas",
    }

    with kb.connect() as conn:
        blocked_tid = kb.create_task(
            conn,
            title="Marketing forum stage",
            assignee="writer",
            workflow_key="BUILD-695:marketing-blocked",
            current_step_key="copy",
            session_id=marketing_owner["session_id"],
            owner_context=marketing_owner,
        )
        completed_tid = kb.create_task(
            conn,
            title="Concurrent DM stage",
            assignee="researcher",
            workflow_key="BUILD-695:orchestrator-complete",
            current_step_key="research",
            session_id=orchestrator_owner["session_id"],
            owner_context=orchestrator_owner,
        )
        kb.block_task(conn, blocked_tid, reason="Needs campaign choice. TAIL_BLOCK_REASON")
        kb.complete_task(conn, completed_tid, summary="Research ready. TAIL_DM_SUMMARY")

    default_adapter = OwnerWakeAdapter()
    marketing_adapter = OwnerWakeAdapter()
    orchestrator_adapter = OwnerWakeAdapter()
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: default_adapter}
    runner._profile_adapters = {
        "marketing": {Platform.TELEGRAM: marketing_adapter},
        "orchestrator": {Platform.TELEGRAM: orchestrator_adapter},
    }
    runner._kanban_sub_fail_counts = {}

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert default_adapter.sent == []
    assert default_adapter.handled == []
    assert len(marketing_adapter.handled) == 1
    marketing_wake = marketing_adapter.handled[0]
    assert marketing_wake.source.chat_id == "marketing-chat"
    assert marketing_wake.source.chat_type == "group"
    assert marketing_wake.source.thread_id == "topic-17"
    assert marketing_wake.metadata["gateway_session_id"] == "marketing-forum-session"
    assert marketing_wake.metadata["gateway_session_key"] == marketing_owner["session_key"]
    assert blocked_tid in marketing_wake.text
    assert "BUILD-695:marketing-blocked" in marketing_wake.text
    assert "copy" in marketing_wake.text
    assert "blocked" in marketing_wake.text
    assert "TAIL_BLOCK_REASON" in marketing_wake.text

    assert len(orchestrator_adapter.handled) == 1
    orchestrator_wake = orchestrator_adapter.handled[0]
    assert orchestrator_wake.source.chat_id == "orchestrator-chat"
    assert orchestrator_wake.source.chat_type == "dm"
    assert orchestrator_wake.source.thread_id is None
    assert orchestrator_wake.metadata["gateway_session_id"] == "orchestrator-dm-session"
    assert completed_tid in orchestrator_wake.text
    assert "BUILD-695:orchestrator-complete" in orchestrator_wake.text
    assert "research" in orchestrator_wake.text
    assert "completed" in orchestrator_wake.text
    assert "TAIL_DM_SUMMARY" in orchestrator_wake.text


def test_owner_wake_uses_primary_adapter_for_active_named_profile(
    tmp_path, monkeypatch,
):
    """A standalone named-profile gateway owns ``runner.adapters`` directly."""
    db_path = tmp_path / "named-owner-wake.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    owner = {
        "profile": "marketing",
        "session_id": "standalone-marketing-session",
        "session_key": "agent:main:telegram:dm:marketing-chat",
        "platform": "telegram",
        "chat_id": "marketing-chat",
        "chat_type": "dm",
        "user_id": "nicholas",
    }
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="Standalone Marketing stage",
            assignee="writer",
            owner_context=owner,
        )
        kb.complete_task(conn, tid, summary="Standalone owner ready")

    marketing_adapter = OwnerWakeAdapter()
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    setattr(runner, "adapters", {Platform.TELEGRAM: marketing_adapter})
    runner._profile_adapters = {}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_notifier_profile = "marketing"

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(marketing_adapter.handled) == 1
    assert marketing_adapter.handled[0].source.profile == "marketing"


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


# --- BUILD-695 P1 regression: NULL notifier_profile raw sub suppressed -------


def test_legacy_null_profile_raw_sub_suppressed_by_owner_context(tmp_path, monkeypatch):
    """BUILD-695 P1 regression: pre-existing raw sub with NULL notifier_profile.

    Scenario: a task is created without an owner context (e.g. dispatched by
    the CLI before the gateway session was established), then a raw notification
    subscription with notifier_profile=NULL is added targeting the future
    owner's platform/chat_id/thread_id.  When set_task_owner_context later
    stamps that route as the durable owner, the notifier must suppress the
    legacy raw transport sub — it cannot bypass _transport_sub_targets_owner
    just because notifier_profile was NULL at subscription time.

    Assertions:
    - exactly one owner-agent wake (handle_message) fires on the owner adapter
    - no raw adapter send is made to the owner's chat_id (the owner's agent
      turn is the user-facing update, not a raw worker handoff)
    - a subscription to a DISTINCT console topic is still delivered normally
      (suppress only the exact owner triple, not all subs on the task)
    """
    db_path = tmp_path / "legacy-null-profile.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    owner_context = {
        "profile": "marketing",
        "session_id": "mkt-session-695p1",
        "session_key": "agent:marketing:telegram:dm:chat-mkt:thread-mkt",
        "platform": "telegram",
        "chat_id": "chat-mkt",
        "chat_type": "dm",
        "thread_id": "thread-mkt",
        "user_id": "nicholas",
    }

    conn = kb.connect()
    try:
        # Create task WITHOUT owner_context — the legacy path (e.g. CLI task
        # creation before the gateway session is known).
        tid = kb.create_task(
            conn,
            title="Legacy raw-owner-duplicate task",
            assignee="coder",
        )
        # Pre-existing raw sub (notifier_profile=NULL) targeting the future
        # owner's exact conversation. Before BUILD-695 P1 fix this bypassed
        # _transport_sub_targets_owner because "" != "marketing", causing a
        # raw worker handoff to race the owner-agent turn.
        kb.add_notify_sub(
            conn, task_id=tid, platform="telegram",
            chat_id="chat-mkt", thread_id="thread-mkt",
        )
        # A DISTINCT console topic: must NOT be suppressed (only the owner
        # triple is silenced, not all subscriptions).
        kb.add_notify_sub(
            conn, task_id=tid, platform="telegram",
            chat_id="ops-console-695", thread_id="kanban-thread-695",
        )
        # Gateway stamps the owner route (typically from the first agent turn).
        # This also inserts the durable owner_agent wake subscription.
        kb.set_task_owner_context(conn, tid, owner_context)
        # Worker completes the task.
        kb.complete_task(conn, tid, summary="Work done. LEGACY_P1_TAIL")
    finally:
        conn.close()

    telegram_adapter = RecordingAdapter()   # default-profile Telegram adapter
    marketing_adapter = OwnerWakeAdapter()  # marketing-profile adapter (owns owner wake)

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: telegram_adapter}
    runner._profile_adapters = {
        "marketing": {Platform.TELEGRAM: marketing_adapter},
    }
    runner._kanban_sub_fail_counts = {}
    runner._kanban_notifier_profile = "default"
    setattr(runner, "_kanban_notification_sources", {"marketing"})

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # One owner-agent wake must fire on the marketing adapter.
    assert len(marketing_adapter.handled) == 1, marketing_adapter.handled
    wake = marketing_adapter.handled[0]
    assert wake.internal is True
    assert wake.source.chat_id == "chat-mkt"
    assert wake.metadata["kanban_owner_wake"] is True
    assert tid in wake.text
    assert "LEGACY_P1_TAIL" in wake.text

    # No raw send must reach the owner's conversation.
    assert marketing_adapter.sent == [], (
        "raw send to owner conversation must be suppressed — "
        "notifier_profile=NULL must not bypass the owner-route filter"
    )
    raw_to_owner = [s for s in telegram_adapter.sent if s["chat_id"] == "chat-mkt"]
    assert raw_to_owner == [], (
        "raw adapter must not send to owner chat_id even with NULL notifier_profile"
    )

    # The distinct console topic is delivered normally.
    console_sends = [s for s in telegram_adapter.sent if s["chat_id"] == "ops-console-695"]
    assert len(console_sends) == 1, (
        "distinct console topic must still receive the completion notification"
    )
    assert "LEGACY_P1_TAIL" in console_sends[0]["text"]


class _RuntimeOwnerWakeAdapter(BasePlatformAdapter):
    """Real adapter intake path with a deterministic rendered agent reply."""

    def __init__(self):
        from gateway.config import PlatformConfig

        super().__init__(PlatformConfig(enabled=True), Platform.TELEGRAM)
        self.sent = []

    async def connect(self, *, is_reconnect: bool = False):
        return True

    async def disconnect(self):
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append({
            "chat_id": chat_id,
            "content": content,
            "metadata": metadata or {},
        })
        return SendResult(success=True, message_id=str(len(self.sent)))

    async def send_typing(self, chat_id, metadata=None):
        return None

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}


def test_owner_wake_runs_real_adapter_and_runner_pipeline_to_marketing_thread(
    tmp_path, monkeypatch,
):
    """BUILD-695: durable owner wake produces exactly one real user update.

    This deliberately exercises BasePlatformAdapter.handle_message() plus
    GatewayRunner._handle_message(), rather than asserting a recording
    handle_message stub received a synthetic event. The agent boundary is
    deterministic: it returns a Marketing-authored update that the real
    adapter background path delivers into the persisted forum thread.
    """
    db_path = tmp_path / "owner-wake-runtime.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    owner = {
        "profile": "marketing",
        "session_id": "marketing-session-runtime-695",
        "session_key": "agent:marketing:telegram:group:marketing-chat:topic-695",
        "platform": "telegram",
        "chat_id": "marketing-chat",
        "chat_type": "group",
        "thread_id": "topic-695",
        "user_id": "nicholas",
    }
    with kb.connect() as conn:
        completed_id = kb.create_task(
            conn,
            title="Completed marketing handoff",
            assignee="writer",
            workflow_key="BUILD-695:marketing-runtime",
            current_step_key="copy",
            session_id=owner["session_id"],
            owner_context=owner,
        )
        kb.complete_task(conn, completed_id, summary="Copy ready. RUNTIME_COMPLETED_TAIL")

    adapter = _RuntimeOwnerWakeAdapter()
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.config = SimpleNamespace(
        group_sessions_per_user=True,
        thread_sessions_per_user=False,
        multiplex_profiles=True,
        extra={},
    )
    runner.adapters = {}
    runner._profile_adapters = {"marketing": {Platform.TELEGRAM: adapter}}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_notifier_profile = "default"
    runner._kanban_notification_sources = {"marketing"}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._active_session_leases = {}
    runner._pending_messages = {}
    runner._update_prompt_pending = {}
    runner._session_run_generation = {}
    runner._session_model_overrides = {}
    runner._external_drain_active = False
    runner._session_db = None
    runner.session_store = SimpleNamespace(
        _generate_session_key=lambda source: owner["session_key"],
    )
    runner._async_session_store = SimpleNamespace(
        get_or_create_session=AsyncMock(
            return_value=SimpleNamespace(
                session_key=owner["session_key"], session_id=owner["session_id"],
            )
        ),
    )
    runner._persist_active_agents = lambda: None
    runner._active_session_limit_message = lambda _key: None
    runner._claim_active_session_slot = lambda _key, _source: (None, None)
    runner._begin_session_run_generation = lambda _key: 1
    runner._restore_moa_one_shot = lambda _event, _key: None
    runner._release_running_agent_state = lambda _key: True
    runner._is_telegram_topic_root_lobby = lambda _source: False
    runner._recover_telegram_topic_thread_id = lambda _source: None
    runner._is_user_authorized = lambda _source: True
    runner._active_profile_name = lambda: "default"
    runner.pairing_store = SimpleNamespace()
    runner.hooks = SimpleNamespace(emit_collect=AsyncMock(return_value=[]))
    runner._handle_message_with_agent = AsyncMock(
        return_value="Marketing update: copy is ready; brand approval is needed."
    )

    adapter.set_message_handler(runner._handle_message)
    adapter.set_busy_session_handler(runner._handle_active_session_busy_message)

    async def _run_runtime_tick():
        await _run_one_notifier_tick(monkeypatch, runner)
        while adapter._background_tasks:
            await asyncio.gather(*tuple(adapter._background_tasks))

    asyncio.run(_run_runtime_tick())

    assert runner._handle_message_with_agent.await_count == 1
    wakes = [call.args[0] for call in runner._handle_message_with_agent.await_args_list]
    assert {wake.metadata["kanban_task_id"] for wake in wakes} == {completed_id}
    assert all(wake.internal is True for wake in wakes)
    assert all(wake.source.profile == "marketing" for wake in wakes)
    assert all(wake.source.chat_id == "marketing-chat" for wake in wakes)
    assert all(wake.source.thread_id == "topic-695" for wake in wakes)
    assert all(wake.metadata["gateway_session_id"] == owner["session_id"] for wake in wakes)
    assert all(wake.metadata["gateway_session_key"] == owner["session_key"] for wake in wakes)
    assert any("RUNTIME_COMPLETED_TAIL" in wake.text for wake in wakes)

    # Real BasePlatformAdapter background delivery reaches only the exact
    # persisted source thread, and contains agent-authored text rather than a
    # raw worker handoff.
    assert len(adapter.sent) == 1
    assert {sent["chat_id"] for sent in adapter.sent} == {"marketing-chat"}
    assert all(sent["metadata"]["thread_id"] == "topic-695" for sent in adapter.sent)
    assert all(sent["content"].startswith("Marketing update:") for sent in adapter.sent)
    assert all("RUNTIME_" not in sent["content"] for sent in adapter.sent)


def test_owner_wake_cursor_not_advanced_until_bg_task_done_restart_recovery(
    tmp_path, monkeypatch,
):
    """BUILD-695 P1 regression: cursor must stay at pre-peek value until bg task done.

    Verifies three invariants of the deferred-advance fix:

    A) While the background agent turn is still in-flight (not yet done), the
       owner wake subscription cursor stays at its pre-tick position in the DB.
       A process crash at this moment would leave the event visible to a fresh
       restart — i.e. the wake is re-delivered rather than silently dropped.

    B) After the background turn completes, the done-callback advances the cursor
       so the event is no longer unseen (no ghost delivery on next tick).

    C) A second notifier run with a fresh runner (no in-process inflight state,
       simulating a restarted process) does NOT re-deliver once the cursor has
       been advanced by the completed first runner (idempotent delivery).
    """
    db_path = tmp_path / "owner-wake-cursor-deferred-695.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    # DM session — session_key uses build_session_key(source) without a profile
    # arg, so the namespace is "agent:main" regardless of source.profile.
    # Both BasePlatformAdapter.handle_message() and the notifier compute the same
    # key this way, ensuring _session_tasks lookup succeeds.
    owner = {
        "profile": "ops",
        "session_id": "ops-session-restart-695",
        "session_key": "agent:main:telegram:dm:ops-chat:thread-ops",
        "platform": "telegram",
        "chat_id": "ops-chat",
        "chat_type": "dm",
        "thread_id": "thread-ops",
        "user_id": "nicholas",
    }

    with kb.connect() as conn:
        completed_id = kb.create_task(
            conn,
            title="Deferred-cursor recovery task",
            assignee="worker",
            session_id=owner["session_id"],
            owner_context=owner,
        )
        kb.complete_task(conn, completed_id, summary="Done. DEFERRED_CURSOR_TAIL")

    def _make_ops_runner():
        adapter = _RuntimeOwnerWakeAdapter()
        runner = GatewayRunner.__new__(GatewayRunner)
        runner._running = True
        runner.config = SimpleNamespace(
            group_sessions_per_user=True,
            thread_sessions_per_user=False,
            multiplex_profiles=True,
            extra={},
        )
        runner.adapters = {}
        runner._profile_adapters = {"ops": {Platform.TELEGRAM: adapter}}
        runner._kanban_sub_fail_counts = {}
        runner._kanban_notifier_profile = "default"
        runner._kanban_notification_sources = {"ops"}
        runner._running_agents = {}
        runner._running_agents_ts = {}
        runner._active_session_leases = {}
        runner._pending_messages = {}
        runner._update_prompt_pending = {}
        runner._session_run_generation = {}
        runner._session_model_overrides = {}
        runner._external_drain_active = False
        runner._session_db = None
        runner.session_store = SimpleNamespace(
            _generate_session_key=lambda source: owner["session_key"],
        )
        runner._async_session_store = SimpleNamespace(
            get_or_create_session=AsyncMock(
                return_value=SimpleNamespace(
                    session_key=owner["session_key"],
                    session_id=owner["session_id"],
                )
            ),
        )
        runner._persist_active_agents = lambda: None
        runner._active_session_limit_message = lambda _key: None
        runner._claim_active_session_slot = lambda _key, _source: (None, None)
        runner._begin_session_run_generation = lambda _key: 1
        runner._restore_moa_one_shot = lambda _event, _key: None
        runner._release_running_agent_state = lambda _key: True
        runner._is_telegram_topic_root_lobby = lambda _source: False
        runner._recover_telegram_topic_thread_id = lambda _source: None
        runner._is_user_authorized = lambda _source: True
        runner._active_profile_name = lambda: "default"
        runner.pairing_store = SimpleNamespace()
        runner.hooks = SimpleNamespace(emit_collect=AsyncMock(return_value=[]))
        runner._handle_message_with_agent = AsyncMock(
            return_value="Ops wake reply BUILD-695."
        )
        adapter.set_message_handler(runner._handle_message)
        adapter.set_busy_session_handler(runner._handle_active_session_busy_message)
        return runner, adapter

    async def _scenario():
        # Capture the real asyncio.sleep BEFORE _run_one_notifier_tick patches it,
        # so Phase B can use genuine yields even while the monkeypatch is active.
        _real_sleep = asyncio.sleep

        # ------------------------------------------------------------------ #
        # Phase A: bg task is in-flight (blocked) → cursor NOT advanced       #
        # Simulates: process crash between handle_message() scheduling and    #
        # the agent turn actually executing.                                  #
        # ------------------------------------------------------------------ #
        release_gate = asyncio.Event()

        async def _blocking_agent(*args, **kwargs):
            """Block until the test releases the gate (simulates slow/pending turn)."""
            await release_gate.wait()
            return "Ops reply after gate."

        runner1, adapter1 = _make_ops_runner()
        # Replace the default mock with one that blocks mid-turn.
        runner1._handle_message_with_agent = AsyncMock(side_effect=_blocking_agent)

        # Tick runs: notifier peeks the "completed" event, calls handle_message(),
        # which spawns a bg task that immediately blocks on release_gate.
        # The fake_sleep(1) path in _run_one_notifier_tick yields once so the
        # bg task starts and hits its first suspend point before returning.
        await _run_one_notifier_tick(monkeypatch, runner1)

        # Invariant A: event is still unseen — cursor was NOT advanced.
        # A process crash right now would leave the event visible; a restarting
        # process with an empty _kanban_owner_inflight would re-deliver it.
        with kb.connect() as conn:
            _, unseen_a = kb.unseen_events_for_sub(
                conn,
                task_id=completed_id,
                platform=kb.OWNER_AGENT_NOTIFY_PLATFORM,
                chat_id=owner["session_id"],
                kinds=list(kb.OWNER_WAKE_KINDS),
            )
        assert len(unseen_a) == 1, (
            "BUILD-695 P1 Invariant A: owner wake cursor must NOT advance "
            "before bg task completes — event still unseen, so a crash here "
            "would enable re-delivery on restart"
        )

        # The in-process idempotency guard is set: same process would not
        # re-schedule, but a fresh process (empty dict) would re-deliver.
        assert completed_id in getattr(runner1, "_kanban_owner_inflight", {}), (
            "in-process inflight guard must be set after scheduling "
            "(prevents same-process duplicate within one live session)"
        )

        # ------------------------------------------------------------------ #
        # Phase B: release bg task → done callback → cursor advance          #
        # ------------------------------------------------------------------ #
        release_gate.set()
        # Drain the background agent task — same idiom as the existing runtime
        # test. asyncio.gather works regardless of asyncio.sleep monkeypatching
        # because it directly awaits task Futures rather than sleeping.
        while adapter1._background_tasks:
            await asyncio.gather(*tuple(adapter1._background_tasks))
        # Done callbacks (_on_owner_bg_done) have now fired, creating the
        # _advance_after_wake task.  Poll until the cursor advances — this
        # avoids a race between the asyncio.to_thread call inside
        # _advance_after_wake and the test's tick count.  Each tick gives
        # the GIL to thread-pool threads via the synchronous kb.connect()
        # call, so 50 ticks is far more than the 2-3 actually needed.
        _unseen_for_b: list = [object()]  # sentinel until we get real data
        for _ in range(50):
            await _real_sleep(0)
            with kb.connect() as _poll_conn:
                _, _unseen_for_b = kb.unseen_events_for_sub(
                    _poll_conn,
                    task_id=completed_id,
                    platform=kb.OWNER_AGENT_NOTIFY_PLATFORM,
                    chat_id=owner["session_id"],
                    kinds=list(kb.OWNER_WAKE_KINDS),
                )
            if not _unseen_for_b:
                break

        # Invariant B: cursor is now advanced past the "completed" event.
        # _unseen_for_b comes from the poll loop above (empty iff cursor advanced).
        assert len(_unseen_for_b) == 0, (
            "BUILD-695 P1 Invariant B: cursor must advance after bg task "
            "done callback fires via _advance_after_wake"
        )

        # ------------------------------------------------------------------ #
        # Phase C: fresh runner (simulates restart after clean shutdown) →   #
        # no re-delivery once cursor is advanced (idempotent delivery)       #
        # ------------------------------------------------------------------ #
        runner2, _adapter2 = _make_ops_runner()
        # runner2 has no _kanban_owner_inflight (fresh process state).
        assert not getattr(runner2, "_kanban_owner_inflight", {}), (
            "fresh runner must start with empty inflight dict"
        )

        await _run_one_notifier_tick(monkeypatch, runner2)

        assert runner2._handle_message_with_agent.await_count == 0, (
            "BUILD-695 P1 Invariant C: fresh runner must NOT re-deliver "
            "after cursor was advanced — idempotent owner wake delivery"
        )

    asyncio.run(_scenario())


def test_cross_process_owner_wake_only_one_turn_scheduled(tmp_path, monkeypatch):
    """BUILD-695 P2: two gateway instances sharing one DB must schedule only one owner turn.

    The in-process ``_kanban_owner_inflight`` dict prevents duplicate scheduling
    within a single process, but two separate gateway processes both start with
    an empty dict and can both see the same peeked event.  The durable
    ``owner_wake_leases`` table in the DB is the cross-process admission gate:
    only the first process to INSERT a lease row for (task_id, event_id) may
    proceed; the second sees the existing row and skips.

    This test simulates two runners sharing one DB by running their notifier
    ticks sequentially (which is equivalent to concurrent real-process ticks
    because SQLite's write lock serializes concurrent writers anyway).  Runner1
    goes first, claims the lease, and schedules a bg task.  Runner2 goes next
    with a fresh (empty) in-process inflight dict; it must NOT schedule a
    second bg task because the lease is already held.
    """
    db_path = tmp_path / "cross-process-695.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    owner = {
        "profile": "ops",
        "session_id": "ops-session-cross-proc-695",
        "session_key": "agent:main:telegram:dm:ops-xp:thread-xp",
        "platform": "telegram",
        "chat_id": "ops-xp",
        "chat_type": "dm",
        "thread_id": "thread-xp",
        "user_id": "nicholas",
    }

    with kb.connect() as conn:
        completed_id = kb.create_task(
            conn,
            title="Cross-process dedup task",
            assignee="worker",
            session_id=owner["session_id"],
            owner_context=owner,
        )
        kb.complete_task(conn, completed_id, summary="Cross-proc done.")

    def _make_owner_runner():
        adapter = _RuntimeOwnerWakeAdapter()
        runner = GatewayRunner.__new__(GatewayRunner)
        runner._running = True
        runner.config = SimpleNamespace(
            group_sessions_per_user=True,
            thread_sessions_per_user=False,
            multiplex_profiles=True,
            extra={},
        )
        runner.adapters = {}
        runner._profile_adapters = {"ops": {Platform.TELEGRAM: adapter}}
        runner._kanban_sub_fail_counts = {}
        runner._kanban_notifier_profile = "default"
        runner._kanban_notification_sources = {"ops"}
        runner._running_agents = {}
        runner._running_agents_ts = {}
        runner._active_session_leases = {}
        runner._pending_messages = {}
        runner._update_prompt_pending = {}
        runner._session_run_generation = {}
        runner._session_model_overrides = {}
        runner._external_drain_active = False
        runner._session_db = None
        runner.session_store = SimpleNamespace(
            _generate_session_key=lambda source: owner["session_key"],
        )
        runner._async_session_store = SimpleNamespace(
            get_or_create_session=AsyncMock(
                return_value=SimpleNamespace(
                    session_key=owner["session_key"],
                    session_id=owner["session_id"],
                )
            ),
        )
        runner._persist_active_agents = lambda: None
        runner._active_session_limit_message = lambda _key: None
        runner._claim_active_session_slot = lambda _key, _source: (None, None)
        runner._begin_session_run_generation = lambda _key: 1
        runner._restore_moa_one_shot = lambda _event, _key: None
        runner._release_running_agent_state = lambda _key: True
        runner._is_telegram_topic_root_lobby = lambda _source: False
        runner._recover_telegram_topic_thread_id = lambda _source: None
        runner._is_user_authorized = lambda _source: True
        runner._active_profile_name = lambda: "default"
        runner.pairing_store = SimpleNamespace()
        runner.hooks = SimpleNamespace(emit_collect=AsyncMock(return_value=[]))
        runner._handle_message_with_agent = AsyncMock(
            return_value="Cross-proc wake reply."
        )
        adapter.set_message_handler(runner._handle_message)
        adapter.set_busy_session_handler(runner._handle_active_session_busy_message)
        return runner, adapter

    async def _scenario():
        real_sleep = asyncio.sleep

        # Runner1 ticks first: it should claim the lease and schedule a bg task.
        runner1, adapter1 = _make_owner_runner()
        await _run_one_notifier_tick(monkeypatch, runner1)

        # Invariant: runner1 scheduled exactly one agent turn.
        # Wait for the background task to start.
        for _ in range(20):
            await real_sleep(0)
            if adapter1._background_tasks:
                break
        assert len(adapter1._background_tasks) >= 1, (
            "runner1 must have spawned a background task for the owner wake"
        )

        # Verify the lease exists in the DB.
        with kb.connect() as conn:
            lease_row = conn.execute(
                "SELECT task_id, event_id FROM owner_wake_leases WHERE task_id = ?",
                (completed_id,),
            ).fetchone()
        assert lease_row is not None, (
            "BUILD-695 P2: owner_wake_leases must contain a row after runner1 claims"
        )

        # Runner2 has an empty _kanban_owner_inflight (simulates a separate process).
        runner2, adapter2 = _make_owner_runner()
        assert not getattr(runner2, "_kanban_owner_inflight", {}), (
            "runner2 must start with empty in-process inflight dict"
        )

        # Runner2 ticks: the DB lease is already held by runner1, so it must skip.
        await _run_one_notifier_tick(monkeypatch, runner2)

        assert runner2._handle_message_with_agent.await_count == 0, (
            "BUILD-695 P2: second runner must NOT schedule a duplicate owner turn "
            "when the cross-process lease is already held"
        )

        # Drain runner1's background task so _advance_after_wake fires.
        while adapter1._background_tasks:
            await asyncio.gather(*tuple(adapter1._background_tasks))
        for _ in range(50):
            await real_sleep(0)
            with kb.connect() as _conn:
                _, unseen = kb.unseen_events_for_sub(
                    _conn,
                    task_id=completed_id,
                    platform=kb.OWNER_AGENT_NOTIFY_PLATFORM,
                    chat_id=owner["session_id"],
                    kinds=list(kb.OWNER_WAKE_KINDS),
                )
            if not unseen:
                break

        # After completion the lease must be released and the cursor advanced.
        with kb.connect() as conn:
            lease_after = conn.execute(
                "SELECT task_id FROM owner_wake_leases WHERE task_id = ?",
                (completed_id,),
            ).fetchone()
        assert lease_after is None, (
            "BUILD-695 P2: lease must be deleted after cursor advance"
        )

        with kb.connect() as conn:
            _, unseen_final = kb.unseen_events_for_sub(
                conn,
                task_id=completed_id,
                platform=kb.OWNER_AGENT_NOTIFY_PLATFORM,
                chat_id=owner["session_id"],
                kinds=list(kb.OWNER_WAKE_KINDS),
            )
        assert len(unseen_final) == 0, (
            "BUILD-695 P2: cursor must be advanced after runner1 bg task completes"
        )

        # Total: exactly one agent turn across both runners.
        total_turns = (
            runner1._handle_message_with_agent.await_count
            + runner2._handle_message_with_agent.await_count
        )
        assert total_turns == 1, (
            f"BUILD-695 P2: exactly one owner turn must fire across two runners; "
            f"got {total_turns}"
        )

    asyncio.run(_scenario())


def test_cross_process_owner_wake_expired_lease_enables_recovery(
    tmp_path, monkeypatch,
):
    """BUILD-695 P2: an expired lease must be reclaimable so crash recovery works.

    Scenario:
    A) Process A claims the lease and crashes before the bg turn executes.
       The cursor is NOT advanced (that is the P1 guarantee). The lease row
       in owner_wake_leases has an expires_at in the past.
    B) Process B (fresh restart) ticks. It sees the expired lease and reclaims
       it (try_claim_owner_wake_lease uses UPDATE WHERE expires_at < now).
    C) Process B schedules the owner turn and advances the cursor.

    Steps A is simulated by inserting a lease row with expires_at = now - 1.
    Step B/C runs a fresh runner and asserts the owner turn fires.
    """
    db_path = tmp_path / "expired-lease-695.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    owner = {
        "profile": "ops",
        "session_id": "ops-session-expired-695",
        "session_key": "agent:main:telegram:dm:ops-exp:thread-exp",
        "platform": "telegram",
        "chat_id": "ops-exp",
        "chat_type": "dm",
        "thread_id": "thread-exp",
        "user_id": "nicholas",
    }

    with kb.connect() as conn:
        completed_id = kb.create_task(
            conn,
            title="Expired lease recovery task",
            assignee="worker",
            session_id=owner["session_id"],
            owner_context=owner,
        )
        kb.complete_task(conn, completed_id, summary="Expired lease recovery done.")

    # Peek the event_id to know what to put in the lease.
    with kb.connect() as conn:
        _, pending = kb.unseen_events_for_sub(
            conn,
            task_id=completed_id,
            platform=kb.OWNER_AGENT_NOTIFY_PLATFORM,
            chat_id=owner["session_id"],
            kinds=list(kb.OWNER_WAKE_KINDS),
        )
    assert len(pending) == 1, "setup: must have exactly one pending owner wake event"
    event_id = pending[0].id

    # Simulate: process A claimed the lease but crashed (expired row, cursor at 0).
    with kb.connect() as conn:
        stale_time = time.time() - 3600  # 1 hour ago — definitely expired
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO owner_wake_leases (task_id, event_id, claimed_at, expires_at)"
                " VALUES (?, ?, ?, ?)",
                (completed_id, event_id, stale_time, stale_time),
            )

    # Verify the event is still unseen (cursor not advanced by the crashed process).
    with kb.connect() as conn:
        _, still_unseen = kb.unseen_events_for_sub(
            conn,
            task_id=completed_id,
            platform=kb.OWNER_AGENT_NOTIFY_PLATFORM,
            chat_id=owner["session_id"],
            kinds=list(kb.OWNER_WAKE_KINDS),
        )
    assert len(still_unseen) == 1, (
        "setup: event must still be unseen (cursor not advanced by crashed process)"
    )

    # Process B: a fresh runner that will reclaim the expired lease.
    def _make_recovery_runner():
        adapter = _RuntimeOwnerWakeAdapter()
        runner = GatewayRunner.__new__(GatewayRunner)
        runner._running = True
        runner.config = SimpleNamespace(
            group_sessions_per_user=True,
            thread_sessions_per_user=False,
            multiplex_profiles=True,
            extra={},
        )
        runner.adapters = {}
        runner._profile_adapters = {"ops": {Platform.TELEGRAM: adapter}}
        runner._kanban_sub_fail_counts = {}
        runner._kanban_notifier_profile = "default"
        runner._kanban_notification_sources = {"ops"}
        runner._running_agents = {}
        runner._running_agents_ts = {}
        runner._active_session_leases = {}
        runner._pending_messages = {}
        runner._update_prompt_pending = {}
        runner._session_run_generation = {}
        runner._session_model_overrides = {}
        runner._external_drain_active = False
        runner._session_db = None
        runner.session_store = SimpleNamespace(
            _generate_session_key=lambda source: owner["session_key"],
        )
        runner._async_session_store = SimpleNamespace(
            get_or_create_session=AsyncMock(
                return_value=SimpleNamespace(
                    session_key=owner["session_key"],
                    session_id=owner["session_id"],
                )
            ),
        )
        runner._persist_active_agents = lambda: None
        runner._active_session_limit_message = lambda _key: None
        runner._claim_active_session_slot = lambda _key, _source: (None, None)
        runner._begin_session_run_generation = lambda _key: 1
        runner._restore_moa_one_shot = lambda _event, _key: None
        runner._release_running_agent_state = lambda _key: True
        runner._is_telegram_topic_root_lobby = lambda _source: False
        runner._recover_telegram_topic_thread_id = lambda _source: None
        runner._is_user_authorized = lambda _source: True
        runner._active_profile_name = lambda: "default"
        runner.pairing_store = SimpleNamespace()
        runner.hooks = SimpleNamespace(emit_collect=AsyncMock(return_value=[]))
        runner._handle_message_with_agent = AsyncMock(
            return_value="Recovery wake reply."
        )
        adapter.set_message_handler(runner._handle_message)
        adapter.set_busy_session_handler(runner._handle_active_session_busy_message)
        return runner, adapter

    async def _scenario():
        real_sleep = asyncio.sleep

        recovery_runner, recovery_adapter = _make_recovery_runner()

        # Fresh process: in-process inflight dict is empty.
        assert not getattr(recovery_runner, "_kanban_owner_inflight", {}), (
            "recovery runner must start with empty inflight dict"
        )

        # Tick: should reclaim the expired lease and schedule the owner turn.
        await _run_one_notifier_tick(monkeypatch, recovery_runner)

        # Wait for background task.
        for _ in range(20):
            await real_sleep(0)
            if recovery_adapter._background_tasks:
                break

        assert len(recovery_adapter._background_tasks) >= 1, (
            "BUILD-695 P2 recovery: fresh runner must reclaim expired lease and "
            "schedule the owner turn"
        )

        # Drain the bg task so _advance_after_wake fires.
        while recovery_adapter._background_tasks:
            await asyncio.gather(*tuple(recovery_adapter._background_tasks))
        for _ in range(50):
            await real_sleep(0)
            with kb.connect() as _conn:
                _, unseen = kb.unseen_events_for_sub(
                    _conn,
                    task_id=completed_id,
                    platform=kb.OWNER_AGENT_NOTIFY_PLATFORM,
                    chat_id=owner["session_id"],
                    kinds=list(kb.OWNER_WAKE_KINDS),
                )
            if not unseen:
                break

        # Cursor advanced and lease deleted.
        with kb.connect() as conn:
            lease_after = conn.execute(
                "SELECT task_id FROM owner_wake_leases WHERE task_id = ?",
                (completed_id,),
            ).fetchone()
        assert lease_after is None, (
            "BUILD-695 P2 recovery: lease must be released after cursor advance"
        )

        with kb.connect() as conn:
            _, unseen_final = kb.unseen_events_for_sub(
                conn,
                task_id=completed_id,
                platform=kb.OWNER_AGENT_NOTIFY_PLATFORM,
                chat_id=owner["session_id"],
                kinds=list(kb.OWNER_WAKE_KINDS),
            )
        assert len(unseen_final) == 0, (
            "BUILD-695 P2 recovery: cursor must be advanced after recovery "
            "runner bg task completes"
        )

        assert recovery_runner._handle_message_with_agent.await_count == 1, (
            "BUILD-695 P2 recovery: exactly one agent turn must fire when "
            "recovering from an expired lease"
        )

    asyncio.run(_scenario())
