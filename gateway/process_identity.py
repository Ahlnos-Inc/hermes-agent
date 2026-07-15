"""Canonical identity and role classification for Hermes gateway processes.

Lifecycle code must never decide that a PID is a gateway from a label, a PID
file, or a substring in an argv dump.  This module is deliberately small and
has no Hermes configuration imports so the CLI and runtime status helpers use
the same classifier.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterable, Mapping


class GatewayRuntimeRole(str, Enum):
    """The role represented by one exact Hermes command line."""

    RUNTIME = "runtime"
    MANAGER = "manager"
    FOREIGN = "foreign"


_PROFILE_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_MANAGEMENT_SUBCOMMANDS = {
    "start",
    "stop",
    "restart",
    "status",
    "install",
    "uninstall",
    "setup",
}


def _normalized_token(token: str) -> str:
    return token.strip("\"'").replace("\\", "/").lower()


def _basename(token: str) -> str:
    return _normalized_token(token).rsplit("/", 1)[-1]


def _is_python_executable(token: str) -> bool:
    return bool(
        re.fullmatch(r"python(?:[0-9]+(?:\.[0-9]+)*)?w?(?:\.exe)?", _basename(token))
    )


def _is_gateway_executable(token: str) -> bool:
    return _basename(token) in {"hermes", "hermes.exe"}


def _is_dedicated_gateway_executable(token: str) -> bool:
    return _basename(token) in {"hermes-gateway", "hermes-gateway.exe"}


def _is_gateway_runtime_script(token: str) -> bool:
    normalized = _normalized_token(token)
    return normalized == "gateway/run.py" or normalized.endswith("/gateway/run.py")


def _is_hermes_home_assignment(token: str) -> bool:
    name, separator, _value = token.partition("=")
    return bool(separator) and name.lower() == "hermes_home"


def _is_gateway_cli_script(token: str) -> bool:
    normalized = _normalized_token(token)
    return normalized == "hermes_cli/main.py" or normalized.endswith(
        "/hermes_cli/main.py"
    )


def _merge_unquoted_windows_executable(tokens: list[str]) -> list[str]:
    """Reassemble the first unquoted spaced Windows executable path.

    WMIC's LIST output can contain ``C:\\Program Files\\Hermes\\Hermes.EXE``
    without quotes.  ``shlex`` necessarily splits that path, but the exact
    executable basename still gives us a safe, bounded reassembly point.
    """
    prefix_length = 1 if tokens and _is_hermes_home_assignment(tokens[0]) else 0
    first = tokens[prefix_length].strip("\"'") if len(tokens) > prefix_length else ""
    starts_with_path = bool(
        re.match(r"^[A-Za-z]:[\\/]", first)
        or first.startswith(("\\\\", "./", ".\\"))
    )
    for index, token in enumerate(tokens[prefix_length:], start=prefix_length):
        if not (
            _is_python_executable(token)
            or _is_gateway_executable(token)
            or _is_dedicated_gateway_executable(token)
        ):
            continue
        if index == prefix_length:
            return tokens
        candidate = " ".join(tokens[prefix_length : index + 1]).strip("\"'")
        if not starts_with_path:
            continue
        return [*tokens[:prefix_length], candidate, *tokens[index + 1 :]]
    return tokens


def _coerce_argv(value: str | Iterable[str]) -> tuple[str, ...]:
    if isinstance(value, str):
        try:
            tokens = shlex.split(value, posix=True)
        except ValueError:
            tokens = value.split()
        try:
            windows_tokens = shlex.split(value, posix=False)
        except ValueError:
            windows_tokens = []
        if windows_tokens:
            merged = _merge_unquoted_windows_executable(windows_tokens)
            if merged != windows_tokens or (
                merged
                and (
                    _is_python_executable(merged[0])
                    or _is_gateway_executable(merged[0])
                    or _is_dedicated_gateway_executable(merged[0])
                )
            ):
                tokens = merged
        return tuple(tokens)
    return tuple(str(part) for part in value)


def _normalized_tokens(argv: tuple[str, ...]) -> list[str]:
    return [_normalized_token(token) for token in argv]


@dataclass(frozen=True)
class _ProfileArgumentParse:
    remaining: tuple[str, ...]
    profile: str | None
    valid: bool


@dataclass(frozen=True)
class _GatewayCommandParse:
    subcommand: str | None
    profile: str | None
    leading_home: str | None
    valid: bool


def _absolute_home_value(value: str) -> bool:
    if not value:
        return False
    try:
        return Path(value).expanduser().is_absolute()
    except (OSError, RuntimeError):
        return False


def _split_leading_hermes_home(
    argv: tuple[str, ...],
) -> tuple[tuple[str, ...], str | None, bool]:
    """Strip exactly one canonical process-table HERMES_HOME assignment."""
    if not argv:
        return argv, None, True

    first = argv[0]
    if _is_hermes_home_assignment(first):
        value = first.split("=", 1)[1]
        if not _absolute_home_value(value):
            return argv, None, False
        if any(_is_hermes_home_assignment(token) for token in argv[1:]):
            return argv, None, False
        return argv[1:], value, True

    if any(_is_hermes_home_assignment(token) for token in argv):
        return argv, None, False
    return argv, None, True


def _entrypoint_arguments(
    argv: tuple[str, ...],
) -> tuple[str, tuple[str, ...]] | None:
    """Return the supported entrypoint kind and its post-entrypoint argv."""
    tokens = _normalized_tokens(argv)
    if not tokens:
        return None

    if _is_dedicated_gateway_executable(tokens[0]):
        return "direct", argv[1:]
    if _is_gateway_runtime_script(tokens[0]):
        return "direct", argv[1:]
    if _is_gateway_executable(tokens[0]):
        return "cli", argv[1:]
    if _is_gateway_cli_script(tokens[0]):
        return "cli", argv[1:]

    if not _is_python_executable(tokens[0]) or len(tokens) < 2:
        return None
    if _is_gateway_runtime_script(tokens[1]):
        return "direct", argv[2:]
    if _is_gateway_cli_script(tokens[1]):
        return "cli", argv[2:]
    if (
        len(tokens) >= 3
        and tokens[1] == "-m"
        and tokens[2] in {"hermes_cli.main", "hermes_cli/main.py"}
    ):
        return "cli", argv[3:]
    return None


def _profile_selector_token(token: str) -> bool:
    return (
        token in {"--profile", "-p"}
        or token.startswith("--profile=")
        or token.startswith("-p=")
    )


def _parse_profile_arguments(args: tuple[str, ...]) -> _ProfileArgumentParse:
    """Apply the CLI's broad profile scan while honoring passthrough bounds."""
    remaining: list[str] = []
    profile: str | None = None
    selector_seen = False
    passthrough = False
    index = 0

    while index < len(args):
        token = args[index]
        if passthrough:
            if _profile_selector_token(token):
                return _ProfileArgumentParse((), None, False)
            remaining.append(token)
            index += 1
            continue

        if token == "--":
            passthrough = True
            remaining.append(token)
            index += 1
            continue

        if token in {"--profile", "-p"}:
            if (
                selector_seen
                or index + 1 >= len(args)
                or not _PROFILE_RE.fullmatch(args[index + 1])
            ):
                return _ProfileArgumentParse((), None, False)
            profile = args[index + 1]
            selector_seen = True
            index += 2
            continue

        if token.startswith("--profile=") or token.startswith("-p="):
            candidate = token.split("=", 1)[1]
            if selector_seen or not _PROFILE_RE.fullmatch(candidate):
                return _ProfileArgumentParse((), None, False)
            profile = candidate
            selector_seen = True
            index += 1
            continue

        remaining.append(token)
        index += 1

    return _ProfileArgumentParse(tuple(remaining), profile, True)


def _parse_gateway_command(argv: tuple[str, ...]) -> _GatewayCommandParse:
    command_argv, leading_home, prefix_valid = _split_leading_hermes_home(argv)
    if not prefix_valid:
        return _GatewayCommandParse(None, None, None, False)

    entrypoint = _entrypoint_arguments(command_argv)
    if entrypoint is None:
        return _GatewayCommandParse(None, None, None, False)
    kind, args = entrypoint

    profile_parse = _parse_profile_arguments(args)
    if not profile_parse.valid:
        return _GatewayCommandParse(None, None, None, False)
    if kind == "direct":
        return _GatewayCommandParse("run", profile_parse.profile, leading_home, True)

    remaining = profile_parse.remaining
    if not remaining or _normalized_token(remaining[0]) != "gateway":
        return _GatewayCommandParse(None, None, leading_home, True)
    subcommand = "run" if len(remaining) == 1 else _normalized_token(remaining[1])
    return _GatewayCommandParse(subcommand, profile_parse.profile, leading_home, True)


def gateway_command_subcommand(argv: str | Iterable[str]) -> str | None:
    """Return the lifecycle subcommand represented by one exact argv grammar."""
    parsed = _parse_gateway_command(_coerce_argv(argv))
    return parsed.subcommand if parsed.valid else None


def _profile_arguments_are_valid(argv: tuple[str, ...]) -> bool:
    """Return whether profile selectors are valid and bounded in this argv."""
    return _parse_profile_arguments(argv).valid


def _profile_from_argv(argv: tuple[str, ...]) -> str | None:
    """Return the profile from a fully recognized gateway command, if any."""
    parsed = _parse_gateway_command(argv)
    return parsed.profile


def _home_from_identity(
    argv: tuple[str, ...],
    environment: Mapping[str, str] | None,
    default_home: Path | None,
    *,
    profile: str | None = None,
    explicit_argv_home: str | None = None,
) -> Path | None:
    raw_home: str | None = None
    environment_has_home = environment is not None and "HERMES_HOME" in environment
    if environment_has_home:
        raw_home = environment.get("HERMES_HOME")
        if not isinstance(raw_home, str) or not raw_home:
            return None
    elif explicit_argv_home is not None:
        raw_home = explicit_argv_home
        if not raw_home:
            return None
    elif default_home is not None:
        default = Path(default_home)
        raw_home = str(
            default / "profiles" / profile if profile and profile != "default" else default
        )
    if not raw_home:
        return None
    home = Path(raw_home).expanduser()
    if not home.is_absolute():
        return None
    try:
        return home.resolve()
    except OSError:
        return None


def classify_gateway_argv(
    argv: str | Iterable[str],
    *,
    environment: Mapping[str, str] | None = None,
    default_home: Path | None = None,
) -> tuple[GatewayRuntimeRole, str | None, Path | None]:
    """Classify one exact argv and its minimal environment identity.

    Only ``HERMES_HOME`` is consumed from ``environment``.  Callers that need
    destructive proof must treat a missing/invalid home as an untrusted
    identity rather than silently falling back to a caller profile.
    """
    exact_argv = _coerce_argv(argv)
    parsed = _parse_gateway_command(exact_argv)
    if not parsed.valid:
        role = GatewayRuntimeRole.FOREIGN
    elif parsed.subcommand == "run":
        role = GatewayRuntimeRole.RUNTIME
    elif parsed.subcommand in _MANAGEMENT_SUBCOMMANDS:
        role = GatewayRuntimeRole.MANAGER
    else:
        role = GatewayRuntimeRole.FOREIGN
    recognized_gateway = parsed.valid and parsed.subcommand is not None
    return role, parsed.profile if recognized_gateway else None, _home_from_identity(
        exact_argv,
        environment,
        default_home if recognized_gateway else None,
        profile=parsed.profile if recognized_gateway else None,
        explicit_argv_home=parsed.leading_home if recognized_gateway else None,
    )


@dataclass(frozen=True, init=False)
class GatewayProcessIdentity:
    """One birth- and argv-attested Hermes process.

    The first three constructor arguments retain compatibility with older
    callers: ``command_line`` may be a joined command string.  New captures
    pass ``argv`` and the classifier-derived identity fields explicitly.
    """

    pid: int
    start_time: int
    command_line: str = field(repr=False)
    argv: tuple[str, ...] = field(repr=False)
    runtime_role: GatewayRuntimeRole
    profile: str | None
    hermes_home: Path | None = field(repr=False)

    def __init__(
        self,
        pid: int,
        start_time: int,
        command_line_or_argv: str | Iterable[str],
        *,
        argv: Iterable[str] | None = None,
        runtime_role: GatewayRuntimeRole | str | None = None,
        profile: str | None = None,
        hermes_home: Path | str | None = None,
        environment: Mapping[str, str] | None = None,
        default_home: Path | None = None,
    ) -> None:
        exact_argv = _coerce_argv(argv if argv is not None else command_line_or_argv)
        command_line = (
            str(command_line_or_argv)
            if isinstance(command_line_or_argv, str) and argv is None
            else " ".join(exact_argv)
        )
        classified_role, classified_profile, classified_home = classify_gateway_argv(
            exact_argv, environment=environment, default_home=default_home
        )
        if runtime_role is None:
            runtime_role = classified_role
        else:
            runtime_role = GatewayRuntimeRole(runtime_role)
        if profile is None:
            profile = classified_profile
        if hermes_home is None:
            hermes_home = classified_home
        elif hermes_home is not None:
            candidate = Path(hermes_home).expanduser()
            hermes_home = candidate.resolve() if candidate.is_absolute() else None

        object.__setattr__(self, "pid", int(pid))
        object.__setattr__(self, "start_time", int(start_time))
        object.__setattr__(self, "command_line", command_line)
        object.__setattr__(self, "argv", exact_argv)
        object.__setattr__(self, "runtime_role", GatewayRuntimeRole(runtime_role))
        object.__setattr__(self, "profile", profile)
        object.__setattr__(self, "hermes_home", hermes_home)

    @property
    def process_birth(self) -> int:
        return self.start_time

    @property
    def exact_argv(self) -> tuple[str, ...]:
        return self.argv

    @property
    def profile_identity(self) -> str | None:
        return self.profile

    @property
    def resolved_hermes_home(self) -> Path | None:
        return self.hermes_home

    @property
    def is_runtime(self) -> bool:
        return self.runtime_role is GatewayRuntimeRole.RUNTIME

    def identity_key(self) -> tuple[object, ...]:
        return (
            self.pid,
            self.start_time,
            self.argv,
            self.runtime_role,
            self.profile,
            self.hermes_home,
        )


def process_identity_matches_target(
    identity: GatewayProcessIdentity,
    *,
    argv: Iterable[str],
    hermes_home: Path,
    profile: str | None,
    role: GatewayRuntimeRole = GatewayRuntimeRole.RUNTIME,
) -> bool:
    """Return whether a live identity is exactly the attested plist target."""
    try:
        expected_home = Path(hermes_home).resolve()
    except OSError:
        return False
    return (
        identity.runtime_role is role
        and identity.argv == tuple(str(part) for part in argv)
        and identity.profile == profile
        and identity.hermes_home == expected_home
    )
