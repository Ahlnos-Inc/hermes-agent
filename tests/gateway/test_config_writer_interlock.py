from __future__ import annotations

import asyncio
import hashlib
import os
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
    digest = hashlib.sha256(str(canonical).encode()).hexdigest()[:24]
    assert config_writer_lock_path(tmp_path).name == (
        f"hermes-config-writer-{digest}.lock"
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

    path = config_writer_lock_path(tmp_path)
    writer = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(writer, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(ConfigWriterMaintenanceActive, match="startup is fenced"):
            acquire_gateway_lifetime_lock(tmp_path)
    finally:
        fcntl.flock(writer, fcntl.LOCK_UN)
        os.close(writer)


def test_symlinked_lock_is_rejected(tmp_path, monkeypatch):
    real = tmp_path / "real.lock"
    real.write_text("")
    link = tmp_path / "link.lock"
    link.symlink_to(real)
    monkeypatch.setattr(
        "gateway.config_writer_interlock.config_writer_lock_path",
        lambda _home=None: link,
    )
    with pytest.raises(ConfigWriterMaintenanceActive, match="safely open"):
        acquire_gateway_lifetime_lock(tmp_path)


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


async def _false_async() -> bool:
    return False
