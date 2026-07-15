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

from gateway.command_line import (
    ArgumentParseFailure,
    NonExitingArgumentParser,
    build_direct_gateway_parser,
    build_legacy_gateway_parser,
)


class GatewayRuntimeRole(str, Enum):
    """The role represented by one exact Hermes command line."""

    RUNTIME = "runtime"
    MANAGER = "manager"
    FOREIGN = "foreign"


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


def _is_supported_entrypoint_token(token: str) -> bool:
    return (
        _is_python_executable(token)
        or _is_gateway_executable(token)
        or _is_dedicated_gateway_executable(token)
        or _is_gateway_runtime_script(token)
        or _is_gateway_cli_script(token)
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
            executable_index = (
                1
                if merged and _is_hermes_home_assignment(merged[0])
                else 0
            )
            if merged != windows_tokens or (
                len(merged) > executable_index
                and _is_supported_entrypoint_token(merged[executable_index])
            ):
                tokens = merged
        return tuple(tokens)
    return tuple(str(part) for part in value)


def _normalized_tokens(argv: tuple[str, ...]) -> list[str]:
    return [_normalized_token(token) for token in argv]


@dataclass(frozen=True)
class _GatewayCommandParse:
    subcommand: str | None
    profile: str | None
    leading_home: str | None
    valid: bool
    entrypoint_kind: str | None = None


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
        return "legacy", argv[1:]
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
    if len(tokens) >= 3 and tokens[1] == "-m" and tokens[2] == "gateway.run":
        return "direct", argv[3:]
    return None


def _parse_direct_runner_tail(args: tuple[str, ...]) -> bool:
    parser = build_direct_gateway_parser(NonExitingArgumentParser)
    try:
        parser.parse_args(args)
    except ArgumentParseFailure:
        return False
    return True


def _parse_legacy_wrapper_tail(args: tuple[str, ...]) -> str | None:
    parser = build_legacy_gateway_parser(NonExitingArgumentParser)
    try:
        parsed = parser.parse_args(args)
    except ArgumentParseFailure:
        return None
    return _normalized_token(parsed.command)


def _build_cli_identity_parser():
    """Build the real top-level plus gateway parser without runtime imports."""
    from hermes_cli._parser import build_top_level_parser
    from hermes_cli.subcommands.gateway import build_gateway_parser

    parser, subparsers, _chat_parser = build_top_level_parser(
        parser_class=NonExitingArgumentParser
    )

    def inert_handler(_args):
        return None

    build_gateway_parser(
        subparsers,
        cmd_gateway=inert_handler,
        cmd_proxy=inert_handler,
        cmd_gateway_enroll=inert_handler,
    )
    return parser


def _parse_cli_command(args: tuple[str, ...]) -> tuple[str, str | None] | None:
    from hermes_cli.profile_argv import parse_profile_argv

    profile_parse = parse_profile_argv(args)
    parser = _build_cli_identity_parser()
    try:
        parsed = parser.parse_args(profile_parse.argv)
    except ArgumentParseFailure:
        return None

    if getattr(parsed, "command", None) != "gateway":
        return None
    if getattr(parsed, "version", False) or getattr(parsed, "oneshot", None) is not None:
        return None

    subcommand = getattr(parsed, "gateway_command", None) or "run"
    if subcommand != "run" and subcommand not in _MANAGEMENT_SUBCOMMANDS:
        return None
    return _normalized_token(subcommand), profile_parse.profile


def _parse_gateway_command(argv: tuple[str, ...]) -> _GatewayCommandParse:
    command_argv, leading_home, prefix_valid = _split_leading_hermes_home(argv)
    if not prefix_valid:
        return _GatewayCommandParse(None, None, None, False)

    entrypoint = _entrypoint_arguments(command_argv)
    if entrypoint is None:
        return _GatewayCommandParse(None, None, None, False)
    kind, args = entrypoint

    if kind == "direct":
        if not _parse_direct_runner_tail(args):
            return _GatewayCommandParse(None, None, None, False)
        return _GatewayCommandParse("run", None, leading_home, True, "direct")

    if kind == "legacy":
        subcommand = _parse_legacy_wrapper_tail(args)
        if subcommand is None:
            return _GatewayCommandParse(None, None, None, False)
        return _GatewayCommandParse(subcommand, None, leading_home, True, "legacy")

    cli_parse = _parse_cli_command(args)
    if cli_parse is None:
        return _GatewayCommandParse(None, None, None, False)
    subcommand, profile = cli_parse
    return _GatewayCommandParse(
        subcommand, profile, leading_home, True, "cli"
    )


def gateway_command_subcommand(argv: str | Iterable[str]) -> str | None:
    """Return the lifecycle subcommand represented by one exact argv grammar."""
    parsed = _parse_gateway_command(_coerce_argv(argv))
    if (
        parsed.valid
        and parsed.entrypoint_kind == "legacy"
        and parsed.subcommand == "restart"
    ):
        # The legacy wrapper's restart verb is a manager invocation.  The
        # runtime matcher also accepts the CLI's no-supervisor restart process,
        # so keep this manager-only shape out of that compatibility path.
        return None
    return parsed.subcommand if parsed.valid else None


def _resolve_home_value(value: object) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    home = Path(value).expanduser()
    if not home.is_absolute():
        return None
    try:
        return home.resolve()
    except (OSError, RuntimeError):
        return None


def _default_profile_home(
    default_home: Path | None, profile: str | None
) -> Path | None:
    if default_home is None:
        return None
    default = _resolve_home_value(str(default_home))
    if default is None:
        return None
    if profile and profile != "default":
        try:
            return (default / "profiles" / profile).resolve()
        except (OSError, RuntimeError):
            return None
    return default


def _profile_home_conflicts(
    home: Path, profile: str | None, default_home: Path | None
) -> bool:
    if not profile or profile == "default":
        return False
    if default_home is not None:
        expected = _default_profile_home(default_home, profile)
        return expected is not None and home != expected
    return not (
        home.parent.name.casefold() == "profiles"
        and home.name.casefold() == profile.casefold()
    )


def _resolve_identity_home(
    environment: Mapping[str, str] | None,
    default_home: Path | None,
    *,
    profile: str | None,
    explicit_argv_home: str | None,
) -> tuple[Path | None, bool]:
    """Resolve both explicit homes and report any identity conflict."""
    environment_has_home = environment is not None and "HERMES_HOME" in environment
    environment_home = (
        _resolve_home_value(environment.get("HERMES_HOME"))
        if environment_has_home and environment is not None
        else None
    )
    explicit_home = _resolve_home_value(explicit_argv_home)

    if environment_has_home and environment_home is None:
        return None, True
    if environment_home is not None and explicit_home is not None:
        if environment_home != explicit_home:
            return None, True
        resolved_home = environment_home
    elif environment_home is not None:
        resolved_home = environment_home
    elif explicit_home is not None:
        resolved_home = explicit_home
    else:
        resolved_home = _default_profile_home(default_home, profile)

    if resolved_home is not None and _profile_home_conflicts(
        resolved_home, profile, default_home
    ):
        return None, True
    return resolved_home, False


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
    if not recognized_gateway:
        return GatewayRuntimeRole.FOREIGN, None, None

    resolved_home, identity_conflict = _resolve_identity_home(
        environment,
        default_home,
        profile=parsed.profile,
        explicit_argv_home=parsed.leading_home,
    )
    if identity_conflict:
        return GatewayRuntimeRole.FOREIGN, None, None
    return role, parsed.profile, resolved_home


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
