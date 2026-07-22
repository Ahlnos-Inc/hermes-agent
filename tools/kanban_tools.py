"""Kanban tools — structured tool-call surface for worker + orchestrator agents.

These tools are registered into the model's schema when the agent is
running under the dispatcher (env var ``HERMES_KANBAN_TASK`` set) or when
the active profile explicitly enables the ``kanban`` toolset for
orchestrator work. A normal ``hermes chat`` session still sees **zero**
kanban tools in its schema unless configured.

Why tools instead of just shelling out to ``hermes kanban``?

1. **Backend portability.** A worker whose terminal tool points at Docker
   / Modal / Singularity / SSH would run ``hermes kanban complete …``
   inside the container, where ``hermes`` isn't installed and the DB
   isn't mounted. Tools run in the agent's Python process, so they
   always reach ``~/.hermes/kanban.db`` regardless of terminal backend.

2. **No shell-quoting footguns.** Passing ``--metadata '{"x": [...]}'``
   through shlex+argparse is fragile. Structured tool args skip it.

3. **Better errors.** Tool-call failures return structured JSON the
   model can reason about, not stderr strings it has to parse.

Humans continue to use the CLI (``hermes kanban …``), the dashboard
(``hermes dashboard``), and the slash command (``/kanban …``) — all
three bypass the agent entirely. The tools are for dispatcher-spawned
worker handoffs and for configured orchestrator profiles that route work
through the board.
"""
from __future__ import annotations

import json
import hashlib
import logging
import os
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from agent.redact import redact_sensitive_text
from hermes_constants import get_hermes_home
from hermes_cli.goals import judge_goal
from tools.registry import registry, tool_error
from hermes_cli.config import cfg_get, load_config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------

KANBAN_LIST_DEFAULT_LIMIT = 50
KANBAN_LIST_MAX_LIMIT = 200


def _profile_has_kanban_toolset() -> bool:
    # Uses load_config() which has mtime-based caching, so this adds
    # negligible overhead. The check_fn results are further TTL-cached
    # (~30s) by the tool registry.
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        toolsets = cfg.get("toolsets", [])
        return "kanban" in toolsets
    except Exception:
        return False


def _check_kanban_mode() -> bool:
    """Task-lifecycle tools are available when:

    1. ``HERMES_KANBAN_TASK`` is set (dispatcher-spawned worker), OR
    2. The current profile has ``kanban`` in its toolsets config
       (orchestrator profiles like techlead that route work via Kanban).

    Humans running ``hermes chat`` without the kanban toolset see zero
    kanban tools. Workers spawned by the kanban dispatcher (gateway-
    embedded by default) and orchestrator profiles with the kanban
    toolset enabled see the Kanban lifecycle tool surface.
    """
    if os.environ.get("HERMES_KANBAN_TASK"):
        return True
    return _profile_has_kanban_toolset()


def _check_kanban_orchestrator_mode() -> bool:
    """Board-routing tools (kanban_list, kanban_unblock) are intentionally
    hidden from task workers.

    Dispatcher-spawned workers should close their own task via the
    lifecycle tools (complete/block/heartbeat), not enumerate or unblock
    board state. Profiles that explicitly opt into the kanban toolset
    and are NOT scoped to a single task are the orchestrator surface.
    """
    if os.environ.get("HERMES_KANBAN_TASK"):
        return False
    return _profile_has_kanban_toolset()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _default_task_id(arg: Optional[str]) -> Optional[str]:
    """Resolve ``task_id`` arg or fall back to the env var the dispatcher set."""
    if arg:
        return arg
    env_tid = os.environ.get("HERMES_KANBAN_TASK")
    return env_tid or None


def _worker_run_id(task_id: str) -> Optional[int]:
    """Return this worker's dispatcher run id when it is scoped to task_id."""
    if os.environ.get("HERMES_KANBAN_TASK") != task_id:
        return None
    raw = os.environ.get("HERMES_KANBAN_RUN_ID")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _auto_subscribe_gateway_source(
    kb,
    conn,
    task_id: str,
    gateway_source: Optional[dict] = None,
    *,
    session_subscribed: bool = False,
) -> dict[str, Any]:
    """Create a kanban notify subscription when gateway source context is set.

    ``agent/agent_runtime_helpers.py`` passes the originating gateway chat as
    ``gateway_source`` when a tool call runs inside a gateway session. When
    all required values are present, this helper calls ``kb.add_notify_sub``.
    It is idempotent — harmless double-call.

    For compatibility with older call paths and tests, falls back to the
    ``HERMES_KANBAN_SUB_*`` environment variables when ``gateway_source`` is
    not provided. When ``kanban.auto_subscribe_home_on_create`` is enabled,
    tool-created tasks without a gateway source subscribe configured gateway
    home channels instead, so orchestrator-created durable work still closes
    the loop.
    """
    if gateway_source is not None:
        platform = gateway_source.get("platform")
        chat_id = gateway_source.get("chat_id")
        thread_id = gateway_source.get("thread_id") or None
        user_id = gateway_source.get("user_id") or None
        source = "gateway_source"
    else:
        platform = os.environ.get("HERMES_KANBAN_SUB_PLATFORM")
        chat_id = os.environ.get("HERMES_KANBAN_SUB_CHAT_ID")
        thread_id = os.environ.get("HERMES_KANBAN_SUB_THREAD_ID") or None
        user_id = os.environ.get("HERMES_KANBAN_SUB_USER_ID") or None
        source = "gateway_env"
    if not platform or not chat_id:
        if gateway_source is not None:
            return {
                "subscribed": False,
                "count": 0,
                "source": source,
                "reason": "missing_gateway_source_fields",
            }
        if session_subscribed:
            return {
                "subscribed": False,
                "count": 0,
                "source": "session",
                "reason": "session_subscription_exists",
            }
        return _auto_subscribe_home_channels(kb, conn, task_id)
    notifier_profile = os.environ.get("HERMES_PROFILE") or None
    try:
        kb.add_notify_sub(
            conn,
            task_id=task_id,
            platform=platform,
            chat_id=chat_id,
            thread_id=thread_id,
            user_id=user_id,
            notifier_profile=notifier_profile,
        )
        return {
            "subscribed": True,
            "count": 1,
            "source": source,
            "channels": [str(platform).lower()],
        }
    except Exception:
        # Best-effort only — never fail a kanban_create because of
        # a subscription glitch.
        logger.debug(
            "auto-subscribe skipped for %s: add_notify_sub raised", task_id,
            exc_info=True,
        )
        return {
            "subscribed": False,
            "count": 0,
            "source": source,
            "reason": "add_notify_sub_failed",
        }


def _auto_subscribe_home_channels(kb, conn, task_id: str) -> dict[str, Any]:
    """Subscribe configured home channels for non-gateway tool-created tasks.

    This is opt-in because CLI/batch callers may intentionally manage their
    own subscriptions. It is useful for front-door orchestrator sessions that
    create durable Kanban work from a local TUI/CLI session where there is no
    originating messaging chat to auto-subscribe.
    """
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
    except Exception:
        cfg = {}
    kanban_cfg = cfg.get("kanban") if isinstance(cfg, dict) else {}
    if not isinstance(kanban_cfg, dict):
        kanban_cfg = {}
    if not bool(kanban_cfg.get("auto_subscribe_home_on_create")):
        return {
            "subscribed": False,
            "count": 0,
            "source": "none",
            "reason": "no_gateway_source",
        }

    homes = _configured_home_channels()
    allowed = _parse_platform_filter(
        kanban_cfg.get("auto_subscribe_home_platforms", "*")
    )
    selected = [
        h for h in homes
        if "*" in allowed or str(h.get("platform", "")).lower() in allowed
    ]
    if not selected:
        return {
            "subscribed": False,
            "count": 0,
            "source": "home_channel",
            "reason": "no_configured_home_channels",
        }

    notifier_profile = os.environ.get("HERMES_PROFILE") or None
    subscribed: list[str] = []
    for home in selected:
        platform = str(home.get("platform") or "").lower()
        chat_id = str(home.get("chat_id") or "")
        if not platform or not chat_id:
            continue
        try:
            kb.add_notify_sub(
                conn,
                task_id=task_id,
                platform=platform,
                chat_id=chat_id,
                thread_id=(home.get("thread_id") or None),
                notifier_profile=notifier_profile,
            )
            subscribed.append(platform)
        except Exception:
            logger.debug(
                "home-channel auto-subscribe skipped for %s on %s",
                task_id,
                platform,
                exc_info=True,
            )
    return {
        "subscribed": bool(subscribed),
        "count": len(subscribed),
        "source": "home_channel",
        "channels": subscribed,
        "reason": None if subscribed else "add_notify_sub_failed",
    }


def _configured_home_channels() -> list[dict[str, Any]]:
    try:
        from gateway.config import load_gateway_config

        gw_cfg = load_gateway_config()
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for platform, pcfg in getattr(gw_cfg, "platforms", {}).items():
        home = getattr(pcfg, "home_channel", None) if pcfg else None
        if not home:
            continue
        platform_value = getattr(platform, "value", str(platform)).lower()
        out.append({
            "platform": platform_value,
            "chat_id": getattr(home, "chat_id", "") or "",
            "thread_id": getattr(home, "thread_id", "") or "",
        })
    return out


def _parse_platform_filter(raw: Any) -> set[str]:
    if raw in (None, "", True):
        return {"*"}
    if isinstance(raw, str):
        return {p.strip().lower() for p in raw.split(",") if p.strip()} or {"*"}
    if isinstance(raw, (list, tuple, set)):
        return {str(p).strip().lower() for p in raw if str(p).strip()} or {"*"}
    return {"*"}


def _stamp_worker_session_metadata(
    task_id: str, metadata: Optional[dict]
) -> Optional[dict]:
    """Add trusted worker session id metadata for this worker's own task."""
    if os.environ.get("HERMES_KANBAN_TASK") != task_id:
        return metadata
    session_id = os.environ.get("HERMES_SESSION_ID")
    if not session_id:
        return metadata
    stamped = dict(metadata or {})
    stamped["worker_session_id"] = session_id
    return stamped


def _enforce_worker_task_ownership(tid: str) -> Optional[str]:
    """Reject worker-driven destructive calls on foreign task IDs.

    A process spawned by the dispatcher has ``HERMES_KANBAN_TASK`` set
    to its own task id. Tools like ``kanban_complete`` / ``kanban_block``
    / ``kanban_heartbeat`` mutate run-lifecycle state, so a buggy or
    prompt-injected worker that passed an explicit ``task_id`` for some
    other task could corrupt sibling or cross-tenant runs (see #19534).

    Orchestrator profiles (kanban toolset enabled but **no**
    ``HERMES_KANBAN_TASK`` in env) aren't subject to this check — their
    job is routing, and they sometimes legitimately close out child
    tasks or reopen blocked ones. Workers are narrowly scoped to their
    one task.

    Returns ``None`` when the call is allowed, or a tool-error string
    when it must be rejected. Callers should ``return`` the error
    verbatim.
    """
    env_tid = os.environ.get("HERMES_KANBAN_TASK")
    if not env_tid:
        # Orchestrator or CLI context — no task-scope restriction.
        return None
    if tid != env_tid:
        return tool_error(
            f"worker is scoped to task {env_tid}; refusing to mutate "
            f"{tid}. Use kanban_comment to hand off information to other "
            f"tasks, or kanban_create to spawn follow-up work."
        )
    return None


def _connect(board: Optional[str] = None):
    """Import + connect lazily so the module imports cleanly in non-kanban
    contexts (e.g. test rigs that import every tool module).

    When ``board`` is provided it's forwarded to :func:`kb.connect`, which
    routes the connection to that board's sqlite file. ``None`` (the
    default) preserves the legacy resolution chain
    (``HERMES_KANBAN_DB`` → ``HERMES_KANBAN_BOARD`` env → current symlink
    → ``default``). Per-tool ``board`` lets a Telegram-side agent override
    the env-pinned active board without restarting Hermes.
    """
    from hermes_cli import kanban_db as kb
    return kb, kb.connect(board=board)


_GOAL_MODE_BLOCK_ALLOWED_KINDS = frozenset({"dependency", "needs_input"})


def _goal_judge_available() -> bool:
    """True when an auxiliary client is configured for the goal judge.

    ``judge_goal`` is fail-open at the source: when no auxiliary model can
    be reached it returns a ``"continue"`` verdict that is indistinguishable
    from a real "not done yet" judgment. The completion gate must not treat
    that as a rejection, or an unconfigured/degraded auxiliary model would
    wedge every ``goal_mode`` worker (it could never close its own task).

    So we probe availability first and only enforce the gate when a judge is
    actually reachable. This mirrors the same client lookup ``judge_goal``
    performs internally.
    """
    try:
        from agent.auxiliary_client import get_text_auxiliary_client
        client, model = get_text_auxiliary_client("goal_judge")
    except Exception:
        return False
    return client is not None and bool(model)


# ---------------------------------------------------------------------------
# Runtime-activity → board-heartbeat bridge (#31752)
# ---------------------------------------------------------------------------
# When the agent ticks ``_touch_activity`` during normal work (between
# tool calls, mid-stream chunks, etc.), we want the kanban board's
# ``last_heartbeat_at`` columns to reflect that liveness so the dispatcher
# watchdog (which reads ``tasks.last_heartbeat_at``, not the agent's
# in-process timestamp) doesn't reclaim an actively-running worker as
# stale. The model is not required to call the explicit ``kanban_heartbeat``
# tool for this to work — that tool stays available for workers that want
# to attach a note or pre-emptively extend a claim across a known-long op.
#
# Constraints:
#   - Best-effort: never raise. The agent loop must not care if the bridge
#     fails (board missing, DB locked, etc.).
#   - Rate-limited to one DB write per 60s per task/run/activity kind; a
#     transport tick must never suppress the first semantic or durable signal.
#   - No-op outside dispatcher-spawned worker context (no ``HERMES_KANBAN_TASK``).
#   - No durable note on these auto-heartbeats; that's reserved for the
#     explicit tool which carries a model-supplied note.

_AUTO_HEARTBEAT_MIN_INTERVAL_SECONDS = 60.0
_auto_heartbeat_last_attempt: dict[tuple[str, str, str], float] | float = {}


def heartbeat_current_worker_from_env(*, activity_kind: str = "semantic") -> bool:
    """Best-effort: extend the kanban claim + bump board heartbeat for the
    current dispatcher-spawned worker, using identity from env vars.

    Returns True only after the typed heartbeat was persisted; False if the
    call was skipped, rejected as stale, or failed. The boolean is
    informational — agent-loop callers should not branch on it.

    Identity comes from:
      * ``HERMES_KANBAN_TASK`` — task id (required; absence means no-op)
      * ``HERMES_KANBAN_RUN_ID`` — pins the run row so we don't heartbeat
        a stale run that may have already been reclaimed
      * ``HERMES_KANBAN_CLAIM_LOCK`` — claim lock for ``heartbeat_claim``;
        falls back to the default ``_claimer_id()`` for locally-driven
        workers that never went through the dispatcher path

    Rate-limited by task, run, and activity kind. The clock advances only
    after a successful DB write so a transient lock/error is retryable.
    """
    global _auto_heartbeat_last_attempt
    tid = os.environ.get("HERMES_KANBAN_TASK")
    if not tid:
        return False
    import time as _time
    now = _time.monotonic()
    run_id_raw = os.environ.get("HERMES_KANBAN_RUN_ID") or ""
    rate_key = (tid, run_id_raw, activity_kind)
    if not isinstance(_auto_heartbeat_last_attempt, dict):
        # Compatibility with callers/tests that reset the old scalar latch.
        _auto_heartbeat_last_attempt = {}
    last_attempt = _auto_heartbeat_last_attempt.get(rate_key, 0.0)
    if (now - last_attempt) < _AUTO_HEARTBEAT_MIN_INTERVAL_SECONDS:
        return False
    try:
        kb, conn = _connect()
        try:
            claim_lock = os.environ.get("HERMES_KANBAN_CLAIM_LOCK")
            try:
                kb.heartbeat_claim(conn, tid, claimer=claim_lock)
            except Exception:
                logger.debug("auto-heartbeat: heartbeat_claim failed", exc_info=True)
            run_id: Optional[int]
            try:
                run_id = int(run_id_raw) if run_id_raw else None
            except (TypeError, ValueError):
                run_id = None
            try:
                persisted = kb.heartbeat_worker(
                    conn,
                    tid,
                    note=None,
                    expected_run_id=run_id,
                    activity_kind=activity_kind,
                )
            except Exception:
                logger.debug("auto-heartbeat: heartbeat_worker failed", exc_info=True)
                persisted = False
        finally:
            try:
                conn.close()
            except Exception:
                pass
        if not persisted:
            return False
        _auto_heartbeat_last_attempt[rate_key] = now
        return True
    except Exception:
        logger.debug("auto-heartbeat: bridge failed", exc_info=True)
        return False


def _ok(**fields: Any) -> str:
    return json.dumps({"ok": True, **fields})


class KanbanTerminalAction(str, Enum):
    """Worker lifecycle transitions that end the current agent run."""

    COMPLETE = "complete"
    BLOCK = "block"
    REWORK = "rework"
    PUBLICATION = "publication"


class KanbanTerminalControl(str):
    """Successful worker finalizer result with out-of-band control semantics.

    This is deliberately a ``str`` subclass: existing registry, middleware,
    observer, callback, and transcript paths continue to receive the exact JSON
    tool result they received before.  The native tool executor can meanwhile
    distinguish a committed worker-lifecycle transition without parsing JSON or
    guessing from a tool name.
    """

    action: KanbanTerminalAction

    def __new__(
        cls,
        content: str,
        *,
        action: KanbanTerminalAction,
    ) -> "KanbanTerminalControl":
        result = super().__new__(cls, content)
        result.action = action
        return result

    @property
    def final_response(self) -> str:
        if self.action is KanbanTerminalAction.COMPLETE:
            return "Kanban task completed."
        if self.action is KanbanTerminalAction.REWORK:
            return "Kanban rework requested; review task parked until the fix is delivered."
        if self.action is KanbanTerminalAction.PUBLICATION:
            return "Kanban publication handoff requested; task parked until the remote ref is verified."
        return "Kanban task blocked."

    @property
    def tool_name(self) -> str:
        if self.action is KanbanTerminalAction.COMPLETE:
            return "kanban_complete"
        if self.action is KanbanTerminalAction.REWORK:
            return "kanban_request_rework"
        if self.action is KanbanTerminalAction.PUBLICATION:
            return "kanban_request_publication"
        return "kanban_block"


def _worker_terminal_ok(
    action: KanbanTerminalAction,
    *,
    task_id: str,
    **fields: Any,
) -> str:
    """Return typed terminal control only to the worker that owns ``task_id``.

    Orchestrators may use the same tools to operate on another card.  Their
    conversation must continue, so those successful calls retain the ordinary
    string result.
    """
    content = _ok(task_id=task_id, **fields)
    if os.environ.get("HERMES_KANBAN_TASK") == task_id:
        return KanbanTerminalControl(content, action=action)
    return content


def _append_kanban_route_telemetry(
    event_type: str,
    *,
    status: Optional[str] = None,
    **fields: Any,
) -> None:
    """Best-effort metadata-only route telemetry for Kanban routing."""
    try:
        from orchestration_telemetry import append_event

        append_event(event_type, surface="kanban_create", status=status, **fields)
    except Exception:
        logger.debug("kanban route telemetry failed", exc_info=True)


def _append_kanban_complete_telemetry(
    event_type: str,
    *,
    status: Optional[str] = None,
    **fields: Any,
) -> None:
    """Best-effort metadata-only completion telemetry for Kanban workers."""
    try:
        from orchestration_telemetry import append_event

        append_event(event_type, surface="kanban_complete", status=status, **fields)
    except Exception:
        logger.debug("kanban complete telemetry failed", exc_info=True)


_SENSITIVE_DATA_MARKERS = {
    "business",
    "customer",
    "credential",
    "credential-adjacent",
    "finance",
    "financial",
    "legal",
    "pii",
    "private",
    "security",
    "supplier",
}
_PUBLIC_OR_FREE_PROVIDERS = {"nous", "openrouter", "copilot"}
_PREMIUM_QUOTA_PRESSURE_PCT = 85.0
_DEFAULT_ROUTING_PRESET = "front_door"
_UNCERTAINTY_ESCALATION_PRESET = "uncertainty_escalation"


def _builtin_model_routing_table() -> dict[str, Any]:
    """Return fail-safe Kanban model-routing lanes.

    A generated profile-local routing table is preferred when present. The
    built-in table keeps model_routing deterministic on fresh profiles while
    preserving the critical privacy default to GPT-5.5 without conflating
    ordinary front-door control-plane work with xhigh hard-reasoning lanes.
    """
    front = {
        "provider": "openai-codex",
        "model": "gpt-5.5",
        "reasoning_effort": "high",
        "route_key": "openai-codex/gpt-5.5",
        "quota_mode": "always_premium",
    }
    hard = {**front, "reasoning_effort": "xhigh"}
    cheap = {
        "provider": "nous",
        "model": "deepseek/deepseek-v4-flash:free",
        "reasoning_effort": "low",
        "route_key": "nous/deepseek/deepseek-v4-flash:free",
        "data_classes_allowed": ["public", "trivial"],
    }
    pro = {
        "provider": "deepseek",
        "model": "deepseek-v4-pro",
        "reasoning_effort": "xhigh",
        "route_key": "deepseek/deepseek-v4-pro",
        "quota_mode": "budget_ok",
        "budget_route": dict(cheap),
        "fallback": [_UNCERTAINTY_ESCALATION_PRESET],
    }
    lanes = {
        _DEFAULT_ROUTING_PRESET: dict(front),
        # Backwards-compatible alias: older prompts/tools may still name this
        # lane, but it must behave like the front door (high), not like an
        # implicit xhigh uncertainty escalation.
        "front_door_or_uncertain": dict(front),
        _UNCERTAINTY_ESCALATION_PRESET: dict(hard),
        "architecture_design": dict(hard),
        "sensitive_business": dict(hard),
        "synthesis_recommendation": dict(hard),
        "complex_debugging": dict(hard),
        "security_review": dict(hard),
        "bounded_low_risk_delegate": dict(cheap),
        "verification_leaf": {**cheap, "min_scores": {"coding": 0.60, "data_scan": 0.50}, "fallback": [_DEFAULT_ROUTING_PRESET]},
        "public_research": {**cheap, "reasoning_effort": "minimal", "fallback": [_DEFAULT_ROUTING_PRESET]},
        "file_authoring_bounded": {**cheap, "fallback": [_DEFAULT_ROUTING_PRESET]},
        "simple_config_patch": {**cheap, "fallback": [_DEFAULT_ROUTING_PRESET]},
        "repo_publish": {
            **front,
            "reasoning_effort": "low",
            "fallback": [_DEFAULT_ROUTING_PRESET],
            "data_classes_allowed": ["public", "trivial", "business", "credential-adjacent"],
        },
        "repo_verification": {
            **front,
            "reasoning_effort": "low",
            "fallback": [_DEFAULT_ROUTING_PRESET],
            "data_classes_allowed": ["business", "credential-adjacent"],
        },
        "code_review_quality": {**pro, "reasoning_effort": "medium"},
        "code_implementation": pro,
        "vault_curation": {**pro, "reasoning_effort": "low"},
    }
    return {
        "version": 1,
        "policy": {
            "uncertainty_bias": "over_resource",
            "front_door_route": front,
            "default_uncertain_route": hard,
            "privacy_rule": "sensitive work must not route to public/free providers",
        },
        "task_lanes": lanes,
        "models": [
            {
                **front,
                "benchmark_profile": {"reasoning": 0.90, "coding": 0.85, "data_scan": 0.80, "writing": 0.85},
                "benchmark_metadata": {"composite_score": 0.90, "confidence": "fallback"},
            },
            {
                **cheap,
                "benchmark_profile": {"reasoning": 0.65, "coding": 0.68, "data_scan": 0.58, "writing": 0.80},
                "benchmark_metadata": {"composite_score": 0.70, "confidence": "fallback"},
            },
            {
                **pro,
                "benchmark_profile": {"reasoning": 0.75, "coding": 0.80, "data_scan": 0.68, "writing": 0.76},
                "benchmark_metadata": {"composite_score": 0.78, "confidence": "fallback"},
            },
        ],
    }


_MODEL_ROUTING_ALIASES = {
    "architecture": "architecture_design",
    "architecture/design": "architecture_design",
    "config_patch": "simple_config_patch",
    "debugging_complex": "complex_debugging",
    "file_authoring": "file_authoring_bounded",
    "frontdoor": "front_door",
    "front_door_or_uncertain": "front_door",
    "implementation": "code_implementation",
    "implementation_routine": "code_implementation",
    "research_public": "public_research",
    "review_security": "security_review",
    "synthesis": "synthesis_recommendation",
    "uncertain": "uncertainty_escalation",
    "uncertainty": "uncertainty_escalation",
    "verification": "verification_leaf",
}


def _hermes_home_path() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()


def _routing_table_path() -> Path:
    override = os.environ.get("HERMES_MODEL_ROUTING_TABLE")
    if override:
        return Path(override).expanduser()
    return _hermes_home_path() / "state" / "model-routing-table.json"


def _route_key(route: dict[str, Any]) -> str:
    provider = str(route.get("provider") or "").strip()
    model = str(route.get("model") or "").strip()
    if route.get("route_key"):
        return str(route["route_key"])
    return f"{provider}/{model}" if provider and model else model


def _load_model_routing_table() -> tuple[dict[str, Any], str]:
    table = _builtin_model_routing_table()
    path = _routing_table_path()
    source = "builtin"
    try:
        if path.exists():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                source = path.name
                table["version"] = loaded.get("version", table.get("version"))
                table["updated_at"] = loaded.get("updated_at")
                if isinstance(loaded.get("policy"), dict):
                    table["policy"] = {**table.get("policy", {}), **loaded["policy"]}
                if isinstance(loaded.get("task_lanes"), dict):
                    table["task_lanes"] = {**table.get("task_lanes", {}), **loaded["task_lanes"]}
                loaded_models = loaded.get("models") if isinstance(loaded.get("models"), list) else []
                by_key = {
                    m.get("route_key") or _route_key(m): m
                    for m in table.get("models", [])
                    if isinstance(m, dict)
                }
                for model in loaded_models:
                    if isinstance(model, dict):
                        by_key[model.get("route_key") or _route_key(model)] = model
                table["models"] = list(by_key.values())
    except Exception:
        logger.debug("failed to load model routing table; using built-in fallback", exc_info=True)
        source = "builtin-load-error"
    table["_source"] = source
    return table, source


def _model_for_route(table: dict[str, Any], route: dict[str, Any]) -> Optional[dict[str, Any]]:
    key = _route_key(route)
    for model in table.get("models", []):
        if not isinstance(model, dict):
            continue
        if (model.get("route_key") or _route_key(model)) == key:
            return model
    return None


def _normalize_routing_preset(value: Any, table: dict[str, Any]) -> str:
    text = str(value or "").strip()
    if not text:
        return _DEFAULT_ROUTING_PRESET
    key = text.lower().replace(" ", "_").replace("-", "_")
    key = _MODEL_ROUTING_ALIASES.get(key, key)
    lanes = table.get("task_lanes") if isinstance(table.get("task_lanes"), dict) else {}
    if key in lanes:
        return key
    if key in {"verification_leaf", "public_research", "file_authoring_bounded", "simple_config_patch"} and "bounded_low_risk_delegate" in lanes:
        return "bounded_low_risk_delegate"
    return _DEFAULT_ROUTING_PRESET


def _lane_route(table: dict[str, Any], preset: str) -> dict[str, Any]:
    lanes = table.get("task_lanes") if isinstance(table.get("task_lanes"), dict) else {}
    lane = lanes.get(preset)
    if not isinstance(lane, dict):
        lane = lanes.get(_DEFAULT_ROUTING_PRESET)
    if not isinstance(lane, dict):
        lane = table.get("policy", {}).get("default_uncertain_route")
    return dict(lane or _builtin_model_routing_table()["task_lanes"][_DEFAULT_ROUTING_PRESET])


def _is_sensitive_data(value: Any) -> bool:
    text = str(value or "").casefold()
    if not text:
        return False
    return any(marker in text for marker in _SENSITIVE_DATA_MARKERS)


def _is_public_or_free_route(route: dict[str, Any], model_meta: Optional[dict[str, Any]] = None) -> bool:
    provider = str(route.get("provider") or "").casefold()
    model = str(route.get("model") or "").casefold()
    route_key = str(route.get("route_key") or "").casefold()
    cost_class = str((model_meta or {}).get("cost_class") or route.get("cost_class") or "").casefold()
    allowed = (model_meta or route).get("data_classes_allowed")
    public_only = isinstance(allowed, list) and allowed and {str(x).casefold() for x in allowed}.issubset({"public", "trivial"})
    return (
        provider in _PUBLIC_OR_FREE_PROVIDERS
        or ":free" in model
        or ":free" in route_key
        or "free" in cost_class
        or "public" in cost_class
        or public_only
    )


def _extract_percentage(value: Any) -> Optional[float]:
    try:
        pct = float(value)
    except (TypeError, ValueError):
        return None
    if pct <= 1.0:
        pct *= 100.0
    return max(0.0, min(100.0, round(pct, 2)))


def _parse_quota_timestamp(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _quota_timestamp_is_expired(value: Any, now: datetime) -> bool:
    parsed = _parse_quota_timestamp(value)
    return parsed is not None and parsed <= now


def _native_quota_state_dirs() -> list[Path]:
    dirs: list[Path] = []
    raw = os.environ.get("HERMES_NATIVE_QUOTA_STATE_DIR")
    if raw:
        dirs.extend(Path(part).expanduser() for part in raw.split(os.pathsep) if part.strip())
    dirs.append(_hermes_home_path() / "state")

    deduped: list[Path] = []
    seen: set[str] = set()
    for root in dirs:
        try:
            key = str(root.resolve())
        except Exception:
            key = str(root)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(root)
    return deduped


def _native_quota_provider_slug(provider: Any = None, model: Any = None) -> str:
    provider_text = str(provider or "").strip().casefold()
    model_text = str(model or "").strip().casefold()
    if provider_text:
        return provider_text.replace("/", "-")
    if model_text.startswith("openai-codex/") or model_text.startswith("gpt-"):
        return "openai-codex"
    return "openai-codex"


def _matching_native_quota_files(provider_slug: str) -> list[Path]:
    candidates: list[Path] = []
    seen: set[str] = set()
    for root in _native_quota_state_dirs():
        try:
            if not root.is_dir():
                continue
            files = root.glob(f"provider-native-quota-{provider_slug}*.json")
        except Exception:
            logger.debug("failed to inspect native quota state dir %s", root, exc_info=True)
            continue
        for path in files:
            try:
                key = str(path.resolve())
            except Exception:
                key = str(path)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(path)
    return candidates


def _native_provider_matches(parsed: dict[str, Any], provider_slug: str) -> bool:
    for key in ("provider", "native_provider"):
        value = parsed.get(key)
        if value:
            return str(value).strip().casefold().replace("/", "-") == provider_slug
    return True


def _quota_pct_from_window(window: Any) -> Optional[float]:
    if not isinstance(window, dict):
        return None
    for key in ("usage_pct", "used_pct", "used_percentage", "usage_percentage", "percent_used"):
        if key in window:
            pct = _extract_percentage(window.get(key))
            if pct is not None:
                return pct
    return None


def _parse_native_quota_snapshot(path: Path, provider_slug: str, now: datetime) -> Optional[dict[str, Any]]:
    parsed = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        return None
    if not _native_provider_matches(parsed, provider_slug):
        return None

    best: Optional[dict[str, Any]] = None

    def consider_windows(windows: Any) -> None:
        nonlocal best
        if not isinstance(windows, dict):
            return
        for name, window in windows.items():
            if not isinstance(window, dict):
                continue
            resets_at = window.get("resets_at")
            if _quota_timestamp_is_expired(resets_at, now):
                continue
            pct = _quota_pct_from_window(window)
            if pct is None:
                continue
            found = {
                "usage_pct": pct,
                "source": path.name,
                "provider": parsed.get("provider") or provider_slug,
                **({"native_provider": parsed.get("native_provider")} if parsed.get("native_provider") else {}),
                "window": str(name),
                **({"resets_at": resets_at} if resets_at else {}),
                **({"fetched_at": parsed.get("fetched_at")} if parsed.get("fetched_at") else {}),
            }
            if best is None or found["usage_pct"] > best["usage_pct"]:
                best = found

    consider_windows(parsed.get("windows"))
    additional = parsed.get("additional_limits")
    if isinstance(additional, list):
        for limit in additional:
            if isinstance(limit, dict):
                consider_windows(limit.get("windows"))
    return best


def _native_quota_sort_key(path: Path) -> float:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(parsed, dict):
            fetched = _parse_quota_timestamp(parsed.get("fetched_at"))
            if fetched is not None:
                return fetched.timestamp()
    except Exception:
        logger.debug("failed to parse native quota fetched_at %s", path, exc_info=True)
    try:
        return path.stat().st_mtime
    except Exception:
        return 0.0


def _parse_legacy_quota_usage(path: Path, now: datetime) -> Optional[dict[str, Any]]:
    parsed = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        return None
    resets_at = parsed.get("resets_at")
    if _quota_timestamp_is_expired(resets_at, now):
        try:
            path.unlink(missing_ok=True)
        except Exception:
            logger.debug("failed to remove expired legacy quota state %s", path, exc_info=True)
        return None
    pct = _quota_pct_from_window(parsed)
    if pct is None:
        return None
    return {
        "usage_pct": pct,
        "source": path.name,
        **({"resets_at": resets_at} if resets_at else {}),
        **({"fetched_at": parsed.get("fetched_at")} if parsed.get("fetched_at") else {}),
    }


def _estimate_quota_usage(provider: Any = None, model: Any = None) -> Optional[dict[str, Any]]:
    """Return a best-effort premium quota usage estimate.

    Provider-native snapshots are authoritative when usable. Legacy
    ``quota-usage.json`` is kept only as a compatibility fallback so stale
    legacy state cannot override a refreshed provider-native quota snapshot.
    """
    now = datetime.now(timezone.utc)
    provider_slug = _native_quota_provider_slug(provider, model)

    native_candidates = sorted(_matching_native_quota_files(provider_slug), key=_native_quota_sort_key, reverse=True)
    for path in native_candidates:
        try:
            found = _parse_native_quota_snapshot(path, provider_slug, now)
            if found:
                return found
        except Exception:
            logger.debug("failed to read native quota estimate %s", path, exc_info=True)

    fallback = _hermes_home_path() / "state" / "quota-usage.json"
    try:
        if fallback.exists():
            return _parse_legacy_quota_usage(fallback, now)
    except Exception:
        logger.debug("failed to read quota estimate %s", fallback, exc_info=True)
    return None


def _benchmark_metadata(table: dict[str, Any], route: dict[str, Any]) -> dict[str, Any]:
    model_meta = _model_for_route(table, route) or {}
    bm = model_meta.get("benchmark_metadata") if isinstance(model_meta.get("benchmark_metadata"), dict) else {}
    return {
        "benchmark_source": table.get("_source", "unknown"),
        "benchmark_composite": bm.get("composite_score", route.get("score")),
        "benchmark_confidence": bm.get("confidence", route.get("confidence")),
    }


def _benchmark_gate_failure(table: dict[str, Any], route: dict[str, Any], lane: dict[str, Any]) -> Optional[str]:
    min_scores = lane.get("min_scores")
    if not isinstance(min_scores, dict) or not min_scores:
        return None
    model_meta = _model_for_route(table, route)
    profile = model_meta.get("benchmark_profile") if isinstance(model_meta, dict) else None
    if not isinstance(profile, dict):
        return "benchmark_failed:missing_profile"
    for dim, required in min_scores.items():
        try:
            required_f = float(required)
            actual = profile.get(dim)
            actual_f = float(actual) if actual is not None else None
        except (TypeError, ValueError):
            continue
        if actual_f is None or actual_f < required_f:
            return f"benchmark_failed:{dim}<{required_f:.2f}"
    return None


def _find_same_effort_route(
    table: dict[str, Any], lane: dict[str, Any], current_preset: str, args: dict[str, Any]
) -> Optional[dict[str, Any]]:
    """Try to find a same-effort alternative lane that passes the benchmark gate.

    When a lane fails its benchmark gate, prefer a same-effort alternative
    over escalating to a higher-effort fallback. This prevents unnecessary
    xhigh escalations when a same-effort lane exists that clears the gate.

    Candidates must use the same provider/model as the current lane
    to avoid accidentally switching to a semantically different lane
    (e.g., architecture_design → security_review).
    """
    current_effort = str(lane.get("reasoning_effort") or "").strip()
    if not current_effort:
        return None
    current_provider = str(lane.get("provider") or "").strip()
    current_model = str(lane.get("model") or "").strip()
    lanes = table.get("task_lanes") if isinstance(table.get("task_lanes"), dict) else {}
    data_sensitive = _is_sensitive_data(args.get("data_sensitivity"))

    for name, candidate in lanes.items():
        if name == current_preset:
            continue
        if not isinstance(candidate, dict):
            continue
        candidate_effort = str(candidate.get("reasoning_effort") or "").strip()
        if candidate_effort != current_effort:
            continue
        # Only consider lanes that use the same provider and model
        # to avoid accidentally switching semantic domains
        if str(candidate.get("provider") or "").strip() != current_provider:
            continue
        if str(candidate.get("model") or "").strip() != current_model:
            continue
        # Don't switch to a lane that would itself fail the benchmark gate
        if _benchmark_gate_failure(table, dict(candidate), candidate):
            continue
        # Privacy check: don't route sensitive data to public/free lanes
        if data_sensitive:
            model_meta = _model_for_route(table, dict(candidate))
            if _is_public_or_free_route(dict(candidate), model_meta):
                continue
        return dict(candidate)

    return None


def _fallback_route(table: dict[str, Any], lane: dict[str, Any]) -> dict[str, Any]:
    fallbacks = lane.get("fallback")
    if isinstance(fallbacks, str):
        fallbacks = [fallbacks]
    if isinstance(fallbacks, list):
        for item in fallbacks:
            preset = _normalize_routing_preset(item, table)
            route = _lane_route(table, preset)
            if route:
                return route
    return _lane_route(table, _DEFAULT_ROUTING_PRESET)


def _resolve_model_routing(args: dict[str, Any], explicit_model_override: Any) -> tuple[Optional[str], Optional[dict[str, Any]]]:
    explicit = str(explicit_model_override).strip() if explicit_model_override is not None else ""
    requested = args.get("model_routing")
    if explicit and not requested:
        return explicit, None
    if not requested:
        return explicit or None, None

    table, source = _load_model_routing_table()
    requested_text = str(requested).strip()
    preset = _normalize_routing_preset(requested_text, table)
    lane = _lane_route(table, preset)
    route = dict(lane)
    fallback_applied = False
    fallback_reason: Optional[str] = None
    privacy_escalated = False
    quota_gate_triggered = False
    quota_usage_pct: Optional[float] = None

    if explicit:
        route["model"] = explicit
        route.setdefault("route_key", explicit)
        fallback_reason = "explicit_model_override"
    else:
        model_meta = _model_for_route(table, route)
        if _is_sensitive_data(args.get("data_sensitivity")) and _is_public_or_free_route(route, model_meta):
            route = _lane_route(table, _DEFAULT_ROUTING_PRESET)
            privacy_escalated = True
            fallback_applied = True
            fallback_reason = "privacy_sensitive_data"
        else:
            gate_failure = _benchmark_gate_failure(table, route, lane)
            if gate_failure:
                # Try same-effort alternatives before escalating
                same_effort = _find_same_effort_route(table, lane, preset, args)
                if same_effort:
                    route = same_effort
                    fallback_reason = f"{gate_failure}→same_effort"
                else:
                    route = _fallback_route(table, lane)
                    fallback_reason = gate_failure
                fallback_applied = True

            quota = _estimate_quota_usage()
            if quota:
                quota_usage_pct = quota.get("usage_pct")
            quota_policy = str(args.get("quota_policy") or "").casefold()
            verifiability = str(args.get("verifiability") or "").casefold()
            data_sensitive = _is_sensitive_data(args.get("data_sensitivity"))
            if (
                not fallback_applied
                and quota_usage_pct is not None
                and quota_usage_pct >= _PREMIUM_QUOTA_PRESSURE_PCT
                and str(lane.get("quota_mode") or "").casefold() == "budget_ok"
                and isinstance(lane.get("budget_route"), dict)
                and quota_policy in {"downgrade_verifiable", "auto", "budget", "preserve_premium"}
                and verifiability in {"high", "deterministic", "tests", "readback", "verified"}
                and not data_sensitive
            ):
                route = dict(lane["budget_route"])
                quota_gate_triggered = True
                fallback_reason = "quota_budget_route"

    model_override = str(route.get("model") or "").strip() or None
    meta = _benchmark_metadata(table, route)
    decision = {
        "preset": requested_text or preset,
        "resolved_preset": preset,
        "provider": route.get("provider"),
        "model": route.get("model"),
        "reasoning_effort": route.get("reasoning_effort"),
        "route_key": route.get("route_key") or _route_key(route),
        "model_override": model_override,
        "fallback_applied": fallback_applied,
        "fallback_reason": fallback_reason,
        "privacy_escalated": privacy_escalated,
        "quota_gate_triggered": quota_gate_triggered,
        "quota_usage_pct": quota_usage_pct,
        "benchmark_source": source,
        **meta,
    }
    return model_override, {k: v for k, v in decision.items() if v is not None}


def _legacy_model_used_from_env() -> Optional[dict[str, str]]:
    provider = os.environ.get("HERMES_PROVIDER") or os.environ.get("HERMES_MODEL_PROVIDER")
    model = os.environ.get("HERMES_MODEL")
    effort = os.environ.get("HERMES_REASONING_EFFORT")
    result = {
        k: v.strip()
        for k, v in {
            "provider": provider,
            "model": model,
            "reasoning_effort": effort,
        }.items()
        if isinstance(v, str) and v.strip()
    }
    return result or None


def _current_model_used(
    kb: Any = None,
    conn: Any = None,
    task_id: Optional[str] = None,
) -> Optional[dict[str, str]]:
    """Return observed runtime truth, with env only for legacy runs."""
    if kb is not None and conn is not None and task_id:
        task = kb.get_task(conn, task_id)
        run_id = task.current_run_id if task else None
        if run_id is not None:
            observed = kb.latest_runtime_observation(conn, run_id)
            if observed is not None:
                result = {
                    key: str(observed[key]).strip()
                    for key in ("provider", "model", "reasoning_effort")
                    if isinstance(observed.get(key), str)
                    and str(observed[key]).strip()
                }
                return result or None
            if kb.get_run_spec(
                conn, run_id, task_id=task_id, require_current=True,
            ) is not None:
                if os.environ.get("HERMES_KANBAN_TASK") == task_id:
                    raise ValueError(
                        f"contracted run {run_id} has no runtime observation"
                    )
                # An operator may close a run externally. There is no active
                # runtime to attest, so leave model_used absent rather than
                # borrowing the operator process's unrelated environment.
                return None
    return _legacy_model_used_from_env()


def _normalize_profile(value: Any) -> Optional[str]:
    """Normalize CLI-compatible assignee sentinels for the tool surface."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"none", "-", "null"}:
        return None
    return text


def _parse_bool_arg(args: dict, name: str, *, default: bool = False):
    value = args.get(name)
    if value is None:
        return default, None
    if isinstance(value, bool):
        return value, None
    text = str(value).strip().lower()
    if text in {"true", "1", "yes"}:
        return True, None
    if text in {"false", "0", "no"}:
        return False, None
    return default, f"{name} must be a boolean or 'true'/'false'"


def _require_orchestrator_tool(tool_name: str) -> Optional[str]:
    """Belt-and-suspenders runtime guard for orchestrator-only handlers.

    The check_fn (`_check_kanban_orchestrator_mode`) keeps these tools
    out of the worker schema entirely, but in case a stale registration
    or test harness routes a worker to one of them anyway, return a
    structured tool_error so the model gets a clear refusal instead of
    silently mutating board state from a worker context.
    """
    if os.environ.get("HERMES_KANBAN_TASK"):
        return tool_error(
            f"{tool_name} is orchestrator-only; dispatcher-spawned workers "
            "must use kanban_complete, kanban_block, kanban_heartbeat, or "
            "kanban_comment for their assigned task."
        )
    return None


def _task_summary_dict(kb, conn, task) -> dict[str, Any]:
    """Compact task shape for board-listing tools."""
    parents = kb.parent_ids(conn, task.id)
    children = kb.child_ids(conn, task.id)
    return {
        "id": task.id,
        "title": task.title,
        "assignee": task.assignee,
        "status": task.status,
        "priority": task.priority,
        "tenant": task.tenant,
        "workspace_kind": task.workspace_kind,
        "workspace_path": task.workspace_path,
        "publication_expected_sha": task.publication_expected_sha,
        "publication_remote": task.publication_remote,
        "publication_ref": task.publication_ref,
        "is_publication": task.is_publication,
        "project_id": task.project_id,
        "created_by": task.created_by,
        "created_at": task.created_at,
        "started_at": task.started_at,
        "completed_at": task.completed_at,
        "current_run_id": task.current_run_id,
        "model_override": task.model_override,
        "parents": parents,
        "children": children,
        "parent_count": len(parents),
        "child_count": len(children),
    }


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _handle_show(args: dict, **kw) -> str:
    """Read bounded task state; ``detail=full`` is the diagnostic escape."""
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    detail = str(args.get("detail") or "compact").strip().lower()
    if detail not in {"compact", "full"}:
        return tool_error("detail must be 'compact' or 'full'")
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            task = kb.get_task(conn, tid)
            if task is None:
                return tool_error(f"task {tid} not found")
            parents = kb.parent_ids(conn, tid)
            children = kb.child_ids(conn, tid)

            def _task_dict(t):
                return {
                    "id": t.id, "title": t.title, "body": t.body,
                    "assignee": t.assignee, "status": t.status,
                    "tenant": t.tenant, "priority": t.priority,
                    "workspace_kind": t.workspace_kind,
                    "workspace_path": t.workspace_path,
                    "publication_expected_sha": t.publication_expected_sha,
                    "publication_remote": t.publication_remote,
                    "publication_ref": t.publication_ref,
                    "is_publication": t.is_publication,
                    "created_by": t.created_by, "created_at": t.created_at,
                    "started_at": t.started_at,
                    "completed_at": t.completed_at,
                    "result": t.result,
                    "current_run_id": t.current_run_id,
                    "model_override": t.model_override,
                }

            def _run_dict(r):
                return {
                    "id": r.id, "profile": r.profile,
                    "status": r.status, "outcome": r.outcome,
                    "summary": r.summary, "error": r.error,
                    "metadata": r.metadata,
                    "started_at": r.started_at, "ended_at": r.ended_at,
                }

            def _compact_text(value: Any, limit: int) -> Optional[str]:
                if value is None:
                    return None
                text = str(value)
                if len(text) <= limit:
                    return text
                return text[:limit] + f"… [{len(text) - limit} chars omitted]"

            def _compact_run_dict(r):
                if r is None:
                    return None
                compact = {
                    "id": r.id,
                    "profile": r.profile,
                    "status": r.status,
                    "outcome": r.outcome,
                    "summary": _compact_text(r.summary, 1024),
                    "error": _compact_text(r.error, 512),
                    "started_at": r.started_at,
                    "ended_at": r.ended_at,
                }
                if isinstance(r.metadata, dict):
                    compact["metadata_keys"] = sorted(str(key) for key in r.metadata)[:32]
                    if isinstance(r.metadata.get("model_used"), dict):
                        compact["model_used"] = {
                            key: _compact_text(r.metadata["model_used"].get(key), 256)
                            for key in ("provider", "model", "reasoning_effort")
                            if r.metadata["model_used"].get(key) is not None
                        }
                return compact

            def _bounded_event_payload(value: Any) -> Any:
                try:
                    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True)
                except (TypeError, ValueError):
                    return None
                raw = serialized.encode("utf-8")
                if len(raw) <= 2048:
                    return value
                preview = raw[:1800].decode("utf-8", errors="ignore")
                return {
                    "truncated": True,
                    "original_bytes": len(raw),
                    "preview": preview,
                }

            if detail == "full":
                comments = kb.list_comments(conn, tid)
                events = kb.list_events(conn, tid)
                runs = kb.list_runs(conn, tid)
                return json.dumps({
                    "task": _task_dict(task),
                    "parents": parents,
                    "children": children,
                    "comments": [
                        {"author": c.author, "body": c.body,
                         "created_at": c.created_at}
                        for c in comments
                    ],
                    "events": [
                        {"kind": e.kind, "payload": e.payload,
                         "created_at": e.created_at, "run_id": e.run_id}
                        for e in events[-50:]   # cap; full log via CLI
                    ],
                    "runs": [_run_dict(r) for r in runs],
                    "worker_context": kb.build_worker_context(conn, tid),
                    "continuation": kb.continuation_status(conn, tid),
                    "detail": "full",
                })

            compact_task = {
                "id": task.id,
                "title": task.title,
                "assignee": task.assignee,
                "status": task.status,
                "current_run_id": task.current_run_id,
            }
            if os.environ.get("HERMES_KANBAN_TASK"):
                # A worker already receives the complete bounded handoff in
                # worker_context. Repeating raw body/comments/runs/events here
                # creates the context-growth loop this compact view prevents.
                return json.dumps({
                    "task": compact_task,
                    "worker_context": kb.build_worker_context(conn, tid),
                    "continuation": kb.continuation_status(conn, tid),
                    "detail": "compact",
                })

            unfinished_parents = 0
            for parent_id in parents:
                parent = kb.get_task(conn, parent_id)
                if parent is not None and parent.status not in {"done", "archived"}:
                    unfinished_parents += 1
            event_row = conn.execute(
                "SELECT kind, payload, created_at, run_id FROM task_events "
                "WHERE task_id = ? AND kind != 'heartbeat' "
                "ORDER BY id DESC LIMIT 1",
                (tid,),
            ).fetchone()
            latest_event = None
            if event_row is not None:
                try:
                    event_payload = (
                        json.loads(event_row["payload"])
                        if event_row["payload"] else None
                    )
                except (TypeError, ValueError):
                    event_payload = None
                latest_event = {
                    "kind": event_row["kind"],
                    "payload": _bounded_event_payload(event_payload),
                    "created_at": event_row["created_at"],
                    "run_id": event_row["run_id"],
                }
            latest_run = kb.latest_run(conn, tid)
            return json.dumps({
                "task": compact_task,
                "current_step_key": task.current_step_key,
                "dependencies": {
                    "parent_count": len(parents),
                    "child_count": len(children),
                    "unfinished_parent_count": unfinished_parents,
                },
                "latest_run": _compact_run_dict(latest_run),
                "latest_event": latest_event,
                "continuation": kb.continuation_status(conn, tid),
                "detail": "compact",
            })

        finally:
            conn.close()
    except ValueError as e:
        # Invalid board slug surfaces as ValueError from _normalize_board_slug.
        return tool_error(f"kanban_show: {e}")
    except Exception as e:
        logger.exception("kanban_show failed")
        return tool_error(f"kanban_show: {e}")


def _handle_list(args: dict, **kw) -> str:
    """List task summaries with the same core filters as the CLI."""
    guard = _require_orchestrator_tool("kanban_list")
    if guard:
        return guard
    assignee = args.get("assignee")
    status = args.get("status")
    tenant = args.get("tenant")
    workflow_key = args.get("workflow_key")
    include_archived, bool_error = _parse_bool_arg(args, "include_archived")
    if bool_error:
        return tool_error(bool_error)
    limit = args.get("limit")
    if limit is None:
        limit = KANBAN_LIST_DEFAULT_LIMIT
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return tool_error("limit must be an integer")
    if limit < 1:
        return tool_error("limit must be >= 1")
    if limit > KANBAN_LIST_MAX_LIMIT:
        return tool_error(f"limit must be <= {KANBAN_LIST_MAX_LIMIT}")
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            # Match CLI list: dependencies that cleared since the last
            # dispatcher tick should be visible to orchestrators immediately.
            promoted = kb.recompute_ready(conn)
            # Fetch one extra row so model-facing output can report that
            # a bounded listing was truncated without dumping the board.
            rows = kb.list_tasks(
                conn,
                assignee=assignee,
                status=status,
                tenant=tenant,
                workflow_key=workflow_key,
                include_archived=include_archived,
                limit=limit + 1,
            )
            truncated = len(rows) > limit
            tasks = rows[:limit]
            return json.dumps({
                "tasks": [_task_summary_dict(kb, conn, t) for t in tasks],
                "count": len(tasks),
                "limit": limit,
                "truncated": truncated,
                "next_limit": (
                    min(limit * 2, KANBAN_LIST_MAX_LIMIT)
                    if truncated and limit < KANBAN_LIST_MAX_LIMIT else None
                ),
                "promoted": promoted,
            })
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_list: {e}")
    except Exception as e:
        logger.exception("kanban_list failed")
        return tool_error(f"kanban_list: {e}")


def _handle_complete(args: dict, **kw) -> str:
    """Mark the current task done with a structured handoff."""
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    ownership_err = _enforce_worker_task_ownership(tid)
    if ownership_err:
        return ownership_err
    summary = args.get("summary")
    metadata = args.get("metadata")
    result = args.get("result")
    if summary:
        summary = redact_sensitive_text(str(summary), force=True)
    if result:
        result = redact_sensitive_text(str(result), force=True)
    if metadata is not None and isinstance(metadata, dict):
        meta_json = json.dumps(metadata)
        meta_json = redact_sensitive_text(meta_json, force=True)
        try:
            metadata = json.loads(meta_json)
        except json.JSONDecodeError:
            pass
    created_cards = args.get("created_cards")
    artifacts = args.get("artifacts")
    if created_cards is not None:
        if isinstance(created_cards, str):
            # Accept a single id as a string for convenience.
            created_cards = [created_cards]
        if not isinstance(created_cards, (list, tuple)):
            return tool_error(
                f"created_cards must be a list of task ids, got "
                f"{type(created_cards).__name__}"
            )
        # Normalise: strings only, stripped, non-empty.
        created_cards = [
            str(c).strip() for c in created_cards if str(c).strip()
        ]
    if artifacts is not None:
        if isinstance(artifacts, str):
            # Accept a single path as a string for convenience.
            artifacts = [artifacts]
        if not isinstance(artifacts, (list, tuple)):
            return tool_error(
                f"artifacts must be a list of file paths, got "
                f"{type(artifacts).__name__}"
            )
        artifacts = [
            str(p).strip() for p in artifacts if str(p).strip()
        ]
        # Carry the artifact list inside metadata so it rides the
        # existing completed-event payload without a schema change at
        # the DB layer.  The gateway notifier reads payload['artifacts']
        # off the completion event and uploads each path as a native
        # attachment.
        if artifacts:
            if metadata is None:
                metadata = {}
            elif not isinstance(metadata, dict):
                return tool_error(
                    f"metadata must be an object/dict, got "
                    f"{type(metadata).__name__}"
                )
            # Don't overwrite an existing metadata.artifacts the worker
            # passed manually — merge instead.
            existing = metadata.get("artifacts")
            if isinstance(existing, (list, tuple)):
                merged: list[str] = []
                seen: set[str] = set()
                for item in list(existing) + artifacts:
                    s = str(item).strip()
                    if s and s not in seen:
                        seen.add(s)
                        merged.append(s)
                metadata["artifacts"] = merged
            else:
                metadata["artifacts"] = artifacts
    if not (summary or result):
        return tool_error(
            "provide at least one of: summary (preferred), result"
        )
    if metadata is not None and not isinstance(metadata, dict):
        return tool_error(
            f"metadata must be an object/dict, got {type(metadata).__name__}"
        )
    model_used = None
    metadata = _stamp_worker_session_metadata(tid, metadata)
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            # Goal-mode pre-completion judge gate (Issue #38367).
            # Prevent workers from bypassing the auxiliary judge by
            # calling kanban_complete before acceptance criteria are met.
            # Only enforce when a judge is actually reachable — see
            # _goal_judge_available for why an unavailable judge fails open.
            task = kb.get_task(conn, tid)
            if task and task.goal_mode and _goal_judge_available():
                verdict = "done"
                reason = ""
                try:
                    verdict, reason, _ = judge_goal(
                        goal=f"{task.title}\n\n{task.body or ''}".strip(),
                        last_response=(summary or result or "").strip(),
                    )
                except Exception as judge_exc:
                    # Defensive: judge_goal swallows its own errors, but if
                    # it ever raises, fail open rather than wedge the worker.
                    logger.warning(
                        "goal judge check failed, allowing completion: %s",
                        judge_exc,
                        exc_info=True,
                    )
                if verdict != "done":
                    return tool_error(
                        f"Goal completion rejected by judge: {reason}. "
                        f"To proceed, either: (1) provide explicit acceptance "
                        f"evidence in your summary matching the task's criteria, "
                        f"or (2) create continuation tasks with parents=[{tid}] "
                        f"and keep this task alive."
                    )

            model_used = _current_model_used(kb, conn, tid)
            if model_used:
                metadata = dict(metadata or {})
                metadata["model_used"] = model_used

            try:
                ok = kb.complete_task(
                    conn, tid,
                    result=result, summary=summary, metadata=metadata,
                    created_cards=created_cards,
                    expected_run_id=_worker_run_id(tid),
                )
            except kb.ArtifactPreservationError as artifact_err:
                return tool_error(
                    f"kanban_complete could not preserve the declared artifacts: "
                    f"{artifact_err}. Your task is still in-flight and its "
                    f"scratch workspace was kept. Fix the artifact path or "
                    f"storage error, then retry kanban_complete with the same handoff."
                )
            except kb.HallucinatedCardsError as hall_err:
                # Structured rejection — surface the phantom ids so the
                # worker can retry with a corrected list or drop the
                # field. Audit event already landed in the DB.
                #
                # The task itself was NOT mutated (the gate runs before
                # the write txn), so the worker can simply call
                # kanban_complete again. Spell that out — without it the
                # model often interprets a tool_error as a terminal
                # failure and either blocks or crashes the run instead
                # of retrying. See #22923.
                return tool_error(
                    f"kanban_complete blocked: the following created_cards "
                    f"do not exist or were not created by this worker: "
                    f"{', '.join(hall_err.phantom)}. "
                    f"Your task is still in-flight (no state change). "
                    f"Retry kanban_complete with the same summary/metadata "
                    f"and either drop these ids from created_cards, or pass "
                    f"created_cards=[] to skip the card-claim check entirely."
                )
            if not ok:
                blockers = kb.open_critical_continuation_blockers(conn, tid)
                if blockers:
                    rendered = ", ".join(
                        f"B{item.id}/{item.severity}: {item.title}"
                        for item in blockers
                    )
                    return tool_error(
                        f"kanban_complete blocked by unresolved critical "
                        f"findings: {rendered}. Resolve every finding with "
                        f"kanban_comment resolve_blocker_id plus "
                        f"resolution_evidence_ref, then retry completion."
                    )
                blocked_event = conn.execute(
                    "SELECT payload FROM task_events WHERE task_id = ? "
                    "AND kind = 'completion_blocked' ORDER BY id DESC LIMIT 1",
                    (tid,),
                ).fetchone()
                if blocked_event is not None:
                    try:
                        blocked_payload = json.loads(blocked_event["payload"] or "{}")
                    except (TypeError, ValueError, json.JSONDecodeError):
                        blocked_payload = {}
                    if blocked_payload.get("reason") == "publication_ref_not_verified":
                        return tool_error(
                            "kanban_complete blocked: publication target ref was "
                            "not verified at the expected SHA. The task is still "
                            "in-flight; publish the exact commit, read the remote "
                            "ref back, then retry."
                        )
                return tool_error(
                    f"could not complete {tid} (unknown id or already terminal)"
                )
            run = kb.latest_run(conn, tid)
            _append_kanban_complete_telemetry(
                "route.completed",
                status="completed",
                route={
                    "chosen_route": "kanban_complete",
                    "provider": (model_used or {}).get("provider"),
                    "model": (model_used or {}).get("model"),
                    "reasoning_effort": (model_used or {}).get("reasoning_effort"),
                },
                tree={"task_id": tid, "run_id": run.id if run else None},
                summary=summary or "",
                result=result or "",
                metadata=metadata or {},
            )
            return _worker_terminal_ok(
                KanbanTerminalAction.COMPLETE,
                task_id=tid,
                run_id=run.id if run else None,
            )
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_complete: {e}")
    except Exception as e:
        logger.exception("kanban_complete failed")
        return tool_error(f"kanban_complete: {e}")


def _handle_block(args: dict, **kw) -> str:
    """Transition the task to blocked with a reason a human will read.

    Also accepts optional ``summary`` and ``metadata`` for structured
    recovery handoff (e.g. iteration-budget exhaustion, review-required
    blocks after partial work).
    """
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    ownership_err = _enforce_worker_task_ownership(tid)
    if ownership_err:
        return ownership_err
    reason = args.get("reason")
    if not reason or not str(reason).strip():
        return tool_error("reason is required — explain what input you need")
    summary = args.get("summary")
    metadata = args.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        return tool_error(
            f"metadata must be an object/dict, got {type(metadata).__name__}"
        )
    # Stamp model/session metadata, mirroring kanban_complete.
    model_used = None
    metadata = _stamp_worker_session_metadata(tid, metadata)
    reason = redact_sensitive_text(str(reason), force=True)
    kind = args.get("kind")
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        if kind is not None and kind not in kb.VALID_BLOCK_KINDS:
            conn.close()
            return tool_error(
                f"kind must be one of {sorted(kb.VALID_BLOCK_KINDS)} (or omit it)"
            )
        # Goal-mode block gate (Issue #38696, sibling of the kanban_complete
        # judge gate in #38367). kanban_block is a second exit path out of
        # the goal loop — run_kanban_goal_loop() treats ANY `blocked` status
        # as terminal, identically to `done`, regardless of kind. Without
        # this, a worker that learns kanban_complete is gated can just call
        # kanban_block(reason="anything") to escape the loop instead.
        # Restrict goal_mode tasks to the kinds that represent a genuine
        # external blocker the worker cannot resolve itself; `capability`
        # and `transient` (or an unset kind) route back through
        # kanban_complete, which the judge now gates.
        task = kb.get_task(conn, tid)
        if (
            task
            and task.goal_mode
            and kind not in _GOAL_MODE_BLOCK_ALLOWED_KINDS
        ):
            conn.close()
            return tool_error(
                f"goal_mode tasks can only block with kind in "
                f"{sorted(_GOAL_MODE_BLOCK_ALLOWED_KINDS)} (got {kind!r}). "
                f"If the task is actually finished or cannot proceed for "
                f"another reason, call kanban_complete instead — the "
                f"completion judge will evaluate it."
            )
        model_used = _current_model_used(kb, conn, tid)
        if model_used:
            metadata = dict(metadata or {})
            metadata["model_used"] = model_used
        try:
            ok = kb.block_task(
                conn, tid,
                reason=reason,
                summary=summary,
                metadata=metadata,
                kind=kind,
                expected_run_id=_worker_run_id(tid),
            )
            if not ok:
                return tool_error(
                    f"could not block {tid} (unknown id or not in "
                    f"running/ready)"
                )
            run = kb.latest_run(conn, tid)
            # Tell the worker where the task actually landed so it doesn't
            # assume it's sitting in 'blocked' when routing sent it elsewhere.
            landed = kb.get_task(conn, tid)
            return _worker_terminal_ok(
                KanbanTerminalAction.BLOCK,
                task_id=tid,
                run_id=run.id if run else None,
                status=landed.status if landed else "blocked",
                block_kind=kind,
            )
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_block: {e}")
    except Exception as e:
        logger.exception("kanban_block failed")
        return tool_error(f"kanban_block: {e}")


def _handle_request_rework(args: dict, **kw) -> str:
    """Atomically create/adopt a fix card and re-arm a review card."""
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    ownership_err = _enforce_worker_task_ownership(tid)
    if ownership_err:
        return ownership_err
    finding = args.get("finding")
    if not finding or not str(finding).strip():
        return tool_error("finding is required — describe the defect to fix")
    request_key = args.get("request_key")
    if not request_key or not str(request_key).strip():
        return tool_error("request_key is required for idempotent rework")
    fix_task_id = args.get("fix_task_id")
    fix_spec = args.get("fix")
    has_fix_task_id = bool(fix_task_id and str(fix_task_id).strip())
    has_fix_spec = fix_spec is not None
    if has_fix_task_id == has_fix_spec:
        return tool_error("provide exactly one of fix_task_id or fix")
    if fix_spec is not None and not isinstance(fix_spec, dict):
        return tool_error("fix must be an object")
    metadata = args.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        return tool_error(
            f"metadata must be an object/dict, got {type(metadata).__name__}"
        )
    board = args.get("board")
    try:
        from hermes_cli import kanban_db as kb

        if fix_spec is not None:
            list_fields = ("skills", "toolsets")
            normalized_lists: dict[str, Optional[tuple[str, ...]]] = {}
            for field in list_fields:
                value = fix_spec.get(field)
                if value is None:
                    normalized_lists[field] = None
                elif isinstance(value, (list, tuple)):
                    normalized_lists[field] = tuple(str(item) for item in value)
                else:
                    return tool_error(f"fix.{field} must be an array")
            fix = kb.NewFixTask(
                title=str(fix_spec.get("title") or ""),
                body=fix_spec.get("body"),
                assignee=str(fix_spec.get("assignee") or ""),
                workspace_kind=fix_spec.get("workspace_kind"),
                workspace_path=fix_spec.get("workspace_path"),
                project_id=fix_spec.get("project_id"),
                branch_name=fix_spec.get("branch_name"),
                priority=fix_spec.get("priority"),
                skills=normalized_lists["skills"],
                toolsets=normalized_lists["toolsets"],
                max_runtime_seconds=fix_spec.get("max_runtime_seconds"),
            )
        else:
            fix = kb.ExistingFixTask(task_id=str(fix_task_id).strip())

        finding = redact_sensitive_text(str(finding), force=True)
        summary = args.get("summary")
        if summary is not None:
            summary = redact_sensitive_text(str(summary), force=True)
        metadata = _stamp_worker_session_metadata(tid, metadata)
        actor = (
            os.environ.get("HERMES_KANBAN_ACTOR")
            or os.environ.get("HERMES_PROFILE")
            or "worker"
        )
        expected_run_id = _worker_run_id(tid)
        if os.environ.get("HERMES_KANBAN_TASK") == tid and expected_run_id is None:
            return tool_error(
                "HERMES_KANBAN_RUN_ID is required when a worker requests rework"
            )
        conn = None
        try:
            kb_obj, conn = _connect(board=board)
            result = kb_obj.request_rework(
                conn,
                tid,
                finding=finding,
                fix=fix,
                request_key=str(request_key).strip(),
                actor=actor,
                summary=summary,
                metadata=metadata,
                expected_run_id=expected_run_id,
                require_no_active_run=expected_run_id is None,
            )
            result_fields = {
                "fix_task_id": result.fix_task_id,
                "fix_action": result.fix_action,
                "review_status": result.review_status,
                "request_event_id": result.request_event_id,
            }
            # A replay from a later run is idempotent data access, not a
            # lifecycle transition for this worker.  Returning ordinary tool
            # output keeps the current run alive so it can finish explicitly.
            if result.fix_action == "replayed" and not result.replayed_same_run:
                return _ok(task_id=tid, **result_fields)
            return _worker_terminal_ok(
                KanbanTerminalAction.REWORK,
                task_id=tid,
                **result_fields,
            )
        finally:
            if conn is not None:
                conn.close()
    except ValueError as e:
        return tool_error(f"kanban_request_rework: {e}")
    except Exception as e:
        logger.exception("kanban_request_rework failed")
        return tool_error(f"kanban_request_rework: {e}")


def _handle_request_publication(args: dict, **kw) -> str:
    """Atomically park a committed task behind a releaser publication card."""
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    ownership_err = _enforce_worker_task_ownership(tid)
    if ownership_err:
        return ownership_err
    request_key = args.get("request_key")
    if not request_key or not str(request_key).strip():
        return tool_error("request_key is required for publication handoff idempotency")
    publication_task_id = args.get("publication_task_id")
    publisher_task_id = args.get("publisher_task_id")
    if publication_task_id and publisher_task_id and str(publication_task_id).strip() != str(publisher_task_id).strip():
        return tool_error(
            "publication_task_id and publisher_task_id must identify the same card"
        )
    publication_task_id = publication_task_id or publisher_task_id
    create_fields = (
        args.get("expected_sha"), args.get("workspace_path"),
        args.get("remote") if args.get("remote") != "origin" else None,
        args.get("remote_ref"),
        args.get("title"), args.get("body"),
    )
    if publication_task_id and any(value is not None for value in create_fields):
        return tool_error(
            "publication_task_id cannot be combined with publication card fields"
        )
    metadata = args.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        return tool_error(
            f"metadata must be an object/dict, got {type(metadata).__name__}"
        )
    board = args.get("board")
    try:
        from hermes_cli import kanban_db as kb

        conn = None
        try:
            kb_obj, conn = _connect(board=board)
            if publication_task_id:
                publication = kb_obj.ExistingPublicationTask(
                    task_id=str(publication_task_id).strip()
                )
            else:
                task = kb_obj.get_task(conn, tid)
                if task is None:
                    return tool_error(f"unknown requester task: {tid}")
                workspace_path = args.get("workspace_path") or task.workspace_path
                if not workspace_path:
                    return tool_error(
                        "workspace_path is required when the requester has no stored workspace"
                    )
                if not args.get("expected_sha"):
                    return tool_error("expected_sha is required for publication handoff")
                if not args.get("remote_ref"):
                    return tool_error("remote_ref is required for publication handoff")
                publication = kb_obj.NewPublicationTask(
                    title=str(args.get("title") or "Publish committed change"),
                    body=args.get("body"),
                    workspace_path=str(workspace_path),
                    expected_sha=str(args.get("expected_sha")),
                    remote=str(args.get("remote") or "origin"),
                    remote_ref=str(args.get("remote_ref")),
                )
            finding = (
                "Publication handoff for committed change"
                if not args.get("summary")
                else str(args.get("summary"))
            )
            summary = redact_sensitive_text(finding, force=True)
            metadata = _stamp_worker_session_metadata(tid, metadata)
            actor = (
                os.environ.get("HERMES_KANBAN_ACTOR")
                or os.environ.get("HERMES_PROFILE")
                or "worker"
            )
            expected_run_id = _worker_run_id(tid)
            if os.environ.get("HERMES_KANBAN_TASK") == tid and expected_run_id is None:
                return tool_error(
                    "HERMES_KANBAN_RUN_ID is required when a worker requests publication"
                )
            result = kb_obj.request_publication_handoff(
                conn,
                tid,
                publication=publication,
                request_key=str(request_key).strip(),
                actor=actor,
                summary=summary,
                metadata=metadata,
                expected_run_id=expected_run_id,
                require_no_active_run=expected_run_id is None,
            )
            result_fields = {
                "publication_task_id": result.publication_task_id,
                "publication_action": result.publication_action,
                "requester_status": result.requester_status,
                "request_event_id": result.request_event_id,
            }
            if result.publication_action == "replayed" and not result.replayed_same_run:
                return _ok(task_id=tid, **result_fields)
            return _worker_terminal_ok(
                KanbanTerminalAction.PUBLICATION,
                task_id=tid,
                **result_fields,
            )
        finally:
            if conn is not None:
                conn.close()
    except ValueError as e:
        return tool_error(f"kanban_request_publication: {e}")
    except Exception as e:
        logger.exception("kanban_request_publication failed")
        return tool_error(f"kanban_request_publication: {e}")


def _handle_heartbeat(args: dict, **kw) -> str:
    """Signal that the worker is still alive during a long operation.

    Extends the claim TTL via ``heartbeat_claim`` AND records a heartbeat
    event via ``heartbeat_worker``. Without the ``heartbeat_claim`` half,
    a diligent worker that loops this tool while a single tool call
    blocks the agent for >DEFAULT_CLAIM_TTL_SECONDS still gets reclaimed
    by ``release_stale_claims`` — which is exactly the trap that
    ``heartbeat_claim``'s docstring warns against.
    """
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    ownership_err = _enforce_worker_task_ownership(tid)
    if ownership_err:
        return ownership_err
    note = args.get("note")
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            # Extend the claim TTL first. The dispatcher pins
            # HERMES_KANBAN_CLAIM_LOCK in the worker env at spawn time
            # (see _default_spawn in kanban_db.py); falling back to the
            # default _claimer_id() covers locally-driven workers that
            # never went through the dispatcher path.
            claim_lock = os.environ.get("HERMES_KANBAN_CLAIM_LOCK")
            kb.heartbeat_claim(conn, tid, claimer=claim_lock)

            ok = kb.heartbeat_worker(
                conn,
                tid,
                note=note,
                expected_run_id=_worker_run_id(tid),
            )
            if not ok:
                return tool_error(
                    f"could not heartbeat {tid} (unknown id or not running)"
                )
            return _ok(task_id=tid)
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_heartbeat: {e}")
    except Exception as e:
        logger.exception("kanban_heartbeat failed")
        return tool_error(f"kanban_heartbeat: {e}")


def _handle_comment(args: dict, **kw) -> str:
    """Append a comment to a task's thread."""
    tid = args.get("task_id")
    if not tid:
        return tool_error(
            "task_id is required (use the current task id if that's what "
            "you mean — pulls from env but kept explicit here)"
        )
    body = args.get("body")
    if not body or not str(body).strip():
        return tool_error("body is required")
    body = redact_sensitive_text(str(body), force=True)
    # Author is intentionally derived from the worker's own runtime
    # identity, NOT from caller-supplied args. Comments are injected
    # into the next worker's system prompt by ``build_worker_context``
    # as ``**{author}** (timestamp): {body}`` — accepting an
    # ``args["author"]`` override let a worker forge a comment from
    # an authoritative-looking name like ``hermes-system`` and poison
    # the future-worker context with what reads as a system directive.
    # Cross-task commenting itself remains unrestricted (see #19713) —
    # comments are the deliberate handoff channel between tasks.
    author = os.environ.get("HERMES_PROFILE") or "worker"
    blocker = args.get("blocker")
    resolve_blocker_id = args.get("resolve_blocker_id")
    resolution_evidence_ref = args.get("resolution_evidence_ref")
    if blocker is not None and resolve_blocker_id is not None:
        return tool_error("pass blocker or resolve_blocker_id, not both")
    if blocker is not None and not isinstance(blocker, dict):
        return tool_error("blocker must be an object")
    if resolve_blocker_id is not None and not resolution_evidence_ref:
        return tool_error(
            "resolution_evidence_ref is required when resolving a blocker"
        )
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            current_task = os.environ.get("HERMES_KANBAN_TASK")
            run_id = _worker_run_id(tid) if current_task == str(tid) else None
            blocker_result = None
            if blocker is not None:
                blocker_title = redact_sensitive_text(
                    str(blocker.get("title") or body), force=True,
                )
                blocker_details = redact_sensitive_text(
                    str(blocker.get("details") or body), force=True,
                )
                blocker_evidence = blocker.get("evidence_ref")
                if blocker_evidence is not None:
                    blocker_evidence = redact_sensitive_text(
                        str(blocker_evidence), force=True,
                    )
                blocker_result = kb.record_continuation_blocker(
                    conn,
                    str(tid),
                    severity=blocker.get("severity"),
                    title=blocker_title,
                    details=blocker_details,
                    evidence_ref=blocker_evidence,
                    discovered_by=author,
                    discovered_run_id=run_id,
                )
            elif resolve_blocker_id is not None:
                resolution_evidence_ref = redact_sensitive_text(
                    str(resolution_evidence_ref), force=True,
                )
                blocker_result = kb.resolve_continuation_blocker(
                    conn,
                    str(tid),
                    int(resolve_blocker_id),
                    resolved_by=author,
                    resolution_evidence_ref=resolution_evidence_ref,
                    resolved_run_id=run_id,
                )
            cid = kb.add_comment(conn, tid, author=author, body=str(body))
            payload = {"task_id": tid, "comment_id": cid}
            if blocker_result is not None:
                payload["blocker"] = {
                    "id": blocker_result.id,
                    "severity": blocker_result.severity,
                    "status": blocker_result.status,
                }
            return _ok(**payload)
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_comment: {e}")
    except Exception as e:
        logger.exception("kanban_comment failed")
        return tool_error(f"kanban_comment: {e}")


def _worker_architecture_context(kb: Any, conn: Any) -> Any:
    """Bind worker-created cards to their authoritative ancestor gate.

    The model cannot provide this context. It is derived only from the
    dispatcher-owned current task id and the authoritative parent graph.
    """
    task_id = os.environ.get("HERMES_KANBAN_TASK")
    if not task_id:
        return None
    gate = kb.get_architecture_gate_for_task(conn, task_id)
    if gate is None or gate.enforcement_mode not in kb.ARCHITECTURE_GATE_ENFORCING_MODES:
        return None
    return kb.MutationContext(
        board_key=gate.board_key,
        principal=gate.creator_principal,
        actor_type="kanban_worker",
        session_id=gate.session_id,
        request_scope_id=gate.request_scope_id,
        workflow_key=gate.workflow_key,
        gate_id=gate.gate_id,
        profile=os.environ.get("HERMES_PROFILE") or None,
        mode=gate.enforcement_mode,
        phase="protected",
    )


def _managed_architecture_gate_policy() -> tuple[str, bool]:
    """Return ``(mode, valid)`` for the synced staged policy."""
    try:
        policy_path = get_hermes_home() / "rules" / "architecture-gate-policy.json"
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        if not isinstance(policy, dict):
            return "off", False
        version = policy.get("version")
        schema_version = policy.get("schema_version")
        if version is not None and version != "v1":
            return "off", False
        if schema_version is not None and schema_version != 1:
            return "off", False
        if version is None and schema_version is None:
            return "off", False
        mode = policy.get("mode")
        if mode in {"off", "shadow", "orchestrator_only"}:
            return str(mode), True
    except FileNotFoundError:
        # The managed gate is an optional staged rollout. Homes that predate
        # it have no policy file and retain the legacy explicit-off behavior;
        # once a file exists, malformed/unreadable content fails closed below.
        return "off", True
    except Exception:
        logger.warning("Unable to read managed architecture-gate policy", exc_info=True)
        return "off", False
    return "off", False


def _managed_architecture_gate_mode() -> str:
    """Compatibility projection; invalid managed policy reads as ``off``."""
    mode, valid = _managed_architecture_gate_policy()
    if valid:
        return mode
    # ``enforce`` remains readable in existing gate rows but is deliberately
    # not a new front-door activation target.
    return "off"


def _trusted_workflow_notifications(
    gateway_source: Optional[dict] = None,
) -> tuple[list[dict[str, Optional[str]]], str]:
    """Derive a terminal notification target from runtime-owned context.

    The workflow tool has no model-visible notification fields. This keeps a
    prompt-injected orchestrator from redirecting completion messages while
    still letting the kernel insert the one terminal subscription in the same
    transaction as the graph.
    """
    try:
        cfg = load_config()
        if not cfg_get(cfg, "kanban", "auto_subscribe_on_create", default=True):
            return [], "disabled"
    except Exception:
        cfg = {}

    if gateway_source is not None:
        platform = str(gateway_source.get("platform") or "").strip()
        chat_id = str(gateway_source.get("chat_id") or "").strip()
        if not platform or not chat_id:
            return [], "gateway_source_incomplete"
        return [{
            "platform": platform,
            "chat_id": chat_id,
            "thread_id": str(gateway_source.get("thread_id") or "") or None,
            "user_id": str(gateway_source.get("user_id") or "") or None,
            "notifier_profile": os.environ.get("HERMES_PROFILE") or None,
        }], "gateway_source"

    try:
        from gateway.session_context import get_session_env

        platform = (
            get_session_env("HERMES_SESSION_PLATFORM", "")
            or os.environ.get("HERMES_SESSION_PLATFORM", "")
        )
        chat_id = (
            get_session_env("HERMES_SESSION_CHAT_ID", "")
            or os.environ.get("HERMES_SESSION_CHAT_ID", "")
        )
        source = "session"
        if not platform or not chat_id:
            session_key = (
                get_session_env("HERMES_SESSION_KEY", "")
                or os.environ.get("HERMES_SESSION_KEY", "")
            )
            if not session_key:
                kanban_cfg = cfg.get("kanban") if isinstance(cfg, dict) else {}
                if not isinstance(kanban_cfg, dict) or not bool(
                    kanban_cfg.get("auto_subscribe_home_on_create")
                ):
                    return [], "unattached"
                allowed = _parse_platform_filter(
                    kanban_cfg.get("auto_subscribe_home_platforms", "*")
                )
                homes = []
                for home in _configured_home_channels():
                    home_platform = str(home.get("platform") or "").lower()
                    home_chat_id = str(home.get("chat_id") or "")
                    if (
                        home_platform
                        and home_chat_id
                        and ("*" in allowed or home_platform in allowed)
                    ):
                        homes.append({
                            "platform": home_platform,
                            "chat_id": home_chat_id,
                            "thread_id": home.get("thread_id") or None,
                            "user_id": None,
                            "notifier_profile": os.environ.get("HERMES_PROFILE") or None,
                        })
                return homes, "home_channel" if homes else "unattached"
            platform, chat_id, source = "tui", session_key, "tui_session"
        return [{
            "platform": str(platform),
            "chat_id": str(chat_id),
            "thread_id": (
                get_session_env("HERMES_SESSION_THREAD_ID", "")
                or os.environ.get("HERMES_SESSION_THREAD_ID", "")
                or None
            ),
            "user_id": (
                get_session_env("HERMES_SESSION_USER_ID", "")
                or os.environ.get("HERMES_SESSION_USER_ID", "")
                or None
            ),
            "notifier_profile": (
                get_session_env("HERMES_SESSION_PROFILE", "")
                or os.environ.get("HERMES_PROFILE")
                or None
            ),
        }], source
    except Exception:
        logger.warning("Unable to derive workflow notification target", exc_info=True)
        return [], "unavailable"


def _trusted_session_id() -> Optional[str]:
    try:
        from gateway.session_context import get_session_env

        value = get_session_env("HERMES_SESSION_ID", "")
    except Exception:
        value = os.environ.get("HERMES_SESSION_ID", "")
    return str(value or "").strip() or None


def _workflow_request_digest(args: dict[str, Any]) -> str:
    """Hash only model-visible semantic inputs, excluding volatile runtime state."""
    payload = {
        key: args[key]
        for key in (
            "workflow_key", "idempotency_key", "steps", "tenant",
            "workspace_kind", "workspace_path", "priority",
            # Presence-gated, so requests predating BUILD-580 hash unchanged.
            "jira_scope_mapping",
        )
        if key in args
    }
    steps = payload.get("steps")
    if isinstance(steps, list) and all(isinstance(step, dict) for step in steps):
        canonical_steps = []
        for raw in steps:
            step = dict(raw)
            if isinstance(step.get("parents"), list):
                step["parents"] = sorted(step["parents"])
            canonical_steps.append(step)
        payload["steps"] = sorted(
            canonical_steps, key=lambda step: str(step.get("key") or "")
        )
    return hashlib.sha256(
        json.dumps(
            payload, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _compiled_workflow_response(kb: Any, conn: Any, compiled: Any) -> str:
    # BUILD-503 fans subscriptions out to every non-done step task (failure
    # kinds only per BUILD-508); report the whole set, not just the terminal.
    task_ids = list(compiled.task_ids.values())
    placeholders = ",".join("?" * len(task_ids))
    total = conn.execute(
        f"SELECT COUNT(*) FROM kanban_notify_subs WHERE task_id IN ({placeholders})",
        tuple(task_ids),
    ).fetchone()[0]
    terminal_count = conn.execute(
        "SELECT COUNT(*) FROM kanban_notify_subs WHERE task_id = ?",
        (compiled.terminal_task_id,),
    ).fetchone()[0]
    return _ok(
        workflow_key=compiled.workflow_key,
        task_ids=compiled.task_ids,
        terminal_task_id=compiled.terminal_task_id,
        notification={
            "subscribed": bool(terminal_count),
            "count": int(total),
            "terminal_count": int(terminal_count),
            "terminal_only": int(total) == int(terminal_count),
        },
    )


def _handle_compile_workflow(args: dict, **kw) -> str:
    """Validate and persist a convergent multi-card workflow in one commit."""
    guard = _require_orchestrator_tool("kanban_compile_workflow")
    if guard:
        return guard
    workflow_key = str(args.get("workflow_key") or "").strip()
    idempotency_key = str(args.get("idempotency_key") or "").strip()
    steps = args.get("steps")
    if not workflow_key:
        return tool_error("workflow_key is required")
    if not idempotency_key:
        return tool_error("idempotency_key is required")
    if not isinstance(steps, list) or not steps:
        return tool_error("steps must be a non-empty list")
    try:
        request_digest = _workflow_request_digest(args)
    except (TypeError, ValueError) as exc:
        return tool_error(f"kanban_compile_workflow: invalid request: {exc}")
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            existing = kb.get_compiled_workflow_retry(
                conn,
                workflow_key=workflow_key,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
            )
            if existing is not None:
                return _compiled_workflow_response(kb, conn, existing)

            architecture_mode, policy_valid = _managed_architecture_gate_policy()
            if not policy_valid:
                return tool_error(
                    "kanban_compile_workflow: managed architecture-gate policy "
                    "is missing or invalid; refusing new graph compilation"
                )
            if architecture_mode != "off":
                return tool_error(
                    "kanban_compile_workflow cannot bypass staged architecture graph "
                    "issuance; use the authorized architecture graph issuance path "
                    "while the managed architecture gate is active"
                )

            resolved_steps: list[Any] = []
            for raw in steps:
                if not isinstance(raw, dict):
                    resolved_steps.append(raw)
                    continue
                step = dict(raw)
                resolved_model, routing = _resolve_model_routing(
                    step, step.pop("model_override", None),
                )
                step.pop("model_routing", None)
                step.pop("task_category", None)
                step.pop("data_sensitivity", None)
                step.pop("verifiability", None)
                step.pop("quota_policy", None)
                if resolved_model:
                    step["model_override"] = resolved_model
                if routing:
                    step["model_provider_override"] = routing.get("provider")
                    step["model_reasoning_effort"] = routing.get("reasoning_effort")
                resolved_steps.append(step)

            notifications, _notification_source = _trusted_workflow_notifications(
                kw.get("gateway_source")
            )
            known_profiles = set(kb.list_profiles_on_disk())
            requested_profiles = {
                str(step.get("assignee") or "").strip()
                for step in resolved_steps
                if isinstance(step, dict)
            }
            unknown_profiles = sorted(
                profile for profile in requested_profiles
                if profile and known_profiles and profile not in known_profiles
            )
            if unknown_profiles:
                raise kb.WorkflowGraphError(
                    "unknown assignee profile(s): " + ", ".join(unknown_profiles)
                )
            # BUILD-580: divergent per-step Jira scopes without an explicit
            # mapping produced an unverifiable handoff (verifier scoped to
            # BUILD-567 while the fix landed under BUILD-575). Steps may
            # declare `jira_key`; when they diverge, the request must carry
            # `jira_scope_mapping` accounting for every non-canonical key.
            # Keys are validated here and stripped before graph compilation
            # (the step schema downstream is strict).
            step_jira_keys = {
                str(step.get("jira_key") or "").strip()
                for step in resolved_steps
                if isinstance(step, dict)
            }
            step_jira_keys.discard("")
            if len(step_jira_keys) > 1:
                mapping = args.get("jira_scope_mapping")
                mapped: set = set()
                if isinstance(mapping, dict):
                    mapped = {str(k).strip() for k in mapping} | {
                        str(v).strip() for v in mapping.values()
                    }
                unmapped = sorted(step_jira_keys - mapped)
                if unmapped:
                    raise kb.WorkflowGraphError(
                        "divergent step jira_key values need an explicit "
                        "jira_scope_mapping; unmapped: " + ", ".join(unmapped)
                    )
            for step in resolved_steps:
                if isinstance(step, dict):
                    step.pop("jira_key", None)
            compiled = kb.compile_workflow_graph(
                conn,
                workflow_key=workflow_key,
                idempotency_key=idempotency_key,
                created_by=os.environ.get("HERMES_PROFILE") or "orchestrator",
                steps=resolved_steps,
                notification=notifications or None,
                tenant=args.get("tenant") or os.environ.get("HERMES_TENANT"),
                session_id=_trusted_session_id(),
                workspace_kind=str(args.get("workspace_kind") or "scratch"),
                workspace_path=args.get("workspace_path"),
                priority=int(args.get("priority") or 0),
                request_digest=request_digest,
                request_scope_id=(
                    f"front-door:{kw.get('turn_id')}" if kw.get("turn_id") else None
                ),
                deny_active_architecture_session=True,
            )
            return _compiled_workflow_response(kb, conn, compiled)
        finally:
            conn.close()
    except (ValueError, TypeError) as e:
        return tool_error(f"kanban_compile_workflow: {e}")
    except Exception as e:
        logger.exception("kanban_compile_workflow failed")
        return tool_error(f"kanban_compile_workflow: {e}")


def _trusted_front_door_architecture_context(
    kb: Any, *, board: Any, assignee: Any, turn_id: Any,
) -> Any:
    """Construct architecture authority from runtime identity, never tool args."""
    if os.environ.get("HERMES_KANBAN_TASK"):
        return None
    if os.environ.get("HERMES_PROFILE") != "orchestrator":
        return None
    try:
        from gateway.session_context import get_session_env
        session_id = get_session_env("HERMES_SESSION_ID", "")
    except Exception:
        session_id = os.environ.get("HERMES_SESSION_ID", "")
    session_id = str(session_id).strip()
    trusted_turn_id = str(turn_id or "").strip()
    mode = _managed_architecture_gate_mode()
    if not session_id or not trusted_turn_id or mode == "off":
        return None
    return kb.MutationContext(
        board_key=str(board or os.environ.get("HERMES_KANBAN_BOARD") or "default"),
        principal=f"orchestrator:{session_id}", actor_type="orchestrator_agent",
        profile="orchestrator", session_id=session_id,
        request_scope_id=f"front-door:{trusted_turn_id}", mode=mode,
        phase=(
            "architecture"
            if str(assignee).strip() == "architect"
            else "implementation"
        ),
    )


def _handle_attach(args: dict, **kw) -> str:
    """Attach an inline (base64) file to a task.

    Mirrors the dashboard's upload endpoint for the agent surface: decode
    the payload, enforce the shared size cap, write it under the per-task
    attachments dir, and record the metadata row — all via
    ``kanban_db.store_attachment_bytes`` so the three surfaces stay in lockstep.
    """
    from hermes_cli import kanban_db as kb

    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    ownership_err = _enforce_worker_task_ownership(tid)
    if ownership_err:
        return ownership_err
    filename = args.get("filename")
    if not filename or not str(filename).strip():
        return tool_error("filename is required")
    content_b64 = args.get("content_base64")
    if not content_b64 or not str(content_b64).strip():
        return tool_error("content_base64 is required")
    import base64
    import binascii
    try:
        data = base64.b64decode(str(content_b64), validate=True)
    except (binascii.Error, ValueError) as e:
        return tool_error(f"content_base64 is not valid base64: {e}")
    content_type = args.get("content_type")
    board = args.get("board")
    try:
        _, conn = _connect(board=board)
        try:
            att_id = kb.store_attachment_bytes(
                conn,
                tid,
                str(filename),
                data,
                content_type=content_type,
                uploaded_by="agent",
                board=board,
            )
            return _ok(task_id=tid, attachment_id=att_id, size=len(data))
        finally:
            conn.close()
    except kb.AttachmentTooLarge as e:
        return tool_error(f"kanban_attach: {e}")
    except ValueError as e:
        return tool_error(f"kanban_attach: {e}")
    except Exception as e:
        logger.exception("kanban_attach failed")
        return tool_error(f"kanban_attach: {e}")


_MAX_ATTACH_URL_REDIRECTS = 5


def _download_url_with_cap(url: str, max_bytes: int) -> tuple[bytes, Optional[str]]:
    """Fetch ``url`` over http(s) with SSRF guarding, capped at ``max_bytes``.

    Every hop — the initial URL and each redirect target — is validated with
    ``tools.url_safety.is_safe_url`` before it is fetched, so a
    model-controlled URL (or a public host 302ing to one) cannot reach
    loopback, private/CGNAT ranges, or cloud metadata endpoints. Redirects
    are followed manually (``follow_redirects=False``) so each Location is
    re-checked, mirroring ``tools.skills_hub._guarded_http_get``.

    Returns ``(data, content_type)``. Raises ``ValueError`` for a non-http(s)
    scheme, an SSRF-blocked target, too many redirects, or a body that
    overruns the cap (the caller maps it to a clean tool error). Reads in
    chunks so an oversize response is rejected without buffering the whole
    thing.
    """
    from urllib.parse import urljoin, urlparse

    import httpx

    from tools.url_safety import is_safe_url

    current_url = url
    for _ in range(_MAX_ATTACH_URL_REDIRECTS + 1):
        scheme = (urlparse(current_url).scheme or "").lower()
        if scheme not in ("http", "https"):
            raise ValueError(
                f"unsupported URL scheme {scheme!r}; only http/https are allowed"
            )
        if not is_safe_url(current_url):
            raise ValueError(
                f"URL blocked by SSRF protection (private/internal address): {current_url}"
            )
        chunks: list[bytes] = []
        total = 0
        with httpx.stream(
            "GET",
            current_url,
            headers={"User-Agent": "hermes-kanban/attach"},
            timeout=30,
            follow_redirects=False,
        ) as resp:
            if resp.is_redirect:
                location = resp.headers.get("location")
                if not location:
                    raise ValueError(f"redirect without Location header from {current_url}")
                current_url = urljoin(current_url, location)
                continue
            resp.raise_for_status()
            content_type = (resp.headers.get("content-type") or "").split(";")[0].strip() or None
            for chunk in resp.iter_bytes(1024 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(
                        f"attachment exceeds {max_bytes // (1024 * 1024)} MB limit"
                    )
                chunks.append(chunk)
        return b"".join(chunks), content_type
    raise ValueError(f"too many redirects fetching {url}")


def _handle_attach_url(args: dict, **kw) -> str:
    """Attach a file fetched server-side from a URL.

    The agent passes a URL; Hermes downloads it (with the shared size cap)
    and stores it as a real attachment. Useful when the agent has a link
    rather than the bytes. Only http/https URLs are accepted.
    """
    from hermes_cli import kanban_db as kb

    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    ownership_err = _enforce_worker_task_ownership(tid)
    if ownership_err:
        return ownership_err
    url = args.get("url")
    if not url or not str(url).strip():
        return tool_error("url is required")
    url = str(url).strip()
    filename = args.get("filename") or args.get("title")
    if not filename or not str(filename).strip():
        # Derive a name from the URL path's leaf component.
        from urllib.parse import unquote, urlparse
        leaf = unquote(urlparse(url).path.rsplit("/", 1)[-1]).strip()
        filename = leaf or "download"
    content_type = args.get("content_type")
    board = args.get("board")
    try:
        data, fetched_ct = _download_url_with_cap(url, kb.KANBAN_ATTACHMENT_MAX_BYTES)
    except ValueError as e:
        return tool_error(f"kanban_attach_url: {e}")
    except Exception as e:
        logger.exception("kanban_attach_url download failed")
        return tool_error(f"kanban_attach_url: failed to fetch {url}: {e}")
    try:
        _, conn = _connect(board=board)
        try:
            att_id = kb.store_attachment_bytes(
                conn,
                tid,
                str(filename),
                data,
                content_type=content_type or fetched_ct,
                uploaded_by="agent",
                board=board,
            )
            return _ok(task_id=tid, attachment_id=att_id, size=len(data))
        finally:
            conn.close()
    except kb.AttachmentTooLarge as e:
        return tool_error(f"kanban_attach_url: {e}")
    except ValueError as e:
        return tool_error(f"kanban_attach_url: {e}")
    except Exception as e:
        logger.exception("kanban_attach_url failed")
        return tool_error(f"kanban_attach_url: {e}")


def _handle_attachments(args: dict, **kw) -> str:
    """List a task's attachments (read-only; no ownership restriction)."""
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            if kb.get_task(conn, tid) is None:
                return tool_error(f"task {tid} not found")
            atts = kb.list_attachments(conn, tid)
            return json.dumps({
                "ok": True,
                "task_id": tid,
                "attachments": [
                    {
                        "id": a.id,
                        "filename": a.filename,
                        "content_type": a.content_type,
                        "size": a.size,
                        "uploaded_by": a.uploaded_by,
                        "stored_path": a.stored_path,
                        "created_at": a.created_at,
                    }
                    for a in atts
                ],
            })
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_attachments: {e}")
    except Exception as e:
        logger.exception("kanban_attachments failed")
        return tool_error(f"kanban_attachments: {e}")




def _handle_create(args: dict, **kw) -> str:
    """Create a child task. Orchestrator workers use this to fan out.

    ``parents`` can be a list of task ids; dependency-gated promotion
    works as usual.
    """
    title = args.get("title")
    if not title or not str(title).strip():
        return tool_error("title is required")
    assignee = args.get("assignee")
    if not assignee:
        return tool_error(
            "assignee is required — name the profile that should execute this "
            "task (the dispatcher will only spawn tasks with an assignee)"
        )
    body = args.get("body")
    parents = args.get("parents") or []
    tenant = args.get("tenant") or os.environ.get("HERMES_TENANT")
    # Stamp the originating session id when the agent loop runs under
    # ACP (which sets HERMES_SESSION_ID before invoking tools). NULL on
    # CLI / dashboard paths and on legacy hosts that don't set the env.
    session_id = args.get("session_id") or os.environ.get("HERMES_SESSION_ID")
    priority = args.get("priority")
    # Resolve workspace. If the caller passed one explicitly, honor it.
    # Otherwise, a dispatcher-spawned worker (HERMES_KANBAN_TASK set)
    # inherits its own running task's workspace, so a worker editing a
    # dir:/worktree project that spawns a follow-up child keeps the child
    # in that project instead of a throwaway scratch dir. Orchestrators
    # (kanban toolset, no HERMES_KANBAN_TASK) and CLI/dashboard callers
    # fall back to scratch as before. Explicit None path stays None.
    workspace_kind = args.get("workspace_kind")
    workspace_path = args.get("workspace_path")
    project_id = args.get("project") or args.get("project_id")
    _inherit_workspace = workspace_kind is None and workspace_path is None
    if workspace_kind is None:
        workspace_kind = "scratch"
    triage, bool_error = _parse_bool_arg(args, "triage")
    if bool_error:
        return tool_error(bool_error)
    idempotency_key = args.get("idempotency_key")
    max_runtime_seconds = args.get("max_runtime_seconds")
    initial_status = args.get("initial_status") or "running"
    workflow_key = args.get("workflow_key")
    if workflow_key is not None:
        workflow_key = str(workflow_key).strip() or None
    current_step_key = args.get("current_step_key")
    if current_step_key is not None:
        current_step_key = str(current_step_key).strip() or None
    model_override, model_routing_decision = _resolve_model_routing(
        args,
        args.get("model_override"),
    )
    skills = args.get("skills")
    if isinstance(skills, str):
        # Accept a single skill name as a string for convenience.
        skills = [skills]
    if skills is not None and not isinstance(skills, (list, tuple)):
        return tool_error(
            f"skills must be a list of skill names, got {type(skills).__name__}"
        )
    toolsets = args.get("toolsets")
    if isinstance(toolsets, str):
        toolsets = [toolsets]
    if toolsets is not None and not isinstance(toolsets, (list, tuple)):
        return tool_error(
            f"toolsets must be a list of capability names, got {type(toolsets).__name__}"
        )
    goal_mode, goal_bool_error = _parse_bool_arg(args, "goal_mode")
    if goal_bool_error:
        return tool_error(goal_bool_error)
    goal_max_turns = args.get("goal_max_turns")
    if isinstance(parents, str):
        parents = [parents]
    if not isinstance(parents, (list, tuple)):
        return tool_error(
            f"parents must be a list of task ids, got {type(parents).__name__}"
        )
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            # Inherit the spawning worker's own task workspace when the
            # caller didn't specify one (see resolution note above).
            if _inherit_workspace:
                _self_tid = os.environ.get("HERMES_KANBAN_TASK")
                if _self_tid:
                    _self_task = kb.get_task(conn, _self_tid)
                    if _self_task is not None and _self_task.workspace_kind:
                        workspace_kind = _self_task.workspace_kind
                        workspace_path = _self_task.workspace_path
                        # Keep follow-up children inside the same project so the
                        # whole subtree shares one repo + branch convention.
                        if project_id is None and _self_task.project_id:
                            project_id = _self_task.project_id
            mutation_context = _worker_architecture_context(kb, conn)
            if mutation_context is None:
                mutation_context = _trusted_front_door_architecture_context(
                    kb, board=board, assignee=assignee, turn_id=kw.get("turn_id"),
                )
                if mutation_context is not None:
                    # Scope identity must not come from a model-visible argument.
                    session_id = mutation_context.session_id
            new_tid = kb.create_task(
                conn,
                title=str(title).strip(),
                body=body,
                assignee=str(assignee),
                parents=tuple(parents),
                tenant=tenant,
                priority=int(priority) if priority is not None else 0,
                workspace_kind=str(workspace_kind),
                workspace_path=workspace_path,
                project_id=project_id,
                triage=triage,
                idempotency_key=idempotency_key,
                max_runtime_seconds=(
                    int(max_runtime_seconds)
                    if max_runtime_seconds is not None else None
                ),
                skills=skills,
                toolsets=toolsets,
                goal_mode=goal_mode,
                goal_max_turns=(
                    int(goal_max_turns) if goal_max_turns is not None else None
                ),
                initial_status=str(initial_status),
                created_by=os.environ.get("HERMES_PROFILE") or "worker",
                session_id=session_id,
                workflow_key=workflow_key,
                current_step_key=current_step_key,
                model_override=model_override,
                model_provider_override=(model_routing_decision or {}).get("provider"),
                model_reasoning_effort=(model_routing_decision or {}).get("reasoning_effort"),
                mutation_context=mutation_context,
            )
            new_task = kb.get_task(conn, new_tid)
            task_status = new_task.status if new_task else None
            # --- vault doc impact gate (auto-insert curator before finalizer) ---
            vault_doc_impact = None
            if (
                new_task
                and parents
                and workflow_key
                and not triage
                and initial_status != "blocked"
            ):
                try:
                    from hermes_cli.kanban_vault_doc_impact import (
                        ensure_vault_doc_impact_for_task,
                    )
                    vault_doc_impact = ensure_vault_doc_impact_for_task(
                        conn,
                        new_task,
                        source="kanban_create_tool",
                    )
                except Exception:
                    logger.debug(
                        "vault_doc_impact gate skipped during kanban_create",
                        exc_info=True,
                    )
            # Upstream v2026.6.19 added native session auto-subscribe for
            # gateway/TUI sessions. Keep that boolean contract, and also keep
            # the Ahlnos fork's richer gateway-source/home-channel metadata so
            # orchestrator-created durable workflows still close the loop from
            # local/TUI/Telegram front doors.
            subscribed = _maybe_auto_subscribe(conn, new_tid)
            notification_state = _auto_subscribe_gateway_source(
                kb, conn, new_tid, kw.get("gateway_source"),
                session_subscribed=subscribed,
            )
            if not subscribed and isinstance(notification_state, dict):
                subscribed = bool(notification_state.get("subscribed"))
            skill_list = list(skills or []) if isinstance(skills, (list, tuple)) else []
            _append_kanban_route_telemetry(
                "route.selected",
                status=task_status,
                action_type="create_kanban_task",
                route={
                    "chosen_route": "kanban_create",
                    "assignee_profile": str(assignee),
                    "initial_status": str(initial_status),
                    "workspace_kind": str(workspace_kind),
                    "model_routing_preset": (model_routing_decision or {}).get("preset"),
                    "provider": (model_routing_decision or {}).get("provider"),
                    "model": (model_routing_decision or {}).get("model"),
                    "reasoning_effort": (model_routing_decision or {}).get("reasoning_effort"),
                    "model_override": model_override,
                    "explicit_override": bool(skill_list or max_runtime_seconds or workspace_path),
                },
                routing_decision=model_routing_decision,
                tree={
                    "task_id": new_tid,
                    "parent_task_ids": list(parents),
                    "parent_count": len(parents),
                },
                tooling={
                    "skills": skill_list,
                    "skill_count": len(skill_list),
                },
                input_shape={
                    "title_chars": len(str(title)),
                    "body_chars": len(str(body or "")),
                    "body_supplied": bool(body and str(body).strip()),
                },
                gates={
                    "privacy": "title/body/idempotency_key excluded from telemetry",
                    "lifecycle": "task body remains in Kanban DB; routing log stores metadata only",
                },
                priority=int(priority) if priority is not None else 0,
                tenant_present=bool(tenant),
            )
            return _ok(
                task_id=new_tid,
                status=task_status,
                subscribed=subscribed,
                vault_doc_impact=vault_doc_impact,
                model_override=model_override,
                model_provider_override=(model_routing_decision or {}).get("provider"),
                model_reasoning_effort=(model_routing_decision or {}).get("reasoning_effort"),
                model_routing=model_routing_decision,
                notifications=notification_state,
            )
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_create: {e}")
    except Exception as e:
        logger.exception("kanban_create failed")
        return tool_error(f"kanban_create: {e}")


def _maybe_auto_subscribe(conn: Any, task_id: str) -> bool:
    """Auto-subscribe the calling session to task completion / block events.

    Returns True if a subscription row was written, False otherwise (no
    session context, config gate disabled, or best-effort failure). The
    caller surfaces this in the ``subscribed`` field of the kanban_create
    response so an orchestrator can decide whether to fall back to an
    explicit ``kanban_notify-subscribe`` or to polling.

    Gated by ``kanban.auto_subscribe_on_create`` in config.yaml (default
    True). Disable to mirror pre-feature behaviour, e.g. when the
    originating user/chat opted out via the per-platform notification
    toggle (see ``hermes dashboard``).

    Subscription paths:

    - **Gateway** (telegram/discord/slack/etc): ``HERMES_SESSION_PLATFORM``
      and ``HERMES_SESSION_CHAT_ID`` are set in ContextVars by the
      messaging gateway before agent dispatch. The notification poller
      already keys off these, so we just register a row.

    - **TUI** (herm desktop / herm TUI): the platform/chat_id ContextVars
      are intentionally cleared (TUI is a single-channel local UI, not
      a multi-tenant chat surface), but the agent subprocess inherits
      ``HERMES_SESSION_KEY`` from the parent session. We subscribe with
      ``platform="tui"`` and ``chat_id=<key>``; the
      ``tui_gateway.server._poll_kanban_tui_subs`` consumer reads these
      ``kanban_notify_subs`` rows and posts completion messages into the
      running session.

    - **CLI / cron / test / unattached**: no persistent delivery channel,
      no-op.

    Failure mode: any exception inside the function is logged at WARNING
    with the offending exception + diagnostic env vars and swallowed.
    We never want a notification bookkeeping failure to fail the
    kanban_create that the agent is mid-conversation about.
    """
    try:
        cfg = load_config()
        if not cfg_get(cfg, "kanban", "auto_subscribe_on_create", default=True):
            return False
    except Exception:
        # If config can't load we still default to True — this is the
        # user-friendly behaviour that mirrors the pre-gate implementation.
        pass

    platform = ""
    chat_id = ""
    try:
        from gateway.session_context import get_session_env
        platform = get_session_env("HERMES_SESSION_PLATFORM", "") or os.environ.get("HERMES_SESSION_PLATFORM", "")
        chat_id = get_session_env("HERMES_SESSION_CHAT_ID", "") or os.environ.get("HERMES_SESSION_CHAT_ID", "")
        if not platform or not chat_id:
            # TUI / desktop fallback: platform/chat_id ContextVars are
            # cleared for TUI sessions, but the parent process exports
            # HERMES_SESSION_KEY into the subprocess env. Treat that
            # as a "tui" subscription so the TUI notification poller
            # (tui_gateway/server.py) can pick it up.
            #
            # HERMES_SESSION_ID is intentionally NOT a fallback here:
            # it is set by ACP / the agent subprocess for telemetry
            # regardless of whether the parent is a TUI or a CLI, so
            # treating it as a notification target would auto-subscribe
            # every CLI invocation, which is exactly the over-eager
            # behaviour that got #19718 reverted upstream. The TUI
            # poller keys on HERMES_SESSION_KEY.
            session_key = (
                get_session_env("HERMES_SESSION_KEY", "")
                or os.environ.get("HERMES_SESSION_KEY", "")
            )
            if not session_key:
                return False  # CLI / cron / test — no persistent channel
            platform = "tui"
            chat_id = session_key
        thread_id = get_session_env("HERMES_SESSION_THREAD_ID", "") or os.environ.get("HERMES_SESSION_THREAD_ID", "") or None
        user_id = get_session_env("HERMES_SESSION_USER_ID", "") or os.environ.get("HERMES_SESSION_USER_ID", "") or None
        notifier_profile = (
            get_session_env("HERMES_SESSION_PROFILE", "")
            or os.environ.get("HERMES_PROFILE")
        )

        # Lazy-import to keep the module-level dependency light
        from hermes_cli import kanban_db as _kb
        _kb.add_notify_sub(
            conn, task_id=task_id,
            platform=platform, chat_id=chat_id,
            thread_id=thread_id, user_id=user_id,
            notifier_profile=notifier_profile,
        )
        return True
    except Exception as _exc:
        logger.warning(
            "_maybe_auto_subscribe failed: %r (platform=%r key_set=%r)",
            _exc, platform, bool(chat_id),
        )
        return False


def _handle_unblock(args: dict, **kw) -> str:
    """Transition a blocked task back to ready."""
    guard = _require_orchestrator_tool("kanban_unblock")
    if guard:
        return guard
    tid = args.get("task_id")
    if not tid:
        return tool_error("task_id is required")
    ownership_err = _enforce_worker_task_ownership(str(tid))
    if ownership_err:
        return ownership_err
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            ok = kb.unblock_task(conn, str(tid))
            if not ok:
                return tool_error(f"could not unblock {tid} (not blocked or unknown)")
            return _ok(task_id=str(tid), status="ready")
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_unblock: {e}")
    except Exception as e:
        logger.exception("kanban_unblock failed")
        return tool_error(f"kanban_unblock: {e}")


def _handle_link(args: dict, **kw) -> str:
    """Add a parent→child dependency edge after the fact."""
    parent_id = args.get("parent_id")
    child_id = args.get("child_id")
    if not parent_id or not child_id:
        return tool_error("both parent_id and child_id are required")
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            kb.link_tasks(conn, parent_id=parent_id, child_id=child_id)
            return _ok(parent_id=parent_id, child_id=child_id)
        finally:
            conn.close()
    except ValueError as e:
        # Covers cycle + self-parent rejections
        return tool_error(f"kanban_link: {e}")
    except Exception as e:
        logger.exception("kanban_link failed")
        return tool_error(f"kanban_link: {e}")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

_DESC_TASK_ID_DEFAULT = (
    "Task id. If omitted, defaults to HERMES_KANBAN_TASK from the env "
    "(the task the dispatcher spawned you to work on)."
)

_DESC_BOARD = (
    "Kanban board slug to target. When omitted, the call resolves the "
    "active board the usual way: HERMES_KANBAN_DB env → "
    "HERMES_KANBAN_BOARD env → the 'current' symlink under the kanban "
    "home → 'default'. Pass an explicit slug only when the caller (e.g. "
    "a Telegram routing layer) needs to override the env-pinned active "
    "board for this one call."
)


def _board_schema_prop() -> dict[str, str]:
    """Schema fragment for the optional ``board`` parameter.

    Centralised so a future tweak to the description / validation hint
    only has to land in one place.
    """
    return {"type": "string", "description": _DESC_BOARD}

KANBAN_SHOW_SCHEMA = {
    "name": "kanban_show",
    "description": (
        "Read bounded task state. Workers receive one compact task identity "
        "plus the pre-formatted worker_context handoff; orchestrators receive "
        "compact dependency/latest-run status. Use detail='full' only for "
        "diagnosis when raw comments, attempts, and recent events are needed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "detail": {
                "type": "string",
                "enum": ["compact", "full"],
                "description": "Bounded compact view (default) or diagnostic full history.",
            },
            "board": _board_schema_prop(),
        },
        "required": [],
    },
}

KANBAN_LIST_SCHEMA = {
    "name": "kanban_list",
    "description": (
        "List Kanban task summaries so an orchestrator profile can discover "
        "work to route. Supports the same core filters as the CLI: assignee, "
        "status, tenant, workflow_key, include_archived, and limit. Returns compact rows "
        "with ids, title, status, assignee, priority, parent/child ids, and "
        "counts. Bounded to 50 rows by default, 200 max, with truncation "
        "metadata. Also recomputes ready tasks before listing, matching the "
        "CLI. Orchestrator-only — dispatcher-spawned task workers never see "
        "this tool."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "assignee": {
                "type": "string",
                "description": "Optional assignee/profile filter.",
            },
            "status": {
                "type": "string",
                "enum": [
                    "triage", "todo", "ready", "running",
                    "blocked", "done", "archived",
                ],
                "description": "Optional task status filter.",
            },
            "tenant": {
                "type": "string",
                "description": "Optional tenant/project namespace filter.",
            },
            "workflow_key": {
                "type": "string",
                "description": "Optional workflow instance key filter.",
            },
            "include_archived": {
                "type": "boolean",
                "description": "Include archived tasks. Defaults to false.",
            },
            "limit": {
                "type": "integer",
                "description": "Optional maximum rows to return (default 50, max 200).",
            },
            "board": _board_schema_prop(),
        },
        "required": [],
    },
}

KANBAN_COMPLETE_SCHEMA = {
    "name": "kanban_complete",
    "description": (
        "Mark your current task done with a structured handoff for "
        "downstream workers and humans. Prefer ``summary`` for a "
        "human-readable 1-3 sentence description of what you did; put "
        "machine-readable facts in ``metadata`` (changed_files, "
        "tests_run, decisions, findings, etc). At least one of "
        "``summary`` or ``result`` is required. If you created new "
        "tasks via ``kanban_create`` during this run, list their ids "
        "in ``created_cards`` — the kernel verifies them so phantom "
        "references are caught before they leak into downstream "
        "automation. If you produced deliverable files (charts, PDFs, "
        "spreadsheets, generated images), list their absolute paths "
        "in ``artifacts`` — the gateway notifier will upload them as "
        "native attachments to the human who subscribed to the task, "
        "so the deliverable lands in their chat alongside the summary "
        "instead of being a path they have to fetch by hand."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "summary": {
                "type": "string",
                "description": (
                    "Human-readable handoff, 1-3 sentences. Appears in "
                    "Run History on the dashboard and in downstream "
                    "workers' context."
                ),
            },
            "metadata": {
                "type": "object",
                "description": (
                    "Free-form dict of structured facts about this "
                    "attempt — {\"changed_files\": [...], \"tests_run\": 12, "
                    "\"findings\": [...]}. Surfaced to downstream "
                    "workers alongside ``summary``."
                ),
            },
            "result": {
                "type": "string",
                "description": (
                    "Short result log line (legacy field, maps to "
                    "task.result). Use ``summary`` instead when "
                    "possible; this exists for compatibility with "
                    "callers that still set --result on the CLI."
                ),
            },
            "created_cards": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional structured manifest of task ids you "
                    "created via ``kanban_create`` during this run. "
                    "The kernel verifies each id exists and was "
                    "created by this worker's profile; any phantom "
                    "id blocks the completion with an error listing "
                    "what went wrong (auditable in the task's events). "
                    "Only list ids you got back from a successful "
                    "``kanban_create`` call — do not invent or "
                    "remember ids from prose. Omit the field if you "
                    "did not create any cards."
                ),
            },
            "artifacts": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional list of absolute paths to deliverable "
                    "files you produced during this run — generated "
                    "charts, PDFs, spreadsheets, images, archives. "
                    "Examples: [\"/tmp/q3-revenue.png\", "
                    "\"/tmp/report.pdf\"]. The gateway notifier "
                    "uploads each path as a native attachment to the "
                    "subscribed chat (images embed inline, everything "
                    "else uploads as a file) so the deliverable "
                    "lands with the completion notification. Skip "
                    "intermediate scratch files and references that "
                    "are not the deliverable. The path must exist "
                    "on disk at completion. Files inside a managed scratch "
                    "workspace are copied to durable task attachments before "
                    "cleanup; a missing declared scratch artifact keeps the "
                    "task in-flight so you can fix the path and retry."
                ),
            },
            "board": _board_schema_prop(),
        },
        "required": [],
    },
}

KANBAN_BLOCK_SCHEMA = {
    "name": "kanban_block",
    "description": (
        "Stop work on this task and route it according to WHY you're stuck. "
        "Set ``kind`` to say which: 'dependency' (waiting on another task — "
        "goes to todo and auto-resumes when that task finishes, no human "
        "needed), 'needs_input' (you need a human decision/answer), "
        "'capability' (a hard wall: no access, missing credentials, an action "
        "no agent can do), or 'transient' (a flaky failure that may clear). "
        "``reason`` is shown to the human on the board. If a task keeps "
        "getting unblocked and re-blocked for the same reason, it is "
        "auto-escalated to triage. Use for genuine blockers only — don't "
        "block on things you can resolve yourself. When blocking after partial "
        "work, pass ``summary`` (1-3 sentence handoff) and ``metadata`` "
        "(structured facts: changed_files, tests_run, decisions, failure_code, "
        "etc.) so downstream workers and reviewers can see what happened."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "reason": {
                "type": "string",
                "description": (
                    "What you need answered or what stopped you, in one or "
                    "two sentences. Don't paste the whole conversation; the "
                    "human has the board and can ask follow-ups via comments."
                ),
            },
            "kind": {
                "type": "string",
                "enum": ["dependency", "needs_input", "capability", "transient"],
                "description": (
                    "Why you're blocked. 'dependency' waits in todo and "
                    "resumes automatically; the others surface to a human. "
                    "Omit only if none apply."
                ),
            },
            "summary": {
                "type": "string",
                "description": (
                    "Optional human-readable handoff, 1-3 sentences. "
                    "Persisted on the run row so downstream workers see "
                    "what happened before the block. Useful for "
                    "iteration-exhaustion and review-required blocks."
                ),
            },
            "metadata": {
                "type": "object",
                "description": (
                    "Optional free-form dict of structured facts — "
                    "{\"changed_files\": [...], \"tests_run\": 12, "
                    "\"failure_code\": \"iteration_budget_exhausted\", "
                    "…}. Persisted on the run row and shown in retry "
                    "context."
                ),
            },
            "board": _board_schema_prop(),
        },
        "required": ["reason"],
    },
}

KANBAN_HEARTBEAT_SCHEMA = {
    "name": "kanban_heartbeat",
    "description": (
        "Signal that you're still alive during a long operation "
        "(training, encoding, large crawls). Call every few minutes so "
        "humans see liveness separately from PID checks. Pure side "
        "effect — no work changes."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "note": {
                "type": "string",
                "description": (
                    "Optional short note describing current progress. "
                    "Shown in the event log."
                ),
            },
            "board": _board_schema_prop(),
        },
        "required": [],
    },
}

KANBAN_COMMENT_SCHEMA = {
    "name": "kanban_comment",
    "description": (
        "Append a comment to a task's thread. Use for durable notes "
        "that should outlive this run (questions for the next worker, "
        "partial findings, rationale). Reviews must also record P0/P1 "
        "findings in the typed blocker ledger; completion stays closed until "
        "every critical blocker has resolution evidence. Ephemeral reasoning "
        "doesn't belong here — use your normal response instead."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": (
                    "Task id. Required (may be your own task or "
                    "another's — comment threads are per-task)."
                ),
            },
            "body": {
                "type": "string",
                "description": "Markdown-supported comment body.",
            },
            "blocker": {
                "type": "object",
                "description": (
                    "Optional typed review finding to persist beside the "
                    "comment. Open P0/P1 findings mechanically block task "
                    "completion; P2/P3 remain advisory. Duplicate open "
                    "severity+title findings are idempotent."
                ),
                "properties": {
                    "severity": {
                        "type": "string",
                        "enum": ["P0", "P1", "P2", "P3"],
                    },
                    "title": {"type": "string"},
                    "details": {"type": "string"},
                    "evidence_ref": {
                        "type": "string",
                        "description": "Artifact, test, diff, log, or source reference supporting discovery.",
                    },
                },
                "required": ["severity", "title"],
            },
            "resolve_blocker_id": {
                "type": "integer",
                "description": "Typed blocker id to resolve after implementing its fix.",
            },
            "resolution_evidence_ref": {
                "type": "string",
                "description": "Required commit/test/artifact evidence when resolve_blocker_id is set.",
            },
            "board": _board_schema_prop(),
        },
        "required": ["task_id", "body"],
    },
}

KANBAN_ATTACH_SCHEMA = {
    "name": "kanban_attach",
    "description": (
        "Attach a file to a task by passing its bytes inline (base64). "
        "Use for genuine file artifacts the next worker or a human should "
        "be able to download — generated reports, images, exports. The "
        "file is stored as a real attachment (not a comment link) under "
        "the task's attachments dir, capped at 25 MB. Prefer "
        "kanban_attach_url when you only have a URL."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "filename": {
                "type": "string",
                "description": (
                    "File name to store it under (e.g. 'report.pdf'). "
                    "Directory components are stripped; only the leaf is kept."
                ),
            },
            "content_base64": {
                "type": "string",
                "description": "The file contents, base64-encoded. Max 25 MB decoded.",
            },
            "content_type": {
                "type": "string",
                "description": "Optional MIME type (e.g. 'application/pdf').",
            },
            "board": _board_schema_prop(),
        },
        "required": ["filename", "content_base64"],
    },
}

KANBAN_ATTACH_URL_SCHEMA = {
    "name": "kanban_attach_url",
    "description": (
        "Attach a file to a task by URL — Hermes downloads it server-side "
        "and stores it as a real attachment (capped at 25 MB). Use when "
        "you have a link rather than the bytes. Only http/https URLs are "
        "accepted."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "url": {
                "type": "string",
                "description": "http(s) URL to fetch and store.",
            },
            "filename": {
                "type": "string",
                "description": (
                    "Optional name to store it under. Defaults to the URL "
                    "path's leaf component."
                ),
            },
            "content_type": {
                "type": "string",
                "description": (
                    "Optional MIME type override. Defaults to the "
                    "Content-Type the server returns."
                ),
            },
            "board": _board_schema_prop(),
        },
        "required": ["url"],
    },
}

KANBAN_ATTACHMENTS_SCHEMA = {
    "name": "kanban_attachments",
    "description": (
        "List the files attached to a task: id, filename, content_type, "
        "size, who uploaded it, and the absolute on-disk path you can read."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "board": _board_schema_prop(),
        },
        "required": [],
    },
}

KANBAN_REQUEST_REWORK_SCHEMA = {
    "name": "kanban_request_rework",
    "description": (
        "Reviewer/verifier terminal action. Atomically record a finding, "
        "create or adopt exactly one fix card, link fix→review, close the "
        "current review run, and park or re-arm the review card. Use this "
        "when changes are requested; use kanban_block with kind='needs_input' "
        "only when a genuine human decision is required. request_key is "
        "mandatory so retries cannot duplicate the fix or edge."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "finding": {
                "type": "string",
                "description": "The concrete review defect the fix must address.",
            },
            "request_key": {
                "type": "string",
                "description": "Stable caller-generated idempotency key for this finding.",
            },
            "fix_task_id": {
                "type": "string",
                "description": "Adopt this existing fix card; mutually exclusive with fix.",
            },
            "fix": {
                "type": "object",
                "description": "Create a new fix card; mutually exclusive with fix_task_id.",
                "properties": {
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                    "assignee": {"type": "string"},
                    "workspace_kind": {"type": "string"},
                    "workspace_path": {"type": "string"},
                    "project_id": {"type": "string"},
                    "branch_name": {"type": "string"},
                    "priority": {"type": "integer"},
                    "skills": {"type": "array", "items": {"type": "string"}},
                    "toolsets": {"type": "array", "items": {"type": "string"}},
                    "max_runtime_seconds": {"type": "integer"},
                },
                "required": ["title", "assignee"],
            },
            "summary": {
                "type": "string",
                "description": "Optional concise handoff for the fix worker.",
            },
            "metadata": {
                "type": "object",
                "description": "Optional structured review metadata.",
            },
            "board": _board_schema_prop(),
        },
        "required": ["finding", "request_key"],
        "oneOf": [
            {"required": ["fix_task_id"]},
            {"required": ["fix"]},
        ],
    },
}

KANBAN_REQUEST_PUBLICATION_SCHEMA = {
    "name": "kanban_request_publication",
    "description": (
        "Coder terminal action for a committed change that this lane cannot "
        "publish. Atomically create or adopt a releaser publication card, "
        "link it as parent, close this run, and park this task until the "
        "publication card proves the target remote ref contains the exact "
        "commit SHA. request_key, expected_sha, workspace_path, and remote_ref "
        "are durable handoff evidence; a worker report cannot satisfy the "
        "completion readback. Use kanban_block(kind='capability') only when "
        "no lane can perform the missing action."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "request_key": {
                "type": "string",
                "description": "Mandatory stable idempotency key for this handoff.",
            },
            "publication_task_id": {
                "type": "string",
                "description": "Adopt an existing publication card.",
            },
            "publisher_task_id": {
                "type": "string",
                "description": "Alias for publication_task_id.",
            },
            "expected_sha": {
                "type": "string",
                "description": "Exact local commit SHA to publish.",
            },
            "workspace_path": {
                "type": "string",
                "description": "Absolute coder checkout containing the commit.",
            },
            "remote": {
                "type": "string",
                "description": "Git remote name or URL; defaults to origin.",
            },
            "remote_ref": {
                "type": "string",
                "description": "Target remote ref, e.g. refs/heads/main.",
            },
            "title": {"type": "string"},
            "body": {"type": "string"},
            "summary": {
                "type": "string",
                "description": "Optional concise handoff for the releaser.",
            },
            "metadata": {
                "type": "object",
                "description": "Optional structured commit and verification facts.",
            },
            "board": _board_schema_prop(),
        },
        "required": ["request_key"],
        "oneOf": [
            {"required": ["publication_task_id"]},
            {"required": ["publisher_task_id"]},
            {"required": ["expected_sha", "workspace_path", "remote_ref"]},
        ],
    },
}

KANBAN_CREATE_SCHEMA = {
    "name": "kanban_create",
    "description": (
        "Create a new kanban task, optionally as a child of the current "
        "one (pass the current task id in ``parents``). Used by "
        "orchestrator workers to fan out — decompose work into child "
        "tasks with specific assignees, link them into a pipeline, "
        "then complete your own task. The dispatcher picks up the new "
        "tasks on its next tick and spawns the assigned profiles."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Short task title (required).",
            },
            "assignee": {
                "type": "string",
                "description": (
                    "Profile name that should execute this task "
                    "(e.g. 'researcher-a', 'reviewer', 'writer'). "
                    "Required — tasks without an assignee are never "
                    "dispatched."
                ),
            },
            "body": {
                "type": "string",
                "description": (
                    "Opening post: full spec, acceptance criteria, "
                    "links. The assigned worker reads this as part of "
                    "its context."
                ),
            },
            "parents": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Parent task ids. The new task stays in 'todo' "
                    "until every parent reaches 'done'; then it "
                    "auto-promotes to 'ready'. Typical fan-in: list "
                    "all the researcher task ids when creating a "
                    "synthesizer task."
                ),
            },
            "tenant": {
                "type": "string",
                "description": (
                    "Optional namespace for multi-project isolation. "
                    "Defaults to HERMES_TENANT env if set."
                ),
            },
            "priority": {
                "type": "integer",
                "description": (
                    "Dispatcher tiebreaker. Higher = picked sooner "
                    "when multiple ready tasks share an assignee."
                ),
            },
            "workspace_kind": {
                "type": "string",
                "enum": ["scratch", "dir", "worktree"],
                "description": (
                    "Workspace flavor: 'scratch' (fresh tmp dir, "
                    "default), 'dir' (shared directory, requires "
                    "absolute workspace_path), 'worktree' (git worktree). "
                    "For repository implementation, use project (preferred) "
                    "or an explicit dir/worktree path; scratch is isolated and "
                    "cannot access another repository."
                ),
            },
            "workspace_path": {
                "type": "string",
                "description": (
                    "Absolute path for 'dir' or 'worktree' workspace. "
                    "Relative paths are rejected at dispatch."
                ),
            },
            "project": {
                "type": "string",
                "description": (
                    "Optional project id or slug to link the task to. When "
                    "set, the task becomes a git worktree under the project's "
                    "primary repo with a deterministic branch (project slug + "
                    "task id), instead of a random branch. Use this for repo "
                    "implementation cards so the worker receives an explicit, "
                    "scoped writable worktree."
                ),
            },
            "triage": {
                "type": "boolean",
                "description": (
                    "If true, task lands in 'triage' instead of 'todo' "
                    "— a specifier profile is expected to flesh out "
                    "the body before work starts."
                ),
            },
            "idempotency_key": {
                "type": "string",
                "description": (
                    "If a non-archived task with this key already "
                    "exists, return that task's id instead of creating "
                    "a duplicate. Useful for retry-safe automation."
                ),
            },
            "max_runtime_seconds": {
                "type": "integer",
                "description": (
                    "Per-task runtime cap. When exceeded, the "
                    "dispatcher SIGTERMs the worker and re-queues the "
                    "task with outcome='timed_out'."
                ),
            },
            "initial_status": {
                "type": "string",
                "enum": ["running", "blocked"],
                "description": (
                    "Initial card status. Use 'blocked' for tasks that "
                    "require immediate human ops (R3 gate) to skip the "
                    "brief running-to-blocked transition. Defaults to "
                    "'running', which preserves the usual dispatch path."
                ),
            },
            "workflow_key": {
                "type": "string",
                "description": (
                    "Optional grouping key for tasks that belong to the "
                    "same orchestrator workflow. All tasks sharing a key "
                    "can be listed with ``hermes kanban workflow <key>``. "
                    "Use this to tag child tasks created in a fan-out so "
                    "the originating chat receives a unified status report "
                    "when the finalizer task completes."
                ),
            },
            "current_step_key": {
                "type": "string",
                "description": (
                    "Optional step key identifying the current phase of "
                    "a workflow (e.g. 'finalizer', 'synthesizer', "
                    "'implementation'). The vault-doc-impact gate uses "
                    "this to detect finalizer tasks that may need "
                    "documentation curation before completion."
                ),
            },
            "skills": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Skill names to force-load into the dispatched "
                    "worker. The kanban lifecycle is already injected "
                    "automatically; use this to pin a task to a specialist "
                    "context — e.g. ['translation'] for a translation "
                    "task, ['github-code-review'] for a reviewer task. "
                    "The names must match skills installed on the "
                    "assignee's profile."
                ),
            },
            "toolsets": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional non-empty task-scoped runtime capabilities, "
                    "for example ['terminal', 'file'] for an implementation "
                    "worker or ['web'] for public research. When supplied, "
                    "these replace the assignee profile's broad CLI toolsets; "
                    "Kanban lifecycle tools remain mandatory."
                ),
            },
            "model_override": {
                "type": "string",
                "description": (
                    "Optional model identifier to override the assignee profile's "
                    "default model for this task. Examples: 'deepseek/deepseek-v4-flash:free', "
                    "'deepseek-v4-pro', 'gpt-5.5'. The dispatcher passes it to the worker "
                    "as the model argument. Leave empty to use the profile default."
                ),
            },
            "model_routing": {
                "type": "string",
                "description": (
                    "Optional routing preset to resolve provider, model_override, "
                    "and reasoning effort from the profile model-routing table, "
                    "e.g. 'front_door', 'repo_publish', 'repo_verification', "
                    "'verification_leaf', 'public_research', 'code_implementation', "
                    "'architecture_design', or 'uncertainty_escalation'. "
                    "Legacy 'front_door_or_uncertain' resolves to front_door/high. "
                    "Explicit model_override wins the "
                    "model id while the preset still supplies provider/effort."
                ),
            },
            "task_category": {
                "type": "string",
                "description": "Optional route classification/category for telemetry and quota policy.",
            },
            "data_sensitivity": {
                "type": "string",
                "description": (
                    "Data sensitivity classification such as public, private, "
                    "business, PII, security, finance, legal, or credential-adjacent. "
                    "Sensitive classes are never routed to public/free providers."
                ),
            },
            "verifiability": {
                "type": "string",
                "description": (
                    "How independently verifiable the task output is (high, low, "
                    "tests, readback, deterministic). Used for quota-pressure downgrades."
                ),
            },
            "quota_policy": {
                "type": "string",
                "description": (
                    "Optional quota behavior hint, e.g. 'downgrade_verifiable' "
                    "to allow cheap budget routes for high-verifiability public work."
                ),
            },
            "goal_mode": {
                "type": "boolean",
                "description": (
                    "Run the dispatched worker in a goal loop. When true, "
                    "after each turn an auxiliary judge checks the worker's "
                    "response against this card's title/body; if the work "
                    "isn't done and budget remains, the worker keeps going "
                    "in the same session until the judge agrees it's "
                    "complete (or the goal-turn budget is exhausted, which "
                    "blocks the task for human review). Use this for "
                    "open-ended cards where one shot rarely finishes the "
                    "work. Defaults to false (classic single-shot worker)."
                ),
            },
            "goal_max_turns": {
                "type": "integer",
                "description": (
                    "Turn budget for goal_mode workers. Caps how many "
                    "continuation turns the worker may take before the task "
                    "is blocked for review. Ignored unless goal_mode is "
                    "true. Defaults to the goal-engine default (20)."
                ),
            },
            "board": _board_schema_prop(),
        },
        "required": ["title", "assignee"],
    },
}

KANBAN_COMPILE_WORKFLOW_SCHEMA = {
    "name": "kanban_compile_workflow",
    "description": (
        "Atomically validate and create a complete dependency workflow. Use for "
        "every multi-card decomposition: all tasks, links, the idempotency record, "
        "and one trusted terminal notification commit together or none do. The "
        "graph must converge on exactly one terminal finalizer, synthesizer, or "
        "reporter. Orchestrator-only."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "workflow_key": {
                "type": "string",
                "description": "Stable semantic identity for this workflow instance.",
            },
            "idempotency_key": {
                "type": "string",
                "description": "Stable retry identity for this exact graph specification.",
            },
            "steps": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {
                            "type": "string",
                            "description": "Stable step key used by other steps' parents.",
                        },
                        "title": {"type": "string"},
                        "body": {"type": "string"},
                        "assignee": {
                            "type": "string",
                            "description": "Existing Hermes profile that executes this step.",
                        },
                        "parents": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Predecessor step keys, never task ids.",
                        },
                        "role": {
                            "type": "string",
                            "description": "Semantic role; terminal must be finalizer, synthesizer, or reporter.",
                        },
                        "terminal": {
                            "type": "boolean",
                            "description": "True on exactly one convergent final step.",
                        },
                        "initial_status": {
                            "type": "string",
                            "enum": ["ready", "todo", "done"],
                        },
                        "result": {
                            "type": "string",
                            "description": "Required handoff text for a precompleted root step.",
                        },
                        "skills": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "toolsets": {
                            "type": "array",
                            "minItems": 1,
                            "items": {"type": "string"},
                            "description": (
                                "Exact task-scoped runtime capabilities; "
                                "Kanban lifecycle tools are appended."
                            ),
                        },
                        "max_runtime_seconds": {"type": "integer"},
                        "priority": {"type": "integer"},
                        "model_override": {
                            "type": "string",
                            "description": "Optional exact model id; use model_routing for a policy lane.",
                        },
                        "model_routing": {
                            "type": "string",
                            "description": "Routing lane such as code_implementation or verification_leaf.",
                        },
                        "task_category": {"type": "string"},
                        "data_sensitivity": {"type": "string"},
                        "verifiability": {"type": "string"},
                        "quota_policy": {"type": "string"},
                    },
                    "required": ["key", "title", "assignee"],
                    "additionalProperties": False,
                },
            },
            "tenant": {"type": "string"},
            "workspace_kind": {
                "type": "string",
                "enum": ["scratch", "dir", "worktree"],
            },
            "workspace_path": {"type": "string"},
            "priority": {"type": "integer"},
            "board": _board_schema_prop(),
        },
        "required": ["workflow_key", "idempotency_key", "steps"],
        "additionalProperties": False,
    },
}

KANBAN_UNBLOCK_SCHEMA = {
    "name": "kanban_unblock",
    "description": (
        "Move a blocked Kanban task back to ready. Orchestrator-only — only "
        "profiles with the kanban toolset can unblock routed work; "
        "dispatcher-spawned task workers never see this tool."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "Blocked task id to return to ready.",
            },
            "board": _board_schema_prop(),
        },
        "required": ["task_id"],
    },
}

KANBAN_LINK_SCHEMA = {
    "name": "kanban_link",
    "description": (
        "Add a parent→child dependency edge after both tasks already "
        "exist. The child won't promote to 'ready' until all parents "
        "are 'done'. Cycles and self-links are rejected."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "parent_id": {"type": "string", "description": "Parent task id."},
            "child_id":  {"type": "string", "description": "Child task id."},
            "board": _board_schema_prop(),
        },
        "required": ["parent_id", "child_id"],
    },
}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

registry.register(
    name="kanban_show",
    toolset="kanban",
    schema=KANBAN_SHOW_SCHEMA,
    handler=_handle_show,
    check_fn=_check_kanban_mode,
    emoji="📋",
)

registry.register(
    name="kanban_list",
    toolset="kanban",
    schema=KANBAN_LIST_SCHEMA,
    handler=_handle_list,
    check_fn=_check_kanban_orchestrator_mode,
    emoji="📋",
)

registry.register(
    name="kanban_complete",
    toolset="kanban",
    schema=KANBAN_COMPLETE_SCHEMA,
    handler=_handle_complete,
    check_fn=_check_kanban_mode,
    emoji="✔",
)

registry.register(
    name="kanban_block",
    toolset="kanban",
    schema=KANBAN_BLOCK_SCHEMA,
    handler=_handle_block,
    check_fn=_check_kanban_mode,
    emoji="⏸",
)

registry.register(
    name="kanban_request_rework",
    toolset="kanban",
    schema=KANBAN_REQUEST_REWORK_SCHEMA,
    handler=_handle_request_rework,
    check_fn=_check_kanban_mode,
    emoji="🔁",
)

registry.register(
    name="kanban_request_publication",
    toolset="kanban",
    schema=KANBAN_REQUEST_PUBLICATION_SCHEMA,
    handler=_handle_request_publication,
    check_fn=_check_kanban_mode,
    emoji="📤",
)

registry.register(
    name="kanban_heartbeat",
    toolset="kanban",
    schema=KANBAN_HEARTBEAT_SCHEMA,
    handler=_handle_heartbeat,
    check_fn=_check_kanban_mode,
    emoji="💓",
)

registry.register(
    name="kanban_comment",
    toolset="kanban",
    schema=KANBAN_COMMENT_SCHEMA,
    handler=_handle_comment,
    check_fn=_check_kanban_mode,
    emoji="💬",
)

registry.register(
    name="kanban_attach",
    toolset="kanban",
    schema=KANBAN_ATTACH_SCHEMA,
    handler=_handle_attach,
    check_fn=_check_kanban_mode,
    emoji="📎",
)

registry.register(
    name="kanban_attach_url",
    toolset="kanban",
    schema=KANBAN_ATTACH_URL_SCHEMA,
    handler=_handle_attach_url,
    check_fn=_check_kanban_mode,
    emoji="📎",
)

registry.register(
    name="kanban_attachments",
    toolset="kanban",
    schema=KANBAN_ATTACHMENTS_SCHEMA,
    handler=_handle_attachments,
    check_fn=_check_kanban_mode,
    emoji="📎",
)

registry.register(
    name="kanban_create",
    toolset="kanban",
    schema=KANBAN_CREATE_SCHEMA,
    handler=_handle_create,
    check_fn=_check_kanban_mode,
    emoji="➕",
)

registry.register(
    name="kanban_compile_workflow",
    toolset="kanban",
    schema=KANBAN_COMPILE_WORKFLOW_SCHEMA,
    handler=_handle_compile_workflow,
    check_fn=_check_kanban_orchestrator_mode,
    emoji="🧭",
)

registry.register(
    name="kanban_unblock",
    toolset="kanban",
    schema=KANBAN_UNBLOCK_SCHEMA,
    handler=_handle_unblock,
    check_fn=_check_kanban_orchestrator_mode,
    emoji="▶",
)

registry.register(
    name="kanban_link",
    toolset="kanban",
    schema=KANBAN_LINK_SCHEMA,
    handler=_handle_link,
    check_fn=_check_kanban_mode,
    emoji="🔗",
)
