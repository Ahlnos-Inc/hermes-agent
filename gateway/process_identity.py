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


def _coerce_argv(value: str | Iterable[str]) -> tuple[str, ...]:
    if isinstance(value, str):
        try:
            return tuple(shlex.split(value, posix=True))
        except ValueError:
            return tuple(value.split())
    return tuple(str(part) for part in value)


def _normalized_tokens(argv: tuple[str, ...]) -> list[str]:
    return [token.strip("\"'").replace("\\", "/").lower() for token in argv]


def gateway_command_subcommand(argv: Iterable[str]) -> str | None:
    """Return the exact lifecycle subcommand represented by ``argv``."""
    tokens = _normalized_tokens(tuple(argv))
    if not tokens:
        return None

    for token in tokens:
        if token == "gateway/run.py" or token.endswith("/gateway/run.py"):
            return "run"
        if token.rsplit("/", 1)[-1] in {"hermes-gateway", "hermes-gateway.exe"}:
            return "run"

    joined = " ".join(tokens)
    has_gateway_entry = (
        "hermes_cli.main" in joined
        or "hermes_cli/main.py" in joined
        or any(
            token.rsplit("/", 1)[-1] in {"hermes", "hermes.exe"}
            for token in tokens
        )
    )
    if not has_gateway_entry:
        return None

    filtered: list[str] = []
    skip_next = False
    for token in tokens:
        if skip_next:
            skip_next = False
            continue
        if token in {"--profile", "-p"}:
            skip_next = True
            continue
        if token.startswith("--profile=") or token.startswith("-p="):
            continue
        filtered.append(token)

    for index, token in enumerate(filtered):
        if token != "gateway":
            continue
        return filtered[index + 1] if index + 1 < len(filtered) else "run"
    return None


def _profile_from_argv(argv: tuple[str, ...]) -> str | None:
    profile: str | None = None
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in {"--profile", "-p"}:
            if index + 1 >= len(argv):
                return None
            candidate = argv[index + 1]
            if not _PROFILE_RE.fullmatch(candidate):
                return None
            if profile is not None and profile != candidate:
                return None
            profile = candidate
            index += 2
            continue
        if token.startswith("--profile=") or token.startswith("-p="):
            candidate = token.split("=", 1)[1]
            if not _PROFILE_RE.fullmatch(candidate):
                return None
            if profile is not None and profile != candidate:
                return None
            profile = candidate
        index += 1
    return profile


def _home_from_identity(
    argv: tuple[str, ...],
    environment: Mapping[str, str] | None,
    default_home: Path | None,
) -> Path | None:
    explicit_argv_home: str | None = None
    for token in argv:
        if token.startswith("HERMES_HOME="):
            explicit_argv_home = token.split("=", 1)[1]
            break
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
        profile = _profile_from_argv(argv)
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
    argv: Iterable[str],
    *,
    environment: Mapping[str, str] | None = None,
    default_home: Path | None = None,
) -> tuple[GatewayRuntimeRole, str | None, Path | None]:
    """Classify one exact argv and its minimal environment identity.

    Only ``HERMES_HOME`` is consumed from ``environment``.  Callers that need
    destructive proof must treat a missing/invalid home as an untrusted
    identity rather than silently falling back to a caller profile.
    """
    exact_argv = tuple(str(part) for part in argv)
    subcommand = gateway_command_subcommand(exact_argv)
    if subcommand == "run":
        role = GatewayRuntimeRole.RUNTIME
    elif subcommand in _MANAGEMENT_SUBCOMMANDS:
        role = GatewayRuntimeRole.MANAGER
    else:
        role = GatewayRuntimeRole.FOREIGN
    return role, _profile_from_argv(exact_argv), _home_from_identity(
        exact_argv, environment, default_home
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
