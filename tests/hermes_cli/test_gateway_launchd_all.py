"""Behavioral tests for macOS all-profile launchd lifecycle commands."""

import plistlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import hermes_cli.gateway as gateway_cli


def _write_gateway_plist(path: Path, label: str, hermes_home: Path) -> None:
    profile = label.removeprefix("ai.hermes.gateway-")
    arguments = ["/usr/bin/python3", "-m", "hermes_cli.main"]
    if profile != label:
        arguments.extend(["--profile", profile])
    arguments.extend(["gateway", "run", "--replace"])
    path.write_bytes(
        plistlib.dumps(
            {
                "Label": label,
                "ProgramArguments": arguments,
                "EnvironmentVariables": {"HERMES_HOME": str(hermes_home)},
            }
        )
    )


class _FakeLaunchd:
    def __init__(self, jobs: dict[str, dict], *, unmanaged_pids=()):
        self.jobs = jobs
        self.unmanaged_pids = set(unmanaged_pids)
        self.calls: list[list[str]] = []
        self.next_pid = 900

    def _job(self, target: str) -> dict | None:
        return self.jobs.get(target)

    def _complete(self, cmd: list[str], check: bool, *, returncode=0, stdout=""):
        result = SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")
        if check and returncode:
            raise subprocess.CalledProcessError(returncode, cmd)
        return result

    def run(self, cmd, check=False, **kwargs):
        cmd = list(cmd)
        self.calls.append(cmd)

        if cmd[:2] == ["launchctl", "managername"]:
            return self._complete(cmd, check, stdout="Aqua\n")

        if cmd[:2] == ["launchctl", "print"]:
            target = cmd[2]
            job = self._job(target)
            if job is None or not job["loaded"]:
                return self._complete(cmd, check, returncode=113)
            return self._complete(cmd, check, stdout=f"pid = {job['pid']}\n")

        if cmd[:2] == ["launchctl", "print-disabled"]:
            domain = cmd[2]
            lines = []
            for target, job in self.jobs.items():
                if target.startswith(f"{domain}/") and job["disabled"]:
                    lines.append(f'"{target.split("/", 2)[2]}" => true')
            return self._complete(cmd, check, stdout="\n".join(lines))

        if cmd[:2] == ["launchctl", "enable"]:
            self.jobs[cmd[2]]["disabled"] = False
            return self._complete(cmd, check)

        if cmd[:2] == ["launchctl", "disable"]:
            self.jobs[cmd[2]]["disabled"] = True
            return self._complete(cmd, check)

        if cmd[:2] == ["launchctl", "bootstrap"]:
            domain = cmd[2]
            plist = Path(cmd[3])
            target = f"{domain}/{plist.stem}"
            job = self.jobs.setdefault(
                target,
                {"loaded": False, "disabled": False, "pid": None, "start": None},
            )
            job["loaded"] = True
            self.next_pid += 1
            job["pid"] = self.next_pid
            job["start"] = self.next_pid * 10
            return self._complete(cmd, check)

        if cmd[:2] == ["launchctl", "kickstart"]:
            target = cmd[-1]
            job = self._job(target)
            if job is None or not job["loaded"]:
                return self._complete(cmd, check, returncode=113)
            self.next_pid += 1
            job["pid"] = self.next_pid
            job["start"] = self.next_pid * 10
            return self._complete(cmd, check)

        if cmd[:2] == ["launchctl", "bootout"]:
            target = cmd[2]
            if target in self.jobs:
                self.jobs[target]["loaded"] = False
            return self._complete(cmd, check)

        return self._complete(cmd, check)

    def is_live(self, pid: int, expected_start_time=None) -> bool:
        for job in self.jobs.values():
            if job["loaded"] and job["pid"] == pid:
                return expected_start_time is None or job["start"] == expected_start_time
        return False

    def start_time(self, pid: int | None) -> int | None:
        for job in self.jobs.values():
            if job["pid"] == pid:
                return job["start"]
        return None


@pytest.fixture
def launchd_env(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    root.mkdir()
    agents = tmp_path / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(gateway_cli, "_launchd_user_home", lambda: tmp_path)
    monkeypatch.setattr(gateway_cli.os, "getuid", lambda: 501)
    monkeypatch.setattr(gateway_cli, "is_macos", lambda: True)
    monkeypatch.setattr(gateway_cli, "is_windows", lambda: False)
    monkeypatch.setattr(gateway_cli, "is_termux", lambda: False)
    monkeypatch.setattr(gateway_cli, "supports_systemd_services", lambda: False)
    monkeypatch.setattr(
        gateway_cli, "_dispatch_all_via_service_manager_if_s6", lambda action: False
    )
    return root, agents


def _install_two_profiles(agents: Path, root: Path) -> tuple[Path, Path]:
    default = agents / "ai.hermes.gateway.plist"
    coder = agents / "ai.hermes.gateway-coder.plist"
    _write_gateway_plist(default, "ai.hermes.gateway", root)
    _write_gateway_plist(coder, "ai.hermes.gateway-coder", root / "profiles" / "coder")
    return default, coder


def _patch_launchd_sim(monkeypatch, sim: _FakeLaunchd) -> None:
    monkeypatch.setattr(gateway_cli.subprocess, "run", sim.run)
    monkeypatch.setattr(gateway_cli, "_launchd_pid_is_live", sim.is_live)
    monkeypatch.setattr(gateway_cli, "_launchd_process_start_time", sim.start_time)


def _job(domain: str, label: str, *, pid: int | None, disabled=False, loaded=True) -> tuple[str, dict]:
    return (
        f"{domain}/{label}",
        {"loaded": loaded, "disabled": disabled, "pid": pid, "start": pid * 10 if pid else None},
    )


def _replace_fake_pid(sim: _FakeLaunchd, old_pid: int) -> bool:
    for job in sim.jobs.values():
        if job["pid"] == old_pid:
            sim.next_pid += 1
            job["pid"] = sim.next_pid
            job["start"] = sim.next_pid * 10
            return True
    return False


def test_start_all_cycles_every_installed_target_in_its_own_domain_without_global_kill(
    launchd_env, monkeypatch
):
    root, agents = launchd_env
    _install_two_profiles(agents, root)
    sim = _FakeLaunchd(
        dict(
            [
                _job("gui/501", "ai.hermes.gateway", pid=101),
                _job("user/501", "ai.hermes.gateway-coder", pid=202),
            ]
        ),
        unmanaged_pids=(999,),
    )
    _patch_launchd_sim(monkeypatch, sim)
    monkeypatch.setattr(
        gateway_cli, "kill_gateway_processes", lambda **kwargs: pytest.fail("global kill")
    )

    gateway_cli.gateway_command(
        SimpleNamespace(gateway_command="start", all=True, system=False)
    )

    assert sorted([
        ["launchctl", "bootstrap", "gui/501", str(agents / "ai.hermes.gateway.plist")],
        [
            "launchctl",
            "bootstrap",
            "user/501",
            str(agents / "ai.hermes.gateway-coder.plist"),
        ],
    ]) == sorted([call for call in sim.calls if call[:2] == ["launchctl", "bootstrap"]])
    assert {
        call[2]
        for call in sim.calls
        if call[:2] == ["launchctl", "kickstart"]
    } == {
        "gui/501/ai.hermes.gateway",
        "user/501/ai.hermes.gateway-coder",
    }


def test_start_all_explicitly_releases_preexisting_fence_but_restart_preserves_it(
    launchd_env, monkeypatch
):
    root, agents = launchd_env
    _install_two_profiles(agents, root)
    sim = _FakeLaunchd(
        dict(
            [
                _job("gui/501", "ai.hermes.gateway", pid=101, disabled=True),
                _job("user/501", "ai.hermes.gateway-coder", pid=202),
            ]
        )
    )
    _patch_launchd_sim(monkeypatch, sim)
    monkeypatch.setattr(
        gateway_cli, "kill_gateway_processes", lambda **kwargs: pytest.fail("global kill")
    )
    monkeypatch.setattr(
        gateway_cli,
        "_graceful_restart_via_sigusr1",
        lambda pid, timeout: _replace_fake_pid(sim, pid),
    )

    gateway_cli.launchd_start_all()
    assert ["launchctl", "enable", "gui/501/ai.hermes.gateway"] in sim.calls

    sim.calls.clear()
    sim.jobs["gui/501/ai.hermes.gateway"]["disabled"] = True
    gateway_cli.launchd_restart_all()
    assert not any(
        call[:2] in (["launchctl", "enable"], ["launchctl", "kickstart"])
        and call[-1] == "gui/501/ai.hermes.gateway"
        for call in sim.calls
    )


@pytest.mark.parametrize("operation_name", ["launchd_start_all", "launchd_restart_all"])
def test_all_profile_zero_target_is_a_clean_noop(
    launchd_env, monkeypatch, operation_name
):
    operation = getattr(gateway_cli, operation_name)
    monkeypatch.setattr(
        gateway_cli, "kill_gateway_processes", lambda **kwargs: pytest.fail("global kill")
    )
    operation()


def test_restart_all_attempts_peers_and_aggregates_a_failed_target(
    launchd_env, monkeypatch
):
    root, agents = launchd_env
    _install_two_profiles(agents, root)
    sim = _FakeLaunchd(
        dict(
            [
                _job("gui/501", "ai.hermes.gateway", pid=101),
                _job("user/501", "ai.hermes.gateway-coder", pid=202),
            ]
        )
    )
    _patch_launchd_sim(monkeypatch, sim)
    restart_pids = []

    def fake_graceful(pid, timeout):
        restart_pids.append(pid)
        if pid == 101:
            return False
        job = sim.jobs["user/501/ai.hermes.gateway-coder"]
        sim.next_pid += 1
        job["pid"] = sim.next_pid
        job["start"] = sim.next_pid * 10
        return True

    monkeypatch.setattr(gateway_cli, "_graceful_restart_via_sigusr1", fake_graceful)
    monkeypatch.setattr(
        gateway_cli, "kill_gateway_processes", lambda **kwargs: pytest.fail("global kill")
    )

    with pytest.raises(gateway_cli.LaunchdAllOperationError) as excinfo:
        gateway_cli.launchd_restart_all()

    assert set(restart_pids) == {101, 202}
    assert "gui/501/ai.hermes.gateway" in str(excinfo.value)
    assert sim.jobs["user/501/ai.hermes.gateway-coder"]["pid"] != 202


def test_restart_all_requires_a_new_live_supervised_pid(launchd_env, monkeypatch):
    root, agents = launchd_env
    default, coder = _install_two_profiles(agents, root)
    coder.unlink()
    sim = _FakeLaunchd(
        {"gui/501/ai.hermes.gateway": dict(_job("gui/501", "ai.hermes.gateway", pid=101)[1])}
    )
    _patch_launchd_sim(monkeypatch, sim)
    monkeypatch.setattr(
        gateway_cli, "_launchd_all_wait_for_successor", lambda *a, **k: False
    )
    monkeypatch.setattr(
        gateway_cli, "_graceful_restart_via_sigusr1", lambda pid, timeout: True
    )
    monkeypatch.setattr(
        gateway_cli,
        "_launchd_all_preflight_targets",
        lambda: (
            gateway_cli.LaunchdAllTarget(
                label="ai.hermes.gateway",
                domain="gui/501",
                plist_path=default,
                plist_fingerprint="fingerprint",
                hermes_home=root,
                was_disabled=False,
                was_loaded=True,
                pid=101,
                start_time=1010,
            ),
        ),
    )

    with pytest.raises(gateway_cli.LaunchdAllOperationError):
        gateway_cli.launchd_restart_all()


def test_start_all_revalidates_stale_plist_before_bootstrap_and_attempts_peer(
    launchd_env, monkeypatch
):
    root, agents = launchd_env
    default, coder = _install_two_profiles(agents, root)
    sim = _FakeLaunchd(
        dict(
            [
                _job("gui/501", "ai.hermes.gateway", pid=None, loaded=False),
                _job("user/501", "ai.hermes.gateway-coder", pid=202, loaded=True),
            ]
        )
    )
    _patch_launchd_sim(monkeypatch, sim)
    original_preflight = gateway_cli._launchd_all_preflight_targets

    def preflight_then_stale():
        targets = original_preflight()
        document = plistlib.loads(default.read_bytes())
        document["Comment"] = "changed after preflight"
        default.write_bytes(plistlib.dumps(document))
        return targets

    monkeypatch.setattr(gateway_cli, "_launchd_all_preflight_targets", preflight_then_stale)

    with pytest.raises(gateway_cli.LaunchdAllOperationError):
        gateway_cli.launchd_start_all()

    assert ["launchctl", "bootstrap", "user/501", str(coder)] in sim.calls, sim.calls
    assert ["launchctl", "bootstrap", "gui/501", str(default)] not in sim.calls
    assert ["launchctl", "enable", "gui/501/ai.hermes.gateway"] not in sim.calls


def test_all_profile_preflight_validates_every_plist_before_mutation(launchd_env, monkeypatch):
    root, agents = launchd_env
    _write_gateway_plist(agents / "ai.hermes.gateway.plist", "ai.hermes.gateway", root)
    (agents / "ai.hermes.gateway-coder.plist").write_bytes(b"not a plist")
    sim = _FakeLaunchd(
        {"gui/501/ai.hermes.gateway": dict(_job("gui/501", "ai.hermes.gateway", pid=101)[1])}
    )
    _patch_launchd_sim(monkeypatch, sim)

    with pytest.raises(gateway_cli.LaunchdAllOperationError):
        gateway_cli.launchd_start_all()

    assert not any(
        call[:2]
        in (["launchctl", "enable"], ["launchctl", "bootstrap"], ["launchctl", "kickstart"])
        for call in sim.calls
    )
