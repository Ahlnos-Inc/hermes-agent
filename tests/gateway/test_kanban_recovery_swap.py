"""Behavioral contracts for corrupt-board recovery and delivery-aware alerts.

The swap must abort (and retry next tick) if any writer commits between the
sqlite3 ``.recover`` snapshot and the atomic file swap, and must succeed when
the board is quiet. Silently discarding a concurrent notifier commit — or
letting a fresh ``connect()`` bind a stale ``-wal`` to the recovered inode
mid-swap — was a corruption seed. Hosts without sqlite3's ``dbpage`` support
are an expected, passing outcome rather than a skipped test.
"""

import asyncio
import sqlite3

import pytest

import gateway.kanban_watchers as kw
from hermes_cli import kanban_db as kb


@pytest.fixture
def hermetic_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    return home


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


def test_recovery_follows_real_host_capability(tmp_path, hermetic_home, monkeypatch):
    monkeypatch.setattr(kw.time, "time", lambda: 1_750_000_000.0)
    monkeypatch.setattr(kw.time, "monotonic", lambda: 10_000.0)
    db = _healthy_board(tmp_path)
    original_bytes = db.read_bytes()
    original_inode = db.stat().st_ino
    result = kw._attempt_board_db_recovery(_FakeKb(db), "slug")

    if result.status == kw.RecoveryStatus.RECOVERED:
        assert "corrupt original preserved" in result.detail
        assert db.exists() and _integrity_ok(db)
        # The pre-swap original was preserved, not destroyed.
        assert len(list(tmp_path.glob("kanban.db.corrupt-*.bak"))) == 1
    elif result.status == kw.RecoveryStatus.UNAVAILABLE:
        assert db.read_bytes() == original_bytes
        assert db.stat().st_ino == original_inode
        assert not list(tmp_path.glob("kanban.db.corrupt-*.bak"))
        assert not list(tmp_path.glob("kanban.db.recovered-*.tmp"))
    else:
        raise AssertionError(f"unexpected host recovery result: {result!r}")


def test_recovery_capability_unavailable_leaves_board_untouched(
    tmp_path, hermetic_home, monkeypatch
):
    monkeypatch.setattr(kw.time, "time", lambda: 1_750_000_000.0)
    monkeypatch.setattr(kw.time, "monotonic", lambda: 10_000.0)
    db = _healthy_board(tmp_path)
    original_bytes = db.read_bytes()
    original_inode = db.stat().st_ino
    seen_binaries = []

    fake_cli = tmp_path / "bin" / "sqlite3"
    monkeypatch.setattr(kw, "_resolve_sqlite_cli", lambda: str(fake_cli))

    def fake_probe(sqlite3_cli):
        seen_binaries.append(sqlite3_cli)
        return False, "capability probe exited 1: no such table: sqlite_dbpage (1)"

    monkeypatch.setattr(kw, "_probe_sqlite_recover_capability", fake_probe)

    result = kw._attempt_board_db_recovery(_FakeKb(db), "slug")

    assert result.status == kw.RecoveryStatus.UNAVAILABLE
    assert "sqlite_dbpage" in result.detail
    assert seen_binaries == [str(fake_cli)]
    assert db.read_bytes() == original_bytes
    assert db.stat().st_ino == original_inode
    assert not list(tmp_path.glob("kanban.db.corrupt-*.bak"))
    assert not list(tmp_path.glob("kanban.db.recovered-*.tmp"))


def test_recovery_aborts_when_board_changes_mid_recover(
    tmp_path, hermetic_home, monkeypatch
):
    monkeypatch.setattr(kw.time, "time", lambda: 1_750_000_000.0)
    monkeypatch.setattr(kw.time, "monotonic", lambda: 10_000.0)
    db = _healthy_board(tmp_path)
    # Keep this test deterministic on runners whose sqlite3 lacks dbpage;
    # the first test is the real-host capability contract.
    monkeypatch.setattr(
        kw,
        "_probe_sqlite_recover_capability",
        lambda _sqlite3_cli: (True, "injected .recover capability"),
    )
    real_run = kw.subprocess.run
    calls = {"n": 0}

    def fake_run(*args, **kwargs):
        result = real_run(*args, **kwargs)
        # After the ``.recover`` read completes (first recovery subprocess),
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

    result = kw._attempt_board_db_recovery(_FakeKb(db), "slug")
    assert result.status == kw.RecoveryStatus.RETRY
    assert "changed during recovery" in result.detail
    # No swap happened: the live DB was never renamed out.
    assert not list(tmp_path.glob("kanban.db.corrupt-*.bak"))
    # The concurrent writer's commit survived (it was not clobbered by a swap).
    conn = sqlite3.connect(str(db))
    try:
        assert conn.execute("SELECT count(*) FROM _concurrent").fetchone()[0] == 1
    finally:
        conn.close()
    assert not list(tmp_path.glob("kanban.db.recovered-*.tmp"))


@pytest.mark.parametrize("initial_delivery_failure", ["false", "exception"])
def test_unavailable_recovery_alert_retries_only_after_delivery_deadline(
    tmp_path, hermetic_home, monkeypatch, caplog, initial_delivery_failure
):
    """An unavailable capability is retried and alerted without touching the DB."""
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
            # A changed fingerprint must create a new alert key after the
            # original alert is confirmed delivered.
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
    fake_cli = tmp_path / "bin" / "sqlite3"
    probe_binaries = []
    monkeypatch.setattr(kw, "_resolve_sqlite_cli", lambda: str(fake_cli))

    def fake_probe(sqlite3_cli):
        probe_binaries.append(sqlite3_cli)
        return False, "capability probe exited 1: no such table: sqlite_dbpage (1)"

    monkeypatch.setattr(kw, "_probe_sqlite_recover_capability", fake_probe)

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

    import logging

    with caplog.at_level(logging.INFO, logger="gateway.run"):
        asyncio.run(runner._kanban_dispatcher_watcher())

    assert len(attempts) == 3
    assert [timestamp for timestamp, _message in attempts] == [0.0, 900.0, 901.0]
    first_alert = attempts[0][1]
    assert "alpha" in first_alert
    assert str(db.resolve()) in first_alert
    assert "UNAVAILABLE" in first_alert
    assert "sqlite_dbpage" in first_alert
    assert str(backup.resolve()) in first_alert
    assert "PAUSED" in first_alert
    assert "UNCHANGED" in first_alert
    assert "recover-capable sqlite3" in first_alert
    assert "known-good backup" in first_alert
    assert "kanban init" not in first_alert
    assert probe_binaries == [str(fake_cli)] * 4
    assert not list(tmp_path.glob("*.fdmap-*.txt"))
    unavailable_logs = [
        record for record in caplog.records
        if "automatic recovery is UNAVAILABLE" in record.getMessage()
    ]
    assert [record.levelno for record in unavailable_logs] == [
        logging.ERROR,
        logging.INFO,
        logging.INFO,
        logging.ERROR,
    ]
    assert all(path.is_relative_to(tmp_path) for path in tmp_path.rglob("*"))


def test_recovered_alert_is_delivered_once_for_a_fingerprint(
    tmp_path, hermetic_home, monkeypatch
):
    from gateway.run import GatewayRunner

    import hermes_cli.config as config_mod
    import hermes_cli.kanban_db as kanban_mod

    mono_now = [0.0]
    monkeypatch.setattr(kw.time, "time", lambda: 1_750_000_000.0)
    monkeypatch.setattr(kw.time, "monotonic", lambda: mono_now[0])

    db = tmp_path / "boards" / "alpha.db"
    db.parent.mkdir()
    db.write_bytes(b"not a database")
    backup = tmp_path / "boards" / "alpha.db.corrupt.bak"
    backup.write_bytes(db.read_bytes())

    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner._kanban_corrupt_wall_clock = lambda: 1_750_000_000.0
    runner._kanban_corrupt_monotonic_clock = lambda: mono_now[0]
    attempts = []

    async def fake_notify(message):
        attempts.append(message)
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
    monkeypatch.setattr(
        kw,
        "_attempt_board_db_recovery",
        lambda _kb, _slug, **_kwargs: kw.RecoveryResult(
            kw.RecoveryStatus.RECOVERED,
            "injected recovered result",
        ),
    )

    tick_count = 0

    async def fake_to_thread(fn, *args, **kwargs):
        nonlocal tick_count
        result = fn(*args, **kwargs)
        if getattr(fn, "__name__", "") == "_tick_once":
            tick_count += 1
            mono_now[0] = float(tick_count)
            if tick_count >= 2:
                runner._running = False
        return result

    async def fake_sleep(_delay):
        return None

    monkeypatch.setattr(kw.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(kw.asyncio, "sleep", fake_sleep)

    asyncio.run(runner._kanban_dispatcher_watcher())

    assert len(attempts) == 1
    assert "auto-recovered" in attempts[0]
    assert "dispatch resumes next tick" in attempts[0]
    assert str(db.resolve()) in attempts[0]
