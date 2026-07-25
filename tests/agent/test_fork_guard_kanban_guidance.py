"""Fork merge-survival guard: kanban GitHub-handoff instruction.

The fork's "a push/open-PR failure is not a human blocker — hand off to the
releaser lane" instruction lives inside the large `KANBAN_GUIDANCE` prompt
string. (The lane is `releaser`; `publisher` survives only as a compatibility
alias, and the handoff verb became `kanban_request_publication` — a parked
card with remote-ref readback — rather than `kanban_complete`.) That kind of shared prompt blob is exactly where a 3-way merge silently
drops a clause while auto-resolving. The existing coverage only checks adjacent
generic phrases; this pins the actual semantic instruction. See [audit: kanban
cluster, feature 5].
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from agent.prompt_builder import KANBAN_GUIDANCE


def test_github_auth_handoff_clause_present():
    # The core semantic instruction: GitHub auth is NOT a human blocker...
    assert "is not a human blocker" in KANBAN_GUIDANCE
    # ...and the resolution is a handoff to the releaser lane, not kanban_block.
    idx = KANBAN_GUIDANCE.index("is not a human blocker")
    tail = KANBAN_GUIDANCE[idx: idx + 220]
    assert "kanban_request_publication" in tail, (
        "handoff clause must route to kanban_request_publication, not block"
    )


def test_handoff_requires_verifiable_artifacts():
    # The instruction must still demand branch/SHA/verification so the releaser can push.
    assert "commit " in KANBAN_GUIDANCE and "SHA" in KANBAN_GUIDANCE
    assert "releaser" in KANBAN_GUIDANCE
