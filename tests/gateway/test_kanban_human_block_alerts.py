"""Human-gated blocked events reach the operator's Kanban console topic.

Configured via ``kanban.human_block_alerts`` ({chat_id, thread_id}). The
sweep fires regardless of subscriptions, skips auto-resolving block kinds
(transient/dependency), dedups per event id via the notify_deliveries
ledger, and re-alerts on a re-block after an unblock (new event id).
"""

from gateway.kanban_watchers import _collect_human_blocked_events
from hermes_cli import kanban_db as kb


def _board(tmp_path):
    db = tmp_path / "kanban.db"
    kb.init_db(db)
    return kb.connect(db)


def _mktask(conn, title="t"):
    task = kb.create_task(conn, title=title)
    return task if isinstance(task, str) else task.id


def test_human_blocked_event_is_collected_even_when_subscribed(tmp_path):
    conn = _board(tmp_path)
    task_id = _mktask(conn)
    kb.add_notify_sub(conn, task_id=task_id, platform="telegram", chat_id="123")
    kb._append_event(conn, task_id, "blocked", {"reason": "needs approval", "kind": "needs_input"})

    out = _collect_human_blocked_events(kb, conn)
    assert [o["task_id"] for o in out] == [task_id]
    assert out[0]["delivery_key"] == f"human-block/{task_id}/{out[0]['event'].id}"


def test_auto_resolving_block_kinds_are_skipped(tmp_path):
    conn = _board(tmp_path)
    task_id = _mktask(conn)
    kb._append_event(conn, task_id, "blocked", {"reason": "flaky", "kind": "transient"})
    kb._append_event(conn, task_id, "blocked", {"reason": "parents", "kind": "dependency"})
    assert _collect_human_blocked_events(kb, conn) == []


def test_untyped_block_and_triage_escalation_are_collected(tmp_path):
    conn = _board(tmp_path)
    task_id = _mktask(conn)
    kb._append_event(conn, task_id, "blocked", {"reason": "generic"})
    kb._append_event(conn, task_id, "block_loop_detected", {"reason": "loop", "recurrences": 2})
    out = _collect_human_blocked_events(kb, conn)
    assert [o["event"].kind for o in out] == ["blocked", "block_loop_detected"]


def test_ledger_dedups_per_event_but_reblock_realerts(tmp_path):
    conn = _board(tmp_path)
    task_id = _mktask(conn)
    kb._append_event(conn, task_id, "blocked", {"reason": "one", "kind": "needs_input"})
    first = _collect_human_blocked_events(kb, conn)
    assert len(first) == 1
    kb.record_notify_delivery(
        conn,
        delivery_key=first[0]["delivery_key"],
        task_id=task_id, platform="telegram", chat_id="-100", thread_id="12483",
        first_event_id=first[0]["event"].id, last_event_id=first[0]["event"].id,
        status="delivered",
    )
    assert _collect_human_blocked_events(kb, conn) == []
    # Re-block after an unblock is a NEW human ask: new event id, new alert.
    kb._append_event(conn, task_id, "blocked", {"reason": "again", "kind": "needs_input"})
    again = _collect_human_blocked_events(kb, conn)
    assert len(again) == 1
    assert again[0]["event"].payload["reason"] == "again"


def test_done_and_archived_tasks_are_skipped(tmp_path):
    conn = _board(tmp_path)
    task_id = _mktask(conn)
    kb._append_event(conn, task_id, "blocked", {"reason": "x", "kind": "needs_input"})
    conn.execute("UPDATE tasks SET status='done' WHERE id=?", (task_id,))
    conn.commit()
    assert _collect_human_blocked_events(kb, conn) == []
