"""A failed `kanban_create` leaves nothing behind (BUILD-688).

Both live `tools.kanban_tools: kanban_create failed` occurrences in
`profiles/orchestrator/logs/gateway.error.log` came from the DB-corruption
class, not from a create-specific defect:

* one raised ``KanbanDbCorruptError`` from ``connect()``'s health guard, before
  a single write — fail-closed by construction;
* one raised ``sqlite3.DatabaseError: database disk image is malformed`` from
  ``_append_event`` *inside* ``create_task``, i.e. after the task row had
  already been inserted in the same transaction.

The second is the interesting one, and its safety rests entirely on
``create_task`` wrapping ``_insert_task_in_txn`` in a single ``write_txn``.
These tests pin that: an exception raised anywhere inside the create
transaction must leave no task row, no event row, and no workflow row, and must
leave the idempotency key free for a later successful retry.
"""

from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _counts(conn) -> dict[str, int]:
    out = {}
    for table in ("tasks", "task_events", "task_runs"):
        out[table] = conn.execute(f"select count(*) from {table}").fetchone()[0]
    return out


def _fail_in_append_event(monkeypatch, exc: BaseException) -> list[str]:
    """Make the first _append_event of a create blow up, as corruption did."""
    calls: list[str] = []

    def boom(conn, task_id, kind, payload=None, **kwargs):
        calls.append(kind)
        raise exc

    monkeypatch.setattr(kb, "_append_event", boom)
    return calls


class TestCreateFailureLeavesNothingBehind:
    def test_a_mid_create_db_error_rolls_the_whole_row_back(self, kanban_home, monkeypatch):
        conn = kb.connect()
        before = _counts(conn)

        calls = _fail_in_append_event(
            monkeypatch, sqlite3.DatabaseError("database disk image is malformed")
        )
        with pytest.raises(sqlite3.DatabaseError, match="malformed"):
            kb.create_task(conn, title="doomed", assignee="coder")

        # The failure really did land mid-transaction: the task row is inserted
        # before the create event is appended, so this list being non-empty is
        # what makes the rollback assertion below meaningful.
        assert calls == ["created"]
        assert _counts(conn) == before
        assert conn.execute(
            "select count(*) from tasks where title = ?", ("doomed",)
        ).fetchone()[0] == 0

    def test_the_idempotency_key_is_free_after_a_failed_create(self, kanban_home, monkeypatch):
        conn = kb.connect()

        _fail_in_append_event(
            monkeypatch, sqlite3.DatabaseError("database disk image is malformed")
        )
        with pytest.raises(sqlite3.DatabaseError):
            kb.create_task(
                conn, title="doomed", assignee="coder", idempotency_key="build-688"
            )

        monkeypatch.undo()
        task_id = kb.create_task(
            conn, title="retried", assignee="coder", idempotency_key="build-688"
        )
        row = conn.execute("select title from tasks where id = ?", (task_id,)).fetchone()
        assert row["title"] == "retried"
        assert conn.execute("select count(*) from tasks").fetchone()[0] == 1

    def test_retrying_the_same_key_after_success_is_idempotent(self, kanban_home):
        """The observed recovery shape: the caller retries the identical create."""
        conn = kb.connect()
        first = kb.create_task(
            conn, title="once", assignee="coder", idempotency_key="build-688-ok"
        )
        second = kb.create_task(
            conn, title="once", assignee="coder", idempotency_key="build-688-ok"
        )
        assert first == second
        assert conn.execute("select count(*) from tasks").fetchone()[0] == 1
