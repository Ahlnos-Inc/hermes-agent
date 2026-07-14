"""Regression tests for max_tokens propagation from config.yaml to AIAgent.

Covers #20741: `model.max_tokens` was silently dropped before reaching the
gateway-spawned agent, so providers without a hardcoded default (OpenRouter
free models, Ollama Cloud, custom OpenAI-compatible endpoints) truncated long
generations with `finish_reason="length"`.

The global/env request limit and the selected route's output cap are both
honored; when both exist, the lower limit wins.
"""

import importlib
import os
import sys
import textwrap

import pytest


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with a writable config.yaml and a clean module cache.

    These tests deliberately re-import ``hermes_cli`` / ``gateway`` so each
    config write is read fresh. To avoid leaking that purge into sibling test
    files in the same worker (which breaks their import-time mocks), we snapshot
    the affected modules and restore them on teardown.
    """
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("HERMES_MAX_TOKENS", raising=False)

    _saved = {
        k: v
        for k, v in sys.modules.items()
        if k.startswith(("hermes_cli", "gateway"))
    }

    def write_cfg(body: str) -> None:
        (hermes_home / "config.yaml").write_text(textwrap.dedent(body))

    def fresh_gateway():
        for mod in list(sys.modules.keys()):
            if mod.startswith(("hermes_cli", "gateway")):
                del sys.modules[mod]
        return importlib.import_module("gateway.run")

    try:
        yield write_cfg, fresh_gateway
    finally:
        # Drop anything we (re)imported, then restore the pre-test snapshot so
        # the next test file sees the module objects it was loaded with.
        for k in list(sys.modules.keys()):
            if k.startswith(("hermes_cli", "gateway")):
                del sys.modules[k]
        sys.modules.update(_saved)


def test_top_level_max_tokens_propagates(isolated_home):
    """model.max_tokens is read into the gateway runtime kwargs (#20741)."""
    write_cfg, fresh_gateway = isolated_home
    write_cfg(
        """
        model:
          default: glm-5.1
          provider: openrouter
          max_tokens: 16384
        """
    )
    grun = fresh_gateway()
    kw = grun._resolve_runtime_agent_kwargs()
    assert kw["max_tokens"] == 16384


def test_per_provider_max_output_tokens_fallback(isolated_home):
    """A custom provider's max_output_tokens fills in when no global is set."""
    write_cfg, fresh_gateway = isolated_home
    write_cfg(
        """
        model:
          default: glm-5.1
          provider: mylocal
        providers:
          mylocal:
            api: http://localhost:11434/v1
            api_key: sk-test
            default_model: glm-5.1
            max_output_tokens: 12000
        """
    )
    grun = fresh_gateway()
    kw = grun._resolve_runtime_agent_kwargs()
    assert kw["max_tokens"] == 12000


def test_per_provider_cap_clamps_higher_global_max_tokens(isolated_home):
    """A route-owned output safety cap cannot be raised by a global setting."""
    write_cfg, fresh_gateway = isolated_home
    write_cfg(
        """
        model:
          default: glm-5.1
          provider: mylocal
          max_tokens: 16384
        providers:
          mylocal:
            api: http://localhost:11434/v1
            api_key: sk-test
            default_model: glm-5.1
            max_output_tokens: 12000
        """
    )
    grun = fresh_gateway()
    kw = grun._resolve_runtime_agent_kwargs()
    assert kw["max_tokens"] == 12000


def test_legacy_provider_deadlines_reach_gateway_agent(isolated_home):
    """Legacy provider policy survives normalization and agent construction."""
    from run_agent import AIAgent

    write_cfg, fresh_gateway = isolated_home
    write_cfg(
        """
        model:
          default: qwen-local
          provider: legacy-local
        custom_providers:
          - name: legacy-local
            base_url: http://127.0.0.1:11434/v1
            api_key: no-key-required
            model: qwen-local
            request_timeout_seconds: 41
            stale_timeout_seconds: 42
            total_attempt_timeout_seconds: 43
            first_event_timeout_seconds: 44
        """
    )
    grun = fresh_gateway()
    runtime = grun._resolve_runtime_agent_kwargs()

    assert runtime["request_timeout_seconds"] == 41
    assert runtime["stale_timeout_seconds"] == 42
    assert runtime["total_attempt_timeout_seconds"] == 43
    assert runtime["first_event_timeout_seconds"] == 44

    model = runtime.pop("model")
    agent = AIAgent(
        model=model,
        **runtime,
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    assert agent._route_request_timeout_seconds == 41
    assert agent._route_stale_timeout_seconds == 42
    assert agent._route_total_attempt_timeout_seconds == 43
    assert agent._route_first_event_timeout_seconds == 44


def test_env_override_beats_everything(isolated_home, monkeypatch):
    """HERMES_MAX_TOKENS is the internal override mechanism (highest priority)."""
    write_cfg, fresh_gateway = isolated_home
    monkeypatch.setenv("HERMES_MAX_TOKENS", "2048")
    write_cfg(
        """
        model:
          default: glm-5.1
          provider: mylocal
          max_tokens: 16384
        providers:
          mylocal:
            api: http://localhost:11434/v1
            api_key: sk-test
            default_model: glm-5.1
            max_output_tokens: 12000
        """
    )
    grun = fresh_gateway()
    kw = grun._resolve_runtime_agent_kwargs()
    assert kw["max_tokens"] == 2048


def test_no_config_leaves_max_tokens_none(isolated_home):
    """No cap configured anywhere -> max_tokens is None (no spurious limit)."""
    write_cfg, fresh_gateway = isolated_home
    write_cfg(
        """
        model:
          default: glm-5.1
          provider: openrouter
        """
    )
    grun = fresh_gateway()
    kw = grun._resolve_runtime_agent_kwargs()
    assert kw["max_tokens"] is None


def test_external_moa_runtime_and_model_propagate_for_kanban_worker(
    isolated_home, monkeypatch
):
    write_cfg, fresh_gateway = isolated_home
    write_cfg(
        """
        model:
          provider: moa
          default: architect
        moa:
          default_preset: architect
          presets:
            architect:
              reference_models:
                - provider: openai-codex
                  model: gpt-5.6-sol
              aggregator:
                provider: anthropic
                model: claude-fable-5
                runtime: claude_agent_sdk
        """
    )
    monkeypatch.setenv("HERMES_KANBAN_TASK", "BUILD-425")

    runtime = fresh_gateway()._resolve_runtime_agent_kwargs()

    assert runtime["provider"] == "anthropic"
    assert runtime["model"] == "claude-fable-5"
    assert runtime["runtime"] == "claude_agent_sdk"
    assert runtime["api_key"] == ""
    assert runtime["base_url"] == ""
    assert runtime["moa_config"]["aggregator"]["runtime"] == (
        "claude_agent_sdk"
    )


def test_external_moa_config_stays_native_for_gateway_without_worker_task(
    isolated_home, monkeypatch
):
    write_cfg, fresh_gateway = isolated_home
    write_cfg(
        """
        model:
          provider: moa
          default: architect
        moa:
          default_preset: architect
          presets:
            architect:
              reference_models:
                - provider: openai-codex
                  model: gpt-5.6-sol
              aggregator:
                provider: anthropic
                model: claude-fable-5
                runtime: claude_agent_sdk
        """
    )
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)

    runtime = fresh_gateway()._resolve_runtime_agent_kwargs()

    assert runtime["provider"] == "moa"
    assert runtime["runtime"] == "hermes"
    assert runtime["base_url"] == "moa://local"
    assert runtime["moa_config"] is None


def test_gateway_fallback_uses_resolved_external_moa_actor_model(
    isolated_home, monkeypatch
):
    write_cfg, fresh_gateway = isolated_home
    write_cfg(
        """
        model:
          provider: openrouter
          default: openai/gpt-5
        fallback_providers:
          - provider: moa
            model: architect
        moa:
          default_preset: architect
          presets:
            architect:
              reference_models:
                - provider: openai-codex
                  model: gpt-5.6-sol
              aggregator:
                provider: anthropic
                model: claude-fable-5
                runtime: claude_agent_sdk
        """
    )
    monkeypatch.setenv("HERMES_KANBAN_TASK", "BUILD-425")

    runtime = fresh_gateway()._try_resolve_fallback_provider()

    assert runtime is not None
    assert runtime["provider"] == "anthropic"
    assert runtime["model"] == "claude-fable-5"
    assert runtime["runtime"] == "claude_agent_sdk"
    assert runtime["moa_config"]["aggregator"]["model"] == "claude-fable-5"


def test_lift_helper_accepts_alias_and_rejects_garbage(isolated_home):
    """_lift_max_output_tokens accepts both keys, ignores non-positive/non-int."""
    write_cfg, _ = isolated_home
    write_cfg("model:\n  provider: openrouter\n")
    for mod in list(sys.modules.keys()):
        if mod.startswith("hermes_cli"):
            del sys.modules[mod]
    rp = importlib.import_module("hermes_cli.runtime_provider")

    out: dict = {}
    rp._lift_max_output_tokens({"max_output_tokens": 8192}, out)
    assert out["max_output_tokens"] == 8192

    out = {}
    rp._lift_max_output_tokens({"max_tokens": 4096}, out)
    assert out["max_output_tokens"] == 4096

    for bad in ({"max_output_tokens": 0}, {"max_output_tokens": "x"}, {}):
        out = {}
        rp._lift_max_output_tokens(bad, out)
        assert "max_output_tokens" not in out
