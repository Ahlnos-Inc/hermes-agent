"""Behavioral contracts for the coder-to-releaser publication handoff."""

from __future__ import annotations

import argparse
import json
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import worker_credentials as wc


EXPECTED_SHA = "a" * 40


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    wc.reset_worker_credential_context_for_tests()
    home = tmp_path / ".hermes"
    home.mkdir()
    db = home / "kanban.db"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kb.time, "time", lambda: 1_700_000_000)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db(db)
    yield db
    wc.reset_worker_credential_context_for_tests()


def _create_requester(conn) -> str:
    return kb.create_task(conn, title="coder work", assignee="coder")


def _claim(conn, task_id: str) -> int:
    claimed = kb.claim_task(conn, task_id, claimer="coder-worker")
    assert claimed is not None and claimed.current_run_id is not None
    return int(claimed.current_run_id)


def _bootstrap_publication_worker(
    board: Path,
    task_id: str,
    run_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = board.parent
    (home / wc.MANIFEST_FILENAME).write_text(
        "version: 1\nprofiles:\n  releaser:\n    actions: []\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_PROFILE", "releaser")
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(board))
    monkeypatch.setenv(wc.MANIFEST_DIGEST_ENV, wc.load_manifest(home).digest)
    runtime = wc.bootstrap_worker_credential_context()
    assert runtime is not None and runtime.manifest_verified


# Candidate git binaries in preference order — same fallback chain as
# _find_git_binary() in kanban_db.py.
_GIT_BINS = ("/opt/homebrew/bin/git", "/usr/local/bin/git", "/usr/bin/git", "git")


def _find_git() -> str:
    """Return the first accessible non-stub git binary."""
    import os as _os
    for c in _GIT_BINS:
        if _os.access(c, _os.X_OK):
            return c
    import shutil as _shutil
    return _shutil.which("git") or "git"


def _git_exec_path(git: str) -> str | None:
    """Return the git exec-path via Cellar discovery (no subprocess.run)."""
    import os as _os
    try:
        with _os.popen(
            f'GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0 "{git}" --version 2>/dev/null'
        ) as p:
            ver = p.read().split("git version ")[-1].strip()
        for prefix in ("/opt/homebrew", "/usr/local"):
            cellar = f"{prefix}/Cellar/git/{ver}/libexec/git-core"
            try:
                if "git-upload-pack" in _os.listdir(cellar):
                    return cellar
            except (PermissionError, FileNotFoundError):
                pass
    except Exception:
        pass
    return None


def _git_env() -> dict:
    """Build env for hermetic git calls: correct exec-path, no system config."""
    import os as _os
    git = _find_git()
    env = {**_os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
    ep = _git_exec_path(git)
    if ep:
        env["GIT_EXEC_PATH"] = ep
    return env


def _git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        [_find_git(), *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env=_git_env(),
    )
    return result.stdout.strip()


def _prepare_claimed_publication(
    conn,
    workspace: Path,
    *,
    request_key: str,
) -> tuple[str, str, int]:
    requester = _create_requester(conn)
    handoff = kb.request_publication_handoff(
        conn,
        requester,
        publication=kb.NewPublicationTask(
            expected_sha=EXPECTED_SHA,
            workspace_path=str(workspace),
            remote_ref="refs/heads/main",
        ),
        request_key=request_key,
        actor="coder",
        expected_run_id=_claim(conn, requester),
    )
    publisher = kb.claim_task(
        conn,
        handoff.publication_task_id,
        claimer="releaser-worker",
    )
    assert publisher is not None and publisher.current_run_id is not None
    return requester, handoff.publication_task_id, int(publisher.current_run_id)


def test_publication_handoff_creates_releaser_parent_and_parks_requester(
    board: Path, tmp_path: Path,
) -> None:
    workspace = tmp_path / "coder-workspace"
    workspace.mkdir()
    with kb.connect_closing(board) as conn:
        requester = _create_requester(conn)
        run_id = _claim(conn, requester)

        result = kb.request_publication_handoff(
            conn,
            requester,
            publication=kb.NewPublicationTask(
                expected_sha=EXPECTED_SHA,
                workspace_path=str(workspace),
                remote_ref="refs/heads/main",
            ),
            request_key="publish-request-1",
            actor="coder",
            expected_run_id=run_id,
        )

        assert result.publication_action == "created"
        assert result.requester_status == "todo"
        publisher = kb.get_task(conn, result.publication_task_id)
        assert publisher is not None
        assert publisher.assignee == "releaser"
        assert publisher.workspace_path == str(workspace)
        assert conn.execute(
            "SELECT 1 FROM task_links WHERE parent_id = ? AND child_id = ?",
            (result.publication_task_id, requester),
        ).fetchone() is not None
        requester_row = kb.get_task(conn, requester)
        assert requester_row is not None and requester_row.status == "todo"
        closed = conn.execute(
            "SELECT outcome, ended_at FROM task_runs WHERE id = ?", (run_id,)
        ).fetchone()
        assert closed is not None
        assert closed["outcome"] == "publication_handoff_requested"
        assert closed["ended_at"] is not None
        requester_event = next(
            event for event in kb.list_events(conn, requester)
            if event.kind == "publication_handoff_requested"
        )
        publisher_event = next(
            event for event in kb.list_events(conn, result.publication_task_id)
            if event.kind == "publication_handoff_for"
        )
        assert requester_event.payload == publisher_event.payload
        assert requester_event.payload["request_key"] == "publish-request-1"
        assert requester_event.payload["idempotency_key"] == "publish-request-1"


def test_publication_completion_fails_closed_without_remote_readback(
    board: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "coder-workspace"
    remote = tmp_path / "remote.git"
    _git("init", "--bare", str(remote))
    workspace.mkdir()
    _git("init", "-b", "main", cwd=workspace)
    _git("config", "user.email", "test@example.invalid", cwd=workspace)
    _git("config", "user.name", "Hermes Test", cwd=workspace)
    (workspace / "change.txt").write_text("committed\n", encoding="utf-8")
    _git("add", "change.txt", cwd=workspace)
    _git("commit", "-m", "commit for publication", cwd=workspace)
    expected_sha = _git("rev-parse", "HEAD", cwd=workspace)
    _git("remote", "add", "origin", str(remote), cwd=workspace)

    with kb.connect_closing(board) as conn:
        requester = _create_requester(conn)
        run_id = _claim(conn, requester)
        handoff = kb.request_publication_handoff(
            conn,
            requester,
            publication=kb.NewPublicationTask(
                expected_sha=expected_sha,
                workspace_path=str(workspace),
                remote_ref="refs/heads/main",
            ),
            request_key="publish-readback-missing",
            actor="coder",
            expected_run_id=run_id,
        )
        publisher = kb.get_task(conn, handoff.publication_task_id)
        assert publisher is not None
        publisher = kb.claim_task(conn, publisher.id, claimer="releaser-worker")
        assert publisher is not None and publisher.current_run_id is not None
        _bootstrap_publication_worker(
            board,
            publisher.id,
            int(publisher.current_run_id),
            monkeypatch,
        )

        assert not kb.complete_task(
            conn,
            handoff.publication_task_id,
            result="push command reported success",
            metadata={"publication_verified": True},
            expected_run_id=publisher.current_run_id,
        )
        still_open = kb.get_task(conn, handoff.publication_task_id)
        assert still_open is not None
        assert still_open.status == "running"
        assert still_open.current_run_id == publisher.current_run_id
        requester_waiting = kb.get_task(conn, requester)
        assert requester_waiting is not None and requester_waiting.status == "todo"
        blocked = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? "
            "AND kind = 'completion_blocked' ORDER BY id DESC LIMIT 1",
            (handoff.publication_task_id,),
        ).fetchone()
        assert blocked is not None
        assert "publication" in (blocked["payload"] or "")

        (workspace / "wrong.txt").write_text("wrong target\n", encoding="utf-8")
        _git("add", "wrong.txt", cwd=workspace)
        _git("commit", "-m", "wrong publication target", cwd=workspace)
        _git("push", "origin", "HEAD:refs/heads/main", cwd=workspace)
        assert not kb.complete_task(
            conn,
            handoff.publication_task_id,
            result="the ref moved, but not to the recorded SHA",
            expected_run_id=publisher.current_run_id,
        )
        mismatch = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? "
            "AND kind = 'completion_blocked' ORDER BY id DESC LIMIT 1",
            (handoff.publication_task_id,),
        ).fetchone()
        assert mismatch is not None
        mismatch_payload = json.loads(mismatch["payload"])
        assert mismatch_payload["reason"] == "publication_ref_not_verified"
        assert mismatch_payload["observed_sha"] != expected_sha


def test_publication_completion_succeeds_after_remote_ref_readback(
    board: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "coder-workspace"
    remote = tmp_path / "remote.git"
    _git("init", "--bare", str(remote))
    workspace.mkdir()
    _git("init", "-b", "main", cwd=workspace)
    _git("config", "user.email", "test@example.invalid", cwd=workspace)
    _git("config", "user.name", "Hermes Test", cwd=workspace)
    (workspace / "change.txt").write_text("committed\n", encoding="utf-8")
    _git("add", "change.txt", cwd=workspace)
    _git("commit", "-m", "commit for publication", cwd=workspace)
    expected_sha = _git("rev-parse", "HEAD", cwd=workspace)
    _git("remote", "add", "origin", str(remote), cwd=workspace)

    with kb.connect_closing(board) as conn:
        requester = _create_requester(conn)
        handoff = kb.request_publication_handoff(
            conn,
            requester,
            publication=kb.NewPublicationTask(
                expected_sha=expected_sha,
                workspace_path=str(workspace),
                remote_ref="refs/heads/main",
            ),
            request_key="publish-readback-success",
            actor="coder",
            expected_run_id=_claim(conn, requester),
        )
        publisher = kb.claim_task(
            conn, handoff.publication_task_id, claimer="releaser-worker",
        )
        assert publisher is not None and publisher.current_run_id is not None
        _bootstrap_publication_worker(
            board,
            handoff.publication_task_id,
            int(publisher.current_run_id),
            monkeypatch,
        )
        waiting = kb.get_task(conn, requester)
        assert waiting is not None and waiting.status == "todo"
        _git("push", "origin", "HEAD:refs/heads/main", cwd=workspace)

        assert kb.complete_task(
            conn,
            handoff.publication_task_id,
            summary="remote ref read back at the intended commit",
            expected_run_id=publisher.current_run_id,
        )
        publication = kb.get_task(conn, handoff.publication_task_id)
        requester_after = kb.get_task(conn, requester)
        assert publication is not None and publication.status == "done"
        assert requester_after is not None and requester_after.status == "ready"
        completed = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? "
            "AND kind = 'completed' ORDER BY id DESC LIMIT 1",
            (handoff.publication_task_id,),
        ).fetchone()
        assert completed is not None
        payload = json.loads(completed["payload"])
        assert payload["publication_readback"]["verified"] is True
        assert payload["publication_readback"]["observed_sha"] == expected_sha


def test_publication_completion_readback_does_not_hold_write_lock(
    board: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second connection can commit while the remote readback is blocked."""
    monkeypatch.setenv("HERMES_KANBAN_BUSY_TIMEOUT_MS", "50")
    workspace = tmp_path / "coder-workspace"
    workspace.mkdir()

    with kb.connect_closing(board) as conn:
        requester, publication_id, run_id = _prepare_claimed_publication(
            conn,
            workspace,
            request_key="publish-lock-free-success",
        )
        writer_task = kb.create_task(
            conn,
            title="task the concurrent writer updates",
            assignee="notifier",
        )
        writer_finished = threading.Event()
        writer_errors: list[BaseException] = []

        def writer() -> None:
            try:
                with kb.connect_closing(board) as writer_conn:
                    with kb.write_txn(writer_conn):
                        writer_conn.execute(
                            "UPDATE tasks SET title = ? WHERE id = ?",
                            ("updated during readback", writer_task),
                        )
            except BaseException as exc:
                writer_errors.append(exc)
            finally:
                writer_finished.set()

        writer_thread = threading.Thread(target=writer, daemon=True)

        def fake_readback(contract, **kwargs):
            assert not conn.in_transaction, "readback unexpectedly owns a DB transaction"
            writer_thread.start()
            assert writer_finished.wait(timeout=3), (
                "second connection could not write during remote readback"
            )
            return {"verified": True, "observed_sha": EXPECTED_SHA, "reason": None}

        monkeypatch.setattr(wc, "trusted_publication_readback", fake_readback)
        try:
            assert kb.complete_task(
                conn,
                publication_id,
                result="published",
                expected_run_id=run_id,
            )
        finally:
            writer_thread.join(timeout=3)

        assert not writer_thread.is_alive()
        assert writer_errors == []
        updated_writer_task = kb.get_task(conn, writer_task)
        assert updated_writer_task is not None
        assert updated_writer_task.title == "updated during readback"
        completed_publication = kb.get_task(conn, publication_id)
        assert completed_publication is not None
        assert completed_publication.status == "done"
        waiting_requester = kb.get_task(conn, requester)
        assert waiting_requester is not None and waiting_requester.status == "ready"


def test_publication_completion_rejects_contract_changed_after_readback(
    board: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A verified ref cannot complete after another writer changes its contract."""
    monkeypatch.setenv("HERMES_KANBAN_BUSY_TIMEOUT_MS", "50")
    workspace = tmp_path / "coder-workspace"
    workspace.mkdir()

    with kb.connect_closing(board) as conn:
        requester, publication_id, run_id = _prepare_claimed_publication(
            conn,
            workspace,
            request_key="publish-stale-contract",
        )
        readback_finished = threading.Event()
        mutation_finished = threading.Event()
        mutation_errors: list[BaseException] = []

        def mutate_contract() -> None:
            assert readback_finished.wait(timeout=3)
            try:
                with kb.connect_closing(board) as mutator_conn:
                    with kb.write_txn(mutator_conn):
                        mutator_conn.execute(
                            "UPDATE tasks SET publication_ref = ? WHERE id = ?",
                            ("refs/heads/release", publication_id),
                        )
            except BaseException as exc:
                mutation_errors.append(exc)
            finally:
                mutation_finished.set()

        mutation_thread = threading.Thread(target=mutate_contract, daemon=True)
        mutation_thread.start()
        def readback_then_wait(contract, **kwargs):
            verification = {
                "verified": True,
                "observed_sha": EXPECTED_SHA,
                "reason": None,
            }
            # The underlying git readback has returned. Let a second writer
            # mutate the contract before this function hands verification back
            # to complete_task, which is the stale-read window under test.
            readback_finished.set()
            assert mutation_finished.wait(timeout=3)
            return verification

        monkeypatch.setattr(kb, "_read_publication_remote_ref", readback_then_wait)
        try:
            assert not kb.complete_task(
                conn,
                publication_id,
                result="must not complete stale verification",
                expected_run_id=run_id,
            )
        finally:
            mutation_thread.join(timeout=3)

        assert not mutation_thread.is_alive()
        assert mutation_errors == []
        publication = kb.get_task(conn, publication_id)
        assert publication is not None
        assert publication.status == "running"
        assert publication.current_run_id == run_id
        assert publication.publication_ref == "refs/heads/release"
        requester_after = kb.get_task(conn, requester)
        assert requester_after is not None and requester_after.status == "todo"
        blocked = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? "
            "AND kind = 'completion_blocked' ORDER BY id DESC LIMIT 1",
            (publication_id,),
        ).fetchone()
        assert blocked is not None
        payload = json.loads(blocked["payload"])
        assert payload["reason"] == "publication_contract_changed_during_readback"
        assert payload["verified_contract"]["ref"] == "refs/heads/main"
        assert payload["current_contract"]["ref"] == "refs/heads/release"


@pytest.mark.parametrize("failure_mode", ["unreachable", "timeout"])
def test_publication_readback_failure_is_lock_free_and_leaves_board_open(
    board: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
) -> None:
    """Readback failures do not hold the board lock or close the run."""
    monkeypatch.setenv("HERMES_KANBAN_BUSY_TIMEOUT_MS", "50")
    workspace = tmp_path / "coder-workspace"
    workspace.mkdir()

    with kb.connect_closing(board) as conn:
        requester, publication_id, run_id = _prepare_claimed_publication(
            conn,
            workspace,
            request_key=f"publish-readback-{failure_mode}",
        )
        writer_task = kb.create_task(
            conn,
            title="task the concurrent writer updates",
            assignee="notifier",
        )
        writer_finished = threading.Event()
        writer_errors: list[BaseException] = []

        def writer() -> None:
            try:
                with kb.connect_closing(board) as writer_conn:
                    with kb.write_txn(writer_conn):
                        writer_conn.execute(
                            "UPDATE tasks SET title = ? WHERE id = ?",
                            (f"writer survived {failure_mode}", writer_task),
                        )
            except BaseException as exc:
                writer_errors.append(exc)
            finally:
                writer_finished.set()

        writer_thread = threading.Thread(target=writer, daemon=True)

        def fake_readback(contract, **kwargs):
            assert not conn.in_transaction, "readback unexpectedly owns a DB transaction"
            writer_thread.start()
            assert writer_finished.wait(timeout=3), (
                "second connection could not write during failed readback"
            )
            return {
                "verified": False,
                "observed_sha": None,
                "reason": "timeout" if failure_mode == "timeout" else "transport",
            }

        monkeypatch.setattr(wc, "trusted_publication_readback", fake_readback)
        before = kb.get_task(conn, publication_id)
        assert before is not None
        try:
            assert not kb.complete_task(
                conn,
                publication_id,
                result="readback failed",
                expected_run_id=run_id,
            )
        finally:
            writer_thread.join(timeout=3)

        assert not writer_thread.is_alive()
        assert writer_errors == []
        after = kb.get_task(conn, publication_id)
        assert after is not None
        assert after.status == before.status == "running"
        assert after.current_run_id == before.current_run_id == run_id
        assert after.result == before.result is None
        assert after.completed_at == before.completed_at is None
        updated_writer_task = kb.get_task(conn, writer_task)
        assert updated_writer_task is not None
        assert updated_writer_task.title == f"writer survived {failure_mode}"
        completed_count = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'completed'",
            (publication_id,),
        ).fetchone()[0]
        assert completed_count == 0
        blocked = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? "
            "AND kind = 'completion_blocked' ORDER BY id DESC LIMIT 1",
            (publication_id,),
        ).fetchone()
        assert blocked is not None
        payload = json.loads(blocked["payload"])
        assert payload["reason"] == "publication_ref_not_verified"
        if failure_mode == "timeout":
            assert payload["readback_reason"] == "timeout"


def test_publication_failure_event_never_persists_untrusted_readback_details(
    board: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "coder-workspace"
    workspace.mkdir()
    sentinel = "sentinel-publication-secret"

    with kb.connect_closing(board) as conn:
        _requester, publication_id, run_id = _prepare_claimed_publication(
            conn,
            workspace,
            request_key="publish-secret-safe-event",
        )
        monkeypatch.setattr(
            wc,
            "trusted_publication_readback",
            lambda *_args, **_kwargs: {
                "verified": False,
                "observed_sha": sentinel,
                "reason": f"remote exception: {sentinel}",
                "stderr": sentinel,
                "remote_url": f"https://{sentinel}@example.invalid/repo.git",
            },
        )

        assert not kb.complete_task(
            conn,
            publication_id,
            result="must remain blocked",
            expected_run_id=run_id,
        )
        blocked = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? "
            "AND kind = 'completion_blocked' ORDER BY id DESC LIMIT 1",
            (publication_id,),
        ).fetchone()
        assert blocked is not None
        payload_text = blocked["payload"]
        assert sentinel not in payload_text
        payload = json.loads(payload_text)
        assert payload["readback_reason"] == "transport"
        assert set(payload) == {
            "verified",
            "observed_sha",
            "reason",
            "readback_reason",
            "expected_sha",
            "remote",
            "remote_ref",
        }


def test_publication_handoff_adopts_existing_publication_card(board: Path, tmp_path: Path) -> None:
    workspace = tmp_path / "coder-workspace"
    workspace.mkdir()
    with kb.connect_closing(board) as conn:
        requester = _create_requester(conn)
        publication = kb.create_task(
            conn,
            title="existing publication card",
            assignee="releaser",
            workspace_kind="dir",
            workspace_path=str(workspace),
            publication_expected_sha=EXPECTED_SHA,
            publication_remote="origin",
            publication_ref="refs/heads/main",
        )

        result = kb.request_publication_handoff(
            conn,
            requester,
            publication=kb.ExistingPublicationTask(publication),
            request_key="publish-adopt-1",
            actor="orchestrator",
            require_no_active_run=True,
        )

        assert result.publication_action == "adopted"
        assert result.publication_task_id == publication
        assert result.requester_status == "todo"
        assert conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE publication_expected_sha IS NOT NULL"
        ).fetchone()[0] == 1


def test_publication_handoff_replay_has_no_duplicate_card_edge_or_event(
    board: Path, tmp_path: Path,
) -> None:
    workspace = tmp_path / "coder-workspace"
    workspace.mkdir()
    with kb.connect_closing(board) as conn:
        requester = _create_requester(conn)
        first = kb.request_publication_handoff(
            conn,
            requester,
            publication=kb.NewPublicationTask(
                expected_sha=EXPECTED_SHA,
                workspace_path=str(workspace),
                remote_ref="refs/heads/main",
            ),
            request_key="publish-replay-1",
            actor="orchestrator",
            require_no_active_run=True,
        )
        second = kb.request_publication_handoff(
            conn,
            requester,
            publication=kb.NewPublicationTask(
                expected_sha=EXPECTED_SHA,
                workspace_path=str(workspace),
                remote_ref="refs/heads/main",
            ),
            request_key="publish-replay-1",
            actor="orchestrator",
            require_no_active_run=True,
        )

        assert first.publication_action == "created"
        assert second.publication_action == "replayed"
        assert second.request_event_id == first.request_event_id
        assert conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE publication_expected_sha IS NOT NULL"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM task_links WHERE parent_id = ? AND child_id = ?",
            (first.publication_task_id, requester),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? "
            "AND kind = 'publication_handoff_requested'",
            (requester,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? "
            "AND kind = 'publication_handoff_for'",
            (first.publication_task_id,),
        ).fetchone()[0] == 1


def test_publication_handoff_rolls_back_card_edge_run_and_events(
    board: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "coder-workspace"
    workspace.mkdir()
    with kb.connect_closing(board) as conn:
        requester = _create_requester(conn)
        run_id = _claim(conn, requester)
        before_tasks = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        before_events = conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]

        def fail_link(*args, **kwargs):
            raise RuntimeError("injected publication link failure")

        monkeypatch.setattr(kb, "_link_tasks_in_txn", fail_link)
        with pytest.raises(RuntimeError, match="injected publication link failure"):
            kb.request_publication_handoff(
                conn,
                requester,
                publication=kb.NewPublicationTask(
                    expected_sha=EXPECTED_SHA,
                    workspace_path=str(workspace),
                    remote_ref="refs/heads/main",
                ),
                request_key="publish-rollback-1",
                actor="coder",
                expected_run_id=run_id,
            )

        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == before_tasks
        assert conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == before_events
        assert conn.execute("SELECT COUNT(*) FROM task_links").fetchone()[0] == 0
        requester_row = kb.get_task(conn, requester)
        assert requester_row is not None
        assert requester_row.status == "running"
        assert requester_row.current_run_id == run_id
        run = conn.execute(
            "SELECT ended_at, outcome FROM task_runs WHERE id = ?", (run_id,)
        ).fetchone()
        assert run is not None and run["ended_at"] is None and run["outcome"] is None


def test_capability_block_remains_a_real_block_not_a_publication_handoff(
    board: Path,
) -> None:
    with kb.connect_closing(board) as conn:
        requester = _create_requester(conn)
        run_id = _claim(conn, requester)

        assert kb.block_task(
            conn,
            requester,
            reason="no lane can provide the required signing capability",
            kind="capability",
            expected_run_id=run_id,
        )
        task = kb.get_task(conn, requester)
        assert task is not None
        assert task.status == "blocked"
        assert task.block_kind == "capability"
        assert conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE publication_expected_sha IS NOT NULL"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? "
            "AND kind LIKE 'publication_handoff_%'",
            (requester,),
        ).fetchone()[0] == 0


def test_cli_publication_handoff_supports_dry_run_and_json(
    board: Path, tmp_path: Path, capsys,
) -> None:
    from hermes_cli import kanban as kanban_cli

    workspace = tmp_path / "coder-workspace"
    workspace.mkdir()
    with kb.connect_closing(board) as conn:
        requester = _create_requester(conn)

    root = argparse.ArgumentParser()
    subparsers = root.add_subparsers(dest="command")
    kanban_cli.build_parser(subparsers)
    argv = [
        "kanban", "publish", requester,
        "--sha", EXPECTED_SHA,
        "--workspace", f"dir:{workspace}",
        "--remote-ref", "refs/heads/main",
        "--request-key", "cli-publication-1",
        "--json",
    ]
    args = root.parse_args([*argv, "--dry-run"])
    assert kanban_cli._cmd_publish(args) == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry["dry_run"] is True
    assert dry["publication_action"] == "created"
    with kb.connect_closing(board) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE kind = 'publication_handoff_requested'"
        ).fetchone()[0] == 0

    args = root.parse_args(argv)
    assert kanban_cli._cmd_publish(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["publication_action"] == "created"
    with kb.connect_closing(board) as conn:
        publisher = kb.get_task(conn, result["publication_task_id"])
        assert publisher is not None
        assert publisher.assignee == "releaser"
        assert publisher.publication_ref == "refs/heads/main"


def test_release_target_is_recorded_at_create_time_and_validated(board: Path) -> None:
    """BUILD-795 AC1: the row states where the card may publish.

    It is deliberately independent of the publication triple — an ordinary
    coder card records it, and the publication card inherits it later.
    """
    with kb.connect_closing(board) as conn:
        bound = kb.create_task(
            conn, title="coder work", assignee="coder",
            publication_repo="Ahlnos-Inc/aldnoah",
        )
        assert kb.get_task(conn, bound).publication_repo == "Ahlnos-Inc/aldnoah"

        # Unbound cards keep working (AC4), and an empty value is not a target.
        assert kb.get_task(conn, _create_requester(conn)).publication_repo is None
        blank = kb.create_task(
            conn, title="blank", assignee="coder", publication_repo="   ",
        )
        assert kb.get_task(conn, blank).publication_repo is None

        for bad in ("not-a-slug", "owner/", "/repo", "owner/repo/extra", "own er/repo"):
            with pytest.raises(ValueError, match="owner/repo"):
                kb.create_task(
                    conn, title="bad", assignee="coder", publication_repo=bad,
                )


def test_publication_card_inherits_the_requesters_release_target(
    board: Path, tmp_path: Path,
) -> None:
    """The target comes from the REQUESTER'S row, never the worker's payload.

    The requesting worker chooses the workspace, the remote and the sha. If it
    could also choose the repository its card is checked against, the check
    would be checking the worker against itself.
    """
    workspace = tmp_path / "coder-workspace"
    workspace.mkdir()
    with kb.connect_closing(board) as conn:
        requester = kb.create_task(
            conn, title="coder work", assignee="coder",
            publication_repo="nlachica/hermes-config",
        )
        run_id = _claim(conn, requester)
        result = kb.request_publication_handoff(
            conn,
            requester,
            publication=kb.NewPublicationTask(
                expected_sha=EXPECTED_SHA,
                workspace_path=str(workspace),
                remote_ref="refs/heads/main",
            ),
            request_key="publish-inherit-1",
            actor="coder",
            expected_run_id=run_id,
        )
        publisher = kb.get_task(conn, result.publication_task_id)
        assert publisher.publication_repo == "nlachica/hermes-config"

        # An unbound requester yields an unbound publication card.
        other = _create_requester(conn)
        other_run = _claim(conn, other)
        other_result = kb.request_publication_handoff(
            conn,
            other,
            publication=kb.NewPublicationTask(
                expected_sha=EXPECTED_SHA,
                workspace_path=str(workspace),
                remote_ref="refs/heads/main",
            ),
            request_key="publish-inherit-2",
            actor="coder",
            expected_run_id=other_run,
        )
        assert kb.get_task(conn, other_result.publication_task_id).publication_repo is None
