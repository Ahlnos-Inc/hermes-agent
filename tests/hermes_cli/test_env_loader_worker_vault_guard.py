"""A dispatcher worker may only pull an external secret vault if granted.

BUILD-681 stopped a worker INHERITING the controller's vault-sourced
environment. It was a no-op for any profile whose home can reach a vault access
token, because the worker just re-pulls the whole project itself at import time
— measured live for five profiles (BUILD-789). The credential manifest is the
operative control now: no ``bws_bootstrap`` grant, no pull.

The guard keys on ``HERMES_KANBAN_TASK``, which only a dispatcher-spawned
worker has, so the controller, cron agents, and interactive CLI runs are
untouched.
"""

import pytest

from hermes_cli import env_loader, worker_credentials


@pytest.fixture
def applied(monkeypatch, tmp_path):
    """Run ``_apply_external_secret_sources`` and report whether it pulled."""
    calls: list = []

    def _fake_apply_all(cfg, home_path):
        calls.append(home_path)
        raise RuntimeError("apply_all reached; the guard did not fire")

    home = tmp_path / "home"
    (home / "secrets").mkdir(parents=True)
    monkeypatch.setattr(
        env_loader, "_load_secrets_config", lambda _p: {"bitwarden": {"enabled": True}}
    )
    import agent.secret_sources.registry as registry

    monkeypatch.setattr(registry, "apply_all", _fake_apply_all)

    def _run():
        env_loader.reset_secret_source_cache()
        try:
            env_loader._apply_external_secret_sources(home)
        except RuntimeError:
            pass
        return bool(calls)

    return _run


def _grant(monkeypatch, actions):
    manifest = worker_credentials.WorkerCredentialManifest(
        version=2, profiles={"coder": tuple(actions)}, digest="x" * 64,
    )
    monkeypatch.setattr(worker_credentials, "load_manifest", lambda *a, **k: manifest)


def test_controller_still_pulls(monkeypatch, applied):
    """No task id — this is the controller/CLI/cron path, unchanged."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setenv("HERMES_PROFILE", "orchestrator")
    _grant(monkeypatch, [])
    assert applied() is True


def test_worker_without_grant_does_not_pull(monkeypatch, applied):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_deadbeef")
    monkeypatch.setenv("HERMES_PROFILE", "coder")
    _grant(monkeypatch, [])
    assert applied() is False


def test_worker_with_bws_bootstrap_grant_pulls(monkeypatch, applied):
    """The grant is the whole point: marketing-operator holds one today."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_deadbeef")
    monkeypatch.setenv("HERMES_PROFILE", "coder")
    _grant(monkeypatch, ["bws_bootstrap"])
    assert applied() is True


def test_an_unrelated_grant_is_not_a_vault_grant(monkeypatch, applied):
    """github_write is a terminal-only projection, not a full-process vault key."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_deadbeef")
    monkeypatch.setenv("HERMES_PROFILE", "coder")
    _grant(monkeypatch, ["github_write"])
    assert applied() is False


def test_worker_without_a_profile_fails_closed(monkeypatch, applied):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_deadbeef")
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    _grant(monkeypatch, ["bws_bootstrap"])
    assert applied() is False


def test_unreadable_manifest_fails_closed(monkeypatch, applied):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_deadbeef")
    monkeypatch.setenv("HERMES_PROFILE", "coder")

    def _boom(*a, **k):
        raise OSError("manifest unreadable")

    monkeypatch.setattr(worker_credentials, "load_manifest", _boom)
    assert applied() is False
