"""Hermetic contracts for cross-process Kanban corruption incidents."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def hermetic_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    wall_now = [1_750_000_000.0]
    mono_now = [10_000.0]
    monkeypatch.setattr(kb, "_corruption_wall_clock", lambda: wall_now[0])
    # The DB layer has no retry deadline, but keep a monotonic seam in every
    # incident fixture so tests cannot accidentally consult real time.
    monkeypatch.setattr(kb.time, "monotonic", lambda: mono_now[0])
    kb._INITIALIZED_PATHS.clear()
    return home, wall_now, mono_now


def _healthy_db(path: Path) -> bytes:
    kb.init_db(db_path=path)
    with kb.connect_closing(db_path=path) as conn:
        kb.create_task(conn, title="preserve-me", assignee="worker")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return path.read_bytes()


def _corrupt_header(path: Path) -> None:
    with path.open("r+b") as handle:
        handle.seek(5)
        handle.write(b"\x17\x03\x03\x00\x20" + b"x" * 32)


def _raise_corrupt(path: Path) -> kb.KanbanDbCorruptError:
    with pytest.raises(kb.KanbanDbCorruptError) as info:
        kb.connect(db_path=path)
    return info.value


def test_same_incident_reaches_fresh_processes_after_corrupt_bytes_change(
    tmp_path, hermetic_home
):
    """Workers reuse the marker identity while an open writer changes bytes."""
    db = tmp_path / "kanban.db"
    _healthy_db(db)
    writer = kb.connect(db_path=db)
    task_id = writer.execute("SELECT id FROM tasks LIMIT 1").fetchone()[0]
    _corrupt_header(db)

    first = _raise_corrupt(db)
    writer.execute(
        "INSERT INTO task_events(task_id, kind, payload, created_at) "
        "VALUES (?, 'status', '{}', ?)",
        (task_id, 1),
    )
    writer.commit()
    writer.close()

    child_code = """
import json
import sys
from pathlib import Path
from hermes_cli import kanban_db as kb
try:
    kb.connect(db_path=Path(sys.argv[1]))
except kb.KanbanDbCorruptError as exc:
    print(json.dumps({"incident": exc.incident_id, "backup": str(exc.backup_path)}))
else:
    raise SystemExit("corrupt DB unexpectedly opened")
"""
    child_env = os.environ.copy()
    child_env["HERMES_HOME"] = str(hermetic_home[0])
    child_env.pop("HERMES_KANBAN_DB", None)
    child_env.pop("HERMES_KANBAN_BOARD", None)
    results = []
    for _ in range(3):
        completed = subprocess.run(
            [sys.executable, "-c", child_code, str(db)],
            cwd=str(Path.cwd()),
            env=child_env,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        results.append(json.loads(completed.stdout))

    assert {item["incident"] for item in results} == {first.incident_id}
    assert {item["backup"] for item in results} == {str(first.backup_path)}
    assert len(list(tmp_path.glob("kanban.db.corrupt.*.bak"))) == 1
    assert first.backup_path.read_bytes() != db.read_bytes()


def test_changed_generation_and_in_place_healing_are_explicit_health_transitions(
    tmp_path, hermetic_home
):
    db = tmp_path / "kanban.db"
    healthy_bytes = _healthy_db(db)

    _corrupt_header(db)
    first = _raise_corrupt(db)
    old_stat = db.stat()
    replacement = tmp_path / "replacement.db"
    replacement.write_bytes(healthy_bytes)
    os.utime(replacement, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
    os.replace(replacement, db)
    assert db.stat().st_ino != old_stat.st_ino
    with kb.connect_closing(db_path=db) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert kb.read_corruption_incident(db) is None

    # A second incident on the repaired generation gets a new identity.
    kb._INITIALIZED_PATHS.discard(str(db.resolve()))
    _corrupt_header(db)
    second = _raise_corrupt(db)
    assert second.incident_id != first.incident_id
    repaired_inode = db.stat().st_ino
    db.write_bytes(healthy_bytes)
    assert db.stat().st_ino == repaired_inode

    # Normal opens still fail closed for the unchanged incident. The
    # dispatcher-owned forced probe is the healing transition.
    with pytest.raises(kb.KanbanDbCorruptError) as info:
        kb.connect(db_path=db)
    assert info.value.incident_id == second.incident_id
    assert kb.probe_corruption_incident(db) is True
    assert kb.read_corruption_incident(db) is None
    with kb.connect(db_path=db) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    backups = list(tmp_path.glob("kanban.db.corrupt.*.bak"))
    assert len(backups) == 2
    assert all(path.is_relative_to(tmp_path) for path in tmp_path.rglob("*"))


@pytest.mark.parametrize(
    "error",
    [
        sqlite3.OperationalError("database is locked"),
        sqlite3.OperationalError("disk I/O error"),
        sqlite3.DatabaseError("disk I/O error"),
    ],
)
def test_non_corruption_probe_errors_do_not_publish_incidents(
    tmp_path, hermetic_home, monkeypatch, error
):
    db = tmp_path / "kanban.db"
    _healthy_db(db)
    kb._INITIALIZED_PATHS.discard(str(db.resolve()))

    def fail_connect(_path):
        raise type(error)(str(error))

    monkeypatch.setattr(kb, "_sqlite_connect", fail_connect)
    with pytest.raises(type(error), match=str(error)):
        kb.connect(db_path=db)

    assert kb.read_corruption_incident(db) is None
    assert list(tmp_path.glob("*.corrupt.*.bak")) == []


def test_new_corruption_after_healing_has_one_new_forensic_backup(
    tmp_path, hermetic_home
):
    db = tmp_path / "kanban.db"
    healthy_bytes = _healthy_db(db)

    _corrupt_header(db)
    first = _raise_corrupt(db)
    db.write_bytes(healthy_bytes)
    assert kb.probe_corruption_incident(db) is True
    kb._INITIALIZED_PATHS.discard(str(db.resolve()))

    _corrupt_header(db)
    second = _raise_corrupt(db)
    assert second.incident_id != first.incident_id
    assert second.backup_path != first.backup_path
    assert len(list(tmp_path.glob("kanban.db.corrupt.*.bak"))) == 2


def test_marker_publication_interruption_is_recoverable_without_partial_json(
    tmp_path, hermetic_home, monkeypatch
):
    """A process dying after the pending marker leaves a retryable epoch."""
    db = tmp_path / "kanban.db"
    _healthy_db(db)
    _corrupt_header(db)
    corrupt_bytes = db.read_bytes()
    real_write_marker = kb._atomic_write_json
    calls = [0]

    def interrupt_after_pending(marker, payload):
        calls[0] += 1
        real_write_marker(marker, payload)
        if calls[0] == 1:
            raise OSError("simulated interruption after marker publication")

    monkeypatch.setattr(kb, "_atomic_write_json", interrupt_after_pending)
    with pytest.raises(kb.KanbanDbCorruptError) as first_error:
        kb.connect(db_path=db)
    assert first_error.value.backup_path is None
    pending = kb.read_corruption_incident(db)
    assert pending is not None
    assert pending.preservation_status == kb.CORRUPTION_PRESERVATION_PENDING
    json.loads(kb.corruption_incident_path(db).read_text(encoding="utf-8"))
    assert db.read_bytes() == corrupt_bytes

    with pytest.raises(kb.KanbanDbCorruptError) as second_error:
        kb.connect(db_path=db)
    healed_marker = kb.read_corruption_incident(db)
    assert healed_marker is not None
    assert healed_marker.incident_id == pending.incident_id
    assert healed_marker.preservation_status == kb.CORRUPTION_PRESERVATION_PUBLISHED
    assert second_error.value.backup_path == healed_marker.backup_path
    assert second_error.value.backup_path is not None
    assert second_error.value.backup_path.read_bytes() == corrupt_bytes
    assert len(list(tmp_path.glob("*.corrupt.*.bak"))) == 1
