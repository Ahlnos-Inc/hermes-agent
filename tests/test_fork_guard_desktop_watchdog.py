"""Merge-survival guard (BUILD-190): desktop parent-death watchdog WIRING.

``_watch_parent_death`` (the polling algorithm) is already tested directly in
``tests/test_web_server_parent_watchdog.py``. What is UNGUARDED there is the
INTEGRATION inside ``hermes_cli.web_server._lifespan``:

  - the ``if os.getenv("HERMES_DESKTOP") == "1":`` gate that starts the
    "desktop-parent-watchdog" thread at all;
  - the ``_on_parent_death`` closure wired into that thread, which must call
    ``os.kill(os.getpid(), signal.SIGTERM)``.

A merge could drop this wiring while leaving ``_watch_parent_death`` (and its
green tests) fully intact — silently regressing self-termination of orphaned
desktop backends (BUILD-188/BUILD-190 midnight cron failures). This test
drives the real lifespan via TestClient and inspects the real thread/closure
it creates, rather than re-implementing the wiring.
"""

import os
import signal
import threading

import pytest

from hermes_cli.web_server import _watch_parent_death


def _client():
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")
    from hermes_cli.web_server import app

    return TestClient(app)


def _find_watchdog_thread():
    for t in threading.enumerate():
        if t.name == "desktop-parent-watchdog":
            return t
    return None


def test_watchdog_thread_starts_under_desktop_and_targets_watch_parent_death(
    monkeypatch, _isolate_hermes_home
):
    """HERMES_DESKTOP=1 must start a daemon thread wired to _watch_parent_death."""
    monkeypatch.setenv("HERMES_DESKTOP", "1")

    with _client():
        thread = _find_watchdog_thread()
        assert thread is not None, (
            "expected a 'desktop-parent-watchdog' thread under HERMES_DESKTOP=1 "
            "-- the _lifespan wiring may have been dropped in a merge"
        )
        assert thread.daemon is True
        # Wired to the already-tested polling algorithm, not a copy of it.
        assert thread._target is _watch_parent_death

    # Lifespan shutdown must set the stop event so the thread exits promptly
    # instead of leaking past the TestClient context.
    thread.join(timeout=2.0)
    assert not thread.is_alive()


def test_watchdog_thread_absent_without_desktop_env(monkeypatch, _isolate_hermes_home):
    """Server `hermes dashboard` (no HERMES_DESKTOP) must not start the watchdog."""
    monkeypatch.delenv("HERMES_DESKTOP", raising=False)

    with _client():
        assert _find_watchdog_thread() is None


def test_on_parent_death_closure_sends_sigterm_to_self(monkeypatch, _isolate_hermes_home):
    """The wired on_parent_death callback must self-terminate via SIGTERM.

    Drives the real closure captured by _lifespan (not a re-implementation)
    without waiting for a real reparent -- the polling loop itself is covered
    by tests/test_web_server_parent_watchdog.py.
    """
    monkeypatch.setenv("HERMES_DESKTOP", "1")
    killed = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, sig)))

    with _client():
        thread = _find_watchdog_thread()
        assert thread is not None
        _initial_ppid, on_parent_death, _stop_event = thread._args
        on_parent_death()

    assert killed == [(os.getpid(), signal.SIGTERM)]
