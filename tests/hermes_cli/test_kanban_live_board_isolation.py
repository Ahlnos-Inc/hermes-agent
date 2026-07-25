"""The test suite can never reach the operator's live Kanban board (BUILD-660).

A pytest fixture once created task ``t_a5f0b327`` in the canonical
``~/.hermes/kanban.db`` and ``t_10a9986f`` spawned a fixture PID against a
pytest tempdir that had already been cleaned up — real rows and real worker
events in the operational board, produced by tests. Env isolation alone is a
blocklist; these tests pin the tripwire that catches whatever the blocklist
misses.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kdb

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _live_root(live_hermes_roots: tuple[str, ...]) -> Path:
    return Path(live_hermes_roots[0])


class TestLiveBoardGuard:
    def test_opening_the_canonical_board_is_refused(self, live_hermes_roots):
        """The exact path from the incident: ``<real home>/.hermes/kanban.db``."""
        with pytest.raises(AssertionError) as excinfo:
            kdb._sqlite_connect(_live_root(live_hermes_roots) / "kanban.db")
        message = str(excinfo.value)
        assert "BUILD-660" in message
        assert "LIVE Hermes Kanban board" in message
        assert "HERMES_KANBAN_DB" in message  # actionable: says how to fix it

    def test_opening_a_live_named_board_is_refused(self, live_hermes_roots):
        """Non-default boards live under a different path and are covered too."""
        board = _live_root(live_hermes_roots) / "kanban" / "boards" / "hermes-infra" / "kanban.db"
        with pytest.raises(AssertionError, match="BUILD-660"):
            kdb._sqlite_connect(board)

    def test_a_leaked_env_pin_cannot_route_writes_to_the_live_board(
        self, monkeypatch, live_hermes_roots
    ):
        """Reproduces the leak shape: a fixture re-pins the live DB by hand.

        ``HERMES_KANBAN_DB`` is purged from the environment before every test,
        but a fixture is free to set it again — which is exactly how a test can
        end up writing operational rows. Resolution still points at the live
        board; the guard is what stops the write.
        """
        live = _live_root(live_hermes_roots) / "kanban.db"
        monkeypatch.setenv("HERMES_KANBAN_DB", str(live))
        assert kdb.kanban_db_path() == live
        with pytest.raises(AssertionError, match="BUILD-660"):
            kdb.connect()

    def test_a_test_owned_board_still_opens(self, tmp_path, monkeypatch):
        """The guard must not be a blanket ban on opening a board."""
        monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
        conn = kdb._sqlite_connect(kdb.kanban_db_path())
        try:
            assert conn.execute("select 1").fetchone() == (1,)
        finally:
            conn.close()


class TestBoardPathResolution:
    def test_default_resolution_is_inside_the_per_test_home(self, live_hermes_roots):
        resolved = os.path.realpath(kdb.kanban_db_path())
        assert resolved.startswith(os.path.realpath(os.environ["HERMES_HOME"]) + os.sep)
        assert not resolved.startswith(live_hermes_roots[0] + os.sep)

    def test_named_board_resolution_is_inside_the_per_test_home(self, live_hermes_roots):
        resolved = os.path.realpath(kdb.kanban_db_path("hermes-infra"))
        assert resolved.startswith(os.path.realpath(os.environ["HERMES_HOME"]) + os.sep)
        assert not resolved.startswith(live_hermes_roots[0] + os.sep)

    def test_a_subprocess_resolves_a_test_owned_board(self, live_hermes_roots):
        """Workers are spawned as child processes and inherit env, not fixtures.

        The autouse guard is a monkeypatch and cannot follow them, so the
        HERMES_KANBAN_* purge has to be what holds — assert it does, from a
        real child process launched with this test's environment.
        """
        probe = (
            "import os;"
            "from hermes_cli import kanban_db as k;"
            "print(k.kanban_db_path());"
            "print(k.kanban_db_path('hermes-infra'))"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr
        home = os.path.realpath(os.environ["HERMES_HOME"])
        for line in result.stdout.strip().splitlines():
            resolved = os.path.realpath(line.strip())
            assert resolved.startswith(home + os.sep), resolved
            assert not resolved.startswith(live_hermes_roots[0] + os.sep), resolved
