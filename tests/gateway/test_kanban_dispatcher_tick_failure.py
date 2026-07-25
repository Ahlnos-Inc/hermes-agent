"""A failed dispatcher tick is bounded, attributed, and never lost (BUILD-733).

Live evidence from `profiles/orchestrator/logs/gateway.error.log`: eleven
`kanban dispatcher: tick failed on board hermes-infra` errors between
2026-07-22 22:09 and 2026-07-24 13:24, from three distinct causes —
`sqlite3.OperationalError: disk I/O error`, `OSError: [Errno 28] No space left
on device`, and `sqlite3.DatabaseError: torn-extend detected`. None of those is
the structural-corruption class, so they all land in the generic tick handler
rather than the quarantine path.

The handler's contract is what these tests pin: one board's failure must not
propagate out of the tick, must not stop the other boards on the same tick,
must name the board it happened on, must not quarantine a board that is merely
having an I/O problem, and must leave the next tick free to succeed.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.kanban_watchers import GatewayKanbanWatchersMixin
from hermes_cli import kanban_db as kb
import gateway.kanban_watchers as kw

BOARDS = ["default", "hermes-infra"]

# The exact exceptions observed in the live gateway log, with their counts.
OBSERVED_FAILURES = [
    sqlite3.OperationalError("disk I/O error"),
    sqlite3.OperationalError("unable to open database file"),
    OSError(28, "No space left on device"),
    sqlite3.DatabaseError(
        "torn-extend detected: page count mismatch on kanban.db: "
        "header claims 5899 pages, file has 5898 pages"
    ),
]


class _Adapter:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text})
        return None

    def extract_local_files(self, _text):
        return [], None


class _Watcher(GatewayKanbanWatchersMixin):
    def __init__(self, adapter):
        self._running = True
        self.adapters = {Platform.TELEGRAM: adapter}
        self._kanban_sub_fail_counts = {}
        self.config = SimpleNamespace(get_home_channel=lambda _platform: None)

    def _active_profile_name(self):
        return "default"

    def _authorization_adapter(self, _platform, _profile=None):
        return next(iter(self.adapters.values()))

    async def _kanban_notify_home_fallback(self, message):
        return True


@pytest.fixture
def dispatcher(tmp_path, monkeypatch):
    """A dispatcher watcher over two boards, driven tick by tick."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()

    import hermes_cli.config as config_mod

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
        kb, "list_boards", lambda include_archived=False: [{"slug": s} for s in BOARDS]
    )
    monkeypatch.setattr(kb, "read_board_metadata", lambda slug: {"slug": slug})
    monkeypatch.setattr(kb, "reap_worker_zombies", lambda: [])

    runner = _Watcher(_Adapter())

    def run(dispatch_once, *, ticks=2):
        """Patch dispatch_once, run `ticks` ticks, return the runner."""
        monkeypatch.setattr(kb, "dispatch_once", dispatch_once)
        state = {"ticks": 0}

        async def fake_to_thread(fn, *args, **kwargs):
            result = fn(*args, **kwargs)
            if getattr(fn, "__name__", "") == "_tick_once":
                state["ticks"] += 1
                if state["ticks"] >= ticks:
                    runner._running = False
            return result

        async def fake_sleep(_delay):
            return None

        monkeypatch.setattr(kw.asyncio, "to_thread", fake_to_thread)
        monkeypatch.setattr(kw.asyncio, "sleep", fake_sleep)
        asyncio.run(runner._kanban_dispatcher_watcher())
        return state["ticks"]

    return SimpleNamespace(runner=runner, run=run)


@pytest.mark.parametrize(
    "failure", OBSERVED_FAILURES, ids=lambda e: type(e).__name__ + ":" + str(e)[:24]
)
def test_a_failing_board_does_not_take_down_the_tick(dispatcher, caplog, failure):
    """Each observed cause: logged with its board, other board still dispatched."""
    calls: list[tuple[int, str]] = []
    tick = {"n": 0}

    def dispatch_once(_conn, **kwargs):
        board = kwargs["board"]
        if board == "default":
            tick["n"] += 1  # 'default' is enumerated first each tick
        calls.append((tick["n"], board))
        if board == "hermes-infra" and tick["n"] == 1:
            raise failure
        return None

    with caplog.at_level(logging.ERROR, logger="gateway.run"):
        assert dispatcher.run(dispatch_once, ticks=2) == 2

    # Not lost: the failing board is retried on the next tick and succeeds.
    assert calls == [
        (1, "default"),
        (1, "hermes-infra"),
        (2, "default"),
        (2, "hermes-infra"),
    ]

    # Attributed: the error names the board it happened on.
    errors = [r for r in caplog.records if "tick failed" in r.getMessage()]
    assert len(errors) == 1
    assert "hermes-infra" in errors[0].getMessage()
    assert errors[0].exc_info is not None  # traceback retained, not swallowed


def test_an_io_failure_does_not_quarantine_the_board(dispatcher, caplog):
    """`disk I/O error` is not the structural-corruption class.

    Quarantining a board that merely hit a full disk would stop dispatch for
    every task on it until a human intervened; the corruption path is reserved
    for `file is not a database` / `database disk image is malformed`.
    """
    attempts: list[str] = []

    def dispatch_once(_conn, **kwargs):
        attempts.append(kwargs["board"])
        if kwargs["board"] == "hermes-infra":
            raise sqlite3.OperationalError("disk I/O error")
        return None

    with caplog.at_level(logging.ERROR, logger="gateway.run"):
        dispatcher.run(dispatch_once, ticks=3)

    # Still attempted on every tick — a quarantined board would stop being
    # dispatched, and the corruption path logs its own message, not this one.
    assert attempts == BOARDS * 3
    messages = [r.getMessage() for r in caplog.records]
    assert all("tick failed on board hermes-infra" in m for m in messages)
    assert len(messages) == 3


def test_a_healthy_run_logs_no_tick_failure(dispatcher, caplog):
    """Negative control: the assertions above are not vacuous."""
    seen: list[str] = []

    def dispatch_once(_conn, **kwargs):
        seen.append(kwargs["board"])
        return None

    with caplog.at_level(logging.ERROR, logger="gateway.run"):
        dispatcher.run(dispatch_once, ticks=1)

    assert seen == BOARDS
    assert [r for r in caplog.records if "tick failed" in r.getMessage()] == []
