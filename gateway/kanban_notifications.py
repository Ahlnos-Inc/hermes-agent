"""Shared, transport-neutral rendering for Kanban notification events."""

from __future__ import annotations

import re
from typing import Any, Optional

from agent.redact import redact_sensitive_text

_URL_RE = re.compile(r"https?://[^\s>\])]+")


def _redact(text: Any) -> str:
    # force=True: an operator notification is a safety boundary — never leak a
    # credential from a spawn/provider/auth failure reason regardless of the
    # global redaction preference.
    return redact_sensitive_text(str(text), force=True)


# ``gave_up`` is the one alert whose ONLY exit is a person: the dispatcher has
# stopped retrying and nothing else will move the task. It used to render as
# "gave up after repeated spawn failures" regardless of what actually happened,
# which misnames the dominant case (a worker that crashes) and leaves the
# operator with no attempt count, no pid, and no next action even though the
# event payload already carries all three (BUILD-674).
_GAVE_UP_CAUSE = {
    "crashed": "repeated worker crashes",
    "timed_out": "repeated timeouts",
    "spawn_failed": "repeated spawn failures",
}
_GAVE_UP_REMEDIATION_DEFAULT = (
    "read `hermes kanban{board} show {task_id}`, fix the cause, then "
    "`hermes kanban{board} unblock {task_id}`"
)
_GAVE_UP_REMEDIATION = {
    "crashed": (
        "read `hermes kanban{board} log {task_id}` for the worker's own output, "
        "fix the cause, then `hermes kanban{board} unblock {task_id}`"
    ),
    "timed_out": (
        "raise the task's max_runtime or split it, then "
        "`hermes kanban{board} unblock {task_id}`"
    ),
    "spawn_failed": (
        "check the gateway log for the dispatcher/credential failure, then "
        "`hermes kanban{board} unblock {task_id}`"
    ),
}


def _gave_up_evidence(payload: dict) -> str:
    """Render the identifying evidence a give-up post-mortem starts from.

    ``branch`` is load-bearing: for a worktree task it is the only pointer to
    work the crashed worker committed but never merged (BUILD-584).
    """
    facts = []
    pid = payload.get("pid")
    if pid is not None:
        facts.append(f"pid {pid}")
    exit_kind = payload.get("exit_kind")
    exit_code = payload.get("exit_code")
    if exit_kind == "signaled" and exit_code is not None:
        facts.append(f"killed by signal {exit_code}")
    elif exit_kind == "nonzero_exit" and exit_code is not None:
        facts.append(f"exited {exit_code}")
    elif exit_kind:
        facts.append(f"exit {exit_kind}")
    branch = payload.get("branch")
    if branch:
        facts.append(f"branch {branch}")
    return f" [{', '.join(facts)}]" if facts else ""


def _block_context_lines(task: Any, payload: dict, message_so_far: str) -> list[str]:
    """Actionable context for a block alert: the ask, content links, Jira
    key, and workspace — everything the operator needs to unblock without
    opening a terminal (see ``_block_context_from_metadata`` on the write
    side). Bare Jira keys are enough: the Telegram adapter linkifies them.
    """
    lines: list[str] = []
    ask = _redact(payload.get("ask") or "").strip()
    if ask:
        lines.append(f"❓ {ask}")
    links = payload.get("links")
    if isinstance(links, (list, tuple)) and links:
        # Same force-redact boundary as reason/ask: masks vendor-prefix
        # credentials embedded in a worker-supplied URL. Generic query
        # params intentionally pass through (redact_sensitive_text's
        # web-URL policy) — a link here may be a pre-signed/magic URL the
        # operator must be able to click.
        links = [_redact(link) for link in links if isinstance(link, str)]
    else:
        # Legacy blocks: surface any URL the worker embedded in the reason.
        links = _URL_RE.findall(_redact(str(payload.get("reason") or "")))
    links = [str(link).rstrip(".,;:") for link in links][:10]
    lines.extend(f"🔗 {link}" for link in links)
    visible = message_so_far + "\n".join(lines)
    try:
        from hermes_cli.kanban_continuation import extract_jira_keys

        keys = extract_jira_keys(
            getattr(task, "title", None),
            getattr(task, "body", None),
            getattr(task, "branch_name", None),
        )
    except Exception:
        keys = []
    lines.extend(f"🎫 {key}" for key in keys[:3] if key not in visible)
    workspace = getattr(task, "workspace_path", None)
    if workspace:
        lines.append(f"📁 {workspace}")
    return lines


def render_kanban_event(
    *,
    task_id: str,
    task: Any,
    event: Any,
    run: Any = None,
    board_slug: Optional[str] = None,
) -> Optional[str]:
    """Render one operator-facing notification, or ``None`` when silent."""
    title = (getattr(task, "title", None) or task_id).strip()[:120]
    assignee = getattr(task, "assignee", None)
    tag = f"@{assignee} " if assignee else ""
    board_tag = f"[{board_slug}] " if board_slug else ""
    kind = event.kind
    payload = event.payload or {}

    if kind == "completed":
        handoff_text = (
            getattr(run, "summary", None)
            or payload.get("summary")
            or getattr(task, "result", None)
        )
        handoff = f"\n{_redact(handoff_text).strip()}" if handoff_text and str(handoff_text).strip() else ""
        return f"✔ {board_tag}{tag}Kanban {task_id} done — {title}{handoff}"
    if kind == "blocked":
        reason = _redact(payload.get("reason") or "").strip()
        base = (
            f"⏸ {board_tag}{tag}Kanban {task_id} blocked — {title}"
            + (f": {reason}" if reason else "")
        )
        context = _block_context_lines(task, payload, base)
        return base + ("\n" + "\n".join(context) if context else "")
    if kind == "spawn_failed":
        error = _redact(payload.get("error") or "").strip()
        suffix = f"\n{error}" if error else ""
        return f"✖ {board_tag}{tag}Kanban {task_id} failed to spawn; dispatcher will retry{suffix}"
    if kind == "gave_up":
        error = _redact(payload.get("error") or "").strip()
        suffix = f"\n{error}" if error else ""
        trigger = str(payload.get("trigger_outcome") or "").strip()
        cause = _GAVE_UP_CAUSE.get(trigger, "repeated failures")
        attempts, limit = payload.get("failures"), payload.get("effective_limit")
        counted = (
            f" ({attempts}/{limit} attempts)"
            if attempts is not None and limit is not None
            else ""
        )
        detail = _gave_up_evidence(payload)
        board_arg = f" --board {board_slug}" if board_slug else ""
        remedy = _GAVE_UP_REMEDIATION.get(trigger, _GAVE_UP_REMEDIATION_DEFAULT)
        return (
            f"✖ {board_tag}{tag}Kanban {task_id} gave up after {cause}"
            f"{counted}{detail}{suffix}\n"
            f"Next: {remedy.format(task_id=task_id, board=board_arg)}"
        )
    if kind == "crashed":
        exit_kind = payload.get("exit_kind")
        exit_code = payload.get("exit_code")
        if exit_kind == "signaled" and exit_code is not None:
            detail = f"killed by signal {exit_code}"
        elif exit_kind == "nonzero_exit" and exit_code is not None:
            detail = f"exited {exit_code}"
        else:
            detail = "pid gone"
        return f"✖ {board_tag}{tag}Kanban {task_id} worker crashed ({detail}); dispatcher will retry"
    if kind == "timed_out":
        limit = int(payload.get("limit_seconds") or 0)
        return f"⏱ {board_tag}{tag}Kanban {task_id} timed out (max_runtime={limit}s); will retry"
    if kind == "stale":
        # No terminal event is ever emitted for a stalled run — the dispatcher
        # requeues it — so this is the only notice a subscriber gets that its
        # upstream lost a run's worth of work (BUILD-742).
        elapsed = int(payload.get("elapsed_seconds") or 0)
        age = payload.get("heartbeat_age_seconds")
        detail = (
            f"no progress for {int(age)}s"
            if age is not None
            else "no heartbeat ever"
        )
        return (
            f"🕰 {board_tag}{tag}Kanban {task_id} stalled after {elapsed}s "
            f"({detail}); reclaimed and requeued"
        )
    if kind == "block_loop_detected":
        reason = _redact(payload.get("reason") or "").strip()
        recurrences = payload.get("recurrences")
        base = (
            f"🔁 {board_tag}{tag}Kanban {task_id} escalated to triage after "
            f"{recurrences} repeated blocks" + (f": {reason}" if reason else "")
        )
        context = _block_context_lines(task, payload, base)
        return base + ("\n" + "\n".join(context) if context else "")
    if kind == "rework_loop_escalated":
        rounds = payload.get("round_count") or "?"
        gate_id = str(payload.get("human_gate_task_id") or "").strip()
        target = f"human gate {gate_id}" if gate_id else "human triage"
        digest = _redact(payload.get("blocker_digest") or "").strip()
        suffix = f"\n{digest}" if digest else ""
        return (
            f"⚠ {board_tag}{tag}Kanban {task_id} rework loop escalated after "
            f"{rounds} rounds to {target}; autonomous review not approved."
            f"{suffix}"
        )
    if kind == "status":
        return f"🔄 {board_tag}{tag}Kanban {task_id} → {payload.get('status') or ''}"
    return None
