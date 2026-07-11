"""Worktree-confined command wrapper for Claude's Hermes terminal MCP."""

from __future__ import annotations

import json
import hashlib
import platform
import shlex
import sys
import os
import shutil
import stat
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home


def _seatbelt_string(path: Path) -> str:
    return json.dumps(str(path), ensure_ascii=False)


def _reject_linked_workspace_files(root: Path) -> None:
    """Fail closed if a regular file could alias a path outside the workspace."""

    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            raise RuntimeError(f"Could not inspect worker workspace: {directory}") from exc
        for entry in entries:
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise RuntimeError(f"Could not inspect workspace path: {entry.path}") from exc
            if stat.S_ISDIR(info.st_mode):
                pending.append(Path(entry.path))
            elif stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
                raise RuntimeError(
                    "Workspace terminal rejects hard-linked regular file: "
                    f"{entry.path}. Recreate dependency files in copy mode "
                    "(for uv, set UV_LINK_MODE=copy)."
                )


def _metadata_ancestors(path: Path) -> list[Path]:
    return [path, *path.parents]


def _write_terminal_profile(profile: str) -> Path:
    """Persist a stable, owner-only Seatbelt profile outside the workspace."""

    directory = get_hermes_home() / "cache" / "claude-agent-sdk" / "terminal-profiles"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    digest = hashlib.sha256(profile.encode("utf-8")).hexdigest()
    path = directory / f"{digest}.sb"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        fd = -1
    if fd >= 0:
        try:
            data = profile.encode("utf-8")
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
        finally:
            os.close(fd)
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()  # windows-footgun: ok — after Darwin-only Seatbelt guard
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
        or path.read_text(encoding="utf-8") != profile
    ):
        raise RuntimeError("Claude terminal Seatbelt profile failed integrity checks")
    return path


def build_workspace_seatbelt_profile(
    *,
    workspace: str | Path,
    host_home: str | Path,
    allow_network: bool,
    readable_roots: list[str | Path] | None = None,
    readable_paths: list[str | Path] | None = None,
    restrict_reads: bool = True,
    control_write_paths: list[str | Path] | None = None,
    control_write_roots: list[str | Path] | None = None,
    git_object_roots: list[str | Path] | None = None,
) -> str:
    """Build a deny-by-default write boundary with workspace-only effects."""

    root = Path(workspace).expanduser().resolve()
    host = Path(host_home).expanduser().resolve()
    # The control-plane Claude CLI still uses the legacy allow-default branch
    # with narrow write denials. The model-callable terminal always takes the
    # restrict_reads branch below, which is a stable default-deny policy.
    lines = ["(version 1)"]
    if restrict_reads:
        lines.extend(
            [
                "(deny default)",
                '(import "system.sb")',
                "(allow process-exec)",
                "(allow process-fork)",
                "(allow process-info* (target self))",
                "(allow signal (target children))",
            ]
        )
        if not allow_network:
            lines.append("(deny network*)")
    else:
        lines.append("(allow default)")
        if not allow_network:
            lines.append("(deny network*)")
    lines.extend(
        [
            "(deny file-write*)",
            f"(allow file-write* (subpath {_seatbelt_string(root)}))",
            '(allow file-write* (literal "/dev/null"))',
        ]
    )
    if restrict_reads:
        lines.append(f"(allow file-read* (subpath {_seatbelt_string(root)}))")
        for ancestor in _metadata_ancestors(root):
            lines.append(
                f"(allow file-read-metadata (literal {_seatbelt_string(ancestor)}))"
            )
    del host  # retained in the interface to make the protected boundary explicit
    for system_root in (
        "/System",
        "/usr",
        "/bin",
        "/sbin",
        "/dev",
    ):
        if restrict_reads:
            lines.append(f'(allow file-read* (subpath "{system_root}"))')
    if restrict_reads:
        # `/usr` is required for macOS runtime files, but these locally
        # managed descendants can contain service credentials/package state.
        for private_local_root in (
            "/usr/local/etc",
            "/usr/local/var",
            "/opt/homebrew/etc",
            "/opt/homebrew/var",
        ):
            lines.append(f'(deny file-read* (subpath "{private_local_root}"))')
    for readable in readable_roots or []:
        lexical = Path(readable).expanduser().absolute()
        if restrict_reads:
            for path in dict.fromkeys((lexical, lexical.resolve())):
                lines.append(f"(allow file-read* (subpath {_seatbelt_string(path)}))")
                lines.append(
                    f"(allow file-read-metadata (literal {_seatbelt_string(path)}))"
                )
                for parent in path.parents:
                    lines.append(
                        f"(allow file-read-metadata (literal {_seatbelt_string(parent)}))"
                    )
    for readable_path in readable_paths or []:
        lexical = Path(readable_path).expanduser().absolute()
        for path in dict.fromkeys((lexical, lexical.resolve())):
            lines.append(f"(allow file-read* (literal {_seatbelt_string(path)}))")
            for parent in path.parents:
                lines.append(
                    f"(allow file-read-metadata (literal {_seatbelt_string(parent)}))"
                )
    for writable in control_write_paths or []:
        path = Path(writable).expanduser().resolve(strict=False)
        lines.append(f"(allow file-write* (literal {_seatbelt_string(path)}))")
    for writable_root in control_write_roots or []:
        path = Path(writable_root).expanduser().resolve(strict=False)
        lines.append(f"(allow file-write* (subpath {_seatbelt_string(path)}))")
    for object_root in git_object_roots or []:
        path = Path(object_root).expanduser().resolve(strict=False)
        hex_pair = "[0-9a-f][0-9a-f]"
        sha1_tail = "[0-9a-f]" * 38
        temp_suffix = "[A-Za-z0-9]" * 6
        fanout_pattern = json.dumps(f"/{hex_pair}$")
        loose_pattern = json.dumps(f"/{hex_pair}/{sha1_tail}$")
        temp_pattern = json.dumps(f"/tmp_obj_{temp_suffix}$")
        # Git creates an immutable SHA-1 loose object through a six-character
        # tmp_obj_* file. Permit only that exact lifecycle: create the two-hex
        # fan-out directory, temporary file, and 38-hex tail; mutate/unlink only
        # the temporary file. Existing objects and objects/{info,pack} remain
        # immutable.
        lines.append(
            "(allow file-write-create "
            f"(require-all (subpath {_seatbelt_string(path)}) "
            f"(require-any (regex #{fanout_pattern}) "
            f"(regex #{loose_pattern}) (regex #{temp_pattern}))))"
        )
        lines.append(
            "(allow file-write-create file-write-data file-write-mode "
            "file-write-unlink "
            f"(require-all (subpath {_seatbelt_string(path)}) "
            f"(regex #{temp_pattern})))"
        )
    return "\n".join(lines)


@dataclass(frozen=True)
class _GitSandboxMetadata:
    common_dir: Path
    object_dir: Path | None
    control_write_paths: tuple[Path, ...]


def _selected_git(exact_path: str) -> Path | None:
    """Resolve Git outside Seatbelt, avoiding Apple's xcrun launcher."""

    actual_darwin = platform.system() == "Darwin"
    if actual_darwin:
        # Only probe fixed toolchain locations outside the sandbox. Executing
        # an arbitrary PATH candidate here would let a workspace-controlled
        # `git` run before Seatbelt starts.
        for candidate in (Path("/opt/homebrew/bin/git"), Path("/usr/local/bin/git")):
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate.absolute()
        try:
            result = subprocess.run(
                ["/usr/bin/xcrun", "--find", "git"],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            )
            candidate = Path(result.stdout.strip())
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate.absolute()
        except (OSError, subprocess.SubprocessError):
            pass
        return None
    ambient = shutil.which("git", path=exact_path) or shutil.which("git")
    return Path(ambient).absolute() if ambient else None


def _git_output(git: Path, root: Path, *arguments: str) -> str:
    clean_env = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    clean_env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "HOME": str(root),
        }
    )
    result = subprocess.run(
        [str(git), "-C", str(root), *arguments],
        env=clean_env,
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    return result.stdout.strip()


def _git_sandbox_metadata(root: Path, git: Path | None) -> _GitSandboxMetadata | None:
    """Discover the minimum external metadata needed by a linked worktree."""

    if git is None:
        return None
    try:
        common_dir = Path(
            _git_output(
                git,
                root,
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            )
        ).expanduser().resolve()
        git_dir = Path(
            _git_output(
                git,
                root,
                "rev-parse",
                "--path-format=absolute",
                "--git-dir",
            )
        ).expanduser().resolve()
        if not common_dir.is_dir() or not git_dir.is_dir():
            return None
        if git_dir.is_relative_to(root):
            return _GitSandboxMetadata(common_dir, None, ())
        linked_root = common_dir / "worktrees"
        if not git_dir.is_relative_to(linked_root):
            raise RuntimeError(
                "Claude terminal rejected Git metadata outside the workspace: "
                f"{git_dir}"
            )
        workspace_dotgit = root / ".git"
        gitdir_backpointer = git_dir / "gitdir"
        commondir_pointer = git_dir / "commondir"

        def _regular_single_link(path: Path) -> bool:
            try:
                info = path.lstat()
            except OSError:
                return False
            return stat.S_ISREG(info.st_mode) and info.st_nlink == 1

        if not all(
            _regular_single_link(path)
            for path in (workspace_dotgit, gitdir_backpointer, commondir_pointer)
        ):
            raise RuntimeError("Claude terminal rejected mutable linked-worktree pointers")
        dotgit_text = workspace_dotgit.read_text(encoding="utf-8").strip()
        if not dotgit_text.startswith("gitdir:"):
            raise RuntimeError("Claude terminal rejected malformed workspace .git file")
        dotgit_target = Path(dotgit_text.split(":", 1)[1].strip()).expanduser()
        if not dotgit_target.is_absolute():
            dotgit_target = workspace_dotgit.parent / dotgit_target
        backpointer_target = Path(
            gitdir_backpointer.read_text(encoding="utf-8").strip()
        ).expanduser()
        if not backpointer_target.is_absolute():
            backpointer_target = git_dir / backpointer_target
        commondir_target = Path(
            commondir_pointer.read_text(encoding="utf-8").strip()
        ).expanduser()
        if not commondir_target.is_absolute():
            commondir_target = git_dir / commondir_target
        if (
            dotgit_target.resolve() != git_dir
            or backpointer_target.resolve() != workspace_dotgit
            or commondir_target.resolve() != common_dir
        ):
            raise RuntimeError(
                "Claude terminal rejected linked-worktree metadata backpointers"
            )
        object_dir = (common_dir / "objects").resolve()
        if not object_dir.is_dir() or not object_dir.is_relative_to(common_dir):
            raise RuntimeError(
                "Claude terminal rejected Git object storage outside the common "
                f"directory: {object_dir}"
            )
        control_paths: list[Path] = [
            git_dir / "index",
            git_dir / "index.lock",
            git_dir / "COMMIT_EDITMSG",
            git_dir / "HEAD.lock",
            git_dir / "logs" / "HEAD",
            git_dir / "logs" / "HEAD.lock",
        ]
        try:
            branch = _git_output(git, root, "symbolic-ref", "--quiet", "HEAD")
        except subprocess.CalledProcessError:
            branch = ""
        if branch:
            if not branch.startswith("refs/heads/"):
                raise RuntimeError(f"Claude terminal rejected unexpected Git ref: {branch}")
            branch_path = common_dir / branch
            ref = branch_path.resolve(strict=False)
            heads = (common_dir / "refs" / "heads").resolve(strict=False)
            if ref != branch_path or not ref.is_relative_to(heads):
                raise RuntimeError(f"Claude terminal rejected Git ref outside heads: {branch}")
            ref_lock = Path(f"{ref}.lock")
            reflog_path = common_dir / "logs" / branch
            reflog = reflog_path.resolve(strict=False)
            logs_heads = (common_dir / "logs" / "refs" / "heads").resolve(
                strict=False
            )
            reflog_lock = Path(f"{reflog}.lock")
            if (
                ref_lock.resolve(strict=False) != ref_lock
                or reflog != reflog_path
                or not reflog.is_relative_to(logs_heads)
                or reflog_lock.resolve(strict=False) != reflog_lock
            ):
                raise RuntimeError(
                    f"Claude terminal rejected Git reflog outside heads: {branch}"
                )
            control_paths.extend(
                [
                    ref,
                    ref_lock,
                    reflog,
                    reflog_lock,
                ]
            )
        for path in control_paths:
            if path.resolve(strict=False) != path:
                raise RuntimeError(
                    f"Claude terminal rejected linked Git write path: {path}"
                )
            if path.exists() and not _regular_single_link(path):
                raise RuntimeError(
                    f"Claude terminal rejected mutable linked Git write path: {path}"
                )
        return _GitSandboxMetadata(
            common_dir=common_dir,
            object_dir=object_dir,
            control_write_paths=tuple(control_paths),
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _mach_o_dependencies(paths: list[Path]) -> list[Path]:
    """Return exact existing dylib paths needed by the selected executables."""

    otool = Path("/usr/bin/otool")
    if not otool.exists():
        return []
    dependencies: list[Path] = []
    pending = list(paths)
    inspected: set[Path] = set()
    while pending and len(inspected) < 128:
        candidate = pending.pop()
        canonical = candidate.resolve(strict=False)
        if canonical in inspected or not canonical.is_file():
            continue
        inspected.add(canonical)
        try:
            result = subprocess.run(
                [str(otool), "-L", str(canonical)],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        for line in result.stdout.splitlines()[1:]:
            raw = line.strip().split(" ", 1)[0]
            if not raw.startswith("/"):
                continue
            dependency = Path(raw)
            if dependency.exists() and dependency not in dependencies:
                dependencies.append(dependency)
                pending.append(dependency)
    return dependencies


def _homebrew_formula_roots(paths: list[Path]) -> list[Path]:
    """Return immutable versioned Cellar roots for selected Homebrew tools."""

    roots: list[Path] = []
    for path in paths:
        parts = path.resolve(strict=False).parts
        try:
            cellar_index = parts.index("Cellar")
        except ValueError:
            continue
        if len(parts) < cellar_index + 3:
            continue
        root = Path(*parts[: cellar_index + 3])
        if root.is_dir() and root not in roots:
            roots.append(root)
    return roots


def build_workspace_terminal_args(
    arguments: Mapping[str, Any],
    *,
    workspace: str | Path,
    host_home: str | Path,
    exact_env: Mapping[str, str],
    platform_name: str | None = None,
) -> dict[str, Any]:
    """Wrap a Hermes terminal call in exact-env macOS Seatbelt isolation."""

    if (platform_name or platform.system()) != "Darwin":
        raise RuntimeError("Workspace terminal sandbox is unsupported on this OS")
    root = Path(workspace).expanduser().resolve()
    host = Path(host_home).expanduser().resolve()
    if not root.is_dir():
        raise RuntimeError(f"Worker workspace does not exist: {root}")
    command = str(arguments.get("command") or "").strip()
    if not command:
        raise RuntimeError("Workspace terminal requires a command")
    _reject_linked_workspace_files(root)
    git = _selected_git(str(exact_env.get("PATH", "")))
    executable_paths = [
        Path(path)
        for path in (
            sys.executable,
            shutil.which("uv"),
            shutil.which("rg"),
            str(git) if git else None,
            str(root / ".venv" / "bin" / "python"),
            str(root / ".venv" / "bin" / "python3"),
        )
        if path and Path(path).exists()
    ]
    for executable in list(executable_paths):
        if executable.is_symlink():
            target = Path(os.readlink(executable))
            if not target.is_absolute():
                target = executable.parent / target
            executable_paths.append(target.absolute())
    executable_paths.extend(_mach_o_dependencies(executable_paths))

    git_metadata = _git_sandbox_metadata(root, git)
    toolchain_roots = [
        Path(path)
        for path in (
            str(Path(sys.executable).resolve().parents[1]),
        )
        if Path(path).exists()
    ]
    toolchain_roots.extend(_homebrew_formula_roots(executable_paths))
    if git_metadata is not None and not git_metadata.common_dir.is_relative_to(root):
        toolchain_roots.append(git_metadata.common_dir)
    if git is not None and git.is_relative_to(Path("/Library/Developer")):
        toolchain_roots.append(Path("/Library/Developer"))
    profile = build_workspace_seatbelt_profile(
        workspace=root,
        host_home=host,
        allow_network=False,
        readable_roots=toolchain_roots,
        readable_paths=executable_paths,
        control_write_paths=(
            list(git_metadata.control_write_paths) if git_metadata else None
        ),
        git_object_roots=(
            [git_metadata.object_dir]
            if git_metadata and git_metadata.object_dir
            else None
        ),
    )
    profile_path = _write_terminal_profile(profile)
    allowed_env_keys = {
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LOGNAME",
        "PATH",
        "SHELL",
        "TERM",
        "TMPDIR",
        "TZ",
        "USER",
    }
    terminal_env = {
        key: str(value)
        for key, value in exact_env.items()
        if key in allowed_env_keys
    }
    if git is not None:
        existing_path = terminal_env.get("PATH", "")
        terminal_env["PATH"] = os.pathsep.join(
            part for part in (str(git.parent), existing_path) if part
        )
    terminal_env["HOME"] = str(root)
    terminal_tmp = root / ".hermes-claude-runtime" / "tmp"
    terminal_tmp.mkdir(mode=0o700, parents=True, exist_ok=True)
    terminal_tmp.chmod(0o700)
    terminal_env["TMPDIR"] = str(terminal_tmp)
    terminal_env["GIT_CONFIG_NOSYSTEM"] = "1"
    terminal_env["GIT_OPTIONAL_LOCKS"] = "0"
    terminal_env["GIT_CONFIG_COUNT"] = "2"
    terminal_env["GIT_CONFIG_KEY_0"] = "maintenance.auto"
    terminal_env["GIT_CONFIG_VALUE_0"] = "false"
    terminal_env["GIT_CONFIG_KEY_1"] = "gc.auto"
    terminal_env["GIT_CONFIG_VALUE_1"] = "0"
    terminal_env["UV_LINK_MODE"] = "copy"
    env_argv = [f"{key}={value}" for key, value in sorted(terminal_env.items())]
    wrapped_argv = [
        "/usr/bin/env",
        "-i",
        *env_argv,
        "/usr/bin/sandbox-exec",
        "-f",
        str(profile_path),
        "/bin/bash",
        "--noprofile",
        "--norc",
        "-c",
        command,
    ]
    transformed = dict(arguments)
    workdir = Path(str(arguments.get("workdir") or root)).expanduser()
    if not workdir.is_absolute():
        workdir = root / workdir
    resolved_workdir = workdir.resolve(strict=False)
    if resolved_workdir != root and not resolved_workdir.is_relative_to(root):
        raise RuntimeError("Workspace terminal workdir is outside the worker workspace")
    transformed["workdir"] = str(resolved_workdir)
    transformed["command"] = shlex.join(wrapped_argv)
    return transformed


__all__ = ["build_workspace_seatbelt_profile", "build_workspace_terminal_args"]
