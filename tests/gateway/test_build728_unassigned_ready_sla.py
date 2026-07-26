"""BUILD-728: SLA alert for ready kanban tasks that have no assignee.

Since fb8a7a675 the dispatcher-stuck detector correctly treats
``unassigned``/``nonspawnable`` as benign routing steady-states, so it no
longer pages for them. But a ready card with NO assignee can never spawn a
worker either — it just sits silently. These tests pin the narrow nudge that
covers that hole and, just as importantly, that it stays quiet for the two
states that ARE the intended steady condition.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

import gateway.kanban_watchers as kw
from gateway.config import Platform
from gateway.kanban_watchers import GatewayKanbanWatchersMixin
from hermes_cli import kanban_db as kb

BOARDS = ["default"]


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
        self.alerts: list[str] = []

    def _active_profile_name(self):
        return "default"

    def _authorization_adapter(self, _platform, _profile=None):
        return next(iter(self.adapters.values()))

    async def _kanban_notify_home_fallback(self, message):
        self.alerts.append(message)
        return True


def _result(**kwargs):
    """A DispatchResult-shaped stand-in carrying only the skip lists."""
    base = {
        "spawned": [],
        "skipped_unassigned": [],
        "skipped_nonspawnable": [],
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


@pytest.fixture
def dispatcher(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()

    import hermes_cli.config as config_mod

    monkeypatch.setattr(
        config_mod, "load_config",
        lambda: {"kanban": {
            "dispatch_in_gateway": True,
            "dispatch_interval_seconds": 1,
            "auto_decompose": False,
        }},
    )
    monkeypatch.setattr(
        kb, "list_boards", lambda include_archived=False: [{"slug": s} for s in BOARDS]
    )
    monkeypatch.setattr(kb, "read_board_metadata", lambda slug: {"slug": slug})
    monkeypatch.setattr(kb, "reap_worker_zombies", lambda: [])

    runner = _Watcher(_Adapter())

    def seed(*, assignee=None, age_seconds=7200, status="ready"):
        """One task on the board, aged by rewriting its timestamps."""
        with kb.connect(board="default") as conn:
            tid = kb.create_task(conn, title="orphan card", assignee=assignee or "coder")
            old = int(time.time()) - int(age_seconds)
            conn.execute(
                "UPDATE tasks SET status=?, created_at=?, assignee=? WHERE id=?",
                (status, old, assignee, tid),
            )
            conn.execute(
                "UPDATE task_events SET created_at=? WHERE task_id=?", (old, tid),
            )
            conn.commit()
        return tid

    def run(result, *, ticks=1):
        monkeypatch.setattr(
            kb, "dispatch_once", lambda _conn, **kwargs: result,
        )
        state = {"ticks": 0}

        async def fake_to_thread(fn, *args, **kwargs):
            out = fn(*args, **kwargs)
            if getattr(fn, "__name__", "") == "_tick_once":
                state["ticks"] += 1
                if state["ticks"] >= ticks:
                    runner._running = False
            return out

        monkeypatch.setattr(kw.asyncio, "to_thread", fake_to_thread)
        async def fake_sleep(_delay):
            return None

        monkeypatch.setattr(kw.asyncio, "sleep", fake_sleep)
        asyncio.run(runner._kanban_dispatcher_watcher())
        return runner.alerts

    return SimpleNamespace(runner=runner, run=run, seed=seed)


def test_aged_unassigned_ready_task_alerts_exactly_once_per_window(dispatcher):
    tid = dispatcher.seed(assignee=None, age_seconds=7200)

    alerts = dispatcher.run(_result(skipped_unassigned=[tid]), ticks=3)

    assert len(alerts) == 1, f"expected one alert across three ticks, got {alerts}"
    assert tid in alerts[0]
    assert "UNASSIGNED" in alerts[0]


def test_fresh_unassigned_task_is_below_the_sla_window(dispatcher):
    tid = dispatcher.seed(assignee=None, age_seconds=60)

    assert dispatcher.run(_result(skipped_unassigned=[tid]), ticks=2) == []


def test_human_assigned_nonspawnable_task_never_alerts(dispatcher):
    """``nonspawnable`` is the intended steady state — it must stay silent."""
    tid = dispatcher.seed(assignee="nicholas", age_seconds=7 * 86400)

    assert dispatcher.run(_result(skipped_nonspawnable=[tid]), ticks=2) == []


def test_task_assigned_to_a_real_profile_never_alerts(dispatcher):
    """Normal work in flight is not a routing slip."""
    tid = dispatcher.seed(assignee="coder", age_seconds=7 * 86400)

    assert dispatcher.run(_result(skipped_unassigned=[tid]), ticks=2) == [], (
        "an assigned task must not alert even if a caller mis-buckets it"
    )


def test_idle_fleet_does_no_scan_work(dispatcher):
    """Nothing skipped as unassigned → no board reads, no alerts."""
    dispatcher.seed(assignee=None, age_seconds=7200)

    assert dispatcher.run(_result(), ticks=2) == []


# ── the query itself ────────────────────────────────────────────────────────


def test_age_is_measured_from_the_last_event_not_creation(tmp_path, monkeypatch):
    """A card unblocked a minute ago is one minute idle, not one week."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()

    now = int(time.time())
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="reopened", assignee="coder")
        conn.execute(
            "UPDATE tasks SET status='ready', created_at=?, assignee=NULL WHERE id=?",
            (now - 7 * 86400, tid),
        )
        conn.execute(
            "UPDATE task_events SET created_at=? WHERE task_id=?",
            (now - 30, tid),
        )
        conn.commit()

        assert kb.unassigned_ready_over_sla(conn, [tid]) == []

        # Age the last event past the window and it surfaces.
        conn.execute(
            "UPDATE task_events SET created_at=? WHERE task_id=?",
            (now - 3600, tid),
        )
        conn.commit()
        over = kb.unassigned_ready_over_sla(conn, [tid])
        assert [t[0] for t in over] == [tid]
        assert over[0][2] >= 3600
