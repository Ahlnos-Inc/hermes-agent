"""BUILD-567: concurrent spawn/claim against a corrupt board fails closed.

Two workers racing to claim a task on a corrupt board must both fail with an
actionable ``KanbanDbCorruptError`` (never silently recreate the schema or write
a partial run), leave a forensic backup, and — once the board is repaired — the
task must be claimable exactly once with exactly one ``task_runs`` row.
"""

import sqlite3
import threading

from hermes_cli import kanban_db as kb


def _corrupt_page_one(db):
    # Overwrite the SQLite header so every connect() — fast-path header guard or
    # first-open integrity probe — fails closed via KanbanDbCorruptError.
    with open(db, "r+b") as handle:
        handle.write(b"\x00" * 16)


def _race(fn, n=2, timeout=30):
    barrier = threading.Barrier(n)
    out, lock = [], threading.Lock()

    def _run():
        barrier.wait()
        res = fn()
        with lock:
            out.append(res)

    threads = [threading.Thread(target=_run) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=timeout)
    return out


def test_concurrent_spawn_on_corrupt_board_fails_closed_then_repairs(tmp_path):
    db = tmp_path / "kanban.db"
    kb.init_db(db)
    with kb.connect(db) as conn:
        task_id = kb.create_task(conn, title="claim-me")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    healthy_bytes = db.read_bytes()

    _corrupt_page_one(db)

    # Two simultaneous spawn attempts hit the corrupt board.
    def _spawn():
        try:
            conn = kb.connect(db)
            try:
                kb.claim_task(conn, task_id, claimer="host:racer")
            finally:
                conn.close()
            return None
        except Exception as exc:  # noqa: BLE001 - type asserted below
            return exc

    errors = _race(_spawn)

    # Both fail, each with an actionable corruption error naming the board.
    assert len(errors) == 2
    for exc in errors:
        assert isinstance(exc, kb.KanbanDbCorruptError)
        assert "corrupt" in str(exc).lower()
        assert str(db) in str(exc)

    # A forensic backup was preserved (the original was never recreated).
    assert list(tmp_path.glob("kanban.db.corrupt.*.bak"))

    # Repair the board (stand-in for the recovery swap), then the task claims
    # exactly once across two more racers.
    db.write_bytes(healthy_bytes)
    assert kb.probe_corruption_incident(db) is True

    def _claim():
        conn = kb.connect(db)
        try:
            return kb.claim_task(conn, task_id, claimer="host:repaired")
        finally:
            conn.close()

    results = _race(_claim)

    assert sum(1 for r in results if r is not None) == 1
    with kb.connect(db) as conn:
        runs = conn.execute(
            "SELECT count(*) FROM task_runs WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
        assert runs == 1
        assert kb.get_task(conn, task_id).status == "running"
