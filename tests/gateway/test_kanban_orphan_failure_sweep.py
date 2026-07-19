"""Catch-all failure sweep: failure events on unsubscribed tasks reach home.

Covers the 2026-07-18 gsthst-q2 incident class: a task with NO notify
subscription emits `blocked` and nobody is told. The sweep must pick it up,
dedup per (task, kind) via the notify_deliveries ledger, and skip tasks that
have any subscription.
"""

import time

from gateway.kanban_watchers import _collect_unsubscribed_failure_events
from hermes_cli import kanban_db as kb


def _board(tmp_path):
    db = tmp_path / "kanban.db"
    kb.init_db(db)
    return kb.connect(db)


def _mktask(conn, title="t"):
    task = kb.create_task(conn, title=title)
    return task if isinstance(task, str) else task.id


def test_unsubscribed_blocked_event_is_collected(tmp_path):
    conn = _board(tmp_path)
    task_id = _mktask(conn)
    kb._append_event(conn, task_id, "blocked", {"reason": "x"})

    out = _collect_unsubscribed_failure_events(kb, conn)
    assert [o["task_id"] for o in out] == [task_id]
    assert out[0]["event"].kind == "blocked"
    assert out[0]["delivery_key"] == f"home-sweep/{task_id}/blocked"


def test_subscribed_task_is_skipped(tmp_path):
    conn = _board(tmp_path)
    task_id = _mktask(conn)
    kb._append_event(conn, task_id, "blocked", {"reason": "x"})
    kb.add_notify_sub(
        conn, task_id=task_id, platform="telegram", chat_id="123",
    )
    assert _collect_unsubscribed_failure_events(kb, conn) == []


def test_recorded_delivery_dedups_same_kind(tmp_path):
    conn = _board(tmp_path)
    task_id = _mktask(conn)
    kb._append_event(conn, task_id, "blocked", {"reason": "x"})
    out = _collect_unsubscribed_failure_events(kb, conn)
    assert len(out) == 1
    kb.record_notify_delivery(
        conn,
        delivery_key=out[0]["delivery_key"],
        task_id=task_id, platform="telegram", chat_id="home",
        first_event_id=out[0]["event"].id, last_event_id=out[0]["event"].id,
        status="delivered",
    )
    # Same kind again (crash-retry loop): still deduped.
    kb._append_event(conn, task_id, "blocked", {"reason": "again"})
    assert _collect_unsubscribed_failure_events(kb, conn) == []
    # A different failure kind is a new signal.
    kb._append_event(conn, task_id, "crashed", {"reason": "boom"})
    out2 = _collect_unsubscribed_failure_events(kb, conn)
    assert [o["event"].kind for o in out2] == ["crashed"]


def test_completed_events_are_not_swept(tmp_path):
    conn = _board(tmp_path)
    task_id = _mktask(conn)
    kb._append_event(conn, task_id, "completed", {})
    assert _collect_unsubscribed_failure_events(kb, conn) == []
