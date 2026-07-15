"""Pure pre-argparse handling for the CLI ``--profile`` selector."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from hermes_cli.profile_contract import normalize_and_validate_profile_name


_VALUE_FLAGS = frozenset({
    "-z",
    "--oneshot",
    "-m",
    "--model",
    "--provider",
    "-t",
    "--toolsets",
    "-r",
    "--resume",
    "-s",
    "--skills",
})
_OPTIONAL_VALUE_FLAGS = frozenset({"-c", "--continue"})


@dataclass(frozen=True)
class ProfileArgvParse:
    """Result of scanning one argv without reading config or changing env."""

    argv: tuple[str, ...]
    profile: str | None
    valid: bool


def _inside_mcp_add_args(argv: tuple[str, ...], index: int) -> bool:
    """Return whether *index* is inside ``mcp add --args`` passthrough."""
    try:
        mcp_index = argv.index("mcp", 0, index)
        argv.index("add", mcp_index + 1, index)
    except ValueError:
        return False
    return True


def _profile_selector(token: str) -> tuple[str, int] | None:
    if token in {"--profile", "-p"}:
        return "spaced", 2
    if token.startswith("--profile="):
        return token.split("=", 1)[1], 1
    # ``-p=value`` is not an execution spelling. Recognize it only so the
    # caller can fail closed rather than accidentally treating it as a prompt.
    if token.startswith("-p="):
        return "", 0
    return None


def parse_profile_argv(argv: Iterable[str]) -> ProfileArgvParse:
    """Strip the first valid explicit selector and preserve parser bounds.

    The scan mirrors the execution pre-parser: required-value flags consume
    their following token, ``--continue`` consumes a following non-option, and
    ``--`` plus MCP command-argv passthrough stop the scan. A second selector
    is deliberately left in the returned argv so the canonical argparse pass
    rejects it.
    """
    original = tuple(str(part) for part in argv)
    profile: str | None = None
    selector_start: int | None = None
    selector_size = 0
    duplicate = False
    index = 0

    while index < len(original):
        token = original[index]
        if token == "--" or (
            token == "--args" and _inside_mcp_add_args(original, index)
        ):
            break

        selector = _profile_selector(token)
        if selector is not None:
            kind, size = selector
            if kind == "spaced":
                if index + 1 >= len(original):
                    return ProfileArgvParse(original, None, False)
                candidate = original[index + 1]
            elif size == 1:
                candidate = kind
            else:
                return ProfileArgvParse(original, None, False)

            if selector_start is not None:
                duplicate = True
                index += size
                continue

            try:
                profile = normalize_and_validate_profile_name(candidate)
            except (TypeError, ValueError):
                return ProfileArgvParse(original, None, False)
            selector_start = index
            selector_size = size
            index += size
            continue

        if "=" not in token and token in _VALUE_FLAGS:
            index += 2 if index + 1 < len(original) else 1
            continue
        if (
            "=" not in token
            and token in _OPTIONAL_VALUE_FLAGS
            and index + 1 < len(original)
            and not original[index + 1].startswith("-")
        ):
            index += 2
            continue
        index += 1

    if selector_start is None:
        return ProfileArgvParse(original, None, True)
    stripped = original[:selector_start] + original[selector_start + selector_size :]
    return ProfileArgvParse(stripped, profile, not duplicate)
