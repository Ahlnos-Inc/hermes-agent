"""Writer-safe board recovery-swap behavior.

The swap must abort (and retry next tick) if any writer commits between the
sqlite3 ``.recover`` snapshot and the atomic file swap, and must succeed when
the board is quiet. Silently discarding a concurrent notifier commit — or
letting a fresh ``connect()`` bind a stale ``-wal`` to the recovered inode
mid-swap — was a corruption seed.
"""

import os
from pathlib import Path
import shutil
import sqlite3
import subprocess

import pytest

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
    conn = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
    try:
        return conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def _task_count(db):
    conn = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
    try:
        return conn.execute("SELECT count(*) FROM tasks").fetchone()[0]
    finally:
        conn.close()


def _install_sqlite_process_fake(
    monkeypatch,
    tmp_path,
    live_db,
    *,
    after_recover=None,
    recover_failure=None,
):
    """Emulate only the external sqlite3 process used by production recovery."""
    executable = tmp_path / "fake-sqlite3"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    calls = []

    def fake_run(args, **kwargs):
        command = tuple(str(arg) for arg in args)
        input_data = kwargs.pop("input", None)
        assert kwargs.pop("capture_output", None) is True
        timeout = kwargs.pop("timeout", None)
        assert kwargs.pop("check", False) is False
        assert not kwargs
        assert command[0] == str(executable)
        calls.append(command)

        if len(command) == 3 and Path(command[1]) == live_db and command[2] == ".recover":
            assert input_data is None
            assert timeout == 300
            if recover_failure is None:
                with sqlite3.connect(str(live_db)) as conn:
                    stdout = "\n".join(conn.iterdump()).encode()
                result = subprocess.CompletedProcess(command, 0, stdout, b"")
            else:
                returncode, stdout, stderr = recover_failure
                result = subprocess.CompletedProcess(command, returncode, stdout, stderr)
            if after_recover is not None:
                after_recover()
            return result

        if len(command) == 2:
            assert timeout == 300
            assert isinstance(input_data, bytes) and input_data.strip()
            recovered_db = Path(command[1])
            assert recovered_db.name.startswith(live_db.name + ".recovered-")
            with sqlite3.connect(str(recovered_db)) as conn:
                conn.executescript(input_data.decode())
            return subprocess.CompletedProcess(command, 0, b"", b"")

        if len(command) == 3 and command[2] == "PRAGMA integrity_check":
            assert input_data is None
            assert timeout == 120
            recovered_db = Path(command[1])
            with sqlite3.connect(str(recovered_db)) as conn:
                result = conn.execute("PRAGMA integrity_check").fetchone()[0]
            return subprocess.CompletedProcess(command, 0, f"{result}\n".encode(), b"")

        raise AssertionError(f"unexpected sqlite3 operation: {command!r}")

    monkeypatch.setattr(kw.shutil, "which", lambda name: str(executable) if name == "sqlite3" else None)
    monkeypatch.setattr(kw.subprocess, "run", fake_run)
    return calls


def test_recovery_succeeds_when_board_is_quiet(tmp_path, monkeypatch):
    db = _healthy_board(tmp_path)
    original = db.read_bytes()
    calls = _install_sqlite_process_fake(monkeypatch, tmp_path, db)

    ok, detail = kw._attempt_board_db_recovery(_FakeKb(db), "slug")

    assert ok, detail
    assert "corrupt original preserved" in detail
    assert len(calls) == 3
    assert db.exists() and _integrity_ok(db)
    assert _task_count(db) == 1
    backups = list(tmp_path.glob("kanban.db.corrupt-*.bak"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original
    assert not list(tmp_path.glob("kanban.db.recovered-*.tmp"))
    assert not Path(f"{db}-wal").exists()
    assert not Path(f"{db}-shm").exists()


def test_recovery_aborts_when_board_changes_mid_recover(tmp_path, monkeypatch):
    db = _healthy_board(tmp_path)

    def commit_after_snapshot():
        with sqlite3.connect(str(db)) as writer:
            writer.execute("CREATE TABLE IF NOT EXISTS _concurrent(x)")
            writer.execute("INSERT INTO _concurrent VALUES (1)")

    calls = _install_sqlite_process_fake(
        monkeypatch,
        tmp_path,
        db,
        after_recover=commit_after_snapshot,
    )

    ok, detail = kw._attempt_board_db_recovery(_FakeKb(db), "slug")

    assert not ok
    assert "changed during recovery" in detail
    assert "could not acquire write lock" not in detail
    assert len(calls) == 3
    assert not list(tmp_path.glob("kanban.db.corrupt-*.bak"))
    assert not list(tmp_path.glob("kanban.db.recovered-*.tmp"))
    with sqlite3.connect(str(db)) as conn:
        assert conn.execute("SELECT count(*) FROM _concurrent").fetchone()[0] == 1


def test_recovery_aborts_when_board_file_is_replaced_mid_recover(tmp_path, monkeypatch):
    db = _healthy_board(tmp_path)
    replacement = tmp_path / "replacement.db"
    with sqlite3.connect(str(replacement)) as conn:
        conn.execute("CREATE TABLE replacement_marker(value TEXT)")
        conn.execute("INSERT INTO replacement_marker VALUES ('replacement')")
    replacement_bytes = replacement.read_bytes()

    calls = _install_sqlite_process_fake(
        monkeypatch,
        tmp_path,
        db,
        after_recover=lambda: os.replace(replacement, db),
    )

    ok, detail = kw._attempt_board_db_recovery(_FakeKb(db), "slug")

    assert not ok
    assert "board file was replaced during recovery" in detail
    assert len(calls) == 3
    assert db.read_bytes() == replacement_bytes
    assert not list(tmp_path.glob("kanban.db.corrupt-*.bak"))
    assert not list(tmp_path.glob("kanban.db.recovered-*.tmp"))


def test_recovery_fails_closed_when_cli_returns_empty_recovery(tmp_path, monkeypatch):
    db = _healthy_board(tmp_path)
    original = db.read_bytes()
    calls = _install_sqlite_process_fake(
        monkeypatch,
        tmp_path,
        db,
        recover_failure=(0, b"", b"sql error: no such table: sqlite_dbpage (1)\n"),
    )

    ok, detail = kw._attempt_board_db_recovery(_FakeKb(db), "slug")

    assert not ok
    assert ".recover failed:" in detail
    assert "sqlite_dbpage" in detail
    assert len(calls) == 1
    assert db.read_bytes() == original
    assert not list(tmp_path.glob("kanban.db.corrupt-*.bak"))
    assert not list(tmp_path.glob("kanban.db.recovered-*.tmp"))


def _require_working_real_recover(tmp_path):
    sqlite3_cli = shutil.which("sqlite3")
    if not sqlite3_cli:
        pytest.skip("sqlite3 CLI is not installed")

    probe = tmp_path / "recover-capability.db"
    recovered = tmp_path / "recover-capability-reloaded.db"
    with sqlite3.connect(str(probe)) as conn:
        conn.execute("CREATE TABLE probe(value TEXT)")
        conn.execute("INSERT INTO probe VALUES ('present')")

    dump = subprocess.run(
        [sqlite3_cli, str(probe), ".recover"],
        capture_output=True,
        timeout=30,
    )
    if dump.returncode != 0 or not dump.stdout.strip():
        pytest.skip("sqlite3 CLI cannot execute .recover on this host")
    load = subprocess.run(
        [sqlite3_cli, str(recovered)],
        input=dump.stdout,
        capture_output=True,
        timeout=30,
    )
    check = subprocess.run(
        [sqlite3_cli, str(recovered), "PRAGMA integrity_check"],
        capture_output=True,
        timeout=30,
    )
    if load.returncode != 0 or check.stdout.decode(errors="replace").strip() != "ok":
        pytest.skip("sqlite3 CLI .recover output is not reloadable on this host")


def test_real_sqlite_cli_recovery_smoke_when_supported(tmp_path):
    _require_working_real_recover(tmp_path)
    db = _healthy_board(tmp_path)

    ok, detail = kw._attempt_board_db_recovery(_FakeKb(db), "slug")

    assert ok, detail
    assert db.exists() and _integrity_ok(db)
    assert _task_count(db) == 1
