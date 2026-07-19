"""Regression tests for BUILD-575: the fast-path header guard must not
release the process's SQLite locks, and malformed notify rows must degrade
to a warning instead of a NameError.

Mechanism under test (SQLite "How To Corrupt An SQLite Database File"
section 2.2): POSIX advisory locks are per-process — closing ANY file
descriptor that references a locked file drops every fcntl lock the whole
process holds on it. SQLite's unix VFS defends against this only for fds
it manages itself; a plain ``open()``/``close()`` of kanban.db inside a
process that holds live SQLite locks silently unlocks the database for
every other process. With per-op ``connect()`` running the header guard on
every kanban operation, a gateway mid-checkpoint lost its WAL locks every
notifier tick, letting worker processes checkpoint concurrently — the
2026-07-19 marketing/hermes-infra page-image corruption (an older
generation of the sqlite_sequence page written over the
kanban_notify_subs root page).
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


def _other_process_can_write(db: Path) -> bool:
    """True if a separate process can grab a write lock on ``db`` now."""
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sqlite3, sys\n"
                f"conn = sqlite3.connect({str(db)!r}, timeout=0.25)\n"
                "try:\n"
                "    conn.execute('BEGIN IMMEDIATE')\n"
                "    conn.execute('INSERT INTO probe VALUES (1)')\n"
                "    conn.commit()\n"
                "except sqlite3.OperationalError:\n"
                "    sys.exit(3)\n"
                "sys.exit(0)\n"
            ),
        ],
        capture_output=True,
        timeout=30,
    )
    return probe.returncode == 0


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX fcntl semantics")
def test_header_guard_does_not_drop_this_process_sqlite_locks(tmp_path):
    db = tmp_path / "kanban.db"
    holder = sqlite3.connect(db)
    holder.execute("CREATE TABLE probe (x)")
    holder.commit()
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO probe VALUES (0)")

    # Baseline: while the write transaction is open, no other process may
    # write. If this fails the harness itself is broken, not the guard.
    assert not _other_process_can_write(db), "baseline write lock not held"

    # The fast-path guard runs on every kanban connect(). It must not
    # open/close the DB file with a lock-dropping descriptor.
    kb._guard_cached_db_header(db)

    assert not _other_process_can_write(db), (
        "header guard released this process's SQLite locks — plain "
        "open()/close() of the DB file drops POSIX fcntl locks held by "
        "SQLite (corruption class of BUILD-575)"
    )
    holder.rollback()
    holder.close()


def test_header_guard_still_detects_clobbered_header(tmp_path):
    db = tmp_path / "kanban.db"
    db.write_bytes(b"\x17\x03\x03\x00\x20" + b"garbage" * 16)
    with pytest.raises(kb.KanbanDbCorruptError):
        kb._guard_cached_db_header(db)


def test_header_guard_reopens_after_file_replacement(tmp_path):
    """The cached header descriptor must follow os.replace() swaps.

    Board recovery swaps a rebuilt file into the same path; a cached fd
    pinned to the old inode would validate a stale header forever.
    """
    db = tmp_path / "kanban.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t (x)")
    conn.commit()
    conn.close()
    kb._guard_cached_db_header(db)  # caches a descriptor for the old inode

    bad = tmp_path / "replacement"
    bad.write_bytes(b"\x17\x03\x03\x00\x20" + b"garbage" * 16)
    bad.replace(db)

    with pytest.raises(kb.KanbanDbCorruptError):
        kb._guard_cached_db_header(db)


def test_list_notify_subs_skips_malformed_rows_without_crashing(tmp_path):
    """2026-07-19: five sqlite_sequence-shaped rows (integer platform, NULL
    chat_id) made list_notify_subs raise NameError on the very guard meant
    to skip them, killing every notifier tick."""
    db = tmp_path / "kanban.db"
    conn = kb.connect(db_path=db)
    try:
        conn.execute(
            "INSERT INTO kanban_notify_subs (task_id, platform, chat_id, thread_id, created_at)"
            " VALUES ('t_ok', 'telegram', '123', '', 1)"
        )
        # The live corrupt rows held an integer platform, written by a
        # cross-linked page rather than SQL — column affinity and NOT NULL
        # never applied. SQL can't store an integer in this TEXT-affinity
        # column (23 becomes '23'), but BLOBs survive affinity untouched,
        # giving the same non-str platform shape the guard must skip.
        conn.execute(
            "INSERT INTO kanban_notify_subs (task_id, platform, chat_id, thread_id, created_at)"
            " VALUES ('task_runs', X'170303', '999', '', 1)"
        )
        conn.commit()
        subs = kb.list_notify_subs(conn)
    finally:
        conn.close()
    assert [s["task_id"] for s in subs] == ["t_ok"]


def test_concurrent_checkpoint_stress_keeps_integrity(tmp_path):
    """Two writer processes checkpoint aggressively while this process runs
    the fast-path header guard in a loop — the exact 2026-07-19 traffic
    shape (worker completion + notifier tick). Integrity must hold."""
    db = tmp_path / "kanban.db"
    conn = kb.connect(db_path=db)
    conn.close()

    writer = (
        "import sqlite3, sys\n"
        f"conn = sqlite3.connect({str(db)!r}, timeout=30)\n"
        "conn.execute('PRAGMA journal_mode=WAL')\n"
        "conn.execute('PRAGMA wal_autocheckpoint=10')\n"
        "for i in range(150):\n"
        "    conn.execute(\n"
        "        'INSERT INTO task_events (task_id, kind, payload, created_at)'\n"
        "        ' VALUES (?, ?, ?, ?)', (f't_stress{i}', 'status', '{}', i))\n"
        "    conn.commit()\n"
        "    if i % 7 == 0:\n"
        "        try:\n"
        "            conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')\n"
        "        except sqlite3.OperationalError:\n"
        "            pass\n"
        "conn.close()\n"
    )
    procs = [
        subprocess.Popen([sys.executable, "-c", writer]) for _ in range(2)
    ]
    try:
        while any(p.poll() is None for p in procs):
            kb._guard_cached_db_header(db)
            reader = kb.connect(db_path=db)
            reader.execute("SELECT COUNT(*) FROM task_events").fetchone()
            reader.close()
    finally:
        for p in procs:
            p.wait(timeout=60)
    check = sqlite3.connect(db).execute("PRAGMA integrity_check").fetchone()[0]
    assert check == "ok"
    assert all(p.returncode == 0 for p in procs)
