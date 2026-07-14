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
import os
import socket
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

import psutil

from hermes_constants import get_default_hermes_root

logger = logging.getLogger(__name__)

_STATE_VERSION = 1
_LOCK_POLL_SECONDS = 0.025
_QUEUE_POLL_SECONDS = 0.05
_DEFAULT_MAX_WAIT_SECONDS = 120.0


class LocalModelLeaseError(RuntimeError):
    """Base class for local-model capacity arbitration failures."""


class LocalModelLeaseTimeout(TimeoutError, LocalModelLeaseError):
    """Capacity could not be acquired before the caller's deadline."""


class LocalModelLeaseStateError(LocalModelLeaseError):
    """The durable lease state is invalid; fail closed to avoid overload."""


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


def _default_state() -> dict:
    return {"version": _STATE_VERSION, "active": None, "waiters": []}


def _valid_identity(value: object, *, maximum: int) -> bool:
    return isinstance(value, str) and 0 < len(value) <= maximum


def _valid_active(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    return (
        _valid_identity(value.get("token"), maximum=128)
        and isinstance(value.get("pid"), int)
        and value["pid"] > 0
        and _valid_identity(value.get("host"), maximum=255)
        and isinstance(value.get("priority"), int)
        and isinstance(value.get("acquired_at"), (int, float))
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
        and isinstance(value.get("priority"), int)
        and isinstance(value.get("created_at"), int)
        and isinstance(value.get("expires_at"), (int, float))
        and (value.get("task_id") is None or isinstance(value.get("task_id"), str))
        and (value.get("run_id") is None or isinstance(value.get("run_id"), str))
    )


def _read_state(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
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


def _atomic_write_state(path: Path, state: dict) -> None:
    payload = json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n"
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temp_path.unlink()
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
    ) -> None:
        self.path = path
        self.deadline_monotonic = deadline_monotonic
        self.cancelled = cancelled
        self._handle = None

    def __enter__(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        self._handle = open(self.path, "a+b")
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
    """An acquired capacity slot. Release is idempotent."""

    _state_path: Path
    _lock_path: Path
    _token: str
    _released: bool = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            with _StateLock(
                self._lock_path,
                deadline_monotonic=time.monotonic() + 2.0,
                cancelled=lambda: False,
            ):
                state = _read_state(self._state_path)
                active = state.get("active")
                if isinstance(active, dict) and active.get("token") == self._token:
                    state["active"] = None
                    _atomic_write_state(self._state_path, state)
        except Exception:
            # Never mask a provider result. A process crash/stuck release is
            # diagnosed by the durable state and reclaimed after PID death.
            logger.exception("Failed to release local-model capacity lease")

    def __enter__(self) -> "LocalModelCapacityLease":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def _prune_stale(
    state: dict,
    *,
    hostname: str,
    now_wall: float,
    pid_alive: Callable[[int], bool],
) -> bool:
    changed = False
    active = state.get("active")
    if isinstance(active, dict) and active.get("host") == hostname:
        try:
            active_pid = int(active.get("pid", 0))
        except (TypeError, ValueError):
            active_pid = 0
        if not pid_alive(active_pid):
            state["active"] = None
            changed = True

    live_waiters = []
    for waiter in state.get("waiters", []):
        if waiter.get("host") != hostname:
            live_waiters.append(waiter)
            continue
        try:
            waiter_pid = int(waiter.get("pid", 0))
            expires_at = float(waiter.get("expires_at", 0))
        except (TypeError, ValueError):
            changed = True
            continue
        if expires_at <= now_wall or not pid_alive(waiter_pid):
            changed = True
            continue
        live_waiters.append(waiter)
    if len(live_waiters) != len(state.get("waiters", [])):
        state["waiters"] = live_waiters
    return changed


def acquire_local_model_capacity(
    *,
    provider: str,
    model: str,
    base_url: str,
    deadline_monotonic: float | None,
    cancelled: Callable[[], bool],
    root: Path | None = None,
    priority: int | None = None,
    pid_alive: Callable[[int], bool] = _pid_alive,
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
    lease_root = (root or get_default_hermes_root()) / "shared" / "local-model-leases"
    lease_dir = lease_root / key
    for candidate in (lease_root.parent, lease_root, lease_dir):
        if candidate.is_symlink():
            raise LocalModelLeaseStateError(
                "local-model lease path contains a symbolic link"
            )
    lease_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(lease_root, 0o700)
        os.chmod(lease_dir, 0o700)
    except OSError:
        pass
    state_path = lease_dir / "state.json"
    lock_path = lease_dir / "state.lock"
    if state_path.is_symlink() or lock_path.is_symlink():
        raise LocalModelLeaseStateError(
            "local-model lease state path contains a symbolic link"
        )

    token = uuid.uuid4().hex
    hostname = socket.gethostname()
    pid = os.getpid()
    requested_priority = _priority_from_env() if priority is None else int(priority)
    created_at = time.time_ns()
    expires_at = time.time() + max(0.0, effective_deadline - now_monotonic)
    waiter = {
        "token": token,
        "pid": pid,
        "host": hostname,
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
            ):
                state = _read_state(state_path)
                changed = _prune_stale(
                    state,
                    hostname=hostname,
                    now_wall=time.time(),
                    pid_alive=pid_alive,
                )
                if not registered:
                    state["waiters"].append(waiter)
                    registered = True
                    changed = True
                if state.get("active") is None:
                    waiters = state["waiters"]
                    winner = min(
                        waiters,
                        key=lambda item: (
                            -int(item.get("priority", 0)),
                            int(item.get("created_at", 0)),
                            str(item.get("token", "")),
                        ),
                    )
                    if winner.get("token") == token:
                        state["waiters"] = [
                            item for item in waiters if item.get("token") != token
                        ]
                        state["active"] = {
                            "token": token,
                            "pid": pid,
                            "host": hostname,
                            "priority": requested_priority,
                            "acquired_at": time.time(),
                            "task_id": waiter["task_id"],
                            "run_id": waiter["run_id"],
                        }
                        acquired = True
                        changed = True
                if changed:
                    _atomic_write_state(state_path, state)
            if acquired:
                waited = time.monotonic() - wait_started
                logger.info(
                    "Acquired local-model capacity lease key=%s priority=%s wait=%.3fs",
                    key[:12],
                    requested_priority,
                    waited,
                )
                return LocalModelCapacityLease(state_path, lock_path, token)
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
                ):
                    state = _read_state(state_path)
                    state["waiters"] = [
                        item
                        for item in state.get("waiters", [])
                        if item.get("token") != token
                    ]
                    _atomic_write_state(state_path, state)
            except Exception:
                logger.exception("Failed to remove local-model capacity waiter")
        raise


__all__ = [
    "LocalModelCapacityLease",
    "LocalModelLeaseError",
    "LocalModelLeaseStateError",
    "LocalModelLeaseTimeout",
    "acquire_local_model_capacity",
    "local_model_lease_key",
]
