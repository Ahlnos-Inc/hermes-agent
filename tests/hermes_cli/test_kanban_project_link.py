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


def test_project_linked_task_gets_deterministic_worktree_and_branch(kanban_conn, tmp_path):
    repo = tmp_path / "webapp"
    _init_repo(repo)
    proj = _make_project(repo=str(repo))
    tid = kb.create_task(kanban_conn, title="Add login", project_id=proj.slug)
    task = kb.get_task(kanban_conn, tid)

    assert task.project_id == proj.id
    assert task.workspace_kind == "worktree"
    # Worktree dir anchored under the project's primary repo, keyed on task id.
    assert task.workspace_path == os.path.join(proj.primary_path, ".worktrees", tid)
    # Deterministic branch: <slug>/<task-id>-<title-slug>. NOT a random wt/...
    assert task.branch_name == f"{proj.slug}/{tid}-add-login"
    assert not task.branch_name.startswith("wt/")


def test_explicit_branch_overrides_project_default(kanban_conn, tmp_path):
    repo = tmp_path / "webapp"
    _init_repo(repo)
    proj = _make_project(repo=str(repo))
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


def test_unknown_project_fails_closed_and_persists_nothing(kanban_conn):
    # BUILD-496 invariant 3: an explicitly-requested project that doesn't
    # resolve must fail closed with a typed error — NOT silently degrade to a
    # scratch card (the incident's first hop). No task row or event persists.
    with pytest.raises(kb.WorkspaceContractError) as ei:
        kb.create_task(kanban_conn, title="x", project_id="does-not-exist")
    assert ei.value.code == "unknown_project"
    assert kanban_conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    assert kanban_conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == 0


def test_incident_shape_project_worktree_fails_closed(kanban_conn):
    # The exact incident: kanban_create(project=..., workspace_kind="worktree")
    # with the project unresolvable in the creating profile. Fail closed with
    # the typed error; persist nothing.
    with pytest.raises(kb.WorkspaceContractError) as ei:
        kb.create_task(
            kanban_conn,
            title="ship",
            project_id="does-not-exist",
            workspace_kind="worktree",
        )
    assert ei.value.code == "unknown_project"
    assert kanban_conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    assert kanban_conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == 0


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


# ---------------------------------------------------------------------------
# BUILD-820: give BUILD-795's release-target check a producer.
# ---------------------------------------------------------------------------


def _add_origin(path, url):
    subprocess.run(
        ["git", "remote", "add", "origin", url],
        cwd=path, check=True, capture_output=True, text=True,
    )


def test_project_linked_card_records_the_projects_release_target(kanban_conn, tmp_path):
    repo = tmp_path / "webapp"
    _init_repo(repo)
    _add_origin(repo, "https://github.com/Ahlnos-Inc/hermes-agent.git")
    proj = _make_project(repo=str(repo))

    tid = kb.create_task(kanban_conn, title="Add login", project_id=proj.slug)
    task = kb.get_task(kanban_conn, tid)

    # The slug comes from the project registry's canonical checkout, not from
    # the worker's workspace -- which is cut from that same path, so the
    # BUILD-795 preflight agrees by construction.
    assert task.publication_repo == "Ahlnos-Inc/hermes-agent"
    assert task.workspace_path.startswith(str(repo))


def test_a_project_without_a_github_origin_leaves_the_target_unset(kanban_conn, tmp_path):
    """Unset is pre-795 behaviour, which is the safe default (AC3)."""
    repo = tmp_path / "local-only"
    _init_repo(repo)
    proj = _make_project(name="Local Only", repo=str(repo))

    task = kb.get_task(
        kanban_conn, kb.create_task(kanban_conn, title="work", project_id=proj.slug)
    )
    assert task.publication_repo is None

    # A non-GitHub remote is not a GitHub release target either.
    other = tmp_path / "gitlab"
    _init_repo(other)
    _add_origin(other, "https://gitlab.com/owner/repo.git")
    proj2 = _make_project(name="Gitlab", repo=str(other))
    assert kb.get_task(
        kanban_conn, kb.create_task(kanban_conn, title="w2", project_id=proj2.slug)
    ).publication_repo is None


def test_an_explicit_release_target_wins_over_the_derived_one(kanban_conn, tmp_path):
    repo = tmp_path / "webapp2"
    _init_repo(repo)
    _add_origin(repo, "https://github.com/Ahlnos-Inc/hermes-agent.git")
    proj = _make_project(name="Explicit", repo=str(repo))

    task = kb.get_task(kanban_conn, kb.create_task(
        kanban_conn, title="work", project_id=proj.slug,
        publication_repo="nlachica/hermes-config",
    ))
    assert task.publication_repo == "nlachica/hermes-config"


def test_the_release_target_probe_never_runs_inside_the_board_transaction(
    kanban_conn, tmp_path, monkeypatch,
):
    """`git remote get-url` can take up to 15s; the board write lock cannot.

    `_prepare_task_create` also runs *inside* a write_txn on the rework and
    publication paths, which is why only `create_task` asks it to derive.
    """
    repo = tmp_path / "webapp3"
    _init_repo(repo)
    _add_origin(repo, "https://github.com/Ahlnos-Inc/hermes-agent.git")
    proj = _make_project(name="Lock Free", repo=str(repo))

    from hermes_cli import worker_credentials as wc

    seen = []
    real = wc.github_repo_for_workspace

    def watched(workspace, **kw):
        seen.append(kanban_conn.in_transaction)
        return real(workspace, **kw)

    monkeypatch.setattr(wc, "github_repo_for_workspace", watched)

    kb.create_task(kanban_conn, title="work", project_id=proj.slug)
    assert seen == [False]

    # The rework path prepares its fix card *inside* a write_txn and binds it
    # to the same project, so a probe there would hold the board write lock
    # for up to 15s.  It must not run at all: a fix card stays on the card it
    # reworks, so it has no release target of its own to derive.
    review = kb.create_task(kanban_conn, title="review", assignee="reviewer")
    claimed = kb.claim_task(kanban_conn, review, claimer="reviewer")
    assert claimed is not None and claimed.current_run_id is not None
    kb.request_rework(
        kanban_conn,
        review,
        finding="assertion is inverted",
        fix=kb.NewFixTask(
            title="fix it",
            body="apply the correction",
            assignee="coder",
            project_id=proj.slug,
        ),
        request_key="rework-1",
        actor="reviewer",
        expected_run_id=int(claimed.current_run_id),
    )
    assert seen == [False]
