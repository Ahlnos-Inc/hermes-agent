"""Kanban terminal events delivered to the owning TUI session."""
from __future__ import annotations

import sys
import contextlib
import threading
import time
import types
from pathlib import Path

import pytest

from gateway import kanban_watchers as kw
from hermes_cli import kanban_db as kb
from tui_gateway import server


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _seed_sub(chat_id: str):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="Desktop completion", assignee="worker")
        kb.add_notify_sub(conn, task_id=task_id, platform="tui", chat_id=chat_id)
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, kind="completed")
    return task_id


def _cursor(task_id: str, chat_id: str) -> int:
    with kb.connect() as conn:
        return int(kb.list_notify_subs(conn, task_id=task_id)[0]["last_event_id"])


def _session(**extra):
    return {
        "session_key": "sess-key-1",
        "history_lock": threading.Lock(),
        "running": False,
        **extra,
    }


def _poll(session, sid="sid1"):
    server._sessions[sid] = session
    try:
        server._poll_kanban_tui_subs(sid, session)
    finally:
        server._sessions.pop(sid, None)


def _capture_persisted_submit(submitted):
    def submit(*args, **kwargs):
        submitted.append((args, kwargs))
        callback = kwargs.get("persistence_ack_callback")
        if callback:
            callback()

    return submit


def test_tui_poller_claims_once_emits_and_chains_turn(kanban_home, monkeypatch):
    task_id = _seed_sub("sess-key-1")
    emitted, submitted = [], []
    monkeypatch.setattr(server, "_emit", lambda *args: emitted.append(args))
    monkeypatch.setattr(server, "_run_prompt_submit", _capture_persisted_submit(submitted))

    _poll(_session())

    status_updates = [event for event in emitted if event[0] == "status.update"]
    assert len(status_updates) == 1
    assert status_updates[0][0:2] == ("status.update", "sid1")
    assert "Desktop completion" in status_updates[0][2]["text"]
    assert "done" in status_updates[0][2]["text"]
    assert len(submitted) == 1
    assert _cursor(task_id, "sess-key-1") > 0

    _poll(_session())
    assert len([event for event in emitted if event[0] == "status.update"]) == 1


def test_tui_poller_emits_but_does_not_chain_busy_session(kanban_home, monkeypatch):
    task_id = _seed_sub("sess-key-1")
    emitted, submitted = [], []
    session = _session(running=True)
    monkeypatch.setattr(server, "_emit", lambda *args: emitted.append(args))
    monkeypatch.setattr(server, "_run_prompt_submit", _capture_persisted_submit(submitted))

    _poll(session)

    assert len([event for event in emitted if event[0] == "status.update"]) == 0
    assert submitted == []
    assert _cursor(task_id, "sess-key-1") == 0


def test_tui_poller_drains_busy_session_turn_when_idle(kanban_home, monkeypatch):
    _seed_sub("sess-key-1")
    submitted = []
    session = _session(running=True)
    monkeypatch.setattr(server, "_emit", lambda *_args: None)
    monkeypatch.setattr(server, "_run_prompt_submit", _capture_persisted_submit(submitted))

    _poll(session)
    session["running"] = False
    _poll(session)

    assert len(submitted) == 1
    assert "Kanban" in submitted[0][0][-1]


def test_tui_poller_isolates_per_event_delivery_failures(kanban_home, monkeypatch):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="Desktop completion", assignee="worker")
        kb.add_notify_sub(conn, task_id=task_id, platform="tui", chat_id="sess-key-1")
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, kind="blocked")
            kb._append_event(conn, task_id, kind="completed")
    emitted = []

    def _emit(*args):
        if args[0] == "status.update" and not emitted:
            emitted.append(("failed", args))
            raise RuntimeError("first event failed")
        emitted.append(("ok", args))

    monkeypatch.setattr(server, "_emit", _emit)
    submitted = []
    monkeypatch.setattr(server, "_run_prompt_submit", _capture_persisted_submit(submitted))

    _poll(_session())

    assert submitted
    assert _cursor(task_id, "sess-key-1") > 0


def test_tui_poller_accepts_stale_session_key(kanban_home, monkeypatch):
    _seed_sub("old-key")
    emitted = []
    monkeypatch.setattr(server, "_emit", lambda *args: emitted.append(args))
    submitted = []
    monkeypatch.setattr(server, "_run_prompt_submit", _capture_persisted_submit(submitted))

    class FakeDB:
        get_compression_tip = resolve_resume_session_id = lambda self, origin: "new-key"

    @contextlib.contextmanager
    def fake_session_db(_session):
        yield FakeDB()

    monkeypatch.setattr(server, "_session_db", fake_session_db)

    _poll(_session(session_key="new-key", _stale_session_keys=["old-key"]))

    assert len([event for event in emitted if event[0] == "status.update"]) == 1


def test_compression_preserves_old_session_key_for_kanban_poller(monkeypatch):
    session = {
        "agent": types.SimpleNamespace(session_id="new-key"),
        "session_key": "old-key",
    }
    approval = types.SimpleNamespace(
        disable_session_yolo=lambda *_args: None,
        enable_session_yolo=lambda *_args: None,
        is_session_yolo_enabled=lambda *_args: False,
        register_gateway_notify=lambda *_args: None,
        unregister_gateway_notify=lambda *_args: None,
    )
    monkeypatch.setattr(server, "_transfer_active_session_slot", lambda *_args, **_kwargs: True)
    with pytest.MonkeyPatch.context() as patch:
        patch.setitem(sys.modules, "tools.approval", approval)
        server._sync_session_key_after_compress(
            "sid1", session, clear_pending_title=False, restart_slash_worker=False
        )

    assert session["session_key"] == "new-key"
    assert session["_stale_session_keys"] == ["old-key"]


def test_compression_caps_stale_session_keys(monkeypatch):
    session = {
        "agent": types.SimpleNamespace(session_id="new-key-0"),
        "session_key": "old-key",
    }
    approval = types.SimpleNamespace(
        disable_session_yolo=lambda *_args: None,
        enable_session_yolo=lambda *_args: None,
        is_session_yolo_enabled=lambda *_args: False,
        register_gateway_notify=lambda *_args: None,
        unregister_gateway_notify=lambda *_args: None,
    )
    monkeypatch.setattr(server, "_transfer_active_session_slot", lambda *_args, **_kwargs: True)
    old_keys = []
    with pytest.MonkeyPatch.context() as patch:
        patch.setitem(sys.modules, "tools.approval", approval)
        for i in range(10):
            old_keys.append(session["session_key"])
            server._sync_session_key_after_compress(
                "sid1", session, clear_pending_title=False, restart_slash_worker=False
            )
            session["agent"].session_id = f"new-key-{i + 1}"

    assert len(session["_stale_session_keys"]) == 8
    assert session["_stale_session_keys"][-1] == old_keys[-1]


def test_tui_poller_consumes_event_when_status_delivery_fails(kanban_home, monkeypatch):
    task_id = _seed_sub("sess-key-1")

    def _boom(*_args):
        raise RuntimeError("emit failed")

    monkeypatch.setattr(server, "_emit", _boom)
    submitted = []
    monkeypatch.setattr(server, "_run_prompt_submit", _capture_persisted_submit(submitted))

    _poll(_session())

    assert _cursor(task_id, "sess-key-1") > 0
    assert submitted


def test_tui_poller_leaves_foreign_subscription_unclaimed(kanban_home, monkeypatch):
    task_id = _seed_sub("other-sess")
    emitted = []
    monkeypatch.setattr(server, "_emit", lambda *args: emitted.append(args))

    _poll(_session())

    assert emitted == []
    assert _cursor(task_id, "other-sess") == 0


def test_tui_poller_delivers_block_loop_detected(kanban_home, monkeypatch):
    """BUILD-443: block_loop_detected must reach TUI subscribers.

    Before this fix the kind was absent from TERMINAL_KINDS, so a task that
    hit the block-recurrence limit and dropped to triage never notified the
    operator — exactly the stall a human needs to hear about.
    """
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="loopy task", assignee="worker")
        kb.add_notify_sub(conn, task_id=task_id, platform="tui", chat_id="sess-key-1")
        with kb.write_txn(conn):
            kb._append_event(
                conn, task_id, kind="block_loop_detected",
                payload={"reason": "needs decision", "kind": "needs_input", "recurrences": 2, "limit": 2},
            )
    submitted = []
    monkeypatch.setattr(server, "_emit", lambda *args: None)
    monkeypatch.setattr(server, "_run_prompt_submit", _capture_persisted_submit(submitted))

    _poll(_session())

    assert len(submitted) == 1
    assert "triage" in submitted[0][0][-1].lower()
    assert _cursor(task_id, "sess-key-1") > 0


# --- BUILD-506: gateway orphan-sweep vs. the live tui poller ---------------
#
# THE RACE THAT MATTERS: an active desktop poller must never have a
# delivery stolen out from under it, and the gateway's orphan sweep must
# never double-deliver alongside a live poller. Both sides claim through
# the SAME `claim_unseen_events_for_sub` BEGIN IMMEDIATE + CAS
# (`expected_old_cursor`), so whichever runs first wins and the other's
# claim is a guaranteed no-op — these tests drive the real
# `_poll_kanban_tui_subs` production code path against
# `gateway.kanban_watchers.sweep_orphaned_tui_sub` on the same DB row to
# prove that in both orderings.


def _seed_backdated_failure_sub(chat_id: str, kind: str = "blocked") -> str:
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="Desktop worker task", assignee="worker")
        kb.add_notify_sub(conn, task_id=task_id, platform="tui", chat_id=chat_id)
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, kind=kind, payload={"reason": "x"})
            conn.execute(
                "UPDATE task_events SET created_at = ? WHERE task_id = ?",
                (int(time.time()) - kw.DEFAULT_TUI_ORPHAN_AGE_SECONDS - 60, task_id),
            )
    return task_id


def _sub_row(task_id: str, chat_id: str) -> dict:
    with kb.connect() as conn:
        return next(
            s for s in kb.list_notify_subs(conn, task_id=task_id)
            if s["chat_id"] == chat_id
        )


def test_gateway_sweep_wins_race_tui_poller_finds_nothing_left(kanban_home, monkeypatch):
    """Gateway sweep claims first; the (later, real) tui poller must see no
    unclaimed events left — no double delivery."""
    task_id = _seed_backdated_failure_sub("dead-session", kind="crashed")

    conn = kb.connect()
    try:
        sub = next(
            s for s in kb.list_notify_subs(conn, task_id=task_id)
            if s["chat_id"] == "dead-session"
        )
        result = kw.sweep_orphaned_tui_sub(
            kb, conn, sub, live_session_ids=set(),
            age_gate_seconds=kw.DEFAULT_TUI_ORPHAN_AGE_SECONDS,
        )
    finally:
        conn.close()
    assert result is not None and result["events"], "setup: sweep should have claimed it"

    submitted = []
    monkeypatch.setattr(server, "_emit", lambda *args: None)
    monkeypatch.setattr(server, "_run_prompt_submit", _capture_persisted_submit(submitted))

    _poll(_session(session_key="dead-session"))

    assert submitted == [], "tui poller must not re-deliver what the sweep already claimed"
    assert _cursor(task_id, "dead-session") == result["events"][-1].id


def test_tui_poller_wins_race_gateway_sweep_loses_cas(kanban_home, monkeypatch):
    """Live tui poller claims first via the real production path; a sweep
    holding a STALE pre-race sub snapshot (the actual race window — the
    sweep read subs at the top of its tick before the poller's claim
    landed) must lose the CAS and back off rather than double-deliver."""
    task_id = _seed_backdated_failure_sub("sess-key-1", kind="gave_up")
    stale_sub = _sub_row(task_id, "sess-key-1")  # last_event_id == 0

    submitted = []
    monkeypatch.setattr(server, "_emit", lambda *args: None)
    monkeypatch.setattr(server, "_run_prompt_submit", _capture_persisted_submit(submitted))

    _poll(_session())

    assert len(submitted) == 1, "setup: tui poller should have claimed it"
    assert _cursor(task_id, "sess-key-1") > 0

    conn = kb.connect()
    try:
        result = kw.sweep_orphaned_tui_sub(
            kb, conn, stale_sub, live_session_ids=set(),
            age_gate_seconds=kw.DEFAULT_TUI_ORPHAN_AGE_SECONDS,
        )
    finally:
        conn.close()

    assert result is None, "sweep must lose the CAS against the poller's prior claim"
    # Exactly one delivery happened across both consumers, not two.
    assert len(submitted) == 1
