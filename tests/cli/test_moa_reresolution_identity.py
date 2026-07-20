"""BUILD-589: moa route re-resolution must keep the preset identity.

The first successful resolution of a moa route promotes the preset's
aggregator (e.g. anthropic/claude-opus-4-8/claude_agent_sdk) and writes the
aggregator's wire-model back onto ``self.model``. Re-resolving with
``(provider=moa, model=<claude-aggregator-model>)`` used to lose the preset
(name lookup fails), skip aggregator promotion, and trip the Claude Max
route policy fatally — killing every kanban architect worker (which resolves
credentials at least twice per run) while one-shot interactive probes passed.
"""

import types

from hermes_cli.cli_agent_setup_mixin import CLIAgentSetupMixin


class _StubCLI(CLIAgentSetupMixin):
    def __init__(self):
        self.requested_provider = "moa"
        self.model = "architect-consensus"
        self._explicit_api_key = None
        self._explicit_base_url = None
        self._fallback_model = []
        self.api_mode = "anthropic_messages"
        self.api_key = None
        self.base_url = None
        self.provider = "moa"
        self._model_is_default = False
        self.agent = None
        self.verbose = False
        self.max_tokens = None
        self.reasoning_effort = None

    def _normalize_model_for_provider(self, resolved_provider):
        return False


def test_second_resolution_keeps_preset_identity(monkeypatch):
    calls = []

    def fake_resolve(**kwargs):
        target = kwargs.get("target_model")
        calls.append(target)
        # Emulate the real resolver: the preset promotes to the aggregator;
        # a claude-* model under provider=moa is the policy-fatal shape.
        if target == "architect-consensus":
            return {
                "provider": "anthropic",
                "model": "claude-opus-4-8",
                "runtime": "claude_agent_sdk",
                "api_mode": "anthropic_messages",
                "moa_config": {"aggregator": {"model": "claude-opus-4-8"}},
                "api_key": None,
                "base_url": "",
            }
        raise ValueError(
            "Claude routes are restricted to the first-party Claude Max login"
        )

    import hermes_cli.runtime_provider as rp
    monkeypatch.setattr(rp, "resolve_runtime_provider", fake_resolve)

    import cli as cli_module
    monkeypatch.setattr(
        cli_module, "ChatConsole", lambda: types.SimpleNamespace(print=lambda *a, **k: None),
        raising=False,
    )
    monkeypatch.setattr(cli_module, "_cprint", lambda *a, **k: None, raising=False)

    stub = _StubCLI()
    # First resolution: succeeds, writes the aggregator model onto .model.
    assert stub._ensure_runtime_credentials() is True
    assert stub.model == "claude-opus-4-8"
    # Second resolution (per-turn re-resolve): must re-use the preset
    # identity, not the promoted wire-model.
    assert stub._ensure_runtime_credentials() is True
    assert calls == ["architect-consensus", "architect-consensus"]


def test_fresh_preset_choice_beats_remembered_identity(monkeypatch):
    """Sol review 2a/2b: a user-chosen new preset must not lose to the
    remembered identity — substitution happens only when self.model is
    exactly the wire-model the write-back stored."""
    calls = []

    def fake_resolve(**kwargs):
        calls.append(kwargs.get("target_model"))
        return {
            "provider": "anthropic",
            "model": "claude-opus-4-8",
            "runtime": "claude_agent_sdk",
            "api_mode": "anthropic_messages",
            "moa_config": {"aggregator": {"model": "claude-opus-4-8"}},
            "api_key": None,
            "base_url": "",
        }

    import hermes_cli.runtime_provider as rp
    monkeypatch.setattr(rp, "resolve_runtime_provider", fake_resolve)
    import cli as cli_module
    import types as _t
    monkeypatch.setattr(cli_module, "ChatConsole", lambda: _t.SimpleNamespace(print=lambda *a, **k: None), raising=False)
    monkeypatch.setattr(cli_module, "_cprint", lambda *a, **k: None, raising=False)

    stub = _StubCLI()
    assert stub._ensure_runtime_credentials() is True
    # User switches to a different preset mid-session.
    stub.model = "other-consensus"
    assert stub._ensure_runtime_credentials() is True
    assert calls == ["architect-consensus", "other-consensus"]
