"""Behavior contracts for phase-aware worker activity reporting."""

from __future__ import annotations


def test_activity_methods_report_distinct_worker_signals(monkeypatch):
    from run_agent import AIAgent
    from tools import kanban_tools

    agent = AIAgent.__new__(AIAgent)
    agent._last_activity_ts = 0.0
    agent._last_activity_desc = "initializing"
    agent._last_transport_activity_ts = None
    agent._last_semantic_progress_ts = None
    agent._last_durable_progress_ts = None

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_activity")
    recorded: list[str] = []
    monkeypatch.setattr(
        kanban_tools,
        "heartbeat_current_worker_from_env",
        lambda *, activity_kind: recorded.append(activity_kind),
    )

    agent._touch_activity("wrapper alive")
    agent._record_transport_activity("provider event")
    agent._record_semantic_progress("reasoning delta")
    agent._record_durable_progress("checkpoint persisted")

    assert recorded == ["process", "transport", "semantic", "durable"]
    assert agent._last_activity_desc == "checkpoint persisted"
    assert agent._last_transport_activity_ts is not None
    assert agent._last_semantic_progress_ts is not None
    assert agent._last_durable_progress_ts is not None


def test_reasoning_delta_is_semantic_progress():
    from run_agent import AIAgent

    agent = AIAgent.__new__(AIAgent)
    agent.reasoning_callback = None
    recorded: list[str] = []
    agent._record_semantic_progress = recorded.append

    agent._fire_reasoning_delta("thinking")

    assert recorded == ["provider reasoning delta"]


def test_visible_text_delta_is_semantic_progress():
    from run_agent import AIAgent

    agent = AIAgent.__new__(AIAgent)
    agent._stream_needs_break = False
    agent._stream_think_scrubber = None
    agent._stream_context_scrubber = None
    agent._current_streamed_assistant_text = ""
    agent.stream_delta_callback = None
    agent._stream_callback = None
    recorded: list[str] = []
    agent._record_semantic_progress = recorded.append

    agent._fire_stream_delta("answer")

    assert recorded == ["provider text delta"]


def test_tool_argument_generation_is_semantic_progress():
    from run_agent import AIAgent

    agent = AIAgent.__new__(AIAgent)
    agent.tool_gen_callback = None
    recorded: list[str] = []
    agent._record_semantic_progress = recorded.append

    agent._fire_tool_gen_started("kanban")

    assert recorded == ["provider tool generation"]
