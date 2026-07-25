"""Agent-construction and session-resume display methods for ``HermesCLI``.

Extracted from ``cli.py`` as part of the god-file decomposition campaign
(``~/.hermes/plans/god-file-decomposition.md``, Phase 4 step 2). This mixin holds
the agent lifecycle/setup cluster: runtime-credential resolution, per-turn agent
config, first-use agent construction, and resumed-session preload + history recap.

Behavior-neutral: every method is lifted verbatim from ``HermesCLI``. ``self.*``
calls resolve unchanged via the MRO. Neutral dependencies are imported at module
top level; ``cli.py``-internal helpers/constants are imported lazily inside each
method (``from cli import ...`` resolves at call time, when ``cli`` is fully
loaded) so this module never imports ``cli`` at import time -> no import cycle.
"""

from __future__ import annotations

import sys

from rich.markup import escape as _escape


class CLIAgentSetupMixin:
    """Agent construction + session-resume display methods for ``HermesCLI``."""

    def _ensure_runtime_credentials(self, *, defer_on_exhaustion: bool = False) -> bool:
        """
        Ensure runtime credentials are resolved before agent use.
        Re-resolves provider credentials so key rotation and token refresh
        are picked up without restarting the CLI.
        Returns True if credentials are ready, False on auth failure.

        ``defer_on_exhaustion`` is set ONLY by the initial worker-startup call
        (``_init_agent``). When True and this is a dispatched quiet Kanban
        worker whose primary route AND fallback chain all fail to resolve, the
        give-up durably requeues/blocks the card instead of exiting clean into
        a dispatcher-counted crash (BUILD-573 AC2/AC4). It stays False for the
        per-turn re-resolution and ``/background`` call sites, whose worker is
        alive and mid-progress — deferring their card would be wrong.
        """
        from cli import ChatConsole, _cprint, logger
        from hermes_cli.runtime_provider import (
            resolve_runtime_provider,
            format_runtime_provider_error,
        )

        from hermes_cli.fallback_config import resolve_entry_api_key
        from hermes_cli.resolution_failure import (
            classify_resolution_failure,
            format_resolution_failure,
        )

        def _resolve_entry(entry: dict):
            """Resolve one fallback entry atomically from its OWN route.

            BUILD-591: the primary route's explicit api key / base URL must
            never cross the fallback boundary — neither on first selection nor
            on any later re-resolution of an already-selected fallback.
            """
            kwargs = {
                "requested": str(entry.get("provider") or "").strip().lower(),
                "target_model": str(entry.get("model") or "").strip(),
                "route_config": entry,
            }
            if entry.get("base_url"):
                kwargs["explicit_base_url"] = entry["base_url"]
            entry_api_key = resolve_entry_api_key(entry)
            if entry_api_key:
                kwargs["explicit_api_key"] = entry_api_key
            return resolve_runtime_provider(**kwargs)

        _primary_exc = None
        runtime = None
        _fallback_excs: list = []  # collected per-fallback resolution errors (BUILD-573)
        # BUILD-591: a fallback selected by an earlier call stays pinned, so a
        # per-turn re-resolution re-resolves THAT route rather than re-sending
        # the primary's credentials to the fallback provider. The pin is
        # dropped as soon as the live route no longer matches it (a user or
        # preset route change always wins) or if re-resolving it fails, in
        # which case we fall through to the ordinary primary + full-chain path.
        _pin = getattr(self, "_active_fallback_entry", None)
        if isinstance(_pin, dict) and not (
            str(_pin.get("provider") or "").strip().lower()
            == (self.requested_provider or "").strip().lower()
            and str(_pin.get("model") or "").strip() == (self.model or "").strip()
        ):
            _pin = None
            self._active_fallback_entry = None
        if isinstance(_pin, dict):
            try:
                runtime = _resolve_entry(_pin)
            except Exception as exc:
                self._active_fallback_entry = None
                logger.warning(
                    "Pinned fallback re-resolution failed, retrying the full route: %s",
                    format_resolution_failure(
                        classify_resolution_failure(exc),
                        surface="cli",
                        phase="pinned_fallback",
                        provider=self.requested_provider,
                        model=self.model,
                    ),
                )
        # Credentials passed explicitly on the command line belong to the
        # primary provider only; once we are running on a fallback provider
        # they must not be re-sent (BUILD-591).
        _primary_credentials_apply = (self.requested_provider or "").strip().lower() == (
            getattr(self, "_primary_provider", None) or self.requested_provider or ""
        ).strip().lower()
        # BUILD-589: re-resolution for a moa route must use the preset
        # identity, not the aggregator wire-model a prior resolution wrote
        # onto self.model (see the write-back below).
        _target_model = self.model or None
        if (self.requested_provider or "").strip().lower() == "moa":
            # Substitute ONLY when self.model is exactly the wire-model our
            # own write-back stored — a fresh user/preset/fallback choice
            # (any other value) always wins, so a stale identity can never
            # override an intentional route change (Sol review 2a/2b/2c).
            if self.model and self.model == getattr(self, "_moa_wire_model", None):
                _target_model = getattr(self, "_moa_route_model", None) or _target_model
        if runtime is None:
            try:
                runtime = resolve_runtime_provider(
                    requested=self.requested_provider,
                    explicit_api_key=(
                        self._explicit_api_key if _primary_credentials_apply else None
                    ),
                    explicit_base_url=(
                        self._explicit_base_url if _primary_credentials_apply else None
                    ),
                    target_model=_target_model,
                )
            except Exception as exc:
                _primary_exc = exc
                logger.error(
                    "Provider resolution failed: %s",
                    format_resolution_failure(
                        classify_resolution_failure(exc),
                        surface="cli",
                        phase="primary",
                        provider=self.requested_provider,
                        model=self.model,
                    ),
                )

        # Primary provider auth failed — try fallback providers before giving up.
        if runtime is None and _primary_exc is not None:
            from agent.runtime_target import ClaudeRoutePolicyError

            # A ``max_only`` Claude route-policy rejection is a PERMANENT
            # Claude-auth-unavailability condition (the governed route can never
            # satisfy first-party-Max-only), not a transient credential miss.
            # Treat it exactly like an ``AuthError`` so it activates the
            # configured non-Claude fallback chain (e.g. architect's moa
            # envelope → gpt-5.6-sol) instead of crash-looping to a clean
            # give-up. This reaches parity with the coder lane, whose direct
            # claude_agent_sdk route classifies the same failure as
            # ``auth_permanent`` and falls back via the runtime circuit
            # (BUILD-573).
            if isinstance(_primary_exc, ClaudeRoutePolicyError):
                logger.warning(
                    "Claude route rejected by max_only policy for provider=%r "
                    "model=%r; activating configured fallback chain: %s",
                    self.requested_provider, self.model, _primary_exc,
                )
            # BUILD-591: eligibility is decided by typed classification, not by
            # an isinstance ladder or exception-message text. It is behavior-
            # identical for AuthError and ClaudeRoutePolicyError, and adds the
            # model/provider-mismatch case, which is a permanent misconfiguration
            # and must NOT burn the fallback chain.
            if classify_resolution_failure(_primary_exc).fallback_eligible:
                _fb_chain = self._fallback_model if isinstance(self._fallback_model, list) else []
                for _fb in _fb_chain:
                    _fb_provider = (_fb.get("provider") or "").strip().lower()
                    _fb_model = (_fb.get("model") or "").strip()
                    if not _fb_provider or not _fb_model:
                        continue
                    try:
                        # ``_resolve_entry`` carries the fallback route's own
                        # runtime identity (Claude Max closed-world / runtime-
                        # target validation) and its own key_env/base_url
                        # (#... 998e35313); the primary's explicit credentials
                        # never cross this boundary (BUILD-591).
                        runtime = _resolve_entry(_fb)
                        logger.warning(
                            "Primary provider auth failed (%s). Falling through to fallback: %s/%s",
                            _primary_exc, _fb_provider, _fb_model,
                        )
                        _cprint(f"⚠️  Primary auth failed — switching to fallback: {_fb_provider} / {_fb_model}")
                        self._primary_provider = self.requested_provider
                        self.requested_provider = _fb_provider
                        self.model = _fb_model
                        # Pin the selected entry so a later re-resolution
                        # re-resolves THIS route atomically (BUILD-591).
                        self._active_fallback_entry = dict(_fb)
                        _primary_exc = None
                        break
                    except Exception as _fb_exc:
                        # Keep trying the rest of the chain, but never silently:
                        # a skipped fallback (e.g. a mis-routed Claude entry that
                        # itself trips the max_only policy) must be diagnosable
                        # so an exhausted chain has actionable recovery breadcrumbs
                        # instead of a bare give-up (BUILD-573).
                        logger.warning(
                            "Fallback entry %s/%s failed to resolve, trying next: %s",
                            _fb_provider, _fb_model, _fb_exc,
                        )
                        logger.warning(
                            "Provider fallback attempt failed: %s",
                            format_resolution_failure(
                                classify_resolution_failure(_fb_exc),
                                surface="cli",
                                phase="fallback",
                                provider=_fb_provider,
                                model=_fb_model,
                            ),
                        )
                        _fallback_excs.append(_fb_exc)
                        continue

        if runtime is None:
            message = format_runtime_provider_error(_primary_exc) if _primary_exc else "Provider resolution failed."
            # BUILD-589: this exit used to be silent in worker logs — the
            # dispatcher only saw "pid gone" after 'Goodbye!'. Always log the
            # full resolution failure with inputs so a dead lane is
            # diagnosable from errors.log alone.
            import os as _os
            _log_fn = (
                logger.debug
                if _os.environ.get("PYTEST_CURRENT_TEST")
                else logger.error
            )
            _log_fn(
                "provider resolution failed (fatal for this run): "
                "requested_provider=%r model=%r exc=%r",
                self.requested_provider, self.model, _primary_exc,
                exc_info=_primary_exc,
            )
            ChatConsole().print(f"[bold red]{message}[/]")
            # BUILD-573 AC2/AC4: a dispatched quiet Kanban worker whose whole
            # route (primary + fallback chain) failed to resolve must NOT exit
            # clean into a dispatcher-counted crash → 2-strike self-arrest.
            # Durably requeue (transient, auto-heals) or block-once after a
            # bounded streak (proven-broken chain). Gated to the initial
            # worker-startup call so a per-turn re-resolution or /background
            # failure — whose worker is alive mid-progress — never defers the
            # card. Best-effort; on any failure we keep the plain give-up.
            _is_quiet_worker = getattr(self, "tool_progress_mode", "full") == "off"
            if defer_on_exhaustion and _is_quiet_worker:
                try:
                    from agent.external_runtime import (
                        persist_credential_resolution_exhaustion,
                    )

                    _failures = ([_primary_exc] if _primary_exc else []) + _fallback_excs
                    persist_credential_resolution_exhaustion(_failures)
                except Exception:
                    logger.warning(
                        "credential-resolution durable defer failed; "
                        "falling through to give-up", exc_info=True,
                    )
            return False

        api_key = runtime.get("api_key")
        base_url = runtime.get("base_url")
        resolved_provider = runtime.get("provider", "openrouter")
        resolved_api_mode = runtime.get("api_mode", self.api_mode)
        resolved_agent_runtime = runtime.get("runtime", "hermes")
        resolved_moa_config = runtime.get("moa_config")
        resolved_acp_command = runtime.get("command")
        resolved_acp_args = list(runtime.get("args") or [])
        resolved_credential_pool = runtime.get("credential_pool")
        resolved_max_output_tokens = runtime.get("max_output_tokens")
        resolved_request_timeout = runtime.get("request_timeout_seconds")
        resolved_stale_timeout = runtime.get("stale_timeout_seconds")
        resolved_total_attempt_timeout = runtime.get("total_attempt_timeout_seconds")
        resolved_first_event_timeout = runtime.get("first_event_timeout_seconds")
        # A callable api_key is a bearer-token provider (Azure Foundry
        # Entra ID — ``azure_identity_adapter.build_token_provider``).
        # The OpenAI SDK accepts ``Callable[[], str]`` for ``api_key`` and
        # invokes it before every request. Skip the string-only validation
        # and placeholder substitution for callables.
        _is_callable_provider = callable(api_key) and not isinstance(api_key, str)
        _is_subscription_runtime = resolved_agent_runtime == "claude_agent_sdk"
        if (
            not _is_subscription_runtime
            and not _is_callable_provider
            and (not isinstance(api_key, str) or not api_key)
        ):
            # Custom / local endpoints (llama.cpp, ollama, vLLM, etc.) often
            # don't require authentication.  When a base_url IS configured but
            # no API key was found, use a placeholder so the OpenAI SDK
            # doesn't reject the request and local servers just ignore it.
            _source = runtime.get("source", "")
            _has_custom_base = isinstance(base_url, str) and base_url and "openrouter.ai" not in base_url
            if _has_custom_base:
                api_key = "no-key-required"
                logger.debug(
                    "No API key for custom endpoint %s (source=%s), "
                    "using placeholder — local servers typically ignore auth",
                    base_url, _source,
                )
            else:
                print("\n⚠️  Provider resolver returned an empty API key. "
                      "Set OPENROUTER_API_KEY or run: hermes setup")
                return False
        if (
            not _is_subscription_runtime
            and (not isinstance(base_url, str) or not base_url)
        ):
            print("\n⚠️  Provider resolver returned an empty base URL. "
                  "Check your provider config or run: hermes setup")
            return False

        credentials_changed = api_key != self.api_key or base_url != self.base_url
        routing_changed = (
            resolved_provider != self.provider
            or resolved_api_mode != self.api_mode
            or resolved_agent_runtime != getattr(self, "agent_runtime", "hermes")
            or resolved_acp_command != self.acp_command
            or resolved_acp_args != self.acp_args
            or resolved_moa_config != getattr(self, "moa_config", None)
            or resolved_max_output_tokens
            != getattr(self, "_route_max_output_tokens", None)
            or resolved_request_timeout
            != getattr(self, "_route_request_timeout_seconds", None)
            or resolved_stale_timeout
            != getattr(self, "_route_stale_timeout_seconds", None)
            or resolved_total_attempt_timeout
            != getattr(self, "_route_total_attempt_timeout_seconds", None)
            or resolved_first_event_timeout
            != getattr(self, "_route_first_event_timeout_seconds", None)
        )
        self.provider = resolved_provider
        self.api_mode = resolved_api_mode
        self.agent_runtime = resolved_agent_runtime
        self.acp_command = resolved_acp_command
        self.acp_args = resolved_acp_args
        self.moa_config = resolved_moa_config
        self._credential_pool = resolved_credential_pool
        self._route_max_output_tokens = resolved_max_output_tokens
        self._route_request_timeout_seconds = resolved_request_timeout
        self._route_stale_timeout_seconds = resolved_stale_timeout
        self._route_total_attempt_timeout_seconds = resolved_total_attempt_timeout
        self._route_first_event_timeout_seconds = resolved_first_event_timeout
        self._provider_source = runtime.get("source")
        self.api_key = api_key
        self.base_url = base_url

        # When a custom_provider entry carries an explicit `model` field,
        # use it as the effective model name.  Without this, running
        # `hermes chat --model <provider-name>` sends the provider name
        # (e.g. "my-provider") as the model string to the API instead of
        # the configured model (e.g. "qwen3.6-plus"), causing 400 errors.
        runtime_model = runtime.get("model")
        if runtime_model and isinstance(runtime_model, str):
            # Only use runtime model if: model is unset, or model equals provider name
            should_use_runtime_model = (
                not self.model or  # No model configured yet
                self.model == self.provider or  # Model is the provider slug
                self.model == runtime.get("name")  # Model matches provider display name
            )
            if should_use_runtime_model or resolved_moa_config is not None:
                if resolved_moa_config is not None:
                    # BUILD-589: for moa routes, self.model must carry the
                    # aggregator's wire-model for the API call, but the ROUTE
                    # identity stays the preset. Remember it — re-resolving
                    # with (provider=moa, model=<aggregator's claude-*>) loses
                    # the preset, skips aggregator promotion, and trips the
                    # Claude Max route policy fatally on the second call
                    # (kanban workers resolve at least twice; this killed the
                    # whole architect lane).
                    self._moa_route_model = self.model
                    self._moa_wire_model = runtime_model
                self.model = runtime_model

        # If model is still empty (e.g. user ran `hermes auth add openai-codex`
        # without `hermes model`), fall back to the provider's first catalog
        # model so the API call doesn't fail with "model must be non-empty".
        if not self.model and resolved_provider:
            try:
                from hermes_cli.models import get_default_model_for_provider
                _default = get_default_model_for_provider(resolved_provider)
                if _default:
                    self.model = _default
                    logger.info(
                        "No model configured — defaulting to %s for provider %s",
                        _default, resolved_provider,
                    )
            except Exception:
                pass

        # Normalize model for the resolved provider (e.g. swap non-Codex
        # models when provider is openai-codex).  Fixes #651.
        model_changed = self._normalize_model_for_provider(resolved_provider)

        # AIAgent/OpenAI client holds auth at init time, so rebuild if key,
        # routing, or the effective model changed.
        if (credentials_changed or routing_changed or model_changed) and self.agent is not None:
            self.agent = None
            self._active_agent_route_signature = None

        return True

    def _resolve_turn_agent_config(self, user_message: str) -> dict:
        """Build the effective model/runtime config for a single user turn.

        Always uses the session's primary model/provider.  If the user has
        toggled `/fast` on and the current model supports Priority
        Processing / Anthropic fast mode, attach `request_overrides` so the
        API call is marked accordingly.
        """
        from hermes_cli.models import resolve_fast_mode_overrides

        runtime = {
            "api_key": self.api_key,
            "base_url": self.base_url,
            "provider": self.provider,
            "api_mode": self.api_mode,
            "runtime": getattr(self, "agent_runtime", "hermes"),
            "command": self.acp_command,
            "args": list(self.acp_args or []),
            "credential_pool": getattr(self, "_credential_pool", None),
            "max_output_tokens": getattr(self, "_route_max_output_tokens", None),
            "request_timeout_seconds": getattr(
                self, "_route_request_timeout_seconds", None
            ),
            "stale_timeout_seconds": getattr(
                self, "_route_stale_timeout_seconds", None
            ),
            "total_attempt_timeout_seconds": getattr(
                self, "_route_total_attempt_timeout_seconds", None
            ),
            "first_event_timeout_seconds": getattr(
                self, "_route_first_event_timeout_seconds", None
            ),
        }
        route = {
            "model": self.model,
            "runtime": runtime,
            "signature": (
                self.model,
                runtime["provider"],
                runtime["base_url"],
                runtime["api_mode"],
                runtime["runtime"],
                runtime["command"],
                tuple(runtime["args"]),
            ),
        }

        service_tier = getattr(self, "service_tier", None)
        if not service_tier:
            route["request_overrides"] = None
            return route

        try:
            overrides = resolve_fast_mode_overrides(route["model"])
        except Exception:
            overrides = None
        route["request_overrides"] = overrides
        return route

    def _init_agent(self, *, model_override: str = None, runtime_override: dict = None, request_overrides: dict | None = None) -> bool:
        """
        Initialize the agent on first use.
        When resuming a session, restores conversation history from SQLite.

        Returns:
            bool: True if successful, False otherwise
        """
        from cli import AIAgent, ChatConsole, _DIM, _RST, _accent_hex, _cprint, _prepare_deferred_agent_startup, logger
        if self.agent is not None:
            return True

        # Dispatcher workers carry an immutable run route. Validate the real
        # parsed CLI values before startup hooks, credentials, clients, or an
        # AIAgent can create provider-side effects.
        from hermes_cli.kanban_runtime_contract import (
            RunRouteMismatch,
            preflight_kanban_cli_route,
        )

        try:
            preflight_kanban_cli_route(
                model=model_override or self.model,
                provider=self.requested_provider,
                reasoning_config=self.reasoning_config,
                toolsets=getattr(self, "enabled_toolsets", None),
            )
        except RunRouteMismatch as exc:
            ChatConsole().print(
                f"[bold red]Kanban run route preflight failed: {_escape(str(exc))}[/]"
            )
            return False

        _prepare_deferred_agent_startup()
        self._install_tool_callbacks()
        self._ensure_tirith_security()

        if not self._ensure_runtime_credentials(defer_on_exhaustion=True):
            return False

        from hermes_cli.mcp_startup import wait_for_mcp_discovery

        wait_for_mcp_discovery()

        # Initialize SQLite session store for CLI sessions (if not already done in __init__)
        if self._session_db is None:
            try:
                from hermes_state import SessionDB
                self._session_db = SessionDB()
            except Exception as e:
                logger.warning("SQLite session store not available — session will NOT be indexed: %s", e)

        # If resuming, validate the session exists and load its history.
        # _preload_resumed_session() may have already loaded it (called from
        # run() for immediate display).  In that case, conversation_history
        # is non-empty and we skip the DB round-trip.
        if self._resumed and self._session_db and not self.conversation_history:
            session_meta = self._session_db.get_session(self.session_id)
            # In quiet mode (`hermes chat -Q` / --quiet, surfaced via
            # tool_progress_mode == "off"), resume status lines go to stderr
            # so stdout stays machine-readable for automation wrappers that
            # do `$(hermes chat -Q --resume <id> -q "...")`. Without this,
            # the resume banner pollutes captured stdout. See #11793.
            _quiet_mode = getattr(self, "tool_progress_mode", "full") == "off"
            if not session_meta:
                if _quiet_mode:
                    print(f"Session not found: {self.session_id}", file=sys.stderr)
                    print(
                        "Use a session ID from a previous CLI run (hermes sessions list).",
                        file=sys.stderr,
                    )
                else:
                    _cprint(f"\033[1;31mSession not found: {self.session_id}{_RST}")
                    _cprint(f"{_DIM}Use a session ID from a previous CLI run (hermes sessions list).{_RST}")
                return False
            # If the requested session is the (empty) head of a compression
            # chain, walk to the descendant that actually holds the messages.
            # See #15000 and SessionDB.resolve_resume_session_id.
            try:
                resolved_id = self._session_db.resolve_resume_session_id(self.session_id)
            except Exception:
                resolved_id = self.session_id
            if resolved_id and resolved_id != self.session_id:
                ChatConsole().print(
                    f"[dim]Session {_escape(self.session_id)} was compressed into "
                    f"{_escape(resolved_id)}; resuming the descendant with your "
                    f"transcript.[/dim]"
                )
                self.session_id = resolved_id
                resolved_meta = self._session_db.get_session(self.session_id)
                if resolved_meta:
                    session_meta = resolved_meta
            restored = self._session_db.get_messages_as_conversation(
                self.session_id, repair_alternation=True
            )
            if restored:
                restored = [m for m in restored if m.get("role") != "session_meta"]
                self.conversation_history = restored
                msg_count = len([m for m in restored if m.get("role") == "user"])
                title_part = ""
                if session_meta.get("title"):
                    title_part = f" \"{session_meta['title']}\""
                if _quiet_mode:
                    print(
                        f"↻ Resumed session {self.session_id}{title_part} "
                        f"({msg_count} user message{'s' if msg_count != 1 else ''}, "
                        f"{len(restored)} total messages)",
                        file=sys.stderr,
                    )
                else:
                    ChatConsole().print(
                        f"[bold {_accent_hex()}]↻ Resumed session[/] "
                        f"[bold]{_escape(self.session_id)}[/]"
                        f"[bold {_accent_hex()}]{_escape(title_part)}[/] "
                        f"({msg_count} user message{'s' if msg_count != 1 else ''}, {len(restored)} total messages)"
                    )
                self._restore_session_cwd(session_meta, quiet=_quiet_mode)
            else:
                if _quiet_mode:
                    print(
                        f"Session {self.session_id} found but has no messages. Starting fresh.",
                        file=sys.stderr,
                    )
                else:
                    ChatConsole().print(
                        f"[bold {_accent_hex()}]Session {_escape(self.session_id)} found but has no messages. Starting fresh.[/]"
                    )
            # Re-open the session (clear ended_at so it's active again)
            try:
                self._session_db._conn.execute(
                    "UPDATE sessions SET ended_at = NULL, end_reason = NULL WHERE id = ?",
                    (self.session_id,),
                )
                self._session_db._conn.commit()
            except Exception:
                pass

        try:
            runtime = runtime_override or {
                "api_key": self.api_key,
                "base_url": self.base_url,
                "provider": self.provider,
                "api_mode": self.api_mode,
                "runtime": getattr(self, "agent_runtime", "hermes"),
                "command": self.acp_command,
                "args": list(self.acp_args or []),
                "credential_pool": getattr(self, "_credential_pool", None),
                "moa_config": getattr(self, "moa_config", None),
                "max_output_tokens": getattr(
                    self, "_route_max_output_tokens", None
                ),
                "request_timeout_seconds": getattr(
                    self, "_route_request_timeout_seconds", None
                ),
                "stale_timeout_seconds": getattr(
                    self, "_route_stale_timeout_seconds", None
                ),
                "total_attempt_timeout_seconds": getattr(
                    self, "_route_total_attempt_timeout_seconds", None
                ),
                "first_event_timeout_seconds": getattr(
                    self, "_route_first_event_timeout_seconds", None
                ),
            }
            effective_model = model_override or self.model
            route_max_tokens = runtime.get("max_output_tokens")
            effective_max_tokens = self.max_tokens
            if route_max_tokens is not None:
                effective_max_tokens = (
                    min(effective_max_tokens, route_max_tokens)
                    if effective_max_tokens is not None
                    else route_max_tokens
                )
            self.agent = AIAgent(
                model=effective_model,
                api_key=runtime.get("api_key"),
                base_url=runtime.get("base_url"),
                provider=runtime.get("provider"),
                api_mode=runtime.get("api_mode"),
                runtime=runtime.get("runtime"),
                acp_command=runtime.get("command"),
                acp_args=runtime.get("args"),
                credential_pool=runtime.get("credential_pool"),
                moa_config=runtime.get("moa_config"),
                max_tokens=effective_max_tokens,
                max_iterations=self.max_turns,
                enabled_toolsets=self.enabled_toolsets,
                disabled_toolsets=self.disabled_toolsets,
                verbose_logging=self.verbose,
                quiet_mode=not self.verbose,
                tool_progress_mode=getattr(self, "tool_progress_mode", "all"),
                ephemeral_system_prompt=self.system_prompt if self.system_prompt else None,
                prefill_messages=self.prefill_messages or None,
                reasoning_config=self.reasoning_config,
                service_tier=self.service_tier,
                request_overrides=request_overrides,
                providers_allowed=self._providers_only,
                providers_ignored=self._providers_ignore,
                providers_order=self._providers_order,
                provider_sort=self._provider_sort,
                provider_require_parameters=self._provider_require_params,
                provider_data_collection=self._provider_data_collection,
                openrouter_min_coding_score=self._openrouter_min_coding_score,
                session_id=self.session_id,
                platform="cli",
                session_db=self._session_db,
                clarify_callback=self._clarify_callback,
                reasoning_callback=self._current_reasoning_callback(),

                fallback_model=self._fallback_model,
                thinking_callback=self._on_thinking,
                checkpoints_enabled=self.checkpoints_enabled,
                checkpoint_max_snapshots=self.checkpoint_max_snapshots,
                checkpoint_max_total_size_mb=self.checkpoint_max_total_size_mb,
                checkpoint_max_file_size_mb=self.checkpoint_max_file_size_mb,
                pass_session_id=self.pass_session_id,
                skip_context_files=self.ignore_rules,
                skip_memory=self.ignore_rules,
                tool_progress_callback=self._on_tool_progress,
                tool_start_callback=self._on_tool_start if self._inline_diffs_enabled else None,
                tool_complete_callback=self._on_tool_complete if self._inline_diffs_enabled else None,
                stream_delta_callback=self._stream_delta if self.streaming_enabled else None,
                tool_gen_callback=self._on_tool_gen_start if self.streaming_enabled else None,
                notice_callback=self._on_notice,
                notice_clear_callback=self._on_notice_clear,
                reaction_callback=self._on_reaction,
            )
            # Project the resolved route budgets onto the concrete agent and
            # its primary-runtime snapshot. These limits are control-plane
            # policy, not provider credentials, and must survive turn restores.
            for field, runtime_key in (
                ("_route_request_timeout_seconds", "request_timeout_seconds"),
                ("_route_stale_timeout_seconds", "stale_timeout_seconds"),
                (
                    "_route_total_attempt_timeout_seconds",
                    "total_attempt_timeout_seconds",
                ),
                ("_route_first_event_timeout_seconds", "first_event_timeout_seconds"),
            ):
                value = runtime.get(runtime_key)
                setattr(self.agent, field, value)
                primary_key = field.removeprefix("_")
                if isinstance(getattr(self.agent, "_primary_runtime", None), dict):
                    self.agent._primary_runtime[primary_key] = value
            # The requested route was checked before construction; now attest
            # the route the fully initialized agent will actually use. This is
            # synchronous and fail-closed for contracted Kanban workers.
            try:
                from hermes_cli.kanban_runtime_contract import (
                    attach_kanban_runtime_observer,
                )

                attach_kanban_runtime_observer(self.agent)
            except Exception:
                try:
                    self.agent.close()
                except Exception:
                    pass
                self.agent = None
                raise
            # Store reference for atexit memory provider shutdown.
            # NOTE: this MUST write to the ``cli`` module's global, not a
            # local module global. ``_run_cleanup`` (in cli.py) reads
            # ``cli._active_agent_ref`` to decide whether to fire the memory
            # provider's ``on_session_end`` hook. When this code lived in
            # cli.py a bare ``global _active_agent_ref`` worked; after the
            # god-file extraction into this mixin a ``global`` here would bind
            # *this module's* namespace, leaving ``cli._active_agent_ref`` None
            # forever — so memory shutdown never ran on /exit (#49287).
            import cli as _cli
            _cli._active_agent_ref = self.agent
            # Route agent status output through prompt_toolkit so ANSI escape
            # sequences aren't garbled by patch_stdout's StdoutProxy (#2262).
            self.agent._print_fn = _cprint
            # Hydrate credits notices at session OPEN (parity with the TUI), so a
            # depletion / usage-band warning shows before the first message. The
            # notice_callback is bound above → _on_notice renders the line. Idempotent
            # + fail-open inside the helper; harmless for non-Nous providers.
            try:
                from agent.credits_tracker import seed_credits_at_session_start

                seed_credits_at_session_start(self.agent)
            except Exception:
                pass
            self._active_agent_route_signature = (
                effective_model,
                runtime.get("provider"),
                runtime.get("base_url"),
                runtime.get("api_mode"),
                runtime.get("runtime"),
                runtime.get("command"),
                tuple(runtime.get("args") or ()),
                repr(runtime.get("moa_config")),
            )

            # Force-create DB row on /title intent, then apply title.
            if self._pending_title and self._session_db and self.agent:
                try:
                    self.agent._ensure_db_session()
                    if self.agent._session_db_created:
                        self._session_db.set_session_title(self.session_id, self._pending_title)
                        _cprint(f"  Session title applied: {self._pending_title}")
                        self._pending_title = None
                    # else: row creation failed transiently — keep _pending_title for retry
                except (ValueError, Exception) as e:
                    _cprint(f"  Could not apply pending title: {e}")
                    # Keep _pending_title so it can be retried after row creation succeeds
            return True
        except Exception as e:
            ChatConsole().print(f"[bold red]Failed to initialize agent: {e}[/]")
            return False

    def _preload_resumed_session(self) -> bool:
        """Load a resumed session's history from the DB early (before first chat).

        Called from run() so the conversation history is available for display
        before the user sends their first message.  Sets
        ``self.conversation_history`` and prints the one-liner status.  Returns
        True if history was loaded, False otherwise.

        The corresponding block in ``_init_agent()`` checks whether history is
        already populated and skips the DB round-trip.
        """
        from cli import _accent_hex
        if not self._resumed or not self._session_db:
            return False

        session_meta = self._session_db.get_session(self.session_id)
        if not session_meta:
            self._console_print(
                f"[bold red]Session not found: {self.session_id}[/]"
            )
            self._console_print(
                "[dim]Use a session ID from a previous CLI run "
                "(hermes sessions list).[/]"
            )
            return False

        # If the requested session is the (empty) head of a compression chain,
        # walk to the descendant that actually holds the messages. See #15000.
        try:
            resolved_id = self._session_db.resolve_resume_session_id(self.session_id)
        except Exception:
            resolved_id = self.session_id
        if resolved_id and resolved_id != self.session_id:
            self._console_print(
                f"[dim]Session {self.session_id} was compressed into "
                f"{resolved_id}; resuming the descendant with your transcript.[/]"
            )
            self.session_id = resolved_id
            resolved_meta = self._session_db.get_session(self.session_id)
            if resolved_meta:
                session_meta = resolved_meta

        restored = self._session_db.get_messages_as_conversation(
            self.session_id, repair_alternation=True
        )
        if restored:
            restored = [m for m in restored if m.get("role") != "session_meta"]
            self.conversation_history = restored
            msg_count = len([m for m in restored if m.get("role") == "user"])
            title_part = ""
            if session_meta.get("title"):
                title_part = f' "{session_meta["title"]}"'
            accent_color = _accent_hex()
            self._console_print(
                f"[{accent_color}]↻ Resumed session [bold]{self.session_id}[/bold]"
                f"{title_part} "
                f"({msg_count} user message{'s' if msg_count != 1 else ''}, "
                f"{len(restored)} total messages)[/]"
            )
            self._restore_session_cwd(session_meta)
        else:
            accent_color = _accent_hex()
            self._console_print(
                f"[{accent_color}]Session {self.session_id} found but has no "
                f"messages. Starting fresh.[/]"
            )
            return False

        # Re-open the session (clear ended_at so it's active again)
        try:
            self._session_db._conn.execute(
                "UPDATE sessions SET ended_at = NULL, end_reason = NULL "
                "WHERE id = ?",
                (self.session_id,),
            )
            self._session_db._conn.commit()
        except Exception:
            pass

        return True

    def _display_resumed_history(self):
        """Render a compact recap of previous conversation messages.

        Uses Rich markup with dim/muted styling so the recap is visually
        distinct from the active conversation.  Caps the display at the
        last ``MAX_DISPLAY_EXCHANGES`` user/assistant exchanges and shows
        an indicator for earlier hidden messages.
        """
        from cli import CLI_CONFIG, _record_output_history_entry, _strip_reasoning_tags, _suspend_output_history
        from tools.ansi_strip import sanitize_display_text as _sanitize_display_text
        if not self.conversation_history:
            return

        # Check config: resume_display setting
        if self.resume_display == "minimal":
            return

        # Read limits from config (with hardcoded defaults)
        _disp = CLI_CONFIG.get("display", {})
        MAX_DISPLAY_EXCHANGES = int(_disp.get("resume_exchanges", 10))
        MAX_USER_LEN = int(_disp.get("resume_max_user_chars", 300))
        MAX_ASST_LEN = int(_disp.get("resume_max_assistant_chars", 200))
        MAX_ASST_LINES = int(_disp.get("resume_max_assistant_lines", 3))
        SKIP_TOOL_ONLY = _disp.get("resume_skip_tool_only", True)

        # Collect displayable entries (skip system, tool-result messages)
        entries = []  # list of (role, display_text)
        _last_asst_idx = None       # index of last assistant entry
        _last_asst_full = None      # un-truncated display text for last assistant
        for msg in self.conversation_history:
            role = msg.get("role", "")
            content = msg.get("content")
            tool_calls = msg.get("tool_calls") or []

            if role == "system":
                continue
            if role == "tool":
                continue

            if role == "user":
                text = "" if content is None else str(content)
                # Handle multimodal content (list of dicts)
                if isinstance(content, list):
                    parts = []
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            parts.append(part.get("text", ""))
                        elif isinstance(part, dict) and part.get("type") == "image_url":
                            parts.append("[image]")
                    text = " ".join(parts)
                # Stored history is untrusted for display: strip escape
                # sequences/control chars so replaying a message can't
                # clear the screen, retitle the window, or restyle the
                # recap panel (see tools/ansi_strip.sanitize_display_text).
                text = _sanitize_display_text(text)
                if len(text) > MAX_USER_LEN:
                    text = text[:MAX_USER_LEN] + "..."
                entries.append(("user", text))

            elif role == "assistant":
                text = "" if content is None else str(content)
                text = _sanitize_display_text(_strip_reasoning_tags(text))
                parts = []
                full_parts = []  # un-truncated version
                if text:
                    full_parts.append(text)
                    lines = text.splitlines()
                    if len(lines) > MAX_ASST_LINES:
                        text = "\n".join(lines[:MAX_ASST_LINES]) + " ..."
                    if len(text) > MAX_ASST_LEN:
                        text = text[:MAX_ASST_LEN] + "..."
                    parts.append(text)
                if tool_calls:
                    tc_count = len(tool_calls)
                    # Extract tool names
                    names = []
                    for tc in tool_calls:
                        fn = tc.get("function", {})
                        name = fn.get("name", "unknown") if isinstance(fn, dict) else "unknown"
                        if name not in names:
                            names.append(name)
                    names_str = ", ".join(names[:4])
                    if len(names) > 4:
                        names_str += ", ..."
                    noun = "call" if tc_count == 1 else "calls"
                    tc_summary = f"[{tc_count} tool {noun}: {names_str}]"
                    parts.append(tc_summary)
                    full_parts.append(tc_summary)
                if not parts:
                    # Skip pure-reasoning messages that have no visible output
                    continue
                # Skip tool-call-only entries when SKIP_TOOL_ONLY is enabled
                has_text = bool(text)
                if SKIP_TOOL_ONLY and not has_text and tool_calls:
                    continue
                entries.append(("assistant", " ".join(parts)))
                _last_asst_idx = len(entries) - 1
                _last_asst_full = " ".join(full_parts)

        if not entries:
            return

        # Determine if we need to truncate
        skipped = 0
        if len(entries) > MAX_DISPLAY_EXCHANGES * 2:
            skipped = len(entries) - MAX_DISPLAY_EXCHANGES * 2
            entries = entries[skipped:]

        # Replace last assistant entry with full (un-truncated) text
        # so the user can see where they left off without wasting tokens.
        if _last_asst_idx is not None and _last_asst_full:
            adj_idx = _last_asst_idx - skipped
            if 0 <= adj_idx < len(entries):
                entries[adj_idx] = ("assistant_last", _last_asst_full)

        # Build the display using Rich
        from rich.panel import Panel
        from rich.text import Text

        try:
            from hermes_cli.skin_engine import get_active_skin
            _skin = get_active_skin()
            _history_text_c = _skin.get_color("banner_text", "#FFF8DC")
            _session_label_c = _skin.get_color("session_label", "#DAA520")
            _session_border_c = _skin.get_color("session_border", "#8B8682")
            _assistant_label_c = _skin.get_color("ui_ok", "#8FBC8F")
        except Exception:
            _history_text_c = "#FFF8DC"
            _session_label_c = "#DAA520"
            _session_border_c = "#8B8682"
            _assistant_label_c = "#8FBC8F"

        lines = Text()
        if skipped:
            lines.append(
                f"  ... {skipped} earlier messages ...\n\n",
                style="dim italic",
            )

        for i, (role, text) in enumerate(entries):
            if role == "user":
                lines.append("  ● You: ", style=f"dim bold {_session_label_c}")
                # Show first line inline, indent rest
                msg_lines = text.splitlines()
                lines.append(msg_lines[0] + "\n", style="dim")
                for ml in msg_lines[1:]:
                    lines.append(f"         {ml}\n", style="dim")
            elif role == "assistant_last":
                # Last assistant response shown in full, non-dim
                lines.append("  ◆ Hermes: ", style=f"bold {_assistant_label_c}")
                msg_lines = text.splitlines()
                lines.append(msg_lines[0] + "\n", style="")
                for ml in msg_lines[1:]:
                    lines.append(f"            {ml}\n", style="")
            else:
                lines.append("  ◆ Hermes: ", style=f"dim bold {_assistant_label_c}")
                msg_lines = text.splitlines()
                lines.append(msg_lines[0] + "\n", style="dim")
                for ml in msg_lines[1:]:
                    lines.append(f"            {ml}\n", style="dim")
            if i < len(entries) - 1:
                lines.append("")  # small gap

        panel = Panel(
            lines,
            title=f"[dim {_session_label_c}]Previous Conversation[/]",
            border_style=f"dim {_session_border_c}",
            padding=(0, 1),
            style=_history_text_c,
        )
        _record_output_history_entry(lambda: self._render_resume_history_panel_lines(panel))
        with _suspend_output_history():
            self._console_print(panel)
