"""Recoverable workspace snapshots for dirty and unborn repositories."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check,
        capture_output=True,
        text=True,
    )


def _init_repo(repo: Path, *, commit: bool) -> None:
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Kanban Test")
    _git(repo, "config", "user.email", "kanban@example.com")
    (repo / ".gitignore").write_text("ignored.tmp\n", encoding="utf-8")
    (repo / "tracked.txt").write_text("committed\n", encoding="utf-8")
    if commit:
        _git(repo, "add", ".gitignore", "tracked.txt")
        _git(repo, "commit", "-m", "baseline")


def _isolated_board(tmp_path, monkeypatch, repo: Path):
    from hermes_cli import kanban_db as kb

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.create_board("checkpoint-test", default_workdir=str(repo))
    return kb


def test_dirty_repo_worktree_starts_from_snapshot_without_mutating_source(
    tmp_path, monkeypatch,
):
    repo = tmp_path / "repo"
    _init_repo(repo, commit=True)
    (repo / "tracked.txt").write_text("staged tracked\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    (repo / "tracked.txt").write_text("dirty tracked\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("recover me\n", encoding="utf-8")
    (repo / "ignored.tmp").write_text("do not copy\n", encoding="utf-8")
    before_head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    before_status = _git(repo, "status", "--short").stdout
    before_cached = _git(repo, "diff", "--cached", "--binary").stdout

    kb = _isolated_board(tmp_path, monkeypatch, repo)
    with kb.connect(board="checkpoint-test") as conn:
        task_id = kb.create_task(
            conn,
            title="snapshot dirty WIP",
            workspace_kind="worktree",
            board="checkpoint-test",
        )
        task = kb.get_task(conn, task_id)
        workspace = kb.resolve_workspace(task, board="checkpoint-test")

    assert (workspace / "tracked.txt").read_text() == "dirty tracked\n"
    assert (workspace / "untracked.txt").read_text() == "recover me\n"
    assert not (workspace / "ignored.tmp").exists()
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == before_head
    assert _git(repo, "status", "--short").stdout == before_status
    assert _git(repo, "diff", "--cached", "--binary").stdout == before_cached
    checkpoint = _git(
        repo, "rev-parse", f"refs/hermes/checkpoints/{task_id}",
    ).stdout.strip()
    assert _git(workspace, "rev-parse", "HEAD").stdout.strip() == checkpoint


def test_unborn_repo_worktree_contains_nonignored_source_and_source_stays_unborn(
    tmp_path, monkeypatch,
):
    repo = tmp_path / "repo"
    _init_repo(repo, commit=False)
    (repo / "untracked.txt").write_text("first source\n", encoding="utf-8")
    (repo / "ignored.tmp").write_text("do not copy\n", encoding="utf-8")
    before_status = _git(repo, "status", "--short").stdout
    assert _git(repo, "rev-parse", "--verify", "HEAD", check=False).returncode != 0

    kb = _isolated_board(tmp_path, monkeypatch, repo)
    with kb.connect(board="checkpoint-test") as conn:
        task_id = kb.create_task(
            conn,
            title="snapshot unborn source",
            workspace_kind="worktree",
            board="checkpoint-test",
        )
        task = kb.get_task(conn, task_id)
        workspace = kb.resolve_workspace(task, board="checkpoint-test")

    assert (workspace / "tracked.txt").read_text() == "committed\n"
    assert (workspace / "untracked.txt").read_text() == "first source\n"
    assert not (workspace / "ignored.tmp").exists()
    assert _git(repo, "rev-parse", "--verify", "HEAD", check=False).returncode != 0
    assert _git(repo, "status", "--short").stdout == before_status
    assert _git(
        repo, "show-ref", "--verify", f"refs/hermes/checkpoints/{task_id}",
    ).returncode == 0


def test_dispatch_records_checkpoint_identity(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo, commit=True)
    (repo / "tracked.txt").write_text("dispatch snapshot\n", encoding="utf-8")
    kb = _isolated_board(tmp_path, monkeypatch, repo)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)

    with kb.connect(board="checkpoint-test") as conn:
        task_id = kb.create_task(
            conn,
            title="dispatch recoverably",
            assignee="coder",
            workspace_kind="worktree",
            board="checkpoint-test",
        )
        result = kb.dispatch_once(
            conn,
            board="checkpoint-test",
            spawn_fn=lambda _task, _workspace: kb.SpawnReceipt(
                pid=31_005,
                release=lambda: None,
                abort=lambda: None,
                process_started_at=1234.5,
                process_group_id=31_005,
                session_id=31_005,
            ),
        )
        events = kb.list_events(conn, task_id)

    assert result.spawned and result.spawned[0][0] == task_id
    checkpoint_event = next(event for event in events if event.kind == "workspace_checkpointed")
    assert checkpoint_event.payload["ref"] == f"refs/hermes/checkpoints/{task_id}"
    assert checkpoint_event.payload["sha"] == _git(
        repo, "rev-parse", checkpoint_event.payload["ref"],
    ).stdout.strip()


# --- Fleet worktree provisioning: runtime-remediation graphs (BUILD-592/694/736) ---


def _remediation_steps(workspace_kind_note: str = ""):
    return [
        {"key": "coder", "title": "implement fix", "assignee": "coder",
         "role": "coder", "parents": []},
        {"key": "verifier", "title": "independently verify", "assignee": "verifier",
         "role": "verifier", "parents": ["coder"]},
        {"key": "synth", "title": "synthesize", "assignee": "synth",
         "role": "synthesizer", "parents": ["verifier"], "terminal": True},
    ]


def test_compile_rejects_coder_on_scratch(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo, commit=True)
    kb = _isolated_board(tmp_path, monkeypatch, repo)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "checkpoint-test")
    with kb.connect(board="checkpoint-test") as conn:
        # The operator default that stranded the fleet: a coder with no repo.
        with pytest.raises(kb.WorkspaceContractError) as exc:
            kb.compile_workflow_graph(
                conn,
                workflow_key="sig-remediation-1",
                idempotency_key="ik-remediation-1",
                created_by="hermes-infra-operator",
                workspace_kind="scratch",
                steps=_remediation_steps(),
            )
        assert exc.value.code == "coder_needs_worktree"
        # No partial graph is persisted (all-or-none compile).
        assert conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE workflow_key = ?",
            ("sig-remediation-1",),
        ).fetchone()[0] == 0


def test_compile_accepts_coder_with_explicit_worktree(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo, commit=True)
    kb = _isolated_board(tmp_path, monkeypatch, repo)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "checkpoint-test")
    with kb.connect(board="checkpoint-test") as conn:
        compiled = kb.compile_workflow_graph(
            conn,
            workflow_key="sig-remediation-1b",
            idempotency_key="ik-remediation-1b",
            created_by="hermes-infra-operator",
            workspace_kind="worktree",
            workspace_path=str(repo),  # explicit target repo — no misrouting
            steps=_remediation_steps(),
        )
        coder = kb.get_task(conn, compiled.task_ids["coder"])
    assert coder.workspace_kind == "worktree"


def test_compile_leaves_non_repo_graph_on_scratch(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo, commit=True)
    kb = _isolated_board(tmp_path, monkeypatch, repo)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "checkpoint-test")
    with kb.connect(board="checkpoint-test") as conn:
        compiled = kb.compile_workflow_graph(
            conn,
            workflow_key="sig-research-1",
            idempotency_key="ik-research-1",
            created_by="orchestrator",
            workspace_kind="scratch",
            steps=[
                {"key": "a", "title": "research", "assignee": "researcher",
                 "role": "worker", "parents": []},
                {"key": "z", "title": "report", "assignee": "writer",
                 "role": "reporter", "parents": ["a"], "terminal": True},
            ],
        )
        a = kb.get_task(conn, compiled.task_ids["a"])

    # No coder/verifier role => nothing to build in a repo => stays scratch.
    assert a.workspace_kind == "scratch"


def test_compile_wires_verifier_branch_to_coder(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo, commit=True)
    kb = _isolated_board(tmp_path, monkeypatch, repo)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "checkpoint-test")
    with kb.connect(board="checkpoint-test") as conn:
        compiled = kb.compile_workflow_graph(
            conn,
            workflow_key="sig-remediation-2",
            idempotency_key="ik-remediation-2",
            created_by="hermes-infra-operator",
            workspace_kind="worktree",
            steps=_remediation_steps(),
        )
        coder_id = compiled.task_ids["coder"]
        coder = kb.get_task(conn, coder_id)
        verifier = kb.get_task(conn, compiled.task_ids["verifier"])

    # Verifier mirrors the coder's branch; the coder keeps its own default.
    assert verifier.branch_name == f"wt/{coder_id}"
    assert coder.branch_name is None


def test_verifier_worktree_detaches_at_coder_commit(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo, commit=True)
    kb = _isolated_board(tmp_path, monkeypatch, repo)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "checkpoint-test")
    before_head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    with kb.connect(board="checkpoint-test") as conn:
        compiled = kb.compile_workflow_graph(
            conn,
            workflow_key="sig-remediation-3",
            idempotency_key="ik-remediation-3",
            created_by="hermes-infra-operator",
            workspace_kind="worktree",
            steps=_remediation_steps(),
        )
        coder_id = compiled.task_ids["coder"]
        verifier_id = compiled.task_ids["verifier"]

        # Coder materializes its worktree and commits a fix.
        coder_ws = kb.resolve_workspace(kb.get_task(conn, coder_id), board="checkpoint-test")
        (coder_ws / "fix.py").write_text("print('the fix')\n", encoding="utf-8")
        _git(coder_ws, "add", "fix.py")
        _git(coder_ws, "commit", "-m", "coder: land the fix")
        coder_head = _git(coder_ws, "rev-parse", "HEAD").stdout.strip()

        # Verifier materializes a fresh, isolated checkout of that exact commit.
        verifier_ws = kb.resolve_workspace(kb.get_task(conn, verifier_id), board="checkpoint-test")

    # Independent directory, holding the coder's exact commit, detached.
    assert verifier_ws != coder_ws
    assert (verifier_ws / "fix.py").read_text() == "print('the fix')\n"
    assert _git(verifier_ws, "rev-parse", "HEAD").stdout.strip() == coder_head
    assert _git(verifier_ws, "symbolic-ref", "-q", "HEAD", check=False).returncode != 0

    # The live source repo is never touched.
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == before_head
