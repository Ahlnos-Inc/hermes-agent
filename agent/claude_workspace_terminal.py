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
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from agent.claude_workspace_policy import is_workspace_credential_path
from hermes_constants import get_hermes_home


_READ_ONLY_COMMANDS = frozenset(
    {
        "cat",
        "cmp",
        "diff",
        "file",
        "git",
        "grep",
        "head",
        "ls",
        "pwd",
        "rg",
        "stat",
        "tail",
        "test",
        "wc",
    }
)
# BUILD-700 Slice 1. Verifier/reviewer lanes legitimately probe toolchain
# versions before deciding they are capability-blocked, and rejecting
# `git --version` taught them to report a false capability block instead.
# These probes are admitted ONLY as an exact whole-command match — see
# `_is_benign_introspection` — so this widens nothing else: no arguments, no
# subcommands, no path overrides, and no shell metacharacters reach the tool.
_INTROSPECTION_TOOLS = frozenset(
    {
        "cargo", "git", "go", "jq", "make", "node", "npm", "pnpm",
        "python", "python3", "rg", "rustc", "sqlite3", "uv", "yarn",
    }
)
_INTROSPECTION_FLAGS = frozenset({"--version", "-V", "version"})
# Bare environment dumps carry no arguments and cannot mutate.
_INTROSPECTION_BARE = frozenset({"env", "printenv"})
_READ_ONLY_GIT_SUBCOMMANDS = frozenset(
    {
        "blame",
        "cat-file",
        "describe",
        "diff",
        "for-each-ref",
        "grep",
        "log",
        "ls-files",
        "ls-tree",
        "merge-base",
        "name-rev",
        "rev-parse",
        "shortlog",
        "show",
        "show-ref",
        "status",
    }
)
_READ_ONLY_SHELL_ESCAPE_MARKERS = ("\n", "\r", ";", "&", "|", ">", "<", "`", "$(", "${")
_GIT_HELPER_OPTIONS = (
    "--exec",
    "--ext-diff",
    "--filters",
    "--open-files-in-pager",
    "--textconv",
)


def _git_subcommand(tokens: list[str]) -> str:
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token in {"--no-pager", "--paginate"}:
            index += 1
            continue
        if token == "-C" and index + 1 < len(tokens):
            index += 2
            continue
        if token.startswith("-"):
            return ""
        return token
    return ""


def _is_project_test_runner(executable_path: Path) -> bool:
    return (
        not executable_path.is_absolute()
        and executable_path.parts == ("scripts", "run_tests.sh")
    )


def _is_read_only_test_command(tokens: list[str]) -> bool:
    if not tokens:
        return False
    executable_path = Path(tokens[0])
    executable = executable_path.name
    if _is_project_test_runner(executable_path):
        return True
    if executable in {"py.test", "pytest"}:
        return True
    if executable.startswith("python"):
        return len(tokens) >= 3 and tokens[1] == "-m" and tokens[2] in {
            "pytest",
            "unittest",
        }
    if executable == "uv":
        return len(tokens) >= 3 and tokens[1] == "run" and _is_read_only_test_command(
            tokens[2:]
        )
    if executable == "ruff":
        return len(tokens) >= 2 and tokens[1] == "check"
    if executable in {"npm", "pnpm", "yarn", "bun"}:
        if len(tokens) >= 2 and tokens[1] == "test":
            return True
        return (
            len(tokens) >= 3
            and tokens[1] == "run"
            and tokens[2] in {"check", "lint", "test", "typecheck"}
        )
    if executable == "cargo":
        return len(tokens) >= 2 and tokens[1] in {"check", "clippy", "test"}
    if executable == "go":
        return len(tokens) >= 2 and tokens[1] in {"test", "vet"}
    if executable == "make":
        return len(tokens) >= 2 and tokens[1] in {"check", "lint", "test"}
    return False


def _has_option(
    tokens: list[str],
    options: tuple[str, ...],
    *,
    allow_long_abbreviation: bool = False,
) -> bool:
    for token in tokens:
        token_name = token.split("=", 1)[0]
        for option in options:
            if token_name == option:
                return True
            if option.startswith("-") and not option.startswith("--"):
                if token.startswith(option):
                    return True
            if (
                allow_long_abbreviation
                and token_name.startswith("--")
                and len(token_name) > 2
                and option.startswith(token_name)
            ):
                return True
    return False


def _is_benign_introspection(raw_executable: str, tokens: list[str]) -> bool:
    """True only for an exact version/env probe that cannot carry a payload.

    Whole-command exact match by construction: a bare `env`/`printenv`, or a
    known tool followed by exactly one version flag. Anything with extra
    arguments, a subcommand, or a path-qualified executable falls through to
    the normal mutation-capable rejection.
    """
    if "/" in raw_executable:
        return False
    if len(tokens) == 1:
        return tokens[0] in _INTROSPECTION_BARE
    if len(tokens) == 2:
        return (
            tokens[0] in _INTROSPECTION_TOOLS
            and tokens[1] in _INTROSPECTION_FLAGS
        )
    return False


def _validate_read_only_terminal_command(command: str) -> bool:
    """Validate a reviewer/verifier command and return whether it needs a mirror."""

    if any(marker in command for marker in _READ_ONLY_SHELL_ESCAPE_MARKERS):
        raise RuntimeError(
            "Read-only worker terminal rejected shell control or redirection"
        )
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError as exc:
        raise RuntimeError("Read-only terminal command could not be parsed") from exc
    raw_executable = tokens[0] if tokens else ""
    executable_path = Path(raw_executable)
    executable = executable_path.name
    if "/" in raw_executable and not _is_project_test_runner(executable_path):
        raise RuntimeError(
            "Read-only worker terminal rejected executable path override"
        )
    # Admitted only after the shell-escape and path-override checks above, so an
    # introspection probe can never smuggle a redirection or an absolute path.
    if _is_benign_introspection(raw_executable, tokens):
        return False
    if executable not in _READ_ONLY_COMMANDS and not _is_read_only_test_command(tokens):
        raise RuntimeError(
            f"Read-only worker terminal rejected mutation-capable command: {executable or '<empty>'}"
        )
    if executable == "git" and _git_subcommand(tokens) not in _READ_ONLY_GIT_SUBCOMMANDS:
        raise RuntimeError("Read-only worker terminal rejected mutating git command")
    if executable == "git" and (
        _has_option(
            tokens[2:], _GIT_HELPER_OPTIONS, allow_long_abbreviation=True
        )
        or any(token == "-O" or token.startswith("-O") for token in tokens[2:])
    ):
        raise RuntimeError(
            "Read-only worker terminal rejected external git helper command"
        )
    if executable == "rg" and _has_option(tokens[1:], ("--pre",)):
        raise RuntimeError("Read-only worker terminal rejected external rg helper")
    if executable == "go" and _has_option(tokens[2:], ("-exec",)):
        raise RuntimeError("Read-only worker terminal rejected external go helper")
    if executable in {"npm", "pnpm", "yarn", "bun"} and _has_option(
        tokens[2:], ("--script-shell",)
    ):
        raise RuntimeError("Read-only worker terminal rejected external script shell")
    if executable == "cargo" and _has_option(tokens[2:], ("--config",)):
        raise RuntimeError("Read-only worker terminal rejected cargo helper override")
    if executable == "make" and _has_option(
        tokens[1:], ("--eval", "--file", "--makefile", "-f")
    ):
        raise RuntimeError("Read-only worker terminal rejected makefile override")
    return _is_read_only_test_command(tokens)


def _normalize_read_only_terminal_command(
    command: str,
    *,
    workspace: Path,
    exact_env: Mapping[str, str],
) -> tuple[str, bool]:
    """Resolve the approved executable without consulting the model's shell path."""

    use_mirror = _validate_read_only_terminal_command(command)
    tokens = shlex.split(command, posix=True)
    executable_path = Path(tokens[0])
    executable = executable_path.name
    if _is_project_test_runner(executable_path):
        return shlex.join(tokens), use_mirror
    if executable in {"pytest", "py.test", "ruff"}:
        module = "pytest" if executable in {"pytest", "py.test"} else "ruff"
        tokens = [sys.executable, "-m", module, *tokens[1:]]
        return shlex.join(tokens), use_mirror
    if executable.startswith("python"):
        tokens[0] = sys.executable
        return shlex.join(tokens), use_mirror
    if executable == "git":
        selected_git = _selected_git(str(exact_env.get("PATH") or os.defpath))
        resolved = str(selected_git) if selected_git is not None else None
    else:
        resolved = shutil.which(
            executable, path=str(exact_env.get("PATH") or os.defpath)
        )
    if not resolved:
        raise RuntimeError(
            f"Read-only worker terminal could not resolve approved executable: {executable}"
        )
    resolved_path = Path(resolved).resolve()
    try:
        resolved_path.relative_to(workspace)
    except ValueError:
        pass
    else:
        raise RuntimeError(
            "Read-only worker terminal rejected workspace-selected executable"
        )
    tokens[0] = str(resolved_path)
    return shlex.join(tokens), use_mirror


def _seatbelt_string(path: Path) -> str:
    return json.dumps(str(path), ensure_ascii=False)


class WorkspaceBoundaryProvisioningError(RuntimeError):
    """The immutable workspace write boundary could not be prepared."""


@dataclass(frozen=True)
class WorkspaceTerminalBoundary:
    """The prepared write boundary shared by every terminal capability."""

    root: Path
    readonly_subtrees: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        root = Path(self.root).expanduser().resolve(strict=False)
        normalized: list[Path] = []
        for raw_path in self.readonly_subtrees:
            path = Path(raw_path).expanduser().resolve(strict=False)
            if path == root or not path.is_relative_to(root):
                raise WorkspaceBoundaryProvisioningError(
                    "Workspace terminal read-only subtree is outside the workspace"
                )
            if any(path == existing or path.is_relative_to(existing) for existing in normalized):
                continue
            normalized.append(path)
        normalized.sort(key=lambda path: (len(path.parts), str(path)))
        object.__setattr__(self, "root", root)
        object.__setattr__(self, "readonly_subtrees", tuple(normalized))

    def retarget(self, root: str | Path) -> "WorkspaceTerminalBoundary":
        """Map the boundary's relative read-only roots onto a mirror root."""

        target_root = Path(root).expanduser().resolve(strict=False)
        mapped = tuple(
            target_root / subtree.relative_to(self.root)
            for subtree in self.readonly_subtrees
        )
        return WorkspaceTerminalBoundary(target_root, mapped)


def _path_is_readonly_subtree(path: Path, boundary: WorkspaceTerminalBoundary) -> bool:
    canonical = path.expanduser().resolve(strict=False)
    return any(
        canonical == subtree or canonical.is_relative_to(subtree)
        for subtree in boundary.readonly_subtrees
    )


def _metadata_ancestors(path: Path) -> list[Path]:
    return [path, *path.parents]


def _workspace_credential_paths(root: Path) -> list[Path]:
    return [
        path.resolve()
        for path in root.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and is_workspace_credential_path(path)
    ]


def _write_terminal_profile(profile: str, *, directory: Path | None = None) -> Path:
    """Persist a stable, owner-only Seatbelt profile outside the workspace."""

    directory = directory or (
        get_hermes_home() / "cache" / "claude-agent-sdk" / "terminal-profiles"
    )
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
    workspace_writable: bool = True,
    denied_read_paths: list[str | Path] | None = None,
    denied_write_roots: list[str | Path] | None = None,
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
            "(deny file-link)",
            '(allow file-write* (literal "/dev/null"))',
        ]
    )
    if workspace_writable:
        lines.append(f"(allow file-write* (subpath {_seatbelt_string(root)}))")
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
    for denied_path in denied_read_paths or []:
        path = Path(denied_path).expanduser().resolve(strict=False)
        lines.append(f"(deny file-read* (literal {_seatbelt_string(path)}))")
    for denied_root in denied_write_roots or []:
        path = Path(denied_root).expanduser().resolve(strict=False)
        lines.append(f"(deny file-write* (subpath {_seatbelt_string(path)}))")
    for writable in control_write_paths or []:
        path = Path(writable).expanduser().resolve(strict=False)
        lines.append(f"(allow file-write* (literal {_seatbelt_string(path)}))")
    for writable_root in control_write_roots or []:
        path = Path(writable_root).expanduser().resolve(strict=False)
        lines.append(f"(allow file-write* (subpath {_seatbelt_string(path)}))")
    for object_root in git_object_roots or []:
        path = Path(object_root).expanduser().resolve(strict=False)
        sha1_tail = "[0-9a-f]" * 38
        temp_suffix = "[A-Za-z0-9]" * 6
        # Git creates an immutable SHA-1 loose object through a six-character
        # tmp_obj_* file. Permit only that exact lifecycle: create the two-hex
        # fan-out directory, temporary file, and 38-hex tail; mutate/unlink only
        # the temporary file. Existing objects and objects/{info,pack} remain
        # immutable.
        for value in range(256):
            prefix = f"{value:02x}"
            fanout = path / prefix
            lines.append(
                f"(allow file-write-create (literal {_seatbelt_string(fanout)}))"
            )
        loose_pattern = json.dumps(f"/[0-9a-f][0-9a-f]/{sha1_tail}$")
        temp_pattern = json.dumps(
            f"/[0-9a-f][0-9a-f]/tmp_obj_{temp_suffix}$"
        )
        exclusions = (
            f"(require-not (subpath {_seatbelt_string(path / 'info')})) "
            f"(require-not (subpath {_seatbelt_string(path / 'pack')}))"
        )
        lines.append(
            "(allow file-write-create "
            f"(require-all (subpath {_seatbelt_string(path)}) {exclusions} "
            f"(require-any (regex #{loose_pattern}) (regex #{temp_pattern}))))"
        )
        lines.append(
            "(allow file-write-create file-write-data file-write-mode "
            "file-write-unlink "
            f"(require-all (subpath {_seatbelt_string(path)}) {exclusions} "
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
        for entry in os.scandir(object_dir):
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise RuntimeError(
                    f"Claude terminal could not inspect Git object path: {entry.path}"
                ) from exc
            if not stat.S_ISDIR(info.st_mode):
                continue
            if entry.name in {"info", "pack"}:
                continue
            if len(entry.name) != 2 or any(
                char not in "0123456789abcdef" for char in entry.name
            ):
                raise RuntimeError(
                    f"Claude terminal rejected unexpected Git object directory: {entry.path}"
                )
            for child in os.scandir(entry.path):
                try:
                    child_info = child.stat(follow_symlinks=False)
                except OSError as exc:
                    raise RuntimeError(
                        f"Claude terminal could not inspect Git loose object: {child.path}"
                    ) from exc
                if not stat.S_ISREG(child_info.st_mode):
                    raise RuntimeError(
                        f"Claude terminal rejected nested Git object path: {child.path}"
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


def _registered_worktree_paths(git: Path, root: Path) -> list[Path]:
    """Return paths from Git's registry without interpreting directory names."""

    try:
        raw = _git_output(git, root, "worktree", "list", "--porcelain", "-z")
    except (OSError, subprocess.SubprocessError):
        return []
    paths: list[Path] = []
    for record in raw.split("\x00\x00"):
        for field in record.split("\x00"):
            if field.startswith("worktree "):
                raw_path = field[len("worktree ") :].strip()
                if raw_path:
                    paths.append(Path(raw_path).expanduser())
                break
    return paths


def _discover_attested_nested_worktrees(
    root: Path,
    git: Path | None,
) -> tuple[Path, ...]:
    """Find registered, integrity-checked worktrees nested below ``root``."""

    if git is None:
        return ()
    root_metadata = _git_sandbox_metadata(root, git)
    if root_metadata is None:
        return ()
    candidates: list[Path] = []
    for raw_candidate in _registered_worktree_paths(git, root):
        candidate = raw_candidate.resolve(strict=False)
        if candidate == root or not candidate.is_relative_to(root):
            continue
        if not candidate.is_dir():
            continue
        try:
            candidate_metadata = _git_sandbox_metadata(candidate, git)
        except (OSError, RuntimeError, subprocess.SubprocessError):
            # A registered but malformed candidate is deliberately left in
            # the census. It is not an attested read-only subtree.
            continue
        if candidate_metadata is None or candidate_metadata.object_dir is None:
            continue
        if candidate_metadata.common_dir != root_metadata.common_dir:
            continue
        candidates.append(candidate)
    return tuple(candidates)


def _census_writable_workspace(boundary: WorkspaceTerminalBoundary) -> None:
    """Verify all multiply-linked regular files in the writable scope.

    A directory entry is counted only when it is visible in the effective
    writable scope. This is intentional: an alias in a read-only subtree
    remains an external alias from the included path's point of view and must
    therefore fail the census.
    """

    pending = [boundary.root]
    groups: dict[tuple[int, int], list[Any]] = {}
    while pending:
        directory = pending.pop()
        if _path_is_readonly_subtree(directory, boundary):
            continue
        try:
            entries = os.scandir(directory)
        except OSError as exc:
            raise WorkspaceBoundaryProvisioningError(
                f"Could not inspect worker workspace: {directory}"
            ) from exc
        try:
            for entry in entries:
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    raise WorkspaceBoundaryProvisioningError(
                        f"Could not inspect workspace path: {entry.path}"
                    ) from exc
                path = Path(entry.path)
                if stat.S_ISDIR(info.st_mode):
                    if not _path_is_readonly_subtree(path, boundary):
                        pending.append(path)
                    continue
                if not stat.S_ISREG(info.st_mode) or info.st_nlink <= 1:
                    continue
                key = (int(info.st_dev), int(info.st_ino))
                previous = groups.get(key)
                if previous is None:
                    groups[key] = [1, int(info.st_nlink), path]
                    continue
                previous[0] += 1
                if previous[1] != int(info.st_nlink):
                    raise WorkspaceBoundaryProvisioningError(
                        "Workspace terminal observed inconsistent hard-link metadata: "
                        f"{previous[2]}"
                    )
        finally:
            entries.close()

    for observed_count, link_count, path in groups.values():
        if observed_count != link_count:
            raise WorkspaceBoundaryProvisioningError(
                "Workspace terminal rejects hard-linked regular file with an alias "
                f"outside the writable boundary: {path} "
                f"(observed {observed_count} of {link_count} directory entries). "
                "Recreate dependency files in copy mode (for uv, set "
                "UV_LINK_MODE=copy)."
            )


def prepare_workspace_terminal_boundary(
    workspace: str | Path,
    *,
    git: Path | None = None,
) -> WorkspaceTerminalBoundary:
    """Prepare and attest one immutable terminal write boundary."""

    root = Path(workspace).expanduser().resolve(strict=False)
    if not root.is_dir():
        raise WorkspaceBoundaryProvisioningError(
            f"Worker workspace does not exist: {root}"
        )
    selected_git = git if git is not None else _selected_git(os.defpath)
    try:
        readonly_subtrees = _discover_attested_nested_worktrees(root, selected_git)
        boundary = WorkspaceTerminalBoundary(root, readonly_subtrees)
    except WorkspaceBoundaryProvisioningError:
        raise
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        raise WorkspaceBoundaryProvisioningError(
            str(exc) or f"Could not attest worker workspace boundary: {root}"
        ) from exc
    _census_writable_workspace(boundary)
    return boundary


def _reject_linked_workspace_files(root: Path) -> None:
    """Compatibility wrapper for callers of the former command-time guard."""

    _census_writable_workspace(WorkspaceTerminalBoundary(root))


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
    boundary: WorkspaceTerminalBoundary | None = None,
    platform_name: str | None = None,
    read_only: bool = False,
    runtime_root: str | Path | None = None,
    additional_readable_roots: list[str | Path] | None = None,
    git_metadata_enabled: bool = True,
) -> dict[str, Any]:
    """Wrap a Hermes terminal call in exact-env macOS Seatbelt isolation."""

    # Claude's SDK terminal intentionally has no network-enabled action
    # projection.  A github_write worker must fail closed here instead of
    # silently dropping its grant or pretending that the SDK boundary is
    # authorized.
    from hermes_cli.worker_credentials import has_trusted_worker_action

    if has_trusted_worker_action("github_write"):
        raise RuntimeError(
            "Claude SDK workspace terminal does not support github_write; "
            "use the authorized local terminal boundary"
        )

    root = Path(workspace).expanduser().resolve()
    command = str(arguments.get("command") or "").strip()
    if not command:
        raise RuntimeError("Workspace terminal requires a command")
    if read_only:
        command, use_mirror = _normalize_read_only_terminal_command(
            command,
            workspace=root,
            exact_env=exact_env,
        )
        if use_mirror:
            raise RuntimeError(
                "Read-only test/build commands require a disposable workspace mirror"
            )
        if runtime_root is None:
            raise RuntimeError("Read-only terminal requires host-managed runtime scratch")
    if (platform_name or platform.system()) != "Darwin":
        raise RuntimeError("Workspace terminal sandbox is unsupported on this OS")
    host = Path(host_home).expanduser().resolve()
    if not root.is_dir():
        raise RuntimeError(f"Worker workspace does not exist: {root}")
    git = _selected_git(str(exact_env.get("PATH", "")))
    if boundary is None:
        boundary = prepare_workspace_terminal_boundary(root, git=git)
    elif boundary.root != root:
        raise RuntimeError(
            "Workspace terminal boundary does not match the worker workspace"
        )
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

    git_metadata = _git_sandbox_metadata(root, git) if git_metadata_enabled else None
    toolchain_paths = [
        str(Path(sys.executable).resolve().parents[1]),
        # A venv's interpreter commonly resolves to its base Python outside
        # the venv. Keep the venv itself readable so its site-packages remain
        # importable for normal coder/architect workers as well as mirrors.
        str(Path(sys.prefix).resolve()),
    ]
    toolchain_roots = [Path(path) for path in toolchain_paths if Path(path).exists()]
    toolchain_roots.extend(_homebrew_formula_roots(executable_paths))
    toolchain_roots.extend(
        Path(path).expanduser().resolve()
        for path in additional_readable_roots or []
    )
    if git_metadata is not None and not git_metadata.common_dir.is_relative_to(root):
        toolchain_roots.append(git_metadata.common_dir)
    if git is not None and git.is_relative_to(Path("/Library/Developer")):
        toolchain_roots.append(Path("/Library/Developer"))
    runtime_base = (
        Path(runtime_root).expanduser().resolve()
        if runtime_root is not None
        else root / ".hermes-claude-runtime"
    )
    if _path_is_readonly_subtree(runtime_base, boundary):
        raise RuntimeError(
            "Workspace terminal runtime scratch is inside a read-only worktree"
        )
    runtime_base.mkdir(mode=0o700, parents=True, exist_ok=True)
    runtime_base.chmod(0o700)
    profile = build_workspace_seatbelt_profile(
        workspace=root,
        host_home=host,
        allow_network=False,
        readable_roots=[*toolchain_roots, runtime_base],
        readable_paths=executable_paths,
        control_write_paths=(
            list(git_metadata.control_write_paths)
            if git_metadata and not read_only
            else None
        ),
        git_object_roots=(
            [git_metadata.object_dir]
            if git_metadata and git_metadata.object_dir and not read_only
            else None
        ),
        workspace_writable=not read_only,
        denied_read_paths=_workspace_credential_paths(root) if read_only else [],
        denied_write_roots=list(boundary.readonly_subtrees),
        control_write_roots=[runtime_base],
    )
    profile_path = _write_terminal_profile(
        profile,
        directory=runtime_base / "profiles" if runtime_root is not None else None,
    )
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
    terminal_home = root if runtime_root is None else runtime_base / "home"
    if runtime_root is not None:
        terminal_home.mkdir(mode=0o700, parents=True, exist_ok=True)
        terminal_home.chmod(0o700)
    terminal_env["HOME"] = str(terminal_home)
    terminal_tmp = runtime_base / "tmp"
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
    terminal_env["GIT_PAGER"] = "cat"
    terminal_env["PAGER"] = "cat"
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
    transformed["command"] = command
    workdir = Path(str(arguments.get("workdir") or root)).expanduser()
    if not workdir.is_absolute():
        workdir = root / workdir
    resolved_workdir = workdir.resolve(strict=False)
    if resolved_workdir != root and not resolved_workdir.is_relative_to(root):
        raise RuntimeError("Workspace terminal workdir is outside the worker workspace")
    transformed["workdir"] = str(resolved_workdir)
    transformed["command"] = shlex.join(wrapped_argv)
    return transformed


_MIRROR_LINKED_DEPENDENCIES = (".venv", "venv", "node_modules")
_MIRROR_EXCLUDED_NAMES = frozenset(
    {
        ".hermes-claude-runtime",
        *_MIRROR_LINKED_DEPENDENCIES,
    }
)


def _copy_workspace_to_mirror(
    source: Path,
    destination: Path,
    *,
    boundary: WorkspaceTerminalBoundary,
) -> list[Path]:
    """Copy source state without creating write aliases back to the assignment."""

    if boundary.root != source:
        raise RuntimeError(
            "Workspace terminal boundary does not match the mirror source"
        )

    def _ignore(_directory: str, names: list[str]) -> set[str]:
        return {
            name
            for name in names
            if name in _MIRROR_EXCLUDED_NAMES or is_workspace_credential_path(name)
        }

    shutil.copytree(source, destination, symlinks=True, ignore=_ignore)
    dependency_roots: list[Path] = []
    for name in _MIRROR_LINKED_DEPENDENCIES:
        dependency = source / name
        if not dependency.exists() or not dependency.is_dir():
            continue
        (destination / name).symlink_to(dependency, target_is_directory=True)
        dependency_roots.append(dependency.resolve())
    return dependency_roots


def _remove_disposable_root(root: Path) -> None:
    def _retry(function: Callable[[str], Any], path: str, _error: Any) -> None:
        os.chmod(path, stat.S_IRWXU)
        function(path)

    shutil.rmtree(root, onerror=_retry)


def dispatch_read_only_workspace_terminal(
    arguments: Mapping[str, Any],
    *,
    workspace: str | Path,
    host_home: str | Path,
    exact_env: Mapping[str, str],
    dispatch: Callable[..., Any],
    task_id: str,
    boundary: WorkspaceTerminalBoundary | None = None,
    platform_name: str | None = None,
    scratch_parent: str | Path | None = None,
    extra_readable_roots: list[str | Path] | None = None,
) -> Any:
    """Dispatch one reviewer/verifier command without writable access to source."""

    source = Path(workspace).expanduser().resolve()
    command = str(arguments.get("command") or "").strip()
    normalized, use_mirror = _normalize_read_only_terminal_command(
        command,
        workspace=source,
        exact_env=exact_env,
    )
    if bool(arguments.get("background")):
        raise RuntimeError("Read-only worker terminal rejects background execution")
    if not source.is_dir():
        raise RuntimeError(f"Worker workspace does not exist: {source}")
    selected_git = _selected_git(str(exact_env.get("PATH", "")))
    if boundary is None:
        boundary = prepare_workspace_terminal_boundary(source, git=selected_git)
    elif boundary.root != source:
        raise RuntimeError(
            "Workspace terminal boundary does not match the worker workspace"
        )

    scratch_base = Path(
        scratch_parent
        or get_hermes_home() / "cache" / "claude-agent-sdk" / "read-only-runs"
    ).expanduser().resolve()
    if scratch_base == source or scratch_base.is_relative_to(source):
        raise RuntimeError("Read-only terminal scratch must be outside the workspace")
    scratch_base.mkdir(mode=0o700, parents=True, exist_ok=True)
    scratch_base.chmod(0o700)
    run_root = Path(tempfile.mkdtemp(prefix="run-", dir=scratch_base))
    try:
        dependency_roots: list[Path] = []
        execution_root = source
        if use_mirror:
            execution_root = run_root / "workspace"
            dependency_roots = _copy_workspace_to_mirror(
                source,
                execution_root,
                boundary=boundary,
            )
            source_git_metadata = _git_sandbox_metadata(source, selected_git)
            if (
                source_git_metadata is not None
                and not source_git_metadata.common_dir.is_relative_to(source)
            ):
                dependency_roots.append(source_git_metadata.common_dir)

        original_workdir = Path(str(arguments.get("workdir") or source)).expanduser()
        if not original_workdir.is_absolute():
            original_workdir = source / original_workdir
        original_workdir = original_workdir.resolve(strict=False)
        try:
            relative_workdir = original_workdir.relative_to(source)
        except ValueError as exc:
            raise RuntimeError(
                "Workspace terminal workdir is outside the worker workspace"
            ) from exc

        dispatch_arguments = dict(arguments)
        dispatch_arguments["command"] = normalized if use_mirror else command
        dispatch_arguments["workdir"] = str(execution_root / relative_workdir)
        transformed = build_workspace_terminal_args(
            dispatch_arguments,
            workspace=execution_root,
            host_home=host_home,
            exact_env=exact_env,
            boundary=boundary.retarget(execution_root) if use_mirror else boundary,
            platform_name=platform_name,
            read_only=not use_mirror,
            runtime_root=run_root / "runtime",
            additional_readable_roots=[*dependency_roots, *(extra_readable_roots or [])],
            git_metadata_enabled=not use_mirror,
        )
        return dispatch("terminal", transformed, task_id=task_id)
    finally:
        _remove_disposable_root(run_root)


__all__ = [
    "WorkspaceBoundaryProvisioningError",
    "WorkspaceTerminalBoundary",
    "build_workspace_seatbelt_profile",
    "build_workspace_terminal_args",
    "dispatch_read_only_workspace_terminal",
    "prepare_workspace_terminal_boundary",
]
