"""Fork feature MARKER MANIFEST — the merge-survival tripwire.

A green test suite after an upstream merge is NOT proof the fork survived: two
fork fixes have already vanished in past merges with everything green
(fb1122777 refresh-cap, e525dac82 lane-effort). Behavioral guards catch what
they cover; this manifest is the wide net — a defining symbol of each
fork-unique feature. If a marker disappears, an upstream merge probably dropped
the feature silently: INVESTIGATE before shipping the merge. Do NOT delete a
marker to turn this green — that defeats the purpose. When you *deliberately*
remove or rename a fork feature, update its entry here in the same commit.

Run this before AND after every upstream sync.
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _read(rel: str) -> str | None:
    p = REPO / rel
    return p.read_text(encoding="utf-8") if p.exists() else None


# (feature, marker substring, file it must live in)
PRESENT = [
    ("credential-pool backoff (BUILD-262)", "consecutive_429_count", "agent/credential_pool.py"),
    ("credential-pool parking (BUILD-262/342)", "CredentialPoolExhausted", "agent/credential_pool.py"),
    ("quota-origin memory (BUILD-343)", "quota_origin_reason", "agent/turn_retry_state.py"),
    ("quota-exhaustion classifier (BUILD-343)", "is_quota_exhaustion", "agent/error_classifier.py"),
    ("min_effort fallback guard", "min_effort", "agent/chat_completion_helpers.py"),
    ("cron per-job fallback resolution", "_effective_fallback_chain", "cron/scheduler.py"),
    ("cron fallback/profile validators (fork module)", "_normalize_fallback_providers", "cron/ahlnos_jobs_ext.py"),
    ("delegate route-override clean", "_clean_route_override", "tools/delegate_tool.py"),
    ("delegate route-override plumbing", "override_provider", "tools/delegate_tool.py"),
    ("kanban quota-gated model routing", "HERMES_NATIVE_QUOTA_STATE_DIR", "tools/kanban_tools.py"),
    ("TTS telegram sendAudio opt-in", "voice_via_send_audio", "plugins/platforms/telegram/adapter.py"),
    ("native-quota statusbar", "get_native_quota_statusbar_for_model", "agent/native_quota.py"),
    ("kanban quota exit-code", "KANBAN_RATE_LIMIT_EXIT_CODE", "hermes_cli/kanban_db.py"),
    ("developer-role downgrade on failover (BUILD-345)", "_normalize_developer_role", "agent/transports/chat_completions.py"),
    ("runtime circuit breaker", "open_runtime_circuit", "agent/runtime_circuit.py"),
    ("kanban github-handoff guidance", "is not a human blocker", "agent/prompt_builder.py"),
    ("tui_gateway arms shell hooks (desktop delegation)", "register_from_config", "tui_gateway/entry.py"),
    ("dashboard arms shell hooks (in-memory desktop delegation)", "register_from_config", "hermes_cli/web_server.py"),
]

# Fork-only files — upstream has no version, so a merge can't overwrite them,
# but a botched conflict resolution can still DELETE them. Assert they exist.
FORK_ONLY_FILES = [
    "agent/runtime_circuit.py",
    "agent/claude_agent_runtime.py",
    "agent/runtime_target.py",
    "agent/native_quota.py",
    "cron/ahlnos_jobs_ext.py",
]

# Intentionally NOT restored: e525dac82's inline route-contract enforcement was
# accidentally dropped in merge b4e4ae9a9, but the orchestrator is pure-delegate
# by design, so inline enforcement is moot — we chose not to restore it (routing
# effort is now managed per-role-profile + a front-door hook). Asserted ABSENT so
# the manifest doesn't pretend it's covered; flags if it ever silently reappears.
REGRESSED = [
    ("lane reasoning_effort inline application (e525dac82) — intentionally not restored", "parse_reasoning_effort", "agent/conversation_loop.py"),
]


@pytest.mark.parametrize("feature,marker,rel", PRESENT, ids=[p[0] for p in PRESENT])
def test_fork_marker_present(feature, marker, rel):
    body = _read(rel)
    assert body is not None, f"{feature}: file {rel} is GONE — a merge likely deleted it."
    assert marker in body, (
        f"{feature}: marker {marker!r} missing from {rel} — an upstream merge "
        f"probably dropped this fork feature silently. Investigate; do not just "
        f"delete the marker."
    )


@pytest.mark.parametrize("rel", FORK_ONLY_FILES)
def test_fork_only_file_present(rel):
    assert (REPO / rel).exists(), f"fork-only file {rel} is GONE — a merge-conflict resolution deleted it."


@pytest.mark.parametrize("feature,marker,rel", REGRESSED, ids=[r[0] for r in REGRESSED])
def test_known_regressed_still_absent(feature, marker, rel):
    body = _read(rel) or ""
    assert marker not in body, (
        f"{feature}: marker {marker!r} REAPPEARED in {rel} — feature restored? "
        f"MOVE this entry from REGRESSED to PRESENT."
    )
