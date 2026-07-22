"""Canonical closed activation-manifest contract for BUILD-676.

This module is the cross-repository source of truth.  ``schema_bytes()`` is a
stable, canonical JSON export that policy tooling copies byte-for-byte from the
reviewed core commit.  Runtime validation still binds values that JSON Schema
cannot prove (current code/toolchain hashes, task identity, and cross-field
evidence relationships).
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from typing import Any, Mapping

SCHEMA_VERSION = 1
MANIFEST_KIND = "google_ads_campaign_status_activation"
CAPABILITY_NAME = "google_ads_campaign_status_read"
ALLOWED_PROFILE = "marketing-operator"
OPERATION = "campaign_status_read_v1"
API_MAJOR = "v24"

DESIGN_ARTIFACT_SET_ID = "BUILD-676-secret-capability-v2-2026-07-21"
DESIGN_ADR_SHA256 = "4d3b7487756a7be27403a42dce0b0b0317fa1b7698224edb67f973960a7a856d"
DESIGN_CONSENSUS_SHA256 = "c6901ab1ba1a0b8ca3e8071b9a6d49b6f4a1c0a6b234fe89af459a02452237fe"
DESIGN_MANIFEST_SHA256 = "e622af25834cd53814c0b716938b79009738aa1a14f31a5efb0a768d8120e0b5"
DESIGN_APPROVAL_TASK_ID = "t_2a8aa105"
DESIGN_APPROVAL_SCOPE = "design_for_synthetic_implementation_only"

SOURCE_KEY_NAMES = (
    "google-ads-developer-token",
    "google-ads-manager-customer-id",
    "vitatide-marketing-oauth-client-id",
    "vitatide-marketing-oauth-client-secret",
    "vitatide-marketing-oauth-refresh-token",
)

ACTIVATION_FIELDS = frozenset(
    {
        "schema_version",
        "manifest_kind",
        "activation_schema_sha256",
        "activation_id",
        "design_artifact_set_id",
        "design_adr_sha256",
        "design_consensus_sha256",
        "design_manifest_sha256",
        "design_approval_task_id",
        "design_approval_scope",
        "capability",
        "profile",
        "live_activation_authorized",
        "synthetic_only",
        "task_principal",
        "operation",
        "customer_id",
        "campaign_resource_name",
        "api_major",
        "api_sunset_checked_at",
        "api_sunset_evidence_sha256",
        "backend",
        "source_project_id",
        "source_key_names",
        "response_schema_sha256",
        "implementation_sha256",
        "runtime_sha256",
        "core_commit_sha",
        "config_commit_sha",
        "installed_runtime_sha",
        "helper_toolchain",
        "action_budget",
        "receipt_ttl_seconds",
        "google_account_role",
        "google_account_role_evidence_sha256",
        "oauth_refresh_token_rotation_evidence_sha256",
        "test_commands_sha256",
        "test_results_sha256",
        "leak_scan_sha256",
        "test_evidence",
        "approved_by",
        "approved_at",
        "approval_surface",
    }
)
PRINCIPAL_FIELDS = frozenset(
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
TOOLCHAIN_FIELDS = frozenset(
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
BUDGET_FIELDS = frozenset({"successful_receipts", "provider_attempts"})
TEST_EVIDENCE_FIELDS = frozenset(
    {
        "synthetic_only",
        "commands",
        "results_sha256",
        "leak_scan_sha256",
        "source_adapter",
        "http_adapter",
    }
)

_DIGEST_PATTERN = "^[0-9a-f]{64}$"
_COMMIT_PATTERN = "^[0-9a-f]{40}$"
_TASK_PATTERN = "^t_[0-9a-f]{8}$"
_CUSTOMER_PATTERN = "^[0-9]{1,20}$"
_CAMPAIGN_PATTERN = "^customers/[0-9]{1,20}/campaigns/[0-9]{1,20}$"


def _closed_object(
    properties: Mapping[str, Any], required: frozenset[str] | set[str]
) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": sorted(required),
        "properties": dict(properties),
    }


def schema_document() -> dict[str, Any]:
    """Return the canonical JSON Schema document as fresh plain data."""
    digest = {"type": "string", "pattern": _DIGEST_PATTERN}
    commit = {"type": "string", "pattern": _COMMIT_PATTERN}
    task_id = {"type": "string", "pattern": _TASK_PATTERN}
    nonempty = {"type": "string", "minLength": 1}
    principal = _closed_object(
        {
            "board_identity": nonempty,
            "task_id": task_id,
            "created_at": {"type": "integer", "minimum": 1},
            "creator_principal": nonempty,
            "body_sha256": digest,
            "approval_gate_ids": {
                "type": "array",
                "minItems": 1,
                "uniqueItems": True,
                "items": task_id,
            },
            "lineage_ids": {
                "type": "array",
                "minItems": 1,
                "uniqueItems": True,
                "items": task_id,
            },
        },
        PRINCIPAL_FIELDS,
    )
    toolchain = _closed_object(
        {
            "interpreter_path": {"type": "string", "pattern": "^/"},
            "interpreter_sha256": digest,
            "stdlib_probe_path": {"type": "string", "pattern": "^/"},
            "stdlib_probe_sha256": digest,
            "site_probe_path": {"type": "string", "pattern": "^/"},
            "site_probe_sha256": digest,
            "helper_path": {"type": "string", "pattern": "^/"},
            "helper_sha256": digest,
            "bws_path": {"type": "string", "pattern": "^/"},
            "bws_sha256": digest,
        },
        TOOLCHAIN_FIELDS,
    )
    test_evidence = _closed_object(
        {
            "synthetic_only": {"const": True},
            "commands": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string", "minLength": 1},
            },
            "results_sha256": digest,
            "leak_scan_sha256": digest,
            "source_adapter": {"const": "synthetic"},
            "http_adapter": {"const": "synthetic"},
        },
        TEST_EVIDENCE_FIELDS,
    )
    properties: dict[str, Any] = {
        "schema_version": {"const": SCHEMA_VERSION},
        "manifest_kind": {"const": MANIFEST_KIND},
        "activation_schema_sha256": digest,
        "activation_id": nonempty,
        "design_artifact_set_id": {"const": DESIGN_ARTIFACT_SET_ID},
        "design_adr_sha256": {"const": DESIGN_ADR_SHA256},
        "design_consensus_sha256": {"const": DESIGN_CONSENSUS_SHA256},
        "design_manifest_sha256": {"const": DESIGN_MANIFEST_SHA256},
        "design_approval_task_id": {"const": DESIGN_APPROVAL_TASK_ID},
        "design_approval_scope": {"const": DESIGN_APPROVAL_SCOPE},
        "capability": {"const": CAPABILITY_NAME},
        "profile": {"const": ALLOWED_PROFILE},
        "live_activation_authorized": {"type": "boolean"},
        "synthetic_only": {"type": "boolean"},
        "task_principal": principal,
        "operation": {"const": OPERATION},
        "customer_id": {"type": "string", "pattern": _CUSTOMER_PATTERN},
        "campaign_resource_name": {"type": "string", "pattern": _CAMPAIGN_PATTERN},
        "api_major": {"const": API_MAJOR},
        "api_sunset_checked_at": {"type": "string", "format": "date-time"},
        "api_sunset_evidence_sha256": digest,
        "backend": {"enum": ["synthetic", "local-darwin"]},
        "source_project_id": nonempty,
        "source_key_names": {"const": list(SOURCE_KEY_NAMES)},
        "response_schema_sha256": digest,
        "implementation_sha256": digest,
        "runtime_sha256": digest,
        "core_commit_sha": commit,
        "config_commit_sha": commit,
        "installed_runtime_sha": commit,
        "helper_toolchain": toolchain,
        "action_budget": _closed_object(
            {
                "successful_receipts": {"const": 1},
                "provider_attempts": {"type": "integer", "minimum": 1, "maximum": 3},
            },
            BUDGET_FIELDS,
        ),
        "receipt_ttl_seconds": {"type": "integer", "minimum": 1, "maximum": 300},
        "google_account_role": {"const": "READ_ONLY"},
        "google_account_role_evidence_sha256": digest,
        "oauth_refresh_token_rotation_evidence_sha256": digest,
        "test_commands_sha256": digest,
        "test_results_sha256": digest,
        "leak_scan_sha256": digest,
        "test_evidence": test_evidence,
        "approved_by": nonempty,
        "approved_at": {"type": "string", "format": "date-time"},
        "approval_surface": nonempty,
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://hermes-agent.dev/schemas/google-ads-activation-manifest-v1.json",
        "title": "Hermes Google Ads campaign-status activation manifest v1",
        **_closed_object(properties, ACTIVATION_FIELDS),
    }


def schema_bytes() -> bytes:
    """Return the byte-stable canonical export consumed by core and policy."""
    return (
        json.dumps(
            schema_document(),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("ascii")


def schema_sha256() -> str:
    return hashlib.sha256(schema_bytes()).hexdigest()


def evidence_bindings_are_valid(activation: Mapping[str, Any]) -> bool:
    """Validate cross-field evidence relations omitted by ordinary JSON Schema."""
    tests = activation.get("test_evidence")
    if not isinstance(tests, Mapping):
        return False
    return bool(
        activation.get("activation_schema_sha256") == schema_sha256()
        and activation.get("test_results_sha256") == tests.get("results_sha256")
        and activation.get("leak_scan_sha256") == tests.get("leak_scan_sha256")
        and tests.get("synthetic_only") is True
        and tests.get("source_adapter") == "synthetic"
        and tests.get("http_adapter") == "synthetic"
        and isinstance(tests.get("commands"), list)
        and bool(tests.get("commands"))
        and all(isinstance(item, str) and item.strip() for item in tests["commands"])
        and isinstance(activation.get("core_commit_sha"), str)
        and activation.get("core_commit_sha") == activation.get("installed_runtime_sha")
        and re.fullmatch(_COMMIT_PATTERN, str(activation.get("config_commit_sha") or ""))
    )


if __name__ == "__main__":
    sys.stdout.buffer.write(schema_bytes())
