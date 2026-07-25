"""Tests: kanban worker spawn grants Seatbelt read access to its own repo.

Regression coverage for BUILD-641 / BUILD-581. The per-task grant hook
``HERMES_SANDBOX_EXTRA_READ_PATHS`` shipped with BUILD-581 (``6a08c84fb``) and
is consumed by ``agent/external_runtime.py::_sandbox_extra_read_paths`` on the
way to the Seatbelt profile's ``additional_readable_roots`` — but nothing ever
populated it for a worker spawn, so the mechanism was dormant (BUILD-641).

A worker whose workspace is a managed git worktree is confined to the worktree
itself, while both the source it was handed and the worktree's git common dir
(``<repo>/.git/worktrees/<id>``) live under the repository root, outside that
boundary. Deriving the grant from the task's own recorded worktree identity is
repo-agnostic, which matters because the hermes-infra board is bi-repo: the
same lane builds runtime tickets in the hermes-agent fork and config tickets in
hermes-config, so a static per-profile path list cannot cover both.

Least privilege is preserved: the grant is read-only, is derived from the
task's own workspace identity rather than a caller-supplied path, and a task
with no recorded worktree gets a byte-for-byte unchanged environment.
"""

from __future__ import annotations

import subprocess


def _make_task(kb, **overrides):
    fields = dict(
        id="t_sandbox",
        title="sandbox grant",
        body=None,
        assignee="w",
        status="running",
        priority=0,
        created_by="test",
        created_at=1,
        started_at=None,
        completed_at=None,
        workspace_kind="worktree",
        workspace_path=None,
        claim_lock="lock",
        claim_expires=None,
        tenant=None,
        # Low-level spawn helper called directly: legacy/manual spawn with no
        # persisted run contract, matching test_kanban_worker_terminal_cwd.
        current_run_id=None,
    )
    fields.update(overrides)
    return kb.Task(**fields)


def _isolated_home(monkeypatch, tmp_path):
    root = tmp_path / ".hermes"
    (root / "profiles" / "w").mkdir(parents=True)
    (root / "profiles" / "w" / "config.yaml").write_text(
        "toolsets:\n  - kanban\n", encoding="utf-8"
    )
    root.joinpath("config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    return root


def _capture_spawn_env(kb, monkeypatch, workspace: str, task) -> dict:
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])

    captured: dict = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = dict(kwargs.get("env") or {})
        captured["cwd"] = kwargs.get("cwd")
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    kb._default_spawn(task, workspace)
    return captured


def _granted(captured) -> list[str]:
    raw = captured["env"].get("HERMES_SANDBOX_EXTRA_READ_PATHS", "")
    return [part for part in raw.split(":") if part]


def test_worktree_worker_is_granted_its_repo_root(monkeypatch, tmp_path):
    """The repo the worker was handed is readable from inside its worktree."""
    _isolated_home(monkeypatch, tmp_path)

    from hermes_cli import kanban_db as kb

    repo = tmp_path / "repo"
    workspace = repo / ".worktrees" / "t_sandbox"
    workspace.mkdir(parents=True)

    captured = _capture_spawn_env(
        kb,
        monkeypatch,
        str(workspace),
        _make_task(
            kb,
            workspace_managed=True,
            workspace_repo_root=str(repo),
            workspace_repo_common_dir=str(repo / ".git"),
        ),
    )

    granted = _granted(captured)
    assert str(repo) in granted
    # The worktree's git metadata lives under the common dir; without it git
    # commands inside the worktree fail even though the source is readable.
    assert str(repo / ".git") in granted


def test_common_dir_outside_repo_root_is_granted_separately(monkeypatch, tmp_path):
    """A common dir that is not under the repo root still gets its own grant."""
    _isolated_home(monkeypatch, tmp_path)

    from hermes_cli import kanban_db as kb

    repo = tmp_path / "repo"
    common = tmp_path / "elsewhere" / "git-common"
    workspace = repo / ".worktrees" / "t_sandbox"
    workspace.mkdir(parents=True)

    captured = _capture_spawn_env(
        kb,
        monkeypatch,
        str(workspace),
        _make_task(
            kb,
            workspace_managed=True,
            workspace_repo_root=str(repo),
            workspace_repo_common_dir=str(common),
        ),
    )

    granted = _granted(captured)
    assert str(repo) in granted
    assert str(common) in granted


def test_no_recorded_worktree_leaves_confinement_unchanged(monkeypatch, tmp_path):
    """Default confinement is byte-for-byte unchanged when nothing is recorded."""
    _isolated_home(monkeypatch, tmp_path)

    from hermes_cli import kanban_db as kb

    workspace = tmp_path / "ws"
    workspace.mkdir()

    captured = _capture_spawn_env(
        kb,
        monkeypatch,
        str(workspace),
        _make_task(kb, workspace_kind="dir"),
    )

    assert "HERMES_SANDBOX_EXTRA_READ_PATHS" not in captured["env"]


def test_inherited_grant_is_preserved_and_deduped(monkeypatch, tmp_path):
    """An operator-supplied grant survives; the derived one appends once."""
    _isolated_home(monkeypatch, tmp_path)
    repo = tmp_path / "repo"
    monkeypatch.setenv(
        "HERMES_SANDBOX_EXTRA_READ_PATHS", f"/operator/grant:{repo}"
    )

    from hermes_cli import kanban_db as kb

    workspace = repo / ".worktrees" / "t_sandbox"
    workspace.mkdir(parents=True)

    captured = _capture_spawn_env(
        kb,
        monkeypatch,
        str(workspace),
        _make_task(
            kb,
            workspace_managed=True,
            workspace_repo_root=str(repo),
            workspace_repo_common_dir=str(repo / ".git"),
        ),
    )

    # The operator grant keeps its position, the already-present repo root is
    # not repeated, and only the still-missing common dir is appended.
    assert _granted(captured) == ["/operator/grant", str(repo), str(repo / ".git")]
