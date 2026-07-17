"""BUILD-506: orphaned tui-subscription sweep.

BUILD-503 added origin-session delivery + the notify_deliveries ledger +
a Telegram home fallback for kanban failure events, but a tui subscription
whose desktop process is gone had no live poller — its events sat unclaimed
forever. These tests cover the standalone sweep primitives in
gateway/kanban_watchers.py: the age gate, the cross-process liveness
snapshot, and sweep_orphaned_tui_sub's claim-or-skip decision (unit level).
End-to-end delivery through a full notifier tick lives in
tests/gateway/test_kanban_notifier.py; the poller/sweep race lives in
tests/tui_gateway/test_kanban_notify_poller.py.
"""
from __future__ import annotations

import time

from gateway import kanban_watchers as kw
from hermes_cli import kanban_db as kb


def _seed_tui_sub(chat_id: str = "sess-key-1", kind: str = "blocked"):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="orphan check", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="tui", chat_id=chat_id)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, kind=kind, payload={"reason": "x"})
        return tid
    finally:
        conn.close()


def _backdate_events(task_id: str, seconds_ago: int) -> None:
    conn = kb.connect()
    try:
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_events SET created_at = ? WHERE task_id = ?",
                (int(time.time()) - seconds_ago, task_id),
            )
    finally:
        conn.close()


def _sub_for(task_id: str, chat_id: str = "sess-key-1") -> dict:
    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, task_id=task_id)
        return next(s for s in subs if s["chat_id"] == chat_id)
    finally:
        conn.close()


# --- tui_orphan_age_seconds -------------------------------------------------


def test_tui_orphan_age_seconds_default(monkeypatch):
    monkeypatch.delenv(kw.TUI_ORPHAN_AGE_ENV, raising=False)
    assert kw.tui_orphan_age_seconds() == kw.DEFAULT_TUI_ORPHAN_AGE_SECONDS
    assert kw.DEFAULT_TUI_ORPHAN_AGE_SECONDS == 15 * 60


def test_tui_orphan_age_seconds_env_override(monkeypatch):
    monkeypatch.setenv(kw.TUI_ORPHAN_AGE_ENV, "120")
    assert kw.tui_orphan_age_seconds() == 120


def test_tui_orphan_age_seconds_invalid_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv(kw.TUI_ORPHAN_AGE_ENV, "not-a-number")
    assert kw.tui_orphan_age_seconds() == kw.DEFAULT_TUI_ORPHAN_AGE_SECONDS
    monkeypatch.setenv(kw.TUI_ORPHAN_AGE_ENV, "-5")
    assert kw.tui_orphan_age_seconds() == kw.DEFAULT_TUI_ORPHAN_AGE_SECONDS


# --- _live_tui_session_ids ---------------------------------------------------


def test_live_tui_session_ids_reads_registry(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.active_sessions.active_session_registry_snapshot",
        lambda: [
            {"session_id": "sess-key-1", "pid": 123},
            {"session_id": "sess-key-2", "pid": 456},
            {"pid": 789},  # no session_id — must not crash or leak a "None" entry
        ],
    )
    assert kw._live_tui_session_ids() == {"sess-key-1", "sess-key-2"}


def test_live_tui_session_ids_empty_on_registry_error(monkeypatch):
    def _boom():
        raise RuntimeError("registry file locked")

    monkeypatch.setattr(
        "hermes_cli.active_sessions.active_session_registry_snapshot", _boom,
    )
    assert kw._live_tui_session_ids() == set()


# --- sweep_orphaned_tui_sub --------------------------------------------------


def test_sweep_skips_non_tui_sub(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "k.db"))
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="w")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        with kb.write_txn(conn):
            kb._append_event(conn, tid, kind="blocked")
        sub = kb.list_notify_subs(conn, task_id=tid)[0]
        assert kw.sweep_orphaned_tui_sub(
            kb, conn, sub, live_session_ids=set(), age_gate_seconds=0,
        ) is None
    finally:
        conn.close()


def test_sweep_skips_when_live_session_matches_regardless_of_age(tmp_path, monkeypatch):
    """THE RACE THAT MATTERS: a live session must never lose a delivery,
    even when its unclaimed failure event is ancient (e.g. a desktop that
    left a task blocked for hours before ever going idle again)."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "k.db"))
    kb.init_db()
    tid = _seed_tui_sub()
    _backdate_events(tid, seconds_ago=10_000)
    sub = _sub_for(tid)

    conn = kb.connect()
    try:
        result = kw.sweep_orphaned_tui_sub(
            kb, conn, sub,
            live_session_ids={"sess-key-1"},
            age_gate_seconds=1,
        )
    finally:
        conn.close()
    assert result is None
    # Cursor must be untouched — nothing was claimed.
    assert int(_sub_for(tid)["last_event_id"]) == 0


def test_sweep_skips_fresh_orphan_under_age_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "k.db"))
    kb.init_db()
    tid = _seed_tui_sub()  # created_at == now, no backdating
    sub = _sub_for(tid)

    conn = kb.connect()
    try:
        result = kw.sweep_orphaned_tui_sub(
            kb, conn, sub,
            live_session_ids=set(),
            age_gate_seconds=kw.DEFAULT_TUI_ORPHAN_AGE_SECONDS,
        )
    finally:
        conn.close()
    assert result is None
    assert int(_sub_for(tid)["last_event_id"]) == 0


def test_sweep_claims_old_orphan_past_age_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "k.db"))
    kb.init_db()
    tid = _seed_tui_sub(kind="crashed")
    _backdate_events(tid, seconds_ago=kw.DEFAULT_TUI_ORPHAN_AGE_SECONDS + 60)
    sub = _sub_for(tid)

    conn = kb.connect()
    try:
        result = kw.sweep_orphaned_tui_sub(
            kb, conn, sub,
            live_session_ids=set(),
            age_gate_seconds=kw.DEFAULT_TUI_ORPHAN_AGE_SECONDS,
        )
    finally:
        conn.close()
    assert result is not None
    assert [ev.kind for ev in result["events"]] == ["crashed"]
    assert result["task"].id == tid
    # Cursor advanced — a second sweep attempt on the same sub finds nothing.
    assert int(_sub_for(tid)["last_event_id"]) == result["events"][-1].id


def test_sweep_ignores_non_failure_kinds(tmp_path, monkeypatch):
    """`completed` / `status` / `archived` / `unblocked` are not failures —
    a stale orphaned sub must not fire a home-channel ping for them."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "k.db"))
    kb.init_db()
    tid = _seed_tui_sub(kind="completed")
    _backdate_events(tid, seconds_ago=kw.DEFAULT_TUI_ORPHAN_AGE_SECONDS + 60)
    sub = _sub_for(tid)

    conn = kb.connect()
    try:
        result = kw.sweep_orphaned_tui_sub(
            kb, conn, sub,
            live_session_ids=set(),
            age_gate_seconds=kw.DEFAULT_TUI_ORPHAN_AGE_SECONDS,
        )
    finally:
        conn.close()
    assert result is None


def test_sweep_loses_cas_race_to_concurrent_claimer(tmp_path, monkeypatch):
    """A concurrent claimer (the live tui poller, or another gateway's sweep
    tick) that claims the exact same range between our peek and our claim
    must win — sweep_orphaned_tui_sub must back off, never double-deliver.
    """
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "k.db"))
    kb.init_db()
    tid = _seed_tui_sub(kind="gave_up")
    _backdate_events(tid, seconds_ago=kw.DEFAULT_TUI_ORPHAN_AGE_SECONDS + 60)
    sub = _sub_for(tid)  # snapshot taken BEFORE the concurrent claim below

    conn = kb.connect()
    try:
        # Simulate a concurrent claimer winning first (e.g. a live tui
        # poller's own claim_unseen_events_for_sub, which uses the broader
        # TERMINAL_KINDS filter but claims the same underlying event).
        old, new, events = kb.claim_unseen_events_for_sub(
            conn, task_id=sub["task_id"], platform="tui", chat_id=sub["chat_id"],
            thread_id=sub.get("thread_id") or "", kinds=kb.TERMINAL_KINDS,
        )
        assert events, "setup: concurrent claimer should have found the event"

        # The sweep uses its STALE sub snapshot (last_event_id=0) taken
        # before the race — this is the actual race window.
        result = kw.sweep_orphaned_tui_sub(
            kb, conn, sub,
            live_session_ids=set(),
            age_gate_seconds=1,
        )
    finally:
        conn.close()
    assert result is None, "sweep must lose the CAS, not double-claim"
