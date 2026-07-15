from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import gateway
from gateway import status


@pytest.fixture
def force_ps_path(monkeypatch):
    monkeypatch.setattr(gateway.os.path, "isdir", lambda path: False)
    monkeypatch.setattr(gateway, "is_windows", lambda: False)
    monkeypatch.setattr(gateway, "supports_systemd_services", lambda: True)


def test_strict_inventory_raises_when_ps_fails(monkeypatch, force_ps_path):
    monkeypatch.setattr(
        gateway.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 2, "", "denied"),
    )

    with pytest.raises(
        gateway.GatewayProcessEnumerationError,
        match="returned an error",
    ):
        gateway.find_gateway_pids_strict(all_profiles=True)


def test_strict_ps_failure_does_not_emit_captured_process_data(
    monkeypatch,
    force_ps_path,
    caplog,
    capsys,
):
    marker = "hermes-test-process-environment-marker"
    monkeypatch.setattr(
        gateway.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [],
            2,
            f"100 HERMES_TOKEN={marker}",
            f"ps failed near {marker}",
        ),
    )

    with pytest.raises(gateway.GatewayProcessEnumerationError):
        gateway.find_gateway_pids_strict(all_profiles=True)

    captured = capsys.readouterr()
    assert marker not in caplog.text
    assert marker not in captured.out
    assert marker not in captured.err


def test_strict_inventory_raises_when_ps_times_out(monkeypatch, force_ps_path):
    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("ps", 10)

    monkeypatch.setattr(gateway.subprocess, "run", timeout)

    with pytest.raises(gateway.GatewayProcessEnumerationError, match="failed"):
        gateway.find_gateway_pids_strict(all_profiles=True)


def test_strict_inventory_raises_when_ps_is_unavailable(
    monkeypatch,
    force_ps_path,
):
    def unavailable(*_args, **_kwargs):
        raise FileNotFoundError("ps")

    monkeypatch.setattr(gateway.subprocess, "run", unavailable)

    with pytest.raises(gateway.GatewayProcessEnumerationError, match="failed"):
        gateway.find_gateway_pids_strict(all_profiles=True)


def test_legacy_inventory_remains_best_effort(monkeypatch, force_ps_path):
    monkeypatch.setattr(
        gateway.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 2, "", "denied"),
    )
    monkeypatch.setattr(gateway, "_get_service_pids", lambda: set())

    assert gateway.find_gateway_pids(all_profiles=True) == []


def test_strict_inventory_returns_matching_processes(monkeypatch, force_ps_path):
    monkeypatch.setattr(
        gateway.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [],
            0,
            "4242 python -m hermes_cli.main gateway run\n",
            "",
        ),
    )
    monkeypatch.setattr(gateway, "_get_ancestor_pids", lambda: set())

    assert gateway.find_gateway_pids_strict(all_profiles=True) == [4242]


def test_strict_inventory_excludes_management_commands_without_systemd(
    monkeypatch, force_ps_path
):
    monkeypatch.setattr(gateway, "supports_systemd_services", lambda: False)
    monkeypatch.setattr(
        gateway.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [],
            0,
            "4242 python -m hermes_cli.main gateway restart\n"
            "4343 python -m hermes_cli.main gateway run --replace\n",
            "",
        ),
    )
    monkeypatch.setattr(gateway, "_get_ancestor_pids", lambda: set())

    assert gateway.find_gateway_pids_strict(all_profiles=True) == [4343]


def test_canonical_identity_contains_runtime_profile_and_home_attestation(tmp_path):
    home = tmp_path / "profiles" / "coder"
    argv = (
        "/usr/bin/python3",
        "-m",
        "hermes_cli.main",
        "--profile",
        "coder",
        "gateway",
        "run",
        "--replace",
    )
    identity = gateway.GatewayProcessIdentity(
        4242,
        42420,
        argv,
        environment={"HERMES_HOME": str(home)},
    )

    assert identity.process_birth == 42420
    assert identity.exact_argv == argv
    assert identity.runtime_role is gateway.GatewayRuntimeRole.RUNTIME
    assert identity.profile_identity == "coder"
    assert identity.resolved_hermes_home == home.resolve()
    assert "HERMES_TOKEN" not in repr(identity)


def test_live_identity_requires_explicit_home_attestation(monkeypatch):
    class _Process:
        def cmdline(self):
            return ["python", "-m", "hermes_cli.main", "gateway", "run"]

        def environ(self):
            return {"HERMES_TOKEN": "secret"}

    monkeypatch.setattr(gateway, "_psutil_process", lambda _pid: _Process())
    monkeypatch.setattr(gateway, "_launchd_process_start_time", lambda _pid: 42)

    with pytest.raises(gateway.GatewayProcessEnumerationError, match="HERMES_HOME"):
        gateway._read_live_gateway_process_identity(4242)


def test_strict_proc_inventory_fails_closed_on_permission_error(monkeypatch):
    monkeypatch.setattr(gateway, "is_windows", lambda: False)
    monkeypatch.setattr(gateway, "supports_systemd_services", lambda: True)
    monkeypatch.setattr(gateway.os.path, "isdir", lambda path: path == "/proc")
    monkeypatch.setattr(gateway.os, "getpid", lambda: 1)
    monkeypatch.setattr(gateway.os, "listdir", lambda _path: ["4242"])

    def denied_open(*_args, **_kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr("builtins.open", denied_open)

    with pytest.raises(
        gateway.GatewayProcessEnumerationError,
        match="could not inspect /proc/4242/cmdline",
    ):
        gateway.find_gateway_pids_strict(all_profiles=True)


class _SignalRecorder:
    def __init__(self):
        self.terminated = 0
        self.killed = 0

    def terminate(self):
        self.terminated += 1

    def kill(self):
        self.killed += 1


def _identity(pid=4242, start_time=100, command_line=None):
    return gateway.GatewayProcessIdentity(
        pid,
        start_time,
        command_line
        or "python -m hermes_cli.main gateway run --replace",
    )


def test_identity_aware_termination_signals_normal_exact_identity(monkeypatch):
    captured = _identity()
    live = _SignalRecorder()
    monkeypatch.setattr(
        gateway,
        "_read_live_gateway_process_identity",
        lambda _pid: (captured, live),
    )

    terminated = gateway.terminate_gateway_process_identities_strict([captured])

    assert terminated == (captured,)
    assert live.terminated == 1
    assert live.killed == 0


@pytest.mark.parametrize(
    "live",
    [
        _identity(start_time=101),
        _identity(command_line="python unrelated-worker.py"),
    ],
    ids=["recycled-pid", "changed-gateway-command"],
)
def test_identity_aware_termination_never_signals_changed_identity(monkeypatch, live):
    captured = _identity()
    signals = _SignalRecorder()
    monkeypatch.setattr(
        gateway,
        "_read_live_gateway_process_identity",
        lambda _pid: (live, signals),
    )

    with pytest.raises(gateway.GatewayProcessTerminationError, match="signal skipped"):
        gateway.terminate_gateway_process_identities_strict([captured])

    assert signals.terminated == 0
    assert signals.killed == 0


def test_identity_aware_termination_waits_for_delayed_graceful_exit(monkeypatch):
    captured = _identity()
    live = _SignalRecorder()
    checks = 0

    def pid_exists(_pid):
        nonlocal checks
        checks += 1
        return checks < 2

    monkeypatch.setattr(gateway, "_read_live_gateway_process_identity", lambda _pid: (captured, live))
    monkeypatch.setattr(status, "_pid_exists", pid_exists)
    monkeypatch.setattr(gateway.time, "sleep", lambda _seconds: None)

    terminated = gateway.terminate_gateway_process_identities_strict(
        [captured], graceful_timeout=1.0
    )

    assert terminated == (captured,)
    assert live.terminated == 1
    assert live.killed == 0


def test_identity_aware_termination_escalates_after_term_is_ignored(monkeypatch):
    captured = _identity()
    live = _SignalRecorder()
    alive = True

    def pid_exists(_pid):
        return alive

    def kill():
        nonlocal alive
        live.killed += 1
        alive = False

    live.kill = kill
    monkeypatch.setattr(gateway, "_read_live_gateway_process_identity", lambda _pid: (captured, live))
    monkeypatch.setattr(status, "_pid_exists", pid_exists)
    monkeypatch.setattr(gateway.time, "sleep", lambda _seconds: None)

    terminated = gateway.terminate_gateway_process_identities_strict(
        [captured], graceful_timeout=0.0, kill_timeout=1.0
    )

    assert terminated == (captured,)
    assert live.terminated == 1
    assert live.killed == 1


def test_identity_aware_termination_never_kills_changed_identity_after_term(
    monkeypatch,
):
    captured = _identity()
    changed = _identity(command_line="python unrelated-worker.py")
    live = _SignalRecorder()
    reads = 0

    def read_identity(_pid):
        nonlocal reads
        reads += 1
        return (captured if reads == 1 else changed), live

    monkeypatch.setattr(gateway, "_read_live_gateway_process_identity", read_identity)
    monkeypatch.setattr(status, "_pid_exists", lambda _pid: True)
    monkeypatch.setattr(gateway.time, "sleep", lambda _seconds: None)

    with pytest.raises(gateway.GatewayProcessTerminationError, match="changed"):
        gateway.terminate_gateway_process_identities_strict(
            [captured], graceful_timeout=0.0, kill_timeout=1.0
        )

    assert live.terminated == 1
    assert live.killed == 0


def test_all_profile_lifecycle_lock_is_host_wide_and_secure(tmp_path, monkeypatch):
    lock_path = tmp_path / "state" / "hermes" / "gateway-all-lifecycle.lock"
    monkeypatch.setattr(status, "_get_all_profile_lifecycle_lock_path", lambda: lock_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile-a"))

    handle = status.acquire_all_profile_lifecycle_lock(timeout=0.1)
    try:
        assert lock_path.exists()
        assert lock_path.stat().st_mode & 0o777 == 0o600
        assert lock_path.parent.stat().st_mode & 0o777 == 0o700
        assert lock_path.parent.parent != Path(tmp_path / "profile-a")
    finally:
        status.release_all_profile_lifecycle_lock()


def test_all_profile_lifecycle_lock_rejects_corrupt_record(tmp_path, monkeypatch):
    lock_path = tmp_path / "state" / "gateway-all-lifecycle.lock"
    lock_path.parent.mkdir(mode=0o700)
    lock_path.write_text("not-json", encoding="utf-8")
    lock_path.chmod(0o600)
    monkeypatch.setattr(status, "_get_all_profile_lifecycle_lock_path", lambda: lock_path)

    with pytest.raises(status.GatewayLifecycleLockError, match="corrupt"):
        status.acquire_all_profile_lifecycle_lock(timeout=0.0)


def test_all_profile_lifecycle_lock_times_out_while_owned(tmp_path, monkeypatch):
    fcntl = pytest.importorskip("fcntl")
    lock_path = tmp_path / "state" / "gateway-all-lifecycle.lock"
    lock_path.parent.mkdir(mode=0o700)
    lock_path.touch(mode=0o600)
    monkeypatch.setattr(status, "_get_all_profile_lifecycle_lock_path", lambda: lock_path)

    with lock_path.open("a+") as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(status.GatewayLifecycleLockError, match="timed out"):
            status.acquire_all_profile_lifecycle_lock(timeout=0.0)


def test_all_profile_lifecycle_lock_rejects_symlinked_path(tmp_path, monkeypatch):
    lock_path = tmp_path / "state" / "gateway-all-lifecycle.lock"
    target = tmp_path / "real.lock"
    lock_path.parent.mkdir(mode=0o700)
    target.touch(mode=0o600)
    lock_path.symlink_to(target)
    monkeypatch.setattr(status, "_get_all_profile_lifecycle_lock_path", lambda: lock_path)

    with pytest.raises(status.GatewayLifecycleLockError):
        status.acquire_all_profile_lifecycle_lock(timeout=0.0)


def test_all_profile_lifecycle_transaction_is_reentrant(monkeypatch):
    calls = []

    class _Lock:
        def __enter__(self):
            calls.append("acquire")

        def __exit__(self, *_args):
            calls.append("release")

    monkeypatch.setattr(gateway, "all_profile_lifecycle_lock", lambda **_kwargs: _Lock())
    with gateway._all_profile_lifecycle_transaction():
        with gateway._all_profile_lifecycle_transaction():
            calls.append("body")

    assert calls == ["acquire", "body", "release"]
