"""Provider-neutral runtime identity for primary and fallback targets.

``api_mode`` describes an HTTP/wire protocol. ``runtime`` describes who owns
the agent loop. Keeping them separate lets a target use Hermes' native loop,
Codex app-server, or the Claude Agent SDK without overloading provider names.
"""

from __future__ import annotations

from typing import Any, Mapping


HERMES_RUNTIME = "hermes"
CODEX_APP_SERVER_RUNTIME = "codex_app_server"
CLAUDE_AGENT_SDK_RUNTIME = "claude_agent_sdk"

VALID_AGENT_RUNTIMES = frozenset(
    {HERMES_RUNTIME, CODEX_APP_SERVER_RUNTIME, CLAUDE_AGENT_SDK_RUNTIME}
)

CLAUDE_MAX_ONLY_POLICY = "max_only"
CLAUDE_ROUTE_POLICY_ERROR = (
    "Claude routes are restricted to the first-party Claude Max login via "
    "claude_agent_sdk (provider=anthropic, runtime=claude_agent_sdk, no base_url); "
    "alternate Claude credential paths are disabled"
)


def claude_auth_policy() -> str:
    """Return the profile-scoped Claude credential policy."""

    try:
        import yaml

        from hermes_constants import get_hermes_home

        config_path = get_hermes_home() / "config.yaml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        security = config.get("security", {}) if isinstance(config, dict) else {}
        if isinstance(security, dict):
            return str(security.get("claude_auth_policy") or "any").strip().lower()
    except Exception:
        pass
    return "any"


def validate_claude_runtime_target(
    *,
    provider: str,
    model: str,
    runtime: str,
    base_url: str | None = None,
    policy: str | None = None,
) -> None:
    """Fail closed when a managed Claude route can bypass Claude Max auth."""

    effective_policy = (policy or claude_auth_policy()).strip().lower()
    if effective_policy != CLAUDE_MAX_ONLY_POLICY:
        return
    provider_name = (provider or "").strip().lower()
    runtime_name = (runtime or "").strip().lower()
    model_name = (model or "").strip().lower().split("/")[-1]
    is_claude = "claude" in model_name
    is_governed_route = (
        is_claude
        or provider_name == "anthropic"
        or runtime_name == CLAUDE_AGENT_SDK_RUNTIME
    )
    if not is_governed_route:
        return
    if (
        provider_name != "anthropic"
        or runtime_name != CLAUDE_AGENT_SDK_RUNTIME
        or bool((base_url or "").strip())
    ):
        raise ValueError(CLAUDE_ROUTE_POLICY_ERROR)


def attach_runtime_identity(
    resolved: Mapping[str, Any],
    *,
    route_config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a copy of a resolved provider target with ``runtime`` attached."""

    target = dict(resolved)
    target["runtime"] = resolve_runtime_identity(
        provider=str(target.get("provider") or ""),
        api_mode=str(target.get("api_mode") or ""),
        route_config=route_config,
    )
    validate_claude_runtime_target(
        provider=str(target.get("provider") or ""),
        model=str(target.get("model") or ""),
        runtime=str(target.get("runtime") or ""),
        base_url=str(target.get("base_url") or ""),
    )
    return target


def resolve_runtime_identity(
    *,
    provider: str,
    api_mode: str,
    route_config: Mapping[str, Any] | None = None,
) -> str:
    """Return the agent-loop runtime for a resolved route.

    An explicit ``runtime`` is authoritative. Unknown explicit values fail
    closed rather than silently selecting a different execution boundary.
    """

    config = route_config or {}
    configured = str(config.get("runtime") or "").strip().lower()
    if configured in VALID_AGENT_RUNTIMES:
        return configured
    if configured:
        raise ValueError(f"Unknown agent runtime: {configured}")
    if api_mode == CODEX_APP_SERVER_RUNTIME:
        return CODEX_APP_SERVER_RUNTIME
    openai_runtime = str(config.get("openai_runtime") or "").strip().lower()
    if (
        provider.strip().lower() in {"openai", "openai-codex"}
        and openai_runtime == CODEX_APP_SERVER_RUNTIME
    ):
        return CODEX_APP_SERVER_RUNTIME
    return HERMES_RUNTIME


__all__ = [
    "CLAUDE_MAX_ONLY_POLICY",
    "CLAUDE_ROUTE_POLICY_ERROR",
    "CLAUDE_AGENT_SDK_RUNTIME",
    "CODEX_APP_SERVER_RUNTIME",
    "HERMES_RUNTIME",
    "VALID_AGENT_RUNTIMES",
    "attach_runtime_identity",
    "claude_auth_policy",
    "resolve_runtime_identity",
    "validate_claude_runtime_target",
]
