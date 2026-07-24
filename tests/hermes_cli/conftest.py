"""Fixtures shared across hermes_cli kanban tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _close_kanban_db_connections():
    """Close every kanban.db connection a test opens (BUILD-710).

    Tests overwhelmingly reach the DB via ``with kb.connect() as conn:`` (600+
    call sites), but sqlite3's built-in connection context manager only
    commits/rollbacks the transaction — it never closes the file descriptor
    (this is exactly why ``kanban_db.connect_closing`` exists; see #33159). One
    file at a time this is invisible, but across this package's ~9500 tests the
    unclosed ``kanban.db`` + ``kanban.db-wal`` handles accumulate until the
    process hits the fd limit and two threads wedge on the WAL lock, deadlocking
    the whole suite so it can't run as a single merge-gate process.

    Rather than rewrite every call site, wrap the single fd-opening funnel
    ``_sqlite_connect`` to record each connection and close them all at test
    teardown. Idempotent with ``connect_closing`` (double-close is a no-op) and
    safe because the kernel keeps no connection pool — every ``connect()`` opens
    an independent handle.
    """
    import hermes_cli.kanban_db as kdb

    opened: list = []
    real_sqlite_connect = kdb._sqlite_connect

    def _tracking_sqlite_connect(*args, **kwargs):
        conn = real_sqlite_connect(*args, **kwargs)
        opened.append(conn)
        return conn

    kdb._sqlite_connect = _tracking_sqlite_connect
    try:
        yield
    finally:
        kdb._sqlite_connect = real_sqlite_connect
        for conn in opened:
            try:
                conn.close()
            except Exception:
                pass
        opened.clear()


HERMES_MODULE_PREFIXES = ("hermes_cli", "hermes_state", "hermes_constants")


@pytest.fixture
def purged_hermes_modules():
    """Give one test a fresh ``hermes_cli`` import tree, then put the old one back.

    Several kanban fixtures force a fresh import by deleting every
    ``hermes_cli*`` entry from ``sys.modules`` — and never restore them. A later
    test module that bound ``from hermes_cli import kanban_db as kb`` at import
    time then holds a STALE module object while the code under test imports a
    fresh one, so ``monkeypatch.setattr(kb, ...)`` patches a module nobody
    calls. That is how ``test_cli_daemon_capacity_info_*`` ends up running the
    REAL ``run_daemon()`` and hanging the whole process (BUILD-747), and it is a
    source of the wider order-dependent failures in BUILD-632.

    Teardown drops the whole fresh tree (including submodules imported for the
    first time inside the test, which would otherwise leak the split identity in
    the other direction) before reinstating the originals. Scoped to the hermes
    prefixes so it can't resurrect unrelated fakes other tests inject into
    ``sys.modules``.
    """
    import sys

    saved = {
        name: mod for name, mod in sys.modules.items()
        if name.startswith(HERMES_MODULE_PREFIXES)
    }
    for name in saved:
        del sys.modules[name]
    try:
        yield
    finally:
        for name in [
            n for n in sys.modules if n.startswith(HERMES_MODULE_PREFIXES)
        ]:
            del sys.modules[name]
        sys.modules.update(saved)


@pytest.fixture(autouse=True)
def _assignees_dispatchable(request, monkeypatch):
    """Treat every assignee as dispatchable by default (BUILD-661).

    Mirrors the fixture of the same name in ``tests/tools/conftest.py``. The
    create-time assignee guard landed with that stub in the tools package only,
    but the kanban-create tests in THIS package call
    ``kanban_tools._handle_create`` directly with synthetic assignees
    ("worker", "coder") that have no profile directory in the sandbox, so they
    started failing unconditionally — not from ordering. Tests that assert the
    guard itself opt out with ``@pytest.mark.real_assignee_guard``.
    """
    if request.node.get_closest_marker("real_assignee_guard"):
        return
    try:
        from hermes_cli import kanban_db
    except Exception:
        return
    monkeypatch.setattr(
        kanban_db, "assignee_is_dispatchable", lambda _a: True, raising=False
    )


@pytest.fixture(autouse=True)
def _reset_session_context():
    """Clear the session ContextVars a test sets (BUILD-632).

    pytest runs the whole session in ONE ``contextvars.Context``, and
    ``clear_session_vars()`` resets the platform var to ``""`` rather than its
    unset sentinel. An empty-string platform is not "unset", so
    ``get_session_env`` stops falling back to the environment for every later
    test in the process — that is how a kanban channel-affinity test breaks a
    skills-config env-var test three files later.
    """
    try:
        from gateway import session_context
    except Exception:
        yield
        return
    try:
        yield
    finally:
        reset = getattr(session_context, "reset_session_vars", None)
        if callable(reset):
            reset()


@pytest.fixture
def all_assignees_spawnable(monkeypatch):
    """Pretend every assignee maps to a real Hermes profile.

    Most dispatcher tests use synthetic assignees ("alice", "bob") that
    don't correspond to actual profile directories on disk. Without this
    patch, the dispatcher's profile-exists guard (PR #20105) routes
    those tasks into ``skipped_nonspawnable`` instead of spawning, which
    would break tests that assert spawn behavior.
    """
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)


@pytest.fixture(autouse=True)
def _suppress_concurrent_hermes_gate(request, monkeypatch):
    """Default ``_detect_concurrent_hermes_instances`` to ``[]`` for every test.

    The Windows update path now refuses to proceed when another
    ``hermes.exe`` is detected (issue #26670). On a developer's Windows
    machine running the test suite via ``hermes`` itself, this would
    flag the running agent as a concurrent instance and abort every
    ``cmd_update`` test. Tests that want to exercise the gate explicitly
    re-patch ``_detect_concurrent_hermes_instances`` with their own
    return value — autouse here gives a clean default without touching
    the rest of the suite.

    Tests that need to call the REAL function (e.g. unit tests for the
    helper itself) opt out with ``@pytest.mark.real_concurrent_gate``.
    """
    if request.node.get_closest_marker("real_concurrent_gate"):
        return
    try:
        from hermes_cli import main as _cli_main
    except Exception:
        return
    # raising=False: under pytest's per-test spawn isolation, a concurrent
    # xdist worker importing a module that transitively touches hermes_cli.main
    # can briefly expose a partially-initialized module object here — one where
    # _detect_concurrent_hermes_instances isn't defined yet. A bare setattr
    # would raise AttributeError and error the (unrelated) test. The attribute
    # always exists once main.py finishes importing, so a no-op when it's
    # transiently absent is the correct, race-free default.
    monkeypatch.setattr(
        _cli_main,
        "_detect_concurrent_hermes_instances",
        lambda *_a, **_k: [],
        raising=False,
    )
