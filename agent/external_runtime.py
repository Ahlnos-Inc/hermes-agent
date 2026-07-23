"""Provider-neutral whole-agent runtime attempts used by the main loop."""

from __future__ import annotations

import os
import platform
import re
import stat
import json
import logging
import time
from pathlib import Path
from typing import Any

from agent.claude_cli_boundary import (
    ClaudeAttestationError,
    ClaudeAttestationRejectedError,
    attest_claude_max_auth,
    create_exact_env_cli_wrapper,
    invalidate_claude_auth_attestation,
)
from agent.claude_agent_runtime import ClaudeProjection, RuntimeFailure
from agent.claude_sdk_session import (
    ClaudeAgentSdkSession,
    build_claude_agent_options,
    load_claude_agent_sdk,
)
from agent.error_classifier import FailoverReason, classify_api_error
from agent.request_budgets import resolve_attempt_budgets
from agent.claude_subscription_env import build_claude_subscription_env
from agent.claude_workspace_terminal import (
    WorkspaceBoundaryProvisioningError,
    _selected_git,
    build_workspace_seatbelt_profile,
    prepare_workspace_terminal_boundary,
)
from agent.claude_workspace_files import WorkspaceFileBroker
from hermes_constants import get_hermes_home, get_host_user_home


_runtime_events_logger = logging.getLogger("hermes.runtime_events")
_CLAUDE_SDK_TEMP_ROOT = Path("/tmp")
# A successful auth probe may be reused only across the few synchronous steps
# between runtime preparation and construction of that exact SDK session.
# A different route, or a delayed session construction, always re-probes with
# the boundary cache disabled.
_CLAUDE_NEW_SESSION_ATTESTATION_GRACE_SECONDS = 5.0


def _effective_uid() -> int:
    """Return the POSIX effective uid behind the macOS-only SDK boundary."""

    get_effective_uid = getattr(os, "geteuid", None)
    if not callable(get_effective_uid):
        raise RuntimeError("Claude SDK filesystem isolation requires a POSIX effective uid")
    return int(get_effective_uid())


def _prepare_owner_only_directory(path: Path, *, label: str) -> Path:
    """Create or validate an exact private runtime directory without repair."""

    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.lstat()
    except OSError as exc:
        raise RuntimeError(f"{label} is not owner-only: {path}") from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != _effective_uid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise RuntimeError(f"{label} is not owner-only: {path}")
    return path


def prepare_claude_sdk_temp_dir(*, temp_root: str | Path | None = None) -> Path:
    """Return Claude Code's fixed owner-only per-user temporary directory.

    Claude Code initializes ``/tmp/claude-<uid>`` before honoring ``TMPDIR``.
    Keep that compatibility path private and grant Seatbelt access to only that
    descendant rather than broad write access to ``/tmp``.
    """

    root = Path(temp_root) if temp_root is not None else _CLAUDE_SDK_TEMP_ROOT
    return _prepare_owner_only_directory(
        root / f"claude-{_effective_uid()}", label="Claude SDK temp directory"
    )


def _prepare_claude_worker_tmp_dir(workspace: Path) -> Path:
    """Return the ordinary, isolated TMPDIR for one Claude worker workspace."""

    return _prepare_owner_only_directory(
        workspace / ".hermes-claude-runtime" / "tmp",
        label="Claude worker temp directory",
    )


def _emit_runtime_event(agent: Any, event: str, **fields: Any) -> None:
    """Log a structured external-runtime event and surface significant state."""

    payload = {
        "event": event,
        "ts": time.time(),
        "provider": str(getattr(agent, "provider", "") or ""),
        "model": str(getattr(agent, "model", "") or ""),
        "runtime": str(getattr(agent, "runtime", "hermes") or "hermes"),
        **fields,
    }
    _runtime_events_logger.info(json.dumps(payload, default=str, sort_keys=True))
    status = {
        "runtime_attempt_failure": f"Runtime attempt failed: {fields.get('reason', 'unknown')}",
        "runtime_circuit_open": "Runtime circuit opened; trying fallback.",
        "runtime_fallback_activated": "Runtime fallback activated.",
    }.get(event)
    if status:
        try:
            agent._emit_status(status)
        except Exception:
            pass


def _claude_effort(agent: Any) -> str | None:
    try:
        from agent.routing_contract import active_reasoning_effort

        effort = str(active_reasoning_effort(agent) or "").lower()
    except Exception:
        return None
    if effort in {"low", "medium", "high", "xhigh", "max"}:
        return effort
    return None


def _claude_workspace(agent: Any) -> Path:
    configured = os.getenv("HERMES_KANBAN_WORKSPACE") or getattr(
        agent, "session_cwd", None
    )
    if configured:
        return Path(configured).expanduser().resolve()
    from agent.runtime_cwd import resolve_agent_cwd

    return Path(resolve_agent_cwd()).expanduser().resolve()


def _sandbox_extra_read_paths() -> tuple[str, ...]:
    """Resolve least-privilege extra Seatbelt read paths for the terminal tool.

    Sources (merged): the active profile's ``model.sandbox_extra_read_paths``
    config list, plus the colon-separated ``HERMES_SANDBOX_EXTRA_READ_PATHS``
    env passthrough a kanban worker spawn can set per task. Both unset ->
    empty tuple -> sandbox behavior unchanged (default-deny outside the
    workspace, as today).
    """

    from hermes_cli.config import cfg_get, load_config_readonly

    configured = cfg_get(
        load_config_readonly(), "model", "sandbox_extra_read_paths", default=()
    )
    paths = [str(path) for path in configured] if isinstance(configured, (list, tuple)) else []
    env_value = os.getenv("HERMES_SANDBOX_EXTRA_READ_PATHS", "")
    paths.extend(part for part in env_value.split(":") if part)
    return tuple(paths)


def _claude_project_state_dir(host_home: Path, workspace: Path) -> Path:
    project_key = re.sub(r"[^A-Za-z0-9]", "-", str(workspace))
    path = host_home / ".claude" / "projects" / project_key
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def _load_persisted_claude_session_id(agent: Any) -> str | None:
    db = getattr(agent, "_session_db", None)
    session_id = getattr(agent, "session_id", None)
    if db is None or not session_id:
        return None
    try:
        row = db.get_session(session_id) or {}
        raw = row.get("model_config")
        config = raw if isinstance(raw, dict) else json.loads(raw or "{}")
        value = str(config.get("claude_session_id") or "").strip()
        return value or None
    except Exception:
        return None


def _persist_claude_session_id(agent: Any, claude_session_id: str | None) -> None:
    if not claude_session_id:
        return
    agent._claude_resume_session_id = claude_session_id
    db = getattr(agent, "_session_db", None)
    session_id = getattr(agent, "session_id", None)
    if db is None or not session_id or not hasattr(db, "update_session_meta"):
        return
    try:
        row = db.get_session(session_id) or {}
        raw = row.get("model_config")
        config = raw if isinstance(raw, dict) else json.loads(raw or "{}")
        config = dict(config or {})
        config["claude_session_id"] = claude_session_id
        db.update_session_meta(
            session_id,
            json.dumps(config, sort_keys=True),
            str(getattr(agent, "model", "") or "") or None,
        )
    except Exception:
        pass


def prepare_claude_agent_sdk_runtime(agent: Any) -> RuntimeFailure | None:
    """Attest and sandbox the subscription runtime before circuit lookup."""

    if getattr(agent, "_claude_runtime_context", None) is not None:
        return None
    kanban_task_id = os.getenv("HERMES_KANBAN_TASK", "").strip() or None
    if not kanban_task_id:
        return RuntimeFailure(
            FailoverReason.auth_permanent,
            "Claude Agent SDK runtime currently supports Kanban workers only",
        )
    try:
        sdk = load_claude_agent_sdk()
        host_home = get_host_user_home()
        if not host_home:
            raise RuntimeError(
                "Claude subscription runtime could not resolve its HOME boundary"
            )
        host_home = Path(host_home).expanduser().resolve()
        workspace = _claude_workspace(agent)
        if platform.system() != "Darwin":
            raise RuntimeError(
                "Claude subscription worker filesystem isolation is supported on macOS only"
            )
        exact_env = build_claude_subscription_env(os.environ, host_home=host_home)
        boundary = prepare_workspace_terminal_boundary(
            workspace,
            git=_selected_git(str(exact_env.get("PATH", ""))),
        )
        sdk_root = Path(sdk.__file__).resolve().parent
        cli_name = "claude.exe" if os.name == "nt" else "claude"
        bundled_cli = sdk_root / "_bundled" / cli_name
        claude_tmp = prepare_claude_sdk_temp_dir()
        worker_tmp = _prepare_claude_worker_tmp_dir(workspace)
        exact_env["TMPDIR"] = str(worker_tmp)
        sandbox_profile = build_workspace_seatbelt_profile(
            workspace=workspace,
            host_home=host_home,
            allow_network=True,
            readable_roots=[sdk_root],
            restrict_reads=False,
            control_write_paths=[
                host_home / ".claude.json",
                host_home / ".claude.json.lock",
            ],
            control_write_roots=[
                _claude_project_state_dir(host_home, workspace),
                claude_tmp,
            ],
            denied_write_roots=list(boundary.readonly_subtrees),
        )
        cli_wrapper = create_exact_env_cli_wrapper(
            bundled_cli,
            exact_env,
            get_hermes_home() / "cache" / "claude-agent-sdk" / "launchers",
            sandbox_profile=sandbox_profile,
        )
        attestation = attest_claude_max_auth(cli_wrapper, cache_ttl_seconds=0)
    except WorkspaceBoundaryProvisioningError as exc:
        agent._claude_boundary_provisioning_failure = str(exc)
        return RuntimeFailure(
            FailoverReason.unknown,
            str(exc),
            provisioning=True,
        )
    except Exception as exc:
        if isinstance(exc, ClaudeAttestationError):
            _runtime_events_logger.warning(
                "claude_auth_attestation_failure %s",
                json.dumps(
                    {
                        **exc.diagnostic,
                        "permanent": bool(exc.permanent),
                        "kanban_worker": bool(kanban_task_id),
                        "host_home_resolved": bool(locals().get("host_home")),
                        "wrapper_created": bool(locals().get("cli_wrapper")),
                    },
                    sort_keys=True,
                ),
            )
            return RuntimeFailure(
                FailoverReason.auth_permanent
                if isinstance(exc, ClaudeAttestationRejectedError)
                # A transient attestation (unsettled / unconfirmed subscription
                # state) is a provider-availability outage, not a permanent
                # rejection — route it as retryable ``auth`` so a token-refresh
                # blip cannot fail-close and self-arrest the card.
                else FailoverReason.auth,
                str(exc),
            )
        classified = classify_api_error(
            exc,
            provider=str(getattr(agent, "provider", "") or ""),
            model=str(getattr(agent, "model", "") or ""),
        )
        return RuntimeFailure(classified.reason, classified.message or str(exc))
    agent._claude_max_attestation = attestation
    agent._claude_cli_wrapper = str(cli_wrapper)
    agent._claude_runtime_context = {
        "sdk": sdk,
        "host_home": host_home,
        "workspace": workspace,
        "workspace_boundary": boundary,
        "cli_wrapper": cli_wrapper,
        "kanban_task_id": kanban_task_id,
        "attested_route": (
            str(getattr(agent, "provider", "") or ""),
            str(getattr(agent, "model", "") or ""),
        ),
        "attested_at": time.monotonic(),
    }
    return None


def persist_claude_workspace_boundary_block(
    agent: Any,
    failure: RuntimeFailure,
) -> bool:
    """Fence a quiet Kanban worker when boundary provisioning exhausts fallback."""

    if not failure.provisioning or not getattr(agent, "quiet_mode", False):
        return False
    task_id = os.getenv("HERMES_KANBAN_TASK", "").strip()
    raw_run_id = os.getenv("HERMES_KANBAN_RUN_ID", "").strip()
    if not task_id or not raw_run_id:
        return False
    try:
        run_id = int(raw_run_id)
    except ValueError:
        return False
    try:
        from hermes_cli import kanban_db

        with kanban_db.connect_closing() as conn:
            return kanban_db.block_task(
                conn,
                task_id,
                reason=(f"Claude workspace boundary unavailable: {failure.message}")[:160],
                summary="Worker was fenced before the first model turn.",
                metadata={
                    "failure_code": "workspace_boundary_provisioning",
                    "message": failure.message,
                },
                kind="capability",
                expected_run_id=run_id,
            )
    except Exception:
        _runtime_events_logger.warning(
            "claude_workspace_boundary_block_failed task=%s",
            task_id,
            exc_info=True,
        )
        return False


# Short-transient provider-availability reasons for which a fallback-exhausted
# quiet worker should requeue its card WITHOUT counting a failure. ``auth`` is
# the incident case — a transient Claude Max attestation now lands here (see
# prepare_claude_agent_sdk_runtime). Deliberately EXCLUDED:
#   * auth_permanent / model_not_found / provider_policy_blocked — genuinely
#     terminal, unchanged-request failures that must count / block, never spin.
def _worker_availability_defer_reasons() -> "frozenset[FailoverReason]":
    return frozenset(
        {
            FailoverReason.auth,
            FailoverReason.overloaded,
            FailoverReason.server_error,
            FailoverReason.timeout,
            FailoverReason.provider_unavailable,
        }
    )


# Provider QUOTA walls that ALSO warrant a no-failure self-defer (BUILD-734).
# The exit-75 EX_TEMPFAIL path already gives these a 300s cooldown, but a
# session-detached quiet worker whose exit code the reaper cannot read still
# self-arrests (cf++ → blocked) on the unreliable exit channel. Deferring the
# card here too makes quota recovery independent of exit-code classification —
# spaced by the SAME long (~300s) cooldown, not the 30s delivery cooldown, so a
# quota window is probed cheaply rather than thrashed (that cooldown split is
# resolved by the QUOTA_UNAVAILABLE outcome in check_respawn_guard).
def _worker_quota_defer_reasons() -> "frozenset[FailoverReason]":
    return frozenset(
        {
            FailoverReason.rate_limit,
            FailoverReason.billing,
            FailoverReason.upstream_rate_limit,
        }
    )


def persist_claude_worker_availability_defer(
    agent: Any,
    failure: RuntimeFailure,
) -> bool:
    """Requeue a fallback-exhausted quiet Kanban worker without a failure.

    When the Claude runtime exhausts its fallback chain on a transient
    provider-availability reason (a token-refresh attestation blip, a quota
    wall, a provider outage) the worker process is about to exit. Left alone,
    the dispatcher reaper later infers a generic crash from the dead PID and
    increments ``consecutive_failures`` — two of those self-arrest the card
    even though nothing about the task is wrong. Durably deferring the card
    here (kernel-owned no-failure requeue) makes recovery independent of how
    the reaper classifies the exit, so a temporary auth/availability window
    can never whack-a-mole the card into a permanent block.
    """

    if getattr(failure, "provisioning", False):
        return False
    # Classify from the TURN-level resolved reason, not just this last failure:
    # a quota wall hit on an earlier fallback (then masked by a trailing timeout
    # / provider outage) must still land on the long quota cooldown, not the
    # short 30s one. ``RouteFailureLedger.resolve`` preserves a concrete quota
    # value when one was seen this turn and otherwise collapses to
    # ``provider_unavailable`` (BUILD-734 / Sol review).
    ledger = getattr(agent, "_turn_route_failures", None)
    resolved = (
        ledger.resolve(failure.reason)
        if ledger is not None
        else failure.reason.value
    )
    quota_values = {r.value for r in _worker_quota_defer_reasons()}
    availability_values = {r.value for r in _worker_availability_defer_reasons()}
    is_quota = resolved in quota_values
    if resolved not in quota_values and resolved not in availability_values:
        return False
    if not getattr(agent, "quiet_mode", False):
        return False
    task_id = os.getenv("HERMES_KANBAN_TASK", "").strip()
    raw_run_id = os.getenv("HERMES_KANBAN_RUN_ID", "").strip()
    if not task_id or not raw_run_id:
        return False
    try:
        run_id = int(raw_run_id)
    except ValueError:
        return False
    try:
        from hermes_cli import kanban_db

        outcome = (
            kanban_db.QUOTA_UNAVAILABLE
            if is_quota
            else kanban_db.PROVIDER_AVAILABILITY_UNAVAILABLE
        )
        with kanban_db.connect_closing() as conn:
            deferred = kanban_db.defer_task_for_delivery_authorization_retry(
                conn,
                task_id,
                expected_run_id=run_id,
                error=(
                    f"Claude runtime provider unavailable "
                    f"({failure.reason.value}): {failure.message}"
                )[:480],
                outcome=outcome,
            )
        if deferred:
            _runtime_events_logger.warning(
                "claude_worker_availability_defer %s",
                json.dumps(
                    {
                        "task": task_id,
                        "run_id": run_id,
                        "reason": failure.reason.value,
                    },
                    sort_keys=True,
                ),
            )
        return deferred
    except Exception:
        _runtime_events_logger.warning(
            "claude_worker_availability_defer_failed task=%s",
            task_id,
            exc_info=True,
        )
        return False


def run_claude_agent_sdk_attempt(
    agent: Any,
    *,
    user_message: str,
    effective_task_id: str,
) -> ClaudeProjection:
    """Run one resumable Claude SDK attempt using the active runtime target."""

    preflight_failure = prepare_claude_agent_sdk_runtime(agent)
    if preflight_failure is not None:
        return ClaudeProjection(failure=preflight_failure)
    context = agent._claude_runtime_context
    sdk = context["sdk"]
    host_home = context["host_home"]
    workspace = context["workspace"]
    cli_wrapper = context["cli_wrapper"]
    kanban_task_id = context["kanban_task_id"]
    key = (
        str(getattr(agent, "provider", "") or ""),
        str(getattr(agent, "model", "") or ""),
    )
    sessions = getattr(agent, "_claude_sdk_sessions", None)
    if sessions is None:
        sessions = {}
        agent._claude_sdk_sessions = sessions
    session_attestations = getattr(agent, "_claude_sdk_attestations", None)
    if session_attestations is None:
        session_attestations = {}
        agent._claude_sdk_attestations = session_attestations

    if key in sessions:
        session_attestation = session_attestations.get(key)
        if session_attestation is None:
            # Compatibility for a session constructed before attestations were
            # keyed per route. New sessions always populate the map below.
            session_attestation = getattr(agent, "_claude_max_attestation", None)
            if session_attestation is not None:
                session_attestations[key] = session_attestation
        agent._claude_max_attestation = session_attestation

    if key not in sessions:
        attested_route = tuple(context.get("attested_route") or ())
        try:
            attestation_age = time.monotonic() - float(context["attested_at"])
        except (KeyError, TypeError, ValueError):
            attestation_age = float("inf")
        if (
            attested_route != key
            or attestation_age < 0
            or attestation_age > _CLAUDE_NEW_SESSION_ATTESTATION_GRACE_SECONDS
        ):
            # Billing/auth evidence belongs to an SDK session, not to the
            # lifetime of AIAgent. Never let the boundary's positive cache
            # silently authorize a newly-created session.
            agent._claude_max_attestation = None
            try:
                attestation = attest_claude_max_auth(
                    cli_wrapper,
                    cache_ttl_seconds=0,
                )
            except Exception as exc:
                if isinstance(exc, ClaudeAttestationError):
                    _runtime_events_logger.warning(
                        "claude_auth_attestation_failure %s",
                        json.dumps(
                            {
                                **exc.diagnostic,
                                "permanent": bool(exc.permanent),
                                "kanban_worker": bool(kanban_task_id),
                                "new_sdk_session": True,
                            },
                            sort_keys=True,
                        ),
                    )
                    reason = (
                        FailoverReason.auth_permanent
                        if isinstance(exc, ClaudeAttestationRejectedError)
                        # Transient attestation → retryable availability, never
                        # a permanent fail-close (see prepare-path note above).
                        else FailoverReason.auth
                    )
                    return ClaudeProjection(
                        failure=RuntimeFailure(reason, str(exc))
                    )
                classified = classify_api_error(
                    exc,
                    provider=str(getattr(agent, "provider", "") or ""),
                    model=str(getattr(agent, "model", "") or ""),
                )
                return ClaudeProjection(
                    failure=RuntimeFailure(
                        classified.reason,
                        classified.message or str(exc),
                    )
                )
            agent._claude_max_attestation = attestation
            context["attested_route"] = key
            context["attested_at"] = time.monotonic()

        session_attestation = getattr(agent, "_claude_max_attestation", None)
        if session_attestation is None:
            return ClaudeProjection(
                failure=RuntimeFailure(
                    FailoverReason.unknown,
                    "Claude SDK session has no billing attestation",
                )
            )
        session_attestations[key] = session_attestation

        from model_tools import handle_function_call
        from hermes_cli.profiles import get_active_profile_name

        worker_profile = get_active_profile_name()
        file_broker = WorkspaceFileBroker(
            workspace,
            deny_credential_reads=worker_profile.strip().lower()
            in {"reviewer", "verifier"},
            boundary=context.get("workspace_boundary"),
        )

        def _options(resume: str | None) -> Any:
            return build_claude_agent_options(
                sdk=sdk,
                model=agent.model,
                system_prompt=str(getattr(agent, "_cached_system_prompt", "") or ""),
                workspace=workspace,
                host_home=host_home,
                profile_home=host_home,
                inherited_env=os.environ,
                boundary=context.get("workspace_boundary"),
                tool_definitions=list(getattr(agent, "tools", None) or []),
                dispatch=handle_function_call,
                effective_task_id=effective_task_id,
                kanban_task_id=kanban_task_id,
                max_turns=getattr(agent, "max_iterations", None),
                resume=resume,
                effort=_claude_effort(agent),
                cli_path=cli_wrapper,
                file_broker=file_broker,
                capability_mode=str(
                    getattr(agent, "_claude_capability_mode", "worker") or "worker"
                ),
                worker_profile=worker_profile,
                auxiliary_tool_names=tuple(
                    getattr(agent, "_claude_auxiliary_tool_names", ()) or ()
                ),
                sandbox_extra_read_paths=_sandbox_extra_read_paths(),
            )

        attempt_budgets = resolve_attempt_budgets(agent)
        sessions[key] = ClaudeAgentSdkSession(
            sdk=sdk,
            options_factory=_options,
            stream_delta_callback=getattr(agent, "stream_delta_callback", None),
            tool_progress_callback=getattr(agent, "tool_progress_callback", None),
            resources=[file_broker],
            initial_session_id=(
                getattr(agent, "_claude_resume_session_id", None)
                or _load_persisted_claude_session_id(agent)
            ),
            total_attempt_timeout_seconds=attempt_budgets.total_seconds,
            first_event_timeout_seconds=attempt_budgets.first_event_seconds,
        )

    def _clear_auth_state() -> None:
        invalidate_claude_auth_attestation(cli_wrapper)
        agent._claude_max_attestation = None
        agent._claude_runtime_context = None
        failed_session = sessions.pop(key, None)
        session_attestations.pop(key, None)
        if failed_session is not None:
            failed_session.close()

    try:
        projection = sessions[key].run_turn(user_message)
        _persist_claude_session_id(agent, projection.session_id)
        if getattr(agent, "stream_delta_callback", None) is not None:
            agent.stream_delta_callback(None)
        if projection.failure and projection.failure.reason in {
            FailoverReason.auth,
            FailoverReason.auth_permanent,
        }:
            _clear_auth_state()
        return projection
    except Exception as exc:
        classified = classify_api_error(
            exc,
            provider=str(getattr(agent, "provider", "") or ""),
            model=str(getattr(agent, "model", "") or ""),
        )
        if classified.reason in {FailoverReason.auth, FailoverReason.auth_permanent}:
            _clear_auth_state()
        return ClaudeProjection(
            failure=RuntimeFailure(classified.reason, classified.message or str(exc))
        )


def record_claude_subscription_usage(agent: Any, usage: dict[str, Any] | None) -> dict[str, Any]:
    """Record subscription usage without inventing a pay-as-you-go cost."""

    attestation = getattr(agent, "_claude_max_attestation", None)
    included = bool(attestation and getattr(attestation, "included_usage", False))
    raw = dict(usage or {})
    _emit_runtime_event(
        agent,
        "runtime_billing_mode",
        billing_mode="subscription_included" if included else "unattested",
        cost_status="included" if included else "unknown",
    )

    def _int(name: str) -> int:
        try:
            return max(int(raw.get(name) or 0), 0)
        except (TypeError, ValueError):
            return 0

    input_tokens = _int("input_tokens")
    output_tokens = _int("output_tokens")
    cache_read = _int("cache_read_input_tokens") or _int("cache_read_tokens")
    cache_write = _int("cache_creation_input_tokens") or _int("cache_write_tokens")
    from agent.usage_pricing import CanonicalUsage

    canonical = CanonicalUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        raw_usage=raw,
    )
    prompt_tokens = canonical.prompt_tokens
    total = canonical.total_tokens
    agent.session_api_calls += 1
    agent.session_prompt_tokens += prompt_tokens
    agent.session_completion_tokens += output_tokens
    agent.session_total_tokens += total
    agent.session_input_tokens += input_tokens
    agent.session_output_tokens += output_tokens
    agent.session_cache_read_tokens += cache_read
    agent.session_cache_write_tokens += cache_write
    cost_status = "included" if included else "unknown"
    cost_source = "claude_max_subscription" if included else "unattested"
    agent.session_cost_status = cost_status
    agent.session_cost_source = cost_source
    if getattr(agent, "_session_db", None) is not None and getattr(
        agent, "session_id", None
    ):
        try:
            if not getattr(agent, "_session_db_created", False):
                agent._ensure_db_session()
            agent._session_db.update_token_counts(
                agent.session_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read,
                cache_write_tokens=cache_write,
                estimated_cost_usd=None,
                cost_status=cost_status,
                cost_source=cost_source,
                billing_provider="anthropic",
                billing_mode="subscription_included" if included else None,
                model=agent.model,
                api_call_count=1,
            )
        except Exception:
            # Accounting must never turn a successful worker result into a
            # failed task; the in-memory counters still remain authoritative.
            pass
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": output_tokens,
        "total_tokens": total,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "estimated_cost_usd": None,
        "cost_status": cost_status,
        "cost_source": cost_source,
    }


def record_moa_reference_usage(agent: Any, guidance: Any) -> dict[str, Any]:
    """Record externally aggregated advisor usage at each advisor's own cost."""

    from agent.usage_pricing import CanonicalUsage

    usage = getattr(guidance, "usage", None)
    if not isinstance(usage, CanonicalUsage):
        usage = CanonicalUsage(request_count=0)
    references = tuple(getattr(guidance, "references", ()) or ())
    estimated_cost = getattr(guidance, "estimated_cost_usd", None)
    statuses = {
        str(row.get("cost_status") or "unknown")
        for row in references
        if isinstance(row, dict)
    }
    sources = sorted(
        {
            str(row.get("cost_source") or "unknown")
            for row in references
            if isinstance(row, dict)
        }
    )
    if statuses and statuses <= {"included"}:
        cost_status = "included"
    elif estimated_cost is not None:
        cost_status = "actual" if statuses == {"actual"} else "estimated"
    else:
        cost_status = "unknown"
    cost_source = ",".join(sources) if sources else "moa_reference_fanout"
    _emit_runtime_event(
        agent,
        "moa_reference_billing",
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
        estimated_cost_usd=estimated_cost,
        cost_status=cost_status,
        cost_source=cost_source,
        references=references,
    )

    agent.session_api_calls += usage.request_count
    agent.session_prompt_tokens += usage.prompt_tokens
    agent.session_completion_tokens += usage.output_tokens
    agent.session_total_tokens += usage.total_tokens
    agent.session_input_tokens += usage.input_tokens
    agent.session_output_tokens += usage.output_tokens
    agent.session_cache_read_tokens += usage.cache_read_tokens
    agent.session_cache_write_tokens += usage.cache_write_tokens
    agent.session_reasoning_tokens += usage.reasoning_tokens
    if estimated_cost is not None:
        agent.session_estimated_cost_usd += float(estimated_cost)
    agent.session_cost_status = cost_status
    agent.session_cost_source = cost_source

    if getattr(agent, "_session_db", None) is not None and getattr(
        agent, "session_id", None
    ):
        try:
            if not getattr(agent, "_session_db_created", False):
                agent._ensure_db_session()
            agent._session_db.update_token_counts(
                agent.session_id,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_tokens=usage.cache_read_tokens,
                cache_write_tokens=usage.cache_write_tokens,
                reasoning_tokens=usage.reasoning_tokens,
                estimated_cost_usd=(
                    float(estimated_cost) if estimated_cost is not None else None
                ),
                cost_status=cost_status,
                cost_source=cost_source,
                billing_provider=str(getattr(agent, "provider", "") or "") or None,
                model=str(getattr(agent, "model", "") or "") or None,
                api_call_count=usage.request_count,
            )
        except Exception:
            pass
    return {
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.output_tokens,
        "total_tokens": usage.total_tokens,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
        "estimated_cost_usd": (
            float(estimated_cost) if estimated_cost is not None else None
        ),
        "cost_status": cost_status,
        "cost_source": cost_source,
    }


__all__ = [
    "_emit_runtime_event",
    "prepare_claude_agent_sdk_runtime",
    "prepare_claude_sdk_temp_dir",
    "record_claude_subscription_usage",
    "record_moa_reference_usage",
    "run_claude_agent_sdk_attempt",
]
