"""Behavioral tests for macOS all-profile launchd lifecycle commands."""

import argparse
import hashlib
import os
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
                "RunAtLoad": True,
                "KeepAlive": True,
            }
        )
    )


class _FakeLaunchd:
    def __init__(
        self,
        jobs: dict[str, dict],
        *,
        unmanaged_pids=(),
        bootstrap_exit5_after_load=(),
        bootstrap_without_pid=(),
        pid_appears_before_kickstart=(),
        bootout_failures=(),
        unknown_print_domains=(),
        unknown_disabled_domains=(),
        kickstart_failures=(),
        manager_name="Aqua",
    ):
        self.jobs = jobs
        self.unmanaged_pids = set(unmanaged_pids)
        self.bootstrap_exit5_after_load = set(bootstrap_exit5_after_load)
        self.bootstrap_without_pid = set(bootstrap_without_pid)
        self.pid_appears_before_kickstart = set(pid_appears_before_kickstart)
        self.bootout_failures = set(bootout_failures)
        self.unknown_print_domains = set(unknown_print_domains)
        self.unknown_disabled_domains = set(unknown_disabled_domains)
        self.kickstart_failures = set(kickstart_failures)
        self.manager_name = manager_name
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
            return self._complete(cmd, check, stdout=f"{self.manager_name}\n")

        if cmd[:2] == ["launchctl", "print"]:
            domain = cmd[2].rsplit("/", 1)[0]
            if domain in self.unknown_print_domains:
                return self._complete(cmd, check, returncode=5, stdout="EIO")
            target = cmd[2]
            job = self._job(target)
            if job is None or not job["loaded"]:
                return self._complete(cmd, check, returncode=113)
            return self._complete(cmd, check, stdout=f"pid = {job['pid']}\n")

        if cmd[:2] == ["launchctl", "print-disabled"]:
            domain = cmd[2]
            if domain in self.unknown_disabled_domains:
                return self._complete(cmd, check, returncode=5, stdout="EIO")
            lines = []
            for target, job in self.jobs.items():
                if target.startswith(f"{domain}/") and job["disabled"]:
                    lines.append(f'"{target.split("/", 2)[2]}" => true')
            return self._complete(cmd, check, stdout="\n".join(lines))

        if cmd[:2] == ["launchctl", "enable"]:
            job = self.jobs.setdefault(
                cmd[2], {"loaded": False, "disabled": False, "pid": None, "start": None}
            )
            job["disabled"] = False
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
            if job["loaded"]:
                return self._complete(cmd, check, returncode=5)
            job["loaded"] = True
            if target not in self.bootstrap_without_pid:
                self.next_pid += 1
                job["pid"] = self.next_pid
                job["start"] = self.next_pid * 10
            if target in self.bootstrap_exit5_after_load:
                self.bootstrap_exit5_after_load.remove(target)
                return self._complete(cmd, check, returncode=5)
            return self._complete(cmd, check)

        if cmd[:2] == ["launchctl", "kickstart"]:
            target = cmd[-1]
            if target in self.kickstart_failures:
                return self._complete(cmd, check, returncode=5, stdout="EIO")
            job = self._job(target)
            if job is None or not job["loaded"]:
                return self._complete(cmd, check, returncode=113)
            if target in self.pid_appears_before_kickstart and job["pid"] is None:
                self.next_pid += 1
                job["pid"] = self.next_pid
                job["start"] = self.next_pid * 10
                self.pid_appears_before_kickstart.remove(target)
                return self._complete(cmd, check)
            self.next_pid += 1
            job["pid"] = self.next_pid
            job["start"] = self.next_pid * 10
            return self._complete(cmd, check)

        if cmd[:2] == ["launchctl", "bootout"]:
            target = cmd[2]
            if target in self.bootout_failures:
                return self._complete(cmd, check, returncode=1, stdout="rollback failed")
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
    monkeypatch.setattr(
        gateway_cli, "_launchd_candidate_domains", lambda: ("gui/501", "user/501")
    )
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


def _write_explicit_gateway_plist(
    path: Path, *, label: str, hermes_home: Path, profile: str | None = None
) -> None:
    arguments = ["/usr/bin/python3", "-m", "hermes_cli.main"]
    if profile is not None:
        arguments.extend(["--profile", profile])
    arguments.extend(["gateway", "run", "--replace"])
    path.write_bytes(
        plistlib.dumps(
            {
                "Label": label,
                "ProgramArguments": arguments,
                "EnvironmentVariables": {"HERMES_HOME": str(hermes_home)},
                "RunAtLoad": True,
                "KeepAlive": True,
            }
        )
    )


def _mutating_launchd_calls(sim: _FakeLaunchd) -> list[list[str]]:
    return [
        call
        for call in sim.calls
        if call[:2]
        in (
            ["launchctl", "enable"],
            ["launchctl", "disable"],
            ["launchctl", "bootstrap"],
            ["launchctl", "bootout"],
            ["launchctl", "kickstart"],
        )
    ]


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

    assert not any(
        call[:2] in (["launchctl", "bootstrap"], ["launchctl", "bootout"], ["launchctl", "kickstart"])
        for call in sim.calls
    )
    assert sim.jobs["gui/501/ai.hermes.gateway"]["pid"] == 101
    assert sim.jobs["user/501/ai.hermes.gateway-coder"]["pid"] == 202


def test_start_all_loaded_live_target_is_verified_without_bootstrap_or_kickstart(
    launchd_env, monkeypatch
):
    root, agents = launchd_env
    default, coder = _install_two_profiles(agents, root)
    default.unlink()
    coder.unlink()
    _write_gateway_plist(agents / "ai.hermes.gateway.plist", "ai.hermes.gateway", root)
    sim = _FakeLaunchd(dict([_job("gui/501", "ai.hermes.gateway", pid=101)]))
    _patch_launchd_sim(monkeypatch, sim)

    gateway_cli.launchd_start_all()

    assert not any(
        call[:2] in (["launchctl", "bootstrap"], ["launchctl", "bootout"], ["launchctl", "kickstart"])
        for call in sim.calls
    )
    assert sim.jobs["gui/501/ai.hermes.gateway"]["pid"] == 101


def test_start_all_loaded_no_pid_uses_exact_kickstart_without_bootstrap(
    launchd_env, monkeypatch
):
    root, agents = launchd_env
    default, coder = _install_two_profiles(agents, root)
    default.unlink()
    coder.unlink()
    _write_gateway_plist(agents / "ai.hermes.gateway.plist", "ai.hermes.gateway", root)
    sim = _FakeLaunchd(
        dict([_job("gui/501", "ai.hermes.gateway", pid=None, loaded=True)])
    )
    _patch_launchd_sim(monkeypatch, sim)

    gateway_cli.launchd_start_all()

    assert ["launchctl", "kickstart", "gui/501/ai.hermes.gateway"] in sim.calls
    assert not any(
        call[:2] in (["launchctl", "bootstrap"], ["launchctl", "bootout"])
        for call in sim.calls
    )


def test_start_all_bootstrap_exit5_race_never_boots_out_newly_registered_label(
    launchd_env, monkeypatch
):
    root, agents = launchd_env
    default, coder = _install_two_profiles(agents, root)
    default.unlink()
    coder.unlink()
    _write_gateway_plist(agents / "ai.hermes.gateway.plist", "ai.hermes.gateway", root)
    target = "gui/501/ai.hermes.gateway"
    sim = _FakeLaunchd({}, bootstrap_exit5_after_load=(target,))
    _patch_launchd_sim(monkeypatch, sim)

    gateway_cli.launchd_start_all()

    assert ["launchctl", "bootstrap", "gui/501", str(agents / "ai.hermes.gateway.plist")] in sim.calls
    assert not any(call[:2] == ["launchctl", "bootout"] for call in sim.calls)
    assert sim.jobs[target]["loaded"] is True


def test_start_all_rollback_never_boots_out_a_live_bootstrap_registration(
    launchd_env, monkeypatch
):
    root, agents = launchd_env
    default, coder = _install_two_profiles(agents, root)
    coder.unlink()
    target = "gui/501/ai.hermes.gateway"
    sim = _FakeLaunchd({})
    _patch_launchd_sim(monkeypatch, sim)
    monkeypatch.setattr(
        gateway_cli,
        "_launchd_all_start_bootstrapped_target",
        lambda *args, **kwargs: False,
    )

    with pytest.raises(gateway_cli.LaunchdAllOperationError, match="rollback"):
        gateway_cli.launchd_start_all()

    assert sim.jobs[target]["loaded"] is True
    assert not any(call[:2] == ["launchctl", "bootout"] for call in sim.calls)


def test_stop_all_global_barrier_rejects_enable_race_on_earlier_label(
    launchd_env, monkeypatch
):
    root, agents = launchd_env
    _install_two_profiles(agents, root)
    second = "user/501/ai.hermes.gateway-coder"
    sim = _FakeLaunchd(
        dict(
            [
                _job("gui/501", "ai.hermes.gateway", pid=101),
                _job(
                    "user/501", "ai.hermes.gateway", pid=None, loaded=False
                ),
                _job(
                    "gui/501", "ai.hermes.gateway-coder", pid=None, loaded=False
                ),
                _job("user/501", "ai.hermes.gateway-coder", pid=202),
            ]
        )
    )
    _patch_launchd_sim(monkeypatch, sim)
    real_disable = gateway_cli._launchd_disable

    def disable_with_race(domain, label):
        real_disable(domain, label)
        if label == "ai.hermes.gateway" and domain == "gui/501":
            sim.jobs[second]["disabled"] = False

    monkeypatch.setattr(gateway_cli, "_launchd_disable", disable_with_race)

    result = gateway_cli.launchd_stop_all()

    assert result.sweep_safe is False
    assert result.failures
    assert not any(call[:2] == ["launchctl", "bootout"] for call in sim.calls)


@pytest.mark.parametrize(
    ("manager_name", "expected_domain"),
    [("Aqua", "gui/501"), ("Background", "user/501")],
)
def test_start_all_unloaded_enabled_target_uses_validated_manager_domain(
    launchd_env, monkeypatch, manager_name, expected_domain
):
    root, agents = launchd_env
    default, coder = _install_two_profiles(agents, root)
    coder.unlink()
    sim = _FakeLaunchd({}, manager_name=manager_name)
    _patch_launchd_sim(monkeypatch, sim)

    gateway_cli.launchd_start_all()

    assert [
        "launchctl",
        "bootstrap",
        expected_domain,
        str(default),
    ] in sim.calls
    assert f"{expected_domain}/ai.hermes.gateway" in sim.jobs


def test_start_all_unloaded_enabled_target_rejects_unknown_manager_before_mutation(
    launchd_env, monkeypatch
):
    root, agents = launchd_env
    default, coder = _install_two_profiles(agents, root)
    coder.unlink()
    sim = _FakeLaunchd({}, manager_name="loginwindow")
    _patch_launchd_sim(monkeypatch, sim)

    with pytest.raises(gateway_cli.LaunchdAllOperationError, match="unsupported manager"):
        gateway_cli.launchd_start_all()

    assert _mutating_launchd_calls(sim) == []


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
        lambda pid, timeout, **kwargs: _replace_fake_pid(sim, pid),
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

    def fake_graceful(pid, timeout, **kwargs):
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


def test_restart_all_passes_birth_identity_to_sigusr1_guard(launchd_env, monkeypatch):
    root, agents = launchd_env
    _install_two_profiles(agents, root)
    sim = _FakeLaunchd(
        dict([_job("gui/501", "ai.hermes.gateway", pid=101)])
    )
    _patch_launchd_sim(monkeypatch, sim)
    observed = {}

    def fake_graceful(pid, timeout, expected_start_time=None):
        observed.update(
            pid=pid, timeout=timeout, expected_start_time=expected_start_time
        )
        assert _replace_fake_pid(sim, pid)
        return True

    monkeypatch.setattr(gateway_cli, "_graceful_restart_via_sigusr1", fake_graceful)
    monkeypatch.setattr(
        gateway_cli,
        "_launchd_all_wait_for_successor",
        lambda *args, **kwargs: True,
    )

    gateway_cli.launchd_restart_all()

    assert observed == {
        "pid": 101,
        "timeout": gateway_cli._get_restart_drain_timeout(),
        "expected_start_time": 1010,
    }


def test_sigusr1_birth_identity_change_is_not_signalled(monkeypatch):
    monkeypatch.setattr(gateway_cli, "_launchd_pid_is_live", lambda pid, start: False)
    monkeypatch.setattr(
        gateway_cli.os,
        "kill",
        lambda *_args: pytest.fail("changed PID birth identity must not be signalled"),
    )

    assert (
        gateway_cli._graceful_restart_via_sigusr1(
            4242, 1.0, expected_start_time=42420
        )
        is False
    )


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
        gateway_cli,
        "_graceful_restart_via_sigusr1",
        lambda pid, timeout, **kwargs: True,
    )
    monkeypatch.setattr(
        gateway_cli,
        "_launchd_all_preflight_targets",
        lambda: (
                gateway_cli.LaunchdAllTarget(
                    label="ai.hermes.gateway",
                    domain="gui/501",
                    plist_path=default,
                    plist_fingerprint=gateway_cli._read_launchd_all_plist_identity(default).fingerprint,
                    hermes_home=root,
                    was_loaded=True,
                    pid=101,
                    start_time=1010,
                    probe=gateway_cli.LaunchdLabelProbe(
                        ("gui/501",), (), ("user/501",), (), pids=(("gui/501", 101),)
                    ),
                    plist_stat_identity=gateway_cli._read_launchd_all_plist_identity(default).stat_identity,
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

    assert not any(
        call[:2] in (["launchctl", "bootstrap"], ["launchctl", "bootout"], ["launchctl", "kickstart"])
        and call[-1] == "user/501/ai.hermes.gateway-coder"
        for call in sim.calls
    ), sim.calls
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


def test_all_profile_dual_registration_fails_before_any_mutation(launchd_env, monkeypatch):
    root, agents = launchd_env
    default, coder = _install_two_profiles(agents, root)
    coder.unlink()
    target = "ai.hermes.gateway"
    sim = _FakeLaunchd(
        dict(
            [
                _job("gui/501", target, pid=101),
                _job("user/501", target, pid=202),
            ]
        )
    )
    _patch_launchd_sim(monkeypatch, sim)

    with pytest.raises(gateway_cli.LaunchdAllOperationError, match="both"):
        gateway_cli.launchd_start_all()

    assert _mutating_launchd_calls(sim) == []


def test_restart_all_unloaded_target_bootstraps_and_keeps_immediate_live_process(
    launchd_env, monkeypatch
):
    root, agents = launchd_env
    default, coder = _install_two_profiles(agents, root)
    coder.unlink()
    sim = _FakeLaunchd({})
    _patch_launchd_sim(monkeypatch, sim)

    result = gateway_cli.launchd_restart_all()

    assert result.outcomes[0].status == "restarted"
    assert ["launchctl", "bootstrap", "gui/501", str(default)] in sim.calls
    assert not any(
        call[:2] == ["launchctl", "kickstart"]
        for call in sim.calls
    )
    assert sim.jobs["gui/501/ai.hermes.gateway"]["pid"] == 901
    assert not any(call[:2] == ["launchctl", "bootout"] for call in sim.calls)


def test_restart_all_bootstrap_without_pid_uses_non_killing_kickstart(
    launchd_env, monkeypatch
):
    root, agents = launchd_env
    default, coder = _install_two_profiles(agents, root)
    coder.unlink()
    target = "gui/501/ai.hermes.gateway"
    sim = _FakeLaunchd({}, bootstrap_without_pid=(target,))
    _patch_launchd_sim(monkeypatch, sim)
    clock = iter((0.0, 11.0, 11.0, 11.0))
    monkeypatch.setattr(gateway_cli.time, "monotonic", lambda: next(clock, 11.0))
    monkeypatch.setattr(gateway_cli.time, "sleep", lambda _seconds: None)

    result = gateway_cli.launchd_restart_all()

    assert result.outcomes[0].status == "restarted"
    assert ["launchctl", "kickstart", target] in sim.calls
    assert ["launchctl", "kickstart", "-k", target] not in sim.calls
    assert not any(call[:2] == ["launchctl", "bootout"] for call in sim.calls)


def test_start_all_loaded_no_pid_does_not_replace_pid_that_appears_before_kickstart(
    launchd_env, monkeypatch
):
    root, agents = launchd_env
    default, coder = _install_two_profiles(agents, root)
    coder.unlink()
    target = "gui/501/ai.hermes.gateway"
    sim = _FakeLaunchd(
        {target: dict(_job("gui/501", "ai.hermes.gateway", pid=None)[1])},
        pid_appears_before_kickstart=(target,),
    )
    _patch_launchd_sim(monkeypatch, sim)

    result = gateway_cli.launchd_start_all()

    assert result.outcomes[0].status == "started"
    assert ["launchctl", "kickstart", target] in sim.calls
    assert ["launchctl", "kickstart", "-k", target] not in sim.calls
    assert sim.jobs[target]["pid"] == 901


def test_start_all_bootstrap_liveness_failure_rolls_back_registration_and_fence(
    launchd_env, monkeypatch
):
    root, agents = launchd_env
    _install_two_profiles(agents, root)
    target = "gui/501/ai.hermes.gateway"
    peer = "user/501/ai.hermes.gateway-coder"
    sim = _FakeLaunchd(
        {
            "gui/501/ai.hermes.gateway": dict(
                _job("gui/501", "ai.hermes.gateway", pid=None, loaded=False, disabled=True)[1]
            ),
            "user/501/ai.hermes.gateway": dict(
                _job("user/501", "ai.hermes.gateway", pid=None, loaded=False, disabled=False)[1]
            ),
            peer: dict(_job("user/501", "ai.hermes.gateway-coder", pid=202)[1]),
        },
        bootstrap_without_pid=(target,),
        kickstart_failures=(target,),
    )
    _patch_launchd_sim(monkeypatch, sim)
    monkeypatch.setattr(gateway_cli, "_launchd_all_wait_for_live", lambda *a, **k: False)

    with pytest.raises(
        gateway_cli.LaunchdAllOperationError, match="launchd exit 5"
    ):
        gateway_cli.launchd_start_all()

    assert ["launchctl", "bootout", target] in sim.calls
    assert sim.jobs[target]["loaded"] is False
    assert sim.jobs[target]["disabled"] is True
    assert not any(call[-1] == peer for call in sim.calls if call[:2] == ["launchctl", "bootout"])


def test_start_all_bootstrap_rollback_failure_is_reported_without_peer_bootout(
    launchd_env, monkeypatch
):
    root, agents = launchd_env
    _install_two_profiles(agents, root)
    target = "gui/501/ai.hermes.gateway"
    peer = "user/501/ai.hermes.gateway-coder"
    sim = _FakeLaunchd(
        {
            "gui/501/ai.hermes.gateway": dict(
                _job("gui/501", "ai.hermes.gateway", pid=None, loaded=False)[1]
            ),
            peer: dict(_job("user/501", "ai.hermes.gateway-coder", pid=202)[1]),
        },
        bootstrap_without_pid=(target,),
        kickstart_failures=(target,),
        bootout_failures=(target,),
    )
    _patch_launchd_sim(monkeypatch, sim)
    monkeypatch.setattr(gateway_cli, "_launchd_all_wait_for_live", lambda *a, **k: False)

    with pytest.raises(gateway_cli.LaunchdAllOperationError, match="rollback") as excinfo:
        gateway_cli.launchd_start_all()

    assert "launchd exit 1" in str(excinfo.value)
    assert sim.jobs[target]["loaded"] is True
    assert not any(call[-1] == peer for call in sim.calls if call[:2] == ["launchctl", "bootout"])


@pytest.mark.parametrize(
    "supervision_change",
    [
        lambda document: document.pop("RunAtLoad"),
        lambda document: document.update(RunAtLoad=False),
        lambda document: document.update(RunAtLoad={"SuccessfulExit": True}),
        lambda document: document.pop("KeepAlive"),
        lambda document: document.update(KeepAlive=False),
        lambda document: document.update(KeepAlive={"SuccessfulExit": False}),
    ],
)
def test_all_profile_requires_scalar_launchd_supervision_identity(
    launchd_env, monkeypatch, supervision_change
):
    root, agents = launchd_env
    default, coder = _install_two_profiles(agents, root)
    coder.unlink()
    document = plistlib.loads(default.read_bytes())
    supervision_change(document)
    default.write_bytes(plistlib.dumps(document))
    sim = _FakeLaunchd({})
    _patch_launchd_sim(monkeypatch, sim)

    with pytest.raises(gateway_cli.LaunchdAllOperationError, match="RunAtLoad|KeepAlive"):
        gateway_cli.launchd_start_all()

    assert sim.calls == []


@pytest.mark.parametrize(
    "arguments",
    [
        ["/usr/bin/python3", "-m", "hermes_cli.main", "--evil", "gateway", "run", "--replace"],
        ["/usr/bin/python3", "--profile", "coder", "-m", "hermes_cli.main", "gateway", "run", "--replace"],
        ["/usr/bin/python3", "-m", "hermes_cli.main", "gateway", "--profile", "coder", "run", "--replace"],
    ],
)
def test_all_profile_rejects_noncanonical_program_arguments_before_mutation(
    launchd_env, monkeypatch, arguments
):
    root, agents = launchd_env
    default, coder = _install_two_profiles(agents, root)
    coder.unlink()
    document = plistlib.loads(default.read_bytes())
    document["ProgramArguments"] = arguments
    default.write_bytes(plistlib.dumps(document))
    sim = _FakeLaunchd({})
    _patch_launchd_sim(monkeypatch, sim)

    with pytest.raises(
        gateway_cli.LaunchdAllOperationError,
        match="ProgramArguments|foreign|Hermes gateway runtime",
    ):
        gateway_cli.launchd_start_all()

    assert sim.calls == []


def test_restart_all_bootstrap_exit5_race_does_not_kickstart_new_registration(
    launchd_env, monkeypatch
):
    root, agents = launchd_env
    default, coder = _install_two_profiles(agents, root)
    coder.unlink()
    target = "gui/501/ai.hermes.gateway"
    sim = _FakeLaunchd({}, bootstrap_exit5_after_load=(target,))
    _patch_launchd_sim(monkeypatch, sim)

    with pytest.raises(gateway_cli.LaunchdAllOperationError, match="no kickstart"):
        gateway_cli.launchd_restart_all()

    assert not any(call[:2] in (["launchctl", "bootout"], ["launchctl", "kickstart"]) for call in sim.calls)
    assert sim.jobs[target]["loaded"] is True


def test_all_profile_unknown_liveness_error_is_not_converted_to_timeout(
    launchd_env, monkeypatch
):
    root, agents = launchd_env
    default, coder = _install_two_profiles(agents, root)
    coder.unlink()
    sim = _FakeLaunchd({})
    _patch_launchd_sim(monkeypatch, sim)

    def unknown_liveness(*args, **kwargs):
        raise gateway_cli.LaunchdAllOperationError(
            "gui/501/ai.hermes.gateway print exit 5"
        )

    monkeypatch.setattr(
        gateway_cli,
        "_launchd_all_verify_live",
        unknown_liveness,
    )

    with pytest.raises(gateway_cli.LaunchdAllOperationError, match="print exit 5"):
        gateway_cli.launchd_start_all()


@pytest.mark.parametrize(
    ("fake_option", "message"),
    [
        ("unknown_print_domains", "print exit 5"),
        ("unknown_disabled_domains", "print-disabled exit 5"),
    ],
)
def test_all_profile_unknown_launchd_state_fails_before_mutation(
    launchd_env, monkeypatch, fake_option, message
):
    root, agents = launchd_env
    default, coder = _install_two_profiles(agents, root)
    coder.unlink()
    kwargs = {fake_option: ("gui/501",)}
    sim = _FakeLaunchd(
        {"gui/501/ai.hermes.gateway": dict(_job("gui/501", "ai.hermes.gateway", pid=101)[1])},
        **kwargs,
    )
    _patch_launchd_sim(monkeypatch, sim)

    with pytest.raises(gateway_cli.LaunchdAllOperationError, match=message):
        gateway_cli.launchd_start_all()

    assert _mutating_launchd_calls(sim) == []


def test_restart_preserves_disabled_peer_domain_and_start_clears_it(
    launchd_env, monkeypatch
):
    root, agents = launchd_env
    _install_two_profiles(agents, root)
    default_target = "gui/501/ai.hermes.gateway"
    peer_target = "user/501/ai.hermes.gateway"
    sim = _FakeLaunchd(
        dict(
            [
                _job("gui/501", "ai.hermes.gateway", pid=101),
                _job("user/501", "ai.hermes.gateway", pid=None, disabled=True, loaded=False),
                _job("user/501", "ai.hermes.gateway-coder", pid=202),
            ]
        )
    )
    _patch_launchd_sim(monkeypatch, sim)
    restart_pids = []

    def fake_graceful(pid, timeout, **kwargs):
        restart_pids.append(pid)
        assert _replace_fake_pid(sim, pid)
        return True

    monkeypatch.setattr(gateway_cli, "_graceful_restart_via_sigusr1", fake_graceful)

    gateway_cli.launchd_start_all()
    assert ["launchctl", "enable", peer_target] in sim.calls
    assert sim.jobs[peer_target]["disabled"] is False

    sim.jobs[peer_target]["disabled"] = True
    sim.calls.clear()
    result = gateway_cli.launchd_restart_all()

    assert restart_pids == [202]
    assert not any(call[-1] == default_target for call in sim.calls if call[0:2] == ["launchctl", "enable"])
    assert not any(pid == 101 for pid in restart_pids)
    assert {
        outcome.label: outcome.status for outcome in result.outcomes
    }["ai.hermes.gateway"] == "preserved"


@pytest.mark.parametrize("operation_name", ["launchd_start_all", "launchd_restart_all"])
def test_post_preflight_enabled_target_becoming_disabled_is_no_touch(
    launchd_env, monkeypatch, operation_name
):
    root, agents = launchd_env
    default, coder = _install_two_profiles(agents, root)
    coder.unlink()
    sim = _FakeLaunchd(
        {"gui/501/ai.hermes.gateway": dict(_job("gui/501", "ai.hermes.gateway", pid=101)[1])}
    )
    _patch_launchd_sim(monkeypatch, sim)
    original_preflight = gateway_cli._launchd_all_preflight_targets

    def preflight_then_race():
        targets = original_preflight()
        sim.jobs["gui/501/ai.hermes.gateway"]["disabled"] = True
        return targets

    monkeypatch.setattr(gateway_cli, "_launchd_all_preflight_targets", preflight_then_race)
    monkeypatch.setattr(
        gateway_cli,
        "_graceful_restart_via_sigusr1",
        lambda *args: pytest.fail("concurrent desired-state change must not signal"),
    )

    with pytest.raises(gateway_cli.LaunchdAllOperationError, match="disabled state"):
        getattr(gateway_cli, operation_name)()

    assert _mutating_launchd_calls(sim) == []


def test_post_preflight_disabled_target_becoming_enabled_is_no_touch(launchd_env, monkeypatch):
    root, agents = launchd_env
    default, coder = _install_two_profiles(agents, root)
    coder.unlink()
    target = "gui/501/ai.hermes.gateway"
    sim = _FakeLaunchd({target: dict(_job("gui/501", "ai.hermes.gateway", pid=101, disabled=True)[1])})
    _patch_launchd_sim(monkeypatch, sim)
    original_preflight = gateway_cli._launchd_all_preflight_targets

    def preflight_then_race():
        targets = original_preflight()
        sim.jobs[target]["disabled"] = False
        return targets

    monkeypatch.setattr(gateway_cli, "_launchd_all_preflight_targets", preflight_then_race)
    monkeypatch.setattr(
        gateway_cli,
        "_graceful_restart_via_sigusr1",
        lambda *args: pytest.fail("concurrent desired-state change must not signal"),
    )

    with pytest.raises(gateway_cli.LaunchdAllOperationError, match="disabled state"):
        gateway_cli.launchd_restart_all()

    assert _mutating_launchd_calls(sim) == []


@pytest.mark.parametrize("birth_race", [False, True])
def test_post_preflight_pid_identity_race_is_no_touch(launchd_env, monkeypatch, birth_race):
    root, agents = launchd_env
    default, coder = _install_two_profiles(agents, root)
    coder.unlink()
    target = "gui/501/ai.hermes.gateway"
    sim = _FakeLaunchd({target: dict(_job("gui/501", "ai.hermes.gateway", pid=101)[1])})
    _patch_launchd_sim(monkeypatch, sim)
    original_preflight = gateway_cli._launchd_all_preflight_targets

    def preflight_then_race():
        targets = original_preflight()
        if birth_race:
            sim.jobs[target]["start"] = 9999
        else:
            assert _replace_fake_pid(sim, 101)
        return targets

    monkeypatch.setattr(gateway_cli, "_launchd_all_preflight_targets", preflight_then_race)
    monkeypatch.setattr(
        gateway_cli,
        "_graceful_restart_via_sigusr1",
        lambda *args: pytest.fail("concurrent PID identity change must not signal"),
    )

    with pytest.raises(gateway_cli.LaunchdAllOperationError, match="PID identity"):
        gateway_cli.launchd_restart_all()

    assert _mutating_launchd_calls(sim) == []


def test_all_profile_missing_birth_identity_aborts_before_any_peer_mutation(
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
    real_start_time = sim.start_time
    monkeypatch.setattr(
        gateway_cli,
        "_launchd_process_start_time",
        lambda pid: None if pid == 202 else real_start_time(pid),
    )

    with pytest.raises(gateway_cli.LaunchdAllOperationError, match="birth identity"):
        gateway_cli.launchd_start_all()

    assert _mutating_launchd_calls(sim) == []


def test_start_target_rolls_back_only_its_new_fence_after_operational_failure(
    launchd_env, monkeypatch
):
    root, agents = launchd_env
    _install_two_profiles(agents, root)
    failed_target = "gui/501/ai.hermes.gateway"
    rollback_domain = "user/501/ai.hermes.gateway"
    peer_target = "user/501/ai.hermes.gateway-coder"
    sim = _FakeLaunchd(
        dict(
            [
                _job("gui/501", "ai.hermes.gateway", pid=None, loaded=True),
                _job("user/501", "ai.hermes.gateway", pid=None, disabled=True, loaded=False),
                _job("user/501", "ai.hermes.gateway-coder", pid=202),
            ]
        ),
        kickstart_failures=(failed_target,),
    )
    _patch_launchd_sim(monkeypatch, sim)

    with pytest.raises(gateway_cli.LaunchdAllOperationError):
        gateway_cli.launchd_start_all()

    assert ["launchctl", "enable", rollback_domain] in sim.calls
    assert ["launchctl", "disable", rollback_domain] in sim.calls
    assert not any(call[-1] == peer_target for call in sim.calls if call[:2] == ["launchctl", "disable"])
    assert sim.jobs[rollback_domain]["disabled"] is True


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("owner", "non-owned"),
        ("mode", "writable"),
        ("symlink", "non-regular"),
    ],
)
def test_all_profile_secure_plist_reader_rejects_unsafe_identity(
    launchd_env, monkeypatch, mutation, message
):
    root, agents = launchd_env
    default, coder = _install_two_profiles(agents, root)
    coder.unlink()
    sim = _FakeLaunchd({})
    _patch_launchd_sim(monkeypatch, sim)

    if mutation == "owner":
        real_fstat = gateway_cli.os.fstat

        def wrong_owner(fd):
            metadata = real_fstat(fd)
            return SimpleNamespace(
                st_mode=metadata.st_mode,
                st_uid=os.getuid() + 1,
                st_dev=metadata.st_dev,
                st_ino=metadata.st_ino,
                st_size=metadata.st_size,
                st_mtime_ns=metadata.st_mtime_ns,
                st_ctime_ns=metadata.st_ctime_ns,
            )

        monkeypatch.setattr(gateway_cli.os, "fstat", wrong_owner)
    elif mutation == "mode":
        default.chmod(0o664)
    else:
        replacement = default.parent / "secure-target.plist"
        _write_gateway_plist(replacement, "ai.hermes.gateway", root)
        default.unlink()
        default.symlink_to(replacement)

    with pytest.raises(gateway_cli.LaunchdAllOperationError, match=message):
        gateway_cli.launchd_start_all()

    assert sim.calls == []


def test_all_profile_custom_root_identity_ignores_callers_default_root(
    launchd_env, monkeypatch
):
    root, agents = launchd_env
    custom_root = root.parent / "caller-independent" / "profiles" / "coder"
    custom_root.mkdir(parents=True)
    label = "ai.hermes.gateway-" + hashlib.sha256(
        str(custom_root.resolve()).encode()
    ).hexdigest()[:8]
    plist_path = agents / f"{label}.plist"
    _write_explicit_gateway_plist(
        plist_path, label=label, hermes_home=custom_root, profile=None
    )
    monkeypatch.setenv("HERMES_HOME", str(root))

    import hermes_constants

    monkeypatch.setattr(
        hermes_constants,
        "get_default_hermes_root",
        lambda: root.parent / "a-different-callers-default",
    )
    sim = _FakeLaunchd(
        {f"gui/501/{label}": dict(_job("gui/501", label, pid=101)[1])}
    )
    _patch_launchd_sim(monkeypatch, sim)

    result = gateway_cli.launchd_start_all()

    assert result.outcomes[0].status == "started"
    assert sim.jobs[f"gui/501/{label}"]["pid"] == 101
    assert _mutating_launchd_calls(sim) == []


def test_all_profile_native_root_requires_base_label_and_no_profile(
    launchd_env, monkeypatch
):
    root, agents = launchd_env
    label = "ai.hermes.gateway-" + hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:8]
    plist_path = agents / f"{label}.plist"
    _write_explicit_gateway_plist(plist_path, label=label, hermes_home=root, profile=None)
    sim = _FakeLaunchd({})
    _patch_launchd_sim(monkeypatch, sim)

    with pytest.raises(gateway_cli.LaunchdAllOperationError, match="native default"):
        gateway_cli.launchd_start_all()

    assert sim.calls == []


def test_all_profile_revalidation_rejects_replaced_plist_before_mutation(
    launchd_env, monkeypatch
):
    root, agents = launchd_env
    default, coder = _install_two_profiles(agents, root)
    coder.unlink()
    sim = _FakeLaunchd(
        {"gui/501/ai.hermes.gateway": dict(_job("gui/501", "ai.hermes.gateway", pid=None, loaded=False)[1])}
    )
    _patch_launchd_sim(monkeypatch, sim)
    original_preflight = gateway_cli._launchd_all_preflight_targets

    def preflight_then_replace():
        targets = original_preflight()
        replacement = default.parent / "replacement.plist"
        _write_gateway_plist(replacement, "ai.hermes.gateway", root)
        os.replace(replacement, default)
        return targets

    monkeypatch.setattr(gateway_cli, "_launchd_all_preflight_targets", preflight_then_replace)

    with pytest.raises(gateway_cli.LaunchdAllOperationError, match="identity changed"):
        gateway_cli.launchd_start_all()

    assert _mutating_launchd_calls(sim) == []


def test_gateway_all_parser_help_describes_exact_cross_profile_semantics(capsys):
    from hermes_cli.subcommands.gateway import build_gateway_parser

    parser = argparse.ArgumentParser(prog="hermes")
    subparsers = parser.add_subparsers(dest="command")
    build_gateway_parser(
        subparsers,
        cmd_gateway=lambda args: None,
        cmd_proxy=lambda args: None,
        cmd_gateway_enroll=lambda args: None,
    )

    for verb in ("start", "stop", "restart"):
        with pytest.raises(SystemExit):
            parser.parse_args(["gateway", verb, "--help"])
        help_text = capsys.readouterr().out
        assert "across profiles" in help_text
        assert "on macOS" in help_text
        if verb in ("start", "restart"):
            assert "installed Hermes LaunchAgents only" in help_text
        if verb == "restart":
            assert "SIGUSR1" in help_text
            assert "already-live" in help_text
        if verb == "stop":
            assert "global PID sweep" not in help_text
        assert "Kill ALL" not in help_text
