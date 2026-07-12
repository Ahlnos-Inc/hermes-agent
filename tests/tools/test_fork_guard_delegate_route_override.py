"""
Merge-survival guard: delegate_task's per-call provider/model override
resolution actually reaches the constructed child agent.

Every existing delegate_task test either mocks `_run_single_child` /
`delegate_task` itself (never inspecting the built child), or mocks
`_resolve_delegation_credentials` outright (bypassing the override-merge
logic inside delegate_task's own body at ~L3263 `top_provider =
_clean_route_override(provider)` / `top_model = _clean_route_override(model)`
and ~L3347 `task_provider = _clean_route_override(task.get("provider")) or
top_provider`). None of them let that body run for real and then check what
provider/model the child was actually built with.

This test mocks only the true external boundary: the real credential lookup
(`hermes_cli.runtime_provider.resolve_runtime_provider`, which would
otherwise need a live DeepSeek API key / network) and the child's actual LLM
execution (`run_agent.AIAgent`). `_clean_route_override`,
`_merge_delegation_route_config`, `_resolve_delegation_credentials`, and
`_build_child_agent` all run for real.
"""

import json
import threading
import unittest
from unittest.mock import MagicMock, patch

from tools.delegate_tool import delegate_task


def _make_mock_parent(depth=0):
    """Minimal parent agent double — mirrors tests/tools/test_delegate.py's
    _make_mock_parent (kept local since only ONE new test file is allowed)."""
    parent = MagicMock()
    parent.base_url = "https://openrouter.ai/api/v1"
    parent.api_key = "***"
    parent.provider = "openrouter"
    parent.api_mode = "chat_completions"
    parent.model = "anthropic/claude-sonnet-4"
    parent.platform = "cli"
    parent.providers_allowed = None
    parent.providers_ignored = None
    parent.providers_order = None
    parent.provider_sort = None
    parent._session_db = None
    parent._delegate_depth = depth
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    parent._print_fn = None
    parent.tool_progress_callback = None
    parent.thinking_callback = None
    return parent


_DEEPSEEK_RUNTIME = {
    "provider": "deepseek",
    "model": "deepseek-v4-pro",
    "base_url": "https://api.deepseek.com/v1",
    "api_key": "sk-test-deepseek-key",
    "api_mode": "chat_completions",
}


def _mock_child_agent():
    child = MagicMock()
    child.run_conversation.return_value = {
        "final_response": "done",
        "completed": True,
        "api_calls": 1,
    }
    return child


class TestDelegateTaskRouteOverrideReachesChild(unittest.TestCase):
    """Guards the provider/model override wiring in delegate_task's body.

    If `top_provider`/`top_model` stopped being computed or stopped being
    threaded into `_build_child_agent` (single-task path), or if the
    per-task `_clean_route_override(task.get("provider")) or top_provider`
    merge (batch path) were dropped, the child would fall back to the
    parent's provider ("openrouter"/"anthropic/claude-sonnet-4") instead of
    the caller's override, and these assertions would fail.
    """

    @patch("hermes_cli.runtime_provider.resolve_runtime_provider")
    def test_single_task_provider_model_override_reaches_child_constructor(
        self, mock_resolve
    ):
        mock_resolve.return_value = dict(_DEEPSEEK_RUNTIME)
        parent = _make_mock_parent()

        with patch("run_agent.AIAgent") as MockAgent:
            MockAgent.return_value = _mock_child_agent()

            result = json.loads(
                delegate_task(
                    goal="x",
                    provider="deepseek",
                    model="deepseek-v4-pro",
                    parent_agent=parent,
                )
            )

        self.assertNotIn("error", result)
        self.assertEqual(result["results"][0]["status"], "completed")

        MockAgent.assert_called_once()
        _, kwargs = MockAgent.call_args
        self.assertEqual(kwargs["provider"], "deepseek")
        self.assertEqual(kwargs["model"], "deepseek-v4-pro")

        # Proves the override actually drove real credential resolution
        # (i.e. _clean_route_override + _resolve_delegation_credentials ran
        # on "deepseek", rather than the call silently falling back to the
        # parent's inherited "openrouter" route).
        self.assertEqual(mock_resolve.call_args.kwargs.get("requested"), "deepseek")

    @patch("hermes_cli.runtime_provider.resolve_runtime_provider")
    def test_batch_task_provider_model_override_reaches_child_constructor(
        self, mock_resolve
    ):
        mock_resolve.return_value = dict(_DEEPSEEK_RUNTIME)
        parent = _make_mock_parent()

        with patch("run_agent.AIAgent") as MockAgent:
            MockAgent.return_value = _mock_child_agent()

            result = json.loads(
                delegate_task(
                    tasks=[
                        {
                            "goal": "y",
                            "provider": "deepseek",
                            "model": "deepseek-v4-pro",
                        }
                    ],
                    parent_agent=parent,
                )
            )

        self.assertNotIn("error", result)
        self.assertEqual(result["results"][0]["status"], "completed")

        MockAgent.assert_called_once()
        _, kwargs = MockAgent.call_args
        self.assertEqual(kwargs["provider"], "deepseek")
        self.assertEqual(kwargs["model"], "deepseek-v4-pro")
        self.assertEqual(mock_resolve.call_args.kwargs.get("requested"), "deepseek")


if __name__ == "__main__":
    unittest.main()
