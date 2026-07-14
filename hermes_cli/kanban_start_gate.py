"""Fail-closed start gate for dispatcher-launched Kanban workers."""

from __future__ import annotations

import hmac
import os
import sqlite3
import sys
import time
from pathlib import Path


_GATE_PATH_ENV = "HERMES_KANBAN_START_GATE_PATH"
_GATE_TOKEN_ENV = "HERMES_KANBAN_START_GATE_TOKEN"
_GATE_TIMEOUT_ENV = "HERMES_KANBAN_START_GATE_TIMEOUT_SECONDS"


class KanbanStartGateError(RuntimeError):
    """The worker was not durably attached to its claimed run."""


def _required_env(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise KanbanStartGateError(f"missing required worker field {name}")
    return value


def _wait_for_token(path: Path, expected: str, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            if path.is_symlink():
                raise KanbanStartGateError("worker start gate cannot be a symlink")
            payload = path.read_bytes()
        except FileNotFoundError:
            time.sleep(0.05)
            continue
        except OSError as exc:
            raise KanbanStartGateError("worker start gate could not be read") from exc
        if len(payload) > 256:
            raise KanbanStartGateError("worker start gate payload is invalid")
        if not hmac.compare_digest(payload, expected.encode("utf-8")):
            raise KanbanStartGateError("worker start gate token mismatch")
        return
    raise KanbanStartGateError("worker start gate timed out before durable attach")


def _attest_db_ownership() -> None:
    db_path = Path(_required_env("HERMES_KANBAN_DB")).expanduser().resolve()
    task_id = _required_env("HERMES_KANBAN_TASK")
    claim_lock = _required_env("HERMES_KANBAN_CLAIM_LOCK")
    try:
        run_id = int(_required_env("HERMES_KANBAN_RUN_ID"))
    except ValueError as exc:
        raise KanbanStartGateError("worker run id is invalid") from exc
    pid = os.getpid()
    try:
        conn = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT t.status AS task_status, t.current_run_id, t.claim_lock AS task_lock,
                   t.worker_pid AS task_pid, r.status AS run_status,
                   r.claim_lock AS run_lock, r.worker_pid AS run_pid, r.ended_at
              FROM tasks t
              JOIN task_runs r ON r.id = t.current_run_id
             WHERE t.id = ? AND r.id = ? AND r.task_id = t.id
            """,
            (task_id, run_id),
        ).fetchone()
    except (OSError, sqlite3.Error) as exc:
        raise KanbanStartGateError("worker ownership attestation query failed") from exc
    finally:
        if "conn" in locals():
            conn.close()
    if row is None:
        raise KanbanStartGateError("worker ownership tuple is missing")
    if not (
        row["task_status"] == "running"
        and int(row["current_run_id"]) == run_id
        and row["task_lock"] == claim_lock
        and row["task_pid"] == pid
        and row["run_status"] == "running"
        and row["run_lock"] == claim_lock
        and row["run_pid"] == pid
        and row["ended_at"] is None
    ):
        raise KanbanStartGateError("worker ownership attestation mismatch")


def enforce_kanban_start_gate() -> None:
    """Wait for attach and attest ownership; no-op for non-worker commands."""
    raw_path = (os.environ.get(_GATE_PATH_ENV) or "").strip()
    raw_token = (os.environ.get(_GATE_TOKEN_ENV) or "").strip()
    if not raw_path and not raw_token:
        return
    if not raw_path or not raw_token:
        raise KanbanStartGateError("worker start gate contract is incomplete")
    try:
        timeout_seconds = float(os.environ.get(_GATE_TIMEOUT_ENV, "30"))
    except ValueError as exc:
        raise KanbanStartGateError("worker start gate timeout is invalid") from exc
    if timeout_seconds <= 0 or timeout_seconds > 120:
        raise KanbanStartGateError("worker start gate timeout is outside safety bounds")

    gate_path = Path(raw_path)
    try:
        _wait_for_token(gate_path, raw_token, timeout_seconds)
        _attest_db_ownership()
    finally:
        try:
            gate_path.unlink(missing_ok=True)
        except OSError:
            pass
        os.environ.pop(_GATE_PATH_ENV, None)
        os.environ.pop(_GATE_TOKEN_ENV, None)
        os.environ.pop(_GATE_TIMEOUT_ENV, None)


def enforce_or_exit() -> None:
    """CLI boundary: emit a secret-free diagnostic and stop with EX_TEMPFAIL."""
    try:
        enforce_kanban_start_gate()
    except KanbanStartGateError as exc:
        print(f"Kanban worker start refused: {exc}", file=sys.stderr)
        raise SystemExit(75) from None
