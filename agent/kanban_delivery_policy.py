"""Canonical dynamic delivery containment for unresolved architecture gates.

The policy is keyed from server-side board state and the dispatcher-owned task
identity.  It never trusts model output or a caller supplied ``approved``
flag.  A policy instance deliberately re-resolves its gate at every output and
tool boundary so a gate opened earlier in the same agent turn is observed
before any later byte crosses a transport boundary.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from typing import Optional


_RECEIPT_PREFIX = "Architecture approval pending; output withheld"


@dataclass
class ArchitectureDeliveryPolicy:
    """Dynamic, fail-closed policy for a dispatcher-owned Kanban turn."""

    task_id: str = ""
    gate_id: Optional[str] = None
    architect_task_id: Optional[str] = None
    state: Optional[str] = None
    lookup_failed: bool = False
    _buffer: list[str] = field(default_factory=list, repr=False)

    def refresh(self) -> "ArchitectureDeliveryPolicy":
        """Read current canonical state; active-board lookup errors deny output."""
        if not self.task_id:
            # Unit callers may construct a static policy directly; only a
            # dispatcher-owned task uses the live lookup path.
            return self
        try:
            from hermes_cli.kanban_db import connect, get_delivery_architecture_gate

            with connect() as conn:
                gate = get_delivery_architecture_gate(conn, self.task_id)
            self.lookup_failed = False
            if gate is None:
                self.gate_id = None
                self.architect_task_id = None
                self.state = None
            else:
                self.gate_id = gate.gate_id
                self.architect_task_id = gate.architect_task_id
                self.state = gate.state
        except Exception:
            # A live board task must never leak protected content because its
            # authority projection could not be read.  Non-Kanban turns never
            # construct this object.
            self.lookup_failed = True
            self.gate_id = "unavailable"
            self.architect_task_id = "unavailable"
            self.state = "lookup_failed"
        return self

    @property
    def withholding(self) -> bool:
        self.refresh()
        return self.lookup_failed or (
            self.gate_id is not None and self.state not in {"policy_accepted", "human_approved"}
        )

    @property
    def next_action(self) -> str:
        if self.lookup_failed:
            return "retry authoritative gate lookup"
        if self.state == "open":
            return "complete and validate the architect handoff"
        if self.state == "validated_awaiting_approval":
            return "await exact-digest human approval"
        if self.state == "policy_accepted":
            return "issue the canonical implementation graph"
        if self.state == "invalidated":
            return "reopen and revalidate the architect handoff"
        if self.state == "rejected":
            return "revise the architect handoff"
        return "await authoritative gate resolution"

    @property
    def receipt(self) -> str:
        # Refresh first so a final response cannot use a stale approval state.
        self.refresh()
        return (
            f"{_RECEIPT_PREFIX} (gate {self.gate_id}; architect "
            f"{self.architect_task_id}; state {self.state}; next action: "
            f"{self.next_action})."
        )

    def buffer(self, text: object) -> None:
        if isinstance(text, str) and text:
            self._buffer.append(text)

    def stream_delta(self, text: str) -> Optional[str]:
        if self.withholding:
            self.buffer(text)
            return None
        return text

    def interim(self, text: str) -> Optional[str]:
        if self.withholding:
            self.buffer(text)
            return None
        return text

    def tool_result(self, text: object) -> object:
        if self.withholding:
            self.buffer(text)
            return self.receipt
        return text

    def final(self, text: object) -> object:
        if self.withholding:
            self.buffer(text)
            return self.receipt
        return text


def policy_for_current_kanban_task() -> Optional[ArchitectureDeliveryPolicy]:
    """Return a dynamic policy for the dispatcher-owned turn, if any.

    The wrapper is installed even before a gate exists.  This is the critical
    same-turn property: an orchestrator may create its architect card in one
    tool call, after which the *same* policy instance sees the new gate before
    later tools, streaming, interim text, errors, or final delivery.
    """
    task_id = os.environ.get("HERMES_KANBAN_TASK")
    return ArchitectureDeliveryPolicy(task_id=task_id) if task_id else None
