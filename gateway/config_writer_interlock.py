"""Process-lifetime interlock between gateways and config writers.

Gateways hold a shared lock for their entire runtime.  The managed config
writer holds the exclusive side of the same deterministic lock.  The lock
namespace is persistent under the canonical Hermes root: a temporary-directory
lock can be unlinked by an OS reaper while still held, allowing a replacement
inode to split the lock domain.

Security contract shared with hermes-config (BUILD-472):

* ``<canonical-home>/state/control-plane-locks`` is owner-only ``0700``;
* ``config-writer.lock`` is a regular, owner-only ``0600`` single-link file;
* every directory/file is opened relative to pinned directory descriptors with
  ``O_NOFOLLOW`` where supported;
* the lock inode is never written to or truncated; and
* path/descriptor identity is attested before and after acquiring ``flock``.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from hermes_constants import get_default_hermes_root


_LOCK_NAMESPACE_COMPONENTS = ("state", "control-plane-locks")
_LOCK_FILENAME = "config-writer.lock"


class ConfigWriterMaintenanceActive(RuntimeError):
    """Gateway startup was fenced by active or unsafe config maintenance."""


def _directory_open_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _validate_owned_directory(
    descriptor: int,
    *,
    path: Path | None = None,
    exact_mode: int | None = None,
) -> None:
    info = os.fstat(descriptor)
    if not stat.S_ISDIR(info.st_mode):
        raise ConfigWriterMaintenanceActive("config lock namespace is not a directory")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise ConfigWriterMaintenanceActive("config lock namespace has unsafe ownership")
    if path is not None:
        try:
            path_info = path.lstat()
        except OSError as exc:
            raise ConfigWriterMaintenanceActive(
                "config lock namespace path cannot be attested"
            ) from exc
        if (
            not stat.S_ISDIR(path_info.st_mode)
            or (hasattr(os, "getuid") and path_info.st_uid != os.getuid())
            or (info.st_dev, info.st_ino) != (path_info.st_dev, path_info.st_ino)
        ):
            raise ConfigWriterMaintenanceActive(
                "config lock namespace path changed while opening"
            )
    if exact_mode is not None and stat.S_IMODE(info.st_mode) != exact_mode:
        raise ConfigWriterMaintenanceActive("config lock namespace has unsafe permissions")


def _open_lock_namespace(home: Path | None = None) -> tuple[Path, int]:
    canonical_home = (home or get_default_hermes_root()).expanduser().resolve(
        strict=False
    )
    try:
        canonical_home.mkdir(mode=0o700, parents=True, exist_ok=True)
        current = os.open(canonical_home, _directory_open_flags())
    except OSError as exc:
        raise ConfigWriterMaintenanceActive(
            "cannot safely pin the canonical Hermes root for config maintenance"
        ) from exc
    try:
        _validate_owned_directory(current, path=canonical_home)
        for index, component in enumerate(_LOCK_NAMESPACE_COMPONENTS):
            try:
                os.mkdir(component, 0o700, dir_fd=current)
            except FileExistsError:
                pass
            child = os.open(component, _directory_open_flags(), dir_fd=current)
            try:
                _validate_owned_directory(
                    child,
                    path=canonical_home.joinpath(*_LOCK_NAMESPACE_COMPONENTS[: index + 1]),
                    exact_mode=0o700 if index == len(_LOCK_NAMESPACE_COMPONENTS) - 1 else None,
                )
            except BaseException:
                os.close(child)
                raise
            os.close(current)
            current = child
        return canonical_home / "state" / "control-plane-locks", current
    except BaseException:
        os.close(current)
        raise


def config_writer_lock_path(home: Path | None = None) -> Path:
    """Return the persistent lock path shared with hermes-config's guard."""
    root = (home or get_default_hermes_root()).expanduser().resolve(strict=False)
    return root / "state" / "control-plane-locks" / _LOCK_FILENAME


def _validate_lock_file(descriptor: int, namespace_fd: int) -> os.stat_result:
    info = os.fstat(descriptor)
    try:
        path_info = os.stat(
            _LOCK_FILENAME,
            dir_fd=namespace_fd,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise ConfigWriterMaintenanceActive(
            "config maintenance lock path cannot be attested"
        ) from exc
    safe = (
        stat.S_ISREG(info.st_mode)
        and (not hasattr(os, "getuid") or info.st_uid == os.getuid())
        and stat.S_IMODE(info.st_mode) == 0o600
        and info.st_nlink == 1
        and path_info.st_nlink == 1
        and (info.st_dev, info.st_ino) == (path_info.st_dev, path_info.st_ino)
    )
    if not safe:
        raise ConfigWriterMaintenanceActive("unsafe config maintenance lock")
    return info


def acquire_gateway_lifetime_lock(home: Path | None = None) -> int | None:
    """Acquire the gateway/shared side, failing fast during maintenance."""
    if os.name == "nt":
        return None

    import fcntl

    _path, namespace_fd = _open_lock_namespace(home)
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = -1
    try:
        descriptor = os.open(
            _LOCK_FILENAME,
            flags,
            0o600,
            dir_fd=namespace_fd,
        )
        _validate_lock_file(descriptor, namespace_fd)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ConfigWriterMaintenanceActive(
                "Hermes config maintenance is active; gateway startup is fenced"
            ) from exc
        _validate_lock_file(descriptor, namespace_fd)
        return descriptor
    except OSError as exc:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            finally:
                descriptor = -1
        raise ConfigWriterMaintenanceActive(
            "cannot safely open config maintenance lock"
        ) from exc
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    finally:
        os.close(namespace_fd)


def release_gateway_lifetime_lock(descriptor: int | None) -> None:
    if descriptor is None:
        return
    try:
        if os.name != "nt":
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


__all__ = [
    "ConfigWriterMaintenanceActive",
    "acquire_gateway_lifetime_lock",
    "config_writer_lock_path",
    "release_gateway_lifetime_lock",
]
