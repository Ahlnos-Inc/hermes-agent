"""Pure profile-name normalization and validation shared by CLI ingress paths."""

from __future__ import annotations

import re


_PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_RESERVED_NAMES = frozenset({
    "hermes",
    "default",
    "test",
    "tmp",
    "root",
    "sudo",
})


def normalize_profile_name(name: str) -> str:
    """Return the canonical lowercase profile id or ``default`` alias."""
    if not isinstance(name, str):
        name = str(name)
    stripped = name.strip()
    if not stripped:
        raise ValueError("profile name cannot be empty")
    if stripped.casefold() == "default":
        return "default"
    return stripped.lower()


def validate_profile_name(name: str) -> None:
    """Raise ``ValueError`` when *name* is not a usable profile id."""
    if name == "default":
        return
    if not _PROFILE_ID_RE.fullmatch(name):
        raise ValueError(
            f"Invalid profile name {name!r}. Must match "
            f"[a-z0-9][a-z0-9_-]{{0,63}}"
        )
    if name in _RESERVED_NAMES:
        raise ValueError(
            f"Profile name {name!r} is reserved — it collides with either "
            f"the Hermes installation itself or a common system binary.  "
            f"Pick a different name."
        )


def normalize_and_validate_profile_name(name: str) -> str:
    """Normalize and validate one profile selector without reading state."""
    normalized = normalize_profile_name(name)
    validate_profile_name(normalized)
    return normalized
