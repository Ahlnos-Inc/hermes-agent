from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import gateway
from gateway import status


def _sigusr1():
    value = getattr(signal, "SIGUSR1", None)
    assert value is not None
    return value


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

    assert gateway.find_gateway_pids_strict(all_profiles=True) == [4242, 4343]


def test_strict_runtime_only_policy_excludes_restart_manager_without_systemd(
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

    assert gateway.find_gateway_pids_strict(
        all_profiles=True, include_restart_managers=False
    ) == [4343]


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


@pytest.mark.parametrize(
    "argv",
    [
        (
            "/usr/bin/python3",
            "-m",
            "foreign.wrapper",
            "hermes_cli.main",
            "gateway",
            "run",
        ),
        (
            "/usr/bin/python3",
            "-c",
            "import hermes_cli.main",
            "gateway",
            "run",
        ),
        (
            "/usr/bin/python3",
            "-m",
            "foreign.gateway.run",
        ),
        ("/bin/sh", "-c", "python -m hermes_cli.main gateway run"),
        ("/opt/foreign-hermes", "gateway", "run"),
        ("/usr/bin/python3", "--script", "foreign/gateway/run.py"),
        ("foreign", r"C:\\Program Files\\Hermes\\Hermes.EXE", "gateway", "run"),
        ("hermes", "chat", "gateway", "run"),
        ("hermes", "mcp", "servers", "gateway", "run"),
        ("python", "-m", "hermes_cli/main.py", "gateway", "run"),
    ],
)
def test_canonical_classifier_rejects_decoy_entrypoint_tokens(argv):
    role, profile, home = gateway.classify_gateway_argv(argv)

    assert role is gateway.GatewayRuntimeRole.FOREIGN
    assert profile is None
    assert home is None


@pytest.mark.parametrize(
    "argv",
    [
        ("hermes", "foo", "gateway", "run"),
        ("hermes", "--help", "gateway", "run"),
        ("hermes", "--profile", "x", "nonsense", "gateway", "run"),
        ("python", "-m", "hermes_cli.main", "garbage", "gateway", "run"),
        ("python", "-m", "hermes_cli/main.py", "garbage", "gateway", "run"),
        ("python", "hermes_cli/main.py", "garbage", "gateway", "run"),
        ("hermes_cli/main.py", "dashboard", "gateway", "run"),
        ("hermes", "--profile", "x", "--profile", "y", "gateway", "run"),
        ("hermes", "--profile", "x", "--profile", "x", "gateway", "run"),
        ("hermes", "--profile=bad.profile", "gateway", "run"),
        ("hermes", "gateway", "run", "--", "--profile", "x"),
        ("python", "-m", "hermes_cli.main", "gateway", "run", "--", "-p", "x"),
    ],
    ids=[
        "foreign-prefix",
        "help-prefix",
        "profile-then-foreign-prefix",
        "module-foreign-prefix",
        "historical-module-foreign-prefix",
        "script-foreign-prefix",
        "direct-script-foreign-prefix",
        "multiple-conflicting-profiles",
        "multiple-identical-profiles",
        "invalid-profile",
        "profile-after-passthrough",
        "short-profile-after-passthrough",
    ],
)
def test_canonical_classifier_rejects_non_profile_cli_prefixes(argv):
    role, profile, home = gateway.classify_gateway_argv(argv)

    assert role is gateway.GatewayRuntimeRole.FOREIGN
    assert profile is None
    assert home is None


@pytest.mark.parametrize(
    ("argv", "expected_role", "expected_profile"),
    [
        (("hermes", "gateway", "run"), gateway.GatewayRuntimeRole.RUNTIME, None),
        (("hermes", "--profile", "x", "gateway", "run"), gateway.GatewayRuntimeRole.RUNTIME, "x"),
        (("hermes", "--profile=x", "gateway", "run"), gateway.GatewayRuntimeRole.RUNTIME, "x"),
        (("hermes", "-p", "x", "gateway", "restart"), gateway.GatewayRuntimeRole.MANAGER, "x"),
        (("python", "-m", "hermes_cli.main", "--profile=x", "gateway", "run"), gateway.GatewayRuntimeRole.RUNTIME, "x"),
        (("python", "-m", "hermes_cli.main", "-p", "x", "gateway", "run"), gateway.GatewayRuntimeRole.RUNTIME, "x"),
        (("python", "hermes_cli/main.py", "--profile", "x", "gateway", "run"), gateway.GatewayRuntimeRole.RUNTIME, "x"),
        (("hermes_cli/main.py", "--profile=x", "gateway", "run"), gateway.GatewayRuntimeRole.RUNTIME, "x"),
    ],
    ids=[
        "default-hermes",
        "hermes-long-profile",
        "hermes-inline-profile",
        "hermes-short-profile-manager",
        "module-inline-profile",
        "module-spaced-profile",
        "script-spaced-profile",
        "direct-script-inline-profile",
    ],
)
def test_canonical_classifier_accepts_profile_selectors_before_gateway(
    argv, expected_role, expected_profile
):
    role, profile, _home = gateway.classify_gateway_argv(argv)

    assert role is expected_role
    assert profile == expected_profile


@pytest.mark.parametrize(
    "argv",
    [
        ("hermes", "gateway", "--profile", "x", "run"),
        ("hermes", "gateway", "run", "--profile", "x"),
        ("hermes", "gateway", "run", "--profile=x"),
        ("python", "-m", "hermes_cli.main", "gateway", "run", "-p", "x"),
    ],
    ids=[
        "between-gateway-and-subcommand",
        "after-runtime-subcommand",
        "inline-after-runtime-subcommand",
        "module-after-runtime-subcommand",
    ],
)
def test_canonical_classifier_accepts_profile_selectors_after_gateway(argv):
    role, profile, home = gateway.classify_gateway_argv(argv)

    assert role is gateway.GatewayRuntimeRole.RUNTIME
    assert profile == "x"
    assert home is None


@pytest.mark.parametrize(
    "argv",
    [
        ("hermes", "gateway"),
        ("python", "-m", "hermes_cli.main", "gateway"),
        ("python", "hermes_cli/main.py", "gateway"),
        ("hermes_cli/main.py", "gateway"),
    ],
    ids=[
        "hermes",
        "module",
        "python-script",
        "direct-script",
    ],
)
def test_canonical_classifier_defaults_bare_gateway_to_run(argv):
    role, profile, home = gateway.classify_gateway_argv(argv)

    assert role is gateway.GatewayRuntimeRole.RUNTIME
    assert profile is None
    assert home is None


def test_canonical_classifier_accepts_one_leading_hermes_home_assignment(tmp_path):
    home = tmp_path / "profile-home"
    command_line = f"HERMES_HOME={home} hermes gateway run"

    role, profile, resolved_home = gateway.classify_gateway_argv(command_line)

    assert role is gateway.GatewayRuntimeRole.RUNTIME
    assert profile is None
    assert resolved_home == home.resolve()

    identity = gateway.GatewayProcessIdentity(4242, 100, command_line)
    assert identity.exact_argv == (
        f"HERMES_HOME={home}",
        "hermes",
        "gateway",
        "run",
    )


@pytest.mark.parametrize(
    "command_line",
    [
        "FOO=bar hermes gateway run",
        "env HERMES_HOME=/tmp/hermes hermes gateway run",
        "HERMES_HOME=/tmp/hermes FOO=bar hermes gateway run",
        "HERMES_HOME=relative hermes gateway run",
        "hermes gateway run HERMES_HOME=/tmp/hermes",
    ],
    ids=[
        "foreign-env-assignment",
        "env-wrapper",
        "second-env-assignment",
        "relative-home",
        "trailing-home-assignment",
    ],
)
def test_canonical_classifier_rejects_noncanonical_environment_prefixes(command_line):
    role, profile, home = gateway.classify_gateway_argv(command_line)

    assert role is gateway.GatewayRuntimeRole.FOREIGN
    assert profile is None
    assert home is None


@pytest.mark.parametrize(
    "argv",
    [
        ("/usr/bin/python3", "-m", "hermes_cli.main", "gateway", "run"),
        ("/usr/bin/python3", "gateway/run.py"),
        ("/opt/hermes/hermes_cli/main.py", "gateway", "run"),
        ("/usr/local/bin/hermes", "gateway", "run"),
        (r"C:\\Program Files\\Hermes\\Hermes.EXE", "gateway", "run"),
        ("/usr/local/bin/hermes-gateway",),
    ],
)
def test_canonical_classifier_accepts_supported_entrypoint_shapes(argv):
    role, _profile, _home = gateway.classify_gateway_argv(argv)

    assert role is gateway.GatewayRuntimeRole.RUNTIME


def test_canonical_classifier_reassembles_unquoted_windows_hermes_path():
    role, _profile, _home = gateway.classify_gateway_argv(
        r"C:\\Program Files\\Hermes\\Hermes.EXE gateway run --replace"
    )

    assert role is gateway.GatewayRuntimeRole.RUNTIME
    foreign_role, _profile, _home = gateway.classify_gateway_argv(
        r"foreign C:\\Program Files\\Hermes\\Hermes.EXE gateway run"
    )
    assert foreign_role is gateway.GatewayRuntimeRole.FOREIGN


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


def test_sigusr1_canonical_restart_signals_revalidated_process_handle(monkeypatch):
    sent = []

    class _Process:
        def send_signal(self, sig):
            sent.append(sig)

    identity = _identity()
    monkeypatch.setattr(
        gateway,
        "_revalidate_gateway_process_identity",
        lambda _identity: _Process(),
    )
    monkeypatch.setattr(
        gateway.os,
        "kill",
        lambda *_args: pytest.fail("canonical SIGUSR1 must not use raw os.kill"),
    )
    monkeypatch.setattr(
        gateway,
        "_wait_for_exact_gateway_identity_exit",
        lambda _identity, _timeout: True,
    )

    assert gateway._graceful_restart_via_sigusr1(
        identity.pid, 1.0, expected_identity=identity
    ) is True
    assert sent == [_sigusr1()]


def test_sigusr1_canonical_restart_revalidation_failure_does_not_signal(monkeypatch):
    monkeypatch.setattr(
        gateway,
        "_revalidate_gateway_process_identity",
        lambda _identity: (_ for _ in ()).throw(
            gateway.GatewayProcessTerminationError(["identity changed"])
        ),
    )
    monkeypatch.setattr(
        gateway.os,
        "kill",
        lambda *_args: pytest.fail("changed canonical identity must not be signalled"),
    )

    identity = _identity()
    assert gateway._graceful_restart_via_sigusr1(
        identity.pid, 1.0, expected_identity=identity
    ) is False


def test_sigusr1_without_identity_captures_and_signals_exact_handle(monkeypatch):
    sent = []

    class _Process:
        def send_signal(self, sig):
            sent.append(sig)

    identity = _identity()
    process = _Process()
    monkeypatch.setattr(
        gateway,
        "_capture_gateway_process_identity",
        lambda _pid, **_kwargs: identity,
    )
    monkeypatch.setattr(
        gateway,
        "_revalidate_gateway_process_identity",
        lambda _identity: process,
    )
    monkeypatch.setattr(
        gateway.os,
        "kill",
        lambda *_args: pytest.fail("generic SIGUSR1 must not use raw os.kill"),
    )
    monkeypatch.setattr(
        gateway,
        "_wait_for_exact_gateway_identity_exit",
        lambda _identity, _timeout: True,
    )

    assert gateway._graceful_restart_via_sigusr1(identity.pid, 1.0) is True
    assert sent == [_sigusr1()]


def test_sigusr1_identity_replacement_between_capture_and_signal_fails_closed(
    monkeypatch,
):
    identity = _identity()
    monkeypatch.setattr(
        gateway,
        "_capture_gateway_process_identity",
        lambda _pid, **_kwargs: identity,
    )
    monkeypatch.setattr(
        gateway,
        "_revalidate_gateway_process_identity",
        lambda _identity: (_ for _ in ()).throw(
            gateway.GatewayProcessTerminationError(["identity changed"])
        ),
    )
    monkeypatch.setattr(
        gateway.os,
        "kill",
        lambda *_args: pytest.fail("replaced PID must not use raw os.kill"),
    )

    assert gateway._graceful_restart_via_sigusr1(identity.pid, 1.0) is False


def test_sigusr1_identity_access_denial_does_not_signal(monkeypatch):
    identity = _identity()
    monkeypatch.setattr(
        gateway,
        "_capture_gateway_process_identity",
        lambda _pid, **_kwargs: (_ for _ in ()).throw(
            gateway.GatewayProcessTerminationError(["permission denied"])
        ),
    )
    monkeypatch.setattr(
        gateway.os,
        "kill",
        lambda *_args: pytest.fail("denied identity must not use raw os.kill"),
    )

    assert gateway._graceful_restart_via_sigusr1(identity.pid, 1.0) is False


def test_sigusr1_identity_already_gone_is_converged_without_signal(monkeypatch):
    identity = _identity()
    monkeypatch.setattr(
        gateway,
        "_capture_gateway_process_identity",
        lambda _pid, **_kwargs: None,
    )
    monkeypatch.setattr(
        gateway.os,
        "kill",
        lambda *_args: pytest.fail("gone identity must not use raw os.kill"),
    )

    assert gateway._graceful_restart_via_sigusr1(identity.pid, 1.0) is True


def test_launchd_self_sigusr1_uses_revalidated_handle(monkeypatch):
    sent = []
    identity = _identity()

    monkeypatch.setattr(gateway, "_is_pid_ancestor_of_current_process", lambda _pid: True)
    monkeypatch.setattr(
        gateway,
        "_capture_gateway_process_identity",
        lambda _pid, **_kwargs: identity,
    )
    monkeypatch.setattr(
        gateway,
        "_revalidate_gateway_process_identity",
        lambda _identity: SimpleNamespace(send_signal=sent.append),
    )
    monkeypatch.setattr(
        gateway.os,
        "kill",
        lambda *_args: pytest.fail("launchd SIGUSR1 must not use raw os.kill"),
    )

    assert gateway._request_gateway_self_restart(identity.pid) is True
    assert sent == [_sigusr1()]


def test_identity_signal_for_term_uses_revalidated_handle(monkeypatch):
    sent = []
    identity = _identity()
    monkeypatch.setattr(
        gateway,
        "_revalidate_gateway_process_identity",
        lambda _identity: SimpleNamespace(send_signal=sent.append),
    )
    monkeypatch.setattr(
        gateway.os,
        "kill",
        lambda *_args: pytest.fail("identity TERM must not use raw os.kill"),
    )

    assert gateway._signal_gateway_process_identity(identity, signal.SIGTERM) == "signalled"
    assert sent == [signal.SIGTERM]


def test_strict_identity_inventory_captures_allowed_no_supervisor_manager(
    monkeypatch,
):
    manager = gateway.GatewayProcessIdentity(
        708,
        7080,
        "python -m hermes_cli.main gateway restart",
    )
    monkeypatch.setattr(gateway, "supports_systemd_services", lambda: False)
    monkeypatch.setattr(
        gateway,
        "find_gateway_pids_strict",
        lambda **_kwargs: [manager.pid],
    )
    seen_roles = []

    def read_identity(_pid, **kwargs):
        seen_roles.append(kwargs["allowed_roles"])
        return manager, object()

    monkeypatch.setattr(gateway, "_read_live_gateway_process_identity", read_identity)

    identities = gateway.find_gateway_process_identities_strict(
        all_profiles=True,
        include_restart_managers=True,
    )

    assert identities == [manager]
    assert gateway.GatewayRuntimeRole.MANAGER in seen_roles[0]


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

    state = status._all_profile_lifecycle_lock_context()
    assert state.handle is None
    assert state.depth == 0
    assert state.owner is None


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


def test_all_profile_lifecycle_lock_reads_owner_record_only_after_acquire(
    tmp_path, monkeypatch
):
    lock_path = tmp_path / "state" / "gateway-all-lifecycle.lock"
    lock_path.parent.mkdir(mode=0o700)
    lock_path.write_text('{"pid":', encoding="utf-8")
    lock_path.chmod(0o600)
    monkeypatch.setattr(status, "_get_all_profile_lifecycle_lock_path", lambda: lock_path)

    def acquire_after_writer(handle):
        lock_path.write_text('{"pid": 1234}', encoding="utf-8")
        return True

    monkeypatch.setattr(status, "_try_acquire_file_lock", acquire_after_writer)
    handle = status.acquire_all_profile_lifecycle_lock(timeout=0.0)
    try:
        assert json.loads(lock_path.read_text(encoding="utf-8"))["pid"] == os.getpid()
    finally:
        status.release_all_profile_lifecycle_lock()


def test_all_profile_lifecycle_lock_nested_context_releases_only_at_outer_exit(
    tmp_path, monkeypatch
):
    lock_path = tmp_path / "state" / "gateway-all-lifecycle.lock"
    monkeypatch.setattr(status, "_get_all_profile_lifecycle_lock_path", lambda: lock_path)

    with status.all_profile_lifecycle_lock(timeout=0.0):
        assert status._all_profile_lifecycle_lock_context().depth == 1
        with status.all_profile_lifecycle_lock(timeout=0.0):
            state = status._all_profile_lifecycle_lock_context()
            assert state.handle is not None
            assert state.depth == 2
        assert status._all_profile_lifecycle_lock_context().handle is not None
        assert status._all_profile_lifecycle_lock_context().depth == 1
    assert status._all_profile_lifecycle_lock_context().handle is None
    assert status._all_profile_lifecycle_lock_context().depth == 0
    assert status._all_profile_lifecycle_lock_context().owner is None

    with pytest.raises(RuntimeError, match="body failure"):
        with status.all_profile_lifecycle_lock(timeout=0.0):
            raise RuntimeError("body failure")
    state = status._all_profile_lifecycle_lock_context()
    assert state.handle is None
    assert state.depth == 0
    assert state.owner is None


def test_all_profile_lifecycle_lock_excludes_different_thread(tmp_path, monkeypatch):
    lock_path = tmp_path / "state" / "gateway-all-lifecycle.lock"
    monkeypatch.setattr(status, "_get_all_profile_lifecycle_lock_path", lambda: lock_path)
    result = {}

    with status.all_profile_lifecycle_lock(timeout=0.0):
        def contender():
            try:
                status.acquire_all_profile_lifecycle_lock(timeout=0.0)
            except status.GatewayLifecycleLockError as exc:
                result["error"] = str(exc)
            else:
                result["acquired"] = True
                status.release_all_profile_lifecycle_lock()

        thread = threading.Thread(target=contender)
        thread.start()
        thread.join(timeout=1.0)

    assert not thread.is_alive()
    assert "timed out" in result["error"]
    assert "acquired" not in result


def test_all_profile_lifecycle_lock_async_siblings_cannot_nest_or_release(
    tmp_path, monkeypatch
):
    lock_path = tmp_path / "state" / "gateway-all-lifecycle.lock"
    monkeypatch.setattr(status, "_get_all_profile_lifecycle_lock_path", lambda: lock_path)

    async def exercise():
        holder_ready = asyncio.Event()
        child_checked = asyncio.Event()
        holder_released = asyncio.Event()
        result = {}

        async def child_created_inside_holder():
            await holder_ready.wait()
            try:
                status.acquire_all_profile_lifecycle_lock(timeout=0.0)
            except status.GatewayLifecycleLockError as exc:
                result["acquire_error"] = str(exc)
            else:
                result["acquired_while_held"] = True
                status.release_all_profile_lifecycle_lock()

            try:
                status.release_all_profile_lifecycle_lock()
            except status.GatewayLifecycleLockError as exc:
                result["release_error"] = str(exc)
            else:
                result["released_while_held"] = True
            child_checked.set()

            await holder_released.wait()
            handle = status.acquire_all_profile_lifecycle_lock(timeout=0.0)
            result["acquired_after_release"] = handle is not None
            status.release_all_profile_lifecycle_lock()

        with status.all_profile_lifecycle_lock(timeout=0.0):
            child = asyncio.create_task(child_created_inside_holder())
            holder_ready.set()
            await child_checked.wait()
            state = status._all_profile_lifecycle_lock_context()
            assert state.depth == 1
            assert state.owner.task is asyncio.current_task()

        holder_released.set()
        await child
        return result

    result = asyncio.run(exercise())
    assert "another execution context" in result["acquire_error"]
    assert "another execution context" in result["release_error"]
    assert "acquired_while_held" not in result
    assert "released_while_held" not in result
    assert result["acquired_after_release"] is True
    state = status._all_profile_lifecycle_lock_context()
    assert state.handle is None
    assert state.depth == 0
    assert state.owner is None


def test_gateway_all_lifecycle_lock_override_does_not_use_hermes_home(
    tmp_path, monkeypatch
):
    override = tmp_path / "isolated-locks"
    hermes_home = tmp_path / "profile-home"
    monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(override))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    host_path = Path.home() / ".local" / "state" / "hermes" / "gateway-all-lifecycle.lock"

    def snapshot(path):
        try:
            metadata = path.stat()
        except FileNotFoundError:
            return (False, None, None, None)
        return (True, metadata.st_ino, metadata.st_mtime_ns, path.read_bytes())

    host_before = snapshot(host_path)
    calls = []
    monkeypatch.setattr(
        gateway,
        "_gateway_command_inner",
        lambda _args: calls.append("inner"),
    )

    gateway.gateway_command(SimpleNamespace(gateway_command="start", all=True))

    assert calls == ["inner"]
    assert (override / "gateway-all-lifecycle.lock").exists()
    assert not (hermes_home / "gateway-all-lifecycle.lock").exists()
    assert snapshot(host_path) == host_before


def test_gateway_all_lifecycle_entrypoint_uses_status_lock(monkeypatch):
    calls = []

    class _Lock:
        def __enter__(self):
            calls.append("acquire")

        def __exit__(self, *_args):
            calls.append("release")

    monkeypatch.setattr(gateway, "all_profile_lifecycle_lock", lambda **_kwargs: _Lock())
    monkeypatch.setattr(
        gateway,
        "_launchd_start_all_locked",
        lambda: calls.append("locked") or "result",
    )

    assert gateway.launchd_start_all() == "result"
    assert calls == ["acquire", "locked", "release"]


def test_build_472_gateway_identity_all_entry_point_semantics():
    from gateway.process_identity import classify_gateway_argv

    def kind(argv, **kwargs):
        role, _profile, _home = classify_gateway_argv(argv, **kwargs)
        return role.value

    assert kind(["hermes-gateway"]) == "runtime"
    assert kind(["hermes-gateway", "run"]) == "runtime"
    for verb in ("install", "uninstall", "start", "stop", "restart", "status"):
        assert kind(["hermes-gateway", verb]) == "manager"
    assert kind(["hermes-gateway", "unknown"]) == "foreign"

    assert kind(["python", "-m", "gateway.run"]) == "runtime"
    assert kind(
        [
            "python",
            "gateway/run.py",
            "--config",
            "x",
            "--config=y",
            "-c",
            "z",
            "--verbose",
            "-v",
        ]
    ) == "runtime"
    assert kind(["python", "-m", "gateway.run", "--profile", "p"]) == "foreign"
    assert kind(["python", "-m", "gateway.run", "--unknown"]) == "foreign"
    assert kind(["python", "-m", "gateway.run", "positional"]) == "foreign"


def test_build_472_cli_prefix_and_profile_grammar():
    from gateway.process_identity import classify_gateway_argv

    def kind(argv, **kwargs):
        role, _profile, _home = classify_gateway_argv(argv, **kwargs)
        return role.value

    assert kind(["hermes", "--accept-hooks", "--model", "X", "gateway"]) == "runtime"
    assert kind(["hermes", "--model=X", "-m", "Y", "--tui", "gateway"]) == "runtime"
    assert kind(["hermes", "--skills", "a", "--skills=b", "-p", "one", "gateway"]) == "runtime"
    assert kind(["hermes", "gateway", "-p", "one"]) == "runtime"
    assert kind(["hermes", "-p", "one", "--profile=two", "gateway"]) == "foreign"
    assert kind(["hermes", "-p=x", "gateway"]) == "foreign"
    assert kind(["hermes", "--unknown", "gateway"]) == "foreign"
    assert kind(["hermes", "--model", "gateway"]) == "foreign"
    assert kind(["hermes", "-c", "gateway", "run"]) == "foreign"
    assert kind(["hermes", "--model", "gateway", "--profile"]) == "foreign"


def test_build_472_windows_prefix_and_home_consistency():
    from gateway.process_identity import _coerce_argv, classify_gateway_argv

    prefixed = _coerce_argv(
        r"HERMES_HOME=C:\hermes C:\Program Files\Hermes\Hermes.EXE gateway"
    )
    assert prefixed[0] == r"HERMES_HOME=C:\hermes"
    assert prefixed[1].lower() == r"c:\program files\hermes\hermes.exe"

    no_prefix = _coerce_argv(r"C:\Program Files\Hermes\Hermes.EXE gateway")
    assert no_prefix[0].lower() == r"c:\program files\hermes\hermes.exe"

    role, profile, home = classify_gateway_argv(
        ["hermes", "gateway"],
        environment={"HERMES_HOME": "/tmp/b"},
    )
    assert role.value == "runtime"
    assert profile is None
    assert home == Path("/tmp/b").resolve()

    role, profile, home = classify_gateway_argv(
        ["HERMES_HOME=/tmp/a", "hermes", "gateway"],
        environment={"HERMES_HOME": "/tmp/b"},
    )
    assert role.value == "foreign"
    assert profile is None
    assert home is None

    role, profile, home = classify_gateway_argv(
        ["hermes", "-p", "a", "gateway"],
        environment={"HERMES_HOME": "/tmp/root/profiles/a"},
    )
    assert role.value == "runtime"
    assert profile == "a"
    assert home == Path("/tmp/root/profiles/a").resolve()

    role, profile, home = classify_gateway_argv(
        ["hermes", "-p", "a", "gateway"],
        default_home=Path("/tmp/root"),
    )
    assert role.value == "runtime"
    assert profile == "a"
    assert home == Path("/tmp/root/profiles/a").resolve()

    role, profile, home = classify_gateway_argv(
        ["hermes", "-p", "a", "gateway"],
        environment={"HERMES_HOME": "/tmp/root/profiles/b"},
    )
    assert role.value == "foreign"
    assert profile is None
    assert home is None


def test_read_identity_reports_zombie_argv_denial_as_gone(monkeypatch):
    # macOS KERN_PROCARGS2 returns EINVAL (psutil AccessDenied) for zombie
    # processes; a zombie has exited and must read as gone, not unreadable.
    import psutil

    class _ZombieProcess:
        def cmdline(self):
            raise psutil.AccessDenied(4242)

        def environ(self):
            raise psutil.AccessDenied(4242)

        def status(self):
            return psutil.STATUS_ZOMBIE

    monkeypatch.setattr(gateway, "_psutil_process", lambda _pid: _ZombieProcess())
    monkeypatch.setattr(gateway, "_launchd_process_start_time", lambda _pid: 42)

    with pytest.raises(ProcessLookupError):
        gateway._read_live_gateway_process_identity(4242)


def test_read_identity_still_reports_live_argv_denial_as_permission(monkeypatch):
    import psutil

    class _ProtectedProcess:
        def cmdline(self):
            raise psutil.AccessDenied(4242)

        def environ(self):
            raise psutil.AccessDenied(4242)

        def status(self):
            return "running"

    monkeypatch.setattr(gateway, "_psutil_process", lambda _pid: _ProtectedProcess())
    monkeypatch.setattr(gateway, "_launchd_process_start_time", lambda _pid: 42)

    with pytest.raises(PermissionError):
        gateway._read_live_gateway_process_identity(4242)


def _raise_unreadable_identity(_identity):
    raise gateway.GatewayProcessIdentityUnreadableError(
        ["PID 4242: permission denied while revalidating identity"]
    )


def test_wait_for_exit_treats_unreadable_gone_birth_identity_as_exited(monkeypatch):
    # macOS teardown race: a draining PID's argv read fails with
    # sysctl(KERN_PROCARGS2) EINVAL before the PID disappears.
    identity = _identity()
    monkeypatch.setattr(status, "_pid_exists", lambda _pid: True)
    monkeypatch.setattr(
        gateway, "_revalidate_gateway_process_identity", _raise_unreadable_identity
    )
    monkeypatch.setattr(gateway, "_launchd_process_start_time", lambda _pid: None)

    assert gateway._wait_for_exact_gateway_identity_exit(identity, 1.0) is True


def test_wait_for_exit_treats_unreadable_reused_pid_as_exited(monkeypatch):
    identity = _identity()
    monkeypatch.setattr(status, "_pid_exists", lambda _pid: True)
    monkeypatch.setattr(
        gateway, "_revalidate_gateway_process_identity", _raise_unreadable_identity
    )
    monkeypatch.setattr(
        gateway,
        "_launchd_process_start_time",
        lambda _pid: identity.start_time + 1,
    )

    assert gateway._wait_for_exact_gateway_identity_exit(identity, 1.0) is True


def test_wait_for_exit_stays_red_when_unreadable_live_identity_persists(monkeypatch):
    identity = _identity()
    monkeypatch.setattr(status, "_pid_exists", lambda _pid: True)
    monkeypatch.setattr(
        gateway, "_revalidate_gateway_process_identity", _raise_unreadable_identity
    )
    monkeypatch.setattr(
        gateway,
        "_launchd_process_start_time",
        lambda _pid: identity.start_time,
    )

    with pytest.raises(gateway.GatewayProcessTerminationError, match="permission denied"):
        gateway._wait_for_exact_gateway_identity_exit(identity, 0.0)
