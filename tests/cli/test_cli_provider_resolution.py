import importlib
import sys
import types
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from hermes_cli.auth import AuthError
from hermes_cli import main as hermes_main


# ---------------------------------------------------------------------------
# Module isolation: _import_cli() wipes tools.* / cli / run_agent from
# sys.modules so it can re-import cli fresh.  Without cleanup the wiped
# modules leak into subsequent tests, breaking
# mock patches that target "tools.file_tools._get_file_ops" etc.
# ---------------------------------------------------------------------------

def _reset_modules(prefixes: tuple[str, ...]):
    for name in list(sys.modules):
        if any(name == p or name.startswith(p + ".") for p in prefixes):
            sys.modules.pop(name, None)


@pytest.fixture(autouse=True)
def _restore_cli_and_tool_modules():
    """Save and restore tools/cli/run_agent modules around every test."""
    prefixes = ("tools", "cli", "run_agent")
    original_modules = {
        name: module
        for name, module in sys.modules.items()
        if any(name == p or name.startswith(p + ".") for p in prefixes)
    }
    try:
        yield
    finally:
        _reset_modules(prefixes)
        sys.modules.update(original_modules)


def _install_prompt_toolkit_stubs():
    class _Dummy:
        def __init__(self, *args, **kwargs):
            pass

    class _Condition:
        def __init__(self, func):
            self.func = func

        def __bool__(self):
            return bool(self.func())

    class _ANSI(str):
        pass

    root = types.ModuleType("prompt_toolkit")
    history = types.ModuleType("prompt_toolkit.history")
    styles = types.ModuleType("prompt_toolkit.styles")
    patch_stdout = types.ModuleType("prompt_toolkit.patch_stdout")
    application = types.ModuleType("prompt_toolkit.application")
    layout = types.ModuleType("prompt_toolkit.layout")
    processors = types.ModuleType("prompt_toolkit.layout.processors")
    filters = types.ModuleType("prompt_toolkit.filters")
    dimension = types.ModuleType("prompt_toolkit.layout.dimension")
    menus = types.ModuleType("prompt_toolkit.layout.menus")
    widgets = types.ModuleType("prompt_toolkit.widgets")
    key_binding = types.ModuleType("prompt_toolkit.key_binding")
    completion = types.ModuleType("prompt_toolkit.completion")
    formatted_text = types.ModuleType("prompt_toolkit.formatted_text")

    history.FileHistory = _Dummy
    styles.Style = _Dummy
    patch_stdout.patch_stdout = lambda *args, **kwargs: nullcontext()
    application.Application = _Dummy
    layout.Layout = _Dummy
    layout.HSplit = _Dummy
    layout.Window = _Dummy
    layout.FormattedTextControl = _Dummy
    layout.ConditionalContainer = _Dummy
    processors.Processor = _Dummy
    processors.Transformation = _Dummy
    processors.PasswordProcessor = _Dummy
    processors.ConditionalProcessor = _Dummy
    filters.Condition = _Condition
    dimension.Dimension = _Dummy
    menus.CompletionsMenu = _Dummy
    widgets.TextArea = _Dummy
    key_binding.KeyBindings = _Dummy
    completion.Completer = _Dummy
    completion.Completion = _Dummy
    formatted_text.ANSI = _ANSI
    root.print_formatted_text = lambda *args, **kwargs: None

    sys.modules.setdefault("prompt_toolkit", root)
    sys.modules.setdefault("prompt_toolkit.history", history)
    sys.modules.setdefault("prompt_toolkit.styles", styles)
    sys.modules.setdefault("prompt_toolkit.patch_stdout", patch_stdout)
    sys.modules.setdefault("prompt_toolkit.application", application)
    sys.modules.setdefault("prompt_toolkit.layout", layout)
    sys.modules.setdefault("prompt_toolkit.layout.processors", processors)
    sys.modules.setdefault("prompt_toolkit.filters", filters)
    sys.modules.setdefault("prompt_toolkit.layout.dimension", dimension)
    sys.modules.setdefault("prompt_toolkit.layout.menus", menus)
    sys.modules.setdefault("prompt_toolkit.widgets", widgets)
    sys.modules.setdefault("prompt_toolkit.key_binding", key_binding)
    sys.modules.setdefault("prompt_toolkit.completion", completion)
    sys.modules.setdefault("prompt_toolkit.formatted_text", formatted_text)


def _import_cli():
    for name in list(sys.modules):
        if name == "cli" or name == "run_agent" or name == "tools" or name.startswith("tools."):
            sys.modules.pop(name, None)

    if "firecrawl" not in sys.modules:
        sys.modules["firecrawl"] = types.SimpleNamespace(Firecrawl=object)

    try:
        importlib.import_module("prompt_toolkit")
    except ModuleNotFoundError:
        _install_prompt_toolkit_stubs()
    return importlib.import_module("cli")


def test_hermes_cli_init_does_not_eagerly_resolve_runtime_provider(monkeypatch):
    cli = _import_cli()
    calls = {"count": 0}

    def _unexpected_runtime_resolve(**kwargs):
        calls["count"] += 1
        raise AssertionError("resolve_runtime_provider should not be called in HermesCLI.__init__")

    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", _unexpected_runtime_resolve)
    monkeypatch.setattr("hermes_cli.runtime_provider.format_runtime_provider_error", lambda exc: str(exc))

    shell = cli.HermesCLI(model="gpt-5", compact=True, max_turns=1)

    assert shell is not None
    assert calls["count"] == 0


def test_runtime_resolution_failure_is_not_sticky(monkeypatch):
    cli = _import_cli()
    calls = {"count": 0}

    def _runtime_resolve(**kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise AuthError("synthetic failure", category="availability")
        return {
            "provider": "openrouter",
            "api_mode": "chat_completions",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "test-key",
            "source": "env/config",
        }

    class _DummyAgent:
        def __init__(self, *args, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", _runtime_resolve)
    monkeypatch.setattr("hermes_cli.runtime_provider.format_runtime_provider_error", lambda exc: str(exc))
    monkeypatch.setattr(cli, "AIAgent", _DummyAgent)

    shell = cli.HermesCLI(model="gpt-5", compact=True, max_turns=1)
    shell.requested_provider = "openrouter"

    assert shell._init_agent() is False
    assert shell._init_agent() is True
    assert calls["count"] == 2
    assert shell.agent is not None


def test_typed_temporary_auth_failure_selects_atomic_fallback(monkeypatch):
    cli = _import_cli()
    calls = []

    def _runtime_resolve(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise AuthError("synthetic primary failure", category="availability")
        return {
            "provider": "openrouter",
            "api_mode": "chat_completions",
            "base_url": "https://fallback.example.test/v1",
            "api_key": "fallback-key",
            "source": "fallback",
        }

    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", _runtime_resolve)
    shell = cli.HermesCLI(model="gpt-5", compact=True, max_turns=1)
    shell.requested_provider = "anthropic"
    shell._explicit_api_key = "primary-secret"
    shell._explicit_base_url = "https://primary.example.test/v1"
    shell._fallback_model = [{"provider": "openrouter", "model": "openai/gpt-5", "base_url": "https://fallback.example.test/v1"}]

    assert shell._ensure_runtime_credentials() is True
    assert shell.requested_provider == "openrouter"
    assert shell.model == "openai/gpt-5"
    assert calls[1]["route_config"]["provider"] == "openrouter"
    assert "explicit_api_key" not in calls[1] or calls[1]["explicit_api_key"] != "primary-secret"
    assert calls[1]["explicit_base_url"] == "https://fallback.example.test/v1"


def test_active_fallback_reresolves_its_own_route_and_clears_on_route_change(monkeypatch):
    cli = _import_cli()
    calls = []

    def _runtime_resolve(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise AuthError("synthetic primary failure", category="availability")
        return {
            "provider": kwargs["requested"],
            "api_mode": "chat_completions",
            "base_url": kwargs.get("explicit_base_url") or "https://primary.example.test/v1",
            "api_key": "test-key",
            "source": "test",
        }

    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", _runtime_resolve)
    shell = cli.HermesCLI(model="gpt-5", compact=True, max_turns=1)
    shell.requested_provider = "anthropic"
    shell._explicit_api_key = "primary-secret"
    shell._explicit_base_url = "https://primary.example.test/v1"
    shell._fallback_model = [{"provider": "openrouter", "model": "openai/gpt-5", "base_url": "https://fallback.example.test/v1"}]

    assert shell._ensure_runtime_credentials() is True
    assert shell._ensure_runtime_credentials() is True
    assert calls[2]["requested"] == "openrouter"
    assert calls[2].get("explicit_api_key") != "primary-secret"
    assert calls[2]["explicit_base_url"] == "https://fallback.example.test/v1"

    # A route change under the pin drops it — and the primary's explicit
    # credentials still must not reach the (still non-primary) provider.
    shell.model = "manual-route-change"
    assert shell._ensure_runtime_credentials() is True
    assert calls[3]["requested"] == "openrouter"
    assert calls[3].get("explicit_api_key") != "primary-secret"
    assert calls[3].get("explicit_base_url") != "https://primary.example.test/v1"
    assert shell._active_fallback_entry is None


def test_model_provider_mismatch_does_not_start_cli_fallback(monkeypatch):
    cli = _import_cli()
    from agent.runtime_target import RuntimeTargetError

    calls = []

    def _runtime_resolve(**kwargs):
        calls.append(kwargs)
        raise RuntimeTargetError("model/provider mismatch", code="model_provider_mismatch")

    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", _runtime_resolve)
    shell = cli.HermesCLI(model="gpt-5", compact=True, max_turns=1)
    shell._fallback_model = [{"provider": "openrouter", "model": "openai/gpt-5"}]

    assert shell._ensure_runtime_credentials() is False
    assert len(calls) == 1


def test_runtime_resolution_rebuilds_agent_on_routing_change(monkeypatch):
    cli = _import_cli()

    def _runtime_resolve(**kwargs):
        return {
            "provider": "openai-codex",
            "api_mode": "codex_responses",
            "base_url": "https://same-endpoint.example/v1",
            "api_key": "same-key",
            "source": "env/config",
        }

    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", _runtime_resolve)
    monkeypatch.setattr("hermes_cli.runtime_provider.format_runtime_provider_error", lambda exc: str(exc))

    shell = cli.HermesCLI(model="gpt-5", compact=True, max_turns=1)
    shell.provider = "openrouter"
    shell.api_mode = "chat_completions"
    shell.base_url = "https://same-endpoint.example/v1"
    shell.api_key = "same-key"
    shell.agent = object()

    assert shell._ensure_runtime_credentials() is True
    assert shell.agent is None
    assert shell.provider == "openai-codex"
    assert shell.api_mode == "codex_responses"


def test_cli_first_turn_without_kanban_task_keeps_native_moa(monkeypatch):
    cli = _import_cli()
    from hermes_cli import runtime_provider as rp

    config = {
        "model": {"provider": "moa", "default": "architect"},
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
        },
    }
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(rp, "_get_model_config", lambda: config["model"])
    monkeypatch.setattr(rp, "load_config", lambda: config)
    shell = cli.HermesCLI(model="architect", compact=True, max_turns=1)
    shell.requested_provider = "moa"
    shell.provider = "moa"

    assert shell._ensure_runtime_credentials() is True
    assert shell.model == "architect"
    assert shell.provider == "moa"
    assert shell.agent_runtime == "hermes"
    assert shell.moa_config is None


def test_architect_moa_policy_rejection_routes_to_configured_fallback(monkeypatch):
    """BUILD-573: a permanent Claude route-policy rejection at PRIMARY
    resolution (architect's ``max_only`` moa envelope) must fall through to the
    configured non-Claude fallback chain, NOT crash-loop to a clean give-up.

    Before the fix the primary ``ClaudeRoutePolicyError`` (a ``ValueError``, not
    an ``AuthError``) skipped the fallback chain, ``_ensure_runtime_credentials``
    returned False, and the worker exited clean ('Goodbye!') → dispatcher
    recorded a crash. Parity with the coder lane requires the policy rejection
    to activate ``gpt-5.6-sol`` exactly like an auth failure does.
    """
    cli = _import_cli()
    from agent.runtime_target import ClaudeRoutePolicyError, CLAUDE_ROUTE_POLICY_ERROR

    calls = {"count": 0}

    def _runtime_resolve(**kwargs):
        calls["count"] += 1
        # First call = the architect moa primary → policy-rejected.
        if kwargs.get("requested") == "moa":
            raise ClaudeRoutePolicyError(CLAUDE_ROUTE_POLICY_ERROR)
        # Fallback entry (gpt-5.6-sol) resolves cleanly.
        return {
            "provider": "openai-codex",
            "api_mode": "codex_responses",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "api_key": "sol-token",
            "source": "fallback-chain",
        }

    class _DummyAgent:
        def __init__(self, *args, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", _runtime_resolve)
    monkeypatch.setattr("hermes_cli.runtime_provider.format_runtime_provider_error", lambda exc: str(exc))
    monkeypatch.setattr("hermes_cli.fallback_config.resolve_entry_api_key", lambda entry: None)
    monkeypatch.setattr(cli, "AIAgent", _DummyAgent)

    shell = cli.HermesCLI(model="architect", compact=True, max_turns=1)
    shell.requested_provider = "moa"
    shell.provider = "moa"
    shell._fallback_model = [
        {"provider": "openai-codex", "model": "gpt-5.6-sol", "reasoning_effort": "xhigh"},
    ]

    assert shell._ensure_runtime_credentials() is True
    # The policy-rejected primary handed off to the configured fallback.
    assert shell.provider == "openai-codex"
    assert shell.model == "gpt-5.6-sol"
    assert calls["count"] == 2


def _run_fallback_after_primary_error(monkeypatch, primary_exc, fallback_chain):
    """Drive ``_ensure_runtime_credentials`` where the moa primary raises
    ``primary_exc`` and ``fallback_chain`` entries resolve in order. Returns the
    shell after resolution so callers can assert the chosen route."""
    cli = _import_cli()
    resolved = {}

    def _runtime_resolve(**kwargs):
        if kwargs.get("requested") == "moa":
            raise primary_exc
        # Echo the resolved fallback entry so the test can assert which won.
        prov = kwargs.get("requested")
        resolved["provider"] = prov
        return {
            "provider": "anthropic" if prov == "anthropic" else "openai-codex",
            "api_mode": "anthropic_messages" if prov == "anthropic" else "codex_responses",
            "runtime": "claude_agent_sdk" if prov == "anthropic" else "hermes",
            # claude_agent_sdk is a subscription runtime (empty base_url/key ok);
            # a codex fallback needs a concrete endpoint like the real chain.
            "base_url": "" if prov == "anthropic" else "https://chatgpt.com/backend-api/codex",
            "api_key": "" if prov == "anthropic" else "sol-token",
            "source": "fallback-chain",
        }

    class _DummyAgent:
        def __init__(self, *args, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", _runtime_resolve)
    monkeypatch.setattr("hermes_cli.runtime_provider.format_runtime_provider_error", lambda exc: str(exc))
    monkeypatch.setattr("hermes_cli.fallback_config.resolve_entry_api_key", lambda entry: None)
    monkeypatch.setattr(cli, "AIAgent", _DummyAgent)

    shell = cli.HermesCLI(model="architect", compact=True, max_turns=1)
    shell.requested_provider = "moa"
    shell.provider = "moa"
    shell._fallback_model = fallback_chain
    ok = shell._ensure_runtime_credentials()
    return shell, ok


def test_policy_rejection_and_auth_error_take_identical_fallback_path(monkeypatch):
    """BUILD-573 AC5: a ``ClaudeRoutePolicyError`` at the primary must activate
    the configured fallback identically to an ``AuthError`` — the coder lane
    already reaches fallback on Claude auth failure; the architect lane now has
    parity from its startup credential resolution."""
    from hermes_cli.auth import AuthError
    from agent.runtime_target import ClaudeRoutePolicyError, CLAUDE_ROUTE_POLICY_ERROR

    chain = [{"provider": "openai-codex", "model": "gpt-5.6-sol"}]

    shell_auth, ok_auth = _run_fallback_after_primary_error(
        monkeypatch, AuthError("primary auth failed"), chain
    )
    shell_pol, ok_pol = _run_fallback_after_primary_error(
        monkeypatch, ClaudeRoutePolicyError(CLAUDE_ROUTE_POLICY_ERROR), chain
    )

    assert ok_auth is True and ok_pol is True
    assert shell_auth.provider == shell_pol.provider == "openai-codex"
    assert shell_auth.model == shell_pol.model == "gpt-5.6-sol"


def _drive_exhausted_chain(monkeypatch, *, defer_on_exhaustion, quiet):
    """Drive ``_ensure_runtime_credentials`` where BOTH the moa primary AND the
    single fallback entry fail to resolve. Returns the list of failures passed
    to ``persist_credential_resolution_exhaustion`` (empty if it was never
    invoked)."""
    cli = _import_cli()
    from agent.runtime_target import ClaudeRoutePolicyError, CLAUDE_ROUTE_POLICY_ERROR

    def _runtime_resolve(**kwargs):
        if kwargs.get("requested") == "moa":
            raise ClaudeRoutePolicyError(CLAUDE_ROUTE_POLICY_ERROR)
        raise RuntimeError("fallback endpoint 503")

    captured = {"failures": None}

    def _fake_persist(failures):
        captured["failures"] = failures
        return True

    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", _runtime_resolve)
    monkeypatch.setattr("hermes_cli.runtime_provider.format_runtime_provider_error", lambda exc: str(exc))
    monkeypatch.setattr("hermes_cli.fallback_config.resolve_entry_api_key", lambda entry: None)
    monkeypatch.setattr(
        "agent.external_runtime.persist_credential_resolution_exhaustion", _fake_persist
    )

    shell = cli.HermesCLI(model="architect", compact=True, max_turns=1)
    shell.requested_provider = "moa"
    shell.provider = "moa"
    shell.tool_progress_mode = "off" if quiet else "full"
    shell._fallback_model = [{"provider": "openai-codex", "model": "gpt-5.6-sol"}]

    ok = shell._ensure_runtime_credentials(defer_on_exhaustion=defer_on_exhaustion)
    assert ok is False
    return captured["failures"]


def test_exhausted_chain_durably_defers_dispatched_worker(monkeypatch):
    """BUILD-573 AC2/AC4: the initial worker-startup resolution, when the whole
    chain is exhausted in a dispatched quiet worker, hands the primary + every
    fallback error to the durable defer/block helper — not a bare give-up."""
    from agent.runtime_target import ClaudeRoutePolicyError

    failures = _drive_exhausted_chain(monkeypatch, defer_on_exhaustion=True, quiet=True)
    assert failures is not None
    # Primary policy rejection + the fallback 503 both reach the helper.
    assert any(isinstance(f, ClaudeRoutePolicyError) for f in failures)
    assert any("503" in str(f) for f in failures)


def test_exhausted_chain_no_defer_on_per_turn_resolution(monkeypatch):
    """A per-turn re-resolution (defer_on_exhaustion=False) must NEVER requeue
    the card — its worker is alive mid-progress."""
    failures = _drive_exhausted_chain(monkeypatch, defer_on_exhaustion=False, quiet=True)
    assert failures is None


def test_exhausted_chain_no_defer_for_interactive_shell(monkeypatch):
    """An interactive (non-quiet) shell is not a dispatched worker even if the
    startup flag is set — no durable card mutation."""
    failures = _drive_exhausted_chain(monkeypatch, defer_on_exhaustion=True, quiet=False)
    assert failures is None


def test_policy_rejection_honors_fallback_chain_order(monkeypatch):
    """BUILD-573: architect's real chain is [claude-direct, gpt-5.6-sol]. A
    policy-rejected moa envelope routes into the chain and the FIRST resolvable
    entry (claude-direct, which passes validate and defers attestation to the
    SDK-attempt path) wins — so architect degrades to the same claude_agent_sdk
    circuit the coder lane uses, rather than crash-looping."""
    from agent.runtime_target import ClaudeRoutePolicyError, CLAUDE_ROUTE_POLICY_ERROR

    chain = [
        {"provider": "anthropic", "model": "claude-opus-4-8", "runtime": "claude_agent_sdk"},
        {"provider": "openai-codex", "model": "gpt-5.6-sol"},
    ]
    shell, ok = _run_fallback_after_primary_error(
        monkeypatch, ClaudeRoutePolicyError(CLAUDE_ROUTE_POLICY_ERROR), chain
    )
    assert ok is True
    assert shell.provider == "anthropic"
    assert shell.model == "claude-opus-4-8"


def test_cli_turn_routing_uses_primary_when_disabled(monkeypatch):
    cli = _import_cli()
    shell = cli.HermesCLI(model="gpt-5", compact=True, max_turns=1)
    shell.provider = "openrouter"
    shell.api_mode = "chat_completions"
    shell.base_url = "https://openrouter.ai/api/v1"
    shell.api_key = "sk-primary"

    result = shell._resolve_turn_agent_config("what time is it in tokyo?")

    assert result["model"] == "gpt-5"
    assert result["runtime"]["provider"] == "openrouter"


def test_cli_prefers_config_provider_over_stale_env_override(monkeypatch):
    cli = _import_cli()

    monkeypatch.setenv("HERMES_INFERENCE_PROVIDER", "openrouter")
    config_copy = dict(cli.CLI_CONFIG)
    model_copy = dict(config_copy.get("model", {}))
    model_copy["provider"] = "custom"
    model_copy["base_url"] = "https://api.fireworks.ai/inference/v1"
    config_copy["model"] = model_copy
    monkeypatch.setattr(cli, "CLI_CONFIG", config_copy)

    shell = cli.HermesCLI(model="fireworks/minimax-m2p5", compact=True, max_turns=1)

    assert shell.requested_provider == "custom"


def test_codex_provider_replaces_incompatible_default_model(monkeypatch):
    """When provider resolves to openai-codex and no model was explicitly
    chosen, the global config default (e.g. anthropic/claude-opus-4.6) must
    be replaced with a Codex-compatible model.  Fixes #651."""
    cli = _import_cli()

    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    # Ensure local user config does not leak a model into the test
    monkeypatch.setitem(cli.CLI_CONFIG, "model", {
        "default": "",
        "base_url": "https://openrouter.ai/api/v1",
    })

    def _runtime_resolve(**kwargs):
        return {
            "provider": "openai-codex",
            "api_mode": "codex_responses",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "api_key": "test-key",
            "source": "env/config",
        }

    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", _runtime_resolve)
    monkeypatch.setattr("hermes_cli.runtime_provider.format_runtime_provider_error", lambda exc: str(exc))
    monkeypatch.setattr(
        "hermes_cli.codex_models.get_codex_model_ids",
        lambda access_token=None: ["gpt-5.2-codex", "gpt-5.1-codex-mini"],
    )

    shell = cli.HermesCLI(compact=True, max_turns=1)

    assert shell._model_is_default is True
    assert shell._ensure_runtime_credentials() is True
    assert shell.provider == "openai-codex"
    assert "anthropic" not in shell.model
    assert "claude" not in shell.model
    assert shell.model == "gpt-5.2-codex"


def test_model_flow_nous_prints_subscription_guidance_without_mutating_explicit_tts(monkeypatch, capsys):
    monkeypatch.setattr(
        "hermes_cli.nous_subscription.managed_nous_tools_enabled",
        lambda *args, **kwargs: True,
    )
    config = {
        "model": {"provider": "nous", "default": "claude-opus-4-6"},
        "tts": {"provider": "elevenlabs"},
        "browser": {"cloud_provider": "browser-use"},
    }

    monkeypatch.setattr(
        "hermes_cli.auth.get_provider_auth_state",
        lambda provider: {"access_token": "nous-token"},
    )
    monkeypatch.setattr(
        "hermes_cli.auth.resolve_nous_runtime_credentials",
        lambda *args, **kwargs: {
            "base_url": "https://inference.example.com/v1",
            "api_key": "nous-key",
        },
    )
    monkeypatch.setattr(
        "hermes_cli.auth.fetch_nous_models",
        lambda *args, **kwargs: ["claude-opus-4-6"],
    )
    monkeypatch.setattr("hermes_cli.auth._prompt_model_selection", lambda model_ids, current_model="", pricing=None, **kw: "claude-opus-4-6")
    monkeypatch.setattr("hermes_cli.auth._save_model_choice", lambda model: None)
    monkeypatch.setattr("hermes_cli.auth._update_config_for_provider", lambda provider, url: None)

    hermes_main._model_flow_nous(config, current_model="claude-opus-4-6")

    out = capsys.readouterr().out
    assert "Default model set to:" in out
    assert config["tts"]["provider"] == "elevenlabs"
    assert config["browser"]["cloud_provider"] == "browser-use"


def test_model_flow_nous_does_not_restore_stale_custom_api_key(tmp_path, monkeypatch):
    import yaml

    config_home = tmp_path / "hermes"
    config_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(config_home))

    config_path = config_home / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "model": {
                    "provider": "custom",
                    "default": "glm-5.2",
                    "base_url": "https://api.neuralwatt.com/v1",
                    "api_key": "${NEURALWATT_API_KEY}",
                    "api_mode": "chat_completions",
                }
            },
            sort_keys=False,
        )
    )

    stale_config = yaml.safe_load(config_path.read_text()) or {}
    selected_model = "deepseek/deepseek-v4-flash"

    monkeypatch.setattr(
        "hermes_cli.auth.get_provider_auth_state",
        lambda provider: {
            "access_token": "nous-token",
            "portal_base_url": "https://portal.example.com",
        },
    )
    monkeypatch.setattr(
        "hermes_cli.auth.resolve_nous_runtime_credentials",
        lambda *args, **kwargs: {
            "base_url": "https://inference-api.nousresearch.com/v1",
            "api_key": "nous-key",
        },
    )
    monkeypatch.setattr(
        "hermes_cli.models.get_curated_nous_model_ids",
        lambda: [selected_model],
    )
    monkeypatch.setattr("hermes_cli.models.get_pricing_for_provider", lambda provider: {})
    monkeypatch.setattr("hermes_cli.models.check_nous_free_tier", lambda **kwargs: False)
    monkeypatch.setattr(
        "hermes_cli.models.union_with_portal_paid_recommendations",
        lambda model_ids, pricing, portal_url: (model_ids, pricing),
    )
    monkeypatch.setattr(
        "hermes_cli.auth._prompt_model_selection",
        lambda *args, **kwargs: selected_model,
    )
    monkeypatch.setattr(
        "hermes_cli.nous_subscription.prompt_enable_tool_gateway",
        lambda config: None,
    )

    hermes_main._model_flow_nous(stale_config, current_model="glm-5.2")

    config = yaml.safe_load(config_path.read_text()) or {}
    model = config.get("model")
    assert model["provider"] == "nous"
    assert model["default"] == selected_model
    assert model["base_url"] == "https://inference-api.nousresearch.com/v1"
    assert "api_key" not in model
    assert "api_mode" not in model


def _seed_stale_custom_model(tmp_path, monkeypatch):
    import yaml

    config_home = tmp_path / "hermes"
    config_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(config_home))
    config_path = config_home / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "model": {
                    "provider": "custom",
                    "default": "glm-5.2",
                    "base_url": "https://api.neuralwatt.com/v1",
                    "api_key": "${NEURALWATT_API_KEY}",
                    "api": "legacy-stale-key",
                    "api_mode": "anthropic_messages",
                }
            },
            sort_keys=False,
        )
    )
    (config_home / ".env").write_text("")
    return config_path


def test_model_flow_openrouter_clears_stale_custom_key(tmp_path, monkeypatch):
    import yaml

    config_path = _seed_stale_custom_model(tmp_path, monkeypatch)

    monkeypatch.setattr(
        "hermes_cli.main._prompt_api_key",
        lambda *args, **kwargs: ("sk-openrouter", False),
    )
    monkeypatch.setattr(
        "hermes_cli.models.model_ids",
        lambda **kwargs: ["anthropic/claude-sonnet-4.6"],
    )
    monkeypatch.setattr("hermes_cli.models.get_pricing_for_provider", lambda *a, **k: {})
    monkeypatch.setattr(
        "hermes_cli.auth._prompt_model_selection",
        lambda *args, **kwargs: "anthropic/claude-sonnet-4.6",
    )
    monkeypatch.setattr("hermes_cli.auth.deactivate_provider", lambda: None)

    hermes_main._model_flow_openrouter({}, current_model="glm-5.2")

    config = yaml.safe_load(config_path.read_text()) or {}
    model = config["model"]
    assert model["provider"] == "openrouter"
    assert model["default"] == "anthropic/claude-sonnet-4.6"
    assert model["api_mode"] == "chat_completions"
    assert "api_key" not in model
    assert "api" not in model


def test_model_flow_anthropic_clears_stale_custom_key_and_mode(tmp_path, monkeypatch):
    import yaml

    config_path = _seed_stale_custom_model(tmp_path, monkeypatch)

    monkeypatch.setattr("hermes_cli.auth.get_anthropic_key", lambda: "sk-ant-api03-test")
    monkeypatch.setattr(
        "agent.anthropic_adapter.read_claude_code_credentials",
        lambda: None,
    )
    monkeypatch.setattr(
        "agent.anthropic_adapter.is_claude_code_token_valid",
        lambda creds: False,
    )
    monkeypatch.setattr(
        "hermes_cli.model_setup_flows._prompt_auth_credentials_choice",
        lambda title: "use",
    )
    monkeypatch.setattr(
        "hermes_cli.auth._prompt_model_selection",
        lambda *args, **kwargs: "claude-sonnet-4-6",
    )
    monkeypatch.setattr("hermes_cli.auth.deactivate_provider", lambda: None)

    hermes_main._model_flow_anthropic({}, current_model="glm-5.2")

    config = yaml.safe_load(config_path.read_text()) or {}
    model = config["model"]
    assert model["provider"] == "anthropic"
    assert model["default"] == "claude-sonnet-4-6"
    assert "base_url" not in model
    assert "api_key" not in model
    assert "api" not in model
    assert "api_mode" not in model


def test_model_flow_nous_offers_tool_gateway_prompt_when_unconfigured(monkeypatch, capsys):
    from hermes_cli.nous_account import NousPortalAccountInfo

    # Entitled account (paid → all tools eligible) drives the offer; the prompt
    # is a per-tool checklist now, so capture the call rather than scrape stdout.
    monkeypatch.setattr(
        "hermes_cli.nous_subscription.get_nous_portal_account_info",
        lambda **kwargs: NousPortalAccountInfo(
            logged_in=True,
            source="account_api",
            fresh=True,
            paid_service_access=True,
        ),
    )
    captured = {}

    def _fake_checklist(title, items, pre_selected=None):
        captured["title"] = title
        captured["items"] = list(items)
        return []  # decline; we only assert the prompt was offered

    monkeypatch.setattr("hermes_cli.setup.prompt_checklist", _fake_checklist, raising=False)

    config = {
        "model": {"provider": "nous", "default": "claude-opus-4-6"},
        "tts": {"provider": "edge"},
    }

    monkeypatch.setattr(
        "hermes_cli.auth.get_provider_auth_state",
        lambda provider: {"access_token": "***"},
    )
    monkeypatch.setattr(
        "hermes_cli.auth.resolve_nous_runtime_credentials",
        lambda *args, **kwargs: {
            "base_url": "https://inference.example.com/v1",
            "api_key": "***",
        },
    )
    monkeypatch.setattr(
        "hermes_cli.auth.fetch_nous_models",
        lambda *args, **kwargs: ["claude-opus-4-6"],
    )
    monkeypatch.setattr("hermes_cli.auth._prompt_model_selection", lambda model_ids, current_model="", pricing=None, **kw: "claude-opus-4-6")
    monkeypatch.setattr("hermes_cli.auth._save_model_choice", lambda model: None)
    monkeypatch.setattr("hermes_cli.auth._update_config_for_provider", lambda provider, url: None)
    hermes_main._model_flow_nous(config, current_model="claude-opus-4-6")

    # The per-tool Tool Gateway checklist was offered.
    assert "title" in captured
    assert "Tool Gateway" in captured["title"] or "tool pool" in captured["title"].lower()


def test_codex_provider_uses_config_model(monkeypatch):
    """Model comes from config.yaml, not LLM_MODEL env var.
    Config.yaml is the single source of truth to avoid multi-agent conflicts."""
    cli = _import_cli()

    # LLM_MODEL env var should be IGNORED (even if set)
    monkeypatch.setenv("LLM_MODEL", "should-be-ignored")
    monkeypatch.delenv("OPENAI_MODEL", raising=False)

    # Set model via config
    monkeypatch.setitem(cli.CLI_CONFIG, "model", {
        "default": "gpt-5.2-codex",
        "provider": "openai-codex",
        "base_url": "https://chatgpt.com/backend-api/codex",
    })

    def _runtime_resolve(**kwargs):
        return {
            "provider": "openai-codex",
            "api_mode": "codex_responses",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "api_key": "fake-codex-token",
            "source": "env/config",
        }

    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", _runtime_resolve)
    monkeypatch.setattr("hermes_cli.runtime_provider.format_runtime_provider_error", lambda exc: str(exc))
    # Prevent live API call from overriding the config model
    monkeypatch.setattr(
        "hermes_cli.codex_models.get_codex_model_ids",
        lambda access_token=None: ["gpt-5.2-codex"],
    )

    shell = cli.HermesCLI(compact=True, max_turns=1)

    assert shell._ensure_runtime_credentials() is True
    assert shell.provider == "openai-codex"
    # Model from config (may be normalized by codex provider logic)
    assert "codex" in shell.model.lower()
    # LLM_MODEL env var is NOT used
    assert shell.model != "should-be-ignored"


def test_codex_config_model_not_replaced_by_normalization(monkeypatch):
    """When the user sets model.default in config.yaml to a specific codex
    model, _normalize_model_for_provider must NOT replace it with the latest
    available model from the API.  Regression test for #1887."""
    cli = _import_cli()

    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)

    # User explicitly configured gpt-5.3-codex in config.yaml
    monkeypatch.setitem(cli.CLI_CONFIG, "model", {
        "default": "gpt-5.3-codex",
        "provider": "openai-codex",
        "base_url": "https://chatgpt.com/backend-api/codex",
    })

    def _runtime_resolve(**kwargs):
        return {
            "provider": "openai-codex",
            "api_mode": "codex_responses",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "api_key": "fake-key",
            "source": "env/config",
        }

    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", _runtime_resolve)
    monkeypatch.setattr("hermes_cli.runtime_provider.format_runtime_provider_error", lambda exc: str(exc))
    # API returns a DIFFERENT model than what the user configured
    monkeypatch.setattr(
        "hermes_cli.codex_models.get_codex_model_ids",
        lambda access_token=None: ["gpt-5.4", "gpt-5.3-codex"],
    )

    shell = cli.HermesCLI(compact=True, max_turns=1)

    # Config model is NOT the global default — user made a deliberate choice
    assert shell._model_is_default is False
    assert shell._ensure_runtime_credentials() is True
    assert shell.provider == "openai-codex"
    # Model must stay as user configured, not replaced by gpt-5.4
    assert shell.model == "gpt-5.3-codex"


def test_codex_provider_preserves_explicit_codex_model(monkeypatch):
    """If the user explicitly passes a Codex-compatible model, it must be
    preserved even when the provider resolves to openai-codex."""
    cli = _import_cli()

    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)

    def _runtime_resolve(**kwargs):
        return {
            "provider": "openai-codex",
            "api_mode": "codex_responses",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "api_key": "test-key",
            "source": "env/config",
        }

    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", _runtime_resolve)
    monkeypatch.setattr("hermes_cli.runtime_provider.format_runtime_provider_error", lambda exc: str(exc))

    shell = cli.HermesCLI(model="gpt-5.1-codex-mini", compact=True, max_turns=1)

    assert shell._model_is_default is False
    assert shell._ensure_runtime_credentials() is True
    assert shell.model == "gpt-5.1-codex-mini"


def test_codex_provider_strips_provider_prefix_from_model(monkeypatch):
    """openai/gpt-5.3-codex should become gpt-5.3-codex — the Codex
    Responses API does not accept provider-prefixed model slugs."""
    cli = _import_cli()

    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)

    def _runtime_resolve(**kwargs):
        return {
            "provider": "openai-codex",
            "api_mode": "codex_responses",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "api_key": "test-key",
            "source": "env/config",
        }

    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", _runtime_resolve)
    monkeypatch.setattr("hermes_cli.runtime_provider.format_runtime_provider_error", lambda exc: str(exc))

    shell = cli.HermesCLI(model="openai/gpt-5.3-codex", compact=True, max_turns=1)

    assert shell._ensure_runtime_credentials() is True
    assert shell.model == "gpt-5.3-codex"


def test_cmd_model_falls_back_to_auto_on_invalid_provider(monkeypatch, capsys):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"model": {"default": "gpt-5", "provider": "invalid-provider"}},
    )
    monkeypatch.setattr("hermes_cli.config.save_config", lambda cfg: None)
    monkeypatch.setattr("hermes_cli.config.get_env_value", lambda key: "")
    monkeypatch.setattr("hermes_cli.config.save_env_value", lambda key, value: None)

    def _resolve_provider(requested, **kwargs):
        if requested == "invalid-provider":
            raise AuthError("Unknown provider 'invalid-provider'.", code="invalid_provider")
        return "openrouter"

    monkeypatch.setattr("hermes_cli.auth.resolve_provider", _resolve_provider)
    monkeypatch.setattr(hermes_main, "_prompt_provider_choice", lambda choices, **kwargs: len(choices) - 1)
    monkeypatch.setattr("sys.stdin", type("FakeTTY", (), {"isatty": lambda self: True})())

    hermes_main.cmd_model(SimpleNamespace())
    output = capsys.readouterr().out

    assert "Warning:" in output
    assert "falling back to auto provider detection" in output.lower()
    assert "No change." in output


def test_model_flow_custom_saves_verified_v1_base_url(monkeypatch, capsys):
    monkeypatch.setattr(
        "hermes_cli.config.get_env_value",
        lambda key: "" if key in {"OPENAI_BASE_URL", "OPENAI_API_KEY"} else "",
    )
    saved_env = {}
    monkeypatch.setattr("hermes_cli.config.save_env_value", lambda key, value: saved_env.__setitem__(key, value))
    monkeypatch.setattr("hermes_cli.auth._save_model_choice", lambda model: saved_env.__setitem__("MODEL", model))
    monkeypatch.setattr("hermes_cli.auth.deactivate_provider", lambda: None)
    monkeypatch.setattr("hermes_cli.main._save_custom_provider", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "hermes_cli.models.probe_api_models",
        lambda api_key, base_url: {
            "models": ["llm"],
            "probed_url": "http://localhost:8000/v1/models",
            "resolved_base_url": "http://localhost:8000/v1",
            "suggested_base_url": "http://localhost:8000/v1",
            "used_fallback": True,
        },
    )
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"model": {"default": "", "provider": "custom", "base_url": ""}},
    )
    monkeypatch.setattr("hermes_cli.config.save_config", lambda cfg: None)

    # After the probe detects a single model ("llm"), the flow asks
    # "Use this model? [Y/n]:" — confirm with Enter, then context length,
    # then display name. The api_mode prompt also runs before model selection.
    answers = iter(["http://localhost:8000", "local-key", "", "", "", "", ""])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    monkeypatch.setattr("hermes_cli.secret_prompt.masked_secret_prompt", lambda _prompt="": next(answers))

    hermes_main._model_flow_custom({})
    output = capsys.readouterr().out

    assert "Saving the working base URL instead" in output
    assert "Detected model: llm" in output
    # OPENAI_BASE_URL is no longer saved to .env — config.yaml is authoritative
    assert "OPENAI_BASE_URL" not in saved_env
    assert saved_env["MODEL"] == "llm"


def test_model_flow_custom_persists_selected_api_mode(monkeypatch):
    saved_cfg = {"model": {"default": "", "provider": "custom", "base_url": ""}}
    captured_provider = {}

    monkeypatch.setattr(
        "hermes_cli.config.get_env_value",
        lambda key: "" if key in {"OPENAI_BASE_URL", "OPENAI_API_KEY"} else "",
    )
    monkeypatch.setattr("hermes_cli.auth._save_model_choice", lambda model: None)
    monkeypatch.setattr("hermes_cli.auth.deactivate_provider", lambda: None)
    monkeypatch.setattr(
        "hermes_cli.models.probe_api_models",
        lambda api_key, base_url: {
            "models": [],
            "probed_url": f"{base_url.rstrip('/')}/models",
            "resolved_base_url": None,
            "suggested_base_url": None,
            "used_fallback": False,
        },
    )
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: saved_cfg)
    monkeypatch.setattr("hermes_cli.config.save_config", lambda cfg: saved_cfg.update(cfg))
    monkeypatch.setattr(
        "hermes_cli.main._save_custom_provider",
        lambda base_url, api_key="", model="", context_length=None, name=None, api_mode=None: captured_provider.update(
            {
                "base_url": base_url,
                "api_key": api_key,
                "model": model,
                "context_length": context_length,
                "name": name,
                "api_mode": api_mode,
            }
        ),
    )

    answers = iter(
        [
            "https://codex.example.com/v1",
            "3",
            "chosen-model",
            "",
            "",
        ]
    )
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    monkeypatch.setattr("hermes_cli.secret_prompt.masked_secret_prompt", lambda _prompt="": "test-key")

    hermes_main._model_flow_custom({"model": {"provider": "custom"}})

    assert saved_cfg["model"]["provider"] == "custom"
    assert saved_cfg["model"]["base_url"] == "https://codex.example.com/v1"
    assert saved_cfg["model"]["api_key"] == "test-key"
    assert saved_cfg["model"]["api_mode"] == "codex_responses"
    assert captured_provider["api_mode"] == "codex_responses"


def test_cmd_model_forwards_nous_login_tls_options(monkeypatch):
    monkeypatch.setattr(hermes_main, "_require_tty", lambda *a: None)
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"model": {"default": "gpt-5", "provider": "nous"}},
    )
    monkeypatch.setattr("hermes_cli.config.save_config", lambda cfg: None)
    monkeypatch.setattr("hermes_cli.config.get_env_value", lambda key: "")
    monkeypatch.setattr("hermes_cli.config.save_env_value", lambda key, value: None)
    monkeypatch.setattr("hermes_cli.auth.resolve_provider", lambda requested, **kwargs: "nous")
    monkeypatch.setattr("hermes_cli.auth.get_provider_auth_state", lambda provider_id: None)
    monkeypatch.setattr(hermes_main, "_prompt_provider_choice", lambda choices, **kwargs: 0)

    captured = {}

    def _fake_login(login_args, provider_config):
        captured["portal_url"] = login_args.portal_url
        captured["inference_url"] = login_args.inference_url
        captured["client_id"] = login_args.client_id
        captured["scope"] = login_args.scope
        captured["no_browser"] = login_args.no_browser
        captured["timeout"] = login_args.timeout
        captured["ca_bundle"] = login_args.ca_bundle
        captured["insecure"] = login_args.insecure

    monkeypatch.setattr("hermes_cli.auth._login_nous", _fake_login)

    hermes_main.cmd_model(
        SimpleNamespace(
            portal_url="https://portal.nousresearch.com",
            inference_url="https://inference.nousresearch.com/v1",
            client_id="hermes-local",
            scope="openid profile",
            no_browser=True,
            timeout=7.5,
            ca_bundle="/tmp/local-ca.pem",
            insecure=True,
        )
    )

    assert captured == {
        "portal_url": "https://portal.nousresearch.com",
        "inference_url": "https://inference.nousresearch.com/v1",
        "client_id": "hermes-local",
        "scope": "openid profile",
        "no_browser": True,
        "timeout": 7.5,
        "ca_bundle": "/tmp/local-ca.pem",
        "insecure": True,
    }


# ---------------------------------------------------------------------------
# _auto_provider_name — unit tests
# ---------------------------------------------------------------------------

def test_auto_provider_name_localhost():
    from hermes_cli.main import _auto_provider_name
    assert _auto_provider_name("http://localhost:11434/v1") == "Local (localhost:11434)"
    assert _auto_provider_name("http://127.0.0.1:1234/v1") == "Local (127.0.0.1:1234)"


def test_auto_provider_name_runpod():
    from hermes_cli.main import _auto_provider_name
    assert "RunPod" in _auto_provider_name("https://xyz.runpod.io/v1")


def test_auto_provider_name_remote():
    from hermes_cli.main import _auto_provider_name
    result = _auto_provider_name("https://api.together.xyz/v1")
    assert result == "Api.together.xyz"


def test_save_custom_provider_uses_provided_name(monkeypatch, tmp_path):
    """When a display name is passed, it should appear in the saved entry."""
    import yaml
    from hermes_cli.main import _save_custom_provider

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.dump({}))

    monkeypatch.setattr(
        "hermes_cli.config.load_config", lambda: yaml.safe_load(cfg_path.read_text()) or {},
    )
    saved = {}
    def _save(cfg):
        saved.update(cfg)
    monkeypatch.setattr("hermes_cli.config.save_config", _save)

    _save_custom_provider("http://localhost:11434/v1", name="Ollama")
    entries = saved.get("custom_providers", [])
    assert len(entries) == 1
    assert entries[0]["name"] == "Ollama"
