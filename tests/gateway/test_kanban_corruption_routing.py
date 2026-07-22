"""Behavioral tests for notifier-originated corruption routing."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.kanban_watchers import GatewayKanbanWatchersMixin
from hermes_cli import kanban_db as kb
import gateway.kanban_watchers as kw


@pytest.fixture
def hermetic_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    wall_now = [1_750_000_000.0]
    mono_now = [10_000.0]
    monkeypatch.setattr(kw.time, "time", lambda: wall_now[0])
    monkeypatch.setattr(kw.time, "monotonic", lambda: mono_now[0])
    monkeypatch.setattr(kb, "_corruption_wall_clock", lambda: wall_now[0])
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home, wall_now, mono_now


class RecordingAdapter:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text})
        return None

    def extract_local_files(self, _text):
        return [], None


class FakeWatcher(GatewayKanbanWatchersMixin):
    def __init__(self, adapter, *, wall_now, mono_now):
        self._running = True
        self.adapters = {Platform.TELEGRAM: adapter}
        self._kanban_sub_fail_counts = {}
        self._kanban_wall_clock = lambda: wall_now[0]
        self._kanban_monotonic_clock = lambda: mono_now[0]
        self.config = SimpleNamespace(get_home_channel=lambda _platform: None)

    def _active_profile_name(self):
        return "default"

    def _authorization_adapter(self, _platform, _profile=None):
        return next(iter(self.adapters.values()))

    async def _kanban_notify_home_fallback(self, message):
        return True


async def _run_one_notifier_tick(monkeypatch, runner):
    async def fake_sleep(delay):
        if delay != 5:
            runner._running = False

    monkeypatch.setattr(kw.asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def test_notifier_detection_publishes_incident_dispatcher_consumes_it(
    tmp_path, hermetic_home, monkeypatch
):
    adapter = RecordingAdapter()
    runner = FakeWatcher(
        adapter,
        wall_now=hermetic_home[1],
        mono_now=hermetic_home[2],
    )
    db = Path(kb.kanban_db_path())
    real_list_notify_subs = kb.list_notify_subs

    # Keep the real connection fast path, but make the first notifier query
    # encounter the same malformed-image error as the production b-tree read.
    def corrupt_notify_read(_conn, *args, **kwargs):
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(kb, "list_notify_subs", corrupt_notify_read)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    incident = kb.read_corruption_incident(db)
    assert incident is not None
    assert incident.backup_path is not None and incident.backup_path.exists()
    assert incident.preservation_status == kb.CORRUPTION_PRESERVATION_PUBLISHED

    attempts = []

    def fake_recovery(_kb, slug, **_kwargs):
        attempts.append((slug, incident.incident_id))
        return kw.RecoveryResult(
            kw.RecoveryStatus.UNAVAILABLE,
            "injected capability unavailable",
        )

    monkeypatch.setattr(kb, "list_notify_subs", real_list_notify_subs)
    monkeypatch.setattr(kw, "_attempt_board_db_recovery", fake_recovery)
    monkeypatch.setattr(kb, "reap_worker_zombies", lambda: [])
    runner._running = True

    async def dispatcher_sleep(_delay):
        if _delay != 5:
            runner._running = False

    monkeypatch.setattr(kw.asyncio, "sleep", dispatcher_sleep)
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "kanban": {
                "dispatch_in_gateway": True,
                "dispatch_interval_seconds": 1,
                "auto_decompose": False,
            }
        },
    )
    asyncio.run(runner._kanban_dispatcher_watcher())

    assert attempts == [("default", incident.incident_id)]
    assert len(list(tmp_path.glob("**/kanban.db.corrupt.*.bak"))) == 1


def test_notifier_corrupt_board_does_not_suppress_healthy_board(
    tmp_path, hermetic_home, monkeypatch
):
    kb.create_board("damaged")
    kb.create_board("healthy")
    damaged_db = Path(kb.kanban_db_path("damaged"))
    healthy_db = Path(kb.kanban_db_path("healthy"))
    with kb.connect_closing(board="healthy") as conn:
        task_id = kb.create_task(conn, title="deliver healthy", assignee="worker")
        kb.add_notify_sub(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="healthy-chat",
        )
        kb.complete_task(conn, task_id, summary="healthy board event")

    real_list_notify_subs = kb.list_notify_subs

    def list_subs_isolated(conn, *args, **kwargs):
        db_file = Path(conn.execute("PRAGMA database_list").fetchone()[2]).resolve()
        if db_file == damaged_db.resolve():
            raise sqlite3.DatabaseError("database disk image is malformed")
        return real_list_notify_subs(conn, *args, **kwargs)

    monkeypatch.setattr(kb, "list_notify_subs", list_subs_isolated)
    adapter = RecordingAdapter()
    runner = FakeWatcher(
        adapter,
        wall_now=hermetic_home[1],
        mono_now=hermetic_home[2],
    )
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert task_id in adapter.sent[0]["text"]
    damaged_incident = kb.read_corruption_incident(damaged_db)
    assert damaged_incident is not None
    assert kb.read_corruption_incident(healthy_db) is None
    assert all(path.is_relative_to(tmp_path) for path in tmp_path.rglob("*"))


def test_dispatcher_reenables_same_runner_after_same_size_atomic_replacement(
    tmp_path, hermetic_home, monkeypatch
):
    """A changed inode is health-probed and dispatched without a restart."""
    from gateway.run import GatewayRunner
    import hermes_cli.config as config_mod

    db = Path(kb.kanban_db_path())
    kb.init_db(db)
    with kb.connect_closing(db) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    healthy_bytes = db.read_bytes()
    old_stat = db.stat()
    with db.open("r+b") as handle:
        handle.seek(5)
        handle.write(b"\x17\x03\x03\x00\x20" + b"x" * 32)
    incident = kb.ensure_corruption_incident(db, "notifier detected malformed image")

    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner._kanban_wall_clock = lambda: hermetic_home[1][0]
    runner._kanban_monotonic_clock = lambda: hermetic_home[2][0]

    async def fake_notify(_message):
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
        kb,
        "list_boards",
        lambda include_archived=False: [{"slug": "default"}],
    )
    monkeypatch.setattr(kb, "read_board_metadata", lambda slug: {"slug": slug})
    monkeypatch.setattr(
        kw,
        "_attempt_board_db_recovery",
        lambda *_args, **_kwargs: kw.RecoveryResult(
            kw.RecoveryStatus.UNAVAILABLE, "test capability unavailable"
        ),
    )
    dispatches = []
    monkeypatch.setattr(
        kb,
        "dispatch_once",
        lambda _conn, **kwargs: dispatches.append(kwargs["board"]),
    )
    monkeypatch.setattr(kb, "reap_worker_zombies", lambda: [])

    tick_count = 0

    async def fake_to_thread(fn, *args, **kwargs):
        nonlocal tick_count
        result = fn(*args, **kwargs)
        if getattr(fn, "__name__", "") == "_tick_once":
            tick_count += 1
            if tick_count == 1:
                replacement = tmp_path / "healthy-replacement.db"
                replacement.write_bytes(healthy_bytes)
                os.utime(
                    replacement,
                    ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns),
                )
                os.replace(replacement, db)
                hermetic_home[2][0] += 1.0
            else:
                runner._running = False
        return result

    async def fake_sleep(_delay):
        return None

    monkeypatch.setattr(kw.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(kw.asyncio, "sleep", fake_sleep)

    asyncio.run(runner._kanban_dispatcher_watcher())

    assert incident.incident_id != ""
    assert dispatches == ["default"]
    assert kb.read_corruption_incident(db) is None
    assert all(path.is_relative_to(tmp_path) for path in tmp_path.rglob("*"))
