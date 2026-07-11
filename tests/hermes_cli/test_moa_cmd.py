from types import SimpleNamespace

from hermes_cli import moa_cmd


def test_pick_aggregator_preserves_claude_max_runtime_by_default(monkeypatch):
    current = {
        "provider": "anthropic",
        "model": "claude-fable-5",
        "runtime": "claude_agent_sdk",
    }
    monkeypatch.setattr(
        moa_cmd,
        "_pick_slot",
        lambda _current: {"provider": "anthropic", "model": "claude-fable-5"},
    )
    monkeypatch.setattr(
        moa_cmd,
        "_prompt_choice",
        lambda _title, _rows, default=0: default,
    )

    selected = moa_cmd._pick_aggregator_slot(current)

    assert selected == current


def test_pick_aggregator_can_explicitly_switch_back_to_native(monkeypatch):
    current = {
        "provider": "anthropic",
        "model": "claude-fable-5",
        "runtime": "claude_agent_sdk",
    }
    monkeypatch.setattr(
        moa_cmd,
        "_pick_slot",
        lambda _current: {"provider": "anthropic", "model": "claude-fable-5"},
    )
    monkeypatch.setattr(moa_cmd, "_prompt_choice", lambda *_args, **_kwargs: 0)

    selected = moa_cmd._pick_aggregator_slot(current)

    assert selected == {"provider": "anthropic", "model": "claude-fable-5"}


def test_pick_aggregator_drops_claude_runtime_for_non_anthropic_model(monkeypatch):
    monkeypatch.setattr(
        moa_cmd,
        "_pick_slot",
        lambda _current: {"provider": "openrouter", "model": "openai/gpt-5"},
    )
    monkeypatch.setattr(
        moa_cmd,
        "_prompt_choice",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("non-Anthropic aggregators must not offer Claude runtime")
        ),
    )

    selected = moa_cmd._pick_aggregator_slot(
        {
            "provider": "anthropic",
            "model": "claude-fable-5",
            "runtime": "claude_agent_sdk",
        }
    )

    assert selected == {"provider": "openrouter", "model": "openai/gpt-5"}


def test_configure_round_trip_keeps_external_aggregator_runtime(monkeypatch):
    config = {
        "moa": {
            "default_preset": "architect",
            "presets": {
                "architect": {
                    "reference_models": [
                        {"provider": "openai-codex", "model": "gpt-5.6-sol"}
                    ],
                    "aggregator": {
                        "provider": "anthropic",
                        "model": "claude-fable-5",
                        "runtime": "claude_agent_sdk",
                    },
                }
            },
        }
    }
    saved = {}
    monkeypatch.setattr(moa_cmd, "load_config", lambda: config)
    monkeypatch.setattr(moa_cmd, "save_config", lambda value: saved.update(value))
    monkeypatch.setattr(
        moa_cmd,
        "_pick_slot",
        lambda current: {
            "provider": current["provider"],
            "model": current["model"],
        },
    )
    monkeypatch.setattr(
        moa_cmd,
        "_prompt_choice",
        lambda _title, _rows, default=0: default,
    )

    moa_cmd.cmd_moa(
        SimpleNamespace(moa_command="configure", name="architect")
    )

    assert saved["moa"]["presets"]["architect"]["aggregator"] == {
        "provider": "anthropic",
        "model": "claude-fable-5",
        "runtime": "claude_agent_sdk",
    }
