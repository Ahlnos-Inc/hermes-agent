"""Shared, transport-neutral rendering for Kanban notification events."""

from __future__ import annotations

from typing import Any, Optional

from agent.redact import redact_sensitive_text


def _redact(text: Any) -> str:
    # force=True: an operator notification is a safety boundary — never leak a
    # credential from a spawn/provider/auth failure reason regardless of the
    # global redaction preference.
    return redact_sensitive_text(str(text), force=True)


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
        return f"⏸ {board_tag}{tag}Kanban {task_id} blocked" + (f": {reason}" if reason else "")
    if kind == "spawn_failed":
        error = _redact(payload.get("error") or "").strip()
        suffix = f"\n{error}" if error else ""
        return f"✖ {board_tag}{tag}Kanban {task_id} failed to spawn; dispatcher will retry{suffix}"
    if kind == "gave_up":
        error = _redact(payload.get("error") or "").strip()
        suffix = f"\n{error}" if error else ""
        return f"✖ {board_tag}{tag}Kanban {task_id} gave up after repeated spawn failures{suffix}"
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
    if kind == "block_loop_detected":
        reason = _redact(payload.get("reason") or "").strip()
        recurrences = payload.get("recurrences")
        return (
            f"🔁 {board_tag}{tag}Kanban {task_id} escalated to triage after "
            f"{recurrences} repeated blocks" + (f": {reason}" if reason else "")
        )
    if kind == "status":
        return f"🔄 {board_tag}{tag}Kanban {task_id} → {payload.get('status') or ''}"
    return None
