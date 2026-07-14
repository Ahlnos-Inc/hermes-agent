"""Process-lifetime interlock between gateways and config writers.

Gateways hold a shared lock for their entire runtime. The managed config
writer holds the exclusive side of the same deterministic per-root lock. This
closes both races: a writer cannot begin while any gateway is alive, and a
gateway cannot start after a writer's one-time drain check.
"""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from pathlib import Path

from hermes_constants import get_default_hermes_root


class ConfigWriterMaintenanceActive(RuntimeError):
    """Gateway startup was fenced by an active config transaction."""


def config_writer_lock_path(home: Path | None = None) -> Path:
    """Return the lock path shared with hermes-config's writer guard."""
    root = (home or get_default_hermes_root()).expanduser().resolve(strict=False)
    digest = hashlib.sha256(str(root).encode()).hexdigest()[:24]
    return Path(tempfile.gettempdir()) / f"hermes-config-writer-{digest}.lock"


def acquire_gateway_lifetime_lock(home: Path | None = None) -> int | None:
    """Acquire the gateway/shared side, failing fast during maintenance.

    The hermes-config guard is POSIX-only today (it uses ``fcntl``), so native
    Windows retains its existing behavior until both sides have one supported
    locking primitive. Returning ``None`` keeps the release helper uniform.
    """
    if os.name == "nt":
        return None

    import fcntl

    path = config_writer_lock_path(home)
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise ConfigWriterMaintenanceActive(
            f"cannot safely open config maintenance lock: {path}"
        ) from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or (
            hasattr(os, "getuid") and info.st_uid != os.getuid()
        ):
            raise ConfigWriterMaintenanceActive(
                f"unsafe config maintenance lock: {path}"
            )
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ConfigWriterMaintenanceActive(
                "Hermes config maintenance is active; gateway startup is fenced"
            ) from exc
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def release_gateway_lifetime_lock(descriptor: int | None) -> None:
    if descriptor is None:
        return
    if os.name != "nt":
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_UN)
    os.close(descriptor)


__all__ = [
    "ConfigWriterMaintenanceActive",
    "acquire_gateway_lifetime_lock",
    "config_writer_lock_path",
    "release_gateway_lifetime_lock",
]
