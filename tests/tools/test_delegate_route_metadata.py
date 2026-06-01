from __future__ import annotations

import types


class TestDelegateRouteMetadata:
    def test_reasoning_effort_label_handles_disabled_and_missing_configs(self):
        from tools.delegate_tool import _reasoning_effort_label

        assert _reasoning_effort_label({"enabled": True, "effort": "xhigh"}) == "xhigh"
        assert _reasoning_effort_label({"enabled": False, "effort": "high"}) == "none"
        assert _reasoning_effort_label(None) == ""

    def test_progress_callback_relays_route_metadata(self):
        from tools.delegate_tool import _build_child_progress_callback

        calls = []

        def parent_cb(event_type, tool_name=None, preview=None, args=None, **kwargs):
            calls.append((event_type, tool_name, preview, args, kwargs))

        parent = types.SimpleNamespace(tool_progress_callback=parent_cb)
        callback = _build_child_progress_callback(
            task_index=0,
            goal="inspect repo",
            parent_agent=parent,
            task_count=1,
            subagent_id="sa-route",
            parent_id="sa-parent",
            depth=1,
            model="deepseek-v4-pro",
            provider="deepseek",
            reasoning_effort="low",
            role="leaf",
            execution_mode="delegate_task",
            route_reason="delegation provider override",
            toolsets=["terminal", "file"],
        )

        assert callback is not None
        callback("subagent.start", preview="inspect repo")

        assert calls == [
            (
                "subagent.start",
                None,
                "inspect repo",
                None,
                {
                    "task_index": 0,
                    "task_count": 1,
                    "goal": "inspect repo",
                    "subagent_id": "sa-route",
                    "parent_id": "sa-parent",
                    "depth": 1,
                    "model": "deepseek-v4-pro",
                    "provider": "deepseek",
                    "reasoning_effort": "low",
                    "role": "leaf",
                    "execution_mode": "delegate_task",
                    "route_reason": "delegation provider override",
                    "toolsets": ["terminal", "file"],
                    "tool_count": 0,
                },
            )
        ]

    def test_progress_callback_spinner_surfaces_route_notice_for_classic_cli(self):
        from tools.delegate_tool import _build_child_progress_callback

        class Spinner:
            def __init__(self):
                self.lines = []

            def print_above(self, line):
                self.lines.append(line)

        spinner = Spinner()
        parent = types.SimpleNamespace(_delegate_spinner=spinner)
        callback = _build_child_progress_callback(
            task_index=0,
            goal="inspect repo",
            parent_agent=parent,
            task_count=1,
            model="deepseek-v4-pro",
            provider="deepseek",
            reasoning_effort="low",
            role="leaf",
            execution_mode="delegate_task",
            route_reason="delegation provider override",
        )

        assert callback is not None
        callback("subagent.start", preview="inspect repo")

        assert spinner.lines == [
            " ├─ 🔀 inspect repo · delegated · leaf · deepseek/deepseek-v4-pro · effort low · reason delegation provider override"
        ]

    def test_active_subagent_snapshot_includes_route_metadata_without_agent_object(self):
        from tools import delegate_tool

        record = {
            "subagent_id": "sa-live",
            "parent_id": None,
            "depth": 0,
            "goal": "live child",
            "model": "deepseek-v4-pro",
            "provider": "deepseek",
            "reasoning_effort": "low",
            "role": "leaf",
            "execution_mode": "delegate_task",
            "route_reason": "delegation provider override",
            "status": "running",
            "tool_count": 0,
            "agent": object(),
        }

        try:
            delegate_tool._register_subagent(record)
            snapshot = delegate_tool.list_active_subagents()
        finally:
            delegate_tool._unregister_subagent("sa-live")

        assert snapshot == [
            {
                "subagent_id": "sa-live",
                "parent_id": None,
                "depth": 0,
                "goal": "live child",
                "model": "deepseek-v4-pro",
                "provider": "deepseek",
                "reasoning_effort": "low",
                "role": "leaf",
                "execution_mode": "delegate_task",
                "route_reason": "delegation provider override",
                "status": "running",
                "tool_count": 0,
            }
        ]


class TestChildReasoningMetadata:
    """Verify that child reasoning metadata is stashed immutably at build time
    and reported correctly across all result paths."""

    def test_helper_returns_all_keys(self):
        from tools.delegate_tool import _child_reasoning_metadata
        import types

        child = types.SimpleNamespace(
            reasoning_config={"enabled": True, "effort": "xhigh"},
            _delegate_requested_reasoning_effort="low",
            _delegate_effective_reasoning_effort="low",
            _delegate_reasoning_override_source="per-call",
        )
        meta = _child_reasoning_metadata(child)
        assert meta["reasoning_effort"] == "xhigh"  # live
        assert meta["requested_reasoning_effort"] == "low"
        assert meta["effective_reasoning_effort"] == "low"
        assert meta["reasoning_override_source"] == "per-call"

    def test_helper_falls_back_when_stash_missing(self):
        from tools.delegate_tool import _child_reasoning_metadata
        import types

        child = types.SimpleNamespace(
            reasoning_config={"enabled": True, "effort": "high"},
        )
        meta = _child_reasoning_metadata(child)
        assert meta["reasoning_effort"] == "high"
        assert meta["requested_reasoning_effort"] is None
        assert meta["effective_reasoning_effort"] == "high"  # falls back to live
        assert meta["reasoning_override_source"] == ""

    def test_helper_reports_none_for_disabled(self):
        from tools.delegate_tool import _child_reasoning_metadata
        import types

        child = types.SimpleNamespace(
            reasoning_config={"enabled": False},
            _delegate_requested_reasoning_effort=None,
            _delegate_effective_reasoning_effort="none",
            _delegate_reasoning_override_source="inherited",
        )
        meta = _child_reasoning_metadata(child)
        assert meta["reasoning_effort"] == "none"
        assert meta["requested_reasoning_effort"] is None
        assert meta["effective_reasoning_effort"] == "none"
        assert meta["reasoning_override_source"] == "inherited"

    def test_build_child_stashes_override_source_per_call(self):
        """Per-call reasoning_effort override is recorded as 'per-call'."""
        import threading
        from unittest.mock import MagicMock, patch
        from tools.delegate_tool import _build_child_agent

        parent = MagicMock()
        parent.base_url = "https://api.openai.com/v1"
        parent.api_key = "sk-***"
        parent.provider = "openai"
        parent.api_mode = "chat_completions"
        parent.model = "gpt-4o"
        parent.platform = "cli"
        parent.providers_allowed = None
        parent.providers_ignored = None
        parent.providers_order = None
        parent.provider_sort = None
        parent._session_db = None
        parent._delegate_depth = 0
        parent._active_children = []
        parent._active_children_lock = threading.Lock()
        parent._print_fn = None
        parent.tool_progress_callback = None
        parent.thinking_callback = None
        parent.reasoning_config = {"enabled": True, "effort": "high"}

        with patch("tools.delegate_tool._load_config", return_value={}), \
             patch("tools.delegate_tool._get_max_spawn_depth", return_value=2), \
             patch("tools.delegate_tool._get_orchestrator_enabled", return_value=True):
            child = _build_child_agent(
                task_index=0, goal="test", context=None,
                toolsets=["terminal"], model=None,
                max_iterations=50, task_count=1,
                parent_agent=parent,
                override_reasoning_effort="low",
            )
            assert child._delegate_requested_reasoning_effort == "low"
            assert child._delegate_effective_reasoning_effort == "low"
            assert child._delegate_reasoning_override_source == "per-call"

    def test_build_child_stashes_inherited_when_no_override(self):
        """No override → source is 'inherited' and effective == parent's."""
        import threading
        from unittest.mock import MagicMock, patch
        from tools.delegate_tool import _build_child_agent

        parent = MagicMock()
        parent.base_url = "https://api.openai.com/v1"
        parent.api_key = "sk-***"
        parent.provider = "openai"
        parent.api_mode = "chat_completions"
        parent.model = "gpt-4o"
        parent.platform = "cli"
        parent.providers_allowed = None
        parent.providers_ignored = None
        parent.providers_order = None
        parent.provider_sort = None
        parent._session_db = None
        parent._delegate_depth = 0
        parent._active_children = []
        parent._active_children_lock = threading.Lock()
        parent._print_fn = None
        parent.tool_progress_callback = None
        parent.thinking_callback = None
        parent.reasoning_config = {"enabled": True, "effort": "xhigh"}

        with patch("tools.delegate_tool._load_config", return_value={}), \
             patch("tools.delegate_tool._get_max_spawn_depth", return_value=2), \
             patch("tools.delegate_tool._get_orchestrator_enabled", return_value=True):
            child = _build_child_agent(
                task_index=0, goal="test", context=None,
                toolsets=["terminal"], model=None,
                max_iterations=50, task_count=1,
                parent_agent=parent,
            )
            assert child._delegate_requested_reasoning_effort is None
            assert child._delegate_effective_reasoning_effort == "xhigh"
            assert child._delegate_reasoning_override_source == "inherited"

    def test_single_route_telemetry_not_duplicated(self):
        """After build, there is exactly one _orchestration_route_telemetry
        setattr call (the first one with quota/gates)."""
        import threading
        from unittest.mock import MagicMock, patch
        from tools.delegate_tool import _build_child_agent

        parent = MagicMock()
        parent.base_url = "https://api.openai.com/v1"
        parent.api_key = "sk-***"
        parent.provider = "openai"
        parent.api_mode = "chat_completions"
        parent.model = "gpt-4o"
        parent.platform = "cli"
        parent.providers_allowed = None
        parent.providers_ignored = None
        parent.providers_order = None
        parent.provider_sort = None
        parent._session_db = None
        parent._delegate_depth = 0
        parent._active_children = []
        parent._active_children_lock = threading.Lock()
        parent._print_fn = None
        parent.tool_progress_callback = None
        parent.thinking_callback = None
        parent.reasoning_config = {"enabled": True, "effort": "medium"}

        with patch("tools.delegate_tool._load_config", return_value={}), \
             patch("tools.delegate_tool._get_max_spawn_depth", return_value=2), \
             patch("tools.delegate_tool._get_orchestrator_enabled", return_value=True):
            child = _build_child_agent(
                task_index=0, goal="test", context=None,
                toolsets=["terminal"], model=None,
                max_iterations=50, task_count=1,
                parent_agent=parent,
            )
            telemetry = child._orchestration_route_telemetry
            # Must have the 'quota' key (present in first, missing from second copy)
            assert "quota" in telemetry
            # gates must include "quota" gate
            assert "quota" in telemetry.get("gates", {})
