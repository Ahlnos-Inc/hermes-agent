import os
import threading

import pytest

from agent.claude_workspace_files import WorkspaceFileBroker
from agent.claude_workspace_terminal import WorkspaceTerminalBoundary


def test_workspace_file_broker_reads_and_writes_relative_files(tmp_path):
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    (workspace / "source.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    broker = WorkspaceFileBroker(workspace)

    assert "2: two" in broker.handle("read_file", {"path": "source.txt", "offset": 2})
    result = broker.handle("write_file", {"path": "result.txt", "content": "safe"})

    assert result["success"] is True
    assert (workspace / "result.txt").read_text(encoding="utf-8") == "safe"


def test_workspace_file_broker_credential_policy_is_read_only_opt_in(tmp_path):
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    credential_names = [
        ".env",
        ".env.development.local",
        ".ENV.PRODUCTION.LOCAL",
        ".EnVrC",
    ]
    for name in credential_names:
        (workspace / name).write_text("SECRET=value\n", encoding="utf-8")

    coder_broker = WorkspaceFileBroker(workspace)
    assert "SECRET=value" in coder_broker.handle("read_file", {"path": ".env"})

    read_only_broker = WorkspaceFileBroker(
        workspace, deny_credential_reads=True
    )
    for name in credential_names:
        with pytest.raises(RuntimeError, match="credential"):
            read_only_broker.handle("read_file", {"path": name})


def test_workspace_file_broker_rejects_symlink_and_hardlink_writes(tmp_path):
    workspace = tmp_path / "worktree"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("original", encoding="utf-8")
    (workspace / "symlink.txt").symlink_to(secret)
    os.link(secret, workspace / "hardlink.txt")
    broker = WorkspaceFileBroker(workspace)

    for path in ("symlink.txt", "hardlink.txt"):
        with pytest.raises(RuntimeError):
            broker.handle("write_file", {"path": path, "content": "escaped"})

    assert secret.read_text(encoding="utf-8") == "original"
    with pytest.raises(RuntimeError):
        broker.handle("read_file", {"path": "hardlink.txt"})


def test_workspace_file_broker_rejects_writes_under_readonly_subtree(tmp_path):
    workspace = tmp_path / "worktree"
    nested = workspace / "nested-worktree"
    workspace.mkdir()
    nested.mkdir()
    boundary = WorkspaceTerminalBoundary(workspace, (nested,))
    broker = WorkspaceFileBroker(workspace, boundary=boundary)

    with pytest.raises(RuntimeError, match="read-only worktree"):
        broker.handle(
            "write_file",
            {"path": "nested-worktree/result.txt", "content": "blocked"},
        )

    assert not (nested / "result.txt").exists()


def test_workspace_file_broker_bounds_reads_and_closes_root_fd(tmp_path):
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    (workspace / "huge.bin").write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    broker = WorkspaceFileBroker(workspace)
    root_fd = broker._root_fd

    with pytest.raises(RuntimeError, match="2 MiB"):
        broker.handle("read_file", {"path": "huge.bin"})
    broker.close()

    with pytest.raises(OSError):
        os.fstat(root_fd)


def test_workspace_file_broker_bounds_each_write_and_cumulative_turn(tmp_path):
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    broker = WorkspaceFileBroker(workspace)

    with pytest.raises(RuntimeError, match="2 MiB"):
        broker.handle(
            "write_file",
            {"path": "too-large.bin", "content": "x" * (2 * 1024 * 1024 + 1)},
        )

    for index in range(4):
        broker.handle(
            "write_file",
            {"path": f"part-{index}.bin", "content": "x" * (2 * 1024 * 1024)},
        )
    with pytest.raises(RuntimeError, match="per-turn"):
        broker.handle("write_file", {"path": "overflow.bin", "content": "x"})

    broker.begin_turn()
    broker.handle("write_file", {"path": "next-turn.txt", "content": "ok"})


def test_workspace_file_broker_survives_symlink_swap_race(tmp_path):
    workspace = tmp_path / "worktree"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    victim = outside / "victim.txt"
    victim.write_text("original", encoding="utf-8")
    target = workspace / "target.txt"
    target.write_text("inside", encoding="utf-8")
    broker = WorkspaceFileBroker(workspace)
    stop = threading.Event()

    def swap():
        while not stop.is_set():
            try:
                target.unlink(missing_ok=True)
                target.symlink_to(victim)
                target.unlink(missing_ok=True)
                target.write_text("inside", encoding="utf-8")
            except OSError:
                pass

    thread = threading.Thread(target=swap)
    thread.start()
    try:
        for _ in range(200):
            try:
                broker.handle("write_file", {"path": "target.txt", "content": "updated"})
            except (RuntimeError, FileNotFoundError):
                pass
    finally:
        stop.set()
        thread.join()

    assert victim.read_text(encoding="utf-8") == "original"
