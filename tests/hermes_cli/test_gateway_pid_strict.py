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
