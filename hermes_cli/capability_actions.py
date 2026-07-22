"""Controller-only action capability preparation and receipt persistence.

This module is imported by the Kanban dispatcher only after a task is claimed.
It never projects source values into a worker.  A successful preparation
creates a run-bound delivery to a non-secret receipt; the worker-side tool is a
read-only view of that row.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
import time
from dataclasses import replace
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from hermes_constants import get_default_hermes_root
from hermes_cli.sqlite_util import write_txn
from hermes_cli.worker_credentials import (
    CAPABILITIES,
    CONTRACT_VERSION,
    GOOGLE_ADS_ACTIVATION_FILENAME,
    WorkerCredentialManifest,
    load_manifest,
)

CAPABILITY_NAME = "google_ads_campaign_status_read"
ACTIVATION_SCHEMA_VERSION = 1
HELPER_PROTOCOL_VERSION = 1
MAX_HELPER_PROTOCOL_BYTES = 16 * 1024
HELPER_TIMEOUT_SECONDS = 75
ACTION_RESERVATION_STALE_SECONDS = HELPER_TIMEOUT_SECONDS + 5
ACTION_RESERVATION_WAIT_SECONDS = HELPER_TIMEOUT_SECONDS + 5
ACTION_RESERVATION_POLL_SECONDS = 0.05
ALLOWED_RECEIPT_FIELDS = frozenset(
    {
        "campaign_resource_name",
        "campaign_id",
        "name",
        "status",
        "serving_status",
        "provider_request_id",
    }
)
ALLOWED_HELPER_CATEGORIES = frozenset(
    {
        "capability_not_authorized",
        "capability_source_missing",
        "capability_source_unavailable",
        "oauth_authorization_failed",
        "google_ads_authorization_failed",
        "google_ads_transient",
        "campaign_not_found",
        "response_invalid",
        "capability_internal_error",
    }
)
ERROR_TO_INCIDENT = {
    "capability_not_authorized": "not_authorized",
    "capability_source_missing": "missing_secret",
    "capability_source_unavailable": "source_unavailable",
    "oauth_authorization_failed": "provider_authorization_failed",
    "google_ads_authorization_failed": "provider_authorization_failed",
    "google_ads_transient": "provider_transient_exhausted",
    "campaign_not_found": "not_authorized",
    "response_invalid": "source_unavailable",
    "capability_internal_error": "source_unavailable",
    "action_budget_exhausted": "action_budget_exhausted",
}
_ACTIVATION_FIELDS = frozenset(
    {
        "schema_version",
        "activation_id",
        "capability",
        "profile",
        "live_activation_authorized",
        "synthetic_only",
        "task_principal",
        "operation",
        "customer_id",
        "campaign_resource_name",
        "api_major",
        "backend",
        "source_project_id",
        "source_key_names",
        "response_schema_sha256",
        "implementation_sha256",
        "runtime_sha256",
        "core_commit_sha",
        "config_commit_sha",
        "installed_runtime_sha",
        "test_commands_sha256",
        "test_results_sha256",
        "leak_scan_sha256",
        "helper_toolchain",
        "action_budget",
        "receipt_ttl_seconds",
        "google_account_role",
        "approved_by",
        "approved_at",
        "approval_surface",
    }
)
_PRINCIPAL_FIELDS = frozenset(
    {
        "board_identity",
        "task_id",
        "created_at",
        "creator_principal",
        "body_sha256",
        "approval_gate_ids",
        "lineage_ids",
    }
)
_TOOLCHAIN_FIELDS = frozenset(
    {
        "interpreter_path",
        "interpreter_sha256",
        "stdlib_probe_path",
        "stdlib_probe_sha256",
        "site_probe_path",
        "site_probe_sha256",
        "helper_path",
        "helper_sha256",
        "bws_path",
        "bws_sha256",
    }
)
_BUDGET_FIELDS = frozenset({"successful_receipts", "provider_attempts"})
_DIGEST_RE = __import__("re").compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = __import__("re").compile(r"^[0-9a-f]{40}$")
_CUSTOMER_RE = __import__("re").compile(r"^[0-9]{1,20}$")
_CAMPAIGN_RE = __import__("re").compile(
    r"^customers/([0-9]{1,20})/campaigns/([0-9]{1,20})$"
)


class ControllerActionFailure(RuntimeError):
    """A deterministic, secret-free action-preparation failure."""

    def __init__(self, category: str, grant_digest: str):
        self.category = (
            category if category in ERROR_TO_INCIDENT else "capability_internal_error"
        )
        self.incident_class = ERROR_TO_INCIDENT[self.category]
        self.capability_name = CAPABILITY_NAME
        self.grant_digest = grant_digest
        super().__init__(self.category)


@dataclass(frozen=True)
class ReceiptDelivery:
    delivery_id: int
    receipt_id: int
    receipt_digest: str
    run_id: int
    reused: bool
    receipt: Mapping[str, Any]


HelperRunner = Callable[[Mapping[str, Any], Mapping[str, Any], str], Mapping[str, Any]]


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_digest(value: Any) -> bool:
    return isinstance(value, str) and _DIGEST_RE.fullmatch(value) is not None


def _helper_path() -> Path:
    return Path(__file__).with_name("google_ads_action_helper.py").resolve()


def _runtime_path() -> Path:
    return Path(__file__).with_name("worker_credentials.py").resolve()


def _stdlib_probe_path() -> Path:
    probe = Path(os.__file__ or "").resolve()
    if not probe.is_file():
        raise ControllerActionFailure("capability_not_authorized", "0" * 64)
    return probe


def _site_probe_path() -> Path:
    import site

    probe = Path(site.__file__ or "").resolve()
    if not probe.is_file():
        raise ControllerActionFailure("capability_not_authorized", "0" * 64)
    return probe


def implementation_sha256() -> str:
    return _sha256_file(_helper_path())


def runtime_sha256() -> str:
    # Bind every core authority surface that can grant, execute, persist, or
    # expose this action. Hashing only worker_credentials.py would let a
    # changed dispatcher/receipt reader pass an older activation manifest.
    root = Path(__file__).resolve().parent.parent
    paths = (
        _runtime_path(),
        Path(__file__).resolve(),
        Path(__file__).with_name("kanban_db.py").resolve(),
        (root / "tools" / "google_ads_receipt_tool.py").resolve(),
    )
    payload = [
        {"path": path.relative_to(root).as_posix(), "sha256": _sha256_file(path)}
        for path in paths
    ]
    return _sha256_bytes(_canonical_json(payload).encode("ascii"))


def response_schema_sha256() -> str:
    from hermes_cli.google_ads_action_helper import response_schema_digest

    return response_schema_digest()


def activation_bytes(activation: Mapping[str, Any]) -> bytes:
    return _canonical_json(activation).encode("ascii")


def activation_sha256(activation: Mapping[str, Any]) -> str:
    return _sha256_bytes(activation_bytes(activation))


def _read_activation(
    root: Path,
    expected_sha256: str,
    override: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], str]:
    if override is None:
        path = root / GOOGLE_ADS_ACTIVATION_FILENAME
        try:
            raw = path.read_bytes()
        except OSError:
            raise ControllerActionFailure("capability_not_authorized", expected_sha256) from None
        if _sha256_bytes(raw) != expected_sha256:
            raise ControllerActionFailure("capability_not_authorized", expected_sha256)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ControllerActionFailure("capability_not_authorized", expected_sha256) from None
    else:
        parsed = dict(override)
        if activation_sha256(parsed) != expected_sha256:
            raise ControllerActionFailure("capability_not_authorized", expected_sha256)
    if not isinstance(parsed, dict):
        raise ControllerActionFailure("capability_not_authorized", expected_sha256)
    return parsed, expected_sha256


def _ancestor_ids(conn, task_id: str) -> list[str]:
    rows = conn.execute(
        """WITH RECURSIVE ancestors(id) AS (
               SELECT parent_id FROM task_links WHERE child_id = ?
               UNION
               SELECT l.parent_id FROM task_links l JOIN ancestors a ON l.child_id = a.id
           ) SELECT id FROM ancestors ORDER BY id""",
        (task_id,),
    ).fetchall()
    return [str(row["id"]) for row in rows]


def _task_principal(conn, task: Any, board_identity: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT id, body, created_at, created_by FROM tasks WHERE id = ?",
        (task.id,),
    ).fetchone()
    if row is None:
        return {}
    direct = conn.execute(
        "SELECT parent_id FROM task_links WHERE child_id = ? ORDER BY parent_id",
        (task.id,),
    ).fetchall()
    return {
        "board_identity": board_identity,
        "task_id": str(row["id"]),
        "created_at": int(row["created_at"]),
        "creator_principal": str(row["created_by"] or ""),
        "body_sha256": _sha256_bytes(str(row["body"] or "").encode("utf-8")),
        "approval_gate_ids": [str(item["parent_id"]) for item in direct],
        "lineage_ids": _ancestor_ids(conn, task.id),
    }


def _path_is_outside_workspace(path: Path, workspace: str) -> bool:
    try:
        resolved = path.resolve(strict=True)
        workspace_path = Path(workspace).resolve(strict=False)
        return resolved != workspace_path and not resolved.is_relative_to(workspace_path)
    except (OSError, ValueError):
        return False


def _validate_toolchain(toolchain: Any, workspace: str, grant_digest: str) -> None:
    if not isinstance(toolchain, dict) or set(toolchain) != _TOOLCHAIN_FIELDS:
        raise ControllerActionFailure("capability_not_authorized", grant_digest)
    expected_paths = {
        "interpreter_path": Path(sys.executable).resolve(),
        "stdlib_probe_path": _stdlib_probe_path(),
        "site_probe_path": _site_probe_path(),
        "helper_path": _helper_path(),
    }
    for name, expected in expected_paths.items():
        candidate = Path(str(toolchain.get(name) or ""))
        if not candidate.is_absolute() or candidate.resolve(strict=False) != expected:
            raise ControllerActionFailure("capability_not_authorized", grant_digest)
    for name in (
        "interpreter_path",
        "stdlib_probe_path",
        "site_probe_path",
        "helper_path",
        "bws_path",
    ):
        path = Path(str(toolchain.get(name) or ""))
        digest_name = name.replace("_path", "_sha256")
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            raise ControllerActionFailure("capability_not_authorized", grant_digest) from None
        if (
            not path.is_absolute()
            or path != resolved
            or not path.is_file()
            or not _path_is_outside_workspace(path, workspace)
            or not _is_digest(toolchain.get(digest_name))
            or _sha256_file(path) != toolchain[digest_name]
        ):
            raise ControllerActionFailure("capability_not_authorized", grant_digest)
        mode = path.stat().st_mode
        if mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise ControllerActionFailure("capability_not_authorized", grant_digest)
    bws_path = Path(str(toolchain["bws_path"]))
    if not os.access(bws_path, os.X_OK):
        raise ControllerActionFailure("capability_not_authorized", grant_digest)


def _validate_activation(
    conn,
    task: Any,
    activation: dict[str, Any],
    *,
    manifest: WorkerCredentialManifest,
    activation_digest: str,
    board_identity: str,
    workspace: str,
    synthetic: bool,
) -> dict[str, Any]:
    fail = lambda: ControllerActionFailure("capability_not_authorized", activation_digest)
    if set(activation) != _ACTIVATION_FIELDS or activation.get("schema_version") != ACTIVATION_SCHEMA_VERSION:
        raise fail()
    definition = CAPABILITIES[CAPABILITY_NAME]
    if activation.get("capability") != CAPABILITY_NAME:
        raise fail()
    if activation.get("profile") != task.assignee or task.assignee != "marketing-operator":
        raise fail()
    if activation.get("operation") != definition.operation:
        raise fail()
    if activation.get("api_major") != definition.api_major:
        raise fail()
    if activation.get("source_key_names") != list(definition.source_keys):
        raise fail()
    if activation.get("response_schema_sha256") != response_schema_sha256():
        raise fail()
    if activation.get("implementation_sha256") != implementation_sha256():
        raise fail()
    if activation.get("runtime_sha256") != runtime_sha256():
        raise fail()
    if any(
        not isinstance(activation.get(field), str)
        or _COMMIT_RE.fullmatch(activation[field]) is None
        for field in ("core_commit_sha", "config_commit_sha", "installed_runtime_sha")
    ):
        raise fail()
    if any(
        not _is_digest(activation.get(field))
        for field in (
            "test_commands_sha256",
            "test_results_sha256",
            "leak_scan_sha256",
        )
    ):
        raise fail()
    if not isinstance(activation.get("activation_id"), str) or not activation["activation_id"]:
        raise fail()
    if not isinstance(activation.get("source_project_id"), str) or not activation["source_project_id"]:
        raise fail()
    if activation.get("google_account_role") != "READ_ONLY":
        raise fail()
    if not all(
        isinstance(activation.get(field), str) and activation[field]
        for field in ("approved_by", "approved_at", "approval_surface")
    ):
        raise fail()
    if synthetic:
        if activation.get("synthetic_only") is not True or activation.get("live_activation_authorized") is not False:
            raise fail()
        if activation.get("backend") != "synthetic":
            raise fail()
    else:
        if activation.get("synthetic_only") is not False or activation.get("live_activation_authorized") is not True:
            raise fail()
        if activation.get("backend") != "local-darwin" or sys.platform != "darwin":
            raise fail()
    customer_id = activation.get("customer_id")
    campaign = activation.get("campaign_resource_name")
    match = _CAMPAIGN_RE.fullmatch(campaign) if isinstance(campaign, str) else None
    if not isinstance(customer_id, str) or not _CUSTOMER_RE.fullmatch(customer_id):
        raise fail()
    if match is None or match.group(1) != customer_id:
        raise fail()
    principal = activation.get("task_principal")
    if not isinstance(principal, dict) or set(principal) != _PRINCIPAL_FIELDS:
        raise fail()
    actual_principal = _task_principal(conn, task, board_identity)
    if principal != actual_principal or principal.get("creator_principal") == task.assignee:
        raise fail()
    budget = activation.get("action_budget")
    if not isinstance(budget, dict) or set(budget) != _BUDGET_FIELDS:
        raise fail()
    if budget.get("successful_receipts") != 1:
        raise fail()
    attempts = budget.get("provider_attempts")
    if isinstance(attempts, bool) or not isinstance(attempts, int) or not 1 <= attempts <= 3:
        raise fail()
    ttl = activation.get("receipt_ttl_seconds")
    if isinstance(ttl, bool) or not isinstance(ttl, int) or not 1 <= ttl <= 300:
        raise fail()
    _validate_toolchain(activation.get("helper_toolchain"), workspace, activation_digest)
    if manifest.version != CONTRACT_VERSION:
        raise fail()
    return activation


def _binding_payload(
    activation: Mapping[str, Any],
    *,
    contract_digest: str,
    activation_digest: str,
) -> dict[str, Any]:
    principal = activation["task_principal"]
    return {
        "version": 1,
        "capability": CAPABILITY_NAME,
        "activation_digest": activation_digest,
        "contract_digest": contract_digest,
        "task_principal_digest": _sha256_bytes(activation_bytes(principal)),
        "operation": activation["operation"],
        "customer_id": activation["customer_id"],
        "campaign_resource_name": activation["campaign_resource_name"],
        "api_major": activation["api_major"],
        "response_schema_digest": activation["response_schema_sha256"],
        "backend": activation["backend"],
        "implementation_digest": activation["implementation_sha256"],
        "runtime_digest": activation["runtime_sha256"],
    }


def _insert_delivery(conn, *, task_id: str, run_id: int, receipt_row: Any, now: int, reused: bool) -> ReceiptDelivery:
    cur = conn.execute(
        """INSERT INTO capability_receipt_deliveries
           (task_id, run_id, capability_name, receipt_id, receipt_digest,
            delivered_at, reused)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(run_id, capability_name) DO NOTHING""",
        (
            task_id,
            run_id,
            CAPABILITY_NAME,
            int(receipt_row["id"]),
            str(receipt_row["receipt_digest"]),
            now,
            1 if reused else 0,
        ),
    )
    if cur.rowcount == 0:
        delivery = conn.execute(
            """SELECT d.id AS delivery_id, d.receipt_id, d.receipt_digest,
                      d.run_id, d.reused, r.receipt_json
                 FROM capability_receipt_deliveries d
                 JOIN capability_action_receipts r ON r.id = d.receipt_id
                WHERE d.run_id = ? AND d.capability_name = ?""",
            (run_id, CAPABILITY_NAME),
        ).fetchone()
    else:
        delivery = conn.execute(
            """SELECT d.id AS delivery_id, d.receipt_id, d.receipt_digest,
                      d.run_id, d.reused, r.receipt_json
                 FROM capability_receipt_deliveries d
                 JOIN capability_action_receipts r ON r.id = d.receipt_id
                WHERE d.id = ?""",
            (int(cur.lastrowid),),
        ).fetchone()
    if delivery is None:
        raise RuntimeError("capability receipt delivery persistence failed")
    return ReceiptDelivery(
        delivery_id=int(delivery["delivery_id"]),
        receipt_id=int(delivery["receipt_id"]),
        receipt_digest=str(delivery["receipt_digest"]),
        run_id=int(delivery["run_id"]),
        reused=bool(delivery["reused"]),
        receipt=json.loads(delivery["receipt_json"]),
    )


def _reserve_or_reuse(
    conn,
    *,
    task_id: str,
    run_id: int,
    binding_digest: str,
    bindings: Mapping[str, Any],
    activation: Mapping[str, Any],
    now: int,
    grant_digest: str,
) -> tuple[ReceiptDelivery | None, int]:
    with write_txn(conn):
        existing_delivery = conn.execute(
            """SELECT d.id AS delivery_id, d.receipt_id, d.receipt_digest,
                      d.run_id, d.reused, r.receipt_json
                 FROM capability_receipt_deliveries d
                 JOIN capability_action_receipts r ON r.id = d.receipt_id
                WHERE d.run_id = ? AND d.capability_name = ?""",
            (run_id, CAPABILITY_NAME),
        ).fetchone()
        if existing_delivery is not None:
            return (
                ReceiptDelivery(
                    delivery_id=int(existing_delivery["delivery_id"]),
                    receipt_id=int(existing_delivery["receipt_id"]),
                    receipt_digest=str(existing_delivery["receipt_digest"]),
                    run_id=int(existing_delivery["run_id"]),
                    reused=bool(existing_delivery["reused"]),
                    receipt=json.loads(existing_delivery["receipt_json"]),
                ),
                0,
            )
        receipt = conn.execute(
            """SELECT * FROM capability_action_receipts
                WHERE binding_digest = ? AND expires_at > ?
                ORDER BY checked_at DESC, id DESC LIMIT 1""",
            (binding_digest, now),
        ).fetchone()
        if receipt is not None:
            return (
                _insert_delivery(
                    conn,
                    task_id=task_id,
                    run_id=run_id,
                    receipt_row=receipt,
                    now=now,
                    reused=True,
                ),
                0,
            )
        reserved = conn.execute(
            """SELECT id, reserved_at FROM capability_action_uses
                WHERE binding_digest = ? AND outcome_category = 'reserved'
                ORDER BY id DESC LIMIT 1""",
            (binding_digest,),
        ).fetchone()
        if reserved is not None:
            if now - int(reserved["reserved_at"]) <= ACTION_RESERVATION_STALE_SECONDS:
                # A negative id tells the caller to await the authoritative
                # reservation rather than issuing a concurrent provider read.
                return None, -int(reserved["id"])
            conn.execute(
                """UPDATE capability_action_uses
                      SET outcome_category = 'capability_source_unavailable',
                          completed_at = ?, row_version = row_version + 1
                    WHERE id = ? AND outcome_category = 'reserved'""",
                (now, int(reserved["id"])),
            )
        attempts = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM capability_action_uses WHERE binding_digest = ?",
                (binding_digest,),
            ).fetchone()["n"]
        )
        successes = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM capability_action_receipts WHERE binding_digest = ?",
                (binding_digest,),
            ).fetchone()["n"]
        )
        budget = activation["action_budget"]
        if attempts >= int(budget["provider_attempts"]) or successes >= int(
            budget["successful_receipts"]
        ):
            raise ControllerActionFailure("action_budget_exhausted", grant_digest)
        attempt = attempts + 1
        cur = conn.execute(
            """INSERT INTO capability_action_uses
               (task_id, run_id, capability_name, binding_digest,
                activation_digest, contract_digest, task_principal_digest,
                operation, scope_digest, api_major, response_schema_digest,
                implementation_digest, runtime_digest, backend, attempt_number,
                outcome_category, reserved_at, row_version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved', ?, 1)""",
            (
                task_id,
                run_id,
                CAPABILITY_NAME,
                binding_digest,
                bindings["activation_digest"],
                bindings["contract_digest"],
                bindings["task_principal_digest"],
                bindings["operation"],
                _sha256_bytes(
                    activation_bytes(
                        {
                            "customer_id": bindings["customer_id"],
                            "campaign_resource_name": bindings["campaign_resource_name"],
                        }
                    )
                ),
                bindings["api_major"],
                bindings["response_schema_digest"],
                bindings["implementation_digest"],
                bindings["runtime_digest"],
                bindings["backend"],
                attempt,
                now,
            ),
        )
        return None, int(cur.lastrowid)


def _await_reserved_action(
    conn,
    *,
    use_id: int,
    task_id: str,
    run_id: int,
    binding_digest: str,
    now: int,
    grant_digest: str,
) -> ReceiptDelivery:
    deadline = time.monotonic() + ACTION_RESERVATION_WAIT_SECONDS
    while True:
        delivery = read_run_receipt(conn, task_id, run_id)
        if delivery is not None:
            return delivery
        row = conn.execute(
            "SELECT outcome_category FROM capability_action_uses WHERE id = ?",
            (use_id,),
        ).fetchone()
        if row is None:
            raise ControllerActionFailure("capability_internal_error", grant_digest)
        outcome = str(row["outcome_category"])
        if outcome == "success":
            with write_txn(conn):
                receipt = conn.execute(
                    """SELECT * FROM capability_action_receipts
                        WHERE action_use_id = ? AND binding_digest = ? LIMIT 1""",
                    (use_id, binding_digest),
                ).fetchone()
                if receipt is not None:
                    return _insert_delivery(
                        conn,
                        task_id=task_id,
                        run_id=run_id,
                        receipt_row=receipt,
                        now=now,
                        reused=True,
                    )
        elif outcome != "reserved":
            raise ControllerActionFailure(outcome, grant_digest)
        if time.monotonic() >= deadline:
            raise ControllerActionFailure(
                "capability_source_unavailable", grant_digest
            )
        time.sleep(ACTION_RESERVATION_POLL_SECONDS)


def _helper_request(activation: Mapping[str, Any], bindings: Mapping[str, Any]) -> dict[str, Any]:
    toolchain = activation["helper_toolchain"]
    return {
        "version": HELPER_PROTOCOL_VERSION,
        "capability": CAPABILITY_NAME,
        "operation": activation["operation"],
        "api_major": activation["api_major"],
        "customer_id": activation["customer_id"],
        "campaign_resource_name": activation["campaign_resource_name"],
        "source_project_id": activation["source_project_id"],
        "bws_path": toolchain["bws_path"],
        "activation_digest": bindings["activation_digest"],
        "contract_digest": bindings["contract_digest"],
        "task_principal_digest": bindings["task_principal_digest"],
        "response_schema_digest": bindings["response_schema_digest"],
    }


def _launch_helper(request: Mapping[str, Any], activation: Mapping[str, Any], workspace: str) -> Mapping[str, Any]:
    del workspace
    token = os.environ.get("BWS_ACCESS_TOKEN", "")
    if not token:
        return {
            "version": HELPER_PROTOCOL_VERSION,
            "ok": False,
            "category": "capability_source_unavailable",
        }
    toolchain = activation["helper_toolchain"]
    env = {
        "BWS_ACCESS_TOKEN": token,
        "PYTHONNOUSERSITE": "1",
    }

    def disable_core_dumps() -> None:
        import resource

        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    try:
        proc = subprocess.Popen(
            [toolchain["interpreter_path"], "-I", "-S", toolchain["helper_path"]],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd="/",
            close_fds=True,
            start_new_session=True,
            preexec_fn=disable_core_dumps if os.name == "posix" else None,
        )
        try:
            stdout, stderr = proc.communicate(
                input=activation_bytes(request), timeout=HELPER_TIMEOUT_SECONDS
            )
        except subprocess.TimeoutExpired:
            try:
                if os.name == "posix":
                    os.killpg(proc.pid, signal.SIGKILL)
                else:  # pragma: no cover - live backend is local Darwin
                    proc.kill()
            except (OSError, ProcessLookupError):
                pass
            proc.communicate()
            return {
                "version": HELPER_PROTOCOL_VERSION,
                "ok": False,
                "category": "capability_source_unavailable",
            }
    except OSError:
        return {
            "version": HELPER_PROTOCOL_VERSION,
            "ok": False,
            "category": "capability_source_unavailable",
        }
    if stderr or len(stdout) > MAX_HELPER_PROTOCOL_BYTES:
        return {
            "version": HELPER_PROTOCOL_VERSION,
            "ok": False,
            "category": "response_invalid",
        }
    try:
        parsed = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {
            "version": HELPER_PROTOCOL_VERSION,
            "ok": False,
            "category": "response_invalid",
        }
    if not isinstance(parsed, dict):
        return {
            "version": HELPER_PROTOCOL_VERSION,
            "ok": False,
            "category": "response_invalid",
        }
    if proc.returncode == 0:
        if parsed.get("ok") is not True:
            return {
                "version": HELPER_PROTOCOL_VERSION,
                "ok": False,
                "category": "response_invalid",
            }
    elif parsed.get("ok") is not False:
        return {
            "version": HELPER_PROTOCOL_VERSION,
            "ok": False,
            "category": "response_invalid",
        }
    return parsed


def _validate_helper_result(result: Any, bindings: Mapping[str, Any], grant_digest: str) -> Mapping[str, Any]:
    if not isinstance(result, Mapping) or result.get("version") != HELPER_PROTOCOL_VERSION:
        raise ControllerActionFailure("response_invalid", grant_digest)
    if result.get("ok") is False:
        if set(result) != {"version", "ok", "category"}:
            raise ControllerActionFailure("response_invalid", grant_digest)
        category = result.get("category")
        if category not in ALLOWED_HELPER_CATEGORIES:
            category = "response_invalid"
        raise ControllerActionFailure(str(category), grant_digest)
    if set(result) != {"version", "ok", "receipt", "bindings"} or result.get("ok") is not True:
        raise ControllerActionFailure("response_invalid", grant_digest)
    result_bindings = result.get("bindings")
    expected_bindings = {
        "activation_digest": bindings["activation_digest"],
        "contract_digest": bindings["contract_digest"],
        "task_principal_digest": bindings["task_principal_digest"],
        "response_schema_digest": bindings["response_schema_digest"],
    }
    if result_bindings != expected_bindings:
        raise ControllerActionFailure("response_invalid", grant_digest)
    receipt = result.get("receipt")
    if not isinstance(receipt, Mapping) or set(receipt) != ALLOWED_RECEIPT_FIELDS:
        raise ControllerActionFailure("response_invalid", grant_digest)
    for name in ALLOWED_RECEIPT_FIELDS - {"provider_request_id"}:
        if not isinstance(receipt.get(name), str):
            raise ControllerActionFailure("response_invalid", grant_digest)
    if receipt.get("provider_request_id") is not None and not isinstance(
        receipt.get("provider_request_id"), str
    ):
        raise ControllerActionFailure("response_invalid", grant_digest)
    if receipt["campaign_resource_name"] != bindings["campaign_resource_name"]:
        raise ControllerActionFailure("response_invalid", grant_digest)
    return dict(receipt)


def _record_failed_use(conn, use_id: int, category: str, now: int) -> None:
    with write_txn(conn):
        cur = conn.execute(
            """UPDATE capability_action_uses
                  SET outcome_category = ?, completed_at = ?, row_version = row_version + 1
                WHERE id = ? AND outcome_category = 'reserved'""",
            (category, now, use_id),
        )
        if cur.rowcount != 1:
            raise RuntimeError("capability action reservation changed")


def _record_success(
    conn,
    *,
    task_id: str,
    run_id: int,
    use_id: int,
    binding_digest: str,
    bindings: Mapping[str, Any],
    receipt: Mapping[str, Any],
    activation: Mapping[str, Any],
    now: int,
) -> ReceiptDelivery:
    stored_receipt = {**receipt, "checked_at": now}
    receipt_json = _canonical_json(stored_receipt)
    receipt_digest = _sha256_bytes(receipt_json.encode("ascii"))
    expires_at = now + int(activation["receipt_ttl_seconds"])
    with write_txn(conn):
        cur = conn.execute(
            """UPDATE capability_action_uses
                  SET outcome_category = 'success', completed_at = ?, row_version = row_version + 1
                WHERE id = ? AND outcome_category = 'reserved'""",
            (now, use_id),
        )
        if cur.rowcount != 1:
            raise RuntimeError("capability action reservation changed")
        receipt_cur = conn.execute(
            """INSERT INTO capability_action_receipts
               (task_id, capability_name, binding_digest, activation_digest,
                contract_digest, task_principal_digest, operation, scope_digest,
                api_major, response_schema_digest, implementation_digest,
                runtime_digest, receipt_json, receipt_digest, provider_request_id,
                checked_at, expires_at, action_use_id, row_version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
            (
                task_id,
                CAPABILITY_NAME,
                binding_digest,
                bindings["activation_digest"],
                bindings["contract_digest"],
                bindings["task_principal_digest"],
                bindings["operation"],
                _sha256_bytes(
                    activation_bytes(
                        {
                            "customer_id": bindings["customer_id"],
                            "campaign_resource_name": bindings["campaign_resource_name"],
                        }
                    )
                ),
                bindings["api_major"],
                bindings["response_schema_digest"],
                bindings["implementation_digest"],
                bindings["runtime_digest"],
                receipt_json,
                receipt_digest,
                receipt.get("provider_request_id"),
                now,
                expires_at,
                use_id,
            ),
        )
        receipt_row = conn.execute(
            "SELECT * FROM capability_action_receipts WHERE id = ?",
            (int(receipt_cur.lastrowid),),
        ).fetchone()
        return _insert_delivery(
            conn,
            task_id=task_id,
            run_id=run_id,
            receipt_row=receipt_row,
            now=now,
            reused=False,
        )


def prepare_controller_action(
    conn,
    task: Any,
    *,
    board_identity: str,
    workspace: str,
    root: Path | str | None = None,
    manifest: WorkerCredentialManifest | None = None,
    activation: Mapping[str, Any] | None = None,
    helper_runner: HelperRunner | None = None,
    synthetic: bool = False,
    now: int | None = None,
) -> ReceiptDelivery | None:
    """Prepare the granted controller action or return ``None`` when absent."""
    if task.current_run_id is None:
        raise RuntimeError("controller action requires a claimed run")
    contract = manifest or load_manifest(root)
    profile = str(task.assignee or "")
    if CAPABILITY_NAME not in contract.actions_for(profile):
        return None
    config = contract.config_for(profile, CAPABILITY_NAME)
    expected_activation_digest = str(config.get("activation_sha256") or "")
    if contract.version != CONTRACT_VERSION or not _is_digest(expected_activation_digest):
        raise ControllerActionFailure("capability_not_authorized", contract.digest)
    home = Path(root) if root is not None else get_default_hermes_root()
    parsed_activation, activation_digest = _read_activation(
        home, expected_activation_digest, activation
    )
    parsed_activation = _validate_activation(
        conn,
        task,
        parsed_activation,
        manifest=contract,
        activation_digest=activation_digest,
        board_identity=board_identity,
        workspace=workspace,
        synthetic=synthetic,
    )
    bindings = _binding_payload(
        parsed_activation,
        contract_digest=contract.digest,
        activation_digest=activation_digest,
    )
    binding_digest = _sha256_bytes(activation_bytes(bindings))
    current_now = int(time.time()) if now is None else int(now)
    runner = helper_runner or _launch_helper
    while True:
        delivery, use_id = _reserve_or_reuse(
            conn,
            task_id=task.id,
            run_id=int(task.current_run_id),
            binding_digest=binding_digest,
            bindings=bindings,
            activation=parsed_activation,
            now=current_now,
            grant_digest=contract.digest,
        )
        if delivery is not None:
            return delivery
        if use_id < 0:
            return _await_reserved_action(
                conn,
                use_id=-use_id,
                task_id=task.id,
                run_id=int(task.current_run_id),
                binding_digest=binding_digest,
                now=current_now,
                grant_digest=contract.digest,
            )
        request = _helper_request(parsed_activation, bindings)
        try:
            result = runner(request, parsed_activation, workspace)
            receipt = _validate_helper_result(result, bindings, contract.digest)
        except ControllerActionFailure as exc:
            _record_failed_use(conn, use_id, exc.category, current_now)
            if exc.category in {"capability_source_unavailable", "google_ads_transient"}:
                attempt_count = int(
                    conn.execute(
                        "SELECT COUNT(*) AS n FROM capability_action_uses "
                        "WHERE binding_digest = ?",
                        (binding_digest,),
                    ).fetchone()["n"]
                )
                if attempt_count >= int(
                    parsed_activation["action_budget"]["provider_attempts"]
                ):
                    raise
                continue
            raise
        except Exception:
            _record_failed_use(conn, use_id, "capability_internal_error", current_now)
            raise ControllerActionFailure(
                "capability_internal_error", contract.digest
            ) from None
        return _record_success(
            conn,
            task_id=task.id,
            run_id=int(task.current_run_id),
            use_id=use_id,
            binding_digest=binding_digest,
            bindings=bindings,
            receipt=receipt,
            activation=parsed_activation,
            now=current_now,
        )


def read_run_receipt(conn, task_id: str, run_id: int, capability_name: str = CAPABILITY_NAME) -> ReceiptDelivery | None:
    """Read the exact run-bound non-secret receipt delivery."""
    row = conn.execute(
        """SELECT d.id AS delivery_id, d.receipt_id, d.receipt_digest,
                  d.run_id, d.reused, r.receipt_json
             FROM capability_receipt_deliveries d
             JOIN capability_action_receipts r ON r.id = d.receipt_id
            WHERE d.task_id = ? AND d.run_id = ? AND d.capability_name = ?""",
        (task_id, int(run_id), capability_name),
    ).fetchone()
    if row is None:
        return None
    return ReceiptDelivery(
        delivery_id=int(row["delivery_id"]),
        receipt_id=int(row["receipt_id"]),
        receipt_digest=str(row["receipt_digest"]),
        run_id=int(row["run_id"]),
        reused=bool(row["reused"]),
        receipt=json.loads(row["receipt_json"]),
    )


def validate_capability_incident(
    conn,
    incident_id: int,
    *,
    board_identity: str,
    root: Path | str | None = None,
) -> tuple[str, str, str]:
    """Perform fresh exact-grant validation without starting a worker.

    The fixed read runs against the incident's ended observer run. Its receipt
    can be reused by the next claimed run, so validation does not create a
    second provider read and does not weaken the successful-receipt budget.
    """
    from hermes_cli import kanban_db as kb

    incident = kb.get_capability_incident(conn, int(incident_id))
    if incident is None or incident.state != "open":
        raise ValueError("open capability incident not found")
    observer_run_id = incident.last_run_id or incident.first_run_id or incident.run_id
    if incident.capability_name != CAPABILITY_NAME or observer_run_id is None:
        raise ValueError("capability incident cannot be controller-validated")
    task = kb.get_task(conn, incident.task_id)
    if task is None or not task.workspace_path:
        raise ValueError("capability incident task workspace is unavailable")
    manifest = load_manifest(root)
    if manifest.digest != incident.grant_digest:
        raise ValueError("capability grant digest changed; supersede the old incident")
    validation_task = replace(task, current_run_id=int(observer_run_id))
    delivery = prepare_controller_action(
        conn,
        validation_task,
        board_identity=board_identity,
        workspace=task.workspace_path,
        root=root,
        manifest=manifest,
    )
    if delivery is None:
        raise ValueError("capability is no longer granted")
    evidence = (
        f"fresh controller receipt {delivery.receipt_id}/"
        f"{delivery.receipt_digest} for ended run {observer_run_id}"
    )
    return CAPABILITY_NAME, manifest.digest, evidence


def build_toolchain_manifest(bws_path: Path) -> dict[str, str]:
    """Build exact non-secret toolchain bindings for policy/test tooling."""
    interpreter = Path(sys.executable).resolve()
    stdlib_probe = _stdlib_probe_path()
    site_probe = _site_probe_path()
    helper = _helper_path()
    bws = bws_path.resolve()
    return {
        "interpreter_path": str(interpreter),
        "interpreter_sha256": _sha256_file(interpreter),
        "stdlib_probe_path": str(stdlib_probe),
        "stdlib_probe_sha256": _sha256_file(stdlib_probe),
        "site_probe_path": str(site_probe),
        "site_probe_sha256": _sha256_file(site_probe),
        "helper_path": str(helper),
        "helper_sha256": _sha256_file(helper),
        "bws_path": str(bws),
        "bws_sha256": _sha256_file(bws),
    }
