"""Behavioral contracts for a corrupt board DB after BUILD-716.

The dispatcher no longer tries to rebuild the board in place: the in-place
``sqlite3 .recover`` swap renamed the live DB and its ``-wal``/``-shm`` out from
under every open connection, which is itself a corruption source. What must
survive is the *containment*: quarantine the board, page the operator once per
incident, retry a failed alert delivery on the existing deadline, and leave the
file byte-for-byte alone so an operator can recover from the original.
"""

import asyncio
import logging

import pytest

import gateway.kanban_watchers as kw


@pytest.fixture
def hermetic_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    return home


@pytest.mark.parametrize("initial_delivery_failure", ["false", "exception"])
def test_corrupt_board_alert_retries_only_after_delivery_deadline(
    tmp_path, hermetic_home, monkeypatch, caplog, initial_delivery_failure
):
    """One incident pages once, retries a failed send, and never edits the DB."""
    from gateway.run import GatewayRunner

    import hermes_cli.config as config_mod
    import hermes_cli.kanban_db as kanban_mod

    wall_now = [1_750_000_000.0]
    mono_now = [0.0]
    monkeypatch.setattr(kw.time, "time", lambda: wall_now[0])
    monkeypatch.setattr(kw.time, "monotonic", lambda: mono_now[0])

    db = tmp_path / "boards" / "alpha.db"
    db.parent.mkdir()
    db.write_bytes(b"not a database")
    backup = tmp_path / "boards" / "alpha.db.corrupt.forensics.bak"
    backup.write_bytes(db.read_bytes())
    inode_before = db.stat().st_ino

    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner._kanban_corrupt_wall_clock = lambda: wall_now[0]
    runner._kanban_corrupt_monotonic_clock = lambda: mono_now[0]
    attempts = []

    async def fake_notify(message):
        attempts.append((mono_now[0], message))
        if len(attempts) == 1:
            if initial_delivery_failure == "exception":
                raise RuntimeError("transient Telegram DNS failure")
            return False
        if len(attempts) == 2:
            # Corrupt bytes may change while this incident remains active;
            # the incident ID, not the byte fingerprint, owns the alert key.
            db.write_bytes(b"changed corrupt database")
        return True

    runner._kanban_notify_home_fallback = fake_notify

    monkeypatch.setattr(
        config_mod,
        "load_config",
        lambda: {
            "kanban": {
                "dispatch_in_gateway": True,
                "dispatch_interval_seconds": 1,
                "auto_decompose": False,
            }
        },
    )
    monkeypatch.setattr(
        kanban_mod,
        "list_boards",
        lambda include_archived=False: [{"slug": "alpha"}],
    )
    monkeypatch.setattr(kanban_mod, "read_board_metadata", lambda slug: {"slug": slug})
    monkeypatch.setattr(kanban_mod, "kanban_db_path", lambda board=None: db)

    def fake_connect(*args, **kwargs):
        raise kanban_mod.KanbanDbCorruptError(db, backup, "database disk image is malformed")

    monkeypatch.setattr(kanban_mod, "connect", fake_connect)

    tick_count = 0

    async def fake_to_thread(fn, *args, **kwargs):
        nonlocal tick_count
        if getattr(fn, "__name__", "") == "_tick_once":
            tick_count += 1
            # The first alert attempt is at t=0. Quarantine re-probes at
            # t=300, while alert delivery retries exactly at t=900.
            mono_now[0] = {
                1: 0.0,
                2: 100.0,
                3: 300.0,
                4: 900.0,
                5: 901.0,
                6: 902.0,
            }[tick_count]
        result = fn(*args, **kwargs)
        if getattr(fn, "__name__", "") == "_tick_once":
            if tick_count >= 6:
                runner._running = False
        return result

    async def fake_sleep(_delay):
        return None

    monkeypatch.setattr(kw.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(kw.asyncio, "sleep", fake_sleep)

    with caplog.at_level(logging.INFO, logger="gateway.run"):
        asyncio.run(runner._kanban_dispatcher_watcher())

    assert len(attempts) == 2
    assert [timestamp for timestamp, _message in attempts] == [0.0, 900.0]
    first_alert = attempts[0][1]
    assert "alpha" in first_alert
    assert str(db.resolve()) in first_alert
    assert str(backup.resolve()) in first_alert
    assert "PAUSED" in first_alert
    assert "UNCHANGED" in first_alert
    assert "operator action" in first_alert
    assert "kanban init" not in first_alert
    # BUILD-716: nothing on this path may auto-recover or leave swap debris.
    assert "auto-recovered" not in first_alert
    assert not list(tmp_path.rglob("*.recovered-*"))
    assert not list(tmp_path.rglob("*.rollback-*"))
    assert not list(tmp_path.rglob("*.fdmap-*.txt"))
    # Only the test's own rewrite at attempt 2 changed the file; the inode the
    # dispatcher saw is still the live one.
    assert db.stat().st_ino == inode_before

    first_reports = [
        record for record in caplog.records
        if "the file is left UNCHANGED" in record.getMessage()
    ]
    repeat_reports = [
        record for record in caplog.records
        if "still corrupt" in record.getMessage()
    ]
    assert [record.levelno for record in first_reports] == [logging.ERROR]
    assert repeat_reports
    assert {record.levelno for record in repeat_reports} == {logging.INFO}
    assert all(path.is_relative_to(tmp_path) for path in tmp_path.rglob("*"))
