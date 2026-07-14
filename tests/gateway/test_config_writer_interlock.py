from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path

import pytest

from gateway.config_writer_interlock import (
    ConfigWriterMaintenanceActive,
    acquire_gateway_lifetime_lock,
    config_writer_lock_path,
    release_gateway_lifetime_lock,
)


pytestmark = pytest.mark.skipif(os.name == "nt", reason="config guard uses fcntl")


def test_lock_path_matches_hermes_config_contract(tmp_path):
    canonical = tmp_path.resolve()
    assert config_writer_lock_path(tmp_path) == (
        canonical / "state" / "control-plane-locks" / "config-writer.lock"
    )


def test_gateway_shared_locks_coexist_and_fence_writer(tmp_path):
    import fcntl

    first = acquire_gateway_lifetime_lock(tmp_path)
    second = acquire_gateway_lifetime_lock(tmp_path)
    writer = os.open(config_writer_lock_path(tmp_path), os.O_RDWR)
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(writer, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(writer)
        release_gateway_lifetime_lock(second)
        release_gateway_lifetime_lock(first)


def test_active_writer_fences_gateway_start(tmp_path):
    import fcntl

    namespace = tmp_path / "state" / "control-plane-locks"
    namespace.mkdir(parents=True, mode=0o700)
    path = config_writer_lock_path(tmp_path)
    writer = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(writer, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(ConfigWriterMaintenanceActive, match="startup is fenced"):
            acquire_gateway_lifetime_lock(tmp_path)
    finally:
        fcntl.flock(writer, fcntl.LOCK_UN)
        os.close(writer)


def test_symlinked_lock_is_rejected(tmp_path):
    namespace = tmp_path / "state" / "control-plane-locks"
    namespace.mkdir(parents=True, mode=0o700)
    real = tmp_path / "real.lock"
    real.write_text("")
    link = namespace / "config-writer.lock"
    link.symlink_to(real)
    with pytest.raises(ConfigWriterMaintenanceActive, match="safely open"):
        acquire_gateway_lifetime_lock(tmp_path)


def test_hardlinked_lock_is_rejected(tmp_path):
    namespace = tmp_path / "state" / "control-plane-locks"
    namespace.mkdir(parents=True, mode=0o700)
    lock_path = namespace / "config-writer.lock"
    lock_path.touch(mode=0o600)
    os.link(lock_path, tmp_path / "second-name.lock")

    with pytest.raises(ConfigWriterMaintenanceActive, match="unsafe"):
        acquire_gateway_lifetime_lock(tmp_path)


def test_lock_namespace_and_file_modes_match_writer_contract(tmp_path):
    descriptor = acquire_gateway_lifetime_lock(tmp_path)
    try:
        namespace = tmp_path / "state" / "control-plane-locks"
        assert stat.S_IMODE(namespace.stat().st_mode) == 0o700
        assert stat.S_IMODE(config_writer_lock_path(tmp_path).stat().st_mode) == 0o600
    finally:
        release_gateway_lifetime_lock(descriptor)


def test_existing_permissive_namespace_is_rejected_not_repaired(tmp_path):
    namespace = tmp_path / "state" / "control-plane-locks"
    namespace.mkdir(parents=True, mode=0o700)
    namespace.chmod(0o755)

    with pytest.raises(ConfigWriterMaintenanceActive, match="permissions"):
        acquire_gateway_lifetime_lock(tmp_path)
    assert stat.S_IMODE(namespace.stat().st_mode) == 0o755


def test_public_start_gateway_releases_lock_after_refusal(monkeypatch, tmp_path):
    from gateway import run

    held: list[int] = []
    released: list[int] = []

    monkeypatch.setattr(
        "gateway.config_writer_interlock.acquire_gateway_lifetime_lock",
        lambda: held.append(41) or 41,
    )
    monkeypatch.setattr(
        "gateway.config_writer_interlock.release_gateway_lifetime_lock",
        released.append,
    )
    monkeypatch.setattr(
        run,
        "_start_gateway_under_config_lock",
        lambda **_kwargs: _false_async(),
    )

    assert asyncio.run(run.start_gateway()) is False
    assert held == [41]
    assert released == [41]


def test_release_closes_descriptor_when_unlock_fails(monkeypatch, tmp_path):
    import fcntl

    descriptor = acquire_gateway_lifetime_lock(tmp_path)
    original_flock = fcntl.flock

    def fail_unlock(fd, operation):
        if operation == fcntl.LOCK_UN:
            raise OSError("injected unlock failure")
        return original_flock(fd, operation)

    monkeypatch.setattr(fcntl, "flock", fail_unlock)
    with pytest.raises(OSError, match="unlock failure"):
        release_gateway_lifetime_lock(descriptor)
    with pytest.raises(OSError):
        os.fstat(descriptor)


async def _false_async() -> bool:
    return False
