"""Regression coverage for collection-time HERMES_HOME isolation."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


# ``tests/conftest.py`` is imported before this module. The marker lets the
# subprocess smoke test exercise import-time logging during collection without
# making ordinary collection import the CLI entry point twice.
if os.environ.get("HERMES_COLLECTION_SMOKE") == "1":
    from hermes_constants import get_hermes_home
    from hermes_cli import main as _smoke_main  # noqa: F401

    _smoke_home = get_hermes_home().resolve()
    print(f"HERMES_COLLECTION_SMOKE_HOME={_smoke_home}")
    print(
        "HERMES_COLLECTION_SMOKE_AGENT_LOG="
        f"{int((_smoke_home / 'logs' / 'agent.log').exists())}"
    )


def _canonical(path: Path) -> Path:
    """Canonicalize paths for symlink- and ``..``-safe comparisons."""
    return Path(path).expanduser().resolve(strict=False)


def test_gateway_state_writer_uses_active_home(monkeypatch, tmp_path):
    import gateway.status as status

    home = tmp_path / "gateway-home"
    monkeypatch.setenv("HERMES_HOME", str(home))

    status.write_runtime_status(gateway_state="running")

    state_path = status._get_runtime_status_path()
    assert _canonical(state_path) == _canonical(home / "gateway_state.json")
    assert _canonical(state_path) != _canonical(Path.home() / ".hermes" / "gateway_state.json")
    assert state_path.exists()


def test_gateway_home_resolver_follows_environment_after_import(monkeypatch, tmp_path):
    import gateway.run as gateway_run

    active_home = tmp_path / "active-home"
    pinned_home = tmp_path / "pinned-home"
    monkeypatch.setattr(
        gateway_run, "_hermes_home", gateway_run._HERMES_HOME_AT_IMPORT
    )
    monkeypatch.setenv("HERMES_HOME", str(active_home))

    assert _canonical(gateway_run._current_hermes_home()) == _canonical(active_home)

    monkeypatch.setattr(gateway_run, "_hermes_home", pinned_home)
    assert _canonical(gateway_run._current_hermes_home()) == _canonical(pinned_home)


def test_cron_store_and_execution_ledger_follow_home_after_import(monkeypatch, tmp_path):
    import cron.executions as executions
    import cron.jobs as jobs

    first_home = tmp_path / "first-home"
    second_home = tmp_path / "second-home"
    monkeypatch.setenv("HERMES_HOME", str(first_home))
    assert _canonical(executions._executions_file()) == _canonical(
        first_home / "cron" / "executions.db"
    )

    monkeypatch.setenv("HERMES_HOME", str(second_home))
    assert _canonical(executions._executions_file()) == _canonical(
        second_home / "cron" / "executions.db"
    )
    assert _canonical(jobs._current_cron_store().jobs_file) == _canonical(
        second_home / "cron" / "jobs.json"
    )

    executions.create_execution("hermetic-job", source="test")

    assert (second_home / "cron" / "executions.db").exists()
    assert not (first_home / "cron" / "executions.db").exists()
    assert _canonical(second_home / "cron" / "executions.db") != _canonical(
        Path.home() / ".hermes" / "cron" / "executions.db"
    )


def test_logging_setup_uses_active_home(monkeypatch, tmp_path):
    import hermes_logging

    home = tmp_path / "logging-home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(hermes_logging, "_logging_initialized", False)

    log_dir = hermes_logging.setup_logging(force=True)

    assert _canonical(log_dir) == _canonical(home / "logs")
    assert _canonical(log_dir) != _canonical(Path.home() / ".hermes" / "logs")
    assert (home / "logs" / "agent.log").exists()
    assert (home / "logs" / "errors.log").exists()


def test_session_db_uses_active_home_after_import(monkeypatch, tmp_path):
    import hermes_state

    home = tmp_path / "state-home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    db = hermes_state.SessionDB()
    try:
        assert _canonical(db.db_path) == _canonical(home / "state.db")
        assert _canonical(db.db_path) != _canonical(Path.home() / ".hermes" / "state.db")
        assert db.db_path.exists()
    finally:
        db.close()


def test_collection_bootstrap_precedes_import_time_logging(tmp_path):
    """A clean pytest process must isolate collection before importing CLI code."""
    repo = Path(__file__).resolve().parents[1]
    real_home = tmp_path / "sentinel-home"
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(real_home),
        "PYTHONPATH": str(repo),
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "HERMES_COLLECTION_SMOKE": "1",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
    }
    env.pop("HERMES_HOME", None)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-s",
            str(Path(__file__)),
        ],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "HERMES_COLLECTION_SMOKE_HOME=" in output, output
    assert "HERMES_COLLECTION_SMOKE_AGENT_LOG=1" in output, output
    assert "6 tests collected" in output, output
    assert str(_canonical(real_home)) not in output
    assert not real_home.exists()
