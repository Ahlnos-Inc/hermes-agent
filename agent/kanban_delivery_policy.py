"""Canonical dynamic delivery containment for unresolved architecture gates.

The policy is keyed from server-side board state and the dispatcher-owned task
identity.  It never trusts model output or a caller supplied ``approved``
flag.  A policy instance deliberately re-resolves its gate at every output and
tool boundary so a gate opened earlier in the same agent turn is observed
before any later byte crosses a transport boundary.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
import json
import logging
import os
from typing import Optional


_RECEIPT_PREFIX = "Architecture approval pending; output withheld"
_LOOKUP_RECEIPT_PREFIX = "Architecture authorization unavailable; output withheld"
_VALID_DISPOSITIONS = {
    "none",
    "enforcing_unresolved",
    "enforcing_approved",
}
logger = logging.getLogger(__name__)


@dataclass
class ArchitectureDeliveryPolicy:
    """Dynamic, fail-closed policy for a dispatcher-owned Kanban turn."""

    task_id: str = ""
    gate_id: Optional[str] = None
    architect_task_id: Optional[str] = None
    state: Optional[str] = None
    lookup_failed: bool = False
    attestation_loaded: bool = False
    attested_disposition: Optional[str] = None
    attested_gate_id: Optional[str] = None
    attested_architect_task_id: Optional[str] = None
    attested_row_version: Optional[int] = None
    attested_accepted_run_id: Optional[int] = None
    attested_design_digest: Optional[str] = None
    authorization_conflict: bool = False
    _buffer: list[str] = field(default_factory=list, repr=False)

    def refresh(self) -> "ArchitectureDeliveryPolicy":
        """Read current canonical state; active-board lookup errors deny output."""
        if not self.task_id:
            # Unit callers may construct a static policy directly; only a
            # dispatcher-owned task uses the live lookup path.
            return self
        if self.authorization_conflict:
            # Contract conflicts are revocations, not transient availability
            # failures. Never let a later projection change silently re-enable
            # the same run.
            return self
        if self.attestation_loaded and self.attested_disposition == "none":
            # Gate creation is prohibited while a matching run is active, and
            # claim uses the same scope-aware resolver as the gate. The
            # immutable no-gate attestation therefore needs no runtime DB read.
            self.lookup_failed = False
            return self
        if (
            self.attestation_loaded
            and self.attested_disposition == "enforcing_unresolved"
            and self.task_id == self.attested_architect_task_id
        ):
            # The architect's completion transaction revalidates the live gate
            # before accepting its handoff. Internal work and proposal delivery
            # do not need per-byte SQLite polling.
            self.lookup_failed = False
            self.architect_task_id = self.attested_architect_task_id
            return self
        try:
            from hermes_cli.kanban_db import (
                connect_closing,
                get_delivery_architecture_gate,
            )

            # sqlite3.Connection.__exit__ only commits or rolls back; it does
            # not explicitly close the connection.  Delivery policy checks
            # run at every tool/output boundary, so use the canonical closing
            # wrapper rather than relying on interpreter-specific finalization.
            with connect_closing() as conn:
                gate = get_delivery_architecture_gate(conn, self.task_id)
            recovered = self.lookup_failed
            if self.attestation_loaded:
                if self.attested_disposition not in _VALID_DISPOSITIONS:
                    self.lookup_failed = True
                    self.authorization_conflict = True
                    self.gate_id = "invalid-run-spec"
                    self.architect_task_id = "unavailable"
                    self.state = "invalid_delivery_attestation"
                    return self
                if gate is None and self.attested_disposition != "none":
                    self.lookup_failed = True
                    self.authorization_conflict = True
                    self.gate_id = self.attested_gate_id or "unavailable"
                    self.architect_task_id = "unavailable"
                    self.state = "attested_gate_missing"
                    return self
                if gate is not None and self.attested_disposition == "none":
                    self.lookup_failed = True
                    self.authorization_conflict = True
                    self.gate_id = gate.gate_id
                    self.architect_task_id = gate.architect_task_id
                    self.state = "gate_appeared_after_claim"
                    return self
                if (
                    gate is not None
                    and gate.gate_id != self.attested_gate_id
                ):
                    self.lookup_failed = True
                    self.authorization_conflict = True
                    self.gate_id = gate.gate_id
                    self.architect_task_id = gate.architect_task_id
                    self.state = "attested_gate_mismatch"
                    return self
                if (
                    gate is not None
                    and self.attested_disposition == "enforcing_approved"
                    and (
                        gate.state not in {"policy_accepted", "human_approved"}
                        or gate.row_version != self.attested_row_version
                        or gate.accepted_run_id != self.attested_accepted_run_id
                        or gate.design_digest != self.attested_design_digest
                    )
                ):
                    self.lookup_failed = True
                    self.authorization_conflict = True
                    self.gate_id = gate.gate_id
                    self.architect_task_id = gate.architect_task_id
                    self.state = "delivery_authority_epoch_changed"
                    return self
            self.lookup_failed = False
            if gate is None:
                self.gate_id = None
                self.architect_task_id = None
                self.state = None
            else:
                self.gate_id = gate.gate_id
                self.architect_task_id = gate.architect_task_id
                self.state = gate.state
            if recovered:
                logger.info(
                    "Architecture delivery gate lookup recovered for task %s",
                    self.task_id,
                )
        except Exception:
            # A live board task must never leak protected content because its
            # authority projection could not be read.  Non-Kanban turns never
            # construct this object.
            if not self.lookup_failed:
                logger.warning(
                    "Architecture delivery gate lookup failed for task %s; "
                    "withholding output",
                    self.task_id,
                    exc_info=True,
                )
            self.lookup_failed = True
            # An immutable claim-time ``none`` disposition is authoritative
            # for this run. A transient resolver outage is therefore an
            # availability degradation, not evidence that an approval is
            # pending. Enforcing or malformed attestations remain fail-closed.
            if not (
                self.attestation_loaded
                and self.attested_disposition == "none"
            ):
                self.gate_id = self.attested_gate_id or "unavailable"
                self.architect_task_id = "unavailable"
                self.state = "lookup_failed"
        return self

    @property
    def withholding(self) -> bool:
        self.refresh()
        lookup_denies = self.lookup_failed and not (
            self.attestation_loaded
            and self.attested_disposition == "none"
            and not self.authorization_conflict
        )
        # The architect is the trusted producer of the handoff that the gate
        # validates. It must be able to inspect context, use tools, complete
        # its card, and surface the validated proposal to an approver. The
        # control-plane claim gate prevents non-architect implementation work
        # from running while unresolved; delivery containment is only a
        # backstop for an unexpected protected worker or an authority outage.
        unresolved_protected_worker = (
            self.gate_id is not None
            and self.state not in {"policy_accepted", "human_approved"}
            and self.task_id != self.architect_task_id
        )
        return lookup_denies or self.authorization_conflict or (
            unresolved_protected_worker
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
        prefix = (
            _LOOKUP_RECEIPT_PREFIX
            if self.lookup_failed or self.authorization_conflict
            else _RECEIPT_PREFIX
        )
        return (
            f"{prefix} (gate {self.gate_id}; architect "
            f"{self.architect_task_id}; state {self.state}; next action: "
            f"{self.next_action})."
        )

    def buffer(self, text: object) -> None:
        if isinstance(text, str) and text:
            self._buffer.append(text)

    def stream_delta(self, text: str) -> Optional[str]:
        if self.attested_disposition == "enforcing_approved":
            # Approved implementation turns are delivered atomically after a
            # single final authority-epoch check. Avoid a SQLite read per
            # streamed token and prevent partial output if approval changes.
            self.buffer(text)
            return None
        if self.withholding:
            self.buffer(text)
            return None
        return text

    def interim(self, text: str) -> Optional[str]:
        if self.attested_disposition == "enforcing_approved":
            self.buffer(text)
            return None
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

    @property
    def requires_kernel_requeue(self) -> bool:
        """Whether this enforcing attempt must yield to a fresh claim."""
        self.refresh()
        return bool(
            self.attestation_loaded
            and self.attested_disposition
            in {"enforcing_unresolved", "enforcing_approved", "invalid"}
            and (self.lookup_failed or self.authorization_conflict)
        )


def requeue_current_task_for_delivery_authorization(
    policy: ArchitectureDeliveryPolicy,
) -> bool:
    """Perform the trusted lifecycle transition for a delivery outage."""
    if not policy.requires_kernel_requeue:
        return False
    task_id = os.environ.get("HERMES_KANBAN_TASK")
    run_id = os.environ.get("HERMES_KANBAN_RUN_ID")
    if not task_id or not run_id:
        return False
    reason = (
        "delivery authority changed; fresh claim required"
        if policy.authorization_conflict
        else "delivery gate lookup unavailable"
    )
    try:
        from hermes_cli.kanban_db import (
            connect_closing,
            defer_task_for_delivery_authorization_retry,
        )

        with connect_closing() as conn:
            return defer_task_for_delivery_authorization_retry(
                conn,
                task_id,
                expected_run_id=int(run_id),
                error=reason,
            )
    except Exception:
        logger.error(
            "Unable to requeue task %s run %s after delivery authorization "
            "failure",
            task_id,
            run_id,
            exc_info=True,
        )
        return False


def _invalid_policy(task_id: str, state: str) -> ArchitectureDeliveryPolicy:
    return ArchitectureDeliveryPolicy(
        task_id=task_id,
        lookup_failed=True,
        attestation_loaded=True,
        attested_disposition="invalid",
        gate_id="invalid-run-spec",
        architect_task_id="unavailable",
        state=state,
        authorization_conflict=True,
    )


@lru_cache(maxsize=8)
def _policy_from_spawn_contract(
    task_id: str,
    run_id: str,
    raw_attestation: str,
) -> ArchitectureDeliveryPolicy:
    """Build one mutable live policy from the dispatcher's trusted contract."""
    try:
        from hermes_cli.kanban_db import validate_delivery_policy_snapshot

        parsed = json.loads(raw_attestation)
        policy = validate_delivery_policy_snapshot(parsed)
    except Exception:
        logger.warning(
            "Unable to load delivery authorization for task %s run %s; "
            "withholding output",
            task_id,
            run_id,
            exc_info=True,
        )
        return _invalid_policy(task_id, "invalid_delivery_attestation")
    return ArchitectureDeliveryPolicy(
        task_id=task_id,
        gate_id=policy["gate_id"],
        architect_task_id=policy["architect_task_id"],
        state=policy["state"],
        attestation_loaded=True,
        attested_disposition=policy["disposition"],
        attested_gate_id=policy["gate_id"],
        attested_architect_task_id=policy["architect_task_id"],
        attested_row_version=policy["row_version"],
        attested_accepted_run_id=policy["accepted_run_id"],
        attested_design_digest=policy["design_digest"],
    )


def policy_for_current_kanban_task() -> Optional[ArchitectureDeliveryPolicy]:
    """Return a dynamic policy for the dispatcher-owned turn, if any.

    The wrapper is installed even before a gate exists.  This is the critical
    same-turn property: an orchestrator may create its architect card in one
    tool call, after which the *same* policy instance sees the new gate before
    later tools, streaming, interim text, errors, or final delivery.
    """
    task_id = os.environ.get("HERMES_KANBAN_TASK")
    if not task_id:
        return None
    run_id = os.environ.get("HERMES_KANBAN_RUN_ID")
    raw_attestation = os.environ.get("HERMES_KANBAN_DELIVERY_POLICY")
    if not run_id:
        # Legacy/manual worker launches predate claimed RunSpecs. They have no
        # immutable authority contract to enforce and retain their historical
        # behavior. Dispatcher-claimed workers always carry a run id; a run id
        # with a missing attestation remains fail-closed below.
        if not raw_attestation:
            return None
        return _policy_from_spawn_contract(task_id, "legacy", raw_attestation)
    if not raw_attestation:
        return _invalid_policy(task_id, "missing_delivery_attestation")
    return _policy_from_spawn_contract(task_id, run_id, raw_attestation)
