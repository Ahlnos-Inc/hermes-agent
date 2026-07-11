"""Shared path policy for read-only Claude workspace capabilities."""

from __future__ import annotations

from pathlib import PurePath
from typing import Any


_CREDENTIAL_BASENAMES = frozenset(
    {
        ".envrc",
        ".git-credentials",
        ".netrc",
        ".npmrc",
        ".pypirc",
    }
)


def is_workspace_credential_path(path: Any) -> bool:
    """Return whether a path basename is credential-bearing, case-insensitively."""

    name = PurePath(str(path or "")).name.casefold()
    return name == ".env" or name.startswith(".env.") or name in _CREDENTIAL_BASENAMES


__all__ = ["is_workspace_credential_path"]
