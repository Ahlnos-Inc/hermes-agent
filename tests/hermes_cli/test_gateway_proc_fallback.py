"""Tests for /proc-based gateway PID detection in Docker environments.

Verifies that _scan_gateway_pids() uses /proc/*/cmdline when available
(Docker without procps) and falls back to ps only when /proc is absent.

See: NousResearch/hermes-agent#7622
"""

import os
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

import hermes_cli.gateway as gateway_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_GATEWAY_CMD = "python -m hermes_cli.main gateway run"
_OTHER_CMD = "python -m some_other_thing"


def _fake_proc_dir(entries: dict):
    """Return side_effects that simulate /proc: isdir → True, listdir → pids,
    open(cmdline) → null-delimited command bytes."""
    def _isdir(path):
        return str(path) == "/proc"

    def _listdir(path):
        if str(path) == "/proc":
            return [str(pid) for pid in entries] + ["self", "version"]
        raise FileNotFoundError(path)

    def _open(path, mode="r", **kwargs):
        path_str = str(path)
        if "/cmdline" in path_str:
            pid = int(path_str.split("/proc/")[1].split("/")[0])
            raw = entries.get(pid, "").encode("utf-8").replace(b" ", b"\x00")
            m = MagicMock()
            m.read.return_value = raw
            m.__enter__ = lambda s: s
            m.__exit__ = MagicMock(return_value=False)
            return m
        raise FileNotFoundError(path)

    return _isdir, _listdir, _open


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestProcFallback:
    """_scan_gateway_pids reads /proc when available, skips ps."""

    def test_detects_gateway_pid_via_proc(self):
        my_pid = os.getpid()
        entries = {
            my_pid: "python -m hermes_cli.main",   # own process — excluded
            12345: _GATEWAY_CMD,
            99999: _OTHER_CMD,
        }
        _isdir, _listdir, _open = _fake_proc_dir(entries)

        with (
            patch("hermes_cli.gateway.is_windows", return_value=False),
            patch("os.path.isdir", side_effect=_isdir),
            patch("os.listdir", side_effect=_listdir),
            patch("builtins.open", side_effect=_open),
            patch("hermes_cli.gateway._get_ancestor_pids", return_value=set()),
            patch("subprocess.run") as mock_ps,
        ):
            pids = gateway_mod._scan_gateway_pids(set(), all_profiles=True)

        assert 12345 in pids
        assert 99999 not in pids
        mock_ps.assert_not_called()  # ps must NOT be called when /proc worked

    def test_detects_no_supervisor_restart_process_only_when_enabled(self):
        entries = {
            12345: "python -m hermes_cli.main gateway restart",
            99999: _OTHER_CMD,
        }
        _isdir, _listdir, _open = _fake_proc_dir(entries)

        with (
            patch("hermes_cli.gateway.is_windows", return_value=False),
            patch("os.path.isdir", side_effect=_isdir),
            patch("os.listdir", side_effect=_listdir),
            patch("builtins.open", side_effect=_open),
            patch("hermes_cli.gateway._get_ancestor_pids", return_value=set()),
            patch("subprocess.run") as mock_ps,
        ):
            strict_pids = gateway_mod._scan_gateway_pids(set(), all_profiles=True)

        _isdir, _listdir, _open = _fake_proc_dir(entries)
        with (
            patch("hermes_cli.gateway.is_windows", return_value=False),
            patch("os.path.isdir", side_effect=_isdir),
            patch("os.listdir", side_effect=_listdir),
            patch("builtins.open", side_effect=_open),
            patch("hermes_cli.gateway._get_ancestor_pids", return_value=set()),
            patch("subprocess.run") as mock_ps_enabled,
        ):
            fallback_pids = gateway_mod._scan_gateway_pids(
                set(),
                all_profiles=True,
                include_restart_managers=True,
            )

        assert strict_pids == []
        assert fallback_pids == [12345]
        mock_ps.assert_not_called()
        mock_ps_enabled.assert_not_called()

    def test_excludes_own_pid_from_proc_scan(self):
        my_pid = os.getpid()
        entries = {my_pid: _GATEWAY_CMD}
        _isdir, _listdir, _open = _fake_proc_dir(entries)

        with (
            patch("hermes_cli.gateway.is_windows", return_value=False),
            patch("os.path.isdir", side_effect=_isdir),
            patch("os.listdir", side_effect=_listdir),
            patch("builtins.open", side_effect=_open),
            patch("hermes_cli.gateway._get_ancestor_pids", return_value=set()),
            patch("subprocess.run"),
        ):
            pids = gateway_mod._scan_gateway_pids(set(), all_profiles=True)

        assert my_pid not in pids

    def test_falls_back_to_ps_when_proc_absent(self):
        ps_output = f"12345 {_GATEWAY_CMD}\n99999 {_OTHER_CMD}\n"
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ps_output

        with (
            patch("hermes_cli.gateway.is_windows", return_value=False),
            patch("os.path.isdir", return_value=False),
            patch("hermes_cli.gateway._get_ancestor_pids", return_value=set()),
            patch("subprocess.run", return_value=mock_result) as mock_ps,
        ):
            pids = gateway_mod._scan_gateway_pids(set(), all_profiles=True)

        mock_ps.assert_called_once()
        assert 12345 in pids

    @pytest.mark.parametrize(
        ("macos", "linux", "output_field"),
        [
            (True, False, "command"),
            (False, True, "args"),
        ],
    )
    def test_ps_fallback_uses_platform_argv_and_parses_gateway(
        self,
        monkeypatch,
        macos,
        linux,
        output_field,
    ):
        mock_result = MagicMock(
            returncode=0,
            stdout=f"12345 {_GATEWAY_CMD}\n99999 {_OTHER_CMD}\n",
        )
        monkeypatch.setattr(gateway_mod, "is_windows", lambda: False)
        monkeypatch.setattr(gateway_mod, "is_macos", lambda: macos)
        monkeypatch.setattr(gateway_mod, "is_linux", lambda: linux)
        monkeypatch.setattr(gateway_mod.os.path, "isdir", lambda _path: False)
        monkeypatch.setattr(gateway_mod, "_get_ancestor_pids", lambda: set())
        mock_ps = MagicMock(return_value=mock_result)
        monkeypatch.setattr(gateway_mod.subprocess, "run", mock_ps)

        assert gateway_mod._scan_gateway_pids(set(), all_profiles=True) == [12345]
        mock_ps.assert_called_once_with(
            ["ps", "-A", "-ww", "-o", f"pid=,{output_field}="],
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_proc_permission_error_skips_pid(self):
        def _isdir(path):
            return str(path) == "/proc"

        def _listdir(path):
            if str(path) == "/proc":
                return ["12345", "self"]
            raise FileNotFoundError

        def _open(path, mode="r", **kwargs):
            raise PermissionError("no access")

        with (
            patch("hermes_cli.gateway.is_windows", return_value=False),
            patch("os.path.isdir", side_effect=_isdir),
            patch("os.listdir", side_effect=_listdir),
            patch("builtins.open", side_effect=_open),
            patch("hermes_cli.gateway._get_ancestor_pids", return_value=set()),
            patch("subprocess.run") as mock_ps,
        ):
            pids = gateway_mod._scan_gateway_pids(set(), all_profiles=True)

        # PermissionError swallowed — empty result, no crash
        assert 12345 not in pids
        mock_ps.assert_not_called()  # /proc dir existed, so ps not called


class TestProcessTablePsCommand:
    """The fallback ps invocation is portable and never requests env data."""

    def test_darwin_uses_bsd_command_field_without_environment(self, monkeypatch):
        monkeypatch.setattr(gateway_mod, "is_macos", lambda: True)
        monkeypatch.setattr(gateway_mod, "is_linux", lambda: False)

        assert gateway_mod._process_table_ps_command() == [
            "ps",
            "-A",
            "-ww",
            "-o",
            "pid=,command=",
        ]

    def test_linux_uses_gnu_args_field_without_environment(self, monkeypatch):
        monkeypatch.setattr(gateway_mod, "is_macos", lambda: False)
        monkeypatch.setattr(gateway_mod, "is_linux", lambda: True)

        assert gateway_mod._process_table_ps_command() == [
            "ps",
            "-A",
            "-ww",
            "-o",
            "pid=,args=",
        ]

    @pytest.mark.skipif(sys.platform != "darwin", reason="requires BSD ps")
    def test_darwin_command_succeeds_and_does_not_emit_environment(self):
        sentinel = f"hermes-ps-test-sentinel-{os.getpid()}"
        env = os.environ.copy()
        env["HERMES_PS_SECRET_SENTINEL"] = sentinel

        result = subprocess.run(
            gateway_mod._process_table_ps_command(),
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )

        assert result.returncode == 0, "BSD ps process-table command failed"
        assert any(
            len(line.strip().split(None, 1)) == 2
            and line.strip().split(None, 1)[0].isdigit()
            for line in result.stdout.splitlines()
        ), "BSD ps output did not contain a parseable PID and command"
        if sentinel in result.stdout:
            pytest.fail("BSD ps output included inherited environment data")
