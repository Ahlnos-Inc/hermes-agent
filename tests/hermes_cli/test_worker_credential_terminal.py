from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent import shell_hooks
from hermes_cli import worker_credentials as wc
from tools.code_execution_tool import _scrub_child_env
from tools.env_passthrough import is_env_passthrough
from tools.environments import local


TOKEN = "worker-github-token-never-log"


@pytest.fixture
def worker_contract(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / wc.MANIFEST_FILENAME).write_text(
        "version: 1\n"
        "profiles:\n"
        "  releaser:\n"
        "    actions: [github_write]\n"
        "  verifier:\n"
        "    actions: []\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_PROFILE", "releaser")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-terminal-contract")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "91")
    monkeypatch.setenv(
        wc.MANIFEST_DIGEST_ENV, wc.load_manifest(hermes_home).digest
    )
    wc.reset_worker_credential_context_for_tests()
    yield hermes_home
    wc.cleanup_worker_terminal_artifacts()
    wc.reset_worker_credential_context_for_tests()


def _set_ambient_credentials(monkeypatch):
    for name, value in {
        "BWS_ACCESS_TOKEN": "ambient-bws",
        "GH_TOKEN": "ambient-gh",
        "GITHUB_TOKEN": "ambient-github",
        "COPILOT_GITHUB_TOKEN": "ambient-copilot",
        "GH_TOKEN_SECRET_WRITE": "ambient-source",
        wc.GITHUB_WRITE_HANDOFF_ENV: "ambient-handoff",
    }.items():
        monkeypatch.setenv(name, value)


def test_raw_terminal_cron_hook_and_execute_code_strip_worker_credentials(
    monkeypatch, worker_contract
):
    _set_ambient_credentials(monkeypatch)
    monkeypatch.setenv("HERMES_PROFILE", "verifier")

    terminal_env = local._sanitize_subprocess_env(dict(os.environ))
    raw_terminal_env = local._make_run_env({"PATH": "/usr/bin:/bin"})
    cron_env = local.hermes_subprocess_env(inherit_credentials=True)
    execute_env = _scrub_child_env(
        dict(os.environ),
        is_passthrough=is_env_passthrough,
        is_windows=False,
    )

    captured = {}

    def fake_run(*_args, **kwargs):
        captured["env"] = kwargs["env"]
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(shell_hooks.subprocess, "run", fake_run)
    result = shell_hooks._spawn(
        shell_hooks.ShellHookSpec(event="on_session_start", command="true"),
        "{}",
    )

    assert result["returncode"] == 0
    hook_env = captured["env"]
    for env in (terminal_env, raw_terminal_env, cron_env, execute_env, hook_env):
        assert "BWS_ACCESS_TOKEN" not in env
        assert "GH_TOKEN" not in env
        assert "GITHUB_TOKEN" not in env
        assert "COPILOT_GITHUB_TOKEN" not in env
        assert "GH_TOKEN_SECRET_WRITE" not in env
        assert wc.GITHUB_WRITE_HANDOFF_ENV not in env


def test_non_releaser_cannot_self_grant_with_private_marker(
    monkeypatch, worker_contract
):
    monkeypatch.setenv("HERMES_PROFILE", "verifier")
    monkeypatch.setenv(wc.GITHUB_WRITE_HANDOFF_ENV, TOKEN)
    monkeypatch.setenv("GH_TOKEN", "ambient-gh")

    env = local._make_run_env({"PATH": "/usr/bin:/bin"})

    assert "GH_TOKEN" not in env
    assert wc.GITHUB_WRITE_HANDOFF_ENV not in os.environ
    assert not wc.has_trusted_worker_action("github_write")


def test_denied_worker_terminal_gets_git_and_gh_isolation(
    monkeypatch, worker_contract
):
    monkeypatch.setenv("HERMES_PROFILE", "verifier")
    monkeypatch.setenv("GH_TOKEN", "ambient-gh")
    monkeypatch.setenv("GH_CONFIG_DIR", "/user/gh-config")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/user/gitconfig")

    run_env = local._make_run_env({"PATH": "/usr/bin:/bin"})

    gh_config = Path(run_env["GH_CONFIG_DIR"])
    assert gh_config.is_dir()
    assert list(gh_config.iterdir()) == []
    assert "GH_TOKEN" not in run_env
    assert run_env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert run_env["GIT_CONFIG_COUNT"] == "1"
    assert run_env["GIT_CONFIG_KEY_0"] == "credential.helper"
    assert run_env["GIT_CONFIG_VALUE_0"] == ""
    assert Path(run_env["GIT_CONFIG_GLOBAL"]).is_file()


def test_non_worker_terminal_keeps_existing_git_and_gh_environment(
    monkeypatch, worker_contract
):
    for name in (
        "HERMES_PROFILE",
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_RUN_ID",
        "GH_CONFIG_DIR",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_COUNT",
    ):
        monkeypatch.delenv(name, raising=False)

    run_env = local._make_run_env(
        {
            "PATH": "/usr/bin:/bin",
            "GH_CONFIG_DIR": "/user/gh-config",
            "GIT_CONFIG_GLOBAL": "/user/gitconfig",
        }
    )

    assert run_env["GH_CONFIG_DIR"] == "/user/gh-config"
    assert run_env["GIT_CONFIG_GLOBAL"] == "/user/gitconfig"
    assert "GIT_CONFIG_NOSYSTEM" not in run_env
    assert "GIT_CONFIG_COUNT" not in run_env


def test_releaser_terminal_gets_exact_git_and_gh_isolation(
    monkeypatch, worker_contract, tmp_path, caplog
):
    monkeypatch.setenv(wc.GITHUB_WRITE_HANDOFF_ENV, TOKEN)
    monkeypatch.setenv("GH_TOKEN", "ambient-gh")
    monkeypatch.setenv("GITHUB_TOKEN", "ambient-github")
    monkeypatch.setenv("BWS_ACCESS_TOKEN", "ambient-bws")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_gh = fake_bin / "gh"
    fake_gh.write_text(
        "#!/bin/sh\n"
        f"test \"$GH_TOKEN\" = {TOKEN}\n"
        "test -d \"$GH_CONFIG_DIR\"\n"
        "test ! -e \"$GH_CONFIG_DIR/hosts.yml\"\n"
        "printf 'gh-ok\\n'\n",
        encoding="utf-8",
    )
    fake_gh.chmod(0o700)

    poisoned_home = tmp_path / "poisoned-home"
    (poisoned_home / ".config" / "gh").mkdir(parents=True)
    (poisoned_home / ".config" / "gh" / "hosts.yml").write_text(
        "poisoned-hosts", encoding="utf-8"
    )
    (poisoned_home / ".gitconfig").write_text(
        "[credential]\n\thelper = /bin/false\n", encoding="utf-8"
    )
    system_config = tmp_path / "system.gitconfig"
    system_config.write_text(
        "[credential]\n\thelper = /bin/false\n", encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(poisoned_home))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(system_config))

    run_env = local._make_run_env({
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "HOME": str(poisoned_home),
    })

    git_config = Path(run_env["GIT_CONFIG_GLOBAL"])
    gh_config = Path(run_env["GH_CONFIG_DIR"])
    assert run_env["GH_TOKEN"] == TOKEN
    assert run_env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert run_env["GIT_CONFIG_COUNT"] == "2"
    assert run_env["GIT_CONFIG_VALUE_0"] == ""
    assert run_env["GIT_CONFIG_VALUE_1"] == wc.GIT_ENV_TOKEN_HELPER
    assert git_config.stat().st_mode & 0o777 == 0o600
    assert gh_config.stat().st_mode & 0o777 == 0o700
    assert TOKEN not in git_config.read_text(encoding="utf-8")
    assert TOKEN not in caplog.text

    gh_result = subprocess.run(
        ["gh", "auth", "status"],
        cwd=tmp_path,
        env=run_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert gh_result.returncode == 0, gh_result.stderr
    assert gh_result.stdout == "gh-ok\n"

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, env=run_env, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "credential.helper", "/bin/false"],
        cwd=repo,
        env=run_env,
        check=True,
        capture_output=True,
    )
    fill = subprocess.run(
        ["git", "credential", "fill"],
        cwd=repo,
        env=run_env,
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert fill.returncode == 0, fill.stderr
    assert f"password={TOKEN}" in fill.stdout
    assert TOKEN not in caplog.text


def test_terminal_artifact_cleanup_is_idempotent(monkeypatch, worker_contract):
    monkeypatch.setenv(wc.GITHUB_WRITE_HANDOFF_ENV, TOKEN)
    env = local._make_run_env({"PATH": "/usr/bin:/bin"})
    artifact_dir = Path(env["GIT_CONFIG_GLOBAL"]).parent
    assert artifact_dir.is_dir()

    assert wc.cleanup_worker_terminal_artifacts()
    assert not artifact_dir.exists()
    assert not wc.cleanup_worker_terminal_artifacts()


def test_stale_artifact_gc_is_scoped_and_idempotent(worker_contract):
    runtime_root = wc.worker_credential_runtime_root()
    runtime_root.mkdir(parents=True, exist_ok=True)
    stale = runtime_root / "run-stale"
    stale.mkdir()
    (runtime_root / "do-not-touch").mkdir()
    os.utime(stale, (1, 1))

    assert wc.cleanup_stale_worker_credential_artifacts(max_age_seconds=0) == 1
    assert not stale.exists()
    assert (runtime_root / "do-not-touch").is_dir()
    assert wc.cleanup_stale_worker_credential_artifacts(max_age_seconds=0) == 0


def test_claude_sdk_terminal_fails_closed_for_github_write(
    monkeypatch, worker_contract, tmp_path
):
    monkeypatch.setenv(wc.GITHUB_WRITE_HANDOFF_ENV, TOKEN)

    from agent.claude_workspace_terminal import build_workspace_terminal_args

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(RuntimeError, match="does not support github_write"):
        build_workspace_terminal_args(
            {"command": "git status"},
            workspace=workspace,
            host_home=tmp_path / "host",
            exact_env={"PATH": "/usr/bin:/bin"},
            platform_name="Linux",
        )
