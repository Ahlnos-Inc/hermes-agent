"""Kanban <-> Projects integration: project-linked tasks get a deterministic
worktree path + branch instead of the random ``wt/<task-id>`` fallback."""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

import pytest

from agent.claude_workspace_terminal import build_workspace_terminal_args
from hermes_cli import kanban_db as kb
from hermes_cli import projects_db as pdb


@pytest.fixture
def kanban_conn(tmp_path):
    c = kb.connect(db_path=tmp_path / "kanban.db")
    try:
        yield c
    finally:
        c.close()


def _make_project(name="Web App", repo="/tmp/webapp"):
    with pdb.connect_closing() as pc:
        pid = pdb.create_project(pc, name=name, folders=[repo])
        return pdb.get_project(pc, pid)


def _init_repo(path):
    path.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True, text=True
    )
    (path / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(
        [
            "git", "-c", "user.name=Hermes Test", "-c", "user.email=hermes@example.invalid",
            "commit", "-m", "base",
        ],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )


def test_project_linked_task_gets_deterministic_worktree_and_branch(kanban_conn):
    proj = _make_project()
    tid = kb.create_task(kanban_conn, title="Add login", project_id=proj.slug)
    task = kb.get_task(kanban_conn, tid)

    assert task.project_id == proj.id
    assert task.workspace_kind == "worktree"
    # Worktree dir anchored under the project's primary repo, keyed on task id.
    assert task.workspace_path == os.path.join(proj.primary_path, ".worktrees", tid)
    # Deterministic branch: <slug>/<task-id>-<title-slug>. NOT a random wt/...
    assert task.branch_name == f"{proj.slug}/{tid}-add-login"
    assert not task.branch_name.startswith("wt/")


def test_explicit_branch_overrides_project_default(kanban_conn):
    proj = _make_project()
    tid = kb.create_task(
        kanban_conn,
        title="x",
        project_id=proj.slug,
        workspace_kind="worktree",
        branch_name="feature/custom",
    )
    task = kb.get_task(kanban_conn, tid)
    assert task.branch_name == "feature/custom"


def test_unlinked_task_unchanged(kanban_conn):
    tid = kb.create_task(kanban_conn, title="plain")
    task = kb.get_task(kanban_conn, tid)

    assert task.project_id is None
    assert task.workspace_kind == "scratch"
    # No branch is persisted — the worker still owns the wt/<id> fallback for
    # genuinely ad-hoc worktree tasks, but unlinked scratch tasks have none.
    assert task.branch_name is None


def test_unknown_project_id_falls_back_gracefully(kanban_conn):
    # A project id that doesn't resolve must not crash task creation; the task
    # is created as-is (scratch) and project_id stays unset.
    tid = kb.create_task(kanban_conn, title="x", project_id="does-not-exist")
    task = kb.get_task(kanban_conn, tid)
    assert task.workspace_kind == "scratch"
    assert task.project_id is None


@pytest.mark.skipif(os.uname().sysname != "Darwin", reason="macOS sandbox-exec")
def test_project_linked_coder_worktree_is_writable_but_siblings_and_auth_are_denied(
    kanban_conn, tmp_path
):
    """Project links create the explicit repo scope consumed by Seatbelt."""
    repo = tmp_path / "hermes-config"
    _init_repo(repo)
    project = _make_project(name="Hermes Config", repo=str(repo))
    assert project is not None
    task_id = kb.create_task(
        kanban_conn,
        title="Scoped implementation",
        assignee="coder",
        project_id=project.slug,
    )
    task = kb.get_task(kanban_conn, task_id)
    assert task is not None
    assert task.workspace_kind == "worktree"

    workspace = kb.resolve_workspace(task)
    sibling = tmp_path / "unrelated-repository"
    sibling.mkdir()
    sibling_secret = sibling / "secret.txt"
    sibling_secret.write_text("unrelated\n", encoding="utf-8")
    host_auth = tmp_path / "host" / ".claude.json"
    host_auth.parent.mkdir()
    host_auth.write_text("auth-secret\n", encoding="utf-8")

    transformed = build_workspace_terminal_args(
        {
            "command": "; ".join(
                [
                    "cat README.md > readback.txt",
                    "printf scoped > implementation.txt",
                    f"! cat {shlex.quote(str(sibling_secret))}",
                    f"! cat {shlex.quote(str(host_auth))}",
                ]
            )
        },
        workspace=workspace,
        host_home=host_auth.parent,
        exact_env={"PATH": os.environ["PATH"]},
    )
    argv = shlex.split(transformed["command"])
    profile = Path(argv[argv.index("-f") + 1]).read_text(encoding="utf-8")

    result = subprocess.run(
        ["/bin/bash", "-lc", transformed["command"]],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr
    assert workspace == repo / ".worktrees" / task_id
    assert (workspace / "readback.txt").read_text(encoding="utf-8") == "base\n"
    assert (workspace / "implementation.txt").read_text(encoding="utf-8") == "scoped"
    assert sibling_secret.read_text(encoding="utf-8") == "unrelated\n"
    assert host_auth.read_text(encoding="utf-8") == "auth-secret\n"
    assert f'(allow file-write* (subpath "{workspace}"))' in profile
    assert str(sibling) not in profile
    assert str(host_auth.parent) not in profile
