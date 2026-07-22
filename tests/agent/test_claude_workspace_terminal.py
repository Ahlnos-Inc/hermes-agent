import os
import shlex
import subprocess
import sys
import shutil
from pathlib import Path

import pytest

import agent.claude_workspace_terminal as workspace_terminal
from agent.claude_workspace_terminal import WorkspaceBoundaryProvisioningError
from agent.claude_workspace_terminal import build_workspace_seatbelt_profile
from agent.claude_workspace_terminal import build_workspace_terminal_args
from agent.claude_workspace_terminal import dispatch_read_only_workspace_terminal
from agent.claude_workspace_terminal import prepare_workspace_terminal_boundary


def _linked_worktree(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    workspace = tmp_path / "worktree"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "README.md"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Hermes Test",
            "-c",
            "user.email=hermes@example.invalid",
            "commit",
            "-m",
            "base",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "worktree", "add", "-b", "build/sandbox-test", str(workspace)],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return repo, workspace


@pytest.mark.skipif(os.uname().sysname != "Darwin", reason="macOS sandbox-exec")
def test_workspace_terminal_can_commit_in_linked_worktree_with_apple_git_path(tmp_path):
    _, workspace = _linked_worktree(tmp_path)
    transformed = build_workspace_terminal_args(
        {
            "command": (
                "printf 'sandbox commit\\n' > committed.txt && "
                "git add committed.txt && "
                "git -c user.name='Hermes Test' "
                "-c user.email=hermes@example.invalid commit -m sandbox"
            )
        },
        workspace=workspace,
        host_home=tmp_path / "host",
        exact_env={"PATH": "/usr/bin:/bin"},
    )

    result = subprocess.run(
        ["/bin/bash", "-lc", transformed["command"]],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr
    assert subprocess.run(
        ["git", "log", "-1", "--format=%s"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip() == "sandbox"


@pytest.mark.skipif(os.uname().sysname != "Darwin", reason="macOS sandbox-exec")
def test_workspace_terminal_keeps_shared_git_metadata_and_objects_immutable(tmp_path):
    repo, workspace = _linked_worktree(tmp_path)
    common_dir = Path(
        subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=workspace,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    main_ref = common_dir / "refs" / "heads" / "main"
    config = common_dir / "config"
    tree_oid = subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    existing_object = common_dir / "objects" / tree_oid[:2] / tree_oid[2:]
    originals = {
        config: config.read_bytes(),
        main_ref: main_ref.read_bytes(),
        existing_object: existing_object.read_bytes(),
    }
    transformed = build_workspace_terminal_args(
        {
            "command": "; ".join(
                [
                    f"printf hacked > {shlex.quote(str(config))}",
                    f"printf hacked > {shlex.quote(str(main_ref))}",
                    f"rm -f {shlex.quote(str(existing_object))}",
                ]
            )
        },
        workspace=workspace,
        host_home=tmp_path / "host",
        exact_env={"PATH": "/usr/bin:/bin"},
    )

    result = subprocess.run(
        ["/bin/bash", "-lc", transformed["command"]],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode != 0
    assert "Operation not permitted" in result.stderr
    assert {path: path.read_bytes() for path in originals} == originals
    assert subprocess.run(
        ["git", "status", "--short"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout == ""


@pytest.mark.skipif(os.uname().sysname != "Darwin", reason="macOS sandbox-exec")
def test_workspace_terminal_cannot_retarget_head_between_invocations(tmp_path):
    repo, workspace = _linked_worktree(tmp_path)
    main_before = subprocess.run(
        ["git", "rev-parse", "main"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    retarget = build_workspace_terminal_args(
        {"command": "git symbolic-ref HEAD refs/heads/main"},
        workspace=workspace,
        host_home=tmp_path / "host",
        exact_env={"PATH": "/usr/bin:/bin"},
    )

    retarget_result = subprocess.run(
        ["/bin/bash", "-lc", retarget["command"]],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=20,
    )
    second_invocation = build_workspace_terminal_args(
        {
            "command": (
                "printf 'safe branch commit\\n' > safe.txt && "
                "git add safe.txt && "
                "git -c user.name='Hermes Test' "
                "-c user.email=hermes@example.invalid commit -m safe"
            )
        },
        workspace=workspace,
        host_home=tmp_path / "host",
        exact_env={"PATH": "/usr/bin:/bin"},
    )
    commit_result = subprocess.run(
        ["/bin/bash", "-lc", second_invocation["command"]],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert retarget_result.returncode != 0
    assert commit_result.returncode == 0, commit_result.stderr
    assert subprocess.run(
        ["git", "symbolic-ref", "HEAD"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip() == "refs/heads/build/sandbox-test"
    assert subprocess.run(
        ["git", "rev-parse", "main"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip() == main_before


@pytest.mark.skipif(os.uname().sysname != "Darwin", reason="macOS sandbox-exec")
def test_workspace_terminal_denies_non_loose_git_object_paths(tmp_path):
    repo, workspace = _linked_worktree(tmp_path)
    object_dir = repo / ".git" / "objects"
    (object_dir / "aa").mkdir()
    (object_dir / "info" / "ab").mkdir()
    (object_dir / "pack" / "cd").mkdir()
    denied_paths = [
        object_dir / "info" / "worker-created",
        object_dir / "pack" / "worker-created",
        object_dir / "tmp_obj_worker_created",
        object_dir / "info" / "aa",
        object_dir / "info" / "ab" / "tmp_obj_ABC123",
        object_dir / "pack" / "cd" / ("e" * 38),
        object_dir / "aa" / "bb",
    ]
    transformed = build_workspace_terminal_args(
        {
            "command": "; ".join(
                f"printf poison > {shlex.quote(str(path))}" for path in denied_paths
            )
        },
        workspace=workspace,
        host_home=tmp_path / "host",
        exact_env={"PATH": "/usr/bin:/bin"},
    )

    result = subprocess.run(
        ["/bin/bash", "-lc", transformed["command"]],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode != 0
    assert "Operation not permitted" in result.stderr
    assert all(not path.exists() for path in denied_paths)


def test_workspace_terminal_rejects_linked_gitdir_for_another_workspace(tmp_path):
    _, workspace = _linked_worktree(tmp_path)
    git_dir = Path(
        subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-dir"],
            cwd=workspace,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    (git_dir / "gitdir").write_text(
        str(tmp_path / "different-workspace" / ".git") + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="backpointers"):
        build_workspace_terminal_args(
            {"command": "git status --short"},
            workspace=workspace,
            host_home=tmp_path / "host",
            exact_env={"PATH": "/usr/bin:/bin"},
            platform_name="Darwin",
        )


@pytest.mark.skipif(os.uname().sysname != "Darwin", reason="macOS sandbox-exec")
def test_read_only_linked_worktree_has_no_git_write_grants(tmp_path):
    _, workspace = _linked_worktree(tmp_path)
    runtime_root = tmp_path / "runtime"
    transformed = build_workspace_terminal_args(
        {"command": "git status --short"},
        workspace=workspace,
        host_home=tmp_path / "host",
        exact_env={"PATH": "/usr/bin:/bin"},
        read_only=True,
        runtime_root=runtime_root,
    )
    argv = shlex.split(transformed["command"])
    profile = Path(argv[argv.index("-f") + 1]).read_text(encoding="utf-8")
    git_dir = Path(
        subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-dir"],
            cwd=workspace,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    git_write_rules = [
        line for line in profile.splitlines() if line.startswith("(allow file-write")
    ]

    result = subprocess.run(
        ["/bin/bash", "-lc", transformed["command"]],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr
    assert all(str(git_dir) not in line for line in git_write_rules)
    assert all(str(workspace) not in line for line in git_write_rules)


@pytest.mark.skipif(os.uname().sysname != "Darwin", reason="macOS sandbox-exec")
def test_workspace_terminal_denies_host_and_ambient_but_can_run_tests(tmp_path):
    host_home = tmp_path / "host"
    workspace = host_home / "worktree"
    workspace.mkdir(parents=True)
    (host_home / "sentinel").write_text("host-secret", encoding="utf-8")
    transformed = build_workspace_terminal_args(
        {
            "command": (
                f"test ! -r {shlex.quote(str(host_home / 'sentinel'))} && "
                'test -z "$AMBIENT_SENTINEL_SECRET" && '
                "printf passed > sandbox-proof.txt"
            )
        },
        workspace=workspace,
        host_home=host_home,
        exact_env={
            "HOME": str(host_home),
            "PATH": os.environ["PATH"],
            "USER": os.environ.get("USER", "worker"),
            "LOGNAME": os.environ.get("LOGNAME", "worker"),
        },
    )

    result = subprocess.run(
        ["/bin/bash", "-lc", transformed["command"]],
        cwd=workspace,
        env={**os.environ, "AMBIENT_SENTINEL_SECRET": "must-not-cross"},
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr
    assert (workspace / "sandbox-proof.txt").read_text(encoding="utf-8") == "passed"


@pytest.mark.skipif(os.uname().sysname != "Darwin", reason="macOS sandbox-exec")
def test_workspace_terminal_cannot_write_outside_or_follow_escape_symlink(tmp_path):
    workspace = tmp_path / "worktree"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / "escape").symlink_to(outside, target_is_directory=True)
    transformed = build_workspace_terminal_args(
        {
            "command": (
                f"touch {shlex.quote(str(outside / 'absolute.txt'))}; "
                "touch escape/symlink.txt"
            )
        },
        workspace=workspace,
        host_home=tmp_path / "host",
        exact_env={"PATH": os.environ["PATH"]},
    )

    subprocess.run(
        ["/bin/bash", "-lc", transformed["command"]],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert list(outside.iterdir()) == []


def test_workspace_terminal_preserves_process_controls(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    transformed = build_workspace_terminal_args(
        {"command": "echo ok", "timeout": 30, "background": True},
        workspace=workspace,
        host_home=tmp_path / "host",
        exact_env={"HOME": str(tmp_path / "host"), "PATH": "/usr/bin:/bin"},
        platform_name="Darwin",
    )

    assert transformed["timeout"] == 30
    assert transformed["background"] is True
    assert "sandbox-exec" in transformed["command"]
    argv = shlex.split(transformed["command"])
    profile_path = Path(argv[argv.index("-f") + 1])
    assert profile_path.is_file()
    assert not profile_path.is_relative_to(workspace.resolve())
    assert profile_path.stat().st_mode & 0o777 == 0o600
    assert "(deny default)" in profile_path.read_text(encoding="utf-8")
    assert "(allow default)" not in profile_path.read_text(encoding="utf-8")
    assert transformed["workdir"] == str(workspace.resolve())


@pytest.mark.parametrize(
    "command",
    [
        "rg -n TODO agent",
        "git diff --check",
    ],
)
def test_read_only_workspace_terminal_builds_read_only_inspection_profile(
    tmp_path, command
):
    workspace = tmp_path / "work"
    workspace.mkdir()
    credentials = [
        workspace / ".env",
        workspace / ".env.development.local",
        workspace / ".ENV.PRODUCTION.LOCAL",
        workspace / ".EnVrC",
    ]
    for credential in credentials:
        credential.write_text("SECRET=hidden\n", encoding="utf-8")
    runtime_root = tmp_path / "runtime"

    transformed = build_workspace_terminal_args(
        {"command": command},
        workspace=workspace,
        host_home=tmp_path / "host",
        exact_env={"HOME": str(tmp_path / "host"), "PATH": os.environ["PATH"]},
        platform_name="Darwin",
        read_only=True,
        runtime_root=runtime_root,
    )

    assert "sandbox-exec" in transformed["command"]
    argv = shlex.split(transformed["command"])
    profile = Path(argv[argv.index("-f") + 1]).read_text(encoding="utf-8")
    workspace_rule = f'(allow file-write* (subpath "{workspace.resolve()}"))'
    runtime_rule = f'(allow file-write* (subpath "{runtime_root.resolve()}"))'
    assert workspace_rule not in profile
    assert runtime_rule in profile
    assert all(
        f'(deny file-read* (literal "{credential.resolve()}"))' in profile
        for credential in credentials
    )


@pytest.mark.parametrize("use_extra", [False, True])
def test_read_only_workspace_terminal_extra_readable_roots_plumb_through(
    tmp_path, use_extra
):
    """BUILD-581: `extra_readable_roots` grants extra Seatbelt reads for the
    read-only worker terminal path (reviewer/verifier), absent when unset."""
    workspace = tmp_path / "work"
    workspace.mkdir()
    extra_root = tmp_path / "extra-readable"
    extra_root.mkdir()
    captured = {}

    def dispatch(name, arguments, *, task_id):
        # Read the Seatbelt profile now: the disposable scratch root (and
        # its profile file) is removed once this call returns.
        argv = shlex.split(arguments["command"])
        captured["profile"] = Path(argv[argv.index("-f") + 1]).read_text(
            encoding="utf-8"
        )
        return "ok"

    dispatch_read_only_workspace_terminal(
        {"command": "cat missing.txt"},
        workspace=workspace,
        host_home=tmp_path / "host",
        exact_env={"HOME": str(tmp_path / "host"), "PATH": os.environ["PATH"]},
        dispatch=dispatch,
        task_id="worker-task",
        platform_name="Darwin",
        scratch_parent=tmp_path / "scratch",
        extra_readable_roots=[extra_root] if use_extra else None,
    )

    rule = f'(allow file-read* (subpath "{extra_root.resolve()}"))'
    assert (rule in captured["profile"]) is use_extra


@pytest.mark.parametrize(
    "command",
    [
        "python -m pytest --junit-xml=report.xml --cache-clear",
        "pytest --snapshot-update",
        "ruff check --add-noqa --output-file report.txt .",
    ],
)
def test_read_only_test_and_report_options_use_clean_disposable_mirror(
    tmp_path, command
):
    workspace = tmp_path / "work"
    workspace.mkdir()
    source = workspace / "source.py"
    source.write_text("original\n", encoding="utf-8")
    credential_names = [
        ".env",
        ".env.development.local",
        ".ENV.PRODUCTION.LOCAL",
        ".EnVrC",
    ]
    for name in credential_names:
        (workspace / name).write_text("SECRET=must-not-copy\n", encoding="utf-8")
    scratch = tmp_path / "scratch"
    execution_roots = []

    def dispatch(name, arguments, *, task_id):
        assert name == "terminal"
        assert task_id == "worker-task"
        execution_root = Path(arguments["workdir"])
        execution_roots.append(execution_root)
        assert execution_root != workspace
        assert all(not (execution_root / name).exists() for name in credential_names)
        (execution_root / "source.py").write_text("mutated\n", encoding="utf-8")
        (execution_root / "report.xml").write_text("report\n", encoding="utf-8")
        return "ok"

    result = dispatch_read_only_workspace_terminal(
        {"command": command},
        workspace=workspace,
        host_home=tmp_path / "host",
        exact_env={"PATH": os.environ["PATH"]},
        dispatch=dispatch,
        task_id="worker-task",
        platform_name="Darwin",
        scratch_parent=scratch,
    )

    assert result == "ok"
    assert source.read_text(encoding="utf-8") == "original\n"
    assert all(
        (workspace / name).read_text(encoding="utf-8") == "SECRET=must-not-copy\n"
        for name in credential_names
    )
    assert not (workspace / "report.xml").exists()
    assert execution_roots and all(not root.exists() for root in execution_roots)
    assert list(scratch.iterdir()) == []


def test_read_only_disposable_mirror_is_cleaned_when_dispatch_fails(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    source = workspace / "source.py"
    source.write_text("original\n", encoding="utf-8")
    scratch = tmp_path / "scratch"

    def dispatch(_name, arguments, *, task_id):
        assert task_id == "worker-task"
        execution_root = Path(arguments["workdir"])
        (execution_root / "source.py").write_text("mutated\n", encoding="utf-8")
        raise RuntimeError("executor failed")

    with pytest.raises(RuntimeError, match="executor failed"):
        dispatch_read_only_workspace_terminal(
            {"command": "python -m pytest --cache-clear"},
            workspace=workspace,
            host_home=tmp_path / "host",
            exact_env={"PATH": os.environ["PATH"]},
            dispatch=dispatch,
            task_id="worker-task",
            platform_name="Darwin",
            scratch_parent=scratch,
        )

    assert source.read_text(encoding="utf-8") == "original\n"
    assert list(scratch.iterdir()) == []


def _tree_bytes(root):
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }


@pytest.mark.skipif(os.uname().sysname != "Darwin", reason="macOS sandbox-exec")
def test_read_only_inspection_cannot_compile_into_original_workspace(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    (workspace / "magic").write_text("0 string hello greeting\n", encoding="utf-8")
    before = _tree_bytes(workspace)
    scratch = tmp_path / "scratch"

    def dispatch(_name, arguments, *, task_id):
        assert task_id == "worker-task"
        return subprocess.run(
            ["/bin/bash", "-lc", arguments["command"]],
            cwd=arguments["workdir"],
            capture_output=True,
            text=True,
            timeout=20,
        )

    result = dispatch_read_only_workspace_terminal(
        {"command": "file -C -m magic"},
        workspace=workspace,
        host_home=tmp_path / "host",
        exact_env={"PATH": os.environ["PATH"]},
        dispatch=dispatch,
        task_id="worker-task",
        scratch_parent=scratch,
    )

    assert result.returncode != 0
    assert _tree_bytes(workspace) == before
    assert list(scratch.iterdir()) == []


@pytest.mark.skipif(os.uname().sysname != "Darwin", reason="macOS sandbox-exec")
def test_read_only_pytest_writes_only_disposable_mirror(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    host_secret = tmp_path / "host" / "credential"
    host_secret.parent.mkdir()
    host_secret.write_text("host-secret\n", encoding="utf-8")
    dependency = workspace / "node_modules"
    dependency.mkdir()
    (dependency / "original.txt").write_text("dependency\n", encoding="utf-8")
    (workspace / "test_sample.py").write_text(
        (
            "from pathlib import Path\n"
            "import socket\n"
            "import pytest\n\n"
            "def test_boundaries():\n"
            f"    with pytest.raises(PermissionError): Path({str(host_secret)!r}).read_text()\n"
            "    with pytest.raises(PermissionError): "
            "Path('node_modules/original.txt').write_text('changed')\n"
            "    sock = socket.socket()\n"
            "    try:\n"
            "        with pytest.raises(PermissionError): sock.connect(('127.0.0.1', 9))\n"
            "    finally:\n"
            "        sock.close()\n"
        ),
        encoding="utf-8",
    )
    (workspace / ".env").write_text("SECRET=outside-test\n", encoding="utf-8")
    before = _tree_bytes(workspace)
    scratch = tmp_path / "scratch"
    execution_roots = []

    def dispatch(_name, arguments, *, task_id):
        assert task_id == "worker-task"
        execution_roots.append(Path(arguments["workdir"]))
        return subprocess.run(
            ["/bin/bash", "-lc", arguments["command"]],
            cwd=arguments["workdir"],
            capture_output=True,
            text=True,
            timeout=30,
        )

    result = dispatch_read_only_workspace_terminal(
        {
            "command": (
                "python -m pytest test_sample.py --junit-xml=report.xml "
                "--cache-clear -q"
            )
        },
        workspace=workspace,
        host_home=tmp_path / "host",
        exact_env={"PATH": os.environ["PATH"]},
        dispatch=dispatch,
        task_id="worker-task",
        scratch_parent=scratch,
    )

    assert result.returncode == 0, result.stderr
    assert _tree_bytes(workspace) == before
    assert execution_roots and all(not root.exists() for root in execution_roots)
    assert list(scratch.iterdir()) == []


@pytest.mark.parametrize(
    "command",
    [
        "touch changed.txt",
        "rm -rf .",
        "git add .",
        "git commit -m nope",
        "sed -i '' 's/a/b/' source.py",
        "rg TODO . > report.txt",
        "rg TODO .; touch changed.txt",
        "rg TODO . && touch changed.txt",
        "find . -delete",
        "ln ../outside linked",
        "python -c 'open(\"changed.txt\", \"w\").write(\"x\")'",
        "./rg TODO .",
        "/tmp/git status",
        "git grep --open-files-in-pager=touch TODO",
        "git grep -Otouch TODO",
        "git cat-file --filters HEAD:source.py",
        "git diff --ext-dif",
        "rg --pre touch TODO .",
        "go test -exec touch ./...",
        "npm test --script-shell=/tmp/helper",
        "cargo test --config build.rustc-wrapper=/tmp/helper",
        "make test -f/tmp/Makefile",
    ],
)
def test_read_only_workspace_terminal_rejects_mutation_and_shell_escapes(
    tmp_path, command
):
    workspace = tmp_path / "work"
    workspace.mkdir()

    with pytest.raises(RuntimeError, match="Read-only worker terminal"):
        build_workspace_terminal_args(
            {"command": command},
            workspace=workspace,
            host_home=tmp_path / "host",
            exact_env={"HOME": str(tmp_path / "host"), "PATH": "/usr/bin:/bin"},
            platform_name="Darwin",
            read_only=True,
            runtime_root=tmp_path / "runtime",
        )


def test_workspace_terminal_rejects_outside_workdir(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    with pytest.raises(RuntimeError, match="workdir is outside"):
        build_workspace_terminal_args(
            {"command": "pwd", "workdir": str(tmp_path / "outside")},
            workspace=workspace,
            host_home=tmp_path / "host",
            exact_env={"PATH": "/usr/bin:/bin"},
            platform_name="Darwin",
        )


def test_workspace_terminal_fails_closed_off_macos(tmp_path):
    with pytest.raises(RuntimeError, match="unsupported"):
        build_workspace_terminal_args(
            {"command": "echo no"},
            workspace=tmp_path,
            host_home=tmp_path / "host",
            exact_env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
            platform_name="Linux",
        )


def test_read_only_mutation_rejects_before_platform_preflight(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()

    with pytest.raises(RuntimeError, match="Read-only worker terminal"):
        build_workspace_terminal_args(
            {"command": "touch changed.txt"},
            workspace=workspace,
            host_home=tmp_path / "host",
            exact_env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
            platform_name="Linux",
            read_only=True,
        )

    assert not (workspace / "changed.txt").exists()


@pytest.mark.parametrize("read_only", [False, True])
def test_workspace_terminal_preflight_rejects_hardlinked_regular_file(
    tmp_path, read_only
):
    workspace = tmp_path / "work"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    os.link(outside, workspace / "alias.txt")

    with pytest.raises(RuntimeError, match="hard-linked"):
        build_workspace_terminal_args(
            {"command": "cat alias.txt"},
            workspace=workspace,
            host_home=tmp_path / "host",
            exact_env={"PATH": "/usr/bin:/bin"},
            platform_name="Darwin",
            read_only=read_only,
            runtime_root=tmp_path / "runtime" if read_only else None,
        )


def test_workspace_boundary_accepts_links_wholly_inside_writable_scope(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    source = workspace / "source.txt"
    alias = workspace / "alias.txt"
    source.write_text("inside", encoding="utf-8")
    os.link(source, alias)

    boundary = prepare_workspace_terminal_boundary(workspace)

    assert boundary.root == workspace.resolve()
    assert boundary.readonly_subtrees == ()


def test_workspace_boundary_rejects_link_with_external_alias(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    os.link(outside, workspace / "alias.txt")

    with pytest.raises(WorkspaceBoundaryProvisioningError, match="outside"):
        prepare_workspace_terminal_boundary(workspace)


def test_workspace_boundary_scans_directory_named_worktrees_without_git_attestation(
    tmp_path,
):
    workspace = tmp_path / "work"
    fake_worktrees = workspace / ".worktrees"
    fake_worktrees.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    os.link(outside, fake_worktrees / "alias.txt")

    with pytest.raises(WorkspaceBoundaryProvisioningError, match="outside"):
        prepare_workspace_terminal_boundary(workspace)


def test_workspace_boundary_skips_attested_nested_worktree(tmp_path):
    repo, nested = _linked_worktree(tmp_path)
    nested_target = repo / "nested-worktree"
    subprocess.run(
        ["git", "worktree", "add", "-b", "nested-boundary", str(nested_target)],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    nested_source = nested_target / "source.txt"
    nested_alias = nested_target / "alias.txt"
    nested_source.write_text("nested", encoding="utf-8")
    os.link(nested_source, nested_alias)

    boundary = prepare_workspace_terminal_boundary(repo)

    assert boundary.readonly_subtrees == (nested_target.resolve(),)


def test_workspace_boundary_rejects_alias_between_writable_root_and_nested_worktree(
    tmp_path,
):
    repo, _ = _linked_worktree(tmp_path)
    nested_target = repo / "nested-worktree"
    subprocess.run(
        ["git", "worktree", "add", "-b", "nested-boundary", str(nested_target)],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    source = repo / "source.txt"
    source.write_text("shared", encoding="utf-8")
    os.link(source, nested_target / "alias.txt")

    with pytest.raises(WorkspaceBoundaryProvisioningError, match="outside"):
        prepare_workspace_terminal_boundary(repo)


def test_prepared_workspace_boundary_is_not_recanned_by_terminal_dispatch(
    monkeypatch, tmp_path
):
    workspace = tmp_path / "work"
    workspace.mkdir()
    boundary = prepare_workspace_terminal_boundary(workspace)
    calls = []

    def census(_boundary):
        calls.append(True)

    monkeypatch.setattr(workspace_terminal, "_census_writable_workspace", census)

    transformed = build_workspace_terminal_args(
        {"command": "echo ok"},
        workspace=workspace,
        host_home=tmp_path / "host",
        exact_env={"PATH": "/usr/bin:/bin"},
        boundary=boundary,
        platform_name="Darwin",
    )

    assert transformed["workdir"] == str(workspace.resolve())
    assert calls == []


def test_workspace_seatbelt_denies_nested_worktree_writes_and_file_links(tmp_path):
    workspace = tmp_path / "work"
    nested = workspace / "nested"
    workspace.mkdir()
    nested.mkdir()
    profile = build_workspace_seatbelt_profile(
        workspace=workspace,
        host_home=tmp_path / "host",
        allow_network=False,
        denied_write_roots=[nested],
    )

    assert f'(deny file-write* (subpath "{nested.resolve()}"))' in profile
    assert "(deny file-link)" in profile


@pytest.mark.skipif(os.uname().sysname != "Darwin", reason="macOS sandbox-exec")
def test_workspace_terminal_nested_worktree_and_file_link_writes_are_denied(
    tmp_path,
):
    repo, _ = _linked_worktree(tmp_path)
    nested = repo / "nested-worktree"
    subprocess.run(
        ["git", "worktree", "add", "-b", "nested-boundary", str(nested)],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    boundary = prepare_workspace_terminal_boundary(repo)
    transformed = build_workspace_terminal_args(
        {
            "command": (
                "printf root > root.txt; "
                "printf nested > nested/nested.txt; "
                "ln root.txt nested/linked.txt"
            )
        },
        workspace=repo,
        host_home=tmp_path / "host",
        exact_env={"PATH": "/usr/bin:/bin"},
        boundary=boundary,
        platform_name="Darwin",
    )

    result = subprocess.run(
        ["/bin/bash", "-lc", transformed["command"]],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=20,
    )
    if result.returncode == 71 and "sandbox_apply: Operation not permitted" in result.stderr:
        pytest.skip("Darwin Seatbelt is unavailable in this test environment")

    assert result.returncode != 0
    assert (repo / "root.txt").read_text(encoding="utf-8") == "root"
    assert not (nested / "nested.txt").exists()
    assert not (nested / "linked.txt").exists()


@pytest.mark.skipif(os.uname().sysname != "Darwin", reason="macOS sandbox-exec")
def test_workspace_terminal_denies_symlink_to_outside_created_after_profile(tmp_path):
    workspace = tmp_path / "work"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    transformed = build_workspace_terminal_args(
        {"command": "cat late-link/secret.txt; touch late-link/escaped.txt"},
        workspace=workspace,
        host_home=tmp_path / "host",
        exact_env={"PATH": os.environ["PATH"]},
    )
    (workspace / "late-link").symlink_to(outside, target_is_directory=True)

    result = subprocess.run(
        ["/bin/bash", "-lc", transformed["command"]],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode != 0
    assert "secret" not in result.stdout
    assert not (outside / "escaped.txt").exists()


@pytest.mark.skipif(os.uname().sysname != "Darwin", reason="macOS sandbox-exec")
@pytest.mark.parametrize(
    "command",
    ["cat ../outside/secret.txt", "cat escape/secret.txt"],
)
def test_read_only_workspace_terminal_denies_path_and_symlink_escape(
    tmp_path, command
):
    workspace = tmp_path / "work"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("host-secret", encoding="utf-8")
    (workspace / "escape").symlink_to(outside, target_is_directory=True)
    transformed = build_workspace_terminal_args(
        {"command": command},
        workspace=workspace,
        host_home=tmp_path / "host",
        exact_env={"PATH": os.environ["PATH"]},
        read_only=True,
        runtime_root=tmp_path / "runtime",
    )

    result = subprocess.run(
        ["/bin/bash", "-lc", transformed["command"]],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode != 0
    assert "host-secret" not in result.stdout


@pytest.mark.skipif(os.uname().sysname != "Darwin", reason="macOS sandbox-exec")
def test_workspace_terminal_can_execute_configured_python_toolchain(tmp_path):
    workspace = Path.cwd().resolve()
    proof = workspace / ".hermes-claude-runtime" / "toolchain-proof"
    proof.unlink(missing_ok=True)
    version_checks = " && ".join(
        f"{shlex.quote(path)} --version >/dev/null"
        for path in (shutil.which("uv"), shutil.which("rg"), shutil.which("git"))
        if path
    )
    transformed = build_workspace_terminal_args(
        {
                "command": (
                f"{version_checks} && {shlex.quote(sys.executable)} -c "
                "\"from pathlib import Path; "
                "Path('.hermes-claude-runtime/toolchain-proof').write_text('ok')\""
            )
        },
        workspace=workspace,
        host_home=tmp_path / "host",
        exact_env={"PATH": os.environ["PATH"]},
    )

    result = subprocess.run(
        ["/bin/bash", "-lc", transformed["command"]],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=10,
    )

    try:
        assert result.returncode == 0, result.stderr
        assert proof.read_text(encoding="utf-8") == "ok"
    finally:
        proof.unlink(missing_ok=True)


@pytest.mark.skipif(os.uname().sysname != "Darwin", reason="macOS sandbox-exec")
def test_workspace_terminal_denies_homebrew_configuration_reads(tmp_path):
    candidates = [
        path
        for root in (Path("/opt/homebrew/etc"), Path("/usr/local/etc"))
        if root.exists()
        for path in root.rglob("*")
        if path.is_file()
    ]
    if not candidates:
        pytest.skip("no local package-manager configuration file is present")
    workspace = tmp_path / "work"
    workspace.mkdir()
    transformed = build_workspace_terminal_args(
        {"command": f"cat {shlex.quote(str(candidates[0]))}"},
        workspace=workspace,
        host_home=tmp_path / "host",
        exact_env={"PATH": os.environ["PATH"]},
    )

    result = subprocess.run(
        ["/bin/bash", "-lc", transformed["command"]],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode != 0
    assert "Operation not permitted" in result.stderr


# ---------------------------------------------------------------------------
# BUILD-700 Slice 1: benign introspection probes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "command",
    [
        "git --version",
        "git version",
        "python --version",
        "python3 -V",
        "node --version",
        "rg --version",
        "sqlite3 --version",
        "env",
        "printenv",
    ],
)
def test_benign_introspection_probes_are_admitted(command):
    from agent.claude_workspace_terminal import _validate_read_only_terminal_command

    # Returns False = admitted and needs no mirror.
    assert _validate_read_only_terminal_command(command) is False


@pytest.mark.parametrize(
    "command",
    [
        # Payload smuggled after the version flag.
        "python --version -c import os; os.system('id')",
        "git --version --exec-path=/tmp/evil",
        # Still-mutating git despite looking like a probe.
        "git version-bump",
        "git commit --version",
        # env with an assignment runs a program under a modified environment.
        "env FOO=bar sh",
        "printenv PATH extra",
        # Path override must stay rejected even for a version probe.
        "/usr/bin/git --version",
        "./python --version",
        # Not on the introspection tool list.
        "curl --version",
        "bash --version",
    ],
)
def test_introspection_allowlist_does_not_widen_anything_else(command):
    from agent.claude_workspace_terminal import _validate_read_only_terminal_command

    with pytest.raises(RuntimeError):
        _validate_read_only_terminal_command(command)
