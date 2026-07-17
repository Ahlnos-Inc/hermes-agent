"""
Gateway subcommand for hermes CLI.

Handles: hermes gateway [run|start|stop|restart|status|install|uninstall|setup]
"""

import asyncio
import hashlib
import json
import logging
import os
import plistlib
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

PROJECT_ROOT = Path(__file__).parent.parent.resolve()

from gateway.status import (
    GatewayLifecycleLockError,
    all_profile_lifecycle_lock,
    terminate_pid,
)
from gateway.process_identity import (
    GatewayProcessIdentity,
    GatewayRuntimeRole,
    classify_gateway_argv,
    gateway_command_subcommand,
    process_identity_matches_target,
)
from gateway.restart import (
    DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT,
    GATEWAY_SERVICE_RESTART_EXIT_CODE,
    parse_restart_drain_timeout,
)
from hermes_cli.config import (
    get_env_value,
    get_hermes_home,
    is_managed,
    managed_error,
    read_raw_config,
    save_env_value,
    write_platform_config_field,
)

# display_hermes_home is imported lazily at call sites to avoid ImportError
# when hermes_constants is cached from a pre-update version during `hermes update`.
from hermes_cli.setup import (
    print_header,
    print_info,
    print_success,
    print_warning,
    print_error,
    prompt,
    prompt_choice,
    prompt_yes_no,
)
from hermes_cli.colors import Colors, color

logger = logging.getLogger(__name__)

# =============================================================================
# Process Management (for manual gateway runs)
# =============================================================================


@dataclass(frozen=True)
class GatewayRuntimeSnapshot:
    manager: str
    service_installed: bool = False
    service_running: bool = False
    gateway_pids: tuple[int, ...] = ()
    service_scope: str | None = None

    @property
    def running(self) -> bool:
        return self.service_running or bool(self.gateway_pids)

    @property
    def has_process_service_mismatch(self) -> bool:
        return self.service_installed and self.running and not self.service_running


@dataclass(frozen=True)
class ProfileGatewayProcess:
    profile: str
    path: Path
    pid: int


class GatewayProcessEnumerationError(RuntimeError):
    """The process table could not be enumerated completely and safely."""


class GatewayProcessTerminationError(RuntimeError):
    """One or more exact gateway PIDs could not be terminated safely."""

    def __init__(self, failures: list[str]):
        self.failures = tuple(failures)
        super().__init__("; ".join(self.failures))


class GatewayProcessIdentityUnreadableError(GatewayProcessTerminationError):
    """A previously attested PID's identity became unreadable mid-transaction.

    Distinct from a *changed* identity: on macOS a draining PID's argv read
    can fail with sysctl(KERN_PROCARGS2) EINVAL before the PID disappears, so
    exit-wait loops may tolerate this state while the birth identity settles.
    """


def _get_service_pids() -> set:
    """Return PIDs currently managed by systemd or launchd gateway services.

    Used to avoid killing freshly-restarted service processes when sweeping
    for stale manual gateway processes after a service restart.  Relies on the
    service manager having committed the new PID before the restart command
    returns (true for both systemd and launchd in practice).
    """
    pids: set = set()

    # --- systemd (Linux): user and system scopes ---
    if supports_systemd_services():
        for scope_args in [["systemctl", "--user"], ["systemctl"]]:
            try:
                result = subprocess.run(
                    scope_args
                    + [
                        "list-units",
                        "hermes-gateway*",
                        "--plain",
                        "--no-legend",
                        "--no-pager",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                for line in result.stdout.strip().splitlines():
                    parts = line.split()
                    if not parts or not parts[0].endswith(".service"):
                        continue
                    svc = parts[0]
                    try:
                        show = subprocess.run(
                            scope_args + ["show", svc, "--property=MainPID", "--value"],
                            capture_output=True,
                            text=True,
                            timeout=5,
                        )
                        pid = int(show.stdout.strip())
                        if pid > 0:
                            pids.add(pid)
                    except (ValueError, subprocess.TimeoutExpired):
                        pass
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass

    # --- launchd (macOS) ---
    if is_macos():
        try:
            label = get_launchd_label()
            result = subprocess.run(
                ["launchctl", "list", label],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                # Try plist format first (macOS 26+): "PID" = <N>;
                pid = _parse_launchd_pid_from_list_output(result.stdout)
                if pid is not None and pid > 0:
                    pids.add(pid)
                else:
                    # Fall back to legacy tab-separated format:
                    # "PID\tStatus\tLabel"
                    for line in result.stdout.strip().splitlines():
                        parts = line.split()
                        if len(parts) >= 3 and parts[2] == label:
                            try:
                                pid = int(parts[0])
                                if pid > 0:
                                    pids.add(pid)
                            except ValueError:
                                pass
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    return pids


def _get_parent_pid(pid: int) -> int | None:
    """Return the parent PID for ``pid``, or ``None`` when unavailable.

    Uses psutil (core dependency) which works on every platform.  The
    older implementation shelled out to ``ps -o ppid= -p <pid>``, which
    silently fails on Windows (no ``ps``) so the ancestor walk terminated
    at self — the caller's dedup / exclude logic then couldn't distinguish
    "hermes CLI that invoked this scan" from "real gateway process".
    """
    if pid <= 1:
        return None
    try:
        import psutil  # type: ignore

        return psutil.Process(pid).ppid() or None
    except ImportError:
        pass
    except Exception:
        return None
    # Fallback: shell out to ps (POSIX only).  Git Bash installs ``ps.exe`` on
    # Windows; running it from the windowless desktop/gateway backend flashes a
    # console, and psutil above is the authoritative Windows path anyway.
    if is_windows():
        return None
    if not shutil.which("ps"):
        return None
    try:
        result = subprocess.run(
            ["ps", "-o", "ppid=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    raw = result.stdout.strip()
    if not raw:
        return None
    try:
        parent_pid = int(raw.splitlines()[-1].strip())
    except ValueError:
        return None
    return parent_pid if parent_pid > 0 else None


def _is_pid_ancestor_of_current_process(target_pid: int) -> bool:
    """Return True when ``target_pid`` is this process or one of its ancestors."""
    if target_pid <= 0:
        return False

    pid = os.getpid()
    seen: set[int] = set()
    while pid and pid not in seen:
        if pid == target_pid:
            return True
        seen.add(pid)
        pid = _get_parent_pid(pid) or 0
    return False


def _request_gateway_self_restart(
    pid: int,
    *,
    expected_identity: GatewayProcessIdentity | None = None,
) -> bool:
    """Ask a running gateway ancestor to restart itself asynchronously."""
    sigusr1 = getattr(signal, "SIGUSR1", None)
    if sigusr1 is None:
        return False
    if not _is_pid_ancestor_of_current_process(pid):
        return False
    identity = expected_identity
    if identity is None:
        try:
            identity = _capture_gateway_process_identity(
                pid,
                include_restart_managers=False,
            )
        except GatewayProcessTerminationError:
            return False
    if identity is None:
        return False
    if identity.pid != pid or identity.runtime_role is not GatewayRuntimeRole.RUNTIME:
        return False
    return _signal_gateway_process_identity(identity, sigusr1) == "signalled"


def _graceful_restart_via_sigusr1(
    pid: int,
    drain_timeout: float,
    expected_start_time: int | None = None,
    expected_identity: GatewayProcessIdentity | None = None,
) -> bool:
    """Send SIGUSR1 to a gateway PID and wait for it to exit gracefully.

    SIGUSR1 is wired in gateway/run.py to ``request_restart(via_service=True)``
    which drains in-flight agent runs (up to ``agent.restart_drain_timeout``
    seconds), then exits.  Both systemd (``Restart=always``) and launchd
    (unconditional ``<key>KeepAlive</key><true/>``) restart on any exit.

    This is the drain-aware alternative to ``systemctl restart`` / ``SIGTERM``,
    which SIGKILL in-flight agents after a short timeout.

    Args:
        pid: Gateway process PID (systemd MainPID, launchd PID, or bare
            process PID).
        drain_timeout: Seconds to wait for the process to exit after sending
            SIGUSR1.  Should be slightly larger than the gateway's
            ``agent.restart_drain_timeout`` to allow the drain loop to
            finish cleanly.
        expected_start_time: Optional process birth identity. When supplied,
            it must match the captured identity before the retained,
            revalidated handle can be signalled.

    Returns:
        True if the exact identity was signalled and exited within the timeout,
        or if it was already gone.
        False if SIGUSR1 couldn't be sent or the process didn't exit in
        time (caller should fall back to a harder restart path).
    """
    sigusr1 = getattr(signal, "SIGUSR1", None)
    if sigusr1 is None:
        return False
    if pid <= 0:
        return False
    if expected_identity is None:
        try:
            expected_identity = _capture_gateway_process_identity(
                pid,
                expected_start_time=expected_start_time,
            )
        except GatewayProcessTerminationError:
            return False
        if expected_identity is None:
            # The process vanished before we could capture a canonical
            # identity. There is nothing left to signal.
            return True
    elif expected_identity.pid != pid:
        return False

    if (
        expected_start_time is not None
        and expected_identity.start_time != expected_start_time
    ):
        return False

    signal_result = _signal_gateway_process_identity(
        expected_identity, sigusr1
    )
    if signal_result == "gone":
        return True
    if signal_result != "signalled":
        return False
    import time as _time

    if expected_identity is not None:
        try:
            return _wait_for_exact_gateway_identity_exit(
                expected_identity, max(drain_timeout, 1.0)
            )
        except GatewayProcessTerminationError:
            # A changed/unreadable identity after SIGUSR1 is not proof that the
            # predecessor converged safely.  The caller must fail closed and
            # refuse to signal or accept a successor on this transaction.
            return False

    deadline = _time.monotonic() + max(drain_timeout, 1.0)
    # IMPORTANT Windows note: ``os.kill(pid, 0)`` is NOT a no-op on
    # Windows — Python's implementation calls ``TerminateProcess(handle, 0)``
    # for sig=0, hard-killing the target. Use the cross-platform
    # ``_pid_exists`` helper in gateway.status which does OpenProcess +
    # WaitForSingleObject on Windows.
    from gateway.status import _pid_exists

    while _time.monotonic() < deadline:
        if not _pid_exists(pid):
            return True
        _time.sleep(0.5)
    # Drain didn't finish in time.
    return False


def _get_ancestor_pids() -> set[int]:
    """Return the set of PIDs in the current process's ancestor chain.

    Walks from the current PID up to PID 1 (init) so that process-table scans
    never match the calling CLI process or any of its parents.  This prevents
    ``hermes gateway status`` from falsely counting the ``hermes`` CLI that
    invoked it as a running gateway instance (see #13242).
    """
    ancestors: set[int] = set()
    pid = os.getpid()
    # Cap iterations to avoid infinite loops on exotic platforms.
    for _ in range(64):
        ancestors.add(pid)
        parent = _get_parent_pid(pid)
        if parent is None or parent <= 0 or parent in ancestors:
            break
        pid = parent
    return ancestors


def _append_unique_pid(
    pids: list[int], pid: int | None, exclude_pids: set[int]
) -> None:
    if pid is None or pid <= 0:
        return
    if pid == os.getpid() or pid in exclude_pids or pid in pids:
        return
    pids.append(pid)


def _process_table_ps_command() -> list[str]:
    """Return a full-width, command-only process-table invocation.

    BSD/macOS ``ps`` rejects the old separate ``eww`` operand, while the
    ``e`` flag on platforms that accept it appends every process environment
    to stdout.  That is both unnecessary for gateway matching and capable of
    exposing runtime secrets.  Keep the platform-specific output field
    explicit and request only argv with unlimited width.
    """
    if is_macos():
        output_field = "command"
    elif is_linux():
        output_field = "args"
    else:
        # Other BSD-family ps implementations generally use ``command``.
        output_field = "command"
    return ["ps", "-A", "-ww", "-o", f"pid=,{output_field}="]


def _scan_gateway_pids(
    exclude_pids: set[int],
    all_profiles: bool = False,
    include_restart_managers: bool = False,
    *,
    strict: bool = False,
) -> list[int]:
    """Best-effort process-table scan for gateway PIDs.

    This supplements the profile-scoped PID file so status views can still spot
    a live gateway when the PID file is stale/missing, and ``--all`` sweeps can
    discover gateways outside the current profile.
    """
    # Exclude the entire ancestor chain so the CLI process that invoked this
    # scan (e.g. ``hermes gateway status``) is never mistaken for a running
    # gateway.  See #13242.
    exclude_pids = exclude_pids | _get_ancestor_pids()
    pids: list[int] = []
    # Strict command-line matcher shared with gateway.status: requires the
    # actual ``gateway run`` subcommand (or the dedicated entrypoints), so this
    # scan no longer false-matches ``gateway status``/``dashboard`` siblings or
    # unrelated processes like ``python -m tui_gateway``. Lazy import mirrors the
    # circular-import avoidance used elsewhere in this module.
    current_home = get_hermes_home().resolve()
    current_profile_arg = _profile_arg(str(current_home))
    current_profile_name = (
        current_profile_arg.split()[-1] if current_profile_arg else None
    )
    current_profile_root = (
        current_home.parent.parent
        if current_home.parent.name == "profiles"
        else current_home
    )

    def _matches_current_profile(command: str) -> bool:
        _role, profile, home = classify_gateway_argv(
            command, default_home=current_profile_root
        )
        return profile == current_profile_name and home == current_home

    def _matches_gateway_runtime(command: str) -> bool:
        role, _profile, _home = classify_gateway_argv(command)
        if role is GatewayRuntimeRole.RUNTIME:
            return True
        # Best-effort no-supervisor status/recovery retains the historical
        # manager-runtime compatibility.  Strict destructive inventory never
        # sets this flag, so a concurrent `gateway restart` manager can never
        # be signalled or turn into a false inventory error there.
        return (
            include_restart_managers
            and role is GatewayRuntimeRole.MANAGER
            and gateway_command_subcommand(command) == "restart"
        )

    try:
        if is_windows():
            # Prefer wmic when present (fast, stable output format).  On
            # modern Windows 11 / Win 10 late builds, wmic has been
            # removed as part of the WMIC deprecation — fall back to
            # PowerShell's Get-CimInstance.  Any OSError here (FileNotFoundError
            # on missing wmic) trips the fallback.
            # Hide the console window: this scan runs inside the windowless
            # pythonw.exe gateway/desktop backend, so a bare wmic/powershell
            # spawn would flash a conhost window on every watchdog probe.
            from hermes_cli._subprocess_compat import windows_hide_flags

            _no_window = {"creationflags": windows_hide_flags()}
            wmic_path = shutil.which("wmic")
            used_fallback = False
            result = None
            if wmic_path is not None:
                try:
                    result = subprocess.run(
                        [
                            wmic_path,
                            "process",
                            "get",
                            "ProcessId,CommandLine",
                            "/FORMAT:LIST",
                        ],
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="ignore",
                        timeout=10,
                        **_no_window,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    result = None
            if result is None or result.returncode != 0 or not (result.stdout or ""):
                # Fallback: PowerShell Get-CimInstance, emit LIST-style output
                # so the downstream parser below doesn't need to branch.
                powershell = shutil.which("powershell") or shutil.which("pwsh")
                if powershell is None:
                    if strict:
                        raise GatewayProcessEnumerationError(
                            "gateway process enumeration is unavailable: "
                            "neither wmic nor PowerShell is installed"
                        )
                    return []
                ps_cmd = (
                    "Get-CimInstance Win32_Process | "
                    "ForEach-Object { "
                    "  'CommandLine=' + ($_.CommandLine -replace \"`r`n\",' ' -replace \"`n\",' '); "
                    "  'ProcessId=' + $_.ProcessId; "
                    "  '' "
                    "}"
                )
                try:
                    result = subprocess.run(
                        [powershell, "-NoProfile", "-Command", ps_cmd],
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="ignore",
                        timeout=15,
                        **_no_window,
                    )
                except (OSError, subprocess.TimeoutExpired) as exc:
                    if strict:
                        raise GatewayProcessEnumerationError(
                            "gateway process enumeration via PowerShell failed"
                        ) from exc
                    return []
                used_fallback = True
            if result.returncode != 0 or result.stdout is None:
                if strict:
                    raise GatewayProcessEnumerationError(
                        "gateway process enumeration command returned an error"
                    )
                return []
            current_cmd = ""
            for line in result.stdout.split("\n"):
                line = line.strip()
                if line.startswith("CommandLine="):
                    current_cmd = line[len("CommandLine=") :]
                elif line.startswith("ProcessId="):
                    pid_str = line[len("ProcessId=") :]
                    if _matches_gateway_runtime(current_cmd) and (
                        all_profiles or _matches_current_profile(current_cmd)
                    ):
                        try:
                            _append_unique_pid(pids, int(pid_str), exclude_pids)
                        except ValueError:
                            pass
                    current_cmd = ""
        else:
            # Try /proc first (works in Docker without procps installed),
            # then use the platform-safe, environment-free ps fallback.
            _found_via_proc = False
            if os.path.isdir("/proc"):
                try:
                    my_pid = os.getpid()
                    for entry in os.listdir("/proc"):
                        if not entry.isdigit():
                            continue
                        pid = int(entry)
                        if pid == my_pid or pid in exclude_pids:
                            continue
                        try:
                            with open(f"/proc/{pid}/cmdline", "rb") as _f:
                                cmdline = _f.read().decode("utf-8", errors="replace")
                            cmdline = cmdline.replace("\x00", " ")
                            if _matches_gateway_runtime(cmdline) and (
                                all_profiles or _matches_current_profile(cmdline)
                            ):
                                _append_unique_pid(pids, pid, exclude_pids)
                        except FileNotFoundError:
                            # A process can exit between listdir() and open().
                            # That is a normal scan race, not an incomplete
                            # inventory.
                            continue
                        except (OSError, PermissionError) as exc:
                            if strict:
                                raise GatewayProcessEnumerationError(
                                    "gateway process enumeration could not inspect "
                                    f"/proc/{pid}/cmdline"
                                ) from exc
                            continue
                    _found_via_proc = True
                except GatewayProcessEnumerationError:
                    raise
                except Exception:
                    if strict:
                        logger.debug(
                            "gateway /proc enumeration failed; trying ps",
                            exc_info=True,
                        )

            if not _found_via_proc:
                result = subprocess.run(
                    _process_table_ps_command(),
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if result.returncode != 0:
                    if strict:
                        raise GatewayProcessEnumerationError(
                            "gateway process enumeration via ps returned an error"
                        )
                    return []
                for line in result.stdout.split("\n"):
                    stripped = line.strip()
                    if not stripped or "grep" in stripped:
                        continue

                    pid = None
                    command = ""

                    parts = stripped.split(None, 1)
                    if len(parts) == 2:
                        try:
                            pid = int(parts[0])
                            command = parts[1]
                        except ValueError:
                            pid = None

                    if pid is None:
                        aux_parts = stripped.split()
                        if len(aux_parts) > 10 and aux_parts[1].isdigit():
                            pid = int(aux_parts[1])
                            command = " ".join(aux_parts[10:])

                    if pid is None:
                        continue
                    if _matches_gateway_runtime(command) and (
                        all_profiles or _matches_current_profile(command)
                    ):
                        _append_unique_pid(pids, pid, exclude_pids)
    except GatewayProcessEnumerationError:
        raise
    except (OSError, subprocess.TimeoutExpired) as exc:
        if strict:
            raise GatewayProcessEnumerationError(
                "gateway process enumeration failed"
            ) from exc
        return []

    # Windows-specific: collapse venv launcher stubs.  A venv-built
    # ``pythonw.exe`` in ``<venv>/Scripts/`` is a ~100 KB launcher exe
    # that spawns the base Python (e.g. ``C:\Program Files\Python311\
    # pythonw.exe``) with the same command line, preserving the venv's
    # ``pyvenv.cfg`` context.  This is standard Windows CPython venv
    # behaviour — BUT it means every gateway run produces two pythonw
    # PIDs with identical command lines (one launcher stub, one actual
    # interpreter) which is confusing in ``gateway status`` output.
    # Filter the stub: if a PID in our result is the PARENT of another
    # PID in our result, and both are pythonw.exe, the parent is the
    # launcher stub — drop it, keep the child.
    if is_windows() and len(pids) > 1:
        pids = _filter_venv_launcher_stubs(pids)

    return pids


def _filter_venv_launcher_stubs(pids: list[int]) -> list[int]:
    """Drop venv-launcher ``pythonw.exe`` stubs that are parents of the real
    interpreter process.  See comment at the tail of ``_scan_gateway_pids``.

    Uses ``psutil`` (core dependency).  Safe on any platform; only invoked
    on Windows by the caller because the stub pattern is Windows-specific.
    """
    try:
        import psutil  # type: ignore
    except ImportError:
        return pids

    pid_set = set(pids)
    # Collect each PID's parent so we can flag "child of another matched PID".
    parent_of: dict[int, int | None] = {}
    for pid in pids:
        try:
            parent_of[pid] = psutil.Process(pid).ppid()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            parent_of[pid] = None

    # For each child whose parent is also in our set, drop the parent.
    drop: set[int] = set()
    for pid, ppid in parent_of.items():
        if ppid is not None and ppid in pid_set:
            drop.add(ppid)

    return [p for p in pids if p not in drop]


def find_gateway_pids(
    exclude_pids: set | None = None, all_profiles: bool = False
) -> list:
    """Find PIDs of running gateway processes.

    Args:
        exclude_pids: PIDs to exclude from the result (e.g. service-managed
            PIDs that should not be killed during a stale-process sweep).
        all_profiles: When ``True``, return gateway PIDs across **all**
            profiles (the pre-7923 global behaviour).  ``hermes update``
            needs this because a code update affects every profile.
            When ``False`` (default), only PIDs belonging to the current
            Hermes profile are returned.
    """
    _exclude = set(exclude_pids or set())
    pids: list[int] = []
    if not all_profiles:
        try:
            from gateway.status import get_running_pid

            _append_unique_pid(pids, get_running_pid(), _exclude)
        except Exception:
            pass
    for pid in _get_service_pids():
        _append_unique_pid(pids, pid, _exclude)
    try:
        include_restart_managers = not supports_systemd_services()
    except Exception:
        include_restart_managers = False
    for pid in _scan_gateway_pids(
        _exclude,
        all_profiles=all_profiles,
        include_restart_managers=include_restart_managers,
    ):
        _append_unique_pid(pids, pid, _exclude)
    return pids


def find_gateway_pids_strict(
    exclude_pids: set | None = None,
    all_profiles: bool = True,
    *,
    include_restart_managers: bool | None = None,
) -> list[int]:
    """Return gateway PIDs or raise when the process table is unavailable.

    This is the fail-closed inventory for update/config transactions.  The
    historical :func:`find_gateway_pids` remains best-effort for status views
    and service UX where an unavailable process utility should not crash the
    command.  Strict callers must never interpret an enumeration failure as
    "zero gateways" and begin mutating shared runtime state.
    """
    exclude = set(exclude_pids or set())
    pids: list[int] = []
    if not all_profiles:
        try:
            from gateway.status import get_running_pid

            _append_unique_pid(pids, get_running_pid(), exclude)
        except Exception as exc:
            raise GatewayProcessEnumerationError(
                "current-profile gateway PID could not be inspected"
            ) from exc
    if include_restart_managers is None:
        # On hosts without a supervisor, the restart command itself can host
        # the runtime while it drains/relaunches. Generic strict inventory
        # callers (update/config) need to retain that coverage; callers that
        # will signal a launchd-owned process must explicitly request the
        # runtime-only policy below.
        try:
            include_restart_managers = not supports_systemd_services()
        except Exception:
            include_restart_managers = False
    elif include_restart_managers:
        try:
            include_restart_managers = not supports_systemd_services()
        except Exception:
            include_restart_managers = False
    try:
        scanned_pids = _scan_gateway_pids(
            exclude,
            all_profiles=all_profiles,
            include_restart_managers=include_restart_managers,
            strict=True,
        )
    except GatewayProcessEnumerationError:
        raise
    except Exception as exc:
        raise GatewayProcessEnumerationError(
            "gateway process enumeration failed"
        ) from exc
    for pid in scanned_pids:
        _append_unique_pid(pids, pid, exclude)
    return pids


def _psutil_process(pid: int):
    """Return a psutil process handle for identity-aware destructive work."""
    try:
        import psutil  # type: ignore
    except ImportError as exc:
        raise GatewayProcessEnumerationError(
            "psutil is required for strict gateway process identity checks"
        ) from exc
    return psutil.Process(pid)


def _read_live_gateway_process_identity(
    pid: int,
    *,
    allowed_roles: tuple[GatewayRuntimeRole, ...] = (GatewayRuntimeRole.RUNTIME,),
) -> tuple[GatewayProcessIdentity, object]:
    """Read one live PID's canonical birth/argv/profile/home attestation.

    ``psutil`` is asked for the command line and only the single
    ``HERMES_HOME`` environment key.  The full environment is discarded
    immediately so credentials can never enter an identity, log, or error.
    """
    try:
        import psutil  # type: ignore
    except ImportError as exc:
        raise GatewayProcessEnumerationError(
            "psutil is required for strict gateway process identity checks"
        ) from exc

    process = None
    try:
        process = _psutil_process(pid)
        start_time = _launchd_process_start_time(pid)
        argv = tuple(process.cmdline() or ())
        process_environment = process.environ()
        hermes_home_value = (
            process_environment.get("HERMES_HOME")
            if isinstance(process_environment, dict)
            else None
        )
        # Never retain the process environment: it may contain API keys.
        del process_environment
    except (ProcessLookupError, FileNotFoundError):
        raise
    except (psutil.NoSuchProcess, psutil.ZombieProcess) as exc:
        raise ProcessLookupError(pid) from exc
    except (PermissionError, psutil.AccessDenied):
        # macOS reports EINVAL->AccessDenied for KERN_PROCARGS2 argv/env
        # reads of zombie (exited, unreaped) processes. A zombie is dead,
        # not protected: report it as gone rather than unreadable.
        try:
            if process is not None and process.status() == psutil.STATUS_ZOMBIE:
                raise ProcessLookupError(pid) from None
        except psutil.NoSuchProcess:
            raise ProcessLookupError(pid) from None
        except psutil.Error:
            pass
        raise PermissionError(pid)
    except Exception as exc:
        raise GatewayProcessEnumerationError(
            f"could not inspect gateway PID {pid} identity"
        ) from exc
    if start_time is None:
        raise GatewayProcessEnumerationError(
            f"gateway PID {pid} has no readable birth identity"
        )
    if not argv:
        raise GatewayProcessEnumerationError(
            f"gateway PID {pid} has no readable argv"
        )
    identity = GatewayProcessIdentity(
        pid,
        start_time,
        argv,
        environment=(
            {"HERMES_HOME": hermes_home_value}
            if isinstance(hermes_home_value, str)
            else {}
        ),
    )
    if identity.runtime_role not in allowed_roles:
        raise GatewayProcessEnumerationError(
            f"gateway PID {pid} is not an attested gateway process for the requested role"
        )
    if identity.hermes_home is None:
        raise GatewayProcessEnumerationError(
            f"gateway PID {pid} has no resolved HERMES_HOME identity"
        )
    return identity, process


def _gateway_process_identity_roles(
    include_restart_managers: bool | None,
) -> tuple[GatewayRuntimeRole, ...]:
    """Return the exact roles allowed for destructive process operations."""
    if include_restart_managers is None:
        try:
            include_restart_managers = not supports_systemd_services()
        except Exception:
            include_restart_managers = False
    if include_restart_managers:
        try:
            include_restart_managers = not supports_systemd_services()
        except Exception:
            include_restart_managers = False
    if include_restart_managers:
        return (GatewayRuntimeRole.RUNTIME, GatewayRuntimeRole.MANAGER)
    return (GatewayRuntimeRole.RUNTIME,)


def _capture_gateway_process_identity(
    pid: int,
    *,
    expected_start_time: int | None = None,
    include_restart_managers: bool | None = None,
) -> GatewayProcessIdentity | None:
    """Capture one allowed identity, without retaining process environment."""
    if pid <= 0:
        return None
    try:
        allowed_roles = _gateway_process_identity_roles(include_restart_managers)
        if allowed_roles == (GatewayRuntimeRole.RUNTIME,):
            identity, _process = _read_live_gateway_process_identity(pid)
        else:
            identity, _process = _read_live_gateway_process_identity(
                pid, allowed_roles=allowed_roles
            )
    except ProcessLookupError:
        return None
    except PermissionError as exc:
        raise GatewayProcessTerminationError(
            [f"PID {pid}: permission denied while capturing identity"]
        ) from exc
    except GatewayProcessEnumerationError as exc:
        raise GatewayProcessTerminationError([f"PID {pid}: {exc}"]) from exc
    if expected_start_time is not None and identity.start_time != expected_start_time:
        raise GatewayProcessTerminationError(
            [f"PID {pid}: birth identity changed; signal skipped"]
        )
    return identity


def _capture_current_profile_gateway_identity(
    pid: int,
    *,
    include_restart_managers: bool | None = None,
) -> GatewayProcessIdentity | None:
    """Capture a PID only when it belongs to the active HERMES_HOME.

    PID files and legacy runtime records are discovery hints, not authority for
    destructive work.  A stale record may point at a PID now occupied by a
    different profile's gateway.  Retain the live birth/argv identity and bind
    it to the active profile before any signal is permitted.
    """
    identity = _capture_gateway_process_identity(
        pid,
        include_restart_managers=include_restart_managers,
    )
    if identity is None:
        return None
    expected_home = get_hermes_home().resolve()
    if identity.hermes_home != expected_home:
        raise GatewayProcessTerminationError(
            [
                f"PID {pid}: gateway belongs to {identity.hermes_home}, not "
                f"the active HERMES_HOME {expected_home}; signal skipped"
            ]
        )
    return identity


def find_gateway_process_identities_strict(
    exclude_pids: set[int] | None = None,
    all_profiles: bool = True,
    *,
    include_restart_managers: bool = False,
) -> list[GatewayProcessIdentity]:
    """Return strict gateway inventory with a birth and command attestation.

    The historical integer inventory remains available to status and update
    callers.  macOS destructive transactions use this stronger companion so a
    later signal cannot silently follow a recycled PID or an argv change.
    """
    allowed_roles = _gateway_process_identity_roles(include_restart_managers)
    identities: list[GatewayProcessIdentity] = []
    effective_include_restart_managers = (
        GatewayRuntimeRole.MANAGER in allowed_roles
    )
    for pid in find_gateway_pids_strict(
        exclude_pids=exclude_pids,
        all_profiles=all_profiles,
        include_restart_managers=effective_include_restart_managers,
    ):
        try:
            if allowed_roles == (GatewayRuntimeRole.RUNTIME,):
                identity, _process = _read_live_gateway_process_identity(pid)
            else:
                identity, _process = _read_live_gateway_process_identity(
                    pid, allowed_roles=allowed_roles
                )
        except ProcessLookupError:
            # A process can exit between the strict scan and identity read.
            continue
        except PermissionError as exc:
            raise GatewayProcessEnumerationError(
                f"could not inspect gateway PID {pid} identity: permission denied"
            ) from exc
        except GatewayProcessEnumerationError:
            raise
        except OSError as exc:
            raise GatewayProcessEnumerationError(
                f"could not inspect gateway PID {pid} identity"
            ) from exc
        identities.append(identity)
    return identities


def find_profile_gateway_processes(
    exclude_pids: set | None = None,
) -> list[ProfileGatewayProcess]:
    """Return running gateway PIDs mapped to Hermes profiles via PID files."""
    _exclude = set(exclude_pids or set())
    processes: list[ProfileGatewayProcess] = []
    try:
        from gateway.status import get_running_pid
        from hermes_cli.profiles import list_profiles
    except Exception:
        return processes

    seen: set[int] = set()
    for profile in list_profiles():
        try:
            pid = get_running_pid(profile.path / "gateway.pid", cleanup_stale=False)
        except Exception:
            continue
        if pid is None or pid <= 0 or pid in _exclude or pid in seen:
            continue
        seen.add(pid)
        processes.append(
            ProfileGatewayProcess(profile=profile.name, path=profile.path, pid=pid)
        )
    return processes


def _gateway_run_args_for_profile(profile: str) -> list[str]:
    args = [get_python_path(), "-m", "hermes_cli.main"]
    if profile != "default":
        args.extend(["--profile", profile])
    args.extend(["gateway", "run", "--replace"])
    return args


def _capture_gateway_argv(pid: int) -> list[str] | None:
    """Return the live argv of a running gateway process, or ``None``.

    Used to respawn gateways that have no profile→PID-file mapping (e.g. a
    Windows Scheduled Task running ``pythonw.exe -m hermes_cli.main gateway
    run``). ``_pause_windows_gateways_for_update`` force-kills such gateways
    before mutating the venv; without their original command line we cannot
    bring them back, so we snapshot it here before the kill.

    Best-effort: returns ``None`` if psutil is unavailable, the process is
    gone, access is denied, or the argv doesn't look like a gateway command.
    """
    if pid <= 1:
        return None
    try:
        import psutil  # type: ignore
    except ImportError:
        return None
    try:
        argv = list(psutil.Process(pid).cmdline() or [])
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return None
    except Exception:
        return None
    if not argv:
        return None
    # Guard against snapshotting an unrelated process whose PID happened to be
    # reported by the scan: only respawn things that actually look like a
    # gateway run command line.
    try:
        role, _profile, _home = classify_gateway_argv(argv)
        if role is not GatewayRuntimeRole.RUNTIME:
            return None
    except Exception:
        pass
    return argv


def launch_detached_gateway_restart_by_cmdline(
    old_pid: int, run_argv: list[str]
) -> bool:
    """Relaunch a gateway by replaying its captured command line after exit.

    Companion to ``launch_detached_profile_gateway_restart`` for gateways that
    have no profile→PID-file mapping (Scheduled-Task / manually-launched
    ``gateway run`` whose HERMES_HOME or argv doesn't match a known profile).
    Uses the identical detached-watcher mechanism; only the respawn argv
    differs (the process's own argv instead of a profile-derived one).
    """
    if old_pid <= 0 or not run_argv:
        return False
    return _spawn_gateway_restart_watcher(old_pid, list(run_argv))


def launch_detached_profile_gateway_restart(profile: str, old_pid: int) -> bool:
    """Relaunch a manually-run profile gateway after its current PID exits."""
    if old_pid <= 0:
        return False
    return _spawn_gateway_restart_watcher(old_pid, _gateway_run_args_for_profile(profile))


def _spawn_gateway_restart_watcher(old_pid: int, run_argv: list[str]) -> bool:
    """Spawn the detached watcher that respawns ``run_argv`` once ``old_pid`` exits."""
    if old_pid <= 0 or not run_argv:
        return False

    # The watcher is a tiny Python subprocess that polls the old PID and
    # respawns the gateway once it's gone.  Both legs of the chain need
    # platform-appropriate detach semantics:
    #
    # POSIX — ``start_new_session=True`` (os.setsid in the child) detaches
    # from the parent's process group so Ctrl+C in the CLI doesn't
    # propagate and the watcher/gateway survive the CLI exiting.
    #
    # Windows — ``start_new_session`` is silently accepted but does NOT
    # detach.  The watcher stays attached to the CLI's console and dies
    # when the user closes the terminal, leaving ``hermes update`` users
    # with no running gateway until they re-invoke ``hermes gateway``
    # manually.  The Win32 equivalent is the ``CREATE_NEW_PROCESS_GROUP |
    # DETACHED_PROCESS | CREATE_NO_WINDOW`` creationflags bundle.
    #
    # ``windows_detach_popen_kwargs()`` returns the right kwargs for the
    # host platform and is a no-op on POSIX (just ``start_new_session=True``).
    from hermes_cli._subprocess_compat import (
        windows_detach_flags_without_breakaway,
        windows_detach_popen_kwargs,
    )

    # On Windows the incoming ``run_argv`` leads with the venv's console
    # ``python.exe`` (from ``get_python_path()``).  Respawning the gateway
    # with that interpreter — even under CREATE_NO_WINDOW — leaves a
    # persistent console window, because uv's venv launcher re-execs the
    # base console interpreter, which allocates its own conhost.  Rewrite
    # the argv to the windowless ``pythonw.exe`` (mirroring the clean-start
    # ``_spawn_detached`` path) and capture the cwd + env overlay the base
    # interpreter needs to resolve imports without the venv launcher.
    # No-op on POSIX.  See gateway_windows.windowless_gateway_restart_spec.
    respawn_cwd = ""
    respawn_env_overlay: dict[str, str] = {}
    if sys.platform == "win32":
        try:
            from hermes_cli.gateway_windows import (
                windowless_gateway_restart_spec,
            )

            run_argv, respawn_cwd, respawn_env_overlay = (
                windowless_gateway_restart_spec(list(run_argv))
            )
        except Exception:
            # Best-effort: if the rewrite fails for any reason, fall back to
            # the original argv.  A visible window is worse than nothing, but
            # a failed respawn is worse still — keep the gateway coming back.
            respawn_cwd = ""
            respawn_env_overlay = {}

    # Serialized as JSON literals embedded in the watcher source so the
    # inner respawn can apply cwd= / env= without extra argv plumbing.
    respawn_cwd_literal = json.dumps(respawn_cwd)
    respawn_env_literal = json.dumps(respawn_env_overlay)

    watcher = textwrap.dedent(
        """
        import os
        import subprocess
        import sys
        import time
        from hermes_cli._subprocess_compat import (
            windows_detach_flags,
            windows_detach_flags_without_breakaway,
        )

        pid = int(sys.argv[1])
        cmd = sys.argv[2:]
        _respawn_cwd = {respawn_cwd_literal}
        _respawn_env_overlay = {respawn_env_literal}
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            # ``os.kill(pid, 0)`` is not a no-op on Windows — use the
            # cross-platform existence check.
            from gateway.status import _pid_exists
            if not _pid_exists(pid):
                break
            time.sleep(0.2)

        # Platform-appropriate detach for the respawned gateway.  On POSIX
        # start_new_session=True maps to os.setsid; on Windows we need
        # explicit creationflags because start_new_session is a no-op there.
        # CREATE_BREAKAWAY_FROM_JOB is critical: the watcher itself may have
        # been spawned inside a job object (Electron/Tauri parent), and
        # without breakaway the respawned gateway would die when that job
        # tears down. See _subprocess_compat.windows_detach_flags().
        _popen_kwargs = {{
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }}
        # Anchor the respawned gateway at the stable working dir and overlay
        # the env (VIRTUAL_ENV / PYTHONPATH / HERMES_HOME) the windowless
        # base interpreter needs to import hermes_cli.  Empty on POSIX, where
        # the venv python resolves imports without help.
        if _respawn_cwd:
            _popen_kwargs["cwd"] = _respawn_cwd
        if _respawn_env_overlay:
            _popen_kwargs["env"] = {{**os.environ, **_respawn_env_overlay}}
        if sys.platform == "win32":
            try:
                _popen_kwargs["creationflags"] = windows_detach_flags()
                subprocess.Popen(cmd, **_popen_kwargs)
            except OSError:
                # CREATE_BREAKAWAY_FROM_JOB can be rejected with
                # ERROR_ACCESS_DENIED when the parent's job object refuses
                # breakaway. Retry without it — DETACHED_PROCESS et al.
                # alone are enough in most setups. Mirrors the canonical
                # fallback in gateway_windows._spawn_detached.
                _popen_kwargs["creationflags"] = windows_detach_flags_without_breakaway()
                subprocess.Popen(cmd, **_popen_kwargs)
        else:
            _popen_kwargs["start_new_session"] = True
            subprocess.Popen(cmd, **_popen_kwargs)
        """
    ).strip().format(
        respawn_cwd_literal=respawn_cwd_literal,
        respawn_env_literal=respawn_env_literal,
    )

    watcher_argv = [
        sys.executable,
        "-c",
        watcher,
        str(old_pid),
        *run_argv,
    ]

    # Same platform-aware detach for the watcher process itself — so
    # closing the user's terminal doesn't kill the watcher.
    try:
        subprocess.Popen(
            watcher_argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **windows_detach_popen_kwargs(),
        )
    except OSError:
        # CREATE_BREAKAWAY_FROM_JOB rejected by the parent job object
        # (Electron, Windows Terminal with restrictive job settings, …).
        # Retry without it. POSIX never reaches this branch — there
        # ``start_new_session=True`` cannot raise OSError — so the
        # fallback is only meaningful on Windows.
        try:
            fallback_kwargs: dict = (
                {"creationflags": windows_detach_flags_without_breakaway()}
                if sys.platform == "win32"
                else {"start_new_session": True}
            )
            subprocess.Popen(
                watcher_argv,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **fallback_kwargs,
            )
        except OSError:
            return False
    return True


def _probe_systemd_service_running(system: bool = False) -> tuple[bool, bool]:
    selected_system = _select_systemd_scope(system)
    unit_exists = get_systemd_unit_path(system=selected_system).exists()
    if not unit_exists:
        return selected_system, False
    try:
        result = _run_systemctl(
            ["is-active", get_service_name()],
            system=selected_system,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (RuntimeError, subprocess.TimeoutExpired):
        return selected_system, False
    return selected_system, result.stdout.strip() == "active"


def _read_systemd_unit_environment(system: bool = False) -> dict[str, str]:
    """Parse the gateway unit's ``Environment=`` directives.

    ``systemctl show -p Environment`` returns a single line of
    space-separated ``KEY=VALUE`` pairs; values are not quoted in the output
    even when the unit file quoted them. We split on whitespace and ``=``.
    """
    selected_system = _select_systemd_scope(system)
    try:
        result = _run_systemctl(
            [
                "show",
                get_service_name(),
                "--no-pager",
                "--property",
                "Environment",
            ],
            system=selected_system,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (RuntimeError, subprocess.TimeoutExpired, OSError):
        return {}
    if result.returncode != 0:
        return {}
    parsed: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if not line.startswith("Environment="):
            continue
        body = line[len("Environment=") :].strip()
        for token in body.split():
            if "=" not in token:
                continue
            key, value = token.split("=", 1)
            parsed[key] = value
    return parsed


def _sync_hermes_home_from_systemd_unit(system: bool) -> None:
    """When acting on a system-scope unit, adopt its ``HERMES_HOME``.

    Under ``sudo``, ``HERMES_HOME`` is stripped and ``HOME=/root``, so
    :func:`get_hermes_home` falls back to ``/root/.hermes`` — the wrong
    profile. The unit file pins ``HERMES_HOME`` for the actual gateway
    process, so we mirror that into our own environment to make
    ``read_runtime_status`` / ``get_running_pid`` read the correct files.
    """
    if not system:
        return
    env = _read_systemd_unit_environment(system=True)
    unit_home = env.get("HERMES_HOME", "").strip()
    if not unit_home:
        return
    current = os.environ.get("HERMES_HOME", "").strip()
    if current == unit_home:
        return
    os.environ["HERMES_HOME"] = unit_home


def _read_systemd_unit_properties(
    system: bool = False,
    properties: tuple[str, ...] = (
        "ActiveState",
        "SubState",
        "Result",
        "ExecMainStatus",
        "MainPID",
    ),
) -> dict[str, str]:
    """Return selected ``systemctl show`` properties for the gateway unit."""
    selected_system = _select_systemd_scope(system)
    try:
        result = _run_systemctl(
            [
                "show",
                get_service_name(),
                "--no-pager",
                "--property",
                ",".join(properties),
            ],
            system=selected_system,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (RuntimeError, subprocess.TimeoutExpired, OSError):
        return {}

    if result.returncode != 0:
        return {}

    parsed: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        parsed[key] = value.strip()
    return parsed


def _systemd_main_pid_from_props(props: dict[str, str]) -> int | None:
    try:
        pid = int(props.get("MainPID", "0") or "0")
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def _systemd_main_pid(system: bool = False) -> int | None:
    return _systemd_main_pid_from_props(_read_systemd_unit_properties(system=system))


def _read_gateway_runtime_status() -> dict | None:
    try:
        from gateway.status import read_runtime_status

        state = read_runtime_status()
    except Exception:
        return None
    return state if isinstance(state, dict) else None


def _gateway_runtime_status_for_pid(pid: int | None) -> dict | None:
    if not pid:
        return None
    state = _read_gateway_runtime_status()
    if not state:
        return None
    try:
        state_pid = int(state.get("pid", 0) or 0)
    except (TypeError, ValueError):
        return None
    return state if state_pid == pid else None


def _wait_for_systemd_service_restart(
    *,
    system: bool = False,
    previous_pid: int | None = None,
    timeout: float = 60.0,
) -> bool:
    """Wait for the gateway service to become active after a restart handoff."""
    import time

    svc = get_service_name()
    scope_label = _service_scope_label(system).capitalize()
    deadline = time.monotonic() + timeout
    printed_runtime_wait = False

    while time.monotonic() < deadline:
        props = _read_systemd_unit_properties(system=system)
        active_state = props.get("ActiveState", "")
        sub_state = props.get("SubState", "")
        new_pid = None
        try:
            from gateway.status import get_running_pid

            new_pid = get_running_pid()
        except Exception:
            new_pid = None
        if not new_pid:
            new_pid = _systemd_main_pid_from_props(props)

        if active_state == "active":
            if new_pid and (previous_pid is None or new_pid != previous_pid):
                runtime_state = _gateway_runtime_status_for_pid(new_pid)
                gateway_state = (runtime_state or {}).get("gateway_state")
                if gateway_state == "running":
                    print(f"✓ {scope_label} service restarted (PID {new_pid})")
                    return True
                if gateway_state == "startup_failed":
                    reason = (runtime_state or {}).get(
                        "exit_reason"
                    ) or "startup failed"
                    print(
                        f"⚠ {scope_label} service process restarted (PID {new_pid}), but gateway startup failed: {reason}"
                    )
                    return False
                if not printed_runtime_wait:
                    print(
                        f"⏳ {scope_label} service process started (PID {new_pid}); waiting for gateway runtime..."
                    )
                    printed_runtime_wait = True

        if active_state == "activating" and sub_state == "auto-restart":
            time.sleep(1)
            continue

        if _systemd_unit_is_start_limited(props):
            _print_systemd_start_limit_wait(system=system)
            return False

        time.sleep(2)

    print(
        f"⚠ {scope_label} service did not become active within {int(timeout)}s.\n"
        f"  Check status: {'sudo ' if system else ''}hermes gateway status\n"
        f"  Check logs:   journalctl {'--user ' if not system else ''}-u {svc} -l --since '2 min ago'"
    )
    return False


def _systemd_unit_is_start_limited(props: dict[str, str]) -> bool:
    result = props.get("Result", "").lower()
    sub_state = props.get("SubState", "").lower()
    return result == "start-limit-hit" or sub_state == "start-limit-hit"


def _systemd_error_indicates_start_limit(exc: subprocess.CalledProcessError) -> bool:
    parts: list[str] = []
    for attr in ("stderr", "stdout", "output"):
        value = getattr(exc, attr, None)
        if not value:
            continue
        if isinstance(value, bytes):
            value = value.decode(errors="replace")
        parts.append(str(value))
    text = "\n".join(parts).lower()
    return (
        "start-limit-hit" in text
        or "start request repeated too quickly" in text
        or "start-limit" in text
    )


def _systemd_service_is_start_limited(system: bool = False) -> bool:
    return _systemd_unit_is_start_limited(_read_systemd_unit_properties(system=system))


def _print_systemd_start_limit_wait(system: bool = False) -> None:
    svc = get_service_name()
    scope_label = _service_scope_label(system).capitalize()
    scope_flag = " --system" if system else ""
    systemctl_prefix = "systemctl " if system else "systemctl --user "
    journal_prefix = "journalctl " if system else "journalctl --user "
    print(f"⏳ {scope_label} service is temporarily rate-limited by systemd.")
    print("  systemd is refusing another immediate start after repeated exits.")
    print(
        f"  Wait for the start-limit window to expire, then run: {'sudo ' if system else ''}hermes gateway restart{scope_flag}"
    )
    print(f"  Or clear the failed state manually: {systemctl_prefix}reset-failed {svc}")
    print(f"  Check logs: {journal_prefix}-u {svc} -l --since '5 min ago'")


def _recover_pending_systemd_restart(
    system: bool = False, previous_pid: int | None = None
) -> bool:
    """Recover a planned service restart that is stuck in systemd state."""
    props = _read_systemd_unit_properties(system=system)
    if not props:
        return False

    try:
        from gateway.status import read_runtime_status
    except Exception:
        return False

    runtime_state = read_runtime_status() or {}
    if not runtime_state.get("restart_requested"):
        return False

    active_state = props.get("ActiveState", "")
    sub_state = props.get("SubState", "")
    exec_main_status = props.get("ExecMainStatus", "")
    result = props.get("Result", "")

    if active_state == "activating" and sub_state == "auto-restart":
        print("⏳ Service restart already pending — waiting for systemd relaunch...")
        return _wait_for_systemd_service_restart(
            system=system,
            previous_pid=previous_pid,
        )

    if active_state == "failed" and (
        exec_main_status == str(GATEWAY_SERVICE_RESTART_EXIT_CODE)
        or result == "exit-code"
    ):
        svc = get_service_name()
        scope_label = _service_scope_label(system).capitalize()
        print(
            f"↻ Clearing failed state for pending {scope_label.lower()} service restart..."
        )
        _run_systemctl(
            ["reset-failed", svc],
            system=system,
            check=False,
            timeout=30,
        )
        _run_systemctl(
            ["start", svc],
            system=system,
            check=False,
            timeout=90,
        )
        return _wait_for_systemd_service_restart(
            system=system,
            previous_pid=previous_pid,
        )

    return False


def _parse_launchd_pid_from_list_output(output: str) -> int | None:
    """Extract the PID from ``launchctl list <label>`` output.

    When launchd is actively supervising a process, the output includes a
    ``"PID" = <number>;`` line.  When the service definition is only *registered*
    but not running (macOS 26+ with an unmanageable domain, fallback active),
    the output lacks a PID field entirely.  Returns ``None`` when no PID is
    found or the PID is non-positive (e.g. ``-1`` for a recently-crashed service).
    """
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith('"PID"') or stripped.startswith("PID"):
            parts = stripped.split("=", 1)
            if len(parts) == 2:
                val = parts[1].strip().rstrip(";").strip('"')
                try:
                    pid = int(val)
                    return pid if pid > 0 else None
                except ValueError:
                    return None
    return None


def _probe_launchd_service_running() -> bool:
    """Return True when launchd is actively supervising the gateway process.

    ``launchctl list <label>`` returns exit 0 whenever the service definition is
    registered with launchd — even when ``state = not running`` (macOS 26+).
    We additionally require a PID in the output to confirm launchd is actually
    managing a live process, not just holding a static definition.
    """
    if not get_launchd_plist_path().exists():
        return False
    try:
        result = subprocess.run(
            ["launchctl", "list", get_launchd_label()],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return False
    if result.returncode != 0:
        return False
    return _parse_launchd_pid_from_list_output(result.stdout) is not None


def get_gateway_runtime_snapshot(system: bool = False) -> GatewayRuntimeSnapshot:
    """Return a unified view of gateway liveness for the current profile."""
    gateway_pids = tuple(find_gateway_pids())
    if is_termux():
        return GatewayRuntimeSnapshot(
            manager="Termux / manual process",
            gateway_pids=gateway_pids,
        )

    from hermes_constants import is_container

    if is_linux() and is_container():
        # Phase 4: report s6 supervision when running under our /init.
        # Other container runtimes (or containers built before Phase 2)
        # still get the original "docker (foreground)" label.
        try:
            from hermes_cli.service_manager import detect_service_manager, get_service_manager
            if detect_service_manager() == "s6":
                profile = _profile_suffix() or "default"
                service_name = f"gateway-{profile}"
                mgr = get_service_manager()
                service_installed = False
                service_running = False
                try:
                    service_dir = getattr(mgr, "scandir", None)
                    if service_dir is not None:
                        service_installed = (service_dir / service_name).is_dir()
                except Exception:
                    service_installed = False
                if service_installed:
                    try:
                        service_running = bool(mgr.is_running(service_name))
                    except Exception:
                        service_running = False
                return GatewayRuntimeSnapshot(
                    manager="s6 (container supervisor)",
                    service_installed=service_installed,
                    service_running=service_running,
                    gateway_pids=gateway_pids,
                    service_scope="s6",
                )
        except Exception:
            pass  # Fall through to the legacy label on any detection error.
        return GatewayRuntimeSnapshot(
            manager="docker (foreground)",
            gateway_pids=gateway_pids,
        )

    if supports_systemd_services():
        selected_system, service_running = _probe_systemd_service_running(system=system)
        scope_label = _service_scope_label(selected_system)
        return GatewayRuntimeSnapshot(
            manager=f"systemd ({scope_label})",
            service_installed=get_systemd_unit_path(system=selected_system).exists(),
            service_running=service_running,
            gateway_pids=gateway_pids,
            service_scope=scope_label,
        )

    if is_macos():
        return GatewayRuntimeSnapshot(
            manager="launchd",
            service_installed=get_launchd_plist_path().exists(),
            service_running=_probe_launchd_service_running(),
            gateway_pids=gateway_pids,
            service_scope="launchd",
        )

    return GatewayRuntimeSnapshot(
        manager="manual process",
        gateway_pids=gateway_pids,
    )


def _format_gateway_pids(
    pids: tuple[int, ...] | list[int], *, limit: int | None = 3
) -> str:
    rendered = (
        [str(pid) for pid in pids[:limit] if pid > 0]
        if limit is not None
        else [str(pid) for pid in pids if pid > 0]
    )
    if limit is not None and len(pids) > limit:
        rendered.append("...")
    return ", ".join(rendered)


def _print_gateway_process_mismatch(snapshot: GatewayRuntimeSnapshot) -> None:
    if not snapshot.has_process_service_mismatch:
        return
    print()
    # Distinguish the managed detached fallback (macOS launchd exit-5 path)
    # from a genuinely manual foreground/tmux/nohup run.
    if _launchd_unsupported_marker_exists():
        print(
            "⚠ Gateway is running as a detached fallback process — "
            "launchd cannot supervise it"
        )
        print(f"  PID(s): {_format_gateway_pids(snapshot.gateway_pids, limit=None)}")
        print("  Auto-start at login and auto-restart on crash are NOT available.")
        print("  Stop it with: hermes gateway stop")
    else:
        print(
            "⚠ Gateway process is running for this profile, but the service is not active"
        )
        print(f"  PID(s): {_format_gateway_pids(snapshot.gateway_pids, limit=None)}")
        print("  This is usually a manual foreground/tmux/nohup run, so `hermes gateway`")
        print("  can refuse to start another copy until this process stops.")


def _print_other_profiles_gateway_status() -> None:
    """Print a summary of gateway status across all profiles.

    Shown at the bottom of ``hermes gateway status`` output so users with
    multiple profiles can tell at a glance which gateways are running and
    avoid confusing another profile's process with the current one.
    """
    try:
        from hermes_cli.profiles import get_active_profile_name

        current = get_active_profile_name()
        other_processes = [
            p for p in find_profile_gateway_processes() if p.profile != current
        ]
        if not other_processes:
            return

        print()
        print("Other profiles:")
        for proc in other_processes:
            print(f"  ✓ {proc.profile:<16s} — PID {proc.pid}")
    except Exception:
        pass


def _gateway_list() -> None:
    """List all profiles and their gateway running status.

    Provides a single-command overview of every known profile and whether
    its gateway is currently running, so multi-profile users don't have to
    check each profile individually.
    """
    try:
        from hermes_cli.profiles import list_profiles, get_active_profile_name
    except Exception:
        print("Unable to list profiles.")
        return

    profiles = list_profiles()
    if not profiles:
        print("No profiles found.")
        return

    current = get_active_profile_name()

    print("Gateways:")
    for prof in profiles:
        marker = "✓" if prof.gateway_running else "✗"
        label = prof.name
        if prof.name == current:
            label += " (current)"
        parts = [f"  {marker} {label:<24s}"]
        if prof.gateway_running:
            try:
                from gateway.status import get_running_pid

                pid = get_running_pid(prof.path / "gateway.pid", cleanup_stale=False)
                if pid:
                    parts.append(f"PID {pid}")
            except Exception:
                pass
        else:
            parts.append("not running")
        print(" — ".join(parts))


def kill_gateway_processes(
    force: bool = False, exclude_pids: set | None = None, all_profiles: bool = False
) -> int:
    """Kill any running gateway processes. Returns count killed.

    Args:
        force: Use the platform's force-kill mechanism instead of graceful terminate.
        exclude_pids: PIDs to skip (e.g. service-managed PIDs that were just
            restarted and should not be killed).
        all_profiles: When ``True``, kill across all profiles.  Passed
            through to :func:`find_gateway_pids`.
    """
    pids = find_gateway_pids(exclude_pids=exclude_pids, all_profiles=all_profiles)
    killed = 0

    for pid in pids:
        try:
            terminate_pid(pid, force=force)
            killed += 1
        except ProcessLookupError:
            # Process already gone
            pass
        except PermissionError:
            print(f"⚠ Permission denied to kill PID {pid}")

        except OSError as exc:
            print(f"Failed to kill PID {pid}: {exc}")
    return killed


def kill_gateway_processes_strict(
    pids: list[int] | tuple[int, ...] | set[int], *, force: bool = False
) -> int:
    """Terminate an already-attested PID set and fail closed on real errors.

    This integer-only compatibility companion remains for callers that already
    own an attested PID set.  The destructive macOS ``stop --all`` transaction
    uses :func:`terminate_gateway_process_identities_strict` instead, retaining
    birth and command identity through the final signal.
    """
    killed = 0
    failures: list[str] = []
    for pid in dict.fromkeys(pids):
        try:
            terminate_pid(pid, force=force)
            killed += 1
        except ProcessLookupError:
            pass
        except PermissionError as exc:
            failures.append(f"PID {pid}: permission denied ({exc})")
        except OSError as exc:
            failures.append(f"PID {pid}: {exc}")
    if failures:
        raise GatewayProcessTerminationError(failures)
    return killed


def _revalidate_gateway_process_identity(
    identity: GatewayProcessIdentity,
):
    """Return a live process handle only when its attestation still matches."""
    try:
        if identity.runtime_role is GatewayRuntimeRole.RUNTIME:
            current, process = _read_live_gateway_process_identity(identity.pid)
        else:
            current, process = _read_live_gateway_process_identity(
                identity.pid,
                allowed_roles=(identity.runtime_role,),
            )
    except ProcessLookupError:
        return None
    except PermissionError as exc:
        raise GatewayProcessIdentityUnreadableError(
            [f"PID {identity.pid}: permission denied while revalidating identity"]
        ) from exc
    except GatewayProcessEnumerationError as exc:
        raise GatewayProcessTerminationError(
            [f"PID {identity.pid}: {exc}"]
        ) from exc
    if current.identity_key() != identity.identity_key():
        raise GatewayProcessTerminationError(
            [
                f"PID {identity.pid}: birth/argv/profile/home identity changed; "
                "signal skipped"
            ]
        )
    return process


def _signal_gateway_process_identity(
    identity: GatewayProcessIdentity,
    sig: int,
) -> str:
    """Signal one revalidated process handle, returning ``signalled/gone/failed``."""
    try:
        process = _revalidate_gateway_process_identity(identity)
    except GatewayProcessTerminationError:
        return "failed"
    if process is None:
        return "gone"
    try:
        process.send_signal(sig)
    except ProcessLookupError:
        return "gone"
    except (PermissionError, OSError):
        return "failed"
    except Exception as exc:
        name = type(exc).__name__
        if name in {"NoSuchProcess", "ZombieProcess"}:
            return "gone"
        if name == "AccessDenied":
            return "failed"
        raise
    return "signalled"


def _wait_for_exact_gateway_identity_exit(
    identity: GatewayProcessIdentity,
    timeout: float,
) -> bool:
    """Wait until exactly ``identity`` is gone, rejecting PID reuse.

    A plain PID existence probe is insufficient between TERM and KILL: the
    number may already belong to another process, or argv/environment reads may
    have become unavailable.  Both are red states and deliberately stop the
    convergence transaction before another signal.
    """
    from gateway.status import _pid_exists

    deadline = time.monotonic() + max(timeout, 0.0)
    while True:
        if not _pid_exists(identity.pid):
            return True
        try:
            if _revalidate_gateway_process_identity(identity) is None:
                return True
        except GatewayProcessIdentityUnreadableError:
            # macOS teardown race: a draining PID's argv read can fail with
            # sysctl(KERN_PROCARGS2) EINVAL before the PID disappears. The
            # birth identity is the anchor: if it no longer matches, the
            # exact process is gone; if it still matches, keep waiting and
            # stay red only once the deadline expires.
            if _launchd_process_start_time(identity.pid) != identity.start_time:
                return True
            if time.monotonic() >= deadline:
                raise
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))


def terminate_gateway_process_identities_strict(
    identities: list[GatewayProcessIdentity]
    | tuple[GatewayProcessIdentity, ...]
    | set[GatewayProcessIdentity],
    *,
    force: bool = False,
    graceful_timeout: float = 5.0,
    kill_timeout: float = 5.0,
) -> tuple[GatewayProcessIdentity, ...]:
    """Converge termination for only exact process identities.

    Normal termination sends TERM, waits for the exact identity to disappear,
    then revalidates that same PID/birth/argv/profile/home before KILL. A
    vanished process is success. Reused PIDs, changed or unreadable identity,
    and permission failures are red conditions and never receive the next
    signal. The optional ``force`` mode retains the historical API for callers
    that explicitly request an immediate KILL, while still requiring exact
    identity proof and bounded post-signal convergence.
    """
    try:
        import psutil  # type: ignore
    except ImportError as exc:
        raise GatewayProcessTerminationError(
            ["psutil is required for strict gateway process termination"]
        ) from exc
    terminated: list[GatewayProcessIdentity] = []
    failures: list[str] = []
    seen: set[tuple[object, ...]] = set()
    for identity in identities:
        key = identity.identity_key()
        if key in seen:
            continue
        seen.add(key)
        try:
            process = _revalidate_gateway_process_identity(identity)
            if process is None:
                terminated.append(identity)
                continue
            try:
                if force:
                    process.kill()
                else:
                    process.terminate()
            except (ProcessLookupError, psutil.NoSuchProcess, psutil.ZombieProcess):
                terminated.append(identity)
                continue
            except (PermissionError, psutil.AccessDenied) as exc:
                failures.append(f"PID {identity.pid}: permission denied ({exc})")
                continue
            except OSError as exc:
                failures.append(f"PID {identity.pid}: {exc}")
                continue

            if _wait_for_exact_gateway_identity_exit(identity, graceful_timeout):
                terminated.append(identity)
                continue
            if force:
                failures.append(
                    f"PID {identity.pid}: exact gateway identity remained after KILL"
                )
                continue

            # This is the last permitted revalidation before escalation.  A
            # changed or unreadable identity is never allowed to receive KILL.
            try:
                process = _revalidate_gateway_process_identity(identity)
                if process is None:
                    terminated.append(identity)
                    continue
                process.kill()
            except (ProcessLookupError, psutil.NoSuchProcess, psutil.ZombieProcess):
                terminated.append(identity)
                continue
            except (PermissionError, psutil.AccessDenied) as exc:
                failures.append(f"PID {identity.pid}: permission denied ({exc})")
                continue
            except OSError as exc:
                failures.append(f"PID {identity.pid}: {exc}")
                continue

            try:
                if _wait_for_exact_gateway_identity_exit(identity, kill_timeout):
                    terminated.append(identity)
                else:
                    failures.append(
                        f"PID {identity.pid}: exact gateway identity remained after KILL"
                    )
            except GatewayProcessTerminationError as exc:
                failures.extend(exc.failures)
        except GatewayProcessTerminationError as exc:
            failures.extend(exc.failures)
    if failures:
        raise GatewayProcessTerminationError(failures)
    return tuple(terminated)


def _reap_unsupervised_gateway_orphans() -> bool:
    """Kill no-supervisor gateway orphans the pidfile/runtime record can't see.

    On WSL/no-systemd hosts the manual restart fallback runs the gateway
    in-process under a ``gateway restart`` argv (hermes_cli/gateway.py restart
    branch → ``run_gateway()``). If its pidfile or runtime record goes missing
    or stale, ``get_running_pid()`` returns ``None`` even though a live orphan
    still holds the webhook port, so a follow-up restart stacks a duplicate on
    the same port (#51325). This is a no-op on hosts WITH a service supervisor,
    where a ``gateway restart`` argv is a transient management command, not the
    running gateway — gating on ``supports_systemd_services()`` keeps the
    orphan-aware scan from killing live management processes there.

    Returns True if at least one orphan was reaped.
    """
    try:
        if supports_systemd_services():
            return False
    except Exception:
        return False

    from gateway.status import write_planned_stop_marker

    own = {os.getpid()}
    try:
        # find_gateway_process_identities_strict() includes the explicit
        # no-supervisor `gateway restart` manager role, which can itself host
        # the runtime while it drains/relaunches.
        orphans = find_gateway_process_identities_strict(
            exclude_pids=own,
            all_profiles=False,
            include_restart_managers=True,
        )
    except Exception:
        return False
    if not orphans:
        return False

    expected_home = get_hermes_home().resolve()
    orphans = [
        identity for identity in orphans if identity.hermes_home == expected_home
    ]
    if not orphans:
        return False

    reaped = False
    signalled: list[GatewayProcessIdentity] = []
    for identity in orphans:
        try:
            write_planned_stop_marker(identity.pid)
        except Exception:
            pass
        signal_result = _signal_gateway_process_identity(identity, signal.SIGTERM)
        if signal_result == "gone":
            continue
        if signal_result != "signalled":
            print(
                f"⚠ Could not signal orphaned gateway PID {identity.pid}; "
                "identity was not revalidated"
            )
            continue
        signalled.append(identity)
        reaped = True

    # SIGTERM released the port in the field report but the orphan kept
    # running until a follow-up SIGKILL — wait briefly, then force-kill
    # any survivor so the replacement can bind the port cleanly.
    deadline = time.monotonic() + 5.0
    survivors = list(signalled)
    while survivors and time.monotonic() < deadline:
        remaining: list[GatewayProcessIdentity] = []
        for identity in survivors:
            try:
                if _revalidate_gateway_process_identity(identity) is not None:
                    remaining.append(identity)
            except GatewayProcessTerminationError:
                # Changed or unreadable identities are red; never escalate
                # them to a second signal.
                continue
        survivors = remaining
        if survivors:
            time.sleep(0.2)
    if survivors:
        try:
            terminate_gateway_process_identities_strict(survivors, force=True)
        except GatewayProcessTerminationError:
            pass

    return reaped


def stop_profile_gateway() -> bool:
    """Stop only the gateway for the current profile (HERMES_HOME-scoped).

    Uses the PID file written by start_gateway(), so it only kills the
    gateway belonging to this profile — not gateways from other profiles.
    Returns True if a process was stopped, False if none was found.

    On hosts without a service supervisor (e.g. WSL/no-systemd, where the
    manual restart fallback runs the gateway in-process under a ``gateway
    restart`` argv), the pidfile/runtime record can be missing or stale while
    a live orphan still holds the webhook port. In that case fall back to the
    orphan-aware process scan so the replacement reaps the prior instance
    instead of stacking a duplicate on the same port (#51325).
    """
    try:
        from gateway.status import get_running_pid, remove_pid_file
    except ImportError:
        return False

    pid = get_running_pid()
    if pid is None:
        return _reap_unsupervised_gateway_orphans()

    try:
        identity = _capture_current_profile_gateway_identity(
            pid,
            include_restart_managers=True,
        )
    except GatewayProcessTerminationError:
        print(f"⚠ Permission denied to signal PID {pid}")
        return False
    if identity is None:
        remove_pid_file()
        return True

    try:
        from gateway.status import write_planned_stop_marker

        write_planned_stop_marker(pid)
    except Exception:
        pass

    signal_result = _signal_gateway_process_identity(identity, signal.SIGTERM)
    if signal_result == "gone":
        remove_pid_file()
        return True
    if signal_result != "signalled":
        print(f"⚠ Permission denied to signal PID {pid}")
        return False

    # Wait briefly for exactly the attested process to exit. A PID-only poll
    # could follow a recycled PID and must not drive a later mutation.
    import time as _time

    for _ in range(20):
        try:
            if _revalidate_gateway_process_identity(identity) is None:
                break
        except GatewayProcessTerminationError:
            return False
        _time.sleep(0.5)

    if get_running_pid() is None:
        remove_pid_file()
    return True


def is_linux() -> bool:
    return sys.platform.startswith("linux")


from hermes_constants import is_container, is_termux, is_wsl


def _wsl_systemd_operational() -> bool:
    """Check if systemd is actually running as PID 1 on WSL.

    WSL2 with ``systemd=true`` in wsl.conf has working systemd.
    WSL2 without it (or WSL1) does not — systemctl commands fail.
    """
    return _systemd_operational(system=True)


def _systemd_operational(system: bool = False) -> bool:
    """Return True when the requested systemd scope is usable."""
    try:
        result = _run_systemctl(
            ["is-system-running"],
            system=system,
            capture_output=True,
            text=True,
            timeout=5,
        )
        # "running", "degraded", "starting" all mean systemd is PID 1
        status = result.stdout.strip().lower()
        return status in {"running", "degraded", "starting", "initializing"}
    except (RuntimeError, subprocess.TimeoutExpired, OSError):
        return False


def _container_systemd_operational() -> bool:
    """Return True when a container exposes working user or system systemd.

    This is NOT our Hermes Docker image — that one runs s6-overlay as
    PID 1 (since Phase 2 of the s6-overlay supervision plan) and is
    detected via ``service_manager.detect_service_manager() == "s6"``.
    This function handles the "container managed by something else"
    case: systemd-nspawn, certain k8s pods, containers built FROM
    systemd-bearing distros where the user has wired systemd as their
    init. In those environments systemctl behaves identically to the
    host case, so we fall through to the normal systemd code paths.
    """
    if _systemd_operational(system=False):
        return True
    if _systemd_operational(system=True):
        return True
    return False


def supports_systemd_services() -> bool:
    if not is_linux() or is_termux():
        return False
    if shutil.which("systemctl") is None:
        return False
    if is_wsl():
        return _wsl_systemd_operational()
    if is_container():
        return _container_systemd_operational()
    return True


def is_macos() -> bool:
    return sys.platform == "darwin"


def is_windows() -> bool:
    return sys.platform == "win32"


def _windows_gateway_should_absorb_console_controls() -> bool:
    """Return True for detached Windows gateway runs that should ignore Ctrl+C.

    Foreground ``hermes gateway run`` must remain interruptible from
    PowerShell/CMD. Detached service-style launches opt in via
    ``HERMES_GATEWAY_DETACHED=1``; older wrappers without the env marker are
    treated as detached when no interactive stdin is attached.
    """
    if not is_windows():
        return False

    detached = os.getenv("HERMES_GATEWAY_DETACHED", "").strip().lower()
    if detached in {"1", "true", "yes", "on"}:
        return True

    try:
        return not bool(sys.stdin and sys.stdin.isatty())
    except (ValueError, OSError):
        return True


# =============================================================================
# Service Configuration
# =============================================================================

_SERVICE_BASE = "hermes-gateway"
SERVICE_DESCRIPTION = "Hermes Agent Gateway - Messaging Platform Integration"


def _profile_suffix() -> str:
    """Derive a service-name suffix from the current HERMES_HOME.

    Returns ``""`` for the default root, the profile name for
    ``<root>/profiles/<name>``, or a short hash for any other path.
    Works correctly in Docker (HERMES_HOME=/opt/data) and standard deployments.
    """
    import hashlib
    import re
    from hermes_constants import get_default_hermes_root

    home = get_hermes_home().resolve()
    default = get_default_hermes_root().resolve()
    if home == default:
        return ""
    # Detect <root>/profiles/<name> pattern → use the profile name
    profiles_root = (default / "profiles").resolve()
    try:
        rel = home.relative_to(profiles_root)
        parts = rel.parts
        if len(parts) == 1 and re.match(r"^[a-z0-9][a-z0-9_-]{0,63}$", parts[0]):
            return parts[0]
    except ValueError:
        pass
    # Fallback: short hash for arbitrary HERMES_HOME paths
    return hashlib.sha256(str(home).encode()).hexdigest()[:8]


def _profile_arg(hermes_home: str | None = None, default_root: str | Path | None = None) -> str:
    """Return ``--profile <name>`` only when HERMES_HOME is a named profile.

    For ``~/.hermes/profiles/<name>``, returns ``"--profile <name>"``.
    For the default profile or hash-based custom paths, returns the empty string.

    Args:
        hermes_home: Optional explicit HERMES_HOME path. Defaults to the current
            ``get_hermes_home()`` value. Should be passed when generating a
            service definition for a different user (e.g. system service).
        default_root: Optional Hermes root to compare against. Used when
            generating a system service for another user from a sudo/root
            process, where ``Path.home()`` and ``get_default_hermes_root()``
            refer to root but the target profile lives under the service user.
    """
    import re
    from hermes_constants import get_default_hermes_root

    home = Path(hermes_home or str(get_hermes_home())).resolve()
    default = Path(default_root).resolve() if default_root else get_default_hermes_root().resolve()
    if home == default:
        return ""
    profiles_root = (default / "profiles").resolve()
    try:
        rel = home.relative_to(profiles_root)
        parts = rel.parts
        if len(parts) == 1 and re.match(r"^[a-z0-9][a-z0-9_-]{0,63}$", parts[0]):
            return f"--profile {parts[0]}"
    except ValueError:
        pass
    return ""


def _profile_arg_for_target_user(hermes_home: str, target_home_dir: str) -> str:
    """Return the profile arg for a system service running as another user."""
    target_root = Path(target_home_dir) / ".hermes"
    try:
        Path(hermes_home).resolve().relative_to(target_root.resolve())
        return _profile_arg(hermes_home, default_root=target_root)
    except ValueError:
        return _profile_arg(hermes_home)


def get_service_name() -> str:
    """Derive a systemd service name scoped to this HERMES_HOME.

    Default ``~/.hermes`` returns ``hermes-gateway`` (backward compatible).
    Profile ``~/.hermes/profiles/coder`` returns ``hermes-gateway-coder``.
    Any other HERMES_HOME appends a short hash for uniqueness.
    """
    suffix = _profile_suffix()
    if not suffix:
        return _SERVICE_BASE
    return f"{_SERVICE_BASE}-{suffix}"


def get_systemd_unit_path(system: bool = False) -> Path:
    name = get_service_name()
    if system:
        return Path("/etc/systemd/system") / f"{name}.service"
    return Path.home() / ".config" / "systemd" / "user" / f"{name}.service"


class UserSystemdUnavailableError(RuntimeError):
    """Raised when ``systemctl --user`` cannot reach the user D-Bus session.

    Typically hit on fresh RHEL/Debian SSH sessions where linger is disabled
    and no user@.service is running, so ``/run/user/$UID/bus`` never exists.
    Carries a user-facing remediation message in ``args[0]``.
    """


class SystemScopeRequiresRootError(RuntimeError):
    """Raised when a system-scope gateway operation is attempted as non-root.

    System-scope units live in ``/etc/systemd/system/`` and require root for
    install / uninstall / start / stop / restart via ``systemctl``. The
    previous behavior was ``sys.exit(1)`` which blew past the wizard's
    ``except Exception`` guards and dumped the user at a bare shell prompt
    with no guidance. Raising a typed exception lets callers that can
    recover (the setup wizard) print actionable remediation instead, while
    ``gateway_command`` still exits 1 with the same message for the direct
    CLI path.

    ``args[0]`` carries the user-facing message, ``args[1]`` the action name.
    ``str(e)`` returns only the message (not the tuple repr) so format
    strings like ``f"Failed: {e}"`` render cleanly.
    """

    def __str__(self) -> str:
        return self.args[0] if self.args else ""


def _user_dbus_socket_path() -> Path:
    """Return the expected per-user D-Bus socket path (regardless of existence)."""
    xdg = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"  # windows-footgun: ok — POSIX systemd helper, never invoked on Windows
    return Path(xdg) / "bus"


def _user_systemd_private_socket_path() -> Path:
    """Return the per-user systemd private socket path (regardless of existence)."""
    xdg = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"  # windows-footgun: ok — POSIX systemd helper, never invoked on Windows
    return Path(xdg) / "systemd" / "private"


def _user_systemd_socket_ready() -> bool:
    """Return True when user-scope systemd has a reachable control socket.

    Some distros expose only the per-user systemd private socket even when the
    D-Bus session bus socket is absent. ``systemctl --user`` can still work in
    that configuration, so preflight checks must treat either socket as valid.
    """
    return (
        _user_dbus_socket_path().exists()
        or _user_systemd_private_socket_path().exists()
    )


def _ensure_user_systemd_env() -> None:
    """Ensure DBUS_SESSION_BUS_ADDRESS and XDG_RUNTIME_DIR are set for systemctl --user.

    On headless servers (SSH sessions), these env vars may be missing even when
    the user's systemd instance is running (via linger).  Without them,
    ``systemctl --user`` fails with "Failed to connect to bus: No medium found".
    We detect the standard socket path and set the vars so all subsequent
    subprocess calls inherit them.
    """
    uid = os.getuid()  # windows-footgun: ok — POSIX systemd helper, never invoked on Windows
    if "XDG_RUNTIME_DIR" not in os.environ:
        runtime_dir = f"/run/user/{uid}"
        if Path(runtime_dir).exists():
            os.environ["XDG_RUNTIME_DIR"] = runtime_dir

    if "DBUS_SESSION_BUS_ADDRESS" not in os.environ:
        xdg_runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{uid}")
        bus_path = Path(xdg_runtime) / "bus"
        if bus_path.exists():
            os.environ["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus_path}"


def _wait_for_user_dbus_socket(timeout: float = 3.0) -> bool:
    """Poll for the user systemd runtime socket(s), up to ``timeout`` seconds.

    Linger-enabled user@.service can take a second or two to spawn its control
    socket(s) after ``loginctl enable-linger`` runs. Returns True once either
    the user D-Bus socket or the per-user systemd private socket exists.
    """
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _user_systemd_socket_ready():
            _ensure_user_systemd_env()
            return True
        time.sleep(0.2)
    return _user_systemd_socket_ready()


def _preflight_user_systemd(*, auto_enable_linger: bool = True) -> None:
    """Ensure ``systemctl --user`` will reach the user-scope systemd instance.

    No-op when the user D-Bus socket or per-user systemd private socket is
    already there (the common case on desktops and linger-enabled servers). On
    fresh SSH sessions where both are missing:

    * If linger is already enabled, wait briefly for user@.service to spawn
      the socket.
    * If linger is disabled and ``auto_enable_linger`` is True, try
      ``loginctl enable-linger $USER`` (works as non-root when polkit permits
      it, otherwise needs sudo).
    * If the socket is still missing afterwards, raise
      :class:`UserSystemdUnavailableError` with a precise remediation message.

    Callers should treat the exception as a terminal condition for user-scope
    systemd operations and surface the message to the user.
    """
    _ensure_user_systemd_env()
    if _user_systemd_socket_ready():
        return

    import getpass

    username = getpass.getuser()
    linger_enabled, linger_detail = get_systemd_linger_status()

    if linger_enabled is True:
        if _wait_for_user_dbus_socket(timeout=3.0):
            return
        # Linger is on but socket still missing — unusual; fall through to error.
        _raise_user_systemd_unavailable(
            username,
            reason="User systemd control sockets are missing even though linger is enabled.",
            fix_hint=(
                f"  systemctl start user@{os.getuid()}.service\n"  # windows-footgun: ok — POSIX systemd helper, never invoked on Windows
                "  (may require sudo; try again after the command succeeds)"
            ),
        )

    if auto_enable_linger and shutil.which("loginctl"):
        try:
            result = subprocess.run(
                ["loginctl", "enable-linger", username],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except Exception as exc:
            _raise_user_systemd_unavailable(
                username,
                reason=f"loginctl enable-linger failed ({exc}).",
                fix_hint=f"  sudo loginctl enable-linger {username}",
            )
        else:
            if result.returncode == 0:
                if _wait_for_user_dbus_socket(timeout=5.0):
                    print(f"✓ Enabled linger for {username} — user D-Bus now available")
                    return
                # enable-linger succeeded but the socket never appeared.
                _raise_user_systemd_unavailable(
                    username,
                    reason="Linger was enabled, but the user D-Bus socket did not appear.",
                    fix_hint=(
                        "  Log out and log back in, then re-run the command.\n"
                        f"  Or reboot and run: systemctl --user start {get_service_name()}"
                    ),
                )
            detail = (
                result.stderr or result.stdout or f"exit {result.returncode}"
            ).strip()
            _raise_user_systemd_unavailable(
                username,
                reason=f"loginctl enable-linger was denied: {detail}",
                fix_hint=f"  sudo loginctl enable-linger {username}",
            )

    _raise_user_systemd_unavailable(
        username,
        reason=(
            "User D-Bus session is not available "
            f"({linger_detail or 'linger disabled'})."
        ),
        fix_hint=f"  sudo loginctl enable-linger {username}",
    )


def _raise_user_systemd_unavailable(
    username: str, *, reason: str, fix_hint: str
) -> None:
    """Build a user-facing error message and raise UserSystemdUnavailableError."""
    msg = (
        f"{reason}\n"
        "  systemctl --user cannot reach the user D-Bus session in this shell.\n"
        "\n"
        "  To fix:\n"
        f"{fix_hint}\n"
        "\n"
        "  Alternative: run the gateway in the foreground (stays up until\n"
        "  you exit / close the terminal):\n"
        "    hermes gateway run"
    )
    raise UserSystemdUnavailableError(msg)


def _systemctl_cmd(system: bool = False) -> list[str]:
    if not system:
        _ensure_user_systemd_env()
    return ["systemctl"] if system else ["systemctl", "--user"]


def _journalctl_cmd(system: bool = False) -> list[str]:
    return ["journalctl"] if system else ["journalctl", "--user"]


def _run_systemctl(
    args: list[str], *, system: bool = False, **kwargs
) -> subprocess.CompletedProcess:
    """Run a systemctl command, raising RuntimeError if systemctl is missing.

    Defense-in-depth: callers are gated by ``supports_systemd_services()``,
    but this ensures any future caller that bypasses the gate still gets a
    clear error instead of a raw ``FileNotFoundError`` traceback.
    """
    try:
        return subprocess.run(_systemctl_cmd(system) + args, **kwargs)
    except FileNotFoundError:
        raise RuntimeError("systemctl is not available on this system") from None


def _service_scope_label(system: bool = False) -> str:
    return "system" if system else "user"


def get_installed_systemd_scopes() -> list[str]:
    scopes = []
    seen_paths: set[Path] = set()
    for system, label in ((False, "user"), (True, "system")):
        unit_path = get_systemd_unit_path(system=system)
        if unit_path in seen_paths:
            continue
        if unit_path.exists():
            scopes.append(label)
            seen_paths.add(unit_path)
    return scopes


def has_conflicting_systemd_units() -> bool:
    return len(get_installed_systemd_scopes()) > 1


# Legacy service names from older Hermes installs that predate the
# hermes-gateway rename. Kept as an explicit allowlist (NOT a glob) so
# profile units (hermes-gateway-*.service) and unrelated third-party
# "hermes" units are never matched.
_LEGACY_SERVICE_NAMES: tuple[str, ...] = ("hermes.service",)

# ExecStart content markers that identify a unit as running our gateway.
# A legacy unit is only flagged when its file contains one of these.
_LEGACY_UNIT_EXECSTART_MARKERS: tuple[str, ...] = (
    "hermes_cli.main gateway",
    "hermes_cli/main.py gateway",
    "gateway/run.py",
    " hermes gateway ",
    "/hermes gateway ",
)


def _legacy_unit_search_paths() -> list[tuple[bool, Path]]:
    """Return ``[(is_system, base_dir), ...]`` — directories to scan for legacy units.

    Factored out so tests can monkeypatch the search roots without touching
    real filesystem paths.
    """
    return [
        (False, Path.home() / ".config" / "systemd" / "user"),
        (True, Path("/etc/systemd/system")),
    ]


def _find_legacy_hermes_units() -> list[tuple[str, Path, bool]]:
    """Return ``[(unit_name, unit_path, is_system)]`` for legacy Hermes gateway units.

    Detects unit files installed by older Hermes versions that used a
    different service name (e.g. ``hermes.service`` before the rename to
    ``hermes-gateway.service``). When both a legacy unit and the current
    ``hermes-gateway.service`` are active, they fight over the same bot
    token — the PR #5646 signal-recovery change turns this into a 30-second
    SIGTERM flap loop.

    Safety guards:

    * Explicit allowlist of legacy names (no globbing). Profile units such
      as ``hermes-gateway-coder.service`` and unrelated third-party
      ``hermes-*`` services are never matched.
    * ExecStart content check — only flag units that invoke our gateway
      entrypoint. A user-created ``hermes.service`` running an unrelated
      binary is left untouched.
    * Results are returned purely for caller inspection; this function
      never mutates or removes anything.
    """
    results: list[tuple[str, Path, bool]] = []
    for is_system, base in _legacy_unit_search_paths():
        for name in _LEGACY_SERVICE_NAMES:
            unit_path = base / name
            try:
                if not unit_path.exists():
                    continue
                text = unit_path.read_text(encoding="utf-8", errors="ignore")
            except (OSError, PermissionError):
                continue
            if not any(marker in text for marker in _LEGACY_UNIT_EXECSTART_MARKERS):
                # Not our gateway — leave alone
                continue
            results.append((name, unit_path, is_system))
    return results


def has_legacy_hermes_units() -> bool:
    """Return True when any legacy Hermes gateway unit files exist."""
    return bool(_find_legacy_hermes_units())


def print_legacy_unit_warning() -> None:
    """Warn about legacy Hermes gateway unit files if any are installed.

    Idempotent: prints nothing when no legacy units are detected. Safe to
    call from any status/install/setup path.
    """
    legacy = _find_legacy_hermes_units()
    if not legacy:
        return
    print_warning("Legacy Hermes gateway unit(s) detected from an older install:")
    for name, path, is_system in legacy:
        scope = "system" if is_system else "user"
        print_info(f"    {path}  ({scope} scope)")
    print_info("  These run alongside the current hermes-gateway service and")
    print_info("  cause SIGTERM flap loops — both try to use the same bot token.")
    print_info("  Remove them with:")
    print_info("    hermes gateway migrate-legacy")


def remove_legacy_hermes_units(
    interactive: bool = True,
    dry_run: bool = False,
) -> tuple[int, list[Path]]:
    """Stop, disable, and remove legacy Hermes gateway unit files.

    Iterates over whatever ``_find_legacy_hermes_units()`` returns — which is
    an explicit allowlist of legacy names (not a glob). Profile units and
    unrelated third-party services are never touched.

    Args:
        interactive: When True, prompt before removing. When False, remove
            without asking (used when another prompt has already confirmed,
            e.g. from the install flow).
        dry_run: When True, list what would be removed and return.

    Returns:
        ``(removed_count, remaining_paths)`` — remaining includes units we
        couldn't remove (typically system-scope when not running as root).
    """
    legacy = _find_legacy_hermes_units()
    if not legacy:
        print("No legacy Hermes gateway units found.")
        return 0, []

    user_units = [(n, p) for n, p, is_sys in legacy if not is_sys]
    system_units = [(n, p) for n, p, is_sys in legacy if is_sys]

    print()
    print("Legacy Hermes gateway unit(s) found:")
    for name, path, is_system in legacy:
        scope = "system" if is_system else "user"
        print(f"  {path}  ({scope} scope)")
    print()

    if dry_run:
        print("(dry-run — nothing removed)")
        return 0, [p for _, p, _ in legacy]

    if interactive and not prompt_yes_no("Remove these legacy units?", True):
        print("Skipped. Run again with: hermes gateway migrate-legacy")
        return 0, [p for _, p, _ in legacy]

    removed = 0
    remaining: list[Path] = []

    # User-scope removal
    for name, path in user_units:
        try:
            _run_systemctl(["stop", name], system=False, check=False, timeout=90)
            _run_systemctl(["disable", name], system=False, check=False, timeout=30)
            path.unlink(missing_ok=True)
            print(f"  ✓ Removed {path}")
            removed += 1
        except (OSError, RuntimeError) as e:
            print(f"  ⚠ Could not remove {path}: {e}")
            remaining.append(path)

    if user_units:
        try:
            _run_systemctl(["daemon-reload"], system=False, check=False, timeout=30)
        except RuntimeError:
            pass

    # System-scope removal (needs root)
    if system_units:
        if os.geteuid() != 0:  # windows-footgun: ok — Linux systemd removal path, guarded by `if system == "Linux"` / systemd-only branch
            print()
            print_warning("System-scope legacy units require root to remove.")
            print_info("  Re-run with: sudo hermes gateway migrate-legacy")
            for _, path in system_units:
                remaining.append(path)
        else:
            for name, path in system_units:
                try:
                    _run_systemctl(["stop", name], system=True, check=False, timeout=90)
                    _run_systemctl(
                        ["disable", name], system=True, check=False, timeout=30
                    )
                    path.unlink(missing_ok=True)
                    print(f"  ✓ Removed {path}")
                    removed += 1
                except (OSError, RuntimeError) as e:
                    print(f"  ⚠ Could not remove {path}: {e}")
                    remaining.append(path)

            try:
                _run_systemctl(["daemon-reload"], system=True, check=False, timeout=30)
            except RuntimeError:
                pass

    print()
    if remaining:
        print_warning(
            f"{len(remaining)} legacy unit(s) still present — see messages above."
        )
    else:
        print_success(f"Removed {removed} legacy unit(s).")

    return removed, remaining


def print_systemd_scope_conflict_warning() -> None:
    scopes = get_installed_systemd_scopes()
    if len(scopes) < 2:
        return

    rendered_scopes = " + ".join(scopes)
    print_warning(
        f"Both user and system gateway services are installed ({rendered_scopes})."
    )
    print_info("  This is confusing and can make start/stop/status behavior ambiguous.")
    print_info(
        "  Default gateway commands target the user service unless you pass --system."
    )
    print_info("  Keep one of these:")
    print_info("    hermes gateway uninstall")
    print_info("    sudo hermes gateway uninstall --system")


def _require_root_for_system_service(action: str) -> None:
    if os.geteuid() != 0:  # windows-footgun: ok — POSIX systemd helper, never invoked on Windows
        raise SystemScopeRequiresRootError(
            f"System gateway {action} requires root. Re-run with sudo.",
            action,
        )


def _system_service_identity(run_as_user: str | None = None) -> tuple[str, str, str]:
    import getpass
    import grp
    import pwd

    username = (
        run_as_user
        or os.getenv("SUDO_USER")
        or os.getenv("USER")
        or os.getenv("LOGNAME")
        or getpass.getuser()
    ).strip()
    if not username:
        raise ValueError(
            "Could not determine which user the gateway service should run as"
        )
    if username == "root" and not run_as_user:
        raise ValueError(
            "Refusing to install the gateway system service as root; pass --run-as-user root to override (e.g. in LXC containers)"
        )
    if username == "root":
        print_warning("Installing gateway service to run as root.")
        print_info(
            "  This is fine for LXC/container environments but not recommended on bare-metal hosts."
        )

    try:
        user_info = pwd.getpwnam(username)
    except KeyError as e:
        raise ValueError(f"Unknown user: {username}") from e

    group_name = grp.getgrgid(user_info.pw_gid).gr_name
    return username, group_name, user_info.pw_dir


def _read_systemd_user_from_unit(unit_path: Path) -> str | None:
    if not unit_path.exists():
        return None

    for line in unit_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("User="):
            value = line.split("=", 1)[1].strip()
            return value or None
    return None


def _default_system_service_user() -> str | None:
    for candidate in (os.getenv("SUDO_USER"), os.getenv("USER"), os.getenv("LOGNAME")):
        if candidate and candidate.strip() and candidate.strip() != "root":
            return candidate.strip()
    return None


def prompt_linux_gateway_install_scope() -> str | None:
    # A boot-time system service has to be created by root (writing the unit to
    # /etc/systemd/system). We only offer that scope when the session is already
    # root — a non-root user is never handed a "re-run yourself under sudo"
    # recipe, since that just funnels them into a system install they can't
    # actually perform from here. Non-root sessions get the user service.
    is_root = os.geteuid() == 0  # windows-footgun: ok — Linux systemd install wizard, never invoked on Windows
    if not is_root:
        choice = prompt_choice(
            "  Choose how the gateway should run in the background:",
            [
                "User service (no sudo; best for laptops/dev boxes; may need linger after logout)",
                "Skip service install for now",
            ],
            default=0,
        )
        if choice == 0:
            print_info(
                "  Tip: for a boot-time system service, re-run setup as root "
                "(e.g. from a root shell or `sudo -i`)."
            )
        return {0: "user", 1: None}[choice]

    choice = prompt_choice(
        "  Choose how the gateway should run in the background:",
        [
            "User service (no sudo; best for laptops/dev boxes; may need linger after logout)",
            "System service (starts on boot; runs as your chosen user)",
            "Skip service install for now",
        ],
        default=0,
    )
    return {0: "user", 1: "system", 2: None}[choice]


def install_linux_gateway_from_setup(force: bool = False, enable_on_startup: bool = True) -> tuple[str | None, bool]:
    scope = prompt_linux_gateway_install_scope()
    if scope is None:
        return None, False

    if scope == "system":
        run_as_user = _default_system_service_user()
        if os.geteuid() != 0:  # windows-footgun: ok — Linux systemd install wizard, never invoked on Windows
            # Unreachable from the wizard: prompt_linux_gateway_install_scope()
            # only offers "system" to root sessions. Defensive guard for any
            # direct caller — we do NOT print a self-elevation recipe.
            print_warning(
                "  System service install requires root. Re-run setup from a "
                "root shell, or install a user service instead: hermes gateway install"
            )
            return scope, False

        if not run_as_user:
            while True:
                run_as_user = prompt(
                    "  Run the system gateway service as which user?", default=""
                )
                run_as_user = (run_as_user or "").strip()
                if run_as_user:
                    break
                print_error("  Enter a username.")

        systemd_install(force=force, system=True, run_as_user=run_as_user, enable_on_startup=enable_on_startup)
        return scope, True

    systemd_install(force=force, system=False, enable_on_startup=enable_on_startup)
    return scope, True


def get_systemd_linger_status() -> tuple[bool | None, str]:
    """Return systemd linger status for the current user.

    Returns:
        (True, "") when linger is enabled.
        (False, "") when linger is disabled.
        (None, detail) when the status could not be determined.
    """
    if is_termux():
        return None, "not supported in Termux"
    if not is_linux():
        return None, "not supported on this platform"

    if not shutil.which("loginctl"):
        return None, "loginctl not found"

    username = os.getenv("USER") or os.getenv("LOGNAME")
    if not username:
        try:
            import pwd

            username = pwd.getpwuid(os.getuid()).pw_name  # windows-footgun: ok — POSIX loginctl helper, never invoked on Windows
        except Exception:
            return None, "could not determine current user"

    try:
        result = subprocess.run(
            ["loginctl", "show-user", username, "--property=Linger", "--value"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except Exception as e:
        return None, str(e)

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
        return None, detail or "loginctl query failed"

    value = (result.stdout or "").strip().lower()
    if value in {"yes", "true", "1"}:
        return True, ""
    if value in {"no", "false", "0"}:
        return False, ""

    rendered = value or "<empty>"
    return None, f"unexpected loginctl output: {rendered}"


def print_systemd_linger_guidance() -> None:
    """Print the current linger status and the fix when it is disabled."""
    linger_enabled, linger_detail = get_systemd_linger_status()
    if linger_enabled is True:
        print("✓ Systemd linger is enabled (service survives logout)")
    elif linger_enabled is False:
        print("⚠ Systemd linger is disabled (gateway may stop when you log out)")
        print("  Run: sudo loginctl enable-linger $USER")
    else:
        print(f"⚠ Could not verify systemd linger ({linger_detail})")
        print("  If you want the gateway user service to survive logout, run:")
        print("  sudo loginctl enable-linger $USER")


def _launchd_user_home() -> Path:
    """Return the real macOS user home for launchd artifacts.

    Profile-mode Hermes often sets ``HOME`` to a profile-scoped directory, but
    launchd user agents still live under the actual account home.
    """
    import pwd

    return Path(pwd.getpwuid(os.getuid()).pw_dir)  # windows-footgun: ok — POSIX launchd (macOS) helper, never invoked on Windows


def get_launchd_plist_path() -> Path:
    """Return the launchd plist path, scoped per profile.

    Default ``~/.hermes`` → ``ai.hermes.gateway.plist`` (backward compatible).
    Profile ``~/.hermes/profiles/coder`` → ``ai.hermes.gateway-coder.plist``.
    """
    suffix = _profile_suffix()
    name = f"ai.hermes.gateway-{suffix}" if suffix else "ai.hermes.gateway"
    return _launchd_user_home() / "Library" / "LaunchAgents" / f"{name}.plist"


def _detect_venv_dir() -> Path | None:
    """Detect the active virtualenv directory.

    Checks ``sys.prefix`` first (works regardless of the directory name),
    then ``VIRTUAL_ENV`` env var (covers uv-managed environments where
    sys.prefix == sys.base_prefix), then falls back to probing common
    directory names under PROJECT_ROOT.
    Returns ``None`` when no virtualenv can be found.
    """
    # If we're running inside a virtualenv, sys.prefix points to it.
    if sys.prefix != sys.base_prefix:
        venv = Path(sys.prefix)
        if venv.is_dir():
            return venv

    # uv and some other tools set VIRTUAL_ENV without changing sys.prefix.
    # This catches `uv run` where sys.prefix == sys.base_prefix but the
    # environment IS a venv.  (#8620)
    _virtual_env = os.environ.get("VIRTUAL_ENV")
    if _virtual_env:
        venv = Path(_virtual_env)
        if venv.is_dir():
            return venv

    # Fallback: check common virtualenv directory names under the project root.
    for candidate in (".venv", "venv"):
        venv = PROJECT_ROOT / candidate
        if venv.is_dir():
            return venv

    return None


def get_python_path() -> str:
    venv = _detect_venv_dir()
    if venv is not None:
        if is_windows():
            venv_python = venv / "Scripts" / "python.exe"
        else:
            venv_python = venv / "bin" / "python"
        if venv_python.exists():
            return str(venv_python)
    return sys.executable


# =============================================================================
# Systemd (Linux)
# =============================================================================


def _build_user_local_paths(home: Path, path_entries: list[str]) -> list[str]:
    """Return user-local bin dirs that exist and aren't already in *path_entries*."""
    candidates = [
        str(home / ".local" / "bin"),  # uv, uvx, pip-installed CLIs
        str(home / ".cargo" / "bin"),  # Rust/cargo tools
        str(home / "go" / "bin"),  # Go tools
        str(home / ".npm-global" / "bin"),  # npm global packages
    ]
    return [p for p in candidates if p not in path_entries and Path(p).exists()]


def _build_wsl_interop_paths(path_entries: list[str]) -> list[str]:
    """Return WSL Windows interop PATH entries for generated systemd units.

    WSL shells normally inherit Windows PATH entries such as
    ``/mnt/c/WINDOWS/System32``. systemd user services do not, so gateway tools
    that call ``powershell.exe``/``cmd.exe`` work in a terminal but fail in the
    background service unless we persist the relevant entries at install time.
    """
    if not is_wsl():
        return []

    candidates: list[str] = []
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if entry.startswith("/mnt/"):
            candidates.append(entry)

    for executable in ("powershell.exe", "cmd.exe", "explorer.exe", "wsl.exe"):
        resolved = shutil.which(executable)
        if resolved:
            candidates.append(str(Path(resolved).parent))

    for entry in (
        "/mnt/c/WINDOWS/system32",
        "/mnt/c/WINDOWS",
        "/mnt/c/WINDOWS/System32/Wbem",
        "/mnt/c/WINDOWS/System32/WindowsPowerShell/v1.0/",
        "/mnt/c/WINDOWS/System32/OpenSSH/",
    ):
        if Path(entry).exists():
            candidates.append(entry)

    result: list[str] = []
    seen = set(path_entries)
    for entry in candidates:
        if entry and entry not in seen:
            seen.add(entry)
            result.append(entry)
    return result


def _remap_path_for_user(path: str, target_home_dir: str) -> str:
    """Remap *path* from the current user's home to *target_home_dir*.

    If *path* lives under ``Path.home()`` the corresponding prefix is swapped
    to *target_home_dir*; otherwise the path is returned unchanged.

      /root/.hermes/hermes-agent  -> /home/alice/.hermes/hermes-agent
      /opt/hermes                 -> /opt/hermes  (kept as-is)

    Note: this function intentionally does NOT resolve symlinks. A venv's
    ``bin/python`` is typically a symlink to the base interpreter (e.g. a
    uv-managed CPython at ``~/.local/share/uv/python/.../python3.11``);
    resolving that symlink swaps the unit's ``ExecStart`` to a bare Python
    that has none of the venv's site-packages, so the service crashes on
    the first ``import``. Keep the symlinked path so the venv activates
    its own environment. Lexical expansion only via ``expanduser``.
    """
    current_home = Path.home()
    p = Path(path).expanduser()
    try:
        relative = p.relative_to(current_home)
        return str(Path(target_home_dir) / relative)
    except ValueError:
        return str(p)


def _hermes_home_for_target_user(target_home_dir: str) -> str:
    """Remap the current HERMES_HOME to the equivalent under a target user's home.

    When installing a system service via sudo, get_hermes_home() resolves to
    root's home.  This translates it to the target user's equivalent path:
      /root/.hermes                    → /home/alice/.hermes
      /root/.hermes/profiles/coder     → /home/alice/.hermes/profiles/coder
      /opt/custom-hermes               → /opt/custom-hermes  (kept as-is)
    """
    current_hermes = get_hermes_home().resolve()
    current_default = (Path.home() / ".hermes").resolve()
    target_default = Path(target_home_dir) / ".hermes"

    # Default ~/.hermes → remap to target user's default
    if current_hermes == current_default:
        return str(target_default)

    # Profile or subdir of ~/.hermes → preserve the relative structure
    try:
        relative = current_hermes.relative_to(current_default)
        return str(target_default / relative)
    except ValueError:
        # Completely custom path (not under ~/.hermes) — keep as-is
        return str(current_hermes)


def _build_service_path_dirs(project_root: Path | None = None) -> list[str]:
    """Build PATH directory list for service units, excluding non-existent dirs."""
    if project_root is None:
        project_root = PROJECT_ROOT

    def _is_dir(path: Path) -> bool:
        try:
            return path.is_dir()
        except OSError:
            return False

    candidates = []

    venv_bin = project_root / "venv" / "bin"
    if _is_dir(venv_bin):
        candidates.append(str(venv_bin))
    elif sys.prefix != sys.base_prefix:
        candidates.append(str(Path(sys.prefix) / "bin"))

    node_bin = project_root / "node_modules" / ".bin"
    if _is_dir(node_bin):
        candidates.append(str(node_bin))

    hermes_home = get_hermes_home()
    hermes_node = hermes_home / "node" / "bin"
    if _is_dir(hermes_node):
        candidates.append(str(hermes_node))
    hermes_nm = hermes_home / "node_modules" / ".bin"
    if _is_dir(hermes_nm):
        candidates.append(str(hermes_nm))

    return candidates


def _stable_service_working_dir() -> str:
    """Return a WorkingDirectory that will not disappear out from under systemd.

    The gateway does NOT need its cwd to be the source checkout — ``ExecStart``
    uses an absolute python interpreter and ``-m hermes_cli.main``, so module
    resolution does not depend on cwd. Pinning ``WorkingDirectory`` to
    ``PROJECT_ROOT`` (``Path(__file__).parent.parent``) is actively harmful:
    when the unit is generated from a transient checkout — a ``.worktrees/``
    dir, or a clone that ``hermes update`` later relocates/removes — the path
    rots. systemd then fails the start at the CHDIR step (``status=200/CHDIR``,
    "Changing to the requested working directory failed") *before* Python
    loads, so the on-boot ``refresh_systemd_unit_if_needed()`` self-heal never
    runs and ``Restart=always`` crash-loops forever on a dead directory.

    ``HERMES_HOME`` is the stable anchor: it is where config/state/logs live,
    it never moves, and it is guaranteed to exist whenever the gateway is
    meaningfully installed. Fall back to ``PROJECT_ROOT`` only if HERMES_HOME
    cannot be resolved (it always can in practice).
    """
    try:
        home = get_hermes_home()
        if home and Path(home).is_dir():
            return str(Path(home).resolve())
    except Exception:
        pass
    return str(PROJECT_ROOT)


def generate_systemd_unit(system: bool = False, run_as_user: str | None = None) -> str:
    python_path = get_python_path()
    working_dir = _stable_service_working_dir()
    detected_venv = _detect_venv_dir()
    venv_dir = str(detected_venv) if detected_venv else str(PROJECT_ROOT / "venv")

    path_entries = _build_service_path_dirs()
    resolved_node = shutil.which("node")
    if resolved_node:
        # Use the directory where ``node`` is *found on PATH*, NOT the
        # symlink's resolved target. ``~/.local/bin/node`` is often a symlink
        # into a specific profile's node install (e.g. profiles/jarvis/node/
        # bin/node); calling .resolve() here would chase that symlink and bake
        # one profile's node path into *every* profile's service unit. That
        # cross-profile leak makes systemd_unit_is_current() perpetually false,
        # so each gateway rewrites its unit + daemon-reload on every boot. Using
        # the symlink's own parent keeps the generated unit profile-agnostic.
        resolved_node_dir = str(Path(resolved_node).parent)
        if resolved_node_dir not in path_entries:
            path_entries.append(resolved_node_dir)

    common_bin_paths = [
        "/usr/local/sbin",
        "/usr/local/bin",
        "/usr/sbin",
        "/usr/bin",
        "/sbin",
        "/bin",
    ]
    # systemd's TimeoutStopSec must exceed the gateway's drain_timeout so
    # there's budget left for post-interrupt cleanup (tool subprocess kill,
    # adapter disconnect, session DB close) before systemd escalates to
    # SIGKILL on the cgroup — otherwise bash/sleep tool-call children left
    # by a force-interrupted agent get reaped by systemd instead of us
    # (#8202). 30s of headroom covers the worst case we've observed.
    _drain_timeout = int(_get_restart_drain_timeout() or 0)
    restart_timeout = max(60, _drain_timeout) + 30

    if system:
        username, group_name, home_dir = _system_service_identity(run_as_user)
        hermes_home = _hermes_home_for_target_user(home_dir)
        profile_arg = _profile_arg_for_target_user(hermes_home, home_dir)
        # Remap all paths that may resolve under the calling user's home
        # (e.g. /root/) to the target user's home so the service can
        # actually access them.
        python_path = _remap_path_for_user(python_path, home_dir)
        # Anchor cwd to the target user's HERMES_HOME (stable, always exists)
        # rather than a remapped source-checkout path that can rot. See
        # _stable_service_working_dir() for the full rationale.
        working_dir = str(hermes_home) if hermes_home else _remap_path_for_user(working_dir, home_dir)
        venv_dir = _remap_path_for_user(venv_dir, home_dir)
        path_entries = [_remap_path_for_user(p, home_dir) for p in path_entries]
        path_entries.extend(_build_user_local_paths(Path(home_dir), path_entries))
        path_entries.extend(_build_wsl_interop_paths(path_entries))
        path_entries.extend(common_bin_paths)
        sane_path = ":".join(path_entries)
        return f"""[Unit]
Description={SERVICE_DESCRIPTION}
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
User={username}
Group={group_name}
ExecStart={python_path} -m hermes_cli.main{f" {profile_arg}" if profile_arg else ""} gateway run
WorkingDirectory={working_dir}
Environment="HOME={home_dir}"
Environment="USER={username}"
Environment="LOGNAME={username}"
Environment="PATH={sane_path}"
Environment="VIRTUAL_ENV={venv_dir}"
Environment="HERMES_HOME={hermes_home}"
Restart=always
RestartSec=5
RestartForceExitStatus={GATEWAY_SERVICE_RESTART_EXIT_CODE}
KillMode=mixed
KillSignal=SIGTERM
ExecReload=/bin/kill -USR1 $MAINPID
ExecStopPost=-{python_path} -m gateway.cgroup_cleanup
TimeoutStopSec={restart_timeout}
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"""

    hermes_home = str(get_hermes_home().resolve())
    profile_arg = _profile_arg(hermes_home)
    path_entries.extend(_build_user_local_paths(Path.home(), path_entries))
    path_entries.extend(_build_wsl_interop_paths(path_entries))
    path_entries.extend(common_bin_paths)
    sane_path = ":".join(path_entries)
    return f"""[Unit]
Description={SERVICE_DESCRIPTION}
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
ExecStart={python_path} -m hermes_cli.main{f" {profile_arg}" if profile_arg else ""} gateway run
WorkingDirectory={working_dir}
Environment="PATH={sane_path}"
Environment="VIRTUAL_ENV={venv_dir}"
Environment="HERMES_HOME={hermes_home}"
Restart=always
RestartSec=5
RestartForceExitStatus={GATEWAY_SERVICE_RESTART_EXIT_CODE}
KillMode=mixed
KillSignal=SIGTERM
ExecReload=/bin/kill -USR1 $MAINPID
ExecStopPost=-{python_path} -m gateway.cgroup_cleanup
TimeoutStopSec={restart_timeout}
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
"""


def _normalize_service_definition(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.strip().splitlines())


# Directives that older systemd versions silently ignore/strip.  Normalize
# them out of stale-check comparisons so a unit that differs only by these
# directives is not perpetually flagged as outdated.
_SYSTEMD_OPTIONAL_DIRECTIVES = (
    "RestartMaxDelaySec",
    "RestartSteps",
)


def _strip_optional_systemd_directives(text: str) -> str:
    """Remove systemd directives that older hosts silently drop."""
    lines = text.splitlines()
    filtered = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            key = stripped.split("=", 1)[0].strip()
            if key in _SYSTEMD_OPTIONAL_DIRECTIVES:
                continue
        filtered.append(line)
    return "\n".join(filtered)


def _normalize_launchd_plist_for_comparison(text: str) -> str:
    """Normalize launchd plist text for staleness checks.

    The generated plist intentionally captures a broad PATH assembled from the
    invoking shell so user-installed tools remain reachable under launchd.
    That makes raw text comparison unstable across shells, so ignore the PATH
    payload when deciding whether the installed plist is stale.
    """
    import re

    normalized = _normalize_service_definition(text)
    return re.sub(
        r"(<key>PATH</key>\s*<string>)(.*?)(</string>)",
        r"\1__HERMES_PATH__\3",
        normalized,
        flags=re.S,
    )


def systemd_unit_is_current(system: bool = False) -> bool:
    unit_path = get_systemd_unit_path(system=system)
    if not unit_path.exists():
        return False

    installed = unit_path.read_text(encoding="utf-8")
    expected_user = _read_systemd_user_from_unit(unit_path) if system else None
    expected = generate_systemd_unit(system=system, run_as_user=expected_user)
    # Normalize out directives that older systemd versions silently drop
    # (RestartMaxDelaySec, RestartSteps) so a unit that differs only by
    # those directives is not perpetually flagged as outdated.
    norm_installed = _normalize_service_definition(
        _strip_optional_systemd_directives(installed)
    )
    norm_expected = _normalize_service_definition(
        _strip_optional_systemd_directives(expected)
    )
    return norm_installed == norm_expected


def _temp_home_in_service_definition(definition: str) -> str | None:
    """Return the temp-dir HERMES_HOME baked into a service definition, or None.

    A generated systemd unit / launchd plist carries the resolved HERMES_HOME
    in its environment block. If that path lives under the system temp dir,
    the definition was almost certainly generated by a test/E2E harness that
    exported a throwaway ``HERMES_HOME=/tmp/...`` — writing it to the real
    service file silently breaks the user's gateway on the next (re)start:
    the gateway comes back "active (running)" but pointed at an empty temp
    home ("No messaging platforms enabled"), deaf to every platform.
    Seen live 2026-06-11: an E2E guard probe ran ``hermes gateway restart``
    with ``HERMES_HOME=/tmp/hermes-e2e-<pr>`` exported; the restart path's
    unit refresh baked the temp path into the production unit and the
    post-update restart produced a zombie gateway for 7+ hours.

    Matches both systemd ``Environment="HERMES_HOME=..."`` lines and launchd
    ``<key>HERMES_HOME</key><string>...</string>`` pairs.
    """
    import re
    import tempfile

    candidates = re.findall(r'HERMES_HOME=([^"\n]+)', definition)
    candidates += re.findall(
        r"<key>HERMES_HOME</key>\s*<string>(.*?)</string>", definition, flags=re.S
    )
    temp_roots = {
        Path(tempfile.gettempdir()).resolve(),
        Path("/tmp"),
        Path("/var/tmp"),
        Path("/private/tmp"),
        Path("/private/var/tmp"),
    }
    for raw in candidates:
        try:
            resolved = Path(raw.strip().strip('"')).resolve()
        except (OSError, ValueError):
            continue
        for root in temp_roots:
            if resolved == root or root in resolved.parents:
                return raw.strip()
    return None


def _refuse_temp_home_service_write(definition: str, kind: str) -> bool:
    """Refuse (with guidance) when a service definition carries a temp HERMES_HOME."""
    temp_home = _temp_home_in_service_definition(definition)
    if temp_home is None:
        return False
    print(
        f"✗ Refusing to write the gateway {kind}: HERMES_HOME resolves to a "
        f"temporary directory ({temp_home})."
    )
    print(
        "  This usually means a test/E2E environment exported HERMES_HOME. "
        "Unset it (or run from a clean shell) and retry."
    )
    return True


def refresh_systemd_unit_if_needed(system: bool = False) -> bool:
    """Rewrite the installed systemd unit when the generated definition has changed."""
    unit_path = get_systemd_unit_path(system=system)
    if not unit_path.exists() or systemd_unit_is_current(system=system):
        return False

    expected_user = _read_systemd_user_from_unit(unit_path) if system else None
    new_unit = generate_systemd_unit(system=system, run_as_user=expected_user)

    # ── Test-environment safety belt ─────────────────────────────────────
    # The user-scope unit path resolves under ``Path.home()``, which is NOT
    # sandboxed by the test conftest (only HERMES_HOME is). If a test
    # exercises ``run_gateway()`` with a pytest-tmp HERMES_HOME, the freshly
    # generated unit bakes that ``/tmp/pytest-of-.../hermes_test`` path into
    # ``Environment="HERMES_HOME=..."``. Writing that to the developer's
    # real user systemd unit file silently breaks their gateway on the next
    # reboot (systemd loads the polluted env, the gateway looks at an empty
    # tmp dir, and Telegram/Discord/etc. all show as "not configured").
    # Refuse to write when the generated unit references a pytest tmpdir.
    # Detection sniffs the unit body — tests that legitimately exercise the
    # refresh flow patch ``generate_systemd_unit`` to return synthetic
    # content (``"new unit\n"``) which doesn't contain these markers and
    # still works.
    if not system and (
        "/pytest-of-" in new_unit
        or '/hermes_test"' in new_unit
        or "/hermes_test/" in new_unit
    ):
        return False

    # Structural variant of the same belt: refuse to bake ANY temp-dir
    # HERMES_HOME into the unit (manual E2E homes like /tmp/hermes-e2e-NNN
    # don't carry the pytest markers above but poison the unit identically).
    if _refuse_temp_home_service_write(new_unit, "systemd unit"):
        return False

    unit_path.write_text(new_unit, encoding="utf-8")
    _run_systemctl(["daemon-reload"], system=system, check=True, timeout=30)
    print(
        f"↻ Updated gateway {_service_scope_label(system)} service definition to match the current Hermes install"
    )
    return True


def _print_linger_enable_warning(username: str, detail: str | None = None) -> None:
    print()
    print("⚠ Linger not enabled — gateway may stop when you close this terminal.")
    if detail:
        print(f"  Auto-enable failed: {detail}")
    print()
    print("  On headless servers (VPS, cloud instances) run:")
    print(f"    sudo loginctl enable-linger {username}")
    print()
    print("  Then restart the gateway:")
    print(f"    systemctl --user restart {get_service_name()}.service")
    print()


def _ensure_linger_enabled() -> None:
    """Enable linger when possible so the user gateway survives logout."""
    if is_termux() or not is_linux():
        return

    import getpass

    username = getpass.getuser()
    linger_file = Path(f"/var/lib/systemd/linger/{username}")
    if linger_file.exists():
        print("✓ Systemd linger is enabled (service survives logout)")
        return

    linger_enabled, linger_detail = get_systemd_linger_status()
    if linger_enabled is True:
        print("✓ Systemd linger is enabled (service survives logout)")
        return

    if not shutil.which("loginctl"):
        _print_linger_enable_warning(username, linger_detail or "loginctl not found")
        return

    print("Enabling linger so the gateway survives SSH logout...")
    try:
        result = subprocess.run(
            ["loginctl", "enable-linger", username],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except Exception as e:
        _print_linger_enable_warning(username, str(e))
        return

    if result.returncode == 0:
        print("✓ Linger enabled — gateway will persist after logout")
        return

    detail = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
    _print_linger_enable_warning(username, detail or linger_detail)


def _select_systemd_scope(system: bool = False) -> bool:
    if system:
        return True
    return (
        get_systemd_unit_path(system=True).exists()
        and not get_systemd_unit_path(system=False).exists()
    )


def _system_scope_wizard_would_need_root(system: bool = False) -> bool:
    """True when the setup wizard is about to trigger a system-scope operation
    as a non-root user.

    Replicates the decision ``_select_systemd_scope`` makes inside
    ``systemd_start`` / ``systemd_restart`` / ``systemd_stop`` so the wizard
    can detect the dead-end BEFORE prompting, rather than letting
    ``SystemScopeRequiresRootError`` propagate out and leave the user
    staring at a bare shell.
    """
    if os.geteuid() == 0:  # windows-footgun: ok — systemd scope wizard decision, never invoked on Windows
        return False
    return _select_systemd_scope(system=system)


def _print_system_scope_remediation(action: str) -> None:
    """Print actionable remediation when the wizard skips a system-scope
    prompt because the user isn't root. Keeps the wizard flowing instead of
    aborting.
    """
    svc = get_service_name()
    print_warning(
        f"Gateway is installed as a system-wide service — " f"{action} requires root."
    )
    print_info("  Options:")
    print_info(f"    1. {action.capitalize()} it this time:")
    if action == "start":
        print_info(f"         sudo systemctl start {svc}")
    elif action == "stop":
        print_info(f"         sudo systemctl stop {svc}")
    elif action == "restart":
        print_info(f"         sudo systemctl restart {svc}")
    else:
        print_info(f"         sudo systemctl {action} {svc}")
    print_info("    2. Switch to a per-user service (recommended for personal use):")
    print_info("         sudo hermes gateway uninstall --system")
    print_info("         hermes gateway install")
    print_info("         hermes gateway start")


def _get_restart_drain_timeout() -> float:
    """Return the configured gateway restart drain timeout in seconds."""
    raw = os.getenv("HERMES_RESTART_DRAIN_TIMEOUT", "").strip()
    if not raw:
        cfg = read_raw_config()
        agent_cfg = cfg.get("agent", {}) if isinstance(cfg, dict) else {}
        raw = str(
            agent_cfg.get(
                "restart_drain_timeout", DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT
            )
        )
    return parse_restart_drain_timeout(raw)


def systemd_install(
    force: bool = False,
    system: bool = False,
    run_as_user: str | None = None,
    enable_on_startup: bool = True,
    non_interactive: bool = False,
):
    if system:
        _require_root_for_system_service("install")

    # Offer to remove legacy units (hermes.service from pre-rename installs)
    # before installing the new hermes-gateway.service. If both remain, they
    # flap-fight for the Telegram bot token on every gateway startup.
    # Only removes units matching _LEGACY_SERVICE_NAMES + our ExecStart
    # signature — profile units are never touched.
    if has_legacy_hermes_units():
        print()
        print_legacy_unit_warning()
        print()
        if non_interactive or prompt_yes_no("Remove the legacy unit(s) before installing?", True):
            remove_legacy_hermes_units(interactive=False)
            print()

    unit_path = get_systemd_unit_path(system=system)
    scope_flag = " --system" if system else ""

    if unit_path.exists() and not force:
        if not systemd_unit_is_current(system=system):
            print(
                f"↻ Repairing outdated {_service_scope_label(system)} systemd service at: {unit_path}"
            )
            refresh_systemd_unit_if_needed(system=system)
            if enable_on_startup:
                _run_systemctl(["enable", get_service_name()], system=system, check=True, timeout=30)
            print(f"✓ {_service_scope_label(system).capitalize()} service definition updated")
            return
        print(f"Service already installed at: {unit_path}")
        print("Use --force to reinstall")
        return

    unit_path.parent.mkdir(parents=True, exist_ok=True)
    new_unit = generate_systemd_unit(system=system, run_as_user=run_as_user)
    if _refuse_temp_home_service_write(new_unit, "systemd unit"):
        return
    print(f"Installing {_service_scope_label(system)} systemd service to: {unit_path}")
    unit_path.write_text(new_unit, encoding="utf-8")

    _run_systemctl(["daemon-reload"], system=system, check=True, timeout=30)
    if enable_on_startup:
        _run_systemctl(["enable", get_service_name()], system=system, check=True, timeout=30)

    print()
    enable_label = "installed and enabled" if enable_on_startup else "installed"
    print(f"✓ {_service_scope_label(system).capitalize()} service {enable_label}!")
    print()
    print("Next steps:")
    print(
        f"  {'sudo ' if system else ''}hermes gateway start{scope_flag}              # Start the service"
    )
    print(
        f"  {'sudo ' if system else ''}hermes gateway status{scope_flag}             # Check status"
    )
    print(
        f"  {'journalctl' if system else 'journalctl --user'} -u {get_service_name()} -f  # View logs"
    )
    print()

    if system:
        configured_user = _read_systemd_user_from_unit(unit_path)
        if configured_user:
            print(f"Configured to run as: {configured_user}")
    else:
        _ensure_linger_enabled()

    print_systemd_scope_conflict_warning()
    print_legacy_unit_warning()


def systemd_uninstall(system: bool = False):
    system = _select_systemd_scope(system)
    if system:
        _require_root_for_system_service("uninstall")

    _run_systemctl(["stop", get_service_name()], system=system, check=False, timeout=90)
    _run_systemctl(
        ["disable", get_service_name()], system=system, check=False, timeout=30
    )

    unit_path = get_systemd_unit_path(system=system)
    if unit_path.exists():
        unit_path.unlink()
        print(f"✓ Removed {unit_path}")

    _run_systemctl(["daemon-reload"], system=system, check=True, timeout=30)
    print(f"✓ {_service_scope_label(system).capitalize()} service uninstalled")


def _require_service_installed(action: str, system: bool = False) -> None:
    unit_path = get_systemd_unit_path(system=system)
    if not unit_path.exists():
        scope_flag = " --system" if system else ""
        print(f"✗ Gateway service is not installed")
        print(f"  Run: {'sudo ' if system else ''}hermes gateway install{scope_flag}")
        sys.exit(1)


def systemd_start(system: bool = False):
    system = _select_systemd_scope(system)
    if system:
        _require_root_for_system_service("start")
    else:
        # Fail fast with actionable guidance if the user D-Bus session is not
        # reachable (common on fresh RHEL/Debian SSH sessions without linger).
        # Raises UserSystemdUnavailableError with a remediation message.
        _preflight_user_systemd()
    _require_service_installed("start", system=system)
    refresh_systemd_unit_if_needed(system=system)
    _run_systemctl(["start", get_service_name()], system=system, check=True, timeout=30)
    print(f"✓ {_service_scope_label(system).capitalize()} service started")


def systemd_stop(system: bool = False):
    system = _select_systemd_scope(system)
    if system:
        _require_root_for_system_service("stop")
    _require_service_installed("stop", system=system)
    _sync_hermes_home_from_systemd_unit(system=system)
    try:
        from gateway.status import get_running_pid, write_planned_stop_marker

        pid = get_running_pid(cleanup_stale=False)
        if pid is not None:
            write_planned_stop_marker(pid)
    except Exception:
        pass
    try:
        _run_systemctl(
            ["stop", get_service_name()], system=system, check=True, timeout=90
        )
    except subprocess.TimeoutExpired:
        label = _service_scope_label(system)
        print(
            f"Gateway {label} service is still stopping after 90s; "
            "check `hermes gateway status` or logs for final shutdown state."
        )
        return
    print(f"✓ {_service_scope_label(system).capitalize()} service stopped")


def systemd_restart(system: bool = False):
    system = _select_systemd_scope(system)
    if system:
        _require_root_for_system_service("restart")
    else:
        _preflight_user_systemd()
    _require_service_installed("restart", system=system)
    refresh_systemd_unit_if_needed(system=system)
    _sync_hermes_home_from_systemd_unit(system=system)
    # MainPID is the service manager's exact unit occupant.  A profile PID or
    # runtime record is only a discovery hint and may have been recycled onto
    # another profile, so it must never override systemd's target identity.
    pid = _systemd_main_pid(system=system)
    if pid is not None:
        scope_label = _service_scope_label(system).capitalize()
        svc = get_service_name()
        drain_timeout = _get_restart_drain_timeout()
        identity_capture_failed = False
        try:
            expected_identity = _capture_current_profile_gateway_identity(
                pid,
                include_restart_managers=False,
            )
        except GatewayProcessTerminationError:
            expected_identity = None
            identity_capture_failed = True

        print(f"⏳ {scope_label} service restarting gracefully (PID {pid})...")
        if identity_capture_failed:
            graceful_restart_ok = False
        elif expected_identity is None:
            # The discovered process vanished before identity capture. Do not
            # recapture this PID: a successor must never receive this restart.
            graceful_restart_ok = True
        else:
            graceful_restart_ok = _graceful_restart_via_sigusr1(
                pid,
                drain_timeout + 5,
                expected_identity=expected_identity,
            )
        if graceful_restart_ok:
            # The gateway exits with code 75 for a planned service restart.
            # RestartSec can otherwise delay the relaunch even though the
            # operator asked for an immediate restart, so kick the unit once
            # the old PID has exited and then wait for the replacement PID.
            _run_systemctl(
                ["reset-failed", svc],
                system=system,
                check=False,
                timeout=30,
            )
            _run_systemctl(
                ["restart", svc],
                system=system,
                check=False,
                timeout=90,
            )
            if _wait_for_systemd_service_restart(system=system, previous_pid=pid):
                return
            if _systemd_service_is_start_limited(system=system):
                return

        print(
            f"⚠ Graceful restart did not complete within {int(drain_timeout + 5)}s; "
            "forcing a service restart..."
        )
        _run_systemctl(
            ["reset-failed", svc],
            system=system,
            check=False,
            timeout=30,
        )
        try:
            _run_systemctl(["restart", svc], system=system, check=True, timeout=90)
        except subprocess.CalledProcessError as exc:
            if _systemd_error_indicates_start_limit(
                exc
            ) or _systemd_service_is_start_limited(system=system):
                _print_systemd_start_limit_wait(system=system)
                return
            raise
        except subprocess.TimeoutExpired:
            label = _service_scope_label(system)
            print(
                f"Gateway {label} service is still restarting after 90s; "
                "check `hermes gateway status` or logs for final state."
            )
            return
        _wait_for_systemd_service_restart(system=system, previous_pid=pid)
        return

    if _recover_pending_systemd_restart(system=system, previous_pid=pid):
        return

    _run_systemctl(
        ["reset-failed", get_service_name()],
        system=system,
        check=False,
        timeout=30,
    )
    try:
        _run_systemctl(
            ["restart", get_service_name()], system=system, check=True, timeout=90
        )
    except subprocess.CalledProcessError as exc:
        if _systemd_error_indicates_start_limit(
            exc
        ) or _systemd_service_is_start_limited(system=system):
            _print_systemd_start_limit_wait(system=system)
            return
        raise
    except subprocess.TimeoutExpired:
        label = _service_scope_label(system)
        print(
            f"Gateway {label} service is still restarting after 90s; "
            "check `hermes gateway status` or logs for final state."
        )
        return
    _wait_for_systemd_service_restart(system=system, previous_pid=pid)


def systemd_status(deep: bool = False, system: bool = False, full: bool = False):
    system = _select_systemd_scope(system)
    unit_path = get_systemd_unit_path(system=system)
    scope_flag = " --system" if system else ""

    if not unit_path.exists():
        print("✗ Gateway service is not installed")
        print(f"  Run: {'sudo ' if system else ''}hermes gateway install{scope_flag}")
        return

    _sync_hermes_home_from_systemd_unit(system=system)

    if has_conflicting_systemd_units():
        print_systemd_scope_conflict_warning()
        print()

    if has_legacy_hermes_units():
        print_legacy_unit_warning()
        print()

    if not systemd_unit_is_current(system=system):
        print("⚠ Installed gateway service definition is outdated")
        print(
            f"  Run: {'sudo ' if system else ''}hermes gateway restart{scope_flag}  # auto-refreshes the unit"
        )
        print()

    status_cmd = ["status", get_service_name(), "--no-pager"]
    if full:
        status_cmd.append("-l")

    _run_systemctl(
        status_cmd,
        system=system,
        capture_output=False,
        timeout=10,
    )

    result = _run_systemctl(
        ["is-active", get_service_name()],
        system=system,
        capture_output=True,
        text=True,
        timeout=10,
    )

    status = result.stdout.strip()

    if status == "active":
        print(
            f"✓ {_service_scope_label(system).capitalize()} gateway service is running"
        )
    else:
        print(
            f"✗ {_service_scope_label(system).capitalize()} gateway service is stopped"
        )
        print(f"  Run: {'sudo ' if system else ''}hermes gateway start{scope_flag}")

    configured_user = _read_systemd_user_from_unit(unit_path) if system else None
    if configured_user:
        print(f"Configured to run as: {configured_user}")

    runtime_lines = _runtime_health_lines()
    if runtime_lines:
        print()
        print("Recent gateway health:")
        for line in runtime_lines:
            print(f"  {line}")

    unit_props = _read_systemd_unit_properties(system=system)
    active_state = unit_props.get("ActiveState", "")
    sub_state = unit_props.get("SubState", "")
    exec_main_status = unit_props.get("ExecMainStatus", "")
    result_code = unit_props.get("Result", "")
    if active_state == "activating" and sub_state == "auto-restart":
        print("  ⏳ Restart pending: systemd is waiting to relaunch the gateway")
    elif _systemd_unit_is_start_limited(unit_props):
        print("  ⏳ Restart pending: systemd is temporarily rate-limiting starts")
        print(
            f"  Run after the start-limit window expires: {'sudo ' if system else ''}hermes gateway restart{scope_flag}"
        )
        print(
            f"  Or clear it manually: systemctl {'--user ' if not system else ''}reset-failed {get_service_name()}"
        )
    elif active_state == "failed" and exec_main_status == str(
        GATEWAY_SERVICE_RESTART_EXIT_CODE
    ):
        print("  ⚠ Planned restart is stuck in systemd failed state (exit 75)")
        print(
            f"  Run: systemctl {'--user ' if not system else ''}reset-failed {get_service_name()} && {'sudo ' if system else ''}hermes gateway start{scope_flag}"
        )
    elif active_state == "failed" and result_code:
        print(f"  ⚠ Systemd unit result: {result_code}")

    if system:
        print("✓ System service starts at boot without requiring systemd linger")
    elif deep:
        print_systemd_linger_guidance()
    else:
        linger_enabled, _ = get_systemd_linger_status()
        if linger_enabled is True:
            print("✓ Systemd linger is enabled (service survives logout)")
        elif linger_enabled is False:
            print("⚠ Systemd linger is disabled (gateway may stop when you log out)")
            print("  Run: sudo loginctl enable-linger $USER")

    if deep:
        print()
        print("Recent logs:")
        log_cmd = _journalctl_cmd(system) + [
            "-u",
            get_service_name(),
            "-n",
            "20",
            "--no-pager",
        ]
        if full:
            log_cmd.append("-l")
        subprocess.run(log_cmd, timeout=10)


# =============================================================================
# Launchd (macOS)
# =============================================================================


def get_launchd_label() -> str:
    """Return the launchd service label, scoped per profile."""
    suffix = _profile_suffix()
    return f"ai.hermes.gateway-{suffix}" if suffix else "ai.hermes.gateway"


# Cached launchd domain result — probing is cheap but should only run once per
# process invocation (each ``hermes gateway start/stop/status`` call).
_resolved_launchd_domain: str | None = None


def _launchd_domain() -> str:
    """Return the launchd domain that actually manages the gateway service.

    Probes ``gui/<uid>`` first (Aqua sessions), then ``user/<uid>``
    (Background/SSH sessions).  When neither domain contains a loaded
    service, falls back to ``launchctl managername`` as a heuristic.

    The result is cached for the lifetime of the process so that repeated
    calls (``start``, ``stop``, ``restart``) use a consistent domain.

    See #40831, #23387.
    """
    global _resolved_launchd_domain
    if _resolved_launchd_domain is not None:
        return _resolved_launchd_domain

    uid = os.getuid()  # windows-footgun: ok — POSIX launchd (macOS) helper, never invoked on Windows
    label = get_launchd_label()
    gui_domain = f"gui/{uid}"
    user_domain = f"user/{uid}"

    # 1. Probe gui/<uid> first — in Aqua sessions the service is loaded here.
    try:
        subprocess.run(
            ["launchctl", "print", f"{gui_domain}/{label}"],
            check=True,
            timeout=5,
            capture_output=True,
        )
        _resolved_launchd_domain = gui_domain
        return gui_domain
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # 2. Probe user/<uid> — in Background/SSH sessions this is the working domain.
    try:
        subprocess.run(
            ["launchctl", "print", f"{user_domain}/{label}"],
            check=True,
            timeout=5,
            capture_output=True,
        )
        _resolved_launchd_domain = user_domain
        return user_domain
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # 3. Neither domain has the service loaded — use managername as heuristic.
    #    Aqua → gui/<uid>, anything else (Background, loginwindow) → user/<uid>.
    try:
        result = subprocess.run(
            ["launchctl", "managername"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if "Aqua" in (result.stdout or ""):
            _resolved_launchd_domain = gui_domain
            return gui_domain
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # 4. Default to user/<uid> (matches the pre-probing behavior for
    #    Background/SSH sessions and is the recommended domain on macOS 26+).
    _resolved_launchd_domain = user_domain
    return user_domain


def _launchd_enable(domain: str, label: str) -> None:
    """Clear launchd's persistent disabled bit for one exact Hermes label."""
    subprocess.run(
        ["launchctl", "enable", f"{domain}/{label}"],
        check=True,
        timeout=30,
    )


class LaunchdFenceError(RuntimeError):
    """The exact launchd label could not be put into stopped desired state."""


class LaunchdInventoryError(RuntimeError):
    """The installed LaunchAgents inventory could not be trusted."""


@dataclass(frozen=True)
class LaunchdLabelProbe:
    """Observed launchd state for one exact label in both user domains."""

    registered: tuple[str, ...]
    disabled: tuple[str, ...]
    absent: tuple[str, ...]
    unknown: tuple[str, ...]
    unknown_details: tuple[str, ...] = ()
    disabled_unknown: tuple[str, ...] = ()
    disabled_unknown_details: tuple[str, ...] = ()
    pids: tuple[tuple[str, int | None], ...] = ()

    def pid_for(self, domain: str) -> int | None:
        for candidate, pid in self.pids:
            if candidate == domain:
                return pid
        return None


@dataclass(frozen=True)
class LaunchdFencedGateway:
    """One validated gateway plist and the domains changed by a stop fence."""

    label: str
    plist_path: Path
    fenced_domains: tuple[str, ...]
    restore_domains: tuple[str, ...]
    changed_domains: tuple[str, ...] = ()
    plist_fingerprint: str = ""
    plist_stat_identity: tuple[int, ...] = ()
    hermes_home: Path | None = None
    preflight_snapshot: "_LaunchdAllStateSnapshot | None" = None
    post_mutation_snapshot: "_LaunchdAllStateSnapshot | None" = None
    plist_argv: tuple[str, ...] = ()
    plist_profile: str | None = None


@dataclass(frozen=True)
class LaunchdStopAllResult:
    """Result of the launchd phase of a cross-profile stop/restart."""

    fenced: tuple[LaunchdFencedGateway, ...]
    failures: tuple[str, ...]
    sweep_safe: bool
    stopped_count: int | None = None

    @property
    def stopped(self) -> int:
        if self.stopped_count is not None:
            return self.stopped_count
        # Historical/manual result construction may omit the explicit count.
        # Count only registrations that were actually loaded at preflight;
        # installed but unloaded plists are not stopped processes.
        return sum(
            1
            for target in self.fenced
            if target.preflight_snapshot is not None
            and bool(target.preflight_snapshot.registered)
        )

    def __iter__(self):
        """Preserve the historical ``stopped, failures`` unpacking API."""
        yield self.stopped
        yield list(self.failures)


def _launchd_disable(domain: str, label: str) -> bool:
    """Persist a stopped desired state for one exact Hermes label.

    A failed disable must never be treated as a fence, including launchctl
    exit 5/125. Callers must prove the exact label is unsupervised or abort
    before bootout/global process termination.
    """
    subprocess.run(
        ["launchctl", "disable", f"{domain}/{label}"],
        check=True,
        timeout=30,
    )
    return True


def _launchd_label_is_disabled(domain: str, label: str) -> bool | None:
    """Return whether launchd reports one exact label as disabled.

    ``launchctl print-disabled`` is not available on every supported macOS
    release/domain, so an inability to inspect it is represented as ``None``
    rather than as a false claim about the desired state.
    """
    state, _detail = _launchd_disabled_probe(domain, label)
    return state


class LaunchdAllOperationError(RuntimeError):
    """One or more intended all-profile launchd targets were not verified."""

    def __init__(
        self,
        message: str,
        failures: tuple[str, ...] = (),
        outcomes: tuple["LaunchdAllTargetOutcome", ...] = (),
    ):
        super().__init__(message)
        self.failures = failures
        self.outcomes = outcomes


@dataclass(frozen=True)
class LaunchdAllTargetOutcome:
    """The verified result for one all-profile target."""

    label: str
    domain: str
    status: str
    reason: str = ""


@dataclass(frozen=True)
class LaunchdAllResult:
    """Structured result for a cross-profile launchd lifecycle operation."""

    operation: str
    outcomes: tuple[LaunchdAllTargetOutcome, ...]


@dataclass(frozen=True)
class LaunchdAllTarget:
    """The immutable identity and state snapshot for one installed target."""

    label: str
    domain: str
    plist_path: Path
    plist_fingerprint: str
    hermes_home: Path
    was_loaded: bool
    pid: int | None
    start_time: int | None
    probe: LaunchdLabelProbe = field(
        default_factory=lambda: LaunchdLabelProbe((), (), (), ())
    )
    plist_stat_identity: tuple[int, ...] = ()
    plist_argv: tuple[str, ...] = ()
    plist_profile: str | None = None
    runtime_identity: GatewayProcessIdentity | None = None

    @property
    def was_disabled(self) -> bool:
        return bool(self.probe.disabled)

    @property
    def launchctl_target(self) -> str:
        return f"{self.domain}/{self.label}"


@dataclass(frozen=True)
class _LaunchdAllStateSnapshot:
    """Observable launchd state retained across one transaction mutation."""

    registered: tuple[str, ...]
    disabled: tuple[str, ...]
    pid: int | None
    start_time: int | None
    runtime_identity: GatewayProcessIdentity | None = None


@dataclass(frozen=True)
class _LaunchdAllPlistIdentity:
    label: str
    path: Path
    fingerprint: str
    hermes_home: Path
    stat_identity: tuple[int, ...]
    argv: tuple[str, ...]
    profile: str | None


@dataclass(frozen=True)
class _SecureLaunchdPlist:
    path: Path
    document: dict
    raw: bytes
    fingerprint: str
    stat_identity: tuple[int, ...]


def _launchd_all_snapshot_from_probe(
    probe: LaunchdLabelProbe,
    *,
    runtime_identity: GatewayProcessIdentity | None = None,
) -> _LaunchdAllStateSnapshot:
    """Turn one exact probe into the state used by compensation guards."""
    registration = probe.registered
    domain = registration[0] if len(registration) == 1 else None
    pid = probe.pid_for(domain) if domain is not None else None
    start_time = _launchd_process_start_time(pid) if pid is not None else None
    return _LaunchdAllStateSnapshot(
        registered=registration,
        disabled=tuple(probe.disabled),
        pid=pid,
        start_time=start_time,
        runtime_identity=runtime_identity,
    )


def _launchd_all_initial_snapshot(
    target: LaunchdAllTarget,
) -> _LaunchdAllStateSnapshot:
    """Return the immutable pre-mutation state for legacy helper callers."""
    return _LaunchdAllStateSnapshot(
        registered=target.probe.registered,
        disabled=tuple(target.probe.disabled),
        pid=target.pid,
        start_time=target.start_time,
        runtime_identity=target.runtime_identity,
    )


def _launchd_all_snapshot_matches(
    expected: _LaunchdAllStateSnapshot,
    actual: _LaunchdAllStateSnapshot,
) -> bool:
    return (
        actual.registered == expected.registered
        and set(actual.disabled) == set(expected.disabled)
        and actual.pid == expected.pid
        and actual.start_time == expected.start_time
        and (
            actual.runtime_identity is None
            and expected.runtime_identity is None
            or actual.runtime_identity is not None
            and expected.runtime_identity is not None
            and actual.runtime_identity.identity_key()
            == expected.runtime_identity.identity_key()
        )
    )


def _launchd_all_capture_snapshot(
    target: LaunchdAllTarget,
    *,
    expected_disabled: set[str] | None = None,
    expected_snapshot: _LaunchdAllStateSnapshot | None = None,
) -> _LaunchdAllStateSnapshot:
    """Probe exact state for a transaction snapshot without mutating it."""
    probe = _probe_launchd_label_domains(target.label)
    _launchd_all_require_known_probe(target.label, probe)
    if expected_disabled is not None and set(probe.disabled) != expected_disabled:
        raise LaunchdAllOperationError(
            f"{target.launchctl_target}: concurrent disabled state changed to "
            f"{list(probe.disabled)}"
        )
    runtime_identity = None
    if target is not None:
        pid = probe.pid_for(probe.registered[0]) if len(probe.registered) == 1 else None
        if pid is not None:
            runtime_identity = _attest_launchd_runtime_identity(
                pid,
                _launchd_process_start_time(pid),
                plist_argv=target.plist_argv,
                hermes_home=target.hermes_home,
                profile=target.plist_profile,
            )
    snapshot = _launchd_all_snapshot_from_probe(
        probe, runtime_identity=runtime_identity
    )
    if snapshot.pid is not None and snapshot.start_time is None:
        raise LaunchdAllOperationError(
            f"{target.launchctl_target}: PID {snapshot.pid} has no birth identity"
        )
    if expected_snapshot is not None and not _launchd_all_snapshot_matches(
        expected_snapshot, snapshot
    ):
        raise LaunchdAllOperationError(
            f"{target.launchctl_target}: runtime state diverged from the "
            "immutable transaction snapshot"
        )
    return snapshot


def _launchd_all_fence_snapshot(
    target: LaunchdAllTarget, expected_disabled: set[str]
) -> _LaunchdAllStateSnapshot:
    """Capture a fence post-state allowing only its intended bit removal."""
    expected = _LaunchdAllStateSnapshot(
        registered=target.probe.registered,
        disabled=tuple(sorted(expected_disabled)),
        pid=target.pid,
        start_time=target.start_time,
        runtime_identity=target.runtime_identity,
    )
    return _launchd_all_capture_snapshot(
        target,
        expected_disabled=expected_disabled,
        expected_snapshot=expected,
    )


def _launchd_all_bootstrap_snapshot(
    target: LaunchdAllTarget, expected_disabled: set[str]
) -> _LaunchdAllStateSnapshot:
    """Capture a bootstrap state only after exact registration attestation."""
    snapshot = _launchd_all_capture_snapshot(
        target, expected_disabled=expected_disabled
    )
    if snapshot.registered != (target.domain,):
        raise LaunchdAllOperationError(
            f"{target.launchctl_target}: bootstrap registration is "
            f"{snapshot.registered or 'absent'}"
        )
    return snapshot


def _launchd_all_capture_label_snapshot(
    label: str,
    target: LaunchdFencedGateway | None = None,
) -> _LaunchdAllStateSnapshot:
    """Capture one label snapshot for stop-fence compensation."""
    probe = _probe_launchd_label_domains(label)
    _launchd_all_require_known_probe(label, probe)
    runtime_identity = None
    if target is not None and len(probe.registered) == 1:
        pid = probe.pid_for(probe.registered[0])
        if pid is not None:
            if not target.plist_argv or target.hermes_home is None:
                raise LaunchdAllOperationError(
                    f"{label}: compensation runtime identity was not retained"
                )
            runtime_identity = _attest_launchd_runtime_identity(
                pid,
                _launchd_process_start_time(pid),
                plist_argv=target.plist_argv,
                hermes_home=target.hermes_home,
                profile=target.plist_profile,
            )
    snapshot = _launchd_all_snapshot_from_probe(
        probe, runtime_identity=runtime_identity
    )
    if snapshot.pid is not None and snapshot.start_time is None:
        raise LaunchdAllOperationError(
            f"{label}: PID {snapshot.pid} has no birth identity"
        )
    return snapshot


def _launchd_process_start_time(pid: int | None) -> int | None:
    if pid is None:
        return None
    from gateway.status import get_process_start_time

    return get_process_start_time(pid)


def _launchd_pid_is_live(
    pid: int, expected_start_time: int | None = None
) -> bool:
    from gateway.status import _pid_exists, get_process_start_time

    if not _pid_exists(pid):
        return False
    if expected_start_time is None:
        return True
    current_start_time = get_process_start_time(pid)
    return current_start_time is not None and current_start_time == expected_start_time


def _attest_launchd_runtime_identity(
    pid: int,
    start_time: int | None,
    *,
    plist_argv: Iterable[str],
    hermes_home: Path,
    profile: str | None,
) -> GatewayProcessIdentity:
    """Prove that a launchd PID is the runtime described by its plist."""
    if start_time is None:
        raise LaunchdAllOperationError(
            f"PID {pid} has no birth identity for launchd attestation"
        )
    try:
        identity, _process = _read_live_gateway_process_identity(pid)
    except ProcessLookupError as exc:
        raise LaunchdAllOperationError(
            f"PID {pid} disappeared during launchd attestation"
        ) from exc
    except PermissionError as exc:
        raise LaunchdAllOperationError(
            f"PID {pid} identity is unreadable during launchd attestation"
        ) from exc
    except GatewayProcessEnumerationError as exc:
        raise LaunchdAllOperationError(str(exc)) from exc
    if identity.start_time != start_time:
        raise LaunchdAllOperationError(
            f"PID {pid} birth identity changed during launchd attestation"
        )
    if not process_identity_matches_target(
        identity,
        argv=plist_argv,
        hermes_home=hermes_home,
        profile=profile,
    ):
        raise LaunchdAllOperationError(
            f"PID {pid} is a foreign launchd runtime (argv/profile/home mismatch)"
        )
    return identity


def _read_secure_launchd_plist(path: Path) -> _SecureLaunchdPlist:
    """Read one LaunchAgent through an attested, non-following file descriptor."""
    fd: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise LaunchdInventoryError(f"Refusing non-regular launchd plist: {path}")
        if metadata.st_uid not in {os.getuid(), 0}:
            raise LaunchdInventoryError(f"Refusing non-owned launchd plist: {path}")
        if metadata.st_mode & 0o022:
            raise LaunchdInventoryError(
                f"Refusing writable launchd plist: {path}"
            )
        with os.fdopen(fd, "rb") as handle:
            raw = handle.read()
        fd = None
    except LaunchdInventoryError:
        raise
    except (OSError, ValueError) as exc:
        if path.is_symlink():
            raise LaunchdInventoryError(
                f"Refusing non-regular launchd plist: {path}"
            ) from exc
        raise LaunchdInventoryError(
            f"Could not securely read launchd plist {path}: {exc}"
        ) from exc
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    stat_identity = (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
        int(metadata.st_ctime_ns),
    )
    fingerprint = hashlib.sha256(
        raw + b"\0" + repr(stat_identity).encode("ascii")
    ).hexdigest()
    try:
        document = plistlib.loads(raw)
    except (plistlib.InvalidFileException, ValueError, TypeError) as exc:
        raise LaunchdInventoryError(f"Could not validate launchd plist {path}") from exc
    if not isinstance(document, dict):
        raise LaunchdInventoryError(f"Could not validate launchd plist {path}")
    return _SecureLaunchdPlist(path, document, raw, fingerprint, stat_identity)


def _launchd_gateway_profile_invocation(
    label: str, document: dict
) -> tuple[Path, str | None]:
    """Validate a plist's Hermes identity without consulting the active caller."""
    for key in ("RunAtLoad", "KeepAlive"):
        if document.get(key) is not True:
            raise LaunchdAllOperationError(
                f"{label}: {key} must be scalar true for Hermes supervision"
            )
    environment = document.get("EnvironmentVariables")
    home_value = environment.get("HERMES_HOME") if isinstance(environment, dict) else None
    if not isinstance(home_value, str) or not home_value.strip():
        raise LaunchdAllOperationError(f"{label}: HERMES_HOME identity is missing")
    home_path = Path(home_value)
    if not home_path.is_absolute():
        raise LaunchdAllOperationError(f"{label}: HERMES_HOME must be absolute")
    hermes_home = home_path.resolve()

    arguments = document.get("ProgramArguments")
    if not _launchd_program_arguments_are_hermes_gateway(arguments):
        raise LaunchdAllOperationError(f"{label}: foreign ProgramArguments")
    try:
        gateway_index = arguments.index("gateway")
    except ValueError as exc:
        raise LaunchdAllOperationError(f"{label}: gateway invocation is missing") from exc
    if arguments[gateway_index:] != ["gateway", "run", "--replace"]:
        raise LaunchdAllOperationError(f"{label}: ProgramArguments are not exact")

    profile_name: str | None = None
    profile_positions = [i for i, value in enumerate(arguments) if value == "--profile"]
    if profile_positions:
        if len(profile_positions) != 1:
            raise LaunchdAllOperationError(f"{label}: duplicate --profile arguments")
        profile_index = profile_positions[0]
        if profile_index >= gateway_index or profile_index + 1 >= gateway_index:
            raise LaunchdAllOperationError(f"{label}: malformed --profile invocation")
        profile_name = arguments[profile_index + 1]
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", profile_name):
            raise LaunchdAllOperationError(f"{label}: invalid profile name")

    native_root = (_launchd_user_home() / ".hermes").resolve()
    custom_suffix = hashlib.sha256(str(hermes_home).encode()).hexdigest()[:8]
    custom_label = f"ai.hermes.gateway-{custom_suffix}"

    if hermes_home == native_root:
        if label != "ai.hermes.gateway" or profile_name is not None:
            raise LaunchdAllOperationError(
                f"{label}: native default HERMES_HOME has the wrong identity"
            )
        return hermes_home, None

    # Check the hash/no-profile custom form before the structural profiles/name
    # form. A user may intentionally have a custom root whose final components
    # happen to be ``profiles/<name>``.
    if label == custom_label and profile_name is None:
        return hermes_home, None

    if (
        hermes_home.parent.name == "profiles"
        and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", hermes_home.name)
        and label == f"ai.hermes.gateway-{hermes_home.name}"
        and profile_name == hermes_home.name
    ):
        return hermes_home, profile_name

    raise LaunchdAllOperationError(
        f"{label}: HERMES_HOME, label, and --profile do not describe one target"
    )


def _read_launchd_all_plist_identity(path: Path) -> _LaunchdAllPlistIdentity:
    """Read and attest one plist's label, profile invocation, and home."""
    try:
        secure = _read_secure_launchd_plist(path)
    except LaunchdInventoryError as exc:
        raise LaunchdAllOperationError(str(exc)) from exc

    label = path.stem
    if secure.document.get("Label") != label:
        raise LaunchdAllOperationError(f"Launchd plist label mismatch for {path}")
    hermes_home, _ = _launchd_gateway_profile_invocation(
        label, secure.document
    )
    arguments = tuple(secure.document["ProgramArguments"])
    role, profile, classified_home = classify_gateway_argv(
        arguments,
        environment={"HERMES_HOME": str(hermes_home)},
    )
    if (
        role is not GatewayRuntimeRole.RUNTIME
        or classified_home != hermes_home
    ):
        raise LaunchdAllOperationError(
            f"{label}: canonical runtime identity does not match the plist"
        )
    return _LaunchdAllPlistIdentity(
        label=label,
        path=path,
        fingerprint=secure.fingerprint,
        hermes_home=hermes_home,
        stat_identity=secure.stat_identity,
        argv=arguments,
        profile=profile,
    )


def _launchd_all_preflight_targets() -> tuple[LaunchdAllTarget, ...]:
    """Validate the complete installed inventory before any mutation."""
    targets: list[LaunchdAllTarget] = []
    try:
        inventory = _installed_launchd_gateway_plists()
    except (LaunchdInventoryError, LaunchdAllOperationError) as exc:
        raise LaunchdAllOperationError(str(exc)) from exc

    for label, plist_path in inventory:
        identity = _read_launchd_all_plist_identity(plist_path)
        probe = _probe_launchd_label_domains(label)
        if probe.unknown or probe.disabled_unknown:
            raise LaunchdAllOperationError(
                f"Could not verify launchd state for {label}: "
                + "; ".join(
                    probe.unknown_details
                    + probe.disabled_unknown_details
                    + tuple(probe.unknown)
                    + tuple(probe.disabled_unknown)
                )
            )
        if len(probe.registered) > 1:
            raise LaunchdAllOperationError(
                f"Refusing cross-profile launchd operation: {label} is registered "
                f"in both {probe.registered[0]} and {probe.registered[1]}"
            )
        domain = (
            probe.registered[0]
            if probe.registered
            else _launchd_validated_manager_domain()
        )
        loaded = bool(probe.registered)
        pid = probe.pid_for(domain) if loaded else None
        start_time = _launchd_process_start_time(pid) if pid is not None else None
        if pid is not None and start_time is None:
            raise LaunchdAllOperationError(
                f"{domain}/{label}: loaded PID {pid} has no birth identity"
            )
        runtime_identity = None
        if pid is not None:
            runtime_identity = _attest_launchd_runtime_identity(
                pid,
                start_time,
                plist_argv=identity.argv,
                hermes_home=identity.hermes_home,
                profile=identity.profile,
            )
        targets.append(
            LaunchdAllTarget(
                label=identity.label,
                domain=domain,
                plist_path=identity.path,
                plist_fingerprint=identity.fingerprint,
                hermes_home=identity.hermes_home,
                was_loaded=loaded,
                pid=pid,
                start_time=start_time,
                probe=probe,
                plist_stat_identity=identity.stat_identity,
                plist_argv=identity.argv,
                plist_profile=identity.profile,
                runtime_identity=runtime_identity,
            )
        )
    return tuple(targets)


def _launchd_single_preflight_target(
    label: str,
    plist_path: Path,
) -> LaunchdAllTarget:
    """Attest one profile's plist, launchd domain, PID, and live identity.

    Single-profile lifecycle commands use this immutable target instead of a
    profile runtime record.  If launchd has no registered PID, an explicitly
    managed detached fallback may be used only after it matches the exact
    plist argv/profile/home and has a readable birth identity.
    """
    if not plist_path.exists():
        raise LaunchdAllOperationError(f"Launchd plist is missing: {plist_path}")

    identity = _read_launchd_all_plist_identity(plist_path)
    if identity.label != label:
        raise LaunchdAllOperationError(
            f"Launchd target label mismatch: expected {label}, got {identity.label}"
        )

    probe = _probe_launchd_label_domains(label)
    _launchd_all_require_known_probe(label, probe)
    if len(probe.registered) > 1:
        raise LaunchdAllOperationError(
            f"Refusing single-profile launchd operation: {label} is registered "
            f"in both {probe.registered[0]} and {probe.registered[1]}"
        )
    if not probe.registered and len(probe.disabled) > 1:
        raise LaunchdAllOperationError(
            f"Refusing single-profile launchd operation: {label} is disabled "
            f"in both {probe.disabled[0]} and {probe.disabled[1]}"
        )
    domain = (
        probe.registered[0]
        if probe.registered
        else _launchd_domain_for_label(label)
    )
    pid = probe.pid_for(domain) if probe.registered else None
    start_time = _launchd_process_start_time(pid) if pid is not None else None
    runtime_identity = None
    if pid is not None:
        runtime_identity = _attest_launchd_runtime_identity(
            pid,
            start_time,
            plist_argv=identity.argv,
            hermes_home=identity.hermes_home,
            profile=identity.profile,
        )
    elif _launchd_unsupported_marker_exists():
        # An unsupported launchd domain may leave an intentionally detached
        # runtime.  Its profile record is still untrusted: attest the current
        # live occupant against the exact plist before retaining it.
        from gateway.status import get_running_pid

        fallback_pid = get_running_pid(cleanup_stale=False)
        if fallback_pid is not None:
            fallback_start = _launchd_process_start_time(fallback_pid)
            runtime_identity = _attest_launchd_runtime_identity(
                fallback_pid,
                fallback_start,
                plist_argv=identity.argv,
                hermes_home=identity.hermes_home,
                profile=identity.profile,
            )
            pid = fallback_pid
            start_time = fallback_start

    return LaunchdAllTarget(
        label=identity.label,
        domain=domain,
        plist_path=identity.path,
        plist_fingerprint=identity.fingerprint,
        hermes_home=identity.hermes_home,
        was_loaded=bool(probe.registered),
        pid=pid,
        start_time=start_time,
        probe=probe,
        plist_stat_identity=identity.stat_identity,
        plist_argv=identity.argv,
        plist_profile=identity.profile,
        runtime_identity=runtime_identity,
    )


def _revalidate_launchd_target(target: LaunchdAllTarget) -> None:
    """Re-attest a target immediately before a desired/runtime mutation."""
    identity = _read_launchd_all_plist_identity(target.plist_path)
    if (
        identity.label != target.label
        or identity.hermes_home != target.hermes_home
        or identity.fingerprint != target.plist_fingerprint
        or identity.stat_identity != target.plist_stat_identity
        or identity.argv != target.plist_argv
        or identity.profile != target.plist_profile
    ):
        raise LaunchdAllOperationError(
            f"Launchd target identity changed before mutation: {target.launchctl_target}"
        )


def _launchd_all_bootstrap(target: LaunchdAllTarget) -> bool:
    """Bootstrap one definitively unloaded target without booting out races."""
    _revalidate_launchd_target(target)
    try:
        subprocess.run(
            ["launchctl", "bootstrap", target.domain, str(target.plist_path)],
            check=True,
            timeout=30,
        )
        return False
    except subprocess.CalledProcessError as exc:
        if exc.returncode != 5:
            raise
        # Exit 5 means only that bootstrap did not complete. It is not
        # permission to boot out a label: the label may have appeared in this
        # exact domain concurrently, or may now be ambiguous across domains.
        probe = _probe_launchd_label_domains(target.label)
        _launchd_all_require_known_probe(target.label, probe)
        if probe.registered != (target.domain,):
            raise LaunchdAllOperationError(
                f"{target.launchctl_target}: bootstrap exit 5 left registration "
                f"ambiguous or absent ({probe.registered or 'none'})"
            )
        return True


def _launchd_all_verify_live(
    target: LaunchdAllTarget,
    *,
    successor_of: tuple[int, int] | None = None,
    expected_identity: tuple[int, int] | None = None,
    expected_disabled: set[str] | None = None,
) -> bool:
    """Verify that launchd owns a live PID for the exact target."""
    probe = _probe_launchd_label_domains(target.label)
    _launchd_all_require_known_probe(target.label, probe)
    if expected_disabled is not None and set(probe.disabled) != expected_disabled:
        raise LaunchdAllOperationError(
            f"{target.launchctl_target}: concurrent disabled state changed while "
            f"verifying to {list(probe.disabled)}"
        )
    if len(probe.registered) > 1:
        raise LaunchdAllOperationError(
            f"{target.launchctl_target}: registration became ambiguous"
        )
    if not probe.registered:
        return False
    if probe.registered != (target.domain,):
        raise LaunchdAllOperationError(
            f"{target.launchctl_target}: registration moved to {probe.registered[0]}"
        )
    pid = probe.pid_for(target.domain)
    if pid is None or not _launchd_pid_is_live(pid):
        return False
    start_time = _launchd_process_start_time(pid)
    if start_time is None:
        raise LaunchdAllOperationError(
            f"{target.launchctl_target}: live PID {pid} has no birth identity"
        )
    if expected_identity is not None and (pid, start_time) != expected_identity:
        raise LaunchdAllOperationError(
            f"{target.launchctl_target}: supervised PID identity changed while "
            f"verifying from {expected_identity} to {(pid, start_time)}"
        )
    if not target.plist_argv:
        raise LaunchdAllOperationError(
            f"{target.launchctl_target}: plist argv was not retained for attestation"
        )
    runtime_identity = _attest_launchd_runtime_identity(
        pid,
        start_time,
        plist_argv=target.plist_argv,
        hermes_home=target.hermes_home,
        profile=target.plist_profile,
    )
    if target.runtime_identity is not None and expected_identity is None:
        # A preflight identity is immutable for a loaded predecessor.  For a
        # target that became live after bootstrap, no predecessor is retained.
        if runtime_identity.pid == target.runtime_identity.pid and (
            runtime_identity.start_time == target.runtime_identity.start_time
        ) and runtime_identity.identity_key() != target.runtime_identity.identity_key():
            raise LaunchdAllOperationError(
                f"{target.launchctl_target}: live runtime identity changed"
            )
    if successor_of is not None:
        old_pid, old_start_time = successor_of
        if (pid, start_time) == successor_of:
            return False
        if pid == old_pid and start_time != old_start_time:
            raise LaunchdAllOperationError(
                f"{target.launchctl_target}: supervised PID {pid} was reused "
                "while waiting for a successor"
            )
    return True


def _launchd_all_wait_for_live(
    target: LaunchdAllTarget,
    *,
    successor_of: tuple[int, int] | None = None,
    expected_disabled: set[str] | None = None,
    timeout: float = 10.0,
) -> bool:
    deadline = time.monotonic() + max(timeout, 0.1)
    while time.monotonic() < deadline:
        if _launchd_all_verify_live(
            target,
            successor_of=successor_of,
            expected_disabled=expected_disabled,
        ):
            return True
        time.sleep(0.2)
    return _launchd_all_verify_live(
        target,
        successor_of=successor_of,
        expected_disabled=expected_disabled,
    )


def _launchd_all_wait_for_successor(
    target: LaunchdAllTarget,
    old_pid: int,
    old_start_time: int,
    *,
    expected_disabled: set[str] | None = None,
    timeout: float,
) -> bool:
    return _launchd_all_wait_for_live(
        target,
        successor_of=(old_pid, old_start_time),
        expected_disabled=expected_disabled,
        timeout=timeout,
    )


def _launchd_all_require_known_probe(label: str, probe: LaunchdLabelProbe) -> None:
    if probe.unknown or probe.disabled_unknown:
        details = (
            probe.unknown_details
            + probe.disabled_unknown_details
            + tuple(probe.unknown)
            + tuple(probe.disabled_unknown)
        )
        raise LaunchdAllOperationError(
            f"Could not inspect launchd state for {label}: " + "; ".join(details)
        )


def _launchd_all_observe_target(
    target: LaunchdAllTarget,
    expected_disabled: set[str],
    *,
    allow_live_pid_appearance: bool = False,
) -> tuple[LaunchdLabelProbe, int | None, int | None]:
    """Re-probe and compare the immutable plan before one target mutation."""
    probe = _probe_launchd_label_domains(target.label)
    _launchd_all_require_known_probe(target.label, probe)
    if len(probe.registered) > 1:
        raise LaunchdAllOperationError(
            f"{target.launchctl_target}: concurrent registration in {probe.registered}"
        )
    if probe.registered != target.probe.registered:
        raise LaunchdAllOperationError(
            f"{target.launchctl_target}: concurrent registration changed from "
            f"{target.probe.registered or 'none'} to {probe.registered or 'none'}"
        )
    if set(probe.disabled) != expected_disabled:
        raise LaunchdAllOperationError(
            f"{target.launchctl_target}: concurrent disabled state changed from "
            f"{sorted(expected_disabled)} to {list(probe.disabled)}"
        )
    current_domain = probe.registered[0] if probe.registered else _launchd_validated_manager_domain()
    if current_domain != target.domain:
        raise LaunchdAllOperationError(
            f"{target.label}: concurrent launchd domain changed from "
            f"{target.domain} to {current_domain}"
        )
    pid = probe.pid_for(current_domain) if probe.registered else None
    start_time = _launchd_process_start_time(pid) if pid is not None else None
    if pid != target.pid or start_time != target.start_time:
        if not (
            allow_live_pid_appearance
            and target.was_loaded
            and target.pid is None
            and pid is not None
            and start_time is not None
        ):
            raise LaunchdAllOperationError(
                f"{target.launchctl_target}: concurrent PID identity changed from "
                f"{(target.pid, target.start_time)} to {(pid, start_time)}"
            )
    return probe, pid, start_time


def _launchd_all_prepare_mutation(
    target: LaunchdAllTarget,
    expected_disabled: set[str],
    *,
    allow_live_pid_appearance: bool = False,
) -> tuple[int | None, int | None]:
    _probe, pid, start_time = _launchd_all_observe_target(
        target,
        expected_disabled,
        allow_live_pid_appearance=allow_live_pid_appearance,
    )
    _revalidate_launchd_target(target)
    return pid, start_time


def _launchd_all_prepare_post_bootstrap_kickstart(
    target: LaunchdAllTarget, expected_disabled: set[str]
) -> bool:
    """Validate the intended registration before kickstarting a new load."""
    probe = _probe_launchd_label_domains(target.label)
    _launchd_all_require_known_probe(target.label, probe)
    if probe.registered != (target.domain,):
        raise LaunchdAllOperationError(
            f"{target.launchctl_target}: bootstrap registration is "
            f"{probe.registered or 'absent'}"
        )
    if set(probe.disabled) != expected_disabled:
        raise LaunchdAllOperationError(
            f"{target.launchctl_target}: concurrent disabled state changed "
            f"after bootstrap to {list(probe.disabled)}"
        )
    _revalidate_launchd_target(target)
    pid = probe.pid_for(target.domain)
    if pid is None:
        return False
    start_time = _launchd_process_start_time(pid)
    if start_time is None:
        raise LaunchdAllOperationError(
            f"{target.launchctl_target}: bootstrapped PID {pid} has no birth identity"
        )
    if not _launchd_pid_is_live(pid, start_time):
        return False
    _attest_launchd_runtime_identity(
        pid,
        start_time,
        plist_argv=target.plist_argv,
        hermes_home=target.hermes_home,
        profile=target.plist_profile,
    )
    return True


def _launchd_all_start_bootstrapped_target(
    target: LaunchdAllTarget,
    expected_disabled: set[str],
    *,
    timeout: float = 2.0,
) -> bool:
    """Observe a new registration before deciding whether to kickstart it."""
    if _launchd_all_wait_for_live(
        target, expected_disabled=expected_disabled, timeout=timeout
    ):
        return True
    # Re-probe immediately before the non-killing action. A RunAtLoad/KeepAlive
    # job may have started during the bounded observation; never replace it.
    if _launchd_all_prepare_post_bootstrap_kickstart(target, expected_disabled):
        return True
    subprocess.run(
        ["launchctl", "kickstart", target.launchctl_target],
        check=True,
        timeout=30,
    )
    return _launchd_all_wait_for_live(
        target, expected_disabled=expected_disabled, timeout=timeout
    )


def _launchd_all_rollback_bootstrap(
    target: LaunchdAllTarget,
    expected_disabled: set[str],
    post_mutation_snapshot: _LaunchdAllStateSnapshot | None = None,
) -> list[str]:
    """Report bootstrap compensation without destructive registration removal.

    launchd has no atomic PID-CAS bootout.  A registration that was safe at a
    precheck can gain a KeepAlive PID before a later bootout, so rollback keeps
    every surviving registration in place and reports the partial state for a
    subsequent safe retry.
    """
    failures: list[str] = []
    try:
        _revalidate_launchd_target(target)
        probe = _probe_launchd_label_domains(target.label)
        _launchd_all_require_known_probe(target.label, probe)
        current = _launchd_all_snapshot_from_probe(probe)
        if not current.registered:
            # A disappearing registration is already compensated.
            return failures
        expected = post_mutation_snapshot or _launchd_all_initial_snapshot(target)
        if len(current.registered) > 1:
            failures.append(
                f"{target.launchctl_target} rollback registration conflict; "
                "registration was preserved"
            )
        elif not _launchd_all_snapshot_matches(expected, current):
            failures.append(
                f"{target.launchctl_target} rollback state diverged from the "
                "transaction snapshot; registration was preserved"
            )
        elif set(expected.disabled) != expected_disabled:
            failures.append(
                f"{target.launchctl_target} rollback disabled state changed to "
                f"{list(expected.disabled)}; registration was preserved"
            )
        elif expected.registered != (target.domain,):
            failures.append(
                f"{target.launchctl_target} rollback refused registration in "
                f"{expected.registered or 'none'}; registration was preserved"
            )
        elif target.was_loaded or target.probe.registered:
            failures.append(
                f"{target.launchctl_target} rollback registration was not "
                "created by this bootstrap transaction; registration was preserved"
            )
        elif current.pid is not None and _launchd_pid_is_live(
            current.pid, current.start_time
        ):
            failures.append(
                f"{target.launchctl_target} rollback preserved live PID "
                f"{current.pid}; partial rollback requires a safe retry"
            )
        else:
            failures.append(
                f"{target.launchctl_target} rollback preserved a registered "
                "job without a live PID; partial rollback requires a safe retry"
            )
        # An absent registration is not mutated. A changed/ambiguous/live state
        # is reported as a partial rollback failure above.
    except _LAUNCHD_ALL_OPERATIONAL_ERRORS as exc:
        failures.append(
            f"{target.launchctl_target} rollback ({_launchd_all_failure_detail(exc)})"
        )
    return failures


def _launchd_all_restore_changed_fences(
    target: LaunchdAllTarget,
    changed_domains: list[str],
    expected_disabled: set[str],
    post_mutation_snapshot: _LaunchdAllStateSnapshot | None = None,
) -> list[str]:
    """Restore only desired-state bits changed by one start transaction."""
    failures: list[str] = []
    if not changed_domains:
        return failures
    try:
        _revalidate_launchd_target(target)
        expected = post_mutation_snapshot
        if expected is None:
            failures.append(
                f"{target.launchctl_target} rollback has no transaction state "
                "snapshot; no rollback mutation was attempted"
            )
            return failures
        current = _launchd_all_capture_snapshot(
            target, expected_snapshot=expected
        )
        if not _launchd_all_snapshot_matches(expected, current):
            failures.append(
                f"{target.launchctl_target} rollback state diverged from the "
                "transaction snapshot; no rollback mutation was attempted"
            )
            return failures

        expected_state = expected
        for domain in reversed(changed_domains):
            if domain in expected_state.disabled:
                failures.append(
                    f"{domain}/{target.label} rollback state already disabled "
                    "in the transaction snapshot; no rollback mutation was attempted"
                )
                return failures
            # This probe is intentionally immediately adjacent to the
            # destructive compensation write.  It must still match the exact
            # immutable runtime snapshot; a concurrent registration/PID/birth
            # movement aborts without another disable.
            _revalidate_launchd_target(target)
            _launchd_all_capture_snapshot(
                target, expected_snapshot=expected_state
            )
            try:
                _launchd_disable(domain, target.label)
            except _LAUNCHD_ALL_OPERATIONAL_ERRORS as exc:
                failures.append(
                    f"{domain}/{target.label} rollback "
                    f"({_launchd_all_failure_detail(exc)})"
                )
                return failures

            expected_state = _LaunchdAllStateSnapshot(
                registered=expected_state.registered,
                disabled=tuple(
                    sorted(set(expected_state.disabled) | {domain})
                ),
                pid=expected_state.pid,
                start_time=expected_state.start_time,
                runtime_identity=expected_state.runtime_identity,
            )
            current = _launchd_all_capture_snapshot(
                target, expected_snapshot=expected_state
            )
            if not _launchd_all_snapshot_matches(expected_state, current):
                failures.append(
                    f"{target.launchctl_target} rollback state diverged after "
                    f"{domain}/{target.label}; no further rollback mutation was attempted"
                )
                return failures
        if set(expected_state.disabled) != expected_disabled | set(changed_domains):
            failures.append(
                f"{target.launchctl_target} rollback disabled state is "
                f"{list(expected_state.disabled)}, expected "
                f"{list(expected_disabled | set(changed_domains))}"
            )
    except _LAUNCHD_ALL_OPERATIONAL_ERRORS as exc:
        failures.append(
            f"{target.launchctl_target} rollback ({_launchd_all_failure_detail(exc)})"
        )
    return failures


_LAUNCHD_ALL_OPERATIONAL_ERRORS = (
    LaunchdAllOperationError,
    subprocess.CalledProcessError,
    subprocess.TimeoutExpired,
    OSError,
)


def _launchd_all_failure_detail(exc: BaseException) -> str:
    if isinstance(exc, subprocess.CalledProcessError):
        return f"launchd exit {exc.returncode}"
    return str(exc) or type(exc).__name__


def _launchd_start_all_target(target: LaunchdAllTarget) -> LaunchdAllTargetOutcome:
    """Start one target transactionally, without disturbing a live target."""
    original_disabled = set(target.probe.disabled)
    expected_disabled = set(original_disabled)
    newly_enabled: list[str] = []
    bootstrapped_by_us = False
    fence_post_mutation_snapshot: _LaunchdAllStateSnapshot | None = None
    bootstrap_post_mutation_snapshot: _LaunchdAllStateSnapshot | None = None
    try:
        if target.probe.registered:
            # An existing registration owns exactly one domain.  A disabled
            # peer is independent desired state and must not be cleared by
            # starting the registered target.  Its own disabled bit is a
            # deliberate fence and must not be revived either.
            if target.domain in original_disabled:
                _launchd_all_prepare_mutation(target, expected_disabled)
                return LaunchdAllTargetOutcome(
                    target.label, target.domain, "preserved"
                )
            domains_to_enable = ()
        elif original_disabled:
            # An unloaded target with any pre-existing fence has no
            # authoritative domain.  Preserve the fence rather than choosing
            # a manager domain and inventing a bootstrap.
            _launchd_all_prepare_mutation(target, expected_disabled)
            return LaunchdAllTargetOutcome(target.label, target.domain, "preserved")
        else:
            domains_to_enable = ()

        for domain in domains_to_enable:
            _launchd_all_prepare_mutation(target, expected_disabled)
            _launchd_enable(domain, target.label)
            newly_enabled.append(domain)
            expected_disabled.remove(domain)
            fence_post_mutation_snapshot = _launchd_all_fence_snapshot(
                target, expected_disabled
            )

        if target.was_loaded:
            if target.pid is not None:
                if target.start_time is None:
                    raise LaunchdAllOperationError(
                        f"{target.launchctl_target}: loaded PID {target.pid} has no birth identity"
                    )
                _launchd_all_prepare_mutation(target, expected_disabled)
                if not _launchd_all_verify_live(
                    target,
                    expected_identity=(target.pid, target.start_time),
                    expected_disabled=expected_disabled,
                ):
                    raise LaunchdAllOperationError(
                        f"{target.launchctl_target}: exact supervised PID is not live"
                    )
            else:
                _launchd_all_prepare_mutation(
                    target,
                    expected_disabled,
                    allow_live_pid_appearance=True,
                )
                if _launchd_all_verify_live(
                    target, expected_disabled=expected_disabled
                ):
                    return LaunchdAllTargetOutcome(
                        target.label, target.domain, "started"
                    )
                subprocess.run(
                    ["launchctl", "kickstart", target.launchctl_target],
                    check=True,
                    timeout=30,
                )
                if not _launchd_all_wait_for_live(target, expected_disabled=expected_disabled):
                    raise LaunchdAllOperationError(
                        f"{target.launchctl_target}: kickstart produced no live PID"
                    )
        else:
            _launchd_all_prepare_mutation(target, expected_disabled)
            bootstrap_raced = _launchd_all_bootstrap(target)
            bootstrapped_by_us = not bootstrap_raced
            bootstrap_post_mutation_snapshot = _launchd_all_bootstrap_snapshot(
                target, expected_disabled
            )
            live = _launchd_all_start_bootstrapped_target(
                target, expected_disabled
            )
            if not live:
                raise LaunchdAllOperationError(
                    f"{target.launchctl_target}: bootstrap produced no live PID"
                )
        return LaunchdAllTargetOutcome(target.label, target.domain, "started")
    except _LAUNCHD_ALL_OPERATIONAL_ERRORS as exc:
        rollback_failures: list[str] = []
        if bootstrapped_by_us:
            rollback_failures.extend(
                _launchd_all_rollback_bootstrap(
                    target, expected_disabled, bootstrap_post_mutation_snapshot
                )
            )
        rollback_failures.extend(
            _launchd_all_restore_changed_fences(
                target,
                newly_enabled,
                expected_disabled,
                fence_post_mutation_snapshot,
            )
        )
        detail = _launchd_all_failure_detail(exc)
        if rollback_failures:
            detail += "; rollback failed: " + "; ".join(rollback_failures)
        raise LaunchdAllOperationError(
            f"{target.launchctl_target}: {detail}",
            (f"{target.launchctl_target}: {detail}",),
        ) from exc


def _launchd_start_all_locked() -> LaunchdAllResult:
    """Start every installed Hermes LaunchAgent without a process sweep."""
    try:
        targets = _launchd_all_preflight_targets()
    except LaunchdAllOperationError:
        raise
    if not targets:
        print("✓ No installed launchd gateways; nothing to start")
        return LaunchdAllResult("start", ())

    failures: list[str] = []
    outcomes: list[LaunchdAllTargetOutcome] = []
    for target in targets:
        try:
            outcomes.append(_launchd_start_all_target(target))
        except _LAUNCHD_ALL_OPERATIONAL_ERRORS as exc:
            detail = _launchd_all_failure_detail(exc)
            failure = (
                detail
                if detail.startswith(f"{target.launchctl_target}:")
                else f"{target.launchctl_target}: {detail}"
            )
            failures.append(failure)
            outcomes.append(
                LaunchdAllTargetOutcome(target.label, target.domain, "failed", detail)
            )
    result = LaunchdAllResult("start", tuple(outcomes))
    if failures:
        raise LaunchdAllOperationError(
            "Could not start every installed launchd gateway: " + "; ".join(failures),
            tuple(failures),
            result.outcomes,
        )
    started = sum(outcome.status == "started" for outcome in result.outcomes)
    print(f"✓ Started {started} launchd gateway target(s)")
    return result


def launchd_start_all() -> LaunchdAllResult:
    with all_profile_lifecycle_lock(timeout=30.0):
        return _launchd_start_all_locked()


def _launchd_restart_all_target(target: LaunchdAllTarget) -> LaunchdAllTargetOutcome:
    """Restart one enabled target and verify a new supervised process."""
    expected_disabled = set(target.probe.disabled)
    _launchd_all_prepare_mutation(target, expected_disabled)
    # A registered target is authoritative for its own domain.  A disabled
    # peer must not strand an enabled registration; only the registered
    # target's own disabled bit suppresses restart.  An unloaded target has no
    # authoritative domain, so any pre-disabled bit remains a fence.
    if (
        target.domain in expected_disabled
        if target.probe.registered
        else bool(expected_disabled)
    ):
        return LaunchdAllTargetOutcome(target.label, target.domain, "preserved")

    if target.was_loaded and target.pid is not None:
        if target.start_time is None:
            raise LaunchdAllOperationError(
                f"{target.launchctl_target}: loaded PID {target.pid} has no birth identity"
            )
        if not _launchd_all_verify_live(
            target,
            expected_identity=(target.pid, target.start_time),
            expected_disabled=expected_disabled,
        ):
            raise LaunchdAllOperationError(
                f"{target.launchctl_target}: old supervised PID is not live"
            )
        drain_timeout = _get_restart_drain_timeout()
        predecessor_wait_succeeded = _graceful_restart_via_sigusr1(
            target.pid,
            drain_timeout,
            expected_start_time=target.start_time,
            expected_identity=target.runtime_identity,
        )
        successor_verified = _launchd_all_wait_for_successor(
            target,
            target.pid,
            target.start_time,
            expected_disabled=expected_disabled,
            timeout=(drain_timeout + 30 if predecessor_wait_succeeded else 5),
        )
        if successor_verified:
            return LaunchdAllTargetOutcome(target.label, target.domain, "restarted")
        if not predecessor_wait_succeeded:
            raise LaunchdAllOperationError(
                f"{target.launchctl_target}: old PID did not exit and no exact "
                "successor was verified"
            )
        raise LaunchdAllOperationError(
            f"{target.launchctl_target}: new supervised PID was not verified"
        )

    bootstrapped_by_us = False
    bootstrap_post_mutation_snapshot: _LaunchdAllStateSnapshot | None = None
    try:
        if not target.was_loaded:
            _launchd_all_prepare_mutation(target, expected_disabled)
            bootstrap_raced = _launchd_all_bootstrap(target)
            bootstrapped_by_us = True
            if bootstrap_raced:
                bootstrapped_by_us = False
            bootstrap_post_mutation_snapshot = _launchd_all_bootstrap_snapshot(
                target, expected_disabled
            )
            if not _launchd_all_start_bootstrapped_target(
                target, expected_disabled
            ):
                raise LaunchdAllOperationError(
                    f"{target.launchctl_target}: new supervised PID was not verified"
                )
            return LaunchdAllTargetOutcome(target.label, target.domain, "restarted")

        # A loaded registration with no PID is a not-running job, not an
        # unloaded job. Re-probe immediately before the non-killing kickstart;
        # if a PID appeared, leave it alone and verify it instead.
        _launchd_all_prepare_mutation(
            target,
            expected_disabled,
            allow_live_pid_appearance=True,
        )
        if _launchd_all_verify_live(target, expected_disabled=expected_disabled):
            return LaunchdAllTargetOutcome(target.label, target.domain, "restarted")
        subprocess.run(
            ["launchctl", "kickstart", target.launchctl_target],
            check=True,
            timeout=30,
        )
        if not _launchd_all_wait_for_live(
            target, expected_disabled=expected_disabled
        ):
            raise LaunchdAllOperationError(
                f"{target.launchctl_target}: new supervised PID was not verified"
            )
        return LaunchdAllTargetOutcome(target.label, target.domain, "restarted")
    except _LAUNCHD_ALL_OPERATIONAL_ERRORS as exc:
        rollback_failures = (
            _launchd_all_rollback_bootstrap(
                target, expected_disabled, bootstrap_post_mutation_snapshot
            )
            if bootstrapped_by_us
            else []
        )
        detail = _launchd_all_failure_detail(exc)
        if rollback_failures:
            detail += "; rollback failed: " + "; ".join(rollback_failures)
        raise LaunchdAllOperationError(
            f"{target.launchctl_target}: {detail}",
            (f"{target.launchctl_target}: {detail}",),
        ) from exc


def _launchd_restart_all_locked() -> LaunchdAllResult:
    """Restart enabled installed targets individually, never by global sweep."""
    try:
        targets = _launchd_all_preflight_targets()
    except LaunchdAllOperationError:
        raise
    if not targets:
        print("✓ No installed launchd gateways; nothing to restart")
        return LaunchdAllResult("restart", ())

    failures: list[str] = []
    outcomes: list[LaunchdAllTargetOutcome] = []
    for target in targets:
        try:
            outcomes.append(_launchd_restart_all_target(target))
        except _LAUNCHD_ALL_OPERATIONAL_ERRORS as exc:
            detail = _launchd_all_failure_detail(exc)
            failure = (
                detail
                if detail.startswith(f"{target.launchctl_target}:")
                else f"{target.launchctl_target}: {detail}"
            )
            failures.append(failure)
            outcomes.append(
                LaunchdAllTargetOutcome(target.label, target.domain, "failed", detail)
            )
    result = LaunchdAllResult("restart", tuple(outcomes))
    if failures:
        raise LaunchdAllOperationError(
            "Could not restart every enabled launchd gateway: " + "; ".join(failures),
            tuple(failures),
            result.outcomes,
        )
    restarted = sum(outcome.status == "restarted" for outcome in result.outcomes)
    preserved = sum(outcome.status == "preserved" for outcome in result.outcomes)
    print(
        f"✓ Restarted {restarted} launchd gateway target(s)"
        + (f"; preserved {preserved} pre-disabled target(s)" if preserved else "")
    )
    return result


def launchd_restart_all() -> LaunchdAllResult:
    with all_profile_lifecycle_lock(timeout=30.0):
        return _launchd_restart_all_locked()


_LAUNCHD_GATEWAY_PLIST_PATTERN = re.compile(
    r"^ai\.hermes\.gateway(?:-[a-z0-9][a-z0-9_-]{0,63})?\.plist$"
)


def _launchd_program_arguments_are_hermes_gateway(value) -> bool:
    """Return True only for Hermes' exact Python module gateway entrypoint."""
    if not isinstance(value, list) or not all(isinstance(arg, str) for arg in value):
        return False
    if len(value) < 6 or "python" not in Path(value[0]).name.lower():
        return False
    if value[1:3] != ["-m", "hermes_cli.main"]:
        return False
    if value[-3:] != ["gateway", "run", "--replace"]:
        return False
    prefix = value[3:-3]
    if not prefix:
        return True
    return len(prefix) == 2 and prefix[0] == "--profile" and bool(
        re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", prefix[1])
    )


def _installed_launchd_gateway_plists() -> list[tuple[str, Path]]:
    """Return installed Hermes gateway plists, scoped by exact filenames.

    ``gateway stop --all`` cannot switch ``HERMES_HOME`` for every profile
    safely, so enumerate only Hermes' own LaunchAgent naming convention and
    derive each exact label from its filename.  No wildcard launchctl command
    is used, which keeps unrelated/foreign launchd labels untouched.
    """
    agents_dir = _launchd_user_home() / "Library" / "LaunchAgents"
    try:
        entries = sorted(agents_dir.iterdir(), key=lambda path: path.name)
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise LaunchdInventoryError(
            f"Could not enumerate installed LaunchAgents at {agents_dir}: {exc}"
        ) from exc

    candidates: list[tuple[str, Path]] = []
    for path in entries:
        if not _LAUNCHD_GATEWAY_PLIST_PATTERN.fullmatch(path.name):
            continue
        try:
            payload = _read_secure_launchd_plist(path).document
        except LaunchdInventoryError as exc:
            raise LaunchdInventoryError(
                f"Refusing cross-profile launchd operation: {exc}"
            ) from exc
        except (OSError, ValueError, plistlib.InvalidFileException) as exc:
            raise LaunchdInventoryError(
                f"Refusing cross-profile launchd operation: could not validate {path}: {exc}"
            ) from exc
        label = path.stem
        if not isinstance(payload, dict) or payload.get("Label") != label:
            raise LaunchdInventoryError(
                f"Refusing cross-profile launchd operation: {path} Label must equal {label}"
            )
        if not _launchd_program_arguments_are_hermes_gateway(
            payload.get("ProgramArguments")
        ):
            raise LaunchdInventoryError(
                f"Refusing cross-profile launchd operation: {path} is not a Hermes gateway runtime"
            )
        try:
            _launchd_gateway_profile_invocation(label, payload)
        except LaunchdAllOperationError as exc:
            raise LaunchdInventoryError(
                f"Refusing cross-profile launchd operation: {exc}"
            ) from exc
        candidates.append((label, path))
    return candidates


def _launchd_candidate_domains() -> tuple[str, str]:
    uid = os.getuid()  # windows-footgun: POSIX launchd helper
    return (f"gui/{uid}", f"user/{uid}")


def _launchd_disabled_probe(
    domain: str, label: str
) -> tuple[bool | None, str | None]:
    """Read one exact desired-state bit while retaining operational evidence."""
    command = ["launchctl", "print-disabled", domain]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        return None, f"{domain}/{label} print-disabled {type(exc).__name__}"
    if result.returncode != 0:
        detail = (
            getattr(result, "stderr", "")
            or getattr(result, "stdout", "")
            or ""
        ).strip()
        suffix = f": {detail}" if detail else ""
        return None, f"{domain}/{label} print-disabled exit {result.returncode}{suffix}"

    label_pattern = re.compile(
        rf"^\s*(?:{re.escape(label)}|\"{re.escape(label)}\"|'"
        rf"{re.escape(label)}')\s*=>\s*(?:true|disabled)(?:\s*[,;])?\s*$",
        re.IGNORECASE,
    )
    return (
        any(label_pattern.search(line) for line in (result.stdout or "").splitlines()),
        None,
    )


def _probe_launchd_label_domains(label: str) -> LaunchdLabelProbe:
    """Probe registration, PID, and disabled state in both user domains."""
    registered: list[str] = []
    disabled: list[str] = []
    absent: list[str] = []
    unknown: list[str] = []
    unknown_details: list[str] = []
    disabled_unknown: list[str] = []
    disabled_unknown_details: list[str] = []
    pids: list[tuple[str, int | None]] = []
    for domain in _launchd_candidate_domains():
        command = ["launchctl", "print", f"{domain}/{label}"]
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            unknown.append(domain)
            unknown_details.append(f"{domain}/{label} print {type(exc).__name__}")
        else:
            if result.returncode == 0:
                registered.append(domain)
                pid = _parse_launchd_pid_from_list_output(result.stdout or "")
                if pid is None:
                    match = re.search(
                        r"\bpid\b\s*=\s*(-?\d+)",
                        result.stdout or "",
                        re.IGNORECASE,
                    )
                    if match and int(match.group(1)) > 0:
                        pid = int(match.group(1))
                pids.append((domain, pid))
            elif result.returncode in (3, 113):
                absent.append(domain)
            else:
                unknown.append(domain)
                detail = (
                    getattr(result, "stderr", "")
                    or getattr(result, "stdout", "")
                    or ""
                ).strip()
                suffix = f": {detail}" if detail else ""
                unknown_details.append(
                    f"{domain}/{label} print exit {result.returncode}{suffix}"
                )
        disabled_state, disabled_detail = _launchd_disabled_probe(domain, label)
        if disabled_state is True:
            disabled.append(domain)
        elif disabled_state is None:
            disabled_unknown.append(domain)
            if disabled_detail:
                disabled_unknown_details.append(disabled_detail)
    return LaunchdLabelProbe(
        tuple(registered),
        tuple(disabled),
        tuple(absent),
        tuple(unknown),
        tuple(unknown_details),
        tuple(disabled_unknown),
        tuple(disabled_unknown_details),
        tuple(pids),
    )


def _launchd_validated_manager_domain() -> str:
    """Choose gui/user only from a successful, supported managername result."""
    gui_domain, user_domain = _launchd_candidate_domains()
    command = ["launchctl", "managername"]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LaunchdAllOperationError(
            f"launchctl managername failed ({type(exc).__name__})"
        ) from exc
    if result.returncode != 0:
        detail = (
            getattr(result, "stderr", "")
            or getattr(result, "stdout", "")
            or ""
        ).strip()
        suffix = f": {detail}" if detail else ""
        raise LaunchdAllOperationError(
            f"launchctl managername exit {result.returncode}{suffix}"
        )
    manager = (result.stdout or "").strip()
    if manager == "Aqua":
        return gui_domain
    if manager == "Background":
        return user_domain
    raise LaunchdAllOperationError(
        f"launchctl managername returned unsupported manager {manager or '<empty>'}"
    )


def _launchd_default_domain_for_unloaded_label() -> str:
    """Choose the active manager domain without consulting another label."""
    try:
        return _launchd_validated_manager_domain()
    except LaunchdAllOperationError as exc:
        raise LaunchdInventoryError(str(exc)) from exc


def _launchd_domain_for_label(label: str) -> str:
    """Resolve the managing domain for one exact label, without shared cache."""
    probe = _probe_launchd_label_domains(label)
    if probe.unknown or probe.disabled_unknown:
        _launchd_all_require_known_probe(label, probe)
    if len(probe.registered) > 1:
        raise LaunchdInventoryError(
            f"Refusing cross-profile launchd operation: {label} is registered "
            f"in both {probe.registered[0]} and {probe.registered[1]}"
        )
    if probe.registered:
        return probe.registered[0]
    # Stop-all must preserve the exact domain whose disabled bit already
    # represents this target's prior desired state.  This is separate from
    # all-profile start/restart preflight, where an enabled unloaded target
    # must be assigned only by the validated managername result.
    if probe.disabled:
        return probe.disabled[0]
    return _launchd_default_domain_for_unloaded_label()


# On macOS, exit code 125 ("Domain does not support specified action") and
# 3/113 ("Could not find service") all mean the job isn't currently loaded in
# the target domain, so start/restart should re-bootstrap the plist and retry.
_LAUNCHD_JOB_UNLOADED_EXIT_CODES = frozenset({3, 113, 125})

# launchctl returns 5 ("Input/output error") or a persistent 125 in two very
# different situations, so exit 5 is NOT on its own proof the domain is broken:
#   1. The target label appeared in the domain concurrently. This is a
#      converged registration and must be retained, never booted out.
#   2. The domain genuinely can't manage services (macOS 26+, neither
#      `gui/<uid>` nor `user/<uid>` supports service management). Here launchd
#      cannot supervise the gateway at all and we degrade to a detached
#      background process (the `nohup hermes gateway run` workaround). See #23387.
# `_launchctl_bootstrap()` disambiguates with an exact dual-domain probe.
_LAUNCHCTL_DOMAIN_UNSUPPORTED_CODES = frozenset({5, 125})


def _launchd_error_indicates_unloaded(exc: subprocess.CalledProcessError) -> bool:
    """True when launchctl failed because the job isn't loaded (retry bootstrap)."""
    return exc.returncode in _LAUNCHD_JOB_UNLOADED_EXIT_CODES


def _launchctl_domain_unsupported(returncode: int) -> bool:
    """True when launchctl can't manage the domain even after a fresh bootstrap.

    Codes 5 and 125 persist on macOS hosts where neither `gui/<uid>` nor
    `user/<uid>` supports service management; treat these as "launchd
    unavailable" and degrade gracefully to a detached process.
    """
    return returncode in _LAUNCHCTL_DOMAIN_UNSUPPORTED_CODES


# `launchctl bootstrap` returns this when the target label is *already*
# registered in the domain — either a stale load left by an interrupted
# restart or a concurrent registration. EIO means "already loaded", which is
# recoverable only after an exact same-domain probe; it is never permission to
# bootout an observed registration.
_LAUNCHCTL_BOOTSTRAP_EIO = 5


def _launchctl_bootstrap(
    domain: str, plist_path, label: str, *, timeout: int = 30
) -> bool:
    """Bootstrap a launchd job without destructive exit-5 recovery.

    On modern macOS, ``launchctl bootstrap`` of a label that is still
    registered in ``domain`` fails with ``5: Input/output error`` (EIO). That
    is the *already loaded* case — distinct from the domain being unmanageable,
    which callers handle via :func:`_launchctl_domain_unsupported`. A leftover
    registration from an interrupted restart leaves the job
    loaded-but-not-running, so the next bootstrap hits EIO; without this retry
    we misclassify it as "launchd cannot manage this macOS version" and degrade
    to a detached process, silently losing auto-start and crash-restart.

    Exit 5 is not permission to bootout the label. Probe both exact candidate
    domains instead: a same-domain registration is a converged concurrent
    bootstrap and callers may use a non-killing kickstart if it has no PID;
    wrong-domain, dual, absent, or unknown state fails closed. The return value
    is ``True`` for the same-domain convergence case and ``False`` for a
    bootstrap that completed normally.
    """
    try:
        subprocess.run(
            ["launchctl", "bootstrap", domain, str(plist_path)],
            check=True,
            timeout=timeout,
        )
        return False
    except subprocess.CalledProcessError as exc:
        if exc.returncode != _LAUNCHCTL_BOOTSTRAP_EIO:
            raise
        probe = _probe_launchd_label_domains(label)
        _launchd_all_require_known_probe(label, probe)
        if probe.registered != (domain,):
            raise LaunchdAllOperationError(
                f"{domain}/{label}: bootstrap exit 5 left registration "
                f"ambiguous or absent ({probe.registered or 'none'})"
            ) from exc
        return True


def _launchd_reload_log_path() -> Path:
    """Path the launchd reload watchdog tails for persistent-orphan detection."""
    return get_hermes_home() / "logs" / "launchd-reload.log"


def _append_launchd_reload_log(message: str) -> None:
    """Append a timestamped line to the launchd reload log (best-effort)."""
    path = _launchd_reload_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        from datetime import datetime as _dt

        stamp = _dt.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"[{stamp}] {message}\n")
    except OSError:
        pass


def _launchctl_label_registered(label: str) -> bool:
    """True when ``launchctl list <label>`` reports the job as registered."""
    try:
        result = subprocess.run(
            ["launchctl", "list", label],
            check=False,
            timeout=10,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _retry_launchctl_bootstrap_until_registered(
    domain: str, plist_path, label: str, *, deadline: float
) -> bool:
    """Bootstrap with retry until the label is registered or ``deadline`` passes.

    Wraps :func:`_launchctl_bootstrap` (which already recovers the EIO
    "already loaded" case) in a wall-clock retry loop for the *transient*
    failure mode: under high load or a launchd race the bootstrap can fail
    even after ``bootout`` already tore down the prior registration, leaving
    the service orphaned from ``KeepAlive`` supervision. The reported incident
    happened during a graceful drain (default ``agent.restart_drain_timeout``
    = 180s), so a fixed ~10s window is too short — retry until ``deadline``.

    Both ``CalledProcessError`` and ``TimeoutExpired`` are treated as
    retryable: a ``bootstrap`` that times out after ``bootout`` still leaves
    the service unloaded, so it must be retried, not allowed to escape. On
    each failure a timestamped line is appended to the reload log; success is
    confirmed with ``launchctl list`` (not merely a zero bootstrap exit).
    Returns True once the label is registered, False if the deadline is hit.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            _launchctl_bootstrap(domain, plist_path, label, timeout=30)
            if _launchctl_label_registered(label):
                return True
            _append_launchd_reload_log(
                f"bootstrap attempt {attempt} exited 0 but {domain}/{label} "
                f"is not registered (launchctl list) — retrying"
            )
        except subprocess.CalledProcessError as exc:
            _append_launchd_reload_log(
                f"bootstrap attempt {attempt} failed (rc={exc.returncode}) "
                f"for {domain}/{label} — retrying"
            )
        except subprocess.TimeoutExpired:
            _append_launchd_reload_log(
                f"bootstrap attempt {attempt} timed out for {domain}/{label} "
                f"— retrying"
            )
        if time.monotonic() >= deadline:
            return False
        time.sleep(2)


# ── launchd unsupported marker ─────────────────────────────────────────────
# When launchd can't manage the domain on this host (error 5/125, macOS 26+),
# we write a persistent marker so `launchd_status()` can explain that launchd
# supervision is unavailable regardless of whether a fallback process is
# currently running.  The marker is cleared when bootstrap/kickstart succeeds,
# so an OS update that fixes the underlying issue allows automatic recovery.


def _launchd_unsupported_marker_path() -> Path:
    return get_hermes_home() / ".gateway-launchd-unsupported"


def _write_launchd_unsupported_marker() -> None:
    """Persist that launchd cannot supervise the gateway on this host."""
    import json
    from datetime import datetime, timezone

    try:
        _launchd_unsupported_marker_path().write_text(
            json.dumps({
                "written_at": datetime.now(timezone.utc).isoformat(),
                "reason": "launchd domain unsupported (exit 5/125)",
            }),
            encoding="utf-8",
        )
    except OSError:
        pass


def _clear_launchd_unsupported_marker() -> None:
    """Clear the unsupported marker when launchd bootstrap succeeds."""
    try:
        _launchd_unsupported_marker_path().unlink(missing_ok=True)
    except OSError:
        pass


def _launchd_unsupported_marker_exists() -> bool:
    return _launchd_unsupported_marker_path().exists()


def _gateway_run_command() -> list[str]:
    """Build the `python -m hermes_cli.main [--profile X] gateway run --replace` argv.

    Profile-aware: honors the active HERMES_HOME via `_profile_arg()` so the
    detached fallback launches into the same profile as the CLI invocation.
    """
    cmd = [get_python_path(), "-m", "hermes_cli.main"]
    profile_arg = _profile_arg()
    if profile_arg:
        cmd.extend(profile_arg.split())
    cmd.extend(["gateway", "run", "--replace"])
    return cmd


def _spawn_detached_gateway() -> bool:
    """Launch the gateway as a detached background process (launchd fallback).

    Used when launchctl can no longer bootstrap/kickstart the gateway on
    macOS 26+ (issue #23387). Mirrors the `nohup hermes gateway run --replace`
    workaround but keeps it CLI-managed: stdout/stderr go to the profile's
    gateway logs and the PID is tracked via the gateway.pid file that
    `run_gateway` writes, so stop/status/restart keep working.
    """
    from hermes_cli._subprocess_compat import windows_detach_popen_kwargs

    log_dir = get_hermes_home() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    out_path = log_dir / "gateway.log"
    err_path = log_dir / "gateway.error.log"
    try:
        out = open(out_path, "ab")
        err = open(err_path, "ab")
    except OSError:
        return False
    try:
        with out, err:
            subprocess.Popen(
                _gateway_run_command(),
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                **windows_detach_popen_kwargs(),
            )
    except OSError:
        return False
    return True


def _launchd_fallback_to_detached(reason: str, *, exit_on_failure: bool = True) -> bool:
    """Start the gateway detached when launchd can't manage it, with guidance.

    Returns True if the detached gateway was launched. When it can't be
    launched, prints the manual workaround and (by default) exits non-zero so
    the failure surfaces instead of silently doing nothing.
    """
    from hermes_constants import display_hermes_home as _dhh

    _write_launchd_unsupported_marker()
    print(f"⚠ launchd cannot manage the gateway on this macOS version ({reason}).")
    if _spawn_detached_gateway():
        print("✓ Started gateway as a background process instead")
        print("  It will NOT auto-start at login or auto-restart on crash.")
        print(f"  Logs: {_dhh()}/logs/gateway.log")
        print("  Stop it with: hermes gateway stop")
        return True
    print_error("Failed to start the gateway as a background process.")
    print(
        f"  Try manually: nohup hermes gateway run --replace "
        f"> {_dhh()}/logs/gateway.log 2>&1 &"
    )
    if exit_on_failure:
        sys.exit(1)
    return False


def generate_launchd_plist() -> str:
    python_path = get_python_path()
    # Stable cwd anchor — never the volatile source checkout. See
    # _stable_service_working_dir() for the rationale (same rot risk applies
    # to launchd's WorkingDirectory as to systemd's).
    working_dir = _stable_service_working_dir()
    hermes_home = str(get_hermes_home().resolve())
    log_dir = get_hermes_home() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    label = get_launchd_label()
    profile_arg = _profile_arg(hermes_home)
    # Build a sane PATH for the launchd plist.  launchd provides only a
    # minimal default (/usr/bin:/bin:/usr/sbin:/sbin) which misses Homebrew,
    # nvm, cargo, etc.  We prepend venv/bin and node_modules/.bin (matching
    # the systemd unit), then capture the user's full shell PATH so every
    # user-installed tool (node, ffmpeg, …) is reachable.
    detected_venv = _detect_venv_dir()
    venv_dir = str(detected_venv) if detected_venv else str(PROJECT_ROOT / "venv")
    # Resolve the directory containing the node binary (e.g. Homebrew, nvm)
    # so it's explicitly in PATH even if the user's shell PATH changes later.
    priority_dirs = _build_service_path_dirs()
    resolved_node = shutil.which("node")
    if resolved_node:
        # Use the directory where ``node`` is *found on PATH*, NOT the symlink's
        # resolved target. ``~/.local/bin/node`` is often a symlink into a
        # specific profile's node install; calling .resolve() would chase it and
        # bake one profile's path into every profile's service definition,
        # breaking profile isolation and causing perpetual unit rewrites. See
        # the matching fix in generate_systemd_unit().
        resolved_node_dir = str(Path(resolved_node).parent)
        if resolved_node_dir not in priority_dirs:
            priority_dirs.append(resolved_node_dir)
    sane_path = ":".join(
        dict.fromkeys(
            priority_dirs + [p for p in os.environ.get("PATH", "").split(":") if p]
        )
    )

    # Build ProgramArguments array, including --profile when using a named profile
    prog_args = [
        f"<string>{python_path}</string>",
        "<string>-m</string>",
        "<string>hermes_cli.main</string>",
    ]
    if profile_arg:
        for part in profile_arg.split():
            prog_args.append(f"<string>{part}</string>")
    prog_args.extend(
        [
            "<string>gateway</string>",
            "<string>run</string>",
            "<string>--replace</string>",
        ]
    )
    prog_args_xml = "\n        ".join(prog_args)

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>

    <key>ProgramArguments</key>
    <array>
        {prog_args_xml}
    </array>
    
    <key>WorkingDirectory</key>
    <string>{working_dir}</string>
    
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>{sane_path}</string>
        <key>VIRTUAL_ENV</key>
        <string>{venv_dir}</string>
        <key>HERMES_HOME</key>
        <string>{hermes_home}</string>
    </dict>

    <key>LimitLoadToSessionType</key>
    <array>
        <string>Aqua</string>
        <string>Background</string>
    </array>
    
    <key>RunAtLoad</key>
    <true/>
    
    <key>KeepAlive</key>
    <true/>
    
    <key>StandardOutPath</key>
    <string>{log_dir}/gateway.log</string>
    
    <key>StandardErrorPath</key>
    <string>{log_dir}/gateway.error.log</string>
</dict>
</plist>
"""


def launchd_plist_is_current() -> bool:
    """Check if the installed launchd plist matches the currently generated one."""
    plist_path = get_launchd_plist_path()
    if not plist_path.exists():
        return False

    installed = plist_path.read_text(encoding="utf-8")
    expected = generate_launchd_plist()
    return _normalize_launchd_plist_for_comparison(
        installed
    ) == _normalize_launchd_plist_for_comparison(expected)


def refresh_launchd_plist_if_needed(
    *, enable_before_reload: bool = False
) -> bool | None:
    """Rewrite the installed launchd plist when the generated definition has changed.

    Unlike systemd, launchd picks up plist changes on the next ``launchctl kill``/
    ``launchctl kickstart`` cycle — no daemon-reload is needed. We still bootout/
    bootstrap to make launchd re-read the updated plist immediately.
    """
    plist_path = get_launchd_plist_path()
    if not plist_path.exists() or launchd_plist_is_current():
        return False

    new_plist = generate_launchd_plist()
    if _refuse_temp_home_service_write(new_plist, "launchd plist"):
        return None

    plist_path.write_text(new_plist, encoding="utf-8")
    label = get_launchd_label()
    domain = _launchd_domain()
    target = f"{domain}/{label}"

    # An explicit start/install intent must clear a maintenance stop fence
    # before this refresh's bootout/bootstrap sequence.  The plist write and
    # its temp-home safety check above are deliberately completed first so a
    # precondition failure cannot mutate launchd's desired state.
    if enable_before_reload:
        _launchd_enable(domain, label)

    # If this refresh is running INSIDE the gateway's own launchd process tree
    # (e.g. the agent triggered a self-update via its terminal tool), a direct
    # `launchctl bootout` tears down the service's process group — which
    # includes THIS CLI — before the follow-up `bootstrap` can run. The gateway
    # then stays unloaded and KeepAlive can't revive it (#43842). Detect that
    # case and hand the reload to a detached session that survives the bootout.
    gateway_pid = None
    try:
        from gateway.status import get_running_pid
        gateway_pid = get_running_pid()
    except Exception:
        gateway_pid = None

    if (
        gateway_pid is not None
        and _is_pid_ancestor_of_current_process(gateway_pid)
        and hasattr(os, "setsid")  # POSIX-only; launchd is macOS so always true here
    ):
        # Delegate to a new session: `start_new_session=True` detaches the
        # helper from the gateway's process group, so the bootout that kills
        # the gateway (and us) does not kill the helper before it bootstraps.
        #
        # The bootstrap is retried up to 5 times with verification: under
        # high load (loadavg observed >= 9) or a launchd race, the bootout
        # can succeed (removing the service from launchd) while the
        # follow-up bootstrap fails silently. Without retry+verify the
        # service stays unregistered — KeepAlive can't revive a service
        # launchd no longer knows about, so the gateway stays dark until a
        # manual `launchctl bootstrap`. Failures append a timestamped line
        # to ~/.hermes/logs/launchd-reload.log, which the health watchdog
        # can tail to detect a persistent orphan. See hermes-restart
        # rootcause handoff (2026-06-26 incident).
        reload_log_path = get_hermes_home() / "logs" / "launchd-reload.log"
        try:
            reload_log_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        # Retry until launchctl LISTS the label (not merely a zero bootstrap
        # exit) or the drain window elapses. The failure happens while the old
        # gateway is still draining (default agent.restart_drain_timeout=180s),
        # so a fixed ~10s window is too short — bound by that budget instead.
        _reload_budget = int(max(30.0, _get_restart_drain_timeout()))
        reload_script = (
            f"sleep 2; "
            f"launchctl bootout {shlex.quote(target)} 2>/dev/null; "
            f"sleep 1; "
            f"_deadline=$(($(date +%s) + {_reload_budget})); "
            f"while :; do "
            f"  launchctl bootstrap {shlex.quote(domain)} {shlex.quote(str(plist_path))} 2>/dev/null; "
            f"  if launchctl list {shlex.quote(label)} >/dev/null 2>&1; then break; fi; "
            f"  echo \"[$(date '+%Y-%m-%d %H:%M:%S %z')] bootstrap not yet registered for {shlex.quote(target)} — retrying\" >> {shlex.quote(str(reload_log_path))}; "
            f"  if [ $(date +%s) -ge $_deadline ]; then break; fi; "
            f"  sleep 2; "
            f"done; "
            f"if ! launchctl list {shlex.quote(label)} >/dev/null 2>&1; then "
            f"  echo \"[$(date '+%Y-%m-%d %H:%M:%S %z')] FAILED launchd reload for {shlex.quote(target)} — service NOT registered after {_reload_budget}s of retries\" >> {shlex.quote(str(reload_log_path))}; "
            f"fi"
        )
        try:
            subprocess.Popen(
                ["/bin/bash", "-c", reload_script],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            logger.warning("Deferred launchd reload could not be spawned: %s", e)
            return False
        print(
            "↻ Updated gateway launchd service definition; reload deferred to a "
            "detached helper (refresh ran inside the gateway process tree)"
        )
        return True

    # Bootout/bootstrap so launchd picks up the new definition. The reported
    # incident (2026-06-26) happened when bootout succeeded but bootstrap
    # failed silently under load (loadavg 9.48) during a graceful /restart
    # drain, leaving the service unregistered — KeepAlive can't revive a job
    # launchd no longer knows about. Retry the bootstrap (via the shared
    # _launchctl_bootstrap EIO-recovery helper) until the label is actually
    # registered or the drain window elapses, verify with `launchctl list`,
    # and log exhaustion so the reload watchdog can detect a persistent orphan.
    subprocess.run(
        ["launchctl", "bootout", target],
        check=False,
        timeout=90,
    )
    # Size the retry window to the restart drain timeout (default 180s), not a
    # fixed ~10s: the failure mode occurs while the old gateway is still
    # draining, so a short window can exhaust before launchd settles.
    _reload_budget = max(30.0, _get_restart_drain_timeout())
    _deadline = time.monotonic() + _reload_budget
    if not _retry_launchctl_bootstrap_until_registered(
        domain, plist_path, label, deadline=_deadline
    ):
        _append_launchd_reload_log(
            f"FAILED launchd reload of {target} — service NOT registered after "
            f"retrying for {int(_reload_budget)}s (refresh ran outside gateway "
            f"process tree)"
        )
        logger.error(
            "launchd reload of %s failed — service not registered after %ds of "
            "retries; see %s",
            target,
            int(_reload_budget),
            _launchd_reload_log_path(),
        )
    print(
        "↻ Updated gateway launchd service definition to match the current Hermes install"
    )
    return True


def launchd_install(force: bool = False):
    plist_path = get_launchd_plist_path()
    domain = _launchd_domain()
    label = get_launchd_label()

    if plist_path.exists() and not force:
        if not launchd_plist_is_current():
            print(f"↻ Repairing outdated launchd service at: {plist_path}")
            try:
                refreshed = refresh_launchd_plist_if_needed(enable_before_reload=True)
            except subprocess.CalledProcessError as e:
                if not _launchctl_domain_unsupported(e.returncode):
                    raise
                _launchd_fallback_to_detached(f"launchctl exit {e.returncode}")
                return
            if refreshed is None:
                return
            print("✓ Service definition updated")
            return
        _launchd_enable(domain, label)
        print(f"Service already installed at: {plist_path}")
        print("Use --force to reinstall")
        return

    plist_path.parent.mkdir(parents=True, exist_ok=True)
    new_plist = generate_launchd_plist()
    if _refuse_temp_home_service_write(new_plist, "launchd plist"):
        return
    print(f"Installing launchd service to: {plist_path}")
    plist_path.write_text(new_plist)

    try:
        _launchd_enable(domain, label)
        _launchctl_bootstrap(domain, plist_path, label, timeout=30)
    except subprocess.CalledProcessError as e:
        if not _launchctl_domain_unsupported(e.returncode):
            raise
        _launchd_fallback_to_detached(f"launchctl bootstrap exit {e.returncode}")
        return

    print()
    print("✓ Service installed and loaded!")
    _clear_launchd_unsupported_marker()
    print()
    print("Next steps:")
    print("  hermes gateway status             # Check status")
    from hermes_constants import display_hermes_home as _dhh

    print(f"  tail -f {_dhh()}/logs/gateway.log  # View logs")


def launchd_uninstall():
    plist_path = get_launchd_plist_path()
    label = get_launchd_label()
    subprocess.run(
        ["launchctl", "bootout", f"{_launchd_domain()}/{label}"],
        check=False,
        timeout=90,
    )

    if plist_path.exists():
        plist_path.unlink()
        print(f"✓ Removed {plist_path}")

    print("✓ Service uninstalled")


def launchd_start():
    plist_path = get_launchd_plist_path()
    label = get_launchd_label()
    domain = _launchd_domain()
    target = f"{domain}/{label}"

    # Self-heal if the plist is missing entirely (e.g., manual cleanup, failed upgrade)
    if not plist_path.exists():
        new_plist = generate_launchd_plist()
        if _refuse_temp_home_service_write(new_plist, "launchd plist"):
            sys.exit(1)
        print("↻ launchd plist missing; regenerating service definition")
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        plist_path.write_text(new_plist, encoding="utf-8")
        try:
            _launchd_enable(domain, label)
            _launchctl_bootstrap(domain, plist_path, label, timeout=30)
            subprocess.run(
                ["launchctl", "kickstart", target],
                check=True,
                timeout=30,
            )
        except subprocess.CalledProcessError as e:
            if not _launchctl_domain_unsupported(e.returncode):
                raise
            _launchd_fallback_to_detached(f"launchctl exit {e.returncode}")
            return
        print("✓ Service started")
        _clear_launchd_unsupported_marker()
        return

    try:
        refreshed = refresh_launchd_plist_if_needed(enable_before_reload=True)
        if refreshed is None:
            sys.exit(1)
        if not refreshed:
            _launchd_enable(domain, label)
    except subprocess.CalledProcessError as e:
        if not _launchctl_domain_unsupported(e.returncode):
            raise
        _launchd_fallback_to_detached(f"launchctl exit {e.returncode}")
        return
    try:
        subprocess.run(
            ["launchctl", "kickstart", target],
            check=True,
            timeout=30,
        )
    except subprocess.CalledProcessError as e:
        if not _launchd_error_indicates_unloaded(e):
            raise
        # Job not loaded in this domain — re-bootstrap the plist and retry.
        print("↻ launchd job was unloaded; reloading service definition")
        try:
            _launchctl_bootstrap(domain, plist_path, label, timeout=30)
            subprocess.run(
                ["launchctl", "kickstart", target],
                check=True,
                timeout=30,
            )
        except subprocess.CalledProcessError as e2:
            # Even a fresh bootstrap can't manage the domain on this host —
            # degrade to a detached background process (issue #23387).
            if not _launchctl_domain_unsupported(e2.returncode):
                raise
            _launchd_fallback_to_detached(f"launchctl exit {e2.returncode}")
            return
    print("✓ Service started")
    _clear_launchd_unsupported_marker()


def launchd_stop():
    label = get_launchd_label()
    domain = _launchd_domain()
    fenced = _launchd_stop_target(label, domain, record_planned_stop=True, wait=True)
    if fenced:
        print("✓ Service stopped")
    else:
        print_warning(
            f"Gateway process stopped, but {domain}/{label} could not be persistently fenced"
        )
        print("  Run `hermes gateway status` to inspect launchd support.")


def _launchd_stop_target(
    label: str,
    domain: str,
    *,
    record_planned_stop: bool,
    wait: bool,
) -> bool:
    """Fence and unload one exact launchd gateway target."""
    lifecycle_target = _launchd_single_preflight_target(
        label,
        get_launchd_plist_path(),
    )
    domain = lifecycle_target.domain
    expected_identity = lifecycle_target.runtime_identity
    if record_planned_stop and expected_identity is not None:
        try:
            from gateway.status import write_planned_stop_marker

            write_planned_stop_marker(expected_identity.pid)
        except Exception:
            pass

    try:
        fenced = _launchd_disable(domain, label)
    except subprocess.CalledProcessError as exc:
        fenced = False
        # The cached/current domain can become stale across an Aqua login
        # transition. Re-probe this exact label and retry only a positively
        # registered alternate domain; never treat exit 5/125 as success.
        for actual_domain in _probe_launchd_label_domains(label).registered:
            if actual_domain == domain:
                continue
            try:
                fenced = _launchd_disable(actual_domain, label)
            except subprocess.CalledProcessError:
                continue
            domain = actual_domain
            break
        if not fenced:
            raise LaunchdFenceError(
                f"Could not persist the launchd stop fence for {domain}/{label} "
                f"(launchd exit {exc.returncode}); bootout was skipped"
            ) from exc
    if not fenced:
        # Do not bootout after a failed disable: the exact ordering is the
        # maintenance fence.  The existing PID wait still handles detached
        # fallback processes, while the warning above makes the limitation
        # visible to the caller.
        if wait:
            _wait_for_launchd_target_exit(expected_identity, timeout=10.0)
        return False

    target = f"{domain}/{label}"
    # bootout unloads the service definition so KeepAlive doesn't respawn
    # the process.  A plain `kill SIGTERM` only signals the process — launchd
    # immediately restarts it because KeepAlive is unconditionally true.
    # `hermes gateway start` re-bootstraps when it detects the job is unloaded.
    try:
        subprocess.run(["launchctl", "bootout", target], check=True, timeout=90)
    except subprocess.CalledProcessError as e:
        # Job already unloaded (3/113/125), or the domain can't be managed at
        # all (5/125, macOS 26+ detached-fallback process, issue #23387) — in
        # both cases just fall through to the PID-based kill below.
        if _launchd_error_indicates_unloaded(e) or _launchctl_domain_unsupported(
            e.returncode
        ):
            pass
        else:
            raise
    if wait:
        _wait_for_launchd_target_exit(expected_identity, timeout=10.0)
    return True


def _launchd_all_probe_details(probe: LaunchdLabelProbe) -> str:
    return "; ".join(
        probe.unknown_details
        + probe.disabled_unknown_details
        + tuple(probe.unknown)
        + tuple(probe.disabled_unknown)
    )


def _launchd_stop_barrier_is_valid(
    before: LaunchdLabelProbe,
    after: LaunchdLabelProbe,
    domains: tuple[str, ...],
) -> bool:
    """Return whether one label still satisfies the immutable stop plan."""
    return (
        not after.unknown
        and not after.disabled_unknown
        and set(after.disabled) == set(domains)
        and len(after.registered) <= 1
        and after.registered == before.registered
    )


def _launchd_stop_barrier_failures(
    label: str,
    before: LaunchdLabelProbe,
    after: LaunchdLabelProbe,
    domains: tuple[str, ...],
) -> list[str]:
    failures: list[str] = []
    if after.unknown or after.disabled_unknown:
        failures.append(
            f"{label} post-fence state is unknown: "
            f"{_launchd_all_probe_details(after)}"
        )
    if set(after.disabled) != set(domains):
        failures.append(
            f"{label} post-fence disabled state is {list(after.disabled)}, "
            f"expected {list(domains)}"
        )
    if after.registered != before.registered:
        failures.append(
            f"{label} post-fence registration moved from "
            f"{before.registered or 'none'} to {after.registered or 'none'}"
        )
    if len(after.registered) > 1:
        failures.append(
            f"{label} post-fence registration conflict in {after.registered}"
        )
    return failures


def _launchd_stop_commit_snapshot(
    label: str,
    barrier: _LaunchdAllStateSnapshot,
    probe: LaunchdLabelProbe,
    *,
    expected_runtime_identity: GatewayProcessIdentity | None = None,
    plist_argv: Iterable[str] | None = None,
    hermes_home: Path | None = None,
    profile: str | None = None,
) -> _LaunchdAllStateSnapshot:
    """Build the commit attestation for one mutation-adjacent bootout."""
    _launchd_all_require_known_probe(label, probe)
    runtime_identity = expected_runtime_identity
    if snapshot_pid := (
        probe.pid_for(probe.registered[0]) if len(probe.registered) == 1 else None
    ):
        if runtime_identity is None:
            raise LaunchdAllOperationError(
                f"{label}: commit PID {snapshot_pid} has no retained runtime identity"
            )
        try:
            if plist_argv is not None and hermes_home is not None:
                current_identity = _attest_launchd_runtime_identity(
                    snapshot_pid,
                    _launchd_process_start_time(snapshot_pid),
                    plist_argv=plist_argv,
                    hermes_home=hermes_home,
                    profile=profile,
                )
            else:
                current_identity, _process = _read_live_gateway_process_identity(
                    snapshot_pid
                )
        except (GatewayProcessEnumerationError, PermissionError, ProcessLookupError) as exc:
            raise LaunchdAllOperationError(
                f"{label}: commit runtime identity is unreadable or vanished"
            ) from exc
        if current_identity.identity_key() != runtime_identity.identity_key():
            raise LaunchdAllOperationError(
                f"{label}: commit runtime argv/profile/home identity changed"
            )
        runtime_identity = current_identity
    snapshot = _launchd_all_snapshot_from_probe(
        probe, runtime_identity=runtime_identity
    )
    if snapshot.pid is not None and snapshot.start_time is None:
        raise LaunchdAllOperationError(
            f"{label}: commit PID {snapshot.pid} has no birth identity"
        )
    if set(snapshot.disabled) != set(barrier.disabled):
        raise LaunchdAllOperationError(
            f"{label}: commit disabled state changed from "
            f"{list(barrier.disabled)} to {list(snapshot.disabled)}"
        )
    if not _launchd_all_snapshot_matches(barrier, snapshot):
        raise LaunchdAllOperationError(
            f"{label}: commit runtime identity changed from "
            f"{(barrier.pid, barrier.start_time)} to "
            f"{(snapshot.pid, snapshot.start_time)}"
        )
    return snapshot


def _launchd_stop_all_locked() -> LaunchdStopAllResult:
    """Fence every validated Hermes LaunchAgent before a global PID sweep.

    Inventory validation and all desired-state writes happen before any
    ``bootout``.  Each label is probed independently across ``gui/<uid>`` and
    ``user/<uid>``; the current caller's cached domain is never reused.
    """
    inventory = _installed_launchd_gateway_plists()
    domains = _launchd_candidate_domains()
    planned: list[dict] = []
    preflight_failures: list[str] = []

    # Immutable preflight: validate every plist identity and every label state
    # before the first desired-state write for any label.
    for label, plist_path in inventory:
        try:
            identity = _read_launchd_all_plist_identity(plist_path)
        except LaunchdAllOperationError as exc:
            preflight_failures.append(f"{label}: {exc}")
            continue
        probe = _probe_launchd_label_domains(label)
        if probe.unknown or probe.disabled_unknown:
            preflight_failures.append(
                f"{label}: could not inspect launchd state "
                f"({_launchd_all_probe_details(probe)})"
            )
            continue
        if len(probe.registered) > 1:
            preflight_failures.append(
                f"{label}: registered in both {probe.registered[0]} and "
                f"{probe.registered[1]}"
            )
            continue
        if probe.registered:
            registered_domain = probe.registered[0]
            if registered_domain in probe.disabled:
                # The registered domain is the authority for an existing
                # target.  Its own fence suppresses restore; a disabled peer
                # does not.
                restore_domains = ()
            else:
                restore_domains = probe.registered
        elif probe.disabled:
            # An unloaded target has no authoritative domain.  Preserve any
            # pre-disabled bit rather than inventing a bootstrap domain.
            restore_domains = ()
        else:
            restore_domains = (_launchd_validated_manager_domain(),)
        runtime_identity = None
        if len(probe.registered) == 1:
            registered_domain = probe.registered[0]
            registered_pid = probe.pid_for(registered_domain)
            if registered_pid is not None:
                runtime_identity = _attest_launchd_runtime_identity(
                    registered_pid,
                    _launchd_process_start_time(registered_pid),
                    plist_argv=identity.argv,
                    hermes_home=identity.hermes_home,
                    profile=identity.profile,
                )
        planned.append(
            {
                "label": label,
                "plist_path": plist_path,
                "identity": identity,
                "probe": probe,
                "preflight_snapshot": _launchd_all_snapshot_from_probe(
                    probe, runtime_identity=runtime_identity
                ),
                "restore_domains": restore_domains,
                "changed_domains": tuple(
                    domain for domain in domains if domain not in probe.disabled
                ),
                "runtime_identity": runtime_identity,
            }
        )

    if preflight_failures:
        raise LaunchdInventoryError(
            "Refusing cross-profile launchd operation: "
            + "; ".join(preflight_failures)
        )

    write_failures: dict[str, list[str]] = {plan["label"]: [] for plan in planned}

    # Phase 1: every label/domain receives a disable intent. There are no
    # post-write probes in this phase, so no label can be booted out early.
    for plan in planned:
        label = plan["label"]
        for domain in domains:
            try:
                _launchd_disable(domain, label)
            except _LAUNCHD_ALL_OPERATIONAL_ERRORS as exc:
                write_failures[label].append(
                    f"{domain}/{label} disable ({_launchd_all_failure_detail(exc)})"
                )

    # Phase 2: probe every label only after all writes completed. This is the
    # global barrier that closes a concurrent-enable race on an earlier label.
    barrier: dict[str, LaunchdLabelProbe] = {}
    barrier_snapshots: dict[str, _LaunchdAllStateSnapshot] = {}
    post_write: dict[str, LaunchdLabelProbe | None] = {}
    failures: list[str] = []
    for plan in planned:
        label = plan["label"]
        try:
            after = _probe_launchd_label_domains(label)
        except _LAUNCHD_ALL_OPERATIONAL_ERRORS as exc:
            after = None
            failures.extend(write_failures[label])
            failures.append(
                f"{label} post-fence probe ({_launchd_all_failure_detail(exc)})"
            )
        post_write[label] = after
        if after is not None:
            # A valid desired-state barrier is still unsafe when one disable
            # command failed.  The later probe may show the desired bit due to
            # another actor, but the transaction did not durably establish it.
            failures.extend(write_failures[label])
            valid = _launchd_stop_barrier_is_valid(plan["probe"], after, domains)
            if valid:
                barrier[label] = after
                try:
                    barrier_snapshots[label] = _launchd_stop_commit_snapshot(
                        label,
                        _launchd_all_snapshot_from_probe(
                            after, runtime_identity=plan["runtime_identity"]
                        ),
                        after,
                        expected_runtime_identity=plan["runtime_identity"],
                        plist_argv=plan["identity"].argv,
                        hermes_home=plan["identity"].hermes_home,
                        profile=plan["identity"].profile,
                    )
                except _LAUNCHD_ALL_OPERATIONAL_ERRORS as exc:
                    failures.append(
                        f"{label} post-fence identity barrier "
                        f"({_launchd_all_failure_detail(exc)})"
                    )
            else:
                failures.extend(
                    _launchd_stop_barrier_failures(
                        label, plan["probe"], after, domains
                    )
                )

    fenced: list[LaunchdFencedGateway] = []
    for plan in planned:
        label = plan["label"]
        after = post_write[label]
        fenced.append(
            LaunchdFencedGateway(
                label=label,
                plist_path=plan["plist_path"],
                fenced_domains=(
                    tuple(after.disabled)
                    if after is not None
                    else tuple(plan["probe"].disabled)
                ),
                restore_domains=plan["restore_domains"],
                changed_domains=plan["changed_domains"],
                plist_fingerprint=plan["identity"].fingerprint,
                plist_stat_identity=plan["identity"].stat_identity,
                hermes_home=plan["identity"].hermes_home,
                preflight_snapshot=plan["preflight_snapshot"],
                post_mutation_snapshot=(
                    _launchd_all_snapshot_from_probe(
                        after,
                        runtime_identity=(
                            barrier_snapshots[label].runtime_identity
                            if label in barrier_snapshots
                            else plan["runtime_identity"]
                        ),
                    )
                    if after is not None
                    else None
                ),
                plist_argv=plan["identity"].argv,
                plist_profile=plan["identity"].profile,
            )
        )

    # No exact bootout is allowed until every installed label has a known,
    # dual-domain fence and unchanged unambiguous registration.
    if (
        failures
        or len(barrier) != len(planned)
        or len(barrier_snapshots) != len(planned)
    ):
        known_labels = set(barrier)
        failures.extend(
            f"{plan['label']} has no verified post-fence barrier"
            for plan in planned
            if plan["label"] not in known_labels
            and not any(plan["label"] in failure for failure in failures)
        )
        return LaunchdStopAllResult(
            tuple(fenced), tuple(dict.fromkeys(failures)), False,
            sum(bool(plan["probe"].registered) for plan in planned),
        )

    # Phase 3: commit each bootout only after a fresh exact plist/state
    # attestation.  A divergence aborts this and every remaining bootout.
    for plan in planned:
        label = plan["label"]
        try:
            identity = _read_launchd_all_plist_identity(plan["plist_path"])
            expected_identity = plan["identity"]
            if (
                identity.label != expected_identity.label
                or identity.fingerprint != expected_identity.fingerprint
                or identity.stat_identity != expected_identity.stat_identity
                or identity.hermes_home != expected_identity.hermes_home
            ):
                raise LaunchdAllOperationError(
                    f"{label} plist changed immediately before bootout"
                )
            # This probe is intentionally the last launchd observation before
            # the exact bootout command for this label.
            commit = _launchd_stop_commit_snapshot(
                label,
                barrier_snapshots[label],
                _probe_launchd_label_domains(label),
                expected_runtime_identity=barrier_snapshots[label].runtime_identity,
                plist_argv=expected_identity.argv,
                hermes_home=expected_identity.hermes_home,
                profile=expected_identity.profile,
            )
            if commit.registered != barrier_snapshots[label].registered:
                raise LaunchdAllOperationError(
                    f"{label} commit registration domain changed"
                )
        except _LAUNCHD_ALL_OPERATIONAL_ERRORS as exc:
            failures.append(
                f"{label} commit revalidation "
                f"({_launchd_all_failure_detail(exc)})"
            )
            break
        for domain in barrier[label].registered:
            try:
                subprocess.run(
                    ["launchctl", "bootout", f"{domain}/{label}"],
                    check=True,
                    timeout=90,
                )
            except _LAUNCHD_ALL_OPERATIONAL_ERRORS:
                # The final exact probe decides whether a nonzero result was
                # benign (already absent) or left an unsafe registration.
                pass

    # A commit divergence aborts before the current and remaining bootouts.
    if failures:
        return LaunchdStopAllResult(
            tuple(fenced), tuple(dict.fromkeys(failures)), False,
            sum(bool(plan["probe"].registered) for plan in planned),
        )

    # Phase 4: post-verify every label after all bootouts.
    final_failures: list[str] = []
    for plan in planned:
        label = plan["label"]
        probe = _probe_launchd_label_domains(label)
        if probe.unknown or probe.disabled_unknown:
            final_failures.append(
                f"{label} post-bootout state is unknown: "
                f"{_launchd_all_probe_details(probe)}"
            )
            continue
        if set(probe.disabled) != set(domains):
            final_failures.append(
                f"{label} post-bootout disabled state is {list(probe.disabled)}, "
                f"expected {list(domains)}"
            )
        if probe.registered:
            final_failures.append(
                f"{label} remains registered in {probe.registered} after bootout"
            )

    failures.extend(final_failures)
    if failures:
        return LaunchdStopAllResult(
            tuple(fenced), tuple(dict.fromkeys(failures)), False,
            sum(bool(plan["probe"].registered) for plan in planned),
        )
    return LaunchdStopAllResult(
        tuple(fenced), (), True,
        sum(bool(plan["probe"].registered) for plan in planned),
    )


def launchd_stop_all() -> LaunchdStopAllResult:
    with all_profile_lifecycle_lock(timeout=30.0):
        return _launchd_stop_all_locked()


def _launchd_all_revalidate_fenced_plist(target: LaunchdFencedGateway) -> None:
    """Re-attest the exact plist identity retained by a stop transaction."""
    if not target.plist_fingerprint or not target.plist_stat_identity:
        raise LaunchdAllOperationError(
            f"{target.label}: restore plist has no retained secure identity"
        )
    identity = _read_launchd_all_plist_identity(target.plist_path)
    if (
        identity.label != target.label
        or identity.fingerprint != target.plist_fingerprint
        or identity.stat_identity != target.plist_stat_identity
        or identity.hermes_home != target.hermes_home
        or (target.plist_argv and identity.argv != target.plist_argv)
        or (target.plist_argv and identity.profile != target.plist_profile)
    ):
        raise LaunchdAllOperationError(
            f"{target.label}: restore plist identity changed before bootstrap"
        )


def _launchd_restore_all_locked(
    result: LaunchdStopAllResult,
) -> tuple[int, list[str]]:
    """Restore only the exact domains recorded by ``launchd_stop_all``.

    ``restore_domains`` is transaction evidence, not a hint.  In particular,
    an empty tuple means the target was intentionally pre-disabled: an
    unexpected registration is reported, but no enable/bootstrap/kickstart is
    ever attempted to revive it.
    """
    restored = 0
    failures: list[str] = []
    for target in result.fenced:
        label_errors: list[str] = []
        try:
            # The retained plist is the authority for every later launchd
            # mutation.  Reattest before clearing even one desired-state bit.
            _launchd_all_revalidate_fenced_plist(target)
            probe = _probe_launchd_label_domains(target.label)
            _launchd_all_require_known_probe(target.label, probe)
            if len(probe.registered) > 1:
                raise LaunchdAllOperationError(
                    f"{target.label} restore registration conflict in {probe.registered}"
                )
            if len(target.restore_domains) > 1:
                raise LaunchdAllOperationError(
                    f"{target.label} restore has multiple recorded domains: "
                    f"{target.restore_domains}"
                )
        except _LAUNCHD_ALL_OPERATIONAL_ERRORS as exc:
            label_errors.append(
                f"{target.label} restore preflight ({_launchd_all_failure_detail(exc)})"
            )
            failures.extend(label_errors)
            continue

        restore_domain = target.restore_domains[0] if target.restore_domains else None
        if restore_domain is None:
            # A pre-disabled target has a desired-state fence that is part of
            # its identity.  Only the transaction snapshot can define the
            # expected fence; an old compatibility-shaped result has no such
            # evidence and therefore receives no mutation.
            expected_fence = (
                target.post_mutation_snapshot.disabled
                if target.post_mutation_snapshot is not None
                else ()
            )
            if probe.registered:
                label_errors.append(
                    f"{target.label} restore found unexpected registration "
                    f"in {probe.registered}; pre-disabled target was not revived"
                )
            elif expected_fence and set(probe.disabled) != set(expected_fence):
                label_errors.append(
                    f"{target.label} restore changed its pre-disabled fence from "
                    f"{list(expected_fence)} to {list(probe.disabled)}"
                )
            elif target.post_mutation_snapshot is not None:
                # Restore only bits this stop transaction fenced.  The
                # registered/pre-disabled domain remains disabled and no
                # domain is bootstrapped; an enabled peer is merely returned
                # to its pre-stop desired state.
                expected_state = _LaunchdAllStateSnapshot(
                    registered=(),
                    disabled=tuple(sorted(set(expected_fence))),
                    pid=None,
                    start_time=None,
                )
                try:
                    current = _launchd_all_capture_label_snapshot(target.label, target)
                    if not _launchd_all_snapshot_matches(expected_state, current):
                        raise LaunchdAllOperationError(
                            f"{target.label} restore state diverged before fence release"
                        )
                    for domain in target.changed_domains:
                        if domain not in expected_state.disabled:
                            continue
                        _launchd_all_revalidate_fenced_plist(target)
                        current = _launchd_all_capture_label_snapshot(target.label, target)
                        if not _launchd_all_snapshot_matches(expected_state, current):
                            raise LaunchdAllOperationError(
                                f"{target.label} restore state diverged before "
                                f"enabling {domain}/{target.label}"
                            )
                        _launchd_enable(domain, target.label)
                        expected_state = _LaunchdAllStateSnapshot(
                            registered=expected_state.registered,
                            disabled=tuple(
                                sorted(set(expected_state.disabled) - {domain})
                            ),
                            pid=None,
                            start_time=None,
                        )
                        current = _launchd_all_capture_label_snapshot(target.label, target)
                        if not _launchd_all_snapshot_matches(expected_state, current):
                            raise LaunchdAllOperationError(
                                f"{target.label} restore state diverged after "
                                f"enabling {domain}/{target.label}"
                            )
                    remaining_owned = set(target.changed_domains) & set(
                        expected_state.disabled
                    )
                    if remaining_owned:
                        raise LaunchdAllOperationError(
                            f"{target.label} restore left transaction-owned domains "
                            f"disabled: {sorted(remaining_owned)}"
                        )
                except _LAUNCHD_ALL_OPERATIONAL_ERRORS as exc:
                    label_errors.append(
                        f"{target.label} restore fence release "
                        f"({_launchd_all_failure_detail(exc)})"
                    )
            if label_errors:
                failures.extend(label_errors)
                continue
            restored += 1
            continue

        # The recorded domain is the only domain that may contain or receive
        # this target.  Do not replace it with the domain observed above.
        if restore_domain not in _launchd_candidate_domains():
            failures.append(
                f"{target.label} recorded restore domain is not a user domain: "
                f"{restore_domain}"
            )
            continue
        if probe.registered:
            if probe.registered != (restore_domain,):
                failures.append(
                    f"{target.label} restore registration is {probe.registered}; "
                    f"expected exactly {(restore_domain,)}"
                )
                continue
            pid = probe.pid_for(restore_domain)
            if pid is None:
                failures.append(
                    f"{restore_domain}/{target.label} restore has no live PID"
                )
                continue
            try:
                start_time = _launchd_process_start_time(pid)
                if start_time is None or not _launchd_pid_is_live(pid, start_time):
                    raise LaunchdAllOperationError(
                        f"{restore_domain}/{target.label} restore has no live PID"
                    )
                _attest_launchd_runtime_identity(
                    pid,
                    start_time,
                    plist_argv=target.plist_argv,
                    hermes_home=target.hermes_home,
                    profile=target.plist_profile,
                )
                restored += 1
            except _LAUNCHD_ALL_OPERATIONAL_ERRORS as exc:
                failures.append(
                    f"{restore_domain}/{target.label} restore "
                    f"({_launchd_all_failure_detail(exc)})"
                )
            continue

        try:
            # Only transaction-owned desired-state bits may be cleared.  A
            # concurrent registration is rejected before any enable/bootstrap.
            for domain in target.changed_domains:
                if domain not in probe.disabled:
                    continue
                _launchd_all_revalidate_fenced_plist(target)
                current = _probe_launchd_label_domains(target.label)
                _launchd_all_require_known_probe(target.label, current)
                if current.registered:
                    raise LaunchdAllOperationError(
                        f"{target.label} restore registration appeared in "
                        f"{current.registered}"
                    )
                _launchd_enable(domain, target.label)
                probe = _probe_launchd_label_domains(target.label)
                _launchd_all_require_known_probe(target.label, probe)
            if probe.registered:
                raise LaunchdAllOperationError(
                    f"{target.label} restore registration appeared in "
                    f"{probe.registered}"
                )
            remaining_owned = set(target.changed_domains) & set(probe.disabled)
            if remaining_owned:
                raise LaunchdAllOperationError(
                    f"{target.label} restore left transaction-owned domains disabled: "
                    f"{sorted(remaining_owned)}"
                )
            _launchd_all_revalidate_fenced_plist(target)
            _launchctl_bootstrap(
                restore_domain, target.plist_path, target.label, timeout=30
            )
            probe = _probe_launchd_label_domains(target.label)
            _launchd_all_require_known_probe(target.label, probe)
            if probe.registered != (restore_domain,):
                raise LaunchdAllOperationError(
                    f"{target.label} restored in {probe.registered}; expected "
                    f"exactly {(restore_domain,)}"
                )
            pid = probe.pid_for(restore_domain)
            if pid is None:
                raise LaunchdAllOperationError(
                    f"{restore_domain}/{target.label} restore produced no live PID"
                )
            start_time = _launchd_process_start_time(pid)
            if start_time is None or not _launchd_pid_is_live(pid, start_time):
                raise LaunchdAllOperationError(
                    f"{restore_domain}/{target.label} restore produced no live PID"
                )
            _attest_launchd_runtime_identity(
                pid,
                start_time,
                plist_argv=target.plist_argv,
                hermes_home=target.hermes_home,
                profile=target.plist_profile,
            )
            restored += 1
        except _LAUNCHD_ALL_OPERATIONAL_ERRORS as exc:
            failures.append(
                f"{restore_domain}/{target.label} restore "
                f"({_launchd_all_failure_detail(exc)})"
            )
    return restored, failures


def launchd_restore_all(
    result: LaunchdStopAllResult,
) -> tuple[int, list[str]]:
    with all_profile_lifecycle_lock(timeout=30.0):
        return _launchd_restore_all_locked(result)


def _launchd_release_all_fences_locked(result: LaunchdStopAllResult) -> list[str]:
    """Rollback desired-state writes when restart aborts before bootout/sweep."""
    failures: list[str] = []
    for target in result.fenced:
        try:
            current = _launchd_all_capture_label_snapshot(target.label, target)
            expected = target.post_mutation_snapshot
            if expected is None:
                # Missing transaction evidence is safe only when no enable is
                # needed.  Never promote the current state into permission to
                # mutate; that would erase the very race evidence release is
                # meant to preserve.
                if target.changed_domains and (
                    set(target.changed_domains) & set(current.disabled)
                ):
                    raise LaunchdAllOperationError(
                        f"{target.label} fence release has no transaction "
                        "snapshot while an enable is required"
                    )
                if len(current.registered) > 1:
                    raise LaunchdAllOperationError(
                        f"{target.label} fence release registration conflict in "
                        f"{current.registered}"
                    )
                continue
            if not _launchd_all_snapshot_matches(expected, current):
                raise LaunchdAllOperationError(
                    f"{target.label} fence release state diverged from the "
                    "transaction snapshot; no release mutation was attempted"
                )
            if len(current.registered) > 1:
                raise LaunchdAllOperationError(
                    f"{target.label} fence release registration conflict in "
                    f"{current.registered}"
                )
        except _LAUNCHD_ALL_OPERATIONAL_ERRORS as exc:
            failures.append(
                f"{target.label} fence release preflight "
                f"({_launchd_all_failure_detail(exc)})"
            )
            continue

        expected_state = expected
        for domain in target.changed_domains:
            if domain not in expected_state.disabled:
                continue
            try:
                _launchd_all_revalidate_fenced_plist(target)
                _launchd_enable(domain, target.label)
            except _LAUNCHD_ALL_OPERATIONAL_ERRORS as exc:
                failures.append(
                    f"{domain}/{target.label} enable "
                    f"({_launchd_all_failure_detail(exc)})"
                )
                continue
            expected_state = _LaunchdAllStateSnapshot(
                registered=expected_state.registered,
                disabled=tuple(sorted(set(expected_state.disabled) - {domain})),
                pid=expected_state.pid,
                start_time=expected_state.start_time,
                runtime_identity=expected_state.runtime_identity,
            )

        # A zero exit from launchctl enable is not proof that every desired
        # state bit changed. One post-enable probe is authoritative for the
        # complete release phase.
        try:
            current = _launchd_all_capture_label_snapshot(target.label, target)
        except _LAUNCHD_ALL_OPERATIONAL_ERRORS as exc:
            failures.append(
                f"{target.label} fence release post-enable probe "
                f"({_launchd_all_failure_detail(exc)})"
            )
            continue
        if not _launchd_all_snapshot_matches(expected_state, current):
            failures.append(
                f"{target.label} fence release state diverged after enables; "
                "no further release mutation was attempted"
            )
        remaining_owned = set(target.changed_domains) & set(current.disabled)
        if remaining_owned:
            failures.append(
                f"{target.label} fence release left transaction-owned domains "
                f"disabled: {sorted(remaining_owned)}"
            )
    return failures


def launchd_release_all_fences(result: LaunchdStopAllResult) -> list[str]:
    with all_profile_lifecycle_lock(timeout=30.0):
        return _launchd_release_all_fences_locked(result)


def _coerce_launchd_stop_all_result(value) -> LaunchdStopAllResult:
    """Accept the historical tuple shape from third-party callers/tests."""
    if isinstance(value, LaunchdStopAllResult):
        return value
    stopped, failures = value
    return LaunchdStopAllResult((), tuple(failures), not failures, stopped)


def _wait_for_gateway_exit(
    timeout: float = 10.0, force_after: float | None = 5.0
) -> bool:
    """Wait for the gateway process (by saved PID) to exit.

    Uses the PID from the gateway.pid file — not launchd labels — so this
    works correctly when multiple gateway instances run under separate
    HERMES_HOME directories.

    Args:
        timeout: Total seconds to wait before giving up.
        force_after: Seconds of graceful waiting before escalating to force-kill.
    """
    import time
    from gateway.status import get_running_pid

    pid = get_running_pid()
    if pid is None:
        return True
    try:
        identity = _capture_current_profile_gateway_identity(
            pid,
            include_restart_managers=True,
        )
    except GatewayProcessTerminationError:
        print_error(
            f"Could not attest gateway PID {pid} to the active profile; no signal was sent."
        )
        return False
    if identity is None:
        return True

    deadline = time.monotonic() + timeout
    force_deadline = (
        (time.monotonic() + force_after) if force_after is not None else None
    )
    force_sent = False

    while time.monotonic() < deadline:
        try:
            if _revalidate_gateway_process_identity(identity) is None:
                return True
        except GatewayProcessTerminationError:
            return False

        if (
            force_after is not None
            and not force_sent
            and time.monotonic() >= force_deadline
        ):
            # Grace period expired — force-kill only the retained identity.
            signal_result = _signal_gateway_process_identity(identity, signal.SIGKILL)
            if signal_result == "gone":
                return True
            if signal_result != "signalled":
                return False
            print(
                f"⚠ Gateway PID {identity.pid} did not exit gracefully; sent SIGKILL"
            )
            force_sent = True

        time.sleep(0.3)

    # Timed out even after force-kill.
    try:
        remaining = _revalidate_gateway_process_identity(identity)
    except GatewayProcessTerminationError:
        return False
    if remaining is None:
        return True
    print(
        f"⚠ Gateway PID {identity.pid} still running after {timeout}s — restart may fail"
    )
    return False


def _wait_for_launchd_target_exit(
    identity: GatewayProcessIdentity | None,
    *,
    timeout: float,
) -> bool:
    """Wait for, then force only, the exact preflighted launchd occupant."""
    if identity is None:
        return True
    graceful_timeout = min(5.0, max(0.0, timeout))
    if _wait_for_exact_gateway_identity_exit(identity, graceful_timeout):
        return True
    signal_result = _signal_gateway_process_identity(identity, signal.SIGKILL)
    if signal_result == "gone":
        return True
    if signal_result != "signalled":
        return False
    return _wait_for_exact_gateway_identity_exit(
        identity,
        max(0.0, timeout - graceful_timeout),
    )


def launchd_restart():
    label = get_launchd_label()
    plist_path = get_launchd_plist_path()
    drain_timeout = _get_restart_drain_timeout()
    try:
        lifecycle_target = _launchd_single_preflight_target(label, plist_path)
    except LaunchdAllOperationError as exc:
        print_error(str(exc))
        return
    domain = lifecycle_target.domain
    target = lifecycle_target.launchctl_target
    pid = lifecycle_target.pid
    expected_identity = lifecycle_target.runtime_identity
    enabled_before_restart = False
    if pid is not None:
        # A self-restart exits cleanly and relies on launchd KeepAlive to
        # create the replacement. Clear the exact label's maintenance fence
        # before SIGUSR1; if enable fails, do not signal the running gateway
        # and do not spawn a detached duplicate from the fallback below.
        _revalidate_launchd_target(lifecycle_target)
        _launchd_enable(domain, label)
        enabled_before_restart = True
        if expected_identity is not None and _request_gateway_self_restart(
            pid, expected_identity=expected_identity
        ):
            print("✓ Service restart requested")
            _clear_launchd_unsupported_marker()
            return

    try:
        if pid is not None:
            # Announce the drain BEFORE waiting on it. This wait can run for
            # the full drain budget (180s by default) while the old gateway
            # finishes in-flight agent runs, and it streams into surfaces with
            # no other feedback — the desktop updater's live output most of
            # all, where a silent stop here reads as "update stuck" (#44515).
            # Mirrors the systemd branch's "draining (up to Ns)..." line.
            print(
                f"→ Stopping gateway (PID {pid}) — draining in-flight runs "
                f"(up to {drain_timeout:.0f}s)..."
            )
            if expected_identity is None:
                # The PID disappeared after the initial discovery. Treat that
                # as converged and let launchd kickstart the service.
                pid = None
            else:
                signal_result = _signal_gateway_process_identity(
                    expected_identity, signal.SIGTERM
                )
                if signal_result == "gone":
                    pid = None
                elif signal_result != "signalled":
                    print_error(
                        f"Could not revalidate gateway PID {pid}; no signal was sent."
                    )
                    return
            if pid is not None and expected_identity is not None:
                exited = _wait_for_exact_gateway_identity_exit(
                    expected_identity, timeout=drain_timeout
                )
                if not exited:
                    print(
                        f"⚠ Gateway drain timed out after {drain_timeout:.0f}s — forcing launchd restart"
                    )
        # Clear a maintenance stop fence only after the drain preconditions
        # above have completed, and before any kickstart/bootstrap operation.
        if not enabled_before_restart:
            _launchd_enable(domain, label)
        subprocess.run(["launchctl", "kickstart", "-k", target], check=True, timeout=90)
        print("✓ Service restarted")
        _clear_launchd_unsupported_marker()
    except subprocess.CalledProcessError as e:
        if not _launchd_error_indicates_unloaded(e):
            # Not a "job unloaded" code. If the domain is fundamentally
            # unmanageable (error 5), degrade to detached; the old process was
            # already drained/terminated above. Otherwise re-raise.
            if _launchctl_domain_unsupported(e.returncode):
                _launchd_fallback_to_detached(f"launchctl kickstart exit {e.returncode}")
                return
            raise
        # Job not loaded — bootstrap and start fresh
        print("↻ launchd job was unloaded; reloading")
        try:
            # Restart is the one path where the job is almost always still
            # registered (we just drained it), so a plain bootstrap would hit
            # EIO on the common case. Boot the stale label out first — cheaper
            # and clearer here than routing through _launchctl_bootstrap's
            # bootstrap-first/retry-on-EIO flow. See #23387, #42914.
            subprocess.run(
                ["launchctl", "bootout", target],
                check=False,
                timeout=90,
            )
            subprocess.run(
                ["launchctl", "bootstrap", domain, str(plist_path)],
                check=True,
                timeout=30,
            )
            subprocess.run(["launchctl", "kickstart", target], check=True, timeout=30)
        except subprocess.CalledProcessError as e2:
            if not _launchctl_domain_unsupported(e2.returncode):
                raise
            _launchd_fallback_to_detached(f"launchctl exit {e2.returncode}")
            return
        print("✓ Service restarted")
        _clear_launchd_unsupported_marker()


def launchd_status(deep: bool = False):
    plist_path = get_launchd_plist_path()
    label = get_launchd_label()
    domain = _launchd_domain()
    try:
        result = subprocess.run(
            ["launchctl", "list", label],
            capture_output=True,
            text=True,
            timeout=10,
        )
        service_listed = result.returncode == 0
        list_output = result.stdout
    except subprocess.TimeoutExpired:
        service_listed = False
        list_output = ""

    # Determine whether launchd is actively supervising a process.
    # ``launchctl list`` returns exit 0 whenever the service definition is
    # registered — even when ``state = not running`` (macOS 26+ with an
    # unmanageable domain).  A PID in the output confirms a live process.
    launchd_pid = _parse_launchd_pid_from_list_output(list_output) if service_listed else None

    # Hermes PID tracking — may be a detached fallback process spawned when
    # launchd cannot manage the domain on this host.
    from gateway.status import get_running_pid
    fallback_pid = get_running_pid(cleanup_stale=False)

    # Avoid double-counting: when launchd IS supervising, fallback_pid and
    # launchd_pid point at the same process (the gateway writes both the
    # launchd PID and the Hermes PID file).
    if launchd_pid is not None and fallback_pid == launchd_pid:
        fallback_pid = None

    # Persistent marker written when launchd bootstrap/kickstart fails with
    # exit 5/125 on this host.  Lets us explain *why* launchd can't supervise
    # even when no fallback process is currently running.
    launchd_unsupported = _launchd_unsupported_marker_exists()
    launchd_disabled = _launchd_label_is_disabled(domain, label)

    # ── Report ──
    print(f"Launchd plist: {plist_path}")
    if launchd_disabled is True:
        print(f"⚠ Launchd label {domain}/{label} is disabled (maintenance stop fence is active)")
        print("  Run: hermes gateway start  # explicitly re-enable and start it")
    if launchd_plist_is_current():
        print("✓ Service definition matches the current Hermes install")
    else:
        print("⚠ Service definition is stale relative to the current Hermes install")
        print("  Run: hermes gateway start")

    if service_listed:
        if launchd_pid is not None:
            print(f"✓ Gateway is supervised by launchd (PID {launchd_pid})")
            print("  Auto-start at login and auto-restart on crash are available.")
            if launchd_unsupported:
                print("  (launchd domain was previously unavailable but is now working)")
        elif launchd_unsupported:
            print("⚠ Gateway service is registered but launchd is not supervising it")
            print("  launchd cannot manage the gateway on this macOS version.")
            if fallback_pid:
                print(f"✓ Detached fallback process is running (PID {fallback_pid})")
                print("  Cron jobs will fire. Stop with: hermes gateway stop")
            else:
                print("✗ No fallback process is running")
                print("  Run: hermes gateway start")
            print("  ⚠ Auto-start at login and auto-restart on crash are NOT available.")
        else:
            print("✓ Gateway service is registered with launchd")
            print(list_output)
            if fallback_pid:
                print(f"  Detached gateway process is running (PID {fallback_pid})")
    else:
        print("✗ Gateway service is not loaded")
        print("  Service definition exists locally but launchd has not loaded it.")
        print("  Run: hermes gateway start")
        if fallback_pid:
            print(f"  Note: a detached gateway process is running (PID {fallback_pid})")

    if deep:
        log_file = get_hermes_home() / "logs" / "gateway.log"
        if log_file.exists():
            print()
            print("Recent logs:")
            subprocess.run(["tail", "-20", str(log_file)], timeout=10)


# =============================================================================
# Gateway Runner
# =============================================================================


def _truthy_env(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _is_official_docker_checkout() -> bool:
    return (
        str(PROJECT_ROOT) == "/opt/hermes"
        and (PROJECT_ROOT / "docker" / "entrypoint.sh").is_file()
    )


def _running_under_gateway_supervisor() -> bool:
    """Return True when this process IS the gateway a service manager launched.

    The conflict guard below must never fire on the service's own startup, or
    it would wedge the unit into a respawn/refuse loop. Each supervisor exports
    a reliable marker into the child's environment:

      - systemd sets ``INVOCATION_ID`` for every unit it launches (the same
        marker ``gateway/run.py`` already uses to pick the restart path).
      - launchd sets ``XPC_SERVICE_NAME`` to the job label for jobs it spawns;
        interactive shells inherit the sentinel ``"0"`` instead.
      - the s6-overlay container longrun exports ``HERMES_S6_SUPERVISED_CHILD``.
    """
    if os.environ.get("INVOCATION_ID"):
        return True
    if os.environ.get("HERMES_S6_SUPERVISED_CHILD"):
        return True
    xpc_service = os.environ.get("XPC_SERVICE_NAME", "")
    if xpc_service and xpc_service != "0":
        return True
    return False


def _guard_named_profile_under_multiplexer(force: bool = False) -> None:
    """Refuse a named-profile gateway when a multiplexer is already serving it.

    When the default profile's gateway runs with gateway.multiplex_profiles=on,
    it is the sole inbound process for EVERY profile on the host. Starting a
    separate gateway for a named profile would double-bind that profile's
    platforms (two pollers on one bot token, port fights). In that mode a
    named-profile ``hermes gateway run`` is always a misconfiguration, so we
    hard-error with a pointer to the multiplexer. ``--force`` overrides.

    Inert unless ALL of: (a) this invocation is a named profile, (b) a default-
    profile gateway is running, (c) that gateway's config has multiplexing on.
    """
    if force:
        return
    # (a) Are we a named profile? Default/custom-hash homes return "".
    try:
        suffix = _profile_suffix()
    except Exception:
        return
    if not suffix:
        return  # default profile (or unrecognized) — this guard doesn't apply

    try:
        from hermes_constants import get_default_hermes_root
        default_root = get_default_hermes_root()
        # (b) Is the default-profile gateway running?
        from gateway.status import get_running_pid as _default_running_pid  # noqa
    except Exception:
        return

    try:
        import yaml as _yaml
        from gateway.status import _read_pid_record  # type: ignore

        # (b) default gateway PID file present + alive
        default_pid_path = default_root / "gateway.pid"
        rec = _read_pid_record(default_pid_path)
        if not rec:
            return
        from gateway.status import _pid_exists, _pid_from_record
        pid = _pid_from_record(rec)
        if not pid or not _pid_exists(pid):
            return

        # (c) default config has multiplexing on
        cfg_path = default_root / "config.yaml"
        if not cfg_path.exists():
            return
        with open(cfg_path, encoding="utf-8") as f:
            cfg = _yaml.safe_load(f) or {}
        multiplex = bool(
            cfg.get("multiplex_profiles")
            or (cfg.get("gateway", {}) or {}).get("multiplex_profiles")
        )
        if not multiplex:
            return
    except Exception:
        logger.debug("Multiplexer-conflict probe failed", exc_info=True)
        return

    print_error(
        f"The default gateway is running as a profile multiplexer and already "
        f"serves profile '{suffix}'."
    )
    print(
        "  When gateway.multiplex_profiles is on, the default gateway is the\n"
        "  single inbound process for every profile. Starting a separate\n"
        "  gateway for this profile would double-bind its platforms (two\n"
        "  pollers on one bot token, port conflicts).\n"
    )
    print("  Manage the multiplexer instead (from the default profile):")
    print()
    print("    hermes gateway restart")
    print()
    print("  Pass --force to start a separate profile gateway anyway (not")
    print("  recommended while the multiplexer is running).")
    sys.exit(1)


def _guard_supervised_gateway_conflict(force: bool = False) -> None:
    """Refuse a foreground gateway when a service manager already supervises one.

    Running ``hermes gateway run [--replace]`` (or the manual-restart fallback)
    from a shell on a systemd/launchd host spawns a second, long-lived
    dispatcher that escapes the service cgroup, survives
    ``systemctl restart``, and becomes a silent concurrent writer on the shared
    kanban DB — the documented root cause of multi-writer SQLite WAL corruption
    (issue #35240). Pass ``--force`` to start anyway.
    """
    if force or _running_under_gateway_supervisor():
        return
    try:
        snapshot = get_gateway_runtime_snapshot()
    except Exception:
        # Best-effort guard: a probe failure must never block a real startup.
        logger.debug("Supervised-gateway conflict probe failed", exc_info=True)
        return
    if not (snapshot.service_installed and snapshot.service_running):
        return

    print_error(
        f"A gateway is already running under {snapshot.manager} for this profile."
    )
    print(
        "  Starting another one from a shell leaves an orphan dispatcher that\n"
        "  escapes the service, survives restarts, and writes to the same kanban\n"
        "  DB concurrently — which can corrupt it. Restart the supervised gateway\n"
        "  instead:"
    )
    print()
    print("    hermes gateway restart")
    print()
    print(
        "  Pass --force to start a foreground gateway anyway (not recommended\n"
        "  while the service is running)."
    )
    sys.exit(1)


def _guard_existing_gateway_process_conflict(replace: bool = False) -> None:
    """Refuse duplicate foreground startup before importing gateway.run.

    ``gateway.run`` performs the authoritative PID/lock check, but importing it
    is expensive: it pulls in model_tools/plugin discovery first. On small
    instances, a supervisor or dashboard loop repeatedly running bare
    ``hermes gateway run`` can burn memory/CPU just to fail with "already
    running" after plugin discovery. This cheap PID-file preflight preserves the
    same user-facing contract while avoiding that startup work without scanning
    unrelated gateway processes from other HERMES_HOME roots.
    """
    if replace or _running_under_gateway_supervisor():
        return
    try:
        from gateway.status import get_running_pid

        pid = get_running_pid()
    except Exception:
        logger.debug("Existing-gateway process probe failed", exc_info=True)
        return
    if pid is None:
        return

    print_error(
        f"Another gateway instance is already running (PID {pid})."
    )
    print("  Use 'hermes gateway restart' to replace it,")
    print("  or 'hermes gateway stop' first.")
    print("  Or use 'hermes gateway run --replace' to auto-replace.")
    sys.exit(1)


def _guard_official_docker_root_gateway() -> None:
    """Refuse gateway startup when the official Docker privilege drop was bypassed."""
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return
    if _truthy_env(os.getenv("HERMES_ALLOW_ROOT_GATEWAY")):
        return
    if not _is_official_docker_checkout():
        return

    print_error(
        "Refusing to run the Hermes gateway as root inside the official Docker image."
    )
    print(
        "  The image entrypoint normally drops privileges to the 'hermes' user. "
        "If you override entrypoint in Docker Compose, include "
        "/opt/hermes/docker/entrypoint.sh before the Hermes command."
    )
    print(
        "  Running the gateway as root can leave root-owned files in "
        "$HERMES_HOME and break later non-root dashboard/gateway runs."
    )
    print(
        "  Set HERMES_ALLOW_ROOT_GATEWAY=1 only if you intentionally accept this risk."
    )
    sys.exit(1)


def run_gateway(verbose: int = 0, quiet: bool = False, replace: bool = False, force: bool = False):
    """Run the gateway in foreground.

    Args:
        verbose: Stderr log verbosity count added on top of default WARNING (0=WARNING, 1=INFO, 2+=DEBUG).
        quiet: Suppress all stderr log output.
        replace: If True, kill any existing gateway instance before starting.
                 This prevents systemd restart loops when the old process
                 hasn't fully exited yet.
        force: Skip the supervised-gateway conflict guard and start even when a
               systemd/launchd service is already supervising this profile.
    """
    _guard_official_docker_root_gateway()
    _guard_named_profile_under_multiplexer(force=force)
    _guard_supervised_gateway_conflict(force=force)
    _guard_existing_gateway_process_conflict(replace=replace)
    sys.path.insert(0, str(PROJECT_ROOT))

    # Detached Windows gateway runs must ignore console-control broadcasts
    # from sibling CLI processes, but foreground `hermes gateway run` still
    # needs to obey the banner's "Press Ctrl+C to stop" contract.
    # Service-style launchers set HERMES_GATEWAY_DETACHED=1; older wrappers
    # without the marker are handled by the non-TTY fallback.
    try:
        _stdin_is_tty = bool(sys.stdin and sys.stdin.isatty())
    except (ValueError, OSError):
        _stdin_is_tty = False
    _absorb_windows_console_controls = _windows_gateway_should_absorb_console_controls()
    if _absorb_windows_console_controls:
        try:
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            if hasattr(signal, "SIGBREAK"):
                signal.signal(signal.SIGBREAK, signal.SIG_IGN)
        except (OSError, ValueError):
            # SetConsoleCtrlHandler not available (rare on Windows) —
            # best-effort, proceed either way.
            pass
        # Python's signal module only hooks SIGINT/SIGBREAK. To also
        # absorb CTRL_CLOSE_EVENT / CTRL_LOGOFF_EVENT and any other
        # console control signals Windows may broadcast to the console
        # process group, call the native SetConsoleCtrlHandler(NULL, TRUE)
        # — this tells the kernel to IGNORE all console control events
        # for this process entirely, which is what background services
        # are supposed to do. Belt-and-braces over the Python-level
        # handlers above.
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            # BOOL SetConsoleCtrlHandler(NULL, Add)  —  Add=TRUE means
            # "install the NULL handler", which has the documented
            # effect of ignoring Ctrl+C. Called twice for defense in
            # depth: once before any Python import could have flipped
            # our disposition, once as our last word.
            kernel32.SetConsoleCtrlHandler(None, 1)
        except (OSError, AttributeError):
            pass

    # Refresh the systemd unit definition on every boot so that restart
    # settings (RestartSec, StartLimitIntervalSec, etc.) stay current even
    # when the process was respawned via exit-code-75 (stale-code or
    # /restart) rather than through `hermes gateway restart` which already
    # calls refresh_systemd_unit_if_needed().  Without this, a code update
    # that ships new unit settings won't take effect until the next manual
    # `hermes gateway start/restart` — leaving the gateway vulnerable to
    # the exact failure mode the new settings were meant to prevent.
    if supports_systemd_services():
        try:
            refresh_systemd_unit_if_needed(system=False)
        except Exception:
            pass  # best-effort; don't block gateway startup

    from gateway.run import start_gateway

    print("┌─────────────────────────────────────────────────────────┐")
    print("│           ⚕ Hermes Gateway Starting...                 │")
    print("├─────────────────────────────────────────────────────────┤")
    print("│  Messaging platforms + cron scheduler                    │")
    print("│  Press Ctrl+C to stop                                   │")
    print("└─────────────────────────────────────────────────────────┘")
    print()

    # Exit with code 1 if gateway fails to connect any platform,
    # so systemd Restart=always will retry on transient errors
    verbosity = None if quiet else verbose

    # ── Exit-path diagnostics ────────────────────────────────────────────
    # When the gateway dies silently on Windows (no shutdown log, no
    # traceback in gateway.log / errors.log), we're usually blind to the
    # cause. The code below captures *every* way the asyncio.run() call
    # below can return, with full context dumped to a dedicated log so
    # the next silent death yields evidence instead of a mystery. This
    # is diagnostic scaffolding; cheap to keep on, costs nothing during
    # normal operation, and the emitted lines are opt-in via the
    # HERMES_GATEWAY_EXIT_DIAG env var (default: on while we're still
    # chasing the Windows lifecycle bug).
    import atexit as _atexit
    import traceback as _traceback
    from datetime import datetime as _dt, timezone as _tz

    def _exit_diag(tag: str, **extra: object) -> None:
        if os.environ.get("HERMES_GATEWAY_EXIT_DIAG", "1") != "1":
            return
        try:
            from hermes_constants import get_hermes_home as _ghh

            log_dir = _ghh() / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            ts = _dt.now(_tz.utc).isoformat()
            line = {
                "ts": ts,
                "tag": tag,
                "pid": os.getpid(),
                "python": sys.version.split()[0],
                "platform": sys.platform,
                **extra,
            }
            import json as _json

            with open(log_dir / "gateway-exit-diag.log", "a", encoding="utf-8") as f:
                f.write(_json.dumps(line, default=str) + "\n")
        except Exception:
            pass  # never let the diagnostic itself crash the gateway

    _exit_diag(
        "gateway.start",
        replace=replace,
        argv=sys.argv,
        stdin_is_tty=_stdin_is_tty,
        absorb_windows_console_controls=_absorb_windows_console_controls,
    )

    def _atexit_hook() -> None:
        _exit_diag("atexit.hook", sys_exc=repr(sys.exc_info()))

    _atexit.register(_atexit_hook)

    success = False
    try:
        success = asyncio.run(start_gateway(replace=replace, verbosity=verbosity))
        _exit_diag("asyncio.run.returned", success=success)
    except KeyboardInterrupt:
        # On Windows-detached runs this shouldn't fire (we absorb SIGINT above),
        # but keep the handler for console runs.
        _exit_diag(
            "asyncio.run.KeyboardInterrupt",
            traceback=_traceback.format_exc(),
        )
        print("\nGateway stopped.")
        return
    except SystemExit as e:
        _exit_diag(
            "asyncio.run.SystemExit",
            code=getattr(e, "code", None),
            traceback=_traceback.format_exc(),
        )
        raise
    except BaseException as e:
        # Absolutely everything else: Exception, asyncio.CancelledError,
        # even exotic BaseException subclasses. We want the cause logged.
        _exit_diag(
            "asyncio.run.exception",
            exc_type=type(e).__name__,
            exc_repr=repr(e),
            traceback=_traceback.format_exc(),
        )
        raise
    if not success:
        _exit_diag("gateway.exit_nonzero")
        sys.exit(1)
    _exit_diag("gateway.exit_clean")


# =============================================================================
# Gateway Setup (Interactive Messaging Platform Configuration)
# =============================================================================

# Per-platform config: each entry defines the env vars, setup instructions,
# and prompts needed to configure a messaging platform.
_PLATFORMS = [
    # Telegram moved to plugins/platforms/telegram/ — setup metadata discovered
    # dynamically via the platform registry entry registered by
    # plugins/platforms/telegram/adapter.py::register(). #41112.
    # Discord moved to plugins/platforms/discord/ — its setup metadata is
    # discovered dynamically via _all_platforms() from the platform registry
    # entry registered by plugins/platforms/discord/adapter.py::register().
    # Slack moved to plugins/platforms/slack/ for the same reason — its setup
    # metadata is discovered dynamically via the platform registry entry
    # registered by plugins/platforms/slack/adapter.py::register(). #41112.
    # Matrix moved to plugins/platforms/matrix/ — setup metadata discovered
    # dynamically via the platform registry entry registered by
    # plugins/platforms/matrix/adapter.py::register(). #41112.
    {
        "key": "mattermost",
        "label": "Mattermost",
        "emoji": "💬",
        "token_var": "MATTERMOST_TOKEN",
        "setup_instructions": [
            "1. In Mattermost: Integrations → Bot Accounts → Add Bot Account",
            "   (System Console → Integrations → Bot Accounts must be enabled)",
            "2. Give it a username (e.g. hermes) and copy the bot token",
            "3. Works with any self-hosted Mattermost instance — enter your server URL",
            "4. To find your user ID: click your avatar (top-left) → Profile",
            "   Your user ID is displayed there — click it to copy.",
            "   ⚠ This is NOT your username — it's a 26-character alphanumeric ID.",
            "5. To get a channel ID: click the channel name → View Info → copy the ID",
        ],
        "vars": [
            {
                "name": "MATTERMOST_URL",
                "prompt": "Server URL (e.g. https://mm.example.com)",
                "password": False,
                "help": "Your Mattermost server URL. Works with any self-hosted instance.",
            },
            {
                "name": "MATTERMOST_TOKEN",
                "prompt": "Bot token",
                "password": True,
                "help": "Paste the bot token from step 2 above.",
            },
            {
                "name": "MATTERMOST_ALLOWED_USERS",
                "prompt": "Allowed user IDs (comma-separated)",
                "password": False,
                "is_allowlist": True,
                "help": "Your Mattermost user ID from step 4 above.",
            },
            {
                "name": "MATTERMOST_HOME_CHANNEL",
                "prompt": "Home channel ID (for cron/notification delivery, or empty to set later with /set-home)",
                "password": False,
                "help": "Channel ID where Hermes delivers cron results and notifications.",
            },
            {
                "name": "MATTERMOST_REPLY_MODE",
                "prompt": "Reply mode — 'off' for flat messages, 'thread' for threaded replies (default: off)",
                "password": False,
                "help": "off = flat channel messages, thread = replies nest under your message.",
            },
        ],
    },
    # WhatsApp moved to plugins/platforms/whatsapp/ — setup metadata discovered
    # dynamically via the platform registry entry registered by
    # plugins/platforms/whatsapp/adapter.py::register(). #41112.
    {
        "key": "signal",
        "label": "Signal",
        "emoji": "📡",
        "token_var": "SIGNAL_HTTP_URL",
    },
    # Email and SMS moved to plugins/platforms/{email,sms}/ — setup metadata
    # discovered dynamically via the platform registry entries registered by
    # plugins/platforms/{email,sms}/adapter.py::register(). #41112.
    {
        "key": "weixin",
        "label": "Weixin / WeChat",
        "emoji": "💬",
        "token_var": "WEIXIN_ACCOUNT_ID",
    },
    {
        "key": "bluebubbles",
        "label": "BlueBubbles (iMessage)",
        "emoji": "💬",
        "token_var": "BLUEBUBBLES_SERVER_URL",
        "setup_instructions": [
            "1. Install BlueBubbles on a Mac that will act as your iMessage server:",
            "   https://bluebubbles.app/",
            "2. Complete the BlueBubbles setup wizard — sign in with your Apple ID",
            "3. In BlueBubbles Settings → API, note the Server URL and password",
            "4. The server URL is typically http://<your-mac-ip>:1234",
            "5. Hermes connects via the BlueBubbles REST API and receives",
            "   incoming messages via a local webhook",
            "6. To authorize users, use DM pairing: hermes pairing generate bluebubbles",
            "   Share the code — the user sends it via iMessage to get approved",
        ],
        "vars": [
            {
                "name": "BLUEBUBBLES_SERVER_URL",
                "prompt": "BlueBubbles server URL (e.g. http://192.168.1.10:1234)",
                "password": False,
                "help": "The URL shown in BlueBubbles Settings → API.",
            },
            {
                "name": "BLUEBUBBLES_PASSWORD",
                "prompt": "BlueBubbles server password",
                "password": True,
                "help": "The password shown in BlueBubbles Settings → API.",
            },
            {
                "name": "BLUEBUBBLES_ALLOWED_USERS",
                "prompt": "Pre-authorized phone numbers or iMessage IDs (comma-separated, or leave empty for DM pairing)",
                "password": False,
                "is_allowlist": True,
                "help": "Optional — pre-authorize specific users. Leave empty to use DM pairing instead (recommended).",
            },
            {
                "name": "BLUEBUBBLES_HOME_CHANNEL",
                "prompt": "Home channel (phone number or iMessage ID for cron/notifications, or empty)",
                "password": False,
                "help": "Phone number or Apple ID to deliver cron results and notifications to.",
            },
        ],
    },
    {
        "key": "qqbot",
        "label": "QQ Bot",
        "emoji": "🐧",
        "token_var": "QQ_APP_ID",
        "setup_instructions": [
            "1. Register a QQ Bot application at q.qq.com",
            "2. Note your App ID and App Secret from the application page",
            "3. Enable the required intents (C2C, Group, Guild messages)",
            "4. Configure sandbox or publish the bot",
        ],
        "vars": [
            {
                "name": "QQ_APP_ID",
                "prompt": "QQ Bot App ID",
                "password": False,
                "help": "Your QQ Bot App ID from q.qq.com.",
            },
            {
                "name": "QQ_CLIENT_SECRET",
                "prompt": "QQ Bot App Secret",
                "password": True,
                "help": "Your QQ Bot App Secret from q.qq.com.",
            },
            {
                "name": "QQ_ALLOWED_USERS",
                "prompt": "Allowed user OpenIDs (comma-separated, leave empty for open access)",
                "password": False,
                "is_allowlist": True,
                "help": "Optional — restrict DM access to specific user OpenIDs.",
            },
            {
                "name": "QQBOT_HOME_CHANNEL",
                "prompt": "Home channel (user/group OpenID for cron delivery, or empty)",
                "password": False,
                "help": "OpenID to deliver cron results and notifications to.",
            },
        ],
    },
    {
        "key": "yuanbao",
        "label": "Yuanbao",
        "emoji": "💎",
        "token_var": "YUANBAO_APP_ID",
        "setup_instructions": [
            "1. Download the Yuanbao app from https://yuanbao.tencent.com/",
            "2. In the app, go to PAI → My Bot and create a new bot",
            "3. After the bot is created, copy the App ID and App Secret",
            "4. Enter them below and Hermes will connect automatically over WebSocket",
        ],
        "vars": [
            {
                "name": "YUANBAO_APP_ID",
                "prompt": "App ID",
                "password": False,
                "help": "The App ID from your Yuanbao IM Bot credentials.",
            },
            {
                "name": "YUANBAO_APP_SECRET",
                "prompt": "App Secret",
                "password": True,
                "help": "The App Secret (used for HMAC signing) from your Yuanbao IM Bot.",
            },
        ],
    },
]


def _all_platforms() -> list[dict]:
    """Return the full list of platforms for setup menus.

    Combines the built-in ``_PLATFORMS`` with plugin platforms registered via
    ``platform_registry``. Plugins are discovered on first call so bundled
    platforms (like IRC, which auto-load via ``kind: platform``) appear in
    ``hermes setup gateway`` without needing the gateway to be running.
    Built-ins keep their dict shape; plugin entries are adapted to the same
    shape with ``_registry_entry`` holding the source.

    Platform-specific gating: some platforms can't be configured on
    every host. Currently:
      - Matrix is hidden on Windows. The [matrix] extra pulls
        ``mautrix[encryption]`` -> ``python-olm``, which has no Windows
        wheel and needs ``make`` + libolm to build from sdist. There's
        no native Windows path that works, so we don't offer it in the
        picker. Users who want Matrix on Windows can run hermes under
        WSL.
    """
    # Populate the registry so plugin platforms are visible. Idempotent.
    # Bundled platform plugins (``kind: platform``) auto-load unconditionally,
    # so every shipped messaging channel appears in the setup menu by default.
    # User-installed platform plugins under ~/.hermes/plugins/ still require
    # opt-in via ``plugins.enabled`` (untrusted code).
    try:
        from hermes_cli.plugins import discover_plugins

        discover_plugins()
    except Exception as e:
        logger.debug("plugin discovery failed during platform enumeration: %s", e)

    platforms = [dict(p) for p in _PLATFORMS]

    # Drop platforms that can't function on this host. See docstring.
    if sys.platform == "win32":
        platforms = [p for p in platforms if p.get("key") != "matrix"]

    by_key = {p["key"]: p for p in platforms}

    try:
        from gateway.platform_registry import platform_registry
    except Exception:
        return platforms

    for entry in platform_registry.all_entries():
        if entry.name in by_key:
            continue  # built-in already covers it
        # Drop platforms that can't function on this host. Matrix is hidden on
        # Windows (python-olm has no Windows wheel) — applies whether matrix is
        # a built-in or, post-#41112, a registry-discovered plugin.
        if sys.platform == "win32" and entry.name == "matrix":
            continue
        platforms.append(
            {
                "key": entry.name,
                "label": entry.label,
                "emoji": entry.emoji,
                "token_var": entry.required_env[0] if entry.required_env else "",
                "install_hint": entry.install_hint,
                "_registry_entry": entry,
            }
        )
    return platforms


def _platform_status(platform: dict) -> str:
    """Return a plain-text status string for a platform.

    Returns uncolored text so it can safely be embedded in
    curses menu items (ANSI codes break width calculation).
    """
    entry = platform.get("_registry_entry")
    if entry is not None:
        configured = False
        # Prefer is_connected (checks both env and config.yaml) over
        # check_fn (typically just dependency / env presence).
        if entry.is_connected is not None:
            try:
                from gateway.config import PlatformConfig

                synthetic = PlatformConfig(enabled=True)
                configured = bool(entry.is_connected(synthetic))
            except Exception:
                configured = False
        else:
            # No is_connected hook — fall back to check_fn as a coarse
            # "are deps present" gate. Don't fall back when is_connected
            # is defined and returned False; that would let "SDK is
            # installed" override "no token configured" and incorrectly
            # report the platform as ready.
            try:
                configured = bool(entry.check_fn())
            except Exception:
                configured = False
        return "configured" if configured else "not configured"

    token_var = platform.get("token_var", "")
    if not token_var:
        return "not configured"
    val = get_env_value(token_var)
    if token_var == "WHATSAPP_ENABLED":
        if val and val.lower() == "true":
            session_file = get_hermes_home() / "whatsapp" / "session" / "creds.json"
            if session_file.exists():
                return "configured + paired"
            return "enabled, not paired"
        return "not configured"
    if platform.get("key") == "signal":
        account = get_env_value("SIGNAL_ACCOUNT")
        if val and account:
            return "configured"
        if val or account:
            return "partially configured"
        return "not configured"
    if platform.get("key") == "email":
        pwd = get_env_value("EMAIL_PASSWORD")
        imap = get_env_value("EMAIL_IMAP_HOST")
        smtp = get_env_value("EMAIL_SMTP_HOST")
        if all([val, pwd, imap, smtp]):
            return "configured"
        if any([val, pwd, imap, smtp]):
            return "partially configured"
        return "not configured"
    if platform.get("key") == "matrix":
        homeserver = get_env_value("MATRIX_HOMESERVER")
        password = get_env_value("MATRIX_PASSWORD")
        if (val or password) and homeserver:
            e2ee = get_env_value("MATRIX_ENCRYPTION")
            suffix = " + E2EE" if e2ee and e2ee.lower() in {"true", "1", "yes"} else ""
            return f"configured{suffix}"
        if val or password or homeserver:
            return "partially configured"
        return "not configured"
    if platform.get("key") == "weixin":
        token = get_env_value("WEIXIN_TOKEN")
        if val and token:
            return "configured"
        if val or token:
            return "partially configured"
        return "not configured"
    if val:
        return "configured"
    return "not configured"


def _runtime_health_lines() -> list[str]:
    """Summarize the latest persisted gateway runtime health state."""
    try:
        from gateway.status import read_runtime_status
    except Exception:
        return []

    state = read_runtime_status()
    if not state:
        return []

    lines: list[str] = []
    gateway_state = state.get("gateway_state")
    exit_reason = state.get("exit_reason")
    active_agents = state.get("active_agents")
    restart_requested = state.get("restart_requested")
    platforms = state.get("platforms", {}) or {}

    for platform, pdata in platforms.items():
        if pdata.get("state") == "fatal":
            message = pdata.get("error_message") or "unknown error"
            lines.append(f"⚠ {platform}: {message}")

    if gateway_state == "startup_failed" and exit_reason:
        lines.append(f"⚠ Last startup issue: {exit_reason}")
    elif gateway_state == "draining":
        action = "restart" if restart_requested else "shutdown"
        from gateway.status import parse_active_agents

        count = parse_active_agents(active_agents)
        lines.append(f"⏳ Gateway draining for {action} ({count} active agent(s))")
    elif gateway_state == "stopped" and exit_reason:
        lines.append(f"⚠ Last shutdown reason: {exit_reason}")

    return lines


def _set_platform_unauthorized_dm_behavior(platform_key: str, behavior: str) -> None:
    """Persist a platform-specific unauthorized-DM policy in config.yaml."""
    write_platform_config_field(platform_key, "unauthorized_dm_behavior", behavior, raw=True)


def _setup_standard_platform(platform: dict):
    """Interactive setup for Telegram, Discord, or Slack."""
    emoji = platform["emoji"]
    label = platform["label"]
    token_var = platform["token_var"]

    print()
    print(color(f"  ─── {emoji} {label} Setup ───", Colors.CYAN))

    # Show step-by-step setup instructions if this platform has them
    instructions = platform.get("setup_instructions")
    if instructions:
        print()
        for line in instructions:
            print_info(f"  {line}")

    existing_token = get_env_value(token_var)
    if existing_token:
        print()
        print_success(f"{label} is already configured.")
        if not prompt_yes_no(f"  Reconfigure {label}?", False):
            return

    auto_token_saved = False
    auto_owner_user_id = None
    if platform.get("key") == "telegram":
        print()
        print_info("  Telegram can be configured automatically with a managed bot:")
        print_info("  [1] Automatic (scan QR → confirm in Telegram → done)")
        print_info("  [2] Manual BotFather token")
        choice = prompt("  Choice [1/2]", default="1")
        if choice.strip() == "1":
            try:
                from hermes_cli.telegram_managed_bot import (
                    auto_setup_telegram_bot_result,
                    is_valid_telegram_bot_token,
                )
            except ImportError:
                print_warning("  Automatic setup is unavailable in this install.")
            else:
                result = auto_setup_telegram_bot_result()
                if result and is_valid_telegram_bot_token(result.token):
                    save_env_value(token_var, result.token)
                    print_success("  Saved TELEGRAM_BOT_TOKEN")
                    auto_token_saved = True
                    auto_owner_user_id = result.owner_user_id
                else:
                    if result:
                        print_warning("  Automatic setup returned an invalid Telegram token.")
                    print()
                    print_info("  Falling back to manual setup...")

    allowed_val_set = None  # Track if user set an allowlist (for home channel offer)

    for var in platform["vars"]:
        print()
        print_info(f"  {var['help']}")
        existing = get_env_value(var["name"])
        if existing and var["name"] != token_var:
            print_info(f"  Current: {existing}")

        if auto_token_saved and var["name"] == token_var:
            print_info("  Token saved by automatic setup.")
            continue

        # Allowlist fields get special handling for the deny-by-default security model
        if var.get("is_allowlist"):
            if "TELEGRAM" in var["name"] and auto_owner_user_id:
                detected_id = str(auto_owner_user_id)
                print_success(f"  Detected your Telegram user ID: {detected_id}")
                if prompt_yes_no("  Allow this Telegram account to use the bot?", True):
                    extra = prompt(
                        "  Additional allowed user IDs (comma-separated, optional)",
                        password=False,
                    )
                    ids = [detected_id]
                    for uid in extra.replace(" ", "").split(","):
                        if uid and uid not in ids:
                            ids.append(uid)
                    cleaned = ",".join(ids)
                    save_env_value(var["name"], cleaned)
                    print_success("  Saved — only these users can interact with the bot.")
                    allowed_val_set = cleaned
                    continue

            print_info("  The gateway DENIES all users by default for security.")
            print_info("  Enter user IDs to create an allowlist, or leave empty")
            print_info("  and you'll be asked about open access next.")
            value = prompt(f"  {var['prompt']}", password=False)
            if value:
                cleaned = value.replace(" ", "")
                # For Discord, strip common prefixes (user:123, <@123>, <@!123>)
                if "DISCORD" in var["name"]:
                    parts = []
                    for uid in cleaned.split(","):
                        uid = uid.strip()
                        if uid.startswith("<@") and uid.endswith(">"):
                            uid = uid.lstrip("<@!").rstrip(">")
                        if uid.lower().startswith("user:"):
                            uid = uid[5:]
                        if uid:
                            parts.append(uid)
                    cleaned = ",".join(parts)
                save_env_value(var["name"], cleaned)
                print_success("  Saved — only these users can interact with the bot.")
                allowed_val_set = cleaned
            else:
                # No allowlist — ask about open access vs DM pairing
                print()
                is_email = platform.get("key") == "email"
                if is_email:
                    access_choices = [
                        "Enable open access (any email sender can message the bot)",
                        "Use DM pairing (unknown email senders receive a pairing code)",
                        "Keep unknown senders silent",
                    ]
                    default_access_idx = 2
                else:
                    access_choices = [
                        "Enable open access (anyone can message the bot)",
                        "Use DM pairing (unknown users request access, you approve with 'hermes pairing approve')",
                        "Skip for now (bot will deny all users until configured)",
                    ]
                    default_access_idx = 1
                access_idx = prompt_choice(
                    "  How should unauthorized users be handled?",
                    access_choices,
                    default_access_idx,
                )
                if access_idx == 0:
                    if is_email:
                        save_env_value("EMAIL_ALLOW_ALL_USERS", "true")
                    else:
                        save_env_value("GATEWAY_ALLOW_ALL_USERS", "true")
                    print_warning("  Open access enabled — anyone can use your bot!")
                elif access_idx == 1:
                    if is_email:
                        _set_platform_unauthorized_dm_behavior("email", "pair")
                    print_success(
                        "  DM pairing mode — users will receive a code to request access."
                    )
                    print_info(
                        "  Approve with: hermes pairing approve <platform> <code>"
                    )
                elif is_email:
                    print_success("  Unknown email senders will be ignored.")
                else:
                    print_info(
                        "  Skipped — configure later with 'hermes gateway setup'"
                    )
            continue

        value = prompt(f"  {var['prompt']}", password=var.get("password", False))
        if value:
            save_env_value(var["name"], value)
            print_success(f"  Saved {var['name']}")
        elif var["name"] == token_var:
            print_warning(f"  Skipped — {label} won't work without this.")
            return
        else:
            print_info("  Skipped (can configure later)")

    # If an allowlist was set and home channel wasn't, offer to reuse
    # the first user ID (common for Telegram DMs).
    home_var = f"{label.upper()}_HOME_CHANNEL"
    home_val = get_env_value(home_var)
    if allowed_val_set and not home_val and label == "Telegram":
        first_id = allowed_val_set.split(",")[0].strip()
        if first_id and prompt_yes_no(
            f"  Use your user ID ({first_id}) as the home channel?", True
        ):
            save_env_value(home_var, first_id)
            print_success(f"  Home channel set to {first_id}")

    print()
    print_success(f"{emoji} {label} configured!")


# _setup_whatsapp and _setup_dingtalk moved into their plugins:
# plugins/platforms/{whatsapp,dingtalk}/adapter.py::interactive_setup
# (registered via setup_fn, dispatched through the plugin path). #41112.


# _setup_wecom moved to plugins/platforms/wecom/adapter.py::interactive_setup
# (registered via setup_fn, dispatched through the plugin path). #41112.


def _is_service_installed() -> bool:
    """Check if the gateway is installed as a system service."""
    if supports_systemd_services():
        return (
            get_systemd_unit_path(system=False).exists()
            or get_systemd_unit_path(system=True).exists()
        )
    elif is_macos():
        return get_launchd_plist_path().exists()
    elif is_windows():
        from hermes_cli import gateway_windows

        return gateway_windows.is_installed()
    return False


def _is_service_running() -> bool:
    """Check if the gateway service is currently running."""
    if supports_systemd_services():
        user_unit_exists = get_systemd_unit_path(system=False).exists()
        system_unit_exists = get_systemd_unit_path(system=True).exists()

        if user_unit_exists:
            try:
                result = _run_systemctl(
                    ["is-active", get_service_name()],
                    system=False,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if result.stdout.strip() == "active":
                    return True
            except (RuntimeError, subprocess.TimeoutExpired):
                pass

        if system_unit_exists:
            try:
                result = _run_systemctl(
                    ["is-active", get_service_name()],
                    system=True,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if result.stdout.strip() == "active":
                    return True
            except (RuntimeError, subprocess.TimeoutExpired):
                pass

        return False
    elif is_macos() and get_launchd_plist_path().exists():
        try:
            result = subprocess.run(
                ["launchctl", "list", get_launchd_label()],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return result.returncode == 0
        except subprocess.TimeoutExpired:
            return False
    elif is_windows():
        from hermes_cli import gateway_windows

        if gateway_windows.is_installed():
            # "installed" doesn't necessarily mean "running" on Windows. The
            # canonical check is whether a gateway process actually exists.
            return len(find_gateway_pids()) > 0
    # Check for manual processes
    return len(find_gateway_pids()) > 0


def _setup_weixin():
    """Interactive setup for Weixin / WeChat personal accounts."""
    print()
    print(color("  ─── 💬 Weixin / WeChat Setup ───", Colors.CYAN))
    print()
    print_info("  1. Hermes will open Tencent iLink QR login in this terminal.")
    print_info("  2. Use WeChat to scan and confirm the QR code.")
    print_info(
        "  3. Hermes will store the returned account_id/token in ~/.hermes/.env."
    )
    print_info(
        "  4. This adapter supports native text, image, video, and document delivery."
    )

    existing_account = get_env_value("WEIXIN_ACCOUNT_ID")
    existing_token = get_env_value("WEIXIN_TOKEN")
    if existing_account and existing_token:
        print()
        print_success("Weixin is already configured.")
        if not prompt_yes_no("  Reconfigure Weixin?", False):
            return

    try:
        from gateway.platforms.weixin import check_weixin_requirements, qr_login
    except Exception as exc:
        print_error(f"  Weixin adapter import failed: {exc}")
        print_info("  Install gateway dependencies first, then retry.")
        return

    if not check_weixin_requirements():
        print_error("  Missing dependencies: Weixin needs aiohttp and cryptography.")
        print_info("  Install them, then rerun `hermes gateway setup`.")
        return

    print()
    if not prompt_yes_no("  Start QR login now?", True):
        print_info("  Cancelled.")
        return

    import asyncio

    try:
        credentials = asyncio.run(qr_login(str(get_hermes_home())))
    except KeyboardInterrupt:
        print()
        print_warning("  Weixin setup cancelled.")
        return
    except Exception as exc:
        print_error(f"  QR login failed: {exc}")
        return

    if not credentials:
        print_warning("  QR login did not complete.")
        return

    account_id = credentials.get("account_id", "")
    token = credentials.get("token", "")
    base_url = credentials.get("base_url", "")
    user_id = credentials.get("user_id", "")

    save_env_value("WEIXIN_ACCOUNT_ID", account_id)
    save_env_value("WEIXIN_TOKEN", token)
    if base_url:
        save_env_value("WEIXIN_BASE_URL", base_url)
    save_env_value(
        "WEIXIN_CDN_BASE_URL",
        get_env_value("WEIXIN_CDN_BASE_URL") or "https://novac2c.cdn.weixin.qq.com/c2c",
    )

    print()
    access_choices = [
        "Use DM pairing approval (recommended)",
        "Allow all direct messages",
        "Only allow listed user IDs",
        "Disable direct messages",
    ]
    access_idx = prompt_choice(
        "  How should direct messages be authorized?", access_choices, 0
    )
    if access_idx == 0:
        save_env_value("WEIXIN_DM_POLICY", "pairing")
        save_env_value("WEIXIN_ALLOW_ALL_USERS", "false")
        save_env_value("WEIXIN_ALLOWED_USERS", "")
        print_success("  DM pairing enabled.")
        print_info(
            "  Unknown DM users can request access and you approve them with `hermes pairing approve`."
        )
    elif access_idx == 1:
        save_env_value("WEIXIN_DM_POLICY", "open")
        save_env_value("WEIXIN_ALLOW_ALL_USERS", "true")
        save_env_value("WEIXIN_ALLOWED_USERS", "")
        print_warning("  Open DM access enabled for Weixin.")
    elif access_idx == 2:
        default_allow = user_id or ""
        allowlist = prompt(
            "  Allowed Weixin user IDs (comma-separated)", default_allow, password=False
        ).replace(" ", "")
        save_env_value("WEIXIN_DM_POLICY", "allowlist")
        save_env_value("WEIXIN_ALLOW_ALL_USERS", "false")
        save_env_value("WEIXIN_ALLOWED_USERS", allowlist)
        print_success("  Weixin allowlist saved.")
    else:
        save_env_value("WEIXIN_DM_POLICY", "disabled")
        save_env_value("WEIXIN_ALLOW_ALL_USERS", "false")
        save_env_value("WEIXIN_ALLOWED_USERS", "")
        print_warning("  Direct messages disabled.")

    print()
    print_info(
        "  Note: QR login connects an iLink bot identity (e.g. ...@im.bot), not a"
    )
    print_info(
        "  scriptable personal WeChat account. Ordinary WeChat groups typically cannot"
    )
    print_info(
        "  invite an @im.bot identity, and iLink does not deliver ordinary-group events"
    )
    print_info(
        "  to most bot accounts. The settings below only apply when iLink actually"
    )
    print_info(
        "  delivers group events for your account type — otherwise DM remains the only"
    )
    print_info("  working channel regardless of this choice.")
    group_choices = [
        "Disable group chats (recommended)",
        "Allow all group chats",
        "Only allow listed group chat IDs",
    ]
    group_idx = prompt_choice("  How should group chats be handled?", group_choices, 0)
    if group_idx == 0:
        save_env_value("WEIXIN_GROUP_POLICY", "disabled")
        save_env_value("WEIXIN_GROUP_ALLOWED_USERS", "")
        print_info("  Group chats disabled.")
    elif group_idx == 1:
        save_env_value("WEIXIN_GROUP_POLICY", "open")
        save_env_value("WEIXIN_GROUP_ALLOWED_USERS", "")
        print_warning(
            "  All group chats enabled (only takes effect if iLink delivers group events)."
        )
    else:
        allow_groups = prompt(
            "  Allowed group chat IDs (comma-separated, not member user IDs)",
            "",
            password=False,
        ).replace(" ", "")
        save_env_value("WEIXIN_GROUP_POLICY", "allowlist")
        save_env_value("WEIXIN_GROUP_ALLOWED_USERS", allow_groups)
        print_success(
            "  Group allowlist saved (only takes effect if iLink delivers group events)."
        )

    if user_id:
        print()
        if prompt_yes_no(
            f"  Use your Weixin user ID ({user_id}) as the home channel?", True
        ):
            save_env_value("WEIXIN_HOME_CHANNEL", user_id)
            print_success(f"  Home channel set to {user_id}")

    print()
    print_success("Weixin configured!")
    print_info(f"  Account ID: {account_id}")
    if user_id:
        print_info(f"  User ID: {user_id}")


# _setup_feishu moved to plugins/platforms/feishu/adapter.py::interactive_setup
# (registered via setup_fn, dispatched through the plugin path). #41112.


def _setup_qqbot():
    """Interactive setup for QQ Bot — scan-to-configure or manual credentials."""
    print()
    print(color("  ─── 🐧 QQ Bot Setup ───", Colors.CYAN))

    existing_app_id = get_env_value("QQ_APP_ID")
    existing_secret = get_env_value("QQ_CLIENT_SECRET")
    if existing_app_id and existing_secret:
        print()
        print_success("QQ Bot is already configured.")
        if not prompt_yes_no("  Reconfigure QQ Bot?", False):
            return

    # ── Choose setup method ──
    print()
    method_choices = [
        "Scan QR code to add bot automatically (recommended)",
        "Enter existing App ID and App Secret manually",
    ]
    method_idx = prompt_choice(
        "  How would you like to set up QQ Bot?", method_choices, 0
    )

    credentials = None

    if method_idx == 0:
        # ── QR scan-to-configure ──
        try:
            from gateway.platforms.qqbot import qr_register

            credentials = qr_register()
        except KeyboardInterrupt:
            print()
            print_warning("  QQ Bot setup cancelled.")
            return
        if not credentials:
            print_info("  QR setup did not complete. Continuing with manual input.")

    # ── Manual credential input ──
    if not credentials:
        print()
        print_info("  Go to https://q.qq.com to register a QQ Bot application.")
        print_info("  Note your App ID and App Secret from the application page.")
        print()
        app_id = prompt("  App ID", password=False)
        if not app_id:
            print_warning("  Skipped — QQ Bot won't work without an App ID.")
            return
        app_secret = prompt("  App Secret", password=True)
        if not app_secret:
            print_warning("  Skipped — QQ Bot won't work without an App Secret.")
            return
        credentials = {
            "app_id": app_id.strip(),
            "client_secret": app_secret.strip(),
            "user_openid": "",
        }

    # ── Save core credentials ──
    save_env_value("QQ_APP_ID", credentials["app_id"])
    save_env_value("QQ_CLIENT_SECRET", credentials["client_secret"])

    user_openid = credentials.get("user_openid", "")

    # ── DM security policy ──
    print()
    access_choices = [
        "Use DM pairing approval (recommended)",
        "Allow all direct messages",
        "Only allow listed user OpenIDs",
    ]
    access_idx = prompt_choice(
        "  How should direct messages be authorized?", access_choices, 0
    )
    if access_idx == 0:
        save_env_value("QQ_ALLOW_ALL_USERS", "false")
        if user_openid:
            print()
            if prompt_yes_no(
                f"  Add yourself ({user_openid}) to the allow list?", True
            ):
                save_env_value("QQ_ALLOWED_USERS", user_openid)
                print_success(f"  Allow list set to {user_openid}")
            else:
                save_env_value("QQ_ALLOWED_USERS", "")
        else:
            save_env_value("QQ_ALLOWED_USERS", "")
        print_success("  DM pairing enabled.")
        print_info(
            "  Unknown users can request access; approve with `hermes pairing approve`."
        )
    elif access_idx == 1:
        save_env_value("QQ_ALLOW_ALL_USERS", "true")
        save_env_value("QQ_ALLOWED_USERS", "")
        print_warning("  Open DM access enabled for QQ Bot.")
    else:
        default_allow = user_openid or ""
        allowlist = prompt(
            "  Allowed user OpenIDs (comma-separated)", default_allow, password=False
        ).replace(" ", "")
        save_env_value("QQ_ALLOW_ALL_USERS", "false")
        save_env_value("QQ_ALLOWED_USERS", allowlist)
        print_success("  Allowlist saved.")

    # ── Home channel ──
    if user_openid:
        print()
        if prompt_yes_no(
            f"  Use your QQ user ID ({user_openid}) as the home channel?", True
        ):
            save_env_value("QQBOT_HOME_CHANNEL", user_openid)
            print_success(f"  Home channel set to {user_openid}")
    else:
        print()
        home_channel = prompt(
            "  Home channel OpenID (for cron/notifications, or empty)", password=False
        )
        if home_channel:
            save_env_value("QQBOT_HOME_CHANNEL", home_channel.strip())
            print_success(f"  Home channel set to {home_channel.strip()}")

    print()
    print_success("🐧 QQ Bot configured!")
    print_info(f"  App ID: {credentials['app_id']}")


def _setup_signal():
    """Interactive setup for Signal messenger."""
    import shutil

    print()
    print(color("  ─── 📡 Signal Setup ───", Colors.CYAN))

    existing_url = get_env_value("SIGNAL_HTTP_URL")
    existing_account = get_env_value("SIGNAL_ACCOUNT")
    if existing_url and existing_account:
        print()
        print_success("Signal is already configured.")
        if not prompt_yes_no("  Reconfigure Signal?", False):
            return

    # Check if signal-cli is available
    print()
    if shutil.which("signal-cli"):
        print_success("signal-cli found on PATH.")
    else:
        print_warning("signal-cli not found on PATH.")
        print_info("  Signal requires signal-cli running as an HTTP daemon.")
        print_info("  Install options:")
        print_info(
            "    Linux:  download from https://github.com/AsamK/signal-cli/releases"
        )
        print_info("    macOS:  brew install signal-cli")
        print_info("    Docker: bbernhard/signal-cli-rest-api")
        print()
        print_info("  After installing, link your account and start the daemon:")
        print_info('    signal-cli link -n "HermesAgent"')
        print_info("    signal-cli --account +YOURNUMBER daemon --http 127.0.0.1:8080")
        print()

    # HTTP URL
    print()
    print_info("  Enter the URL where signal-cli HTTP daemon is running.")
    default_url = existing_url or "http://127.0.0.1:8080"
    try:
        url = input(f"  HTTP URL [{default_url}]: ").strip() or default_url
    except (EOFError, KeyboardInterrupt):
        print("\n  Setup cancelled.")
        return

    # Test connectivity
    print_info("  Testing connection...")
    try:
        import httpx

        resp = httpx.get(f"{url.rstrip('/')}/api/v1/check", timeout=10.0)
        if resp.status_code == 200:
            print_success("  signal-cli daemon is reachable!")
        else:
            print_warning(f"  signal-cli responded with status {resp.status_code}.")
            if not prompt_yes_no("  Continue anyway?", False):
                return
    except Exception as e:
        print_warning(f"  Could not reach signal-cli at {url}: {e}")
        if not prompt_yes_no(
            "  Save this URL anyway? (you can start signal-cli later)", True
        ):
            return

    save_env_value("SIGNAL_HTTP_URL", url)

    # Account phone number
    print()
    print_info("  Enter your Signal account phone number in E.164 format.")
    print_info("  Example: +15551234567")
    default_account = existing_account or ""
    try:
        account = input(
            f"  Account number{f' [{default_account}]' if default_account else ''}: "
        ).strip()
        if not account:
            account = default_account
    except (EOFError, KeyboardInterrupt):
        print("\n  Setup cancelled.")
        return

    if not account:
        print_error("  Account number is required.")
        return

    save_env_value("SIGNAL_ACCOUNT", account)

    # Allowed users
    print()
    print_info("  The gateway DENIES all users by default for security.")
    print_info("  Enter phone numbers or UUIDs of allowed users (comma-separated).")
    existing_allowed = get_env_value("SIGNAL_ALLOWED_USERS") or ""
    default_allowed = existing_allowed or account
    try:
        allowed = (
            input(f"  Allowed users [{default_allowed}]: ").strip() or default_allowed
        )
    except (EOFError, KeyboardInterrupt):
        print("\n  Setup cancelled.")
        return

    save_env_value("SIGNAL_ALLOWED_USERS", allowed)

    # Group messaging
    print()
    if prompt_yes_no(
        "  Enable group messaging? (disabled by default for security)", False
    ):
        print()
        print_info("  Enter group IDs to allow, or * for all groups.")
        existing_groups = get_env_value("SIGNAL_GROUP_ALLOWED_USERS") or ""
        try:
            groups = (
                input(f"  Group IDs [{existing_groups or '*'}]: ").strip()
                or existing_groups
                or "*"
            )
        except (EOFError, KeyboardInterrupt):
            print("\n  Setup cancelled.")
            return
        save_env_value("SIGNAL_GROUP_ALLOWED_USERS", groups)

    print()
    print_success("Signal configured!")
    print_info(f"  URL: {url}")
    print_info(f"  Account: {account}")
    print_info("  DM auth: via SIGNAL_ALLOWED_USERS + DM pairing")
    print_info(
        f"  Groups: {'enabled' if get_env_value('SIGNAL_GROUP_ALLOWED_USERS') else 'disabled'}"
    )


def _builtin_setup_fn(key: str):
    """Resolve the interactive setup function for a built-in platform key.

    Late-bound to avoid a circular import with ``hermes_cli.setup`` (which
    imports from this module for the remaining bespoke flows).
    """
    from hermes_cli import setup as _s

    return {
        # telegram moved into the plugin: setup_fn registered by
        # plugins/platforms/telegram/adapter.py::register(). #41112.
        # discord moved into the plugin: setup_fn is registered by
        # plugins/platforms/discord/adapter.py::register() and dispatched
        # via the plugin path in _configure_platform().
        # slack moved into the plugin: setup_fn is registered by
        # plugins/platforms/slack/adapter.py::register() and dispatched
        # via the plugin path in _configure_platform(). #41112.
        # matrix moved into the plugin: setup_fn registered by
        # plugins/platforms/matrix/adapter.py::register() and dispatched via
        # the plugin path in _configure_platform(). #41112.
        # mattermost moved into the plugin: setup_fn is registered by
        # plugins/platforms/mattermost/adapter.py::register() and dispatched
        # via the plugin path in _configure_platform().
        "bluebubbles": _s._setup_bluebubbles,
        "webhooks": _s._setup_webhooks,
        "signal": _setup_signal,
        # whatsapp + dingtalk moved into plugins: setup_fn registered by
        # plugins/platforms/{whatsapp,dingtalk}/adapter.py::register() and
        # dispatched via the plugin path in _configure_platform(). #41112.
        "weixin": _setup_weixin,
        # feishu moved into the plugin: setup_fn registered by
        # plugins/platforms/feishu/adapter.py::register(). #41112.
        # wecom moved into the plugin: setup_fn registered by
        # plugins/platforms/wecom/adapter.py::register(). #41112.
        "qqbot": _setup_qqbot,
    }.get(key)


def _configure_platform(platform: dict) -> None:
    """Run the interactive setup flow for a single platform.

    Dispatch order:
      1. Plugin-provided ``setup_fn`` on the registry entry.
      2. Built-in setup function matched by platform key.
      3. ``_setup_standard_platform`` when the entry has a ``vars`` schema.
      4. Env-var hint fallback for plugins that offer no setup helper.

    Bundled platform plugins (e.g. IRC) auto-load, so no plugin enable step
    is needed here. User-installed platform plugins under ~/.hermes/plugins/
    must already be in ``plugins.enabled`` before they appear in this menu.
    """
    entry = platform.get("_registry_entry")

    if entry is not None and entry.setup_fn is not None:
        entry.setup_fn()
        return

    fn = _builtin_setup_fn(platform["key"])
    if fn is not None:
        fn()
        return

    if platform.get("vars"):
        _setup_standard_platform(platform)
        return

    # Plugin with no setup helper — show env-var instructions.
    label = platform.get("label", platform["key"])
    emoji = platform.get("emoji", "🔌")
    print()
    print(color(f"  ─── {emoji} {label} Setup ───", Colors.CYAN))
    required = entry.required_env if entry else []
    if required:
        print_info(f"  Set these env vars in ~/.hermes/.env: {', '.join(required)}")
    else:
        print_info(
            f"  Configure {label} in config.yaml under gateway.platforms.{platform['key']}"
        )
    if platform.get("install_hint"):
        print_info(f"  {platform['install_hint']}")


def gateway_setup():
    """Interactive setup for messaging platforms + gateway service."""
    if is_managed():
        managed_error("run gateway setup")
        return

    print()
    print(
        color(
            "┌─────────────────────────────────────────────────────────┐",
            Colors.MAGENTA,
        )
    )
    print(
        color(
            "│             ⚕ Gateway Setup                            │", Colors.MAGENTA
        )
    )
    print(
        color(
            "├─────────────────────────────────────────────────────────┤",
            Colors.MAGENTA,
        )
    )
    print(
        color(
            "│  Configure messaging platforms and the gateway service. │",
            Colors.MAGENTA,
        )
    )
    print(
        color(
            "│  Press Ctrl+C at any time to exit.                     │", Colors.MAGENTA
        )
    )
    print(
        color(
            "└─────────────────────────────────────────────────────────┘",
            Colors.MAGENTA,
        )
    )

    # ── Gateway service status ──
    print()
    service_installed = _is_service_installed()
    service_running = _is_service_running()

    if supports_systemd_services() and has_conflicting_systemd_units():
        print_systemd_scope_conflict_warning()
        print()

    if supports_systemd_services() and has_legacy_hermes_units():
        print_legacy_unit_warning()
        print()

    if service_installed and service_running:
        print_success("Gateway service is installed and running.")
    elif service_installed:
        print_warning("Gateway service is installed but not running.")
        if supports_systemd_services() and _system_scope_wizard_would_need_root():
            _print_system_scope_remediation("start")
        elif prompt_yes_no("  Start it now?", True):
            try:
                if supports_systemd_services():
                    systemd_start()
                elif is_macos():
                    launchd_start()
            except UserSystemdUnavailableError as e:
                print_error("  Failed to start — user systemd not reachable:")
                for line in str(e).splitlines():
                    print(f"  {line}")
            except SystemScopeRequiresRootError as e:
                # Defense in depth: the pre-check above should have caught
                # this, but handle the race/edge case gracefully instead of
                # letting the exception escape the wizard.
                print_error(f"  Failed to start: {e}")
                _print_system_scope_remediation("start")
            except subprocess.CalledProcessError as e:
                print_error(f"  Failed to start: {e}")
    else:
        print_info("Gateway service is not installed yet.")
        print_info("You'll be offered to install it after configuring platforms.")

    # ── Platform configuration loop ──
    while True:
        print()
        print_header("Messaging Platforms")

        platforms = _all_platforms()

        menu_items = [
            f"{p['emoji']} {p['label']}  ({_platform_status(p)})" for p in platforms
        ]
        menu_items.append("Done")

        choice = prompt_choice(
            "Select a platform to configure:", menu_items, len(menu_items) - 1
        )
        if choice == len(platforms):
            break

        _configure_platform(platforms[choice])

    # ── Post-setup: offer to install/restart gateway ──
    # Consider any platform (built-in or plugin) where the user has made
    # meaningful progress.  ``_platform_status`` already handles plugin
    # entries via their check_fn and per-platform dual-states like
    # WhatsApp's "enabled, not paired".
    def _is_progress(status: str) -> bool:
        s = status.lower()
        return not (
            s == "not configured"
            or s.startswith("partially")
            or s.startswith("plugin disabled")
        )

    any_configured = any(_is_progress(_platform_status(p)) for p in _all_platforms())

    if any_configured:
        print()
        print(color("─" * 58, Colors.DIM))
        service_installed = _is_service_installed()
        service_running = _is_service_running()

        if service_running:
            if supports_systemd_services() and _system_scope_wizard_would_need_root():
                _print_system_scope_remediation("restart")
            elif prompt_yes_no("  Restart the gateway to pick up changes?", True):
                try:
                    if supports_systemd_services():
                        systemd_restart()
                    elif is_macos():
                        launchd_restart()
                    elif is_windows():
                        from hermes_cli import gateway_windows

                        gateway_windows.restart()
                    else:
                        stop_profile_gateway()
                        print_info("Start manually: hermes gateway")
                except UserSystemdUnavailableError as e:
                    print_error("  Restart failed — user systemd not reachable:")
                    for line in str(e).splitlines():
                        print(f"  {line}")
                except SystemScopeRequiresRootError as e:
                    print_error(f"  Restart failed: {e}")
                    _print_system_scope_remediation("restart")
                except subprocess.CalledProcessError as e:
                    print_error(f"  Restart failed: {e}")
        elif service_installed:
            if supports_systemd_services() and _system_scope_wizard_would_need_root():
                _print_system_scope_remediation("start")
            elif prompt_yes_no("  Start the gateway service?", True):
                try:
                    if supports_systemd_services():
                        systemd_start()
                    elif is_macos():
                        launchd_start()
                    elif is_windows():
                        from hermes_cli import gateway_windows

                        gateway_windows.start()
                except UserSystemdUnavailableError as e:
                    print_error("  Start failed — user systemd not reachable:")
                    for line in str(e).splitlines():
                        print(f"  {line}")
                except SystemScopeRequiresRootError as e:
                    print_error(f"  Start failed: {e}")
                    _print_system_scope_remediation("start")
                except subprocess.CalledProcessError as e:
                    print_error(f"  Start failed: {e}")
        else:
            print()
            if supports_systemd_services() or is_macos() or is_windows():
                if supports_systemd_services():
                    platform_name = "systemd"
                elif is_macos():
                    platform_name = "launchd"
                else:
                    platform_name = "Scheduled Task"
                wsl_note = " (note: services may not survive WSL restarts)" if is_wsl() else ""
                start_now = prompt_yes_no("  Start the gateway now?", True)
                start_on_login = prompt_yes_no(
                    f"  Start the gateway automatically on login/boot as a {platform_name} service?{wsl_note}",
                    True,
                )
                if start_now or start_on_login:
                    try:
                        installed_scope = None
                        did_install = False
                        if supports_systemd_services():
                            installed_scope, did_install = install_linux_gateway_from_setup(
                                force=False,
                                enable_on_startup=start_on_login,
                            )
                        elif is_macos():
                            launchd_install(force=False)
                            did_install = True
                        else:
                            from hermes_cli import gateway_windows

                            gateway_windows.install(force=False)
                            did_install = True
                        print()
                        if did_install and start_now:
                            try:
                                if supports_systemd_services():
                                    systemd_start(system=installed_scope == "system")
                                elif is_macos():
                                    launchd_start()
                                elif is_windows():
                                    from hermes_cli import gateway_windows
                                    gateway_windows.start()
                            except UserSystemdUnavailableError as e:
                                print_error(
                                    "  Start failed — user systemd not reachable:"
                                )
                                for line in str(e).splitlines():
                                    print(f"  {line}")
                            except subprocess.CalledProcessError as e:
                                print_error(f"  Start failed: {e}")
                    except subprocess.CalledProcessError as e:
                        print_error(f"  Install failed: {e}")
                        print_info("  You can try manually: hermes gateway install")
                else:
                    print_info("  Skipped start and auto-start setup.")
                    print_info("  You can install later: hermes gateway install")
                    if supports_systemd_services():
                        print_info(
                            "  Or as a boot-time service: sudo hermes gateway install --system"
                        )
                    print_info("  Or run in foreground:  hermes gateway run")
            elif is_wsl():
                print_info("  WSL detected but systemd is not running.")
                print_info("  Run in foreground: hermes gateway run")
                print_info(
                    "  For persistence:   tmux new -s hermes 'hermes gateway run'"
                )
                print_info(
                    "  To enable systemd: add systemd=true to /etc/wsl.conf, then 'wsl --shutdown'"
                )
            elif is_termux():
                from hermes_constants import display_hermes_home as _dhh

                print_info("  Termux does not use systemd/launchd services.")
                print_info("  Run in foreground: hermes gateway run")
                print_info(
                    f"  Or start it manually in the background (best effort): nohup hermes gateway run >{_dhh()}/logs/gateway.log 2>&1 &"
                )
            else:
                print_info("  Service install not supported on this platform.")
                print_info("  Run in foreground: hermes gateway run")
    else:
        print()
        print_info("No platforms configured. Run 'hermes gateway setup' when ready.")

    print()


# =============================================================================
# Main Command Handler
# =============================================================================

def _dispatch_via_service_manager_if_s6(
    action: str, profile: str | None = None,
) -> bool:
    """If we're in a container with s6, dispatch gateway lifecycle via s6.

    Returns True iff dispatched (caller should ``return``); False
    otherwise — caller continues with the host-side code path.

    ``action`` is one of ``start`` / ``stop`` / ``restart``. The
    profile defaults to the current one (resolved via ``_profile_arg``).
    The s6 service slot was created either by the Phase 4 profile-create
    hook or by the container-boot reconciler (cont-init.d/02-…). If it
    doesn't exist or s6 returns an error, the named errors from
    :mod:`hermes_cli.service_manager` are caught and surfaced as
    actionable CLI messages (no raw ``CalledProcessError`` traceback).
    """
    from hermes_cli.service_manager import (
        GatewayNotRegisteredError,
        S6CommandError,
        detect_service_manager,
        get_service_manager,
    )

    if detect_service_manager() != "s6":
        return False
    if profile is None:
        # _profile_suffix() returns the bare profile name for
        # HERMES_HOME=<root>/profiles/<name>, "" for the default root,
        # or a hash for unrelated paths. Map "" → "default" so the
        # default-profile gateway is reachable as gateway-default.
        profile = _profile_suffix() or "default"
    mgr = get_service_manager()
    service_name = f"gateway-{profile}"
    try:
        if action == "start":
            mgr.start(service_name)
        elif action == "stop":
            mgr.stop(service_name)
        elif action == "restart":
            mgr.restart(service_name)
        else:
            return False
    except GatewayNotRegisteredError as exc:
        print(f"✗ {exc}")
        sys.exit(1)
    except S6CommandError as exc:
        print(f"✗ {exc}")
        sys.exit(1)
    return True


def _dispatch_all_via_service_manager_if_s6(action: str) -> bool:
    """Inside a container with s6, dispatch ``--all`` lifecycle to every
    registered profile gateway.

    Returns True iff dispatched (caller should ``return``); False
    otherwise — caller continues with the host-side code path.

    Without this, ``hermes gateway stop --all`` and ``... restart --all``
    fall through to ``kill_gateway_processes(all_profiles=True)``, which
    just ``pkill``s every gateway process. s6-supervise observes the
    crash and restarts each one ~1s later — so ``--all`` ends up
    *kicking* every gateway instead of *stopping* it. By iterating
    ``list_profile_gateways()`` and sending the lifecycle command
    through the service manager we get the intended semantics (s6's
    ``want up``/``want down`` flips correctly so supervise stays down
    after a stop).

    ``action`` is one of ``stop`` / ``restart`` (``start --all`` isn't
    a supported CLI surface).
    """
    from hermes_cli.service_manager import (
        detect_service_manager,
        get_service_manager,
    )

    if detect_service_manager() != "s6":
        return False
    if action not in ("stop", "restart"):
        return False
    mgr = get_service_manager()
    profiles = mgr.list_profile_gateways()
    if not profiles:
        print("✗ No profile gateways registered under s6")
        return True
    fn = mgr.stop if action == "stop" else mgr.restart
    errors: list[tuple[str, Exception]] = []
    for profile in profiles:
        service_name = f"gateway-{profile}"
        try:
            fn(service_name)
        except Exception as exc:  # noqa: BLE001 — report and continue
            errors.append((profile, exc))
    succeeded = len(profiles) - len(errors)
    verb = "stopped" if action == "stop" else "restarted"
    if succeeded:
        print(f"✓ {verb.capitalize()} {succeeded} profile gateway(s) under s6")
    for profile, exc in errors:
        print(f"✗ Could not {action} gateway-{profile}: {exc}")
    return True



def gateway_command(args):
    """Handle gateway subcommands."""
    try:
        subcmd = getattr(args, "gateway_command", None)
        if subcmd in {"start", "stop", "restart"} and getattr(args, "all", False):
            # The lock spans discovery, launchctl fences, exact process
            # convergence, and restore—not just one helper call.  Raw
            # launchctl invocations from another actor can still race the
            # probe-to-bootout gap; the adjacent revalidations remain required
            # because this lock coordinates cooperating Hermes CLIs only.
            with all_profile_lifecycle_lock(timeout=30.0):
                return _gateway_command_inner(args)
        return _gateway_command_inner(args)
    except GatewayLifecycleLockError as e:
        print_error(f"Could not acquire the all-profile lifecycle lock: {e}")
        print_error("No gateway lifecycle state was changed.")
        sys.exit(1)
    except UserSystemdUnavailableError as e:
        # Clean, actionable message instead of a traceback when the user D-Bus
        # session is unreachable (fresh SSH shell, no linger, container, etc.).
        print_error("User systemd not reachable:")
        for line in str(e).splitlines():
            print(f"  {line}")
        sys.exit(1)
    except SystemScopeRequiresRootError as e:
        # The direct ``hermes gateway install|uninstall|start|stop|restart``
        # path lands here when the user typed a system-scope action without
        # sudo. Same exit code as before — just gives the wizard a way to
        # intercept the same condition with friendlier guidance before the
        # error is raised.
        print(str(e))
        sys.exit(1)
    except LaunchdInventoryError as e:
        print_error(str(e))
        print_error("No launchd service or global process state was changed.")
        sys.exit(1)


def _maybe_redirect_run_to_s6_supervision(args) -> bool:
    """Inside an s6 container, redirect bare ``gateway run`` to the
    supervised path.

    Background. Before the s6 image landed, ``docker run <image> gateway
    run`` was the standard way to start a containerized gateway: the
    gateway was the container's main process, tini reaped zombies, and
    container exit code == gateway exit code. With s6-overlay as PID 1,
    we'd much rather have the gateway run as a supervised s6 longrun
    (auto-restart on crash, dashboard supervised alongside, multiple
    profile gateways under the same /init). This redirect upgrades the
    old invocation transparently — the user gets the new behavior
    without changing their docker run command.

    Three gates make this a no-op outside the intended scope:

      1. ``_dispatch_via_service_manager_if_s6`` returns False unless
         we're in a container with s6 as PID 1. Host runs of
         ``hermes gateway run`` are unaffected.
      2. ``HERMES_S6_SUPERVISED_CHILD`` is exported by
         ``S6ServiceManager._render_run_script`` for the supervised
         process itself — i.e. when s6-supervise execs ``hermes gateway
         run --replace`` as a longrun, this guard short-circuits the
         redirect so the supervised gateway actually runs in
         foreground (otherwise we'd recurse: run → start → run → start
         → ...).
      3. ``--no-supervise`` (or ``HERMES_GATEWAY_NO_SUPERVISE=1``) opts
         out for users who genuinely want pre-s6 semantics — CI smoke
         tests, debugging the foreground startup path, etc.

    Returns True iff dispatched (caller should ``return``).
    """
    no_supervise = getattr(args, "no_supervise", False) or \
        os.environ.get("HERMES_GATEWAY_NO_SUPERVISE", "").lower() in ("1", "true", "yes")
    if no_supervise:
        return False
    if os.environ.get("HERMES_S6_SUPERVISED_CHILD"):
        # We ARE the supervised child s6-supervise is running. Fall
        # through to the foreground code path so the gateway actually
        # starts.
        return False
    if not _dispatch_via_service_manager_if_s6("start"):
        return False
    # Loud breadcrumb: explain the upgrade and how to opt out. Print to
    # stderr so it doesn't pollute stdout-parsing scripts. The
    # supervised gateway's own logs are routed by s6-log to both
    # `docker logs` and ${HERMES_HOME}/logs/gateways/<profile>/current,
    # so the user sees a clear sequence: this banner first, then the
    # gateway's own stdout/stderr from the supervisor.
    print(
        "→ gateway is now running under s6 supervision (auto-restart on crash,\n"
        "  dashboard supervised alongside if HERMES_DASHBOARD is set).\n"
        "  This is the recommended setup for the s6 container image — the\n"
        "  gateway will keep running even if it crashes.\n"
        "  Use `--no-supervise` (or HERMES_GATEWAY_NO_SUPERVISE=1) to opt out\n"
        "  and get the pre-s6 foreground behavior instead.",
        file=sys.stderr,
        flush=True,
    )
    # Keep the CMD process alive as a no-op heartbeat. The supervised
    # gateway's lifetime is independent of this process — s6-supervise
    # restarts it on crash, and we don't want the container to exit when
    # the gateway flaps. The CMD process keeps /init alive until
    # `docker stop` sends SIGTERM, at which point /init runs stage 3
    # shutdown (which tears down the supervised gateway cleanly).
    #
    # Prefer `sleep infinity` (matches the static main-hermes service's
    # pattern in docker/s6-rc.d/main-hermes/run, and frees the Python
    # interpreter — the heartbeat is a tiny `sleep` process, not a
    # resident interpreter). But `os.execvp` does a PATH lookup for the
    # `sleep` binary and historically crashed the whole container with
    # FileNotFoundError when PATH was empty/truncated/clobbered at this
    # point — e.g. after user customizations rewrote PATH, or on minimal
    # images without `sleep` on PATH (issue #36208). Fall back to an
    # in-process block (no external binary, can't fail on PATH) so the
    # container keeps running instead of dying during boot.
    try:
        os.execvp("sleep", ["sleep", "infinity"])
    except OSError:
        # execvp only returns by raising; on success it replaces this
        # process. ENOENT (no `sleep` on PATH) and any other exec error
        # land here.
        print(
            "→ `sleep` is unavailable; keeping the s6 CMD process alive "
            "in-process until the container is stopped.",
            file=sys.stderr,
            flush=True,
        )
        _block_until_terminated()
    return True  # unreachable on the execvp success path


def _block_until_terminated() -> None:
    """Keep the s6 CMD process alive until the container is stopped.

    Fallback heartbeat for when ``os.execvp("sleep", ...)`` can't run
    (``sleep`` missing from PATH — issue #36208). Installs a SIGTERM
    handler that exits with the conventional 128+signum code so
    ``docker stop`` produces a clean, expected exit, then blocks on
    ``signal.pause()``. Falls back to ``threading.Event().wait()`` on
    platforms without ``signal.pause()`` (e.g. Windows) — although this
    path only runs inside the s6 Linux container image, the fallback
    keeps the helper safe to import and unit-test anywhere.
    """
    signal.signal(signal.SIGTERM, lambda signum, _frame: sys.exit(128 + signum))
    pause = getattr(signal, "pause", None)
    if pause is not None:
        while True:
            pause()
    else:  # pragma: no cover - non-Unix fallback, not exercised in the s6 image
        import threading

        threading.Event().wait()


def _gateway_command_inner(args):
    subcmd = getattr(args, "gateway_command", None)

    # Default to run if no subcommand
    if subcmd is None or subcmd == "run":
        if _maybe_redirect_run_to_s6_supervision(args):
            return  # unreachable; execvp doesn't return
        verbose = getattr(args, "verbose", 0)
        quiet = getattr(args, "quiet", False)
        replace = getattr(args, "replace", False)
        force = getattr(args, "force", False)
        run_gateway(verbose, quiet=quiet, replace=replace, force=force)
        return

    if subcmd == "setup":
        gateway_setup()
        return

    # Service management commands
    if subcmd == "install":
        if is_managed():
            managed_error("install gateway service (managed by NixOS)")
            return
        force = getattr(args, "force", False)
        system = getattr(args, "system", False)
        run_as_user = getattr(args, "run_as_user", None)
        if is_termux():
            print("Gateway service installation is not supported on Termux.")
            print("Run manually: hermes gateway")
            sys.exit(1)
        if supports_systemd_services():
            if is_wsl():
                print_warning(
                    "WSL detected — systemd services may not survive WSL restarts."
                )
                print_info(
                    "  Consider running in foreground instead: hermes gateway run"
                )
                print_info(
                    "  Or use tmux/screen for persistence: tmux new -s hermes 'hermes gateway run'"
                )
                print()
            # Honor CLI flags (--start-now / --no-start-now, --start-on-login /
            # --no-start-on-login).  When not provided, prompt interactively or
            # fall back to True for non-TTY / headless contexts (SSH, CI, pipes).
            non_interactive = not (hasattr(sys.stdin, "isatty") and sys.stdin.isatty())
            _sn = getattr(args, "start_now", None)
            if _sn is not None:
                start_now = _sn
            elif not non_interactive:
                start_now = prompt_yes_no("Start the gateway now after installing the service?", True)
            else:
                start_now = True

            _sol = getattr(args, "start_on_login", None)
            if _sol is not None:
                start_on_login = _sol
            elif not non_interactive:
                start_on_login = prompt_yes_no("Start the gateway automatically on login/boot with systemd?", True)
            else:
                start_on_login = True
            systemd_install(
                force=force,
                system=system,
                run_as_user=run_as_user,
                enable_on_startup=start_on_login,
                non_interactive=non_interactive,
            )
            if start_now:
                systemd_start(system=system)
        elif is_macos():
            launchd_install(force)
        elif is_windows():
            from hermes_cli import gateway_windows

            gateway_windows.install(
                force=force,
                start_now=getattr(args, 'start_now', None),
                start_on_login=getattr(args, 'start_on_login', None),
                elevated_handoff=getattr(args, 'elevated_handoff', False),
            )
        elif is_wsl():
            print("WSL detected but systemd is not running.")
            print(
                "Either enable systemd (add systemd=true to /etc/wsl.conf and restart WSL)"
            )
            print("or run the gateway in foreground mode:")
            print()
            print(
                "  hermes gateway run                              # direct foreground"
            )
            print(
                "  tmux new -s hermes 'hermes gateway run'         # persistent via tmux"
            )
            print(
                "  nohup hermes gateway run > ~/.hermes/logs/gateway.log 2>&1 &  # background"
            )
            sys.exit(1)
        elif is_container():
            # Phase 4: inside a container with s6 the gateway service is
            # auto-registered when the profile is created (and reconciled
            # at every container boot). `install` is therefore informational.
            from hermes_cli.service_manager import detect_service_manager
            if detect_service_manager() == "s6":
                print("Per-profile gateways are auto-registered when you create a profile.")
                print()
                print("  hermes profile create <name>     # creates the s6 service slot")
                print("  hermes -p <name> gateway start   # bring it up via s6")
                print("  hermes status                    # see currently-supervised gateways")
                return
            # Fallback for pre-s6 containers or other container runtimes
            # we haven't taught about supervision (Podman without our
            # /init, k8s plain runs, etc.) — the historical guidance still
            # applies.
            print("Service installation is not needed inside a Docker container.")
            print(
                "The container runtime is your service manager — use Docker restart policies instead:"
            )
            print()
            print(
                "  docker run --restart unless-stopped ...   # auto-restart on crash/reboot"
            )
            print("  docker restart <container>                # manual restart")
            print()
            print("To run the gateway: hermes gateway run")
            sys.exit(0)
        else:
            print("Service installation not supported on this platform.")
            print("Run manually: hermes gateway run")
            sys.exit(1)

    elif subcmd == "uninstall":
        if is_managed():
            managed_error("uninstall gateway service (managed by NixOS)")
            return
        system = getattr(args, "system", False)
        if is_termux():
            print(
                "Gateway service uninstall is not supported on Termux because there is no managed service to remove."
            )
            print("Stop manual runs with: hermes gateway stop")
            sys.exit(1)
        if supports_systemd_services():
            systemd_uninstall(system=system)
        elif is_macos():
            launchd_uninstall()
        elif is_windows():
            from hermes_cli import gateway_windows

            gateway_windows.uninstall()
        elif is_container():
            from hermes_cli.service_manager import detect_service_manager
            if detect_service_manager() == "s6":
                print("Per-profile gateways are auto-unregistered when you delete the profile.")
                print()
                print("  hermes profile delete <name>     # tears down the s6 service slot")
                print("  hermes -p <name> gateway stop    # stop without deleting the profile")
                return
            print("Service uninstall is not applicable inside a Docker container.")
            print("To stop the gateway, stop or remove the container:")
            print()
            print("  docker stop <container>")
            print("  docker rm <container>")
            sys.exit(0)
        else:
            print("Not supported on this platform.")
            sys.exit(1)

    elif subcmd == "start":
        system = getattr(args, "system", False)
        start_all = getattr(args, "all", False)

        # Phase 4: inside a container with s6, dispatch via the service
        # manager instead of falling through to systemd/launchd/windows.
        # `--all` isn't meaningful here (each profile has its own service
        # slot — start them individually via `hermes -p <name> gateway
        # start`), so just bring up the current profile's slot.
        if not start_all and _dispatch_via_service_manager_if_s6("start"):
            return

        if start_all and is_macos():
            try:
                launchd_start_all()
            except LaunchdAllOperationError as exc:
                print_error(str(exc))
                raise SystemExit(1) from exc
            return

        if start_all:
            # Kill all stale gateway processes across all profiles before starting
            killed = kill_gateway_processes(all_profiles=True)
            if killed:
                print(
                    f"✓ Killed {killed} stale gateway process(es) across all profiles"
                )
                _wait_for_gateway_exit(timeout=10.0, force_after=5.0)

        if is_termux():
            print(
                "Gateway service start is not supported on Termux because there is no system service manager."
            )
            print("Run manually: hermes gateway")
            sys.exit(1)
        if supports_systemd_services():
            systemd_start(system=system)
        elif is_macos():
            launchd_start()
        elif is_windows():
            from hermes_cli import gateway_windows

            gateway_windows.start()
        elif is_wsl():
            print("WSL detected but systemd is not available.")
            print("Run the gateway in foreground mode instead:")
            print()
            print(
                "  hermes gateway run                              # direct foreground"
            )
            print(
                "  tmux new -s hermes 'hermes gateway run'         # persistent via tmux"
            )
            print(
                "  nohup hermes gateway run > ~/.hermes/logs/gateway.log 2>&1 &  # background"
            )
            print()
            print(
                "To enable systemd: add systemd=true to /etc/wsl.conf and run 'wsl --shutdown' from PowerShell."
            )
            sys.exit(1)
        elif is_container():
            # Reached only when s6 ISN'T running (the early dispatch
            # above handles the s6 case). Pre-s6 containers or other
            # container runtimes that don't ship our /init get the
            # historical guidance: the gateway is the container's main
            # process, so use docker lifecycle commands.
            print("Service start is not applicable inside a Docker container.")
            print("The gateway runs as the container's main process.")
            print()
            print("  docker start <container>     # start a stopped container")
            print("  docker restart <container>   # restart a running container")
            print()
            print("Or run the gateway directly: hermes gateway run")
            sys.exit(0)
        else:
            print("Not supported on this platform.")
            sys.exit(1)

    elif subcmd == "stop":
        # Defense: refuse self-targeting gateway stop from inside the gateway.
        # Prevents agent-initiated kill loops when combined with supervisor KeepAlive.
        if os.getenv("_HERMES_GATEWAY") == "1":
            print_error(
                "Refusing to stop the gateway from inside the gateway process.\n"
                "This command was blocked to prevent restart loops.\n"
                "Use `hermes gateway stop` from a shell outside the running gateway."
            )
            sys.exit(1)

        stop_all = getattr(args, "all", False)
        system = getattr(args, "system", False)

        # Phase 4: inside a container with s6, dispatch via the service
        # manager. ``--all`` iterates every registered profile gateway
        # through s6 (otherwise it would fall through to ``pkill``,
        # which s6-supervise observes as a crash and immediately restarts).
        if stop_all and _dispatch_all_via_service_manager_if_s6("stop"):
            return
        if not stop_all and _dispatch_via_service_manager_if_s6("stop"):
            return

        if stop_all:
            # --all: fence installed launchd services, then terminate only
            # process identities captured before that mutation.
            service_count = 0
            killed = 0
            launchd_result: LaunchdStopAllResult | None = None
            captured_identities: list[GatewayProcessIdentity] = []
            terminated_identities: tuple[GatewayProcessIdentity, ...] = ()
            if supports_systemd_services() and (
                get_systemd_unit_path(system=False).exists()
                or get_systemd_unit_path(system=True).exists()
            ):
                try:
                    systemd_stop(system=system)
                    service_count = 1
                except subprocess.CalledProcessError:
                    pass
            elif is_macos():
                try:
                    # A destructive all-profile operation must never turn an
                    # incomplete process-table read into "zero processes".
                    captured_identities = find_gateway_process_identities_strict(
                        all_profiles=True,
                        include_restart_managers=False,
                    )
                    launchd_result = _coerce_launchd_stop_all_result(
                        launchd_stop_all()
                    )
                    if launchd_result.failures or not launchd_result.sweep_safe:
                        details = ", ".join(launchd_result.failures) or (
                            "post-fence launchd state was not proven safe"
                        )
                        print_error(
                            "Could not durably fence every installed launchd gateway: "
                            + details
                        )
                        print_error("No global process sweep was performed; inspect the listed labels.")
                        raise SystemExit(1)
                    service_count = launchd_result.stopped
                    terminated_identities = terminate_gateway_process_identities_strict(
                        captured_identities
                    )
                    final_identities = find_gateway_process_identities_strict(
                        all_profiles=True,
                        include_restart_managers=False,
                    )
                    captured_keys = {item.identity_key() for item in captured_identities}
                    new_identities = [
                        item
                        for item in final_identities
                        if item.identity_key() not in captured_keys
                    ]
                    if new_identities:
                        raise GatewayProcessTerminationError(
                            [
                                "new gateway process appeared after stop; "
                                "not signalled: "
                                + ", ".join(
                                    f"PID {item.pid}" for item in new_identities
                                )
                            ]
                        )
                    if final_identities:
                        raise GatewayProcessTerminationError(
                            [
                                "captured gateway process remained after stop: "
                                + ", ".join(
                                    f"PID {item.pid}" for item in final_identities
                                )
                            ]
                        )
                except (
                    GatewayProcessEnumerationError,
                    GatewayProcessTerminationError,
                    LaunchdInventoryError,
                    LaunchdAllOperationError,
                ) as exc:
                    print_error(
                        "Could not safely stop all macOS gateway processes: " + str(exc)
                    )
                    raise SystemExit(1) from exc
            elif is_windows():
                from hermes_cli import gateway_windows

                if gateway_windows.is_installed():
                    try:
                        gateway_windows.stop()
                        service_count = 1
                    except (subprocess.CalledProcessError, RuntimeError):
                        pass
            if is_macos():
                # The macOS branch performs a strict, post-barrier termination
                # above. Other platforms retain their historical best-effort
                # process cleanup behavior.
                pass
            else:
                killed = kill_gateway_processes(all_profiles=True)
            if is_macos() and launchd_result is not None:
                service_labels = [
                    target.label
                    for target in launchd_result.fenced
                    if target.preflight_snapshot is not None
                    and bool(target.preflight_snapshot.registered)
                ]
                service_pairs = {
                    (target.preflight_snapshot.pid, target.preflight_snapshot.start_time)
                    for target in launchd_result.fenced
                    if target.preflight_snapshot is not None
                    and target.preflight_snapshot.pid is not None
                }
                residual_terminated = [
                    item
                    for item in terminated_identities
                    if (item.pid, item.start_time) not in service_pairs
                ]
                if service_labels:
                    print(
                        f"✓ Stopped {len(service_labels)} launchd service label(s): "
                        + ", ".join(service_labels)
                    )
                if residual_terminated:
                    print(
                        f"✓ Stopped {len(residual_terminated)} residual gateway "
                        "process(es)"
                    )
                if not service_labels and not residual_terminated:
                    print("✗ No gateway processes found")
            else:
                total = killed + service_count
                if total:
                    print(f"✓ Stopped {total} gateway process(es) across all profiles")
                else:
                    print("✗ No gateway processes found")
            if is_macos() and launchd_result is not None and launchd_result.failures:
                print_error(
                    "Gateway processes were stopped, but these labels were not durably fenced: "
                    + ", ".join(launchd_result.failures)
                )
                raise SystemExit(1)
        else:
            # Default: stop only the current profile's gateway
            service_available = False
            if supports_systemd_services() and (
                get_systemd_unit_path(system=False).exists()
                or get_systemd_unit_path(system=True).exists()
            ):
                try:
                    systemd_stop(system=system)
                    service_available = True
                except subprocess.CalledProcessError:
                    pass
            elif is_macos() and get_launchd_plist_path().exists():
                try:
                    launchd_stop()
                    service_available = True
                except LaunchdFenceError as exc:
                    print_error(str(exc))
                    raise SystemExit(1)
                except subprocess.CalledProcessError:
                    pass
            elif is_windows():
                from hermes_cli import gateway_windows

                if gateway_windows.is_installed():
                    try:
                        gateway_windows.stop()
                        service_available = True
                    except (subprocess.CalledProcessError, RuntimeError):
                        pass

            if not service_available:
                # No systemd/launchd/schtasks service — use profile-scoped PID file
                if stop_profile_gateway():
                    print("✓ Stopped gateway for this profile")
                else:
                    print("✗ No gateway running for this profile")
            else:
                print(f"✓ Stopped {get_service_name()} service")

    elif subcmd == "restart":
        # Defense: refuse self-targeting gateway restart from inside the gateway.
        # Prevents agent-initiated kill loops when combined with supervisor KeepAlive.
        if os.getenv("_HERMES_GATEWAY") == "1":
            print_error(
                "Refusing to restart the gateway from inside the gateway process.\n"
                "This command was blocked to prevent restart loops.\n"
                "Use `hermes gateway restart` from a shell outside the running gateway."
            )
            sys.exit(1)

        # Try service first, fall back to killing and restarting
        service_available = False
        system = getattr(args, "system", False)
        restart_all = getattr(args, "all", False)
        service_configured = False

        # Phase 4: inside a container with s6, dispatch via the service
        # manager (s6-svc -t restarts the supervised process). ``--all``
        # iterates every registered profile gateway through s6; without
        # this it would fall through to ``pkill``, which s6-supervise
        # would observe as a crash and immediately restart anyway.
        if restart_all and _dispatch_all_via_service_manager_if_s6("restart"):
            return
        if not restart_all and _dispatch_via_service_manager_if_s6("restart"):
            return

        if restart_all and is_macos():
            try:
                launchd_restart_all()
            except LaunchdAllOperationError as exc:
                print_error(str(exc))
                raise SystemExit(1) from exc
            return

        if restart_all:
            # --all: stop every gateway process across all profiles, then start fresh
            service_count = 0
            launchd_result: LaunchdStopAllResult | None = None
            if supports_systemd_services() and (
                get_systemd_unit_path(system=False).exists()
                or get_systemd_unit_path(system=True).exists()
            ):
                try:
                    systemd_stop(system=system)
                    service_count = 1
                except subprocess.CalledProcessError:
                    pass
            elif is_macos():
                try:
                    launchd_result = _coerce_launchd_stop_all_result(
                        launchd_stop_all()
                    )
                    if launchd_result.failures and not launchd_result.sweep_safe:
                        rollback_failures = launchd_release_all_fences(launchd_result)
                        print_error(
                            "Could not durably fence every installed launchd gateway: "
                            + ", ".join(launchd_result.failures)
                        )
                        if rollback_failures:
                            print_error(
                                "Restart also could not release these temporary fences: "
                                + ", ".join(rollback_failures)
                            )
                        print_error("Restart aborted before sweeping processes.")
                        raise SystemExit(1)
                    service_count = launchd_result.stopped
                except subprocess.CalledProcessError:
                    pass
            elif is_windows():
                from hermes_cli import gateway_windows

                if gateway_windows.is_installed():
                    try:
                        gateway_windows.stop()
                        service_count = 1
                    except (subprocess.CalledProcessError, RuntimeError):
                        pass
            killed = kill_gateway_processes(all_profiles=True)
            total = killed + service_count
            if total:
                print(f"✓ Stopped {total} gateway process(es) across all profiles")
            _wait_for_gateway_exit(timeout=10.0, force_after=5.0)

            # Start the current profile's service fresh
            print("Starting gateway...")
            if supports_systemd_services() and (
                get_systemd_unit_path(system=False).exists()
                or get_systemd_unit_path(system=True).exists()
            ):
                systemd_start(system=system)
            elif is_macos() and launchd_result is not None and launchd_result.fenced:
                restored_count, restore_failures = launchd_restore_all(launchd_result)
                if restored_count:
                    print(
                        f"✓ Restarted {restored_count} launchd gateway profile(s)"
                    )
                all_failures = list(launchd_result.failures) + restore_failures
                if all_failures:
                    print_error(
                        "Restart left these launchd labels stranded: "
                        + ", ".join(all_failures)
                    )
                    raise SystemExit(1)
            elif is_macos() and get_launchd_plist_path().exists():
                # Compatibility for callers that monkeypatch the historical
                # tuple return from launchd_stop_all().
                launchd_start()
            elif is_windows():
                from hermes_cli import gateway_windows

                # On Windows, even without a registered Scheduled Task / Startup
                # entry, gateway_windows.start() uses the safe detached
                # pythonw.exe launcher.  Do not fall back to run_gateway() here:
                # when invoked from a gateway-hosted agent/tool call, foreground
                # run_gateway() is tied to the very gateway process we just
                # stopped and can die before the replacement is stable.
                gateway_windows.start()
            else:
                run_gateway(verbose=0)
            return

        if supports_systemd_services() and (
            get_systemd_unit_path(system=False).exists()
            or get_systemd_unit_path(system=True).exists()
        ):
            service_configured = True
            try:
                systemd_restart(system=system)
                service_available = True
            except subprocess.CalledProcessError:
                pass
        elif is_macos() and get_launchd_plist_path().exists():
            service_configured = True
            try:
                launchd_restart()
                service_available = True
            except subprocess.CalledProcessError:
                pass
        elif is_windows():
            from hermes_cli import gateway_windows

            # Prefer the Windows-specific restart path: it supports both
            # registered Scheduled Task / Startup installs and no-service
            # detached restarts.  In the normal successful Telegram-triggered
            # restart flow, this avoids the generic foreground run_gateway()
            # path that can be reaped with the old gateway process.  If the
            # Windows backend raises, intentionally preserve the existing
            # generic failure fallback below.
            service_configured = gateway_windows.is_installed()
            try:
                gateway_windows.restart()
                return
            except (subprocess.CalledProcessError, RuntimeError, OSError):
                pass

        if not service_available:
            # systemd/launchd restart failed — check if linger is the issue
            if supports_systemd_services():
                linger_ok, _detail = get_systemd_linger_status()
                if linger_ok is not True:
                    import getpass

                    _username = getpass.getuser()
                    print()
                    print(
                        "⚠ Cannot restart gateway as a service — linger is not enabled."
                    )
                    print(
                        "  The gateway user service requires linger to function on headless servers."
                    )
                    print()
                    print(f"  Run:  sudo loginctl enable-linger {_username}")
                    print()
                    print("  Then restart the gateway:")
                    print("    hermes gateway restart")
                    return

            if service_configured:
                print()
                print("✗ Gateway service restart failed.")
                print(
                    "  The service definition exists, but the service manager did not recover it."
                )
                print("  Fix the service, then retry: hermes gateway start")
                sys.exit(1)

            # Manual restart: stop only this profile's gateway
            if stop_profile_gateway():
                print("✓ Stopped gateway for this profile")

            _wait_for_gateway_exit(timeout=10.0, force_after=5.0)

            # Start fresh
            print("Starting gateway...")
            run_gateway(verbose=0)

    elif subcmd == "status":
        deep = getattr(args, "deep", False)
        full = getattr(args, "full", False)
        system = getattr(args, "system", False)
        snapshot = get_gateway_runtime_snapshot(system=system)

        # Check for service first
        _windows_service_installed = False
        if is_windows():
            from hermes_cli import gateway_windows

            _windows_service_installed = gateway_windows.is_installed()
        if supports_systemd_services() and (
            get_systemd_unit_path(system=False).exists()
            or get_systemd_unit_path(system=True).exists()
        ):
            systemd_status(deep, system=system, full=full)
            _print_gateway_process_mismatch(snapshot)
        elif is_macos() and get_launchd_plist_path().exists():
            launchd_status(deep)
            _print_gateway_process_mismatch(snapshot)
        elif _windows_service_installed:
            from hermes_cli import gateway_windows

            gateway_windows.status(deep=deep)
            _print_gateway_process_mismatch(snapshot)
        else:
            # Check for manually running processes
            pids = list(snapshot.gateway_pids)
            if pids:
                print(f"✓ Gateway is running (PID: {', '.join(map(str, pids))})")
                print("  (Running manually, not as a system service)")
                runtime_lines = _runtime_health_lines()
                if runtime_lines:
                    print()
                    print("Recent gateway health:")
                    for line in runtime_lines:
                        print(f"  {line}")
                print()
                if is_termux():
                    print("Termux note:")
                    print("  Android may stop background jobs when Termux is suspended")
                elif is_wsl():
                    print("WSL note:")
                    print(
                        "  The gateway is running in foreground/manual mode (recommended for WSL)."
                    )
                    print(
                        "  Use tmux or screen for persistence across terminal closes."
                    )
                elif is_windows():
                    print(
                        "To install as a Windows Scheduled Task (auto-start on login):"
                    )
                    print("  hermes gateway install")
                else:
                    print("To install as a service:")
                    print("  hermes gateway install")
                    print("  sudo hermes gateway install --system")
            else:
                print("✗ Gateway is not running")
                runtime_lines = _runtime_health_lines()
                if runtime_lines:
                    print()
                    print("Recent gateway health:")
                    for line in runtime_lines:
                        print(f"  {line}")
                print()
                print("To start:")
                print("  hermes gateway run      # Run in foreground")
                if is_termux():
                    print(
                        "  nohup hermes gateway run > ~/.hermes/logs/gateway.log 2>&1 &  # Best-effort background start"
                    )
                elif is_wsl():
                    print(
                        "  tmux new -s hermes 'hermes gateway run'         # persistent via tmux"
                    )
                    print(
                        "  nohup hermes gateway run > ~/.hermes/logs/gateway.log 2>&1 &  # background"
                    )
                elif is_windows():
                    print(
                        "  hermes gateway install  # Install as Windows Scheduled Task (auto-start on login)"
                    )
                else:
                    print("  hermes gateway install  # Install as user service")
                    print(
                        "  sudo hermes gateway install --system  # Install as boot-time system service"
                    )

        # Show other profiles' gateway status for multi-profile awareness
        _print_other_profiles_gateway_status()

    elif subcmd == "list":
        _gateway_list()

    elif subcmd == "migrate-legacy":
        # Stop, disable, and remove legacy Hermes gateway unit files from
        # pre-rename installs (e.g. hermes.service). Profile units and
        # unrelated third-party services are never touched.
        dry_run = getattr(args, "dry_run", False)
        yes = getattr(args, "yes", False)
        if not supports_systemd_services() and not is_macos():
            print("Legacy unit migration only applies to systemd-based Linux hosts.")
            return
        remove_legacy_hermes_units(interactive=not yes, dry_run=dry_run)
