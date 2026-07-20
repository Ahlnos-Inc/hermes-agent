"""BUILD-567: writer-safe board recovery swap (``_attempt_board_db_recovery``).

The swap must abort (and retry next tick) if any writer commits between the
sqlite3 ``.recover`` snapshot and the atomic file swap, and must succeed when
the board is quiet. Silently discarding a concurrent notifier commit — or
letting a fresh ``connect()`` bind a stale ``-wal`` to the recovered inode
mid-swap — was a corruption seed.
"""

import sqlite3

import gateway.kanban_watchers as kw
from hermes_cli import kanban_db as kb


class _FakeKb:
    """Stand-in: ``_attempt_board_db_recovery`` only calls ``kanban_db_path``."""

    def __init__(self, path):
        self._path = path

    def kanban_db_path(self, slug):
        return self._path


def _healthy_board(tmp_path):
    db = tmp_path / "kanban.db"
    kb.init_db(db)
    with kb.connect(db) as conn:
        kb.create_task(conn, title="t")
    # Materialize the WAL into the main DB so the CLI ``.recover`` open has no
    # frames to checkpoint back (the swap-guard keys on data_version, which a
    # pure checkpoint does not bump, so WAL mode is fine to leave on).
    side = sqlite3.connect(str(db))
    try:
        side.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        side.close()
    return db


def _integrity_ok(db):
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def test_recovery_succeeds_when_board_is_quiet(tmp_path):
    db = _healthy_board(tmp_path)
    ok, detail = kw._attempt_board_db_recovery(_FakeKb(db), "slug")
    assert ok, detail
    assert "corrupt original preserved" in detail
    assert db.exists() and _integrity_ok(db)
    # The pre-swap original was preserved, not destroyed.
    assert len(list(tmp_path.glob("kanban.db.corrupt-*.bak"))) == 1


def test_recovery_aborts_when_board_changes_mid_recover(tmp_path, monkeypatch):
    db = _healthy_board(tmp_path)
    real_run = kw.subprocess.run
    calls = {"n": 0}

    def fake_run(*args, **kwargs):
        result = real_run(*args, **kwargs)
        # After the ``.recover`` read completes (first subprocess call),
        # simulate a concurrent notifier commit landing on the live board.
        if calls["n"] == 0:
            writer = sqlite3.connect(str(db))
            try:
                writer.execute("CREATE TABLE IF NOT EXISTS _concurrent(x)")
                writer.execute("INSERT INTO _concurrent VALUES (1)")
                writer.commit()
            finally:
                writer.close()
        calls["n"] += 1
        return result

    monkeypatch.setattr(kw.subprocess, "run", fake_run)

    ok, detail = kw._attempt_board_db_recovery(_FakeKb(db), "slug")
    assert not ok
    assert "changed during recovery" in detail
    # No swap happened: the live DB was never renamed out.
    assert not list(tmp_path.glob("kanban.db.corrupt-*.bak"))
    # The concurrent writer's commit survived (it was not clobbered by a swap).
    conn = sqlite3.connect(str(db))
    try:
        assert conn.execute("SELECT count(*) FROM _concurrent").fetchone()[0] == 1
    finally:
        conn.close()
