from __future__ import annotations

import subprocess

import pytest

from hermes_cli import gateway


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
