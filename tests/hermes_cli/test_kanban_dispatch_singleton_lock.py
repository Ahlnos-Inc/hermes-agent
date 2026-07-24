"""Tests for BUILD-263: `hermes kanban dispatch` single-dispatcher lock.

2026-07-08 incident: two orphaned shell loops ran `hermes -p orchestrator
kanban dispatch` every 60s for 6 and 19 days alongside the gateway's
internal scheduler — concurrent dispatchers over the same SQLite kanban.db
with no mutual exclusion. The CLI `dispatch` entry now takes the exact same
machine-wide singleton lock the gateway's embedded dispatcher holds
(`gateway.kanban_watchers._acquire_singleton_lock` /
`dispatcher_singleton_lock_path`), refusing loudly (nonzero exit) when
another dispatcher already holds it, including with `--force`. The force
flag is retained for compatibility and never bypasses the singleton lock.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from gateway.kanban_watchers import (
    _acquire_singleton_lock,
    _release_singleton_lock,
    dispatcher_singleton_lock_path,
)
from hermes_cli import kanban as kb_cli
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


def _args(**overrides):
    base = dict(dry_run=True, max=None, failure_limit=2, json=False, force=False)
    base.update(overrides)
    return argparse.Namespace(**base)


def _stub_dispatch_once(monkeypatch, calls):
    def fake(conn, **kwargs):
        calls.append(kwargs)
        return kb.DispatchResult()

    monkeypatch.setattr(kb, "dispatch_once", fake)


def test_dispatch_proceeds_when_lock_is_free(kanban_home, monkeypatch):
    calls = []
    _stub_dispatch_once(monkeypatch, calls)

    rc = kb_cli._cmd_dispatch(_args())

    assert rc == 0
    assert len(calls) == 1


def test_dispatch_releases_lock_after_running(kanban_home, monkeypatch):
    """After a normal (uncontended) run, the lock must be released — a
    subsequent acquire attempt (as the gateway would do) must succeed."""
    calls = []
    _stub_dispatch_once(monkeypatch, calls)

    rc = kb_cli._cmd_dispatch(_args())
    assert rc == 0

    lock_path = dispatcher_singleton_lock_path()
    handle, state = _acquire_singleton_lock(lock_path)
    assert state == "held", "CLI must release the lock when its run finishes"
    _release_singleton_lock(handle)


def test_dispatch_refuses_when_another_dispatcher_holds_the_lock(
    kanban_home, monkeypatch, capsys,
):
    calls = []
    monkeypatch.setattr(
        kb_cli, "_cmd_dispatch_run", lambda _args: calls.append(True) or 0,
    )

    lock_path = dispatcher_singleton_lock_path()
    holder_handle, holder_state = _acquire_singleton_lock(lock_path)
    assert holder_state == "held"
    try:
        rc = kb_cli._cmd_dispatch(_args())
    finally:
        _release_singleton_lock(holder_handle)

    assert rc != 0
    assert calls == [], "_cmd_dispatch_run must not run on lock contention"
    err = capsys.readouterr().err
    assert "refusing" in err.lower()
    assert "--force" in err


def test_dispatch_force_does_not_bypass_a_held_lock(
    kanban_home, monkeypatch, capsys,
):
    calls = []
    monkeypatch.setattr(
        kb_cli, "_cmd_dispatch_run", lambda _args: calls.append(True) or 0,
    )

    lock_path = dispatcher_singleton_lock_path()
    holder_handle, holder_state = _acquire_singleton_lock(lock_path)
    assert holder_state == "held"
    try:
        rc = kb_cli._cmd_dispatch(_args(force=True))
    finally:
        _release_singleton_lock(holder_handle)

    assert rc != 0
    assert calls == [], "--force must not bypass lock contention"
    assert "cannot override" in capsys.readouterr().err


@pytest.mark.parametrize("force", [False, True])
def test_daemon_refuses_when_another_dispatcher_holds_the_lock(
    kanban_home, monkeypatch, capsys, force,
):
    """The legacy daemon must share dispatch's non-bypassable lock guard."""
    calls = []
    monkeypatch.setattr(kb, "run_daemon", lambda **_kwargs: calls.append(True))

    lock_path = dispatcher_singleton_lock_path()
    holder_handle, holder_state = _acquire_singleton_lock(lock_path)
    assert holder_state == "held"
    try:
        rc = kb_cli._cmd_daemon(_args(force=force, interval=0.01))
    finally:
        _release_singleton_lock(holder_handle)

    assert rc == 3
    assert calls == [], "daemon must not start its loop on lock contention"
    err = capsys.readouterr().err
    assert "hermes kanban daemon: refusing" in err
    assert "--force" in err


def test_daemon_holds_lock_while_running(kanban_home, monkeypatch):
    """A running legacy daemon blocks a concurrent one-shot dispatch."""
    dispatch_results = []

    def fake_run_daemon(**_kwargs):
        dispatch_results.append(kb_cli._cmd_dispatch(_args()))

    monkeypatch.setattr(kb, "run_daemon", fake_run_daemon)

    rc = kb_cli._cmd_daemon(_args(force=True, interval=0.01))

    assert rc == 0
    assert dispatch_results == [3]


def test_gateway_skips_dispatcher_when_lock_acquisition_raises(
    kanban_home, monkeypatch, caplog,
):
    """A broken lock mechanism must not silently enable gateway dispatch."""
    import asyncio
    import logging

    from gateway.run import GatewayRunner
    import gateway.kanban_watchers as watchers_mod
    import hermes_cli.config as config_mod

    runner = object.__new__(GatewayRunner)
    runner._running = True
    monkeypatch.setattr(
        config_mod,
        "load_config",
        lambda: {"kanban": {"dispatch_in_gateway": True}},
    )

    lock_path = dispatcher_singleton_lock_path()

    def raise_lock(path):
        assert path == lock_path
        raise OSError("simulated lock filesystem failure")

    monkeypatch.setattr(watchers_mod, "_acquire_singleton_lock", raise_lock)
    dispatch_threads = []

    async def unexpected_dispatch_thread(*_args, **_kwargs):
        dispatch_threads.append(True)
        raise AssertionError("dispatcher loop must not start without its lock")

    monkeypatch.setattr(watchers_mod.asyncio, "to_thread", unexpected_dispatch_thread)

    with caplog.at_level(logging.ERROR, logger="gateway.run"):
        asyncio.run(
            asyncio.wait_for(
                runner._kanban_dispatcher_watcher(),
                timeout=3.0,
            )
        )

    assert dispatch_threads == []
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        str(lock_path) in message
        and "kanban.dispatch_in_gateway" in message
        and "refusing" in message.lower()
        for message in messages
    )


def test_dispatch_reports_holder_pid_when_available(kanban_home, monkeypatch, capsys):
    """The lock file is stamped with the holder's pid (diagnostics only) —
    the refusal message should surface it when present."""
    calls = []
    _stub_dispatch_once(monkeypatch, calls)

    lock_path = dispatcher_singleton_lock_path()
    holder_handle, holder_state = _acquire_singleton_lock(lock_path)
    assert holder_state == "held"
    try:
        kb_cli._cmd_dispatch(_args())
    finally:
        _release_singleton_lock(holder_handle)

    err = capsys.readouterr().err
    assert "pid" in err.lower()


def test_stale_lock_is_reclaimed_automatically_after_holder_process_dies(
    kanban_home, monkeypatch,
):
    """Simulate a crashed holder: close its file handle WITHOUT an explicit
    flock release (mirrors what the OS does automatically when a process
    dies — every fd, and any flock tied to it, is released on process
    exit). No special "stale lock" cleanup code should be required for the
    CLI's next attempt to succeed."""
    calls = []
    _stub_dispatch_once(monkeypatch, calls)

    lock_path = dispatcher_singleton_lock_path()
    dead_handle, dead_state = _acquire_singleton_lock(lock_path)
    assert dead_state == "held"
    dead_handle.close()  # simulate process death — no _release_singleton_lock call

    rc = kb_cli._cmd_dispatch(_args())

    assert rc == 0
    assert len(calls) == 1, "a stale (crashed-holder) lock must be reclaimed automatically"


@pytest.mark.parametrize("force", [False, True])
def test_dispatch_refuses_when_lock_is_unavailable(
    kanban_home, monkeypatch, capsys, force,
):
    """Unavailable locking always fails closed, including with --force."""
    calls = []
    _stub_dispatch_once(monkeypatch, calls)

    import gateway.kanban_watchers as watchers_mod

    monkeypatch.setattr(
        watchers_mod, "_acquire_singleton_lock", lambda _path: (None, "unavailable"),
    )

    lock_path = dispatcher_singleton_lock_path()
    rc = kb_cli._cmd_dispatch(_args(force=force))

    assert rc == 3
    assert calls == []
    err = capsys.readouterr().err
    assert str(lock_path) in err
    assert "cannot run without a working singleton lock" in err
    assert "--force" not in err


def test_daemon_force_refuses_when_lock_is_unavailable(
    kanban_home, monkeypatch, capsys,
):
    calls = []
    monkeypatch.setattr(kb, "run_daemon", lambda **_kwargs: calls.append(True))

    import gateway.kanban_watchers as watchers_mod

    monkeypatch.setattr(
        watchers_mod, "_acquire_singleton_lock", lambda _path: (None, "unavailable"),
    )

    lock_path = dispatcher_singleton_lock_path()
    rc = kb_cli._cmd_daemon(_args(force=True, interval=0.01))

    assert rc == 3
    assert calls == []
    err = capsys.readouterr().err
    assert str(lock_path) in err
    assert "cannot run without a working singleton lock" in err
    assert "--force" not in err


def test_dispatch_force_still_attempts_the_canonical_gateway_lock(
    kanban_home, monkeypatch,
):
    calls = []
    _stub_dispatch_once(monkeypatch, calls)
    observed_paths = []

    import gateway.kanban_watchers as watchers_mod

    real_acquire = watchers_mod._acquire_singleton_lock

    def record_acquire(path):
        observed_paths.append(path)
        return real_acquire(path)

    monkeypatch.setattr(watchers_mod, "_acquire_singleton_lock", record_acquire)

    rc = kb_cli._cmd_dispatch(_args(force=True))

    assert rc == 0
    assert calls
    assert observed_paths == [dispatcher_singleton_lock_path()]


def test_run_slash_dispatch_accepts_force_flag(kanban_home):
    """End-to-end argparse coverage: --force must parse on the real CLI
    surface (not just via a hand-built argparse.Namespace)."""
    out = kb_cli.run_slash("dispatch --dry-run --force")
    assert "Spawned:" in out


# ---------------------------------------------------------------------------
# BUILD-634: embedded dispatcher singleton-lock filesystem-error handling
# ---------------------------------------------------------------------------


def test_acquire_singleton_lock_classifies_filesystem_error(tmp_path):
    """AC1: a filesystem error acquiring the lock returns the distinct
    ``error`` state (retryable), not ``unavailable`` (can't-flock, stable)."""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")  # a regular file where a directory would be needed
    lock_path = blocker / ".dispatcher.lock"  # mkdir(parents=True) under a file → OSError

    handle, state = _acquire_singleton_lock(lock_path)

    assert handle is None
    assert state == "error"


def _make_dispatcher_runner(monkeypatch, *, running):
    import gateway.kanban_watchers as watchers_mod
    import hermes_cli.config as config_mod
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._running = running
    monkeypatch.setattr(
        config_mod, "load_config",
        lambda: {"kanban": {"dispatch_in_gateway": True}},
    )
    # Zero the backoff so the retry loop doesn't add real wall-clock delay.
    monkeypatch.setattr(watchers_mod, "_SINGLETON_LOCK_RETRY_DELAY_SECONDS", 0)

    # Collapse the watcher's asyncio.sleep calls (retry backoff + the 5s
    # startup delay before the dispatch loop) so the coroutine returns
    # promptly. asyncio.wait_for doesn't use asyncio.sleep, so this is safe.
    async def _instant_sleep(*_args, **_kwargs):
        return None

    monkeypatch.setattr(watchers_mod.asyncio, "sleep", _instant_sleep)
    return runner, watchers_mod


def test_gateway_dispatcher_retries_transient_lock_filesystem_error(
    kanban_home, monkeypatch, caplog,
):
    """AC3: a transient filesystem error is retried; once the lock is acquired,
    ownership is verified (non-None handle) before startup."""
    import asyncio
    import logging

    # _running=False so the watcher returns after acquiring the lock without
    # entering the dispatch loop — we only assert the retry+ownership outcome.
    runner, watchers_mod = _make_dispatcher_runner(monkeypatch, running=False)

    fake_handle = object()
    seq = [(None, "error"), (fake_handle, "held")]
    state = {"n": 0}

    def flaky_acquire(_path):
        result = seq[min(state["n"], len(seq) - 1)]
        state["n"] += 1
        return result

    monkeypatch.setattr(watchers_mod, "_acquire_singleton_lock", flaky_acquire)

    with caplog.at_level(logging.INFO, logger="gateway.run"):
        asyncio.run(
            asyncio.wait_for(runner._kanban_dispatcher_watcher(), timeout=3.0)
        )

    # Retried exactly once after the transient error, then acquired + started.
    # (The lock handle is released + nulled on watcher exit, so ownership is
    # asserted via the log trail rather than the post-return attribute.)
    assert state["n"] == 2
    messages = [r.getMessage() for r in caplog.records]
    assert any("retrying" in m.lower() for m in messages)
    assert any("holding singleton dispatcher lock" in m for m in messages)
    assert any("embedded in gateway" in m for m in messages)


def test_gateway_refuses_after_lock_filesystem_error_retries_exhausted(
    kanban_home, monkeypatch, caplog,
):
    """AC2/AC4: a persistent filesystem error exhausts the bounded retry and
    the gateway starts no embedded dispatcher (no second dispatcher)."""
    import asyncio
    import logging

    runner, watchers_mod = _make_dispatcher_runner(monkeypatch, running=True)

    monkeypatch.setattr(
        watchers_mod, "_acquire_singleton_lock", lambda _p: (None, "error"),
    )

    dispatch_started = []

    async def unexpected_dispatch(*_args, **_kwargs):
        dispatch_started.append(True)
        raise AssertionError("dispatcher loop must not start without its lock")

    monkeypatch.setattr(watchers_mod.asyncio, "to_thread", unexpected_dispatch)

    with caplog.at_level(logging.ERROR, logger="gateway.run"):
        asyncio.run(
            asyncio.wait_for(runner._kanban_dispatcher_watcher(), timeout=3.0)
        )

    assert dispatch_started == []
    assert runner._kanban_dispatcher_lock_handle is None
    messages = [r.getMessage() for r in caplog.records]
    assert any(
        "filesystem error" in m and "refusing" in m.lower() and "attempts" in m
        for m in messages
    )
