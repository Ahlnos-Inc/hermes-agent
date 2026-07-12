"""Fork merge-survival guard: cron per-job fallback_providers resolution.

`cron/scheduler.py::_effective_fallback_chain` is the fork's unified resolution
of per-job `fallback_providers` (the fix for the recurring "two sites disagree
on the chain" bug). It is interleaved in a heavily upstream-churned file, so a
merge could silently drop it. These assertions go red if the None/[]/list/
malformed contract regresses. See [audit: routing/infra cluster, item 4].
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from cron.scheduler import _effective_fallback_chain
from hermes_cli.fallback_config import get_fallback_chain


def test_explicit_empty_means_no_fallback():
    # THE bug this feature fixed: explicit [] must NOT widen to the profile chain.
    assert _effective_fallback_chain({"fallback_providers": []}, {"fallback_providers": [{"provider": "p", "model": "m"}]}) == []


def test_none_inherits_profile_chain():
    cfg = {"fallback_providers": [{"provider": "anthropic", "model": "claude-opus-4-8"}]}
    assert _effective_fallback_chain({"fallback_providers": None}, cfg) == get_fallback_chain(cfg)
    # absent key behaves like None (create_job always writes the key, but hand-edited stores may not)
    assert _effective_fallback_chain({}, cfg) == get_fallback_chain(cfg)


def test_explicit_list_is_used_verbatim():
    fb = [{"provider": "deepseek", "model": "deepseek-v4-pro"}]
    assert _effective_fallback_chain({"fallback_providers": fb}, {}) == fb


def test_malformed_fails_closed_to_no_fallback():
    # A malformed hand-edited value must fail closed (no fallback), never widen to profile.
    assert _effective_fallback_chain({"fallback_providers": "anthropic"}, {"fallback_providers": [{"provider": "p", "model": "m"}]}) == []
