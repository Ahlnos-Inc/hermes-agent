from unittest.mock import patch

import pytest


@pytest.mark.parametrize(
    ("configured_limit", "expected_limit"),
    [
        (2048, 2048),
        (8192, 4096),
        (None, 4096),
    ],
)
def test_oneshot_applies_lower_of_global_and_primary_route_caps(
    configured_limit, expected_limit
):
    from hermes_cli import oneshot

    captured = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.suppress_status_output = False
            self.stream_delta_callback = object()
            self.tool_gen_callback = object()

        def run_conversation(self, _prompt):
            return {"final_response": "done", "completed": True}

    runtime = {
        "api_key": "test-key",
        "base_url": "https://example.invalid/v1",
        "provider": "omlx-local",
        "api_mode": "chat_completions",
        "runtime": "hermes",
        "model": "qwen3.5-35b-a3b-4bit",
        "max_output_tokens": 4096,
    }
    model_config = {}
    if configured_limit is not None:
        model_config["max_tokens"] = configured_limit
    with (
        patch(
            "hermes_cli.config.load_config",
            return_value={"model": model_config},
        ),
        patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value=runtime),
        patch("hermes_cli.tools_config._get_platform_tools", return_value=set()),
        patch("hermes_cli.oneshot._create_session_db_for_oneshot", return_value=None),
        patch("hermes_cli.oneshot.get_fallback_chain", return_value=[]),
        patch("run_agent.AIAgent", FakeAgent),
    ):
        response, result = oneshot._run_agent(
            "hello",
            model="qwen3.5-35b-a3b-4bit",
            provider="omlx-local",
            use_config_toolsets=False,
        )

    assert response == "done"
    assert result["completed"] is True
    assert captured["max_tokens"] == expected_limit
