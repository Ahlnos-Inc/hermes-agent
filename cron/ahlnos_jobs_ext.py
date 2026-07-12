"""Ahlnos fork-owned cron helpers, isolated from upstream churn.

These validation helpers are Ahlnos-local additions (per-job ``fallback_providers``
and ``profile`` support). They live in this fork-only module — which upstream
NousResearch never touches — so upstream merges cannot silently drop them the way
inline additions to ``cron/jobs.py`` repeatedly have ("restore profile/
fallback_providers params lost in upstream merge"). ``cron/jobs.py`` re-imports
them, so ``from cron.jobs import _normalize_fallback_providers`` keeps working.

Only PURE helpers (no ``cron.jobs`` module-state coupling) belong here. Path
resolution (``_cron_dir``/``jobs_file``/``output_dir``) is deliberately NOT moved:
it is coupled to jobs.py module constants and its call-sites live inside upstream
functions, so extracting it would not reduce conflicts. That feature is instead
protected by the loud tests in tests/cron/test_cron_profile_isolation.py.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def _normalize_fallback_providers(
    fb: Optional[List[Dict[str, Any]]],
) -> Optional[List[Dict[str, Any]]]:
    """Normalize a fallback_providers list for storage.

    Returns None when ``fb`` is None (inherit profile default).
    Returns a list of dicts otherwise — including explicit empty ``[]``
    which means "no fallback".

    Malformed input is rejected with ValueError at create/update time, the
    same way invalid ``workdir`` / ``profile`` / ``schedule`` values are.
    Stored ``None`` widens behaviour to "inherit the profile chain", so a
    bad value must never silently normalize to it.
    """
    if fb is None:
        return None
    if not isinstance(fb, list):
        raise ValueError(
            "fallback_providers must be a list of provider dicts "
            f"(got {type(fb).__name__}). Pass [] for no fallback or omit "
            "to inherit the profile chain."
        )
    cleaned = [{str(k): v for k, v in e.items()} for e in fb if isinstance(e, dict)]
    if fb and not cleaned:
        raise ValueError(
            "fallback_providers contains no valid entries — each entry must "
            "be a dict with at least 'provider' and 'model'. Pass [] for no "
            "fallback or omit to inherit the profile chain."
        )
    return cleaned


def _normalize_profile(profile: Optional[str]) -> Optional[str]:
    """Normalize and validate an optional cron job profile name.

    Empty / None disables per-job profile selection. Otherwise the profile name
    is canonicalized with the same rules as ``hermes -p`` and must refer to an
    existing profile at create/update time. ``default`` is the built-in root
    profile and is always valid.
    """
    if profile is None:
        return None
    raw = str(profile).strip()
    if not raw:
        return None

    from hermes_cli.profiles import normalize_profile_name, resolve_profile_env

    normalized = normalize_profile_name(raw)
    # resolve_profile_env validates the canonical name and checks that named
    # profiles exist. Store only the stable profile id, not the filesystem path,
    # so profile directories can move with the Hermes root.
    resolve_profile_env(normalized)
    return normalized
