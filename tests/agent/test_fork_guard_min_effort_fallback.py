"""Merge-survival guard for the fork's ``min_effort`` fallback-chain gate.

``try_activate_fallback`` (agent/chat_completion_helpers.py) skips a fallback
chain entry that declares ``min_effort`` when the agent's *current* active
reasoning effort ranks below that floor — the recursion moves on to the next
chain entry instead of activating the gated one. Ranks come from
``_EFFORT_ORDER`` inside the function; effort is read via
``agent.routing_contract.active_reasoning_effort(agent)``.

If upstream's ``try_activate_fallback`` is merged over the fork's version and
the ``min_effort`` block (~lines 1264-1279) is dropped, this test fails: with
a "low" active effort, entry A (``min_effort: "high"``) would activate
directly instead of being skipped in favor of entry B.
"""

from unittest.mock import MagicMock, patch

from run_agent import AIAgent


def _make_agent(reasoning_effort: str):
    """Real AIAgent with __init__ bypassed, wired for a 2-entry fallback chain.

    Entry A gates on min_effort="high"; entry B is unconditional. Both use
    the whole-agent claude_agent_sdk runtime so activation is a pure
    attribute swap — no network client construction.
    """
    with patch.object(AIAgent, "__init__", lambda self, **kw: None):
        agent = AIAgent()

    agent.provider = "openai"
    agent.model = "gpt-5-primary"
    agent.runtime = "hermes"
    agent.base_url = ""
    agent.reasoning_config = {"effort": reasoning_effort, "enabled": True}
    agent._buffer_status = MagicMock()

    agent._fallback_chain = [
        {
            "provider": "anthropic",
            "model": "claude-opus-fallback",
            "runtime": "claude_agent_sdk",
            "min_effort": "high",
        },
        {
            "provider": "anthropic",
            "model": "claude-haiku-fallback",
            "runtime": "claude_agent_sdk",
        },
    ]
    agent._fallback_index = 0
    return agent


def test_min_effort_entry_skipped_when_active_effort_ranks_below():
    """low (rank 2) < high (rank 4) -> entry A skipped, entry B activates."""
    agent = _make_agent("low")

    activated = agent._try_activate_fallback()

    assert activated is True
    assert agent.model == "claude-haiku-fallback"
    assert agent.provider == "anthropic"
    # Both chain entries were consumed: A skipped by the guard, B activated.
    assert agent._fallback_index == 2


def test_min_effort_entry_not_skipped_when_active_effort_ranks_at_or_above():
    """xhigh (rank 5) >= high (rank 4) -> entry A activates directly."""
    agent = _make_agent("xhigh")

    activated = agent._try_activate_fallback()

    assert activated is True
    assert agent.model == "claude-opus-fallback"
    assert agent.provider == "anthropic"
    # Only the first chain entry was consumed: no skip occurred.
    assert agent._fallback_index == 1
