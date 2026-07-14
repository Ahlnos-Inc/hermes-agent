"""Cross-profile capacity arbitration for local model endpoints.

The dispatcher cannot reserve local-model capacity because provider fallback is
resolved later, at request time.  This module therefore arbitrates immediately
before a local provider call.  All profiles share one durable queue under the
default Hermes root; queue mutations are serialized with an OS file lock.

Only opaque endpoint digests and non-secret scheduling metadata are persisted.
The provider URL (which may contain credentials) is never written or logged.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import socket
import stat
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

import psutil

from hermes_constants import get_default_hermes_root

logger = logging.getLogger(__name__)

_STATE_VERSION = 2
_LOCK_POLL_SECONDS = 0.025
_QUEUE_POLL_SECONDS = 0.05
_DEFAULT_MAX_WAIT_SECONDS = 120.0
_DEFAULT_LEASE_TTL_SECONDS = 1800.0
_HEARTBEAT_INTERVAL_SECONDS = 10.0
_HEARTBEAT_WINDOW_SECONDS = 60.0
_OWNER_BIRTH_TOLERANCE_SECONDS = 0.01
_PRIORITY_AGING_SECONDS_PER_POINT = 2.0


class LocalModelLeaseError(RuntimeError):
    """Base class for local-model capacity arbitration failures."""


class LocalModelLeaseTimeout(TimeoutError, LocalModelLeaseError):
    """Capacity could not be acquired before the caller's deadline."""


class LocalModelLeaseQuarantined(LocalModelLeaseTimeout):
    """A prior request exceeded its lease while its owner is still alive."""


class LocalModelLeaseStateError(LocalModelLeaseTimeout):
    """Durable state is invalid; fail closed but permit provider failover."""


class LocalModelLeaseReleaseError(LocalModelLeaseError):
    """The owner could not durably relinquish its capacity slot."""


def _canonical_base_url(base_url: str) -> str:
    """Normalize an endpoint for capacity identity without retaining secrets."""
    raw = base_url.strip()
    try:
        parsed = urlsplit(raw if "://" in raw else f"http://{raw}")
        host = (parsed.hostname or "").casefold()
        if not host:
            return raw.rstrip("/").casefold()
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        try:
            port = parsed.port
        except ValueError:
            port = None
        netloc = f"{host}:{port}" if port is not None else host
        return urlunsplit((
            parsed.scheme.casefold() or "http",
            netloc,
            parsed.path.rstrip("/"),
            "",
            "",
        ))
    except Exception:
        return raw.rstrip("/").casefold()


def local_model_lease_key(provider: str, model: str, base_url: str) -> str:
    """Return an opaque stable capacity key for a concrete local route."""
    identity = json.dumps(
        {
            "provider": provider.strip().casefold(),
            "model": model.strip(),
            "base_url": _canonical_base_url(base_url),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _priority_from_env() -> int:
    raw = os.environ.get("HERMES_KANBAN_PRIORITY", "0").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 0
    return max(-1_000_000_000, min(1_000_000_000, value))


def _max_wait_from_env() -> float:
    raw = os.environ.get(
        "HERMES_LOCAL_MODEL_LEASE_MAX_WAIT_SECONDS",
        str(_DEFAULT_MAX_WAIT_SECONDS),
    )
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_WAIT_SECONDS
    if not value > 0 or value == float("inf"):
        return _DEFAULT_MAX_WAIT_SECONDS
    return value


def _lease_ttl_from_env() -> float:
    raw = os.environ.get(
        "HERMES_LOCAL_MODEL_LEASE_TTL_SECONDS",
        str(_DEFAULT_LEASE_TTL_SECONDS),
    )
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_LEASE_TTL_SECONDS
    if not value > 0 or value == float("inf"):
        return _DEFAULT_LEASE_TTL_SECONDS
    return value


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        process = psutil.Process(pid)
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        return True
    except OSError:
        # Fail conservatively: retaining a stale lease is safer than allowing
        # concurrent inference when process liveness cannot be established.
        logger.warning("Unable to attest local-model lease PID liveness")
        return True


def _process_started_at(pid: int) -> float:
    """Return a PID-reuse-resistant process birth identity."""
    try:
        return float(psutil.Process(pid).create_time())
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError) as exc:
        raise LocalModelLeaseStateError(
            "Unable to attest local-model lease owner process identity"
        ) from exc


def _same_process_birth(pid: int, expected: float) -> bool:
    """Conservatively compare a live PID with its persisted birth identity."""
    try:
        actual = float(psutil.Process(pid).create_time())
    except psutil.NoSuchProcess:
        return False
    except (psutil.AccessDenied, OSError):
        # Expiry remains the cross-host/access-denied recovery bound. Do not
        # guess that a process is stale merely because it cannot be inspected.
        return True
    return abs(actual - float(expected)) <= _OWNER_BIRTH_TOLERANCE_SECONDS


def _default_state() -> dict:
    return {"version": _STATE_VERSION, "active": None, "waiters": []}


def _valid_identity(value: object, *, maximum: int) -> bool:
    return isinstance(value, str) and 0 < len(value) <= maximum


def _valid_number(value: object, *, positive: bool = False) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and (not positive or float(value) > 0)
    )


def _valid_active(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    return (
        _valid_identity(value.get("token"), maximum=128)
        and isinstance(value.get("pid"), int)
        and value["pid"] > 0
        and _valid_identity(value.get("host"), maximum=255)
        and _valid_number(value.get("process_started_at"), positive=True)
        and isinstance(value.get("priority"), int)
        and not isinstance(value.get("priority"), bool)
        and _valid_number(value.get("acquired_at"), positive=True)
        and _valid_number(value.get("heartbeat_at"), positive=True)
        and _valid_number(value.get("expires_at"), positive=True)
        and _valid_number(value.get("max_expires_at"), positive=True)
        and value["acquired_at"] <= value["heartbeat_at"]
        and value["heartbeat_at"] <= value["expires_at"]
        and value["expires_at"] <= value["max_expires_at"]
        and (value.get("task_id") is None or isinstance(value.get("task_id"), str))
        and (value.get("run_id") is None or isinstance(value.get("run_id"), str))
    )


def _valid_waiter(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    return (
        _valid_identity(value.get("token"), maximum=128)
        and isinstance(value.get("pid"), int)
        and value["pid"] > 0
        and _valid_identity(value.get("host"), maximum=255)
        and _valid_number(value.get("process_started_at"), positive=True)
        and isinstance(value.get("priority"), int)
        and not isinstance(value.get("priority"), bool)
        and isinstance(value.get("created_at"), int)
        and not isinstance(value.get("created_at"), bool)
        and value["created_at"] > 0
        and _valid_number(value.get("expires_at"), positive=True)
        and (value.get("task_id") is None or isinstance(value.get("task_id"), str))
        and (value.get("run_id") is None or isinstance(value.get("run_id"), str))
    )


def _read_state(path: Path, *, dir_fd: int | None = None) -> dict:
    try:
        if dir_fd is None:
            payload = path.read_text(encoding="utf-8")
        else:
            flags = os.O_RDONLY
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(path.name, flags, dir_fd=dir_fd)
            try:
                with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                    descriptor = -1
                    payload = handle.read()
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
        raw = json.loads(payload)
    except FileNotFoundError:
        return _default_state()
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LocalModelLeaseStateError(
            f"local-model lease state is unreadable ({path.name})"
        ) from exc
    active = raw.get("active") if isinstance(raw, dict) else None
    waiters = raw.get("waiters") if isinstance(raw, dict) else None
    valid_shape = (
        isinstance(raw, dict)
        and raw.get("version") == _STATE_VERSION
        and (active is None or _valid_active(active))
        and isinstance(waiters, list)
        and all(_valid_waiter(item) for item in waiters)
    )
    if valid_shape:
        tokens = [item["token"] for item in waiters]
        if active is not None:
            tokens.append(active["token"])
        valid_shape = len(tokens) == len(set(tokens))
    if not valid_shape:
        raise LocalModelLeaseStateError(
            f"local-model lease state has an unsupported shape ({path.name})"
        )
    return raw


def _atomic_write_state(
    path: Path,
    state: dict,
    *,
    dir_fd: int | None = None,
) -> None:
    payload = json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n"
    temp_name = f".{path.name}.{uuid.uuid4().hex}.tmp"
    if dir_fd is None:
        fd, raw_temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temp_path = Path(raw_temp_name)
    else:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(temp_name, flags, 0o600, dir_fd=dir_fd)
        temp_path = None
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if dir_fd is None:
            assert temp_path is not None
            os.replace(temp_path, path)
            try:
                parent_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(parent_fd)
                finally:
                    os.close(parent_fd)
            except OSError:
                pass
        else:
            os.replace(
                temp_name,
                path.name,
                src_dir_fd=dir_fd,
                dst_dir_fd=dir_fd,
            )
            os.fsync(dir_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        if dir_fd is None:
            assert temp_path is not None
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
        else:
            try:
                os.unlink(temp_name, dir_fd=dir_fd)
            except FileNotFoundError:
                pass


class _StateLock:
    """Deadline-aware cross-process lock for short state transactions."""

    def __init__(
        self,
        path: Path,
        *,
        deadline_monotonic: float,
        cancelled: Callable[[], bool],
        dir_fd: int | None = None,
    ) -> None:
        self.path = path
        self.deadline_monotonic = deadline_monotonic
        self.cancelled = cancelled
        self.dir_fd = dir_fd
        self._handle = None

    def __enter__(self):
        if self.dir_fd is None:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                os.chmod(self.path.parent, 0o700)
            except OSError:
                pass
            self._handle = open(self.path, "a+b")
        else:
            flags = os.O_CREAT | os.O_RDWR
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            lock_fd = os.open(self.path.name, flags, 0o600, dir_fd=self.dir_fd)
            self._handle = os.fdopen(lock_fd, "a+b")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        while True:
            if self.cancelled():
                self._close()
                raise InterruptedError(
                    "Agent interrupted while waiting for local-model capacity"
                )
            if time.monotonic() >= self.deadline_monotonic:
                self._close()
                raise LocalModelLeaseTimeout(
                    "Local-model capacity lock was not available before deadline"
                )
            try:
                if os.name == "nt":
                    import msvcrt

                    self._handle.seek(0)
                    if self._handle.read(1) == b"":
                        self._handle.write(b"0")
                        self._handle.flush()
                    self._handle.seek(0)
                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except (BlockingIOError, OSError):
                time.sleep(_LOCK_POLL_SECONDS)

    def _close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def __exit__(self, exc_type, exc, tb):
        if self._handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._close()


@dataclass
class LocalModelCapacityLease:
    """An acquired, renewable capacity slot with exact-owner release."""

    _state_path: Path
    _lock_path: Path
    _token: str
    _pid: int
    _host: str
    _process_started_at: float
    _max_expires_at: float
    _dir_fd: int | None = None
    _released: bool = False
    _heartbeat_stop: threading.Event = field(
        default_factory=threading.Event,
        init=False,
        repr=False,
    )
    _heartbeat_thread: threading.Thread | None = field(
        default=None,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"local-model-lease-{self._token[:8]}",
            daemon=True,
        )
        self._heartbeat_thread.start()

    def _owns(self, active: object) -> bool:
        return (
            isinstance(active, dict)
            and active.get("token") == self._token
            and active.get("pid") == self._pid
            and active.get("host") == self._host
            and active.get("process_started_at") == self._process_started_at
        )

    def _heartbeat_once(self) -> bool:
        """Renew this exact owner without exceeding the attempt-derived cap."""
        now = time.time()
        with _StateLock(
            self._lock_path,
            deadline_monotonic=time.monotonic() + 2.0,
            cancelled=lambda: self._heartbeat_stop.is_set(),
            dir_fd=self._dir_fd,
        ):
            state = _read_state(self._state_path, dir_fd=self._dir_fd)
            active = state.get("active")
            if not self._owns(active):
                return False
            # Once expired, an old owner may not resurrect itself. The durable
            # record becomes a fail-fast route quarantine until this exact
            # owner releases or its process is proven dead. Reclaiming merely
            # because a deadline elapsed could overlap a still-hung transport.
            if float(active["expires_at"]) <= now or self._max_expires_at <= now:
                return False
            active["heartbeat_at"] = now
            active["expires_at"] = min(
                self._max_expires_at,
                now + _HEARTBEAT_WINDOW_SECONDS,
            )
            _atomic_write_state(self._state_path, state, dir_fd=self._dir_fd)
            return True

    def _heartbeat_loop(self) -> None:
        while not self._heartbeat_stop.wait(_HEARTBEAT_INTERVAL_SECONDS):
            try:
                if not self._heartbeat_once():
                    self._heartbeat_stop.set()
                    return
            except InterruptedError:
                return
            except Exception:
                # Do not extend in-memory on failure. The durable expires_at is
                # the safety bound and another waiter may reclaim after it.
                logger.exception("Failed to heartbeat local-model capacity lease")

    def _stop_heartbeat(self) -> None:
        self._heartbeat_stop.set()
        thread = self._heartbeat_thread
        if (
            thread is not None
            and thread is not threading.current_thread()
            and thread.is_alive()
        ):
            thread.join(timeout=2.5)

    def release(self) -> None:
        """Durably clear this exact owner, or raise so the caller can retry."""
        if self._released:
            return
        self._stop_heartbeat()
        try:
            with _StateLock(
                self._lock_path,
                deadline_monotonic=time.monotonic() + 2.0,
                cancelled=lambda: False,
                dir_fd=self._dir_fd,
            ):
                state = _read_state(self._state_path, dir_fd=self._dir_fd)
                active = state.get("active")
                if self._owns(active):
                    state["active"] = None
                    _atomic_write_state(
                        self._state_path,
                        state,
                        dir_fd=self._dir_fd,
                    )
                elif isinstance(active, dict) and active.get("token") == self._token:
                    # Same token with a different owner identity is corrupt,
                    # not authority to clear a possibly-successor record.
                    raise LocalModelLeaseStateError(
                        "local-model lease token owner identity changed"
                    )
                # None or a different token means expiry/reclamation already
                # ended our ownership. Exact compare prevents deleting the
                # successor that acquired after our lease expired.
            self._released = True
            if self._dir_fd is not None:
                os.close(self._dir_fd)
                self._dir_fd = None
        except LocalModelLeaseError:
            raise
        except Exception as exc:
            raise LocalModelLeaseReleaseError(
                "Failed to durably release local-model capacity lease"
            ) from exc

    def __enter__(self) -> "LocalModelCapacityLease":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self.release()
        except Exception:
            if exc_type is None:
                raise
            # Preserve an exception raised by the provider/body. The release
            # remains retryable and durable expiry prevents a permanent wedge.
            logger.exception(
                "Failed to release local-model capacity lease while unwinding"
            )


def _prune_stale(
    state: dict,
    *,
    hostname: str,
    now_wall: float,
    pid_alive: Callable[[int], bool],
    same_process_birth: Callable[[int, float], bool],
) -> bool:
    changed = False
    active = state.get("active")
    if isinstance(active, dict):
        stale_local_owner = False
        if active.get("host") == hostname:
            active_pid = int(active["pid"])
            stale_local_owner = not pid_alive(active_pid) or not same_process_birth(
                active_pid,
                float(active["process_started_at"]),
            )
        if stale_local_owner:
            state["active"] = None
            changed = True

    live_waiters = []
    for waiter in state.get("waiters", []):
        expires_at = float(waiter["expires_at"])
        stale_local_waiter = False
        if waiter.get("host") == hostname:
            waiter_pid = int(waiter["pid"])
            stale_local_waiter = not pid_alive(waiter_pid) or not same_process_birth(
                waiter_pid,
                float(waiter["process_started_at"]),
            )
        if expires_at <= now_wall or stale_local_waiter:
            changed = True
            continue
        live_waiters.append(waiter)
    if len(live_waiters) != len(state.get("waiters", [])):
        state["waiters"] = live_waiters
    return changed


def _waiter_order_key(waiter: dict, *, now_ns: int) -> tuple[float, int, str]:
    """Priority FIFO with aging so bounded low-priority work cannot starve."""
    created_at = int(waiter.get("created_at", 0))
    age_seconds = max(0.0, (now_ns - created_at) / 1_000_000_000)
    effective_priority = int(waiter.get("priority", 0)) + (
        age_seconds / _PRIORITY_AGING_SECONDS_PER_POINT
    )
    return (
        -effective_priority,
        created_at,
        str(waiter.get("token", "")),
    )


def _open_pinned_lease_directory(root: Path, key: str) -> tuple[Path, int | None]:
    """Create and pin the lease directory without following internal links."""
    lease_dir = root / "shared" / "local-model-leases" / key
    if os.name == "nt" or not hasattr(os, "O_DIRECTORY"):
        for candidate in (lease_dir.parent.parent, lease_dir.parent, lease_dir):
            if candidate.is_symlink():
                raise LocalModelLeaseStateError(
                    "local-model lease path contains a symbolic link"
                )
        lease_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        return lease_dir, None

    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        current = os.open(root, flags)
    except OSError as exc:
        raise LocalModelLeaseStateError(
            "local-model lease root cannot be pinned safely"
        ) from exc
    try:
        for component in ("shared", "local-model-leases", key):
            try:
                os.mkdir(component, 0o700, dir_fd=current)
            except FileExistsError:
                pass
            try:
                child = os.open(component, flags, dir_fd=current)
            except OSError as exc:
                try:
                    entry_info = os.stat(
                        component,
                        dir_fd=current,
                        follow_symlinks=False,
                    )
                except OSError:
                    entry_info = None
                if entry_info is not None and stat.S_ISLNK(entry_info.st_mode):
                    raise LocalModelLeaseStateError(
                        "local-model lease path contains a symbolic link"
                    ) from exc
                raise LocalModelLeaseStateError(
                    "local-model lease path cannot be pinned safely"
                ) from exc
            info = os.fstat(child)
            if not stat.S_ISDIR(info.st_mode) or (
                hasattr(os, "getuid") and info.st_uid != os.getuid()
            ):
                os.close(child)
                raise LocalModelLeaseStateError(
                    "local-model lease directory has unsafe ownership"
                )
            os.fchmod(child, 0o700)
            os.close(current)
            current = child
        return lease_dir, current
    except BaseException:
        os.close(current)
        raise


def acquire_local_model_capacity(
    *,
    provider: str,
    model: str,
    base_url: str,
    deadline_monotonic: float | None,
    cancelled: Callable[[], bool],
    lease_deadline_monotonic: float | None = None,
    root: Path | None = None,
    priority: int | None = None,
    pid_alive: Callable[[int], bool] = _pid_alive,
    process_started_at: Callable[[int], float] = _process_started_at,
    same_process_birth: Callable[[int, float], bool] = _same_process_birth,
) -> LocalModelCapacityLease:
    """Wait for and acquire the single slot for a concrete local route.

    Higher numeric priorities win among queued waiters; equal priorities are
    FIFO. Capacity is deliberately non-preemptive once granted. The caller's
    absolute deadline always wins over the independent queue-wait safety cap.
    """
    now_monotonic = time.monotonic()
    max_wait_deadline = now_monotonic + _max_wait_from_env()
    effective_deadline = (
        min(deadline_monotonic, max_wait_deadline)
        if deadline_monotonic is not None
        else max_wait_deadline
    )
    if effective_deadline <= now_monotonic:
        raise LocalModelLeaseTimeout(
            "Local-model capacity was not available before attempt deadline"
        )

    key = local_model_lease_key(provider, model, base_url)
    lease_dir, lease_dir_fd = _open_pinned_lease_directory(
        root or get_default_hermes_root(),
        key,
    )
    state_path = lease_dir / "state.json"
    lock_path = lease_dir / "state.lock"
    if state_path.is_symlink() or lock_path.is_symlink():
        raise LocalModelLeaseStateError(
            "local-model lease state path contains a symbolic link"
        )

    token = uuid.uuid4().hex
    hostname = socket.gethostname()
    pid = os.getpid()
    owner_process_started_at = process_started_at(pid)
    requested_priority = _priority_from_env() if priority is None else int(priority)
    created_at = time.time_ns()
    expires_at = time.time() + max(0.0, effective_deadline - now_monotonic)
    lease_budget_deadline = (
        lease_deadline_monotonic
        if lease_deadline_monotonic is not None
        else deadline_monotonic
    )
    lease_deadline = (
        lease_budget_deadline
        if lease_budget_deadline is not None
        else now_monotonic + _lease_ttl_from_env()
    )
    max_lease_expires_at = time.time() + max(
        0.0,
        lease_deadline - time.monotonic(),
    )
    waiter = {
        "token": token,
        "pid": pid,
        "host": hostname,
        "process_started_at": owner_process_started_at,
        "priority": requested_priority,
        "created_at": created_at,
        "expires_at": expires_at,
        "task_id": os.environ.get("HERMES_KANBAN_TASK") or None,
        "run_id": os.environ.get("HERMES_KANBAN_RUN_ID") or None,
    }
    registered = False
    acquired = False
    wait_started = time.monotonic()

    try:
        while True:
            if cancelled():
                raise InterruptedError(
                    "Agent interrupted while waiting for local-model capacity"
                )
            if time.monotonic() >= effective_deadline:
                raise LocalModelLeaseTimeout(
                    "Local-model capacity was not available before deadline"
                )
            with _StateLock(
                lock_path,
                deadline_monotonic=effective_deadline,
                cancelled=cancelled,
                dir_fd=lease_dir_fd,
            ):
                state = _read_state(state_path, dir_fd=lease_dir_fd)
                changed = _prune_stale(
                    state,
                    hostname=hostname,
                    now_wall=time.time(),
                    pid_alive=pid_alive,
                    same_process_birth=same_process_birth,
                )
                active = state.get("active")
                if (
                    isinstance(active, dict)
                    and float(active["expires_at"]) <= time.time()
                ):
                    # The owner is not proven dead, so its underlying provider
                    # call may still be consuming local capacity. Do not queue
                    # behind it or admit overlapping work: fail over promptly.
                    if changed:
                        _atomic_write_state(
                            state_path,
                            state,
                            dir_fd=lease_dir_fd,
                        )
                    raise LocalModelLeaseQuarantined(
                        "Local-model route is quarantined by an unsettled prior attempt"
                    )
                if not registered:
                    state["waiters"].append(waiter)
                    registered = True
                    changed = True
                if state.get("active") is None:
                    waiters = state["waiters"]
                    order_now_ns = time.time_ns()
                    winner = min(
                        waiters,
                        key=lambda item: _waiter_order_key(
                            item,
                            now_ns=order_now_ns,
                        ),
                    )
                    if winner.get("token") == token:
                        acquired_at = time.time()
                        if max_lease_expires_at <= acquired_at:
                            # The attempt budget elapsed while this process was
                            # inside the state transaction. Leave the waiter to
                            # the exception cleanup path; never persist an
                            # already-invalid active lease.
                            raise LocalModelLeaseTimeout(
                                "Local-model attempt deadline elapsed during admission"
                            )
                        active_expires_at = min(
                            max_lease_expires_at,
                            acquired_at + _HEARTBEAT_WINDOW_SECONDS,
                        )
                        state["waiters"] = [
                            item for item in waiters if item.get("token") != token
                        ]
                        state["active"] = {
                            "token": token,
                            "pid": pid,
                            "host": hostname,
                            "process_started_at": owner_process_started_at,
                            "priority": requested_priority,
                            "acquired_at": acquired_at,
                            "heartbeat_at": acquired_at,
                            "expires_at": active_expires_at,
                            "max_expires_at": max_lease_expires_at,
                            "task_id": waiter["task_id"],
                            "run_id": waiter["run_id"],
                        }
                        acquired = True
                        changed = True
                if changed:
                    _atomic_write_state(
                        state_path,
                        state,
                        dir_fd=lease_dir_fd,
                    )
            if acquired:
                waited = time.monotonic() - wait_started
                logger.info(
                    "Acquired local-model capacity lease key=%s priority=%s wait=%.3fs",
                    key[:12],
                    requested_priority,
                    waited,
                )
                lease = LocalModelCapacityLease(
                    state_path,
                    lock_path,
                    token,
                    pid,
                    hostname,
                    owner_process_started_at,
                    max_lease_expires_at,
                    lease_dir_fd,
                )
                lease_dir_fd = None
                return lease
            time.sleep(
                min(
                    _QUEUE_POLL_SECONDS, max(0.0, effective_deadline - time.monotonic())
                )
            )
    except BaseException:
        if registered and not acquired:
            try:
                with _StateLock(
                    lock_path,
                    deadline_monotonic=time.monotonic() + 1.0,
                    cancelled=lambda: False,
                    dir_fd=lease_dir_fd,
                ):
                    state = _read_state(state_path, dir_fd=lease_dir_fd)
                    state["waiters"] = [
                        item
                        for item in state.get("waiters", [])
                        if item.get("token") != token
                    ]
                    _atomic_write_state(
                        state_path,
                        state,
                        dir_fd=lease_dir_fd,
                    )
            except Exception:
                logger.exception("Failed to remove local-model capacity waiter")
        raise
    finally:
        if lease_dir_fd is not None:
            os.close(lease_dir_fd)


__all__ = [
    "LocalModelCapacityLease",
    "LocalModelLeaseError",
    "LocalModelLeaseReleaseError",
    "LocalModelLeaseQuarantined",
    "LocalModelLeaseStateError",
    "LocalModelLeaseTimeout",
    "acquire_local_model_capacity",
    "local_model_lease_key",
]
