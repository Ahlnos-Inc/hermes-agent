"""Automated Vault-V2 documentation-impact gate for Kanban workflows.

Detects when a workflow finalizer/synthesizer task is created, and
automatically inserts a ``vault-v2-curator`` assessment card between
the implementation tasks and the finalizer so documentation impact
is evaluated before the workflow is marked done.

Callers who already know no doc impact is needed can pass
``mode="skip"`` with a ``reason``; a ``vault_doc_impact: no_op``
event is recorded on the finalizer so the automation is auditable
without creating a gate.

A reconciler/backstop can later detect workflows that completed
without a doc-impact record and create remediation curator cards
with ``reconcile_vault_doc_impact``.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import kanban_db as kb

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _load_config() -> Dict[str, Any]:
    """Read the ``kanban.vault_doc_impact`` config section (with defaults)."""
    from hermes_cli.config import load_config  # deferred to avoid circular import

    cfg = load_config().get("kanban", {})
    if not isinstance(cfg, dict):
        cfg = {}
    section = cfg.get("vault_doc_impact", {})
    if not isinstance(section, dict):
        section = {}
    return {
        "enabled": bool(section.get("enabled", True)),
        "curator_assignee": str(section.get("curator_assignee") or "vault-v2-curator"),
        "finalizer_step_keys": _parse_list(
            section.get("finalizer_step_keys", ["finalizer", "synthesizer"])
        ),
        "finalizer_title_keywords": _parse_list(
            section.get("finalizer_title_keywords", ["finalizer", "synthesizer"])
        ),
    }


def _parse_list(value: Any) -> List[str]:
    if isinstance(value, (list, tuple)):
        return [str(x).strip().lower() for x in value if x and str(x).strip()]
    return []


def _is_finalizer_step(task: kb.Task, config: Dict[str, Any]) -> bool:
    """Return True if *task* looks like a workflow finalizer/synthesizer.

    Checks ``current_step_key`` first, then ``title`` as a fallback.
    """
    step = (task.current_step_key or "").strip().lower()
    if step and step in config["finalizer_step_keys"]:
        return True
    title = (task.title or "").lower()
    return any(kw in title for kw in config["finalizer_title_keywords"])


# ---------------------------------------------------------------------------
# Template builder
# ---------------------------------------------------------------------------

_CURATOR_BODY_TEMPLATE: str = (
    "Workflow **{finalizer_title}** ({finalizer_id}) completed and may have "
    "introduced documentation-impact changes.\n\n"
    "**Please assess** the Vault-V2 documentation surface and update canonical "
    "docs where needed. Do *not* blindly mutate docs — review the work, consult "
    "the implementation context, and curate only what changed in durable truth.\n\n"
    "**Sources / context**\n"
    "- Finalizer task: `{finalizer_id}`\n"
    "- Implementation tasks: {parent_list}\n"
    "- Workflow key: `{workflow_key}`\n"
    "- Automation source: `{source}`\n\n"
    "When done, complete this card with a summary of what was updated "
    "(or a no-op reason if nothing needed changing)."
)


def _build_curator_title(finalizer_title: str, source: str = "") -> str:
    base = (finalizer_title or "Workflow").strip()
    return f"Assess Vault-V2 documentation impact for {base}"


# ---------------------------------------------------------------------------
# Gate engine
# ---------------------------------------------------------------------------


def ensure_vault_doc_impact_for_task(
    conn: kb.sqlite3.Connection,
    finalizer: kb.Task,
    *,
    mode: str = "auto",
    reason: str = "",
    source: str = "",
) -> Dict[str, Any]:
    """Ensure a doc-impact gate exists for *finalizer*, or record a skip.

    Args:
        conn: Open DB connection (must be in a write txn or the caller
              surrounds the call with one).
        finalizer: The finalizer/synthesizer task.
        mode: ``"auto"`` (default, check config), ``"skip"`` (record no-op).
        reason: When *mode* is ``"skip"``, why doc impact is not needed.
        source: Identifier for the caller (e.g. ``"kanban_create"``,
                ``"cli"``, ``"reconciler"``).

    Returns:
        A dict with at minimum ``status`` (``gate_created`` / ``no_op`` /
        ``already_recorded``) and, when a gate was created,
        ``gate_task_id``.
    """
    config = _load_config()
    if not config["enabled"]:
        return {"status": "disabled_by_config"}

    existing = _existing_gate(conn, finalizer.id)
    if existing:
        # Repair wiring if needed (idempotent rewiring).
        _rewire_parents_to_gate(conn, finalizer, existing)
        return {
            "status": "already_recorded",
            "gate_task_id": existing,
        }

    if mode == "skip":
        _record_event(
            conn,
            finalizer.id,
            {
                "status": "no_op",
                "reason": reason or "(no reason given)",
                "source": source or "kanban-vault-doc-impact",
            },
        )
        return {
            "status": "no_op",
            "reason": reason or "(no reason given)",
        }

    if not _is_finalizer_step(finalizer, config):
        return {"status": "not_a_finalizer", "step_key": finalizer.current_step_key}

    parents = kb.parent_ids(conn, finalizer.id)
    if not parents:
        # No parents to insert between — create the curator as a
        # standalone task (still records the gate event).
        gate_id = _create_curator_task(conn, finalizer, parents=[], source=source)
        _record_event(
            conn,
            finalizer.id,
            {
                "status": "gate_created",
                "gate_task_id": gate_id,
                "source": source or "kanban-vault-doc-impact",
            },
        )
        return {"status": "gate_created", "gate_task_id": gate_id}

    # Standard path: insert curator between implementation parents and finalizer.
    # 1. Create the curator card (linked under the original parents).
    gate_id = _create_curator_task(conn, finalizer, parents=parents, source=source)
    # 2. Rewire finalizer: replace original parents → curator as sole parent.
    _rewire_parents_to_gate(conn, finalizer, gate_id)

    _record_event(
        conn,
        finalizer.id,
        {
            "status": "gate_created",
            "gate_task_id": gate_id,
            "source": source or "kanban-vault-doc-impact",
        },
    )

    return {"status": "gate_created", "gate_task_id": gate_id}


# ---------------------------------------------------------------------------
# Reconciler / backstop
# ---------------------------------------------------------------------------


def reconcile_vault_doc_impact(
    conn: kb.sqlite3.Connection,
    *,
    dry_run: bool = False,
    source: str = "reconciler",
) -> Dict[str, Any]:
    """Find completed workflows missing doc-impact records and create
    remediation curator cards.

    Scans for ``done`` tasks that look like finalizers (via step key or
    title keyword) and which have no ``vault_doc_impact`` event recorded.
    For each eligible candidate, creates a standalone curator task (the
    original finalizer is already done, so we cannot rewire it).

    Args:
        conn: Open DB connection.
        dry_run: If True, report candidates without creating cards.
        source: Identifies the caller.

    Returns:
        Dict with ``dry_run``, ``scanned``, ``candidates``, and
        ``created`` lists.
    """
    config = _load_config()
    if not config["enabled"]:
        return {
            "dry_run": dry_run,
            "scanned": 0,
            "candidates": [],
            "created": [],
            "message": "disabled by config",
        }

    all_done = kb.list_tasks(conn, status="done", include_archived=False)
    candidates: List[kb.Task] = []
    for task in all_done:
        if not _is_finalizer_step(task, config):
            continue
        if _has_doc_impact_event(conn, task.id):
            continue
        if task.workflow_key and _has_curator_in_workflow(conn, task.workflow_key):
            continue
        candidates.append(task)

    if dry_run:
        return {
            "dry_run": True,
            "scanned": len(all_done),
            "candidates": len(candidates),
            "would_create": [t.id for t in candidates],
        }

    created = []
    for finalizer in candidates:
        gate_id = _create_curator_task(conn, finalizer, parents=[], source=source)
        _record_event(
            conn,
            finalizer.id,
            {
                "status": "remediation_created",
                "gate_task_id": gate_id,
                "source": source,
            },
        )
        created.append(
            {
                "finalizer_task_id": finalizer.id,
                "gate_task_id": gate_id,
                "status": "remediation_created",
            }
        )

    return {
        "dry_run": False,
        "scanned": len(all_done),
        "candidates": len(candidates),
        "created": created,
    }


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _existing_gate(
    conn: kb.sqlite3.Connection, finalizer_id: str
) -> Optional[str]:
    """Return the gate task id if a doc-impact curator was already created
    for *finalizer_id*, or None."""
    events = kb.list_events(conn, finalizer_id)
    for evt in reversed(events):
        if evt.kind != "vault_doc_impact":
            continue
        payload = evt.payload or {}
        if payload.get("status") in {"gate_created", "remediation_created"}:
            gid = payload.get("gate_task_id")
            if gid and kb.get_task(conn, gid):
                return str(gid)
    return None


def _has_doc_impact_event(
    conn: kb.sqlite3.Connection, task_id: str
) -> bool:
    for evt in kb.list_events(conn, task_id):
        if evt.kind == "vault_doc_impact":
            return True
    return False


def _has_curator_in_workflow(
    conn: kb.sqlite3.Connection, workflow_key: str
) -> bool:
    """Check whether any task in the workflow is a curator card."""
    try:
        tasks = kb.list_tasks_by_workflow_key(conn, workflow_key)
    except ValueError:
        return False
    return any(
        t.assignee == _load_config()["curator_assignee"]
        for t in tasks
    )


def _create_curator_task(
    conn: kb.sqlite3.Connection,
    finalizer: kb.Task,
    *,
    parents: Sequence[str] = (),
    source: str = "",
) -> str:
    """Create the vault-v2-curator doc-impact assessment card."""
    config = _load_config()
    title = _build_curator_title(finalizer.title, source=source)
    body = _build_curator_body_int(conn, finalizer, source=source)

    gate_id = kb.create_task(
        conn,
        title=title,
        body=body,
        assignee=config["curator_assignee"],
        parents=parents,
        priority=finalizer.priority + 1,
        workflow_key=finalizer.workflow_key,
        current_step_key="vault_doc_impact",
        created_by=os.environ.get("HERMES_PROFILE") or "kanban-vault-doc-impact",
        tenant=finalizer.tenant,
    )

    # Record the gate creation as an event on the *gate* itself.
    kb._append_event(
        conn,
        gate_id,
        "vault_doc_impact_gate",
        {
            "status": "gate_created_for_finalizer",
            "finalizer_id": finalizer.id,
            "source": source or "kanban-vault-doc-impact",
        },
    )

    return gate_id


def _build_curator_body_int(
    conn: kb.sqlite3.Connection, finalizer: kb.Task, source: str = ""
) -> str:
    parents = kb.parent_ids(conn, finalizer.id)
    return _CURATOR_BODY_TEMPLATE.format(
        finalizer_title=finalizer.title or "Workflow",
        finalizer_id=finalizer.id,
        parent_list=", ".join(f"`{p}`" for p in parents) if parents else "(none)",
        workflow_key=finalizer.workflow_key or "(none)",
        source=source or "kanban-vault-doc-impact",
    )


def _rewire_parents_to_gate(
    conn: kb.sqlite3.Connection,
    finalizer: kb.Task,
    gate_id: str,
) -> None:
    """Replace the finalizer's current parents with *gate_id*.

    Moves any original parents of the finalizer to the gate instead,
    and then links the gate as the sole parent of the finalizer.
    This is idempotent: if wiring is already correct, no change.
    """
    current_parents = kb.parent_ids(conn, finalizer.id)
    gate_current = kb.parent_ids(conn, gate_id)

    # If wiring is already correct: finalizer's only parent is gate,
    # and gate's parents match the original set.
    if current_parents == [gate_id]:
        return

    # Link before unlink. Each call commits its own transaction, so an
    # unlink-first order destroys the finalizer's whole parent set before the
    # new wiring is attempted, and any refusal in between leaves it orphaned
    # with no error the caller sees (kanban_create swallows this at debug).
    # An enforcing architecture gate refuses context-less links on a gated
    # subtree (BUILD-818), which makes that a live failure mode rather than a
    # theoretical one. Doing the additions first means a refusal changes
    # nothing.

    # 1. Move original parents to the gate (if not already there).
    for pid in current_parents:
        if pid != gate_id and pid not in gate_current:
            try:
                kb.link_tasks(conn, pid, gate_id)
            except ValueError:
                pass  # already linked or self-link

    # 2. Link gate → finalizer.
    try:
        kb.link_tasks(conn, gate_id, finalizer.id)
    except ValueError:
        pass  # already linked, or refused by an architecture gate

    # 3. Remove the superseded direct parents from the finalizer — but only
    #    once the gate edge actually exists. `ArchitectureGateError` subclasses
    #    `ValueError`, so a gate refusal above is swallowed silently; unlinking
    #    regardless would leave the finalizer with no parents at all. Skip
    #    gate_id itself: that is the edge step 2 established.
    if gate_id not in kb.parent_ids(conn, finalizer.id):
        return
    for pid in list(current_parents):
        if pid != gate_id:
            kb.unlink_tasks(conn, pid, finalizer.id)


def _record_event(
    conn: kb.sqlite3.Connection,
    task_id: str,
    payload: Dict[str, Any],
) -> None:
    """Record a ``vault_doc_impact`` event on *task_id*."""
    kb._append_event(conn, task_id, "vault_doc_impact", payload)
