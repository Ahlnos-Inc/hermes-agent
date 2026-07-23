"""Durable, bounded continuation contracts for Kanban worker epochs.

The Kanban database remains the authority for task/run state.  This module is
the pure, deterministic edge around it: canonical manifest validation,
content digests, bounded context compilation, and provider-policy checks.
It deliberately performs no Jira/network access and executes no model output.
External authorities are represented by immutable references whose full
evidence remains retrievable from their owning system.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
from pathlib import Path
from typing import Any, Iterable, Optional


MANIFEST_VERSION = 1
COMPILED_CONTEXT_VERSION = 1
DEFAULT_MAX_CORE_BYTES = 16 * 1024
DEFAULT_MAX_TOTAL_BYTES = 48 * 1024
MAX_REFERENCE_COUNT = 256
MAX_ACCEPTANCE_COUNT = 128
MAX_DECISION_COUNT = 128
# Bounded decision projection: hard parse ceiling.
MAX_DECISION_PREVIEW_COUNT = 64
# Per-decision preview budget (same as normalize_manifest statement limit).
MAX_DECISION_PREVIEW_BYTES = 4096
# Total budget for the projected decision set (first+last/sentinel window).
MAX_DECISION_PREVIEWS_TOTAL_BYTES = 8 * 1024
# Omission marker appended when a decision statement is truncated.
_DECISION_TRUNCATION_MARKER_TEMPLATE = (
    "\n\n_[decision text truncated; full comment remains on the Kanban task; "
    "sha256={digest}; omitted_bytes={omitted_bytes}]_"
)
# Sentinel preview inserted when the projected set is trimmed.
_DECISION_OMISSION_SENTINEL_TEMPLATE = (
    "\n\n_[{omitted_count} middle decision comments omitted; retrieve full comments "
    "via the authoritative Kanban task comments]_"
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_JIRA_KEY_RE = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
_CHECKBOX_RE = re.compile(r"^\s*[-*]\s*\[[ xX]\]\s*(.+?)\s*$")
_REFERENCE_KINDS = frozenset({"jira", "kanban", "git", "artifact", "vault"})


class ContinuationContractError(ValueError):
    """Stable fail-closed error with a machine-readable code."""

    def __init__(self, code: str, message: Optional[str] = None):
        self.code = code
        super().__init__(message or code)


def _validate_json_value(value: Any, *, path: str = "$") -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ContinuationContractError(
                "non_finite_number", f"{path} contains a non-finite number"
            )
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(item, path=f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ContinuationContractError(
                    "non_string_key", f"{path} contains a non-string key"
                )
            _validate_json_value(item, path=f"{path}.{key}")
        return
    raise ContinuationContractError(
        "unsupported_json_value", f"{path} contains {type(value).__name__}"
    )


def canonical_json(value: Any) -> str:
    """Return the single canonical JSON representation used for all digests."""
    _validate_json_value(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def content_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def text_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _clean_string(value: Any, *, field: str, maximum: int = 16_384) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContinuationContractError(
            "invalid_manifest", f"{field} must be a non-empty string"
        )
    cleaned = value.strip()
    if len(cleaned.encode("utf-8")) > maximum:
        raise ContinuationContractError(
            "invalid_manifest", f"{field} exceeds {maximum} bytes"
        )
    return cleaned


def _clean_string_list(
    value: Any,
    *,
    field: str,
    maximum_items: int,
    item_maximum: int = 4096,
) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > maximum_items:
        raise ContinuationContractError(
            "invalid_manifest", f"{field} must be a list of at most {maximum_items} items"
        )
    out: list[str] = []
    for index, item in enumerate(value):
        out.append(
            _clean_string(item, field=f"{field}[{index}]", maximum=item_maximum)
        )
    return out


def _canonical_provider(value: Any) -> str:
    return str(value or "").strip().lower().replace("_", "-")


def normalize_provider_policy(value: Any) -> dict[str, Any]:
    """Normalize an allow/deny policy without inventing implicit providers.

    An empty allow list means "any provider not denied".  Deny always wins.
    "*" is permitted only in allow and canonicalizes to an empty allow list.
    """
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ContinuationContractError(
            "invalid_provider_policy", "provider policy must be an object"
        )
    unknown = set(value) - {"version", "allow", "deny"}
    if unknown:
        raise ContinuationContractError(
            "invalid_provider_policy",
            f"unsupported provider policy fields: {sorted(unknown)}",
        )
    if value.get("version") not in {None, 1}:
        raise ContinuationContractError(
            "invalid_provider_policy", "provider policy version must be 1"
        )

    def _providers(raw: Any, field: str) -> list[str]:
        if raw is None:
            return []
        if not isinstance(raw, list) or len(raw) > 128:
            raise ContinuationContractError(
                "invalid_provider_policy", f"{field} must be a bounded list"
            )
        normalized = []
        for item in raw:
            provider = _canonical_provider(item)
            if not provider or "/" in provider or any(ch.isspace() for ch in provider):
                raise ContinuationContractError(
                    "invalid_provider_policy", f"invalid provider in {field}: {item!r}"
                )
            normalized.append(provider)
        return sorted(set(normalized))

    allow = _providers(value.get("allow"), "allow")
    deny = _providers(value.get("deny"), "deny")
    if "*" in deny:
        raise ContinuationContractError(
            "invalid_provider_policy", "deny cannot contain '*'"
        )
    if "*" in allow:
        allow = []
    overlap = set(allow) & set(deny)
    if overlap:
        raise ContinuationContractError(
            "invalid_provider_policy",
            f"providers cannot be both allowed and denied: {sorted(overlap)}",
        )
    return {"version": 1, "allow": allow, "deny": deny}


def provider_allowed(provider: Any, policy: Any) -> bool:
    normalized_policy = normalize_provider_policy(
        {
            "allow": policy.get("allow", []),
            "deny": policy.get("deny", []),
        }
        if isinstance(policy, dict)
        else policy
    )
    candidate = _canonical_provider(provider)
    if not candidate:
        return False
    if candidate in normalized_policy["deny"]:
        return False
    allow = normalized_policy["allow"]
    return not allow or candidate in allow


def assert_provider_allowed(provider: Any, policy: Any, *, phase: str) -> None:
    if not provider_allowed(provider, policy):
        raise ContinuationContractError(
            "provider_policy_denied",
            f"provider {provider!r} is denied during {phase}",
        )


def extract_acceptance_criteria(body: Optional[str]) -> list[str]:
    criteria: list[str] = []
    for line in str(body or "").splitlines():
        match = _CHECKBOX_RE.match(line)
        if match:
            criterion = " ".join(match.group(1).split())
            if criterion and criterion not in criteria:
                criteria.append(criterion)
            if len(criteria) >= MAX_ACCEPTANCE_COUNT:
                break
    return criteria


def extract_jira_keys(*values: Any) -> list[str]:
    keys: set[str] = set()
    for value in values:
        keys.update(_JIRA_KEY_RE.findall(str(value or "")))
    return sorted(keys)


def normalize_reference(value: Any, *, index: int) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContinuationContractError(
            "invalid_manifest", f"references[{index}] must be an object"
        )
    unknown = set(value) - {"kind", "uri", "digest", "required", "label"}
    if unknown:
        raise ContinuationContractError(
            "invalid_manifest",
            f"references[{index}] has unsupported fields: {sorted(unknown)}",
        )
    kind = _clean_string(value.get("kind"), field=f"references[{index}].kind", maximum=64)
    if kind not in _REFERENCE_KINDS:
        raise ContinuationContractError(
            "invalid_manifest", f"unsupported reference kind: {kind}"
        )
    uri = _clean_string(value.get("uri"), field=f"references[{index}].uri", maximum=4096)
    digest = value.get("digest")
    if digest is not None and (
        not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest)
    ):
        raise ContinuationContractError(
            "invalid_manifest", f"references[{index}].digest must be sha256"
        )
    label = value.get("label")
    if label is not None:
        label = _clean_string(label, field=f"references[{index}].label", maximum=512)
    return {
        "kind": kind,
        "uri": uri,
        "digest": digest,
        "required": bool(value.get("required", False)),
        "label": label,
    }


def normalize_manifest(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContinuationContractError("invalid_manifest", "manifest must be an object")
    unknown = set(value) - {
        "version",
        "task_id",
        "run_id",
        "objective",
        "acceptance_criteria",
        "decisions",
        "references",
        "provider_policy",
        "repository",
        "created_at",
    }
    if unknown:
        raise ContinuationContractError(
            "invalid_manifest", f"unsupported manifest fields: {sorted(unknown)}"
        )
    if value.get("version") != MANIFEST_VERSION:
        raise ContinuationContractError(
            "unsupported_manifest_version", "manifest version must be 1"
        )
    task_id = _clean_string(value.get("task_id"), field="task_id", maximum=256)
    run_id = value.get("run_id")
    if type(run_id) is not int or run_id <= 0:
        raise ContinuationContractError("invalid_manifest", "run_id must be positive")
    objective = _clean_string(value.get("objective"), field="objective")
    criteria = _clean_string_list(
        value.get("acceptance_criteria"),
        field="acceptance_criteria",
        maximum_items=MAX_ACCEPTANCE_COUNT,
    )
    decisions_raw = value.get("decisions") or []
    if not isinstance(decisions_raw, list) or len(decisions_raw) > MAX_DECISION_COUNT:
        raise ContinuationContractError(
            "invalid_manifest", "decisions must be a bounded list"
        )
    decisions: list[dict[str, str]] = []
    for index, decision in enumerate(decisions_raw):
        if not isinstance(decision, dict) or set(decision) - {"id", "statement"}:
            raise ContinuationContractError(
                "invalid_manifest", f"decisions[{index}] has invalid shape"
            )
        decisions.append(
            {
                "id": _clean_string(decision.get("id"), field=f"decisions[{index}].id", maximum=128),
                "statement": _clean_string(
                    decision.get("statement"),
                    field=f"decisions[{index}].statement",
                    maximum=4096,
                ),
            }
        )
    refs_raw = value.get("references") or []
    if not isinstance(refs_raw, list) or len(refs_raw) > MAX_REFERENCE_COUNT:
        raise ContinuationContractError(
            "invalid_manifest", "references must be a bounded list"
        )
    references = [normalize_reference(item, index=index) for index, item in enumerate(refs_raw)]
    references.sort(key=lambda item: (item["kind"], item["uri"], item.get("digest") or ""))

    repository = value.get("repository")
    if repository is not None:
        if not isinstance(repository, dict) or set(repository) - {
            "path", "head", "dirty_digest", "dirty", "branch"
        }:
            raise ContinuationContractError("invalid_manifest", "repository has invalid shape")
        path = _clean_string(repository.get("path"), field="repository.path", maximum=4096)
        head = repository.get("head")
        if head is not None:
            head = _clean_string(head, field="repository.head", maximum=128)
        dirty_digest = repository.get("dirty_digest")
        if dirty_digest is not None and (
            not isinstance(dirty_digest, str) or not _SHA256_RE.fullmatch(dirty_digest)
        ):
            raise ContinuationContractError(
                "invalid_manifest", "repository.dirty_digest must be sha256"
            )
        repository = {
            "path": path,
            "head": head,
            "dirty": bool(repository.get("dirty", False)),
            "dirty_digest": dirty_digest,
            "branch": str(repository.get("branch") or "").strip() or None,
        }

    created_at = value.get("created_at")
    if type(created_at) is not int or created_at <= 0:
        raise ContinuationContractError("invalid_manifest", "created_at must be positive")
    return {
        "version": MANIFEST_VERSION,
        "task_id": task_id,
        "run_id": run_id,
        "objective": objective,
        "acceptance_criteria": criteria,
        "decisions": decisions,
        "references": references,
        "provider_policy": normalize_provider_policy(value.get("provider_policy")),
        "repository": repository,
        "created_at": created_at,
    }


def _truncate_utf8(value: str, maximum: int) -> tuple[str, int]:
    raw = value.encode("utf-8")
    if len(raw) <= maximum:
        return value, 0
    marker = "\n\n_[working context truncated; full evidence remains in referenced authorities]_\n"
    marker_raw = marker.encode("utf-8")
    prefix = raw[: max(0, maximum - len(marker_raw))]
    rendered = prefix.decode("utf-8", errors="ignore").rstrip() + marker
    return rendered, len(raw) - len(prefix)


def _decision_truncation_marker(*, digest: str, omitted_bytes: int) -> str:
    return _DECISION_TRUNCATION_MARKER_TEMPLATE.format(
        digest=digest,
        omitted_bytes=omitted_bytes,
    )


def _decision_omission_sentinel(omitted_count: int) -> str:
    return _DECISION_OMISSION_SENTINEL_TEMPLATE.format(
        omitted_count=omitted_count,
    )


def _truncate_decision_statement(
    statement: str,
    *,
    maximum_bytes: int = MAX_DECISION_PREVIEW_BYTES,
) -> tuple[str, int]:
    """Byte-safe truncation of a single decision statement.

    Returns (truncated_statement, omitted_bytes).  The returned statement
    always fits within ``maximum_bytes`` including the truncation marker when
    the budget can represent its authority metadata.
    """
    raw = statement.encode("utf-8")
    if len(raw) <= maximum_bytes:
        return statement, 0
    digest = text_digest(statement)
    omitted = 0
    # The number of omitted bytes is part of the marker, so a decimal-boundary
    # change can alter the available prefix by one byte. Iterate to the stable
    # value rather than publishing a placeholder or an off-by-one count.
    for _ in range(8):
        marker = _decision_truncation_marker(
            digest=digest,
            omitted_bytes=omitted,
        )
        prefix = raw[: max(0, maximum_bytes - len(marker.encode("utf-8")))]
        truncated = prefix.decode("utf-8", errors="ignore").rstrip()
        actual_omitted = len(raw) - len(truncated.encode("utf-8"))
        if actual_omitted == omitted:
            return truncated + marker, actual_omitted
        omitted = actual_omitted
    raise ContinuationContractError("decision_truncation_metadata_unstable")


def _render_core(manifest: dict[str, Any]) -> str:
    lines = [
        "# Durable continuation contract",
        "",
        f"Task: `{manifest['task_id']}` | run: `{manifest['run_id']}`",
        f"Objective: {manifest['objective']}",
    ]
    criteria = manifest["acceptance_criteria"]
    if criteria:
        lines.extend(["", "## Acceptance criteria"])
        lines.extend(f"- [ ] {item}" for item in criteria)
    decisions = manifest["decisions"]
    if decisions:
        lines.extend(["", "## Settled decisions"])
        lines.extend(f"- `{item['id']}`: {item['statement']}" for item in decisions)
    policy = manifest["provider_policy"]
    lines.extend(
        [
            "",
            "## Runtime policy",
            f"- provider allow: {', '.join(policy['allow']) or '(any not denied)'}",
            f"- provider deny: {', '.join(policy['deny']) or '(none)'}",
        ]
    )
    repository = manifest.get("repository")
    if repository:
        lines.extend(
            [
                "",
                "## Repository checkpoint",
                f"- path: `{repository['path']}`",
                f"- head: `{repository.get('head') or '(unborn)'}`",
                f"- branch: `{repository.get('branch') or '(detached/unborn)'}`",
                f"- dirty: `{str(bool(repository.get('dirty'))).lower()}`",
                f"- dirty digest: `{repository.get('dirty_digest') or '(clean)'}`",
            ]
        )
    refs = manifest["references"]
    if refs:
        lines.extend(["", "## Evidence references"])
        for ref in refs:
            required = "required" if ref["required"] else "on-demand"
            digest = f" sha256:{ref['digest']}" if ref.get("digest") else ""
            label = f" — {ref['label']}" if ref.get("label") else ""
            lines.append(
                f"- [{ref['kind']}/{required}] `{ref['uri']}`{digest}{label}"
            )
    return "\n".join(lines).rstrip() + "\n"


def _render_core_with_decision_budget(
    manifest: dict[str, Any],
    *,
    max_core_bytes: int = DEFAULT_MAX_CORE_BYTES,
) -> str:
    """Render the core with a dynamic decision-section budget.

    The decision section is capped at ``min(8 KiB, max_core_bytes // 2)``
    so that decision-only oversize inputs recover with visible truncation
    markers rather than blocking bootstrap.  Non-decision required core
    fields still fail loudly as before.
    """
    # Build core without decisions first.
    base_lines = [
        "# Durable continuation contract",
        "",
        f"Task: `{manifest['task_id']}` | run: `{manifest['run_id']}`",
        f"Objective: {manifest['objective']}",
    ]
    criteria = manifest["acceptance_criteria"]
    if criteria:
        base_lines.extend(["", "## Acceptance criteria"])
        base_lines.extend(f"- [ ] {item}" for item in criteria)

    policy = manifest["provider_policy"]
    base_lines.extend(
        [
            "",
            "## Runtime policy",
            f"- provider allow: {', '.join(policy['allow']) or '(any not denied)'}",
            f"- provider deny: {', '.join(policy['deny']) or '(none)'}",
        ]
    )
    repository = manifest.get("repository")
    if repository:
        base_lines.extend(
            [
                "",
                "## Repository checkpoint",
                f"- path: `{repository['path']}`",
                f"- head: `{repository.get('head') or '(unborn)'}`",
                f"- branch: `{repository.get('branch') or '(detached/unborn)'}`",
                f"- dirty: `{str(bool(repository.get('dirty'))).lower()}`",
                f"- dirty digest: `{repository.get('dirty_digest') or '(clean)'}`",
            ]
        )
    refs = manifest["references"]
    if refs:
        base_lines.extend(["", "## Evidence references"])
        for ref in refs:
            required = "required" if ref["required"] else "on-demand"
            digest = f" sha256:{ref['digest']}" if ref.get("digest") else ""
            label = f" — {ref['label']}" if ref.get("label") else ""
            base_lines.append(
                f"- [{ref['kind']}/{required}] `{ref['uri']}`{digest}{label}"
            )

    base_core = "\n".join(base_lines).rstrip() + "\n"
    base_bytes = len(base_core.encode("utf-8"))

    # Dynamic decision-section budget: min(8 KiB, max_core_bytes // 2).
    decision_budget = min(8 * 1024, max_core_bytes // 2)
    # Reserve room for the section header and an omission marker.
    header_bytes = len("\n## Settled decisions\n".encode("utf-8"))
    omission_marker = "\n\n_[some decision comments omitted; retrieve full comments via the authoritative Kanban task comments]_\n"
    omission_bytes = len(omission_marker.encode("utf-8"))
    available_for_decisions = max(0, decision_budget - header_bytes - omission_bytes)

    decisions = manifest["decisions"]
    if decisions:
        base_lines.extend(["", "## Settled decisions"])
        rendered_decisions: list[str] = []
        decision_bytes_so_far = 0
        for dec in decisions:
            line = f"- `{dec['id']}`: {dec['statement']}"
            line_bytes = len(line.encode("utf-8"))
            if decision_bytes_so_far + line_bytes > available_for_decisions:
                # Omit remaining decisions and add marker.
                rendered_decisions.append(omission_marker)
                break
            rendered_decisions.append(line)
            decision_bytes_so_far += line_bytes
        lines = base_lines + rendered_decisions
    else:
        lines = base_lines

    return "\n".join(lines).rstrip() + "\n"


def compile_context(
    manifest_value: Any,
    working_set: str,
    *,
    max_core_bytes: int = DEFAULT_MAX_CORE_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
) -> dict[str, Any]:
    """Compile a deterministic core + bounded working set.

    Required core is never silently truncated.  Oversized working material is
    truncated with a visible marker and its full digest remains in the bundle,
    so a later agent can retrieve the authority rather than inherit an
    ever-growing transcript.
    """
    manifest = normalize_manifest(manifest_value)
    if not isinstance(working_set, str):
        raise ContinuationContractError(
            "invalid_working_set", "working_set must be a string"
        )
    if type(max_core_bytes) is not int or type(max_total_bytes) is not int:
        raise ContinuationContractError("invalid_context_budget")
    if max_core_bytes <= 0 or max_total_bytes <= max_core_bytes:
        raise ContinuationContractError("invalid_context_budget")

    core = _render_core_with_decision_budget(manifest, max_core_bytes=max_core_bytes)
    core_bytes = len(core.encode("utf-8"))
    if core_bytes > max_core_bytes:
        raise ContinuationContractError(
            "context_core_over_budget",
            f"required context is {core_bytes} bytes (limit {max_core_bytes})",
        )
    separator = "\n# Current working set\n\n"
    available = max_total_bytes - core_bytes - len(separator.encode("utf-8"))
    bounded_working, omitted_bytes = _truncate_utf8(working_set, max(0, available))
    rendered = core + separator + bounded_working.lstrip()
    if len(rendered.encode("utf-8")) > max_total_bytes:
        raise ContinuationContractError("compiled_context_over_budget")

    bundle = {
        "version": COMPILED_CONTEXT_VERSION,
        "manifest_digest": content_digest(manifest),
        "core": core,
        "working_set": bounded_working,
        "working_set_source_digest": text_digest(working_set),
        "rendered": rendered,
        "bytes": {
            "core": core_bytes,
            "working_set": len(bounded_working.encode("utf-8")),
            "total": len(rendered.encode("utf-8")),
            "omitted": omitted_bytes,
            "max_core": max_core_bytes,
            "max_total": max_total_bytes,
        },
    }
    bundle["context_digest"] = content_digest(bundle)
    return bundle


def validate_compiled_context(manifest_value: Any, bundle_value: Any) -> dict[str, Any]:
    manifest = normalize_manifest(manifest_value)
    if not isinstance(bundle_value, dict):
        raise ContinuationContractError("invalid_compiled_context")
    bundle = dict(bundle_value)
    claimed_digest = bundle.pop("context_digest", None)
    if not isinstance(claimed_digest, str) or not _SHA256_RE.fullmatch(claimed_digest):
        raise ContinuationContractError("invalid_compiled_context_digest")
    if content_digest(bundle) != claimed_digest:
        raise ContinuationContractError("compiled_context_digest_mismatch")
    if bundle.get("version") != COMPILED_CONTEXT_VERSION:
        raise ContinuationContractError("unsupported_compiled_context_version")
    if bundle.get("manifest_digest") != content_digest(manifest):
        raise ContinuationContractError("manifest_digest_mismatch")
    rendered = bundle.get("rendered")
    if not isinstance(rendered, str):
        raise ContinuationContractError("invalid_compiled_context")
    byte_info = bundle.get("bytes")
    if not isinstance(byte_info, dict) or byte_info.get("total") != len(
        rendered.encode("utf-8")
    ):
        raise ContinuationContractError("compiled_context_size_mismatch")
    bundle["context_digest"] = claimed_digest
    return bundle


def git_repository_snapshot(path_value: Any) -> Optional[dict[str, Any]]:
    """Return a secret-free Git identity using only fixed argv commands."""
    path = Path(str(path_value or "")).expanduser()
    if not path.is_absolute() or not path.is_dir():
        return None

    def _git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    root_result = _git("rev-parse", "--show-toplevel")
    if root_result.returncode != 0:
        return None
    root = Path(root_result.stdout.strip()).resolve(strict=False)
    head_result = _git("rev-parse", "--verify", "HEAD")
    branch_result = _git("symbolic-ref", "--quiet", "--short", "HEAD")
    status_result = _git("status", "--porcelain=v1", "--untracked-files=all")
    if status_result.returncode != 0:
        raise ContinuationContractError(
            "repository_status_failed", status_result.stderr.strip() or "git status failed"
        )
    status = status_result.stdout
    dirty = bool(status)
    return {
        "path": str(root),
        "head": head_result.stdout.strip() if head_result.returncode == 0 else None,
        "branch": branch_result.stdout.strip() if branch_result.returncode == 0 else None,
        "dirty": dirty,
        "dirty_digest": text_digest(status) if dirty else None,
    }


def assert_repository_compatible(repository: Any) -> None:
    """Fail bootstrap when the exact pre-spawn Git checkpoint has drifted."""
    if repository is None:
        return
    current = git_repository_snapshot(repository.get("path"))
    if current is None:
        raise ContinuationContractError("repository_unavailable")
    for key in ("head", "branch", "dirty", "dirty_digest"):
        if current.get(key) != repository.get(key):
            raise ContinuationContractError(
                "repository_checkpoint_mismatch",
                f"repository {key} changed before worker bootstrap",
            )


def decisions_from_comments(comments: Iterable[Any]) -> list[dict[str, str]]:
    """Extract explicit ``Decision:`` comments; ordinary prose is not promoted.

    Returns a bounded, deterministic preview set:
    - Each statement is byte-safe truncated (UTF-8 bytes, not Python chars).
    - Truncated statements carry an explicit marker with digest/omission info.
    - When the source has more decisions than can fit in the total budget,
      a deterministic first+last window is kept with a sentinel preview
      indicating how many middle decisions were omitted.
    """
    raw_decisions: list[dict[str, str]] = []
    for comment in comments:
        body = str(getattr(comment, "body", "") or "").strip()
        if not body.lower().startswith("decision:"):
            continue
        statement = body.split(":", 1)[1].strip()
        if not statement:
            continue
        raw_decisions.append(
            {
                "id": f"decision-{getattr(comment, 'id', len(raw_decisions) + 1)}",
                "statement": statement,
            }
        )
        if len(raw_decisions) >= MAX_DECISION_COUNT:
            break

    # Byte-safe truncation of each statement.
    projected: list[dict[str, str]] = []
    for dec in raw_decisions:
        truncated, _omitted = _truncate_decision_statement(dec["statement"])
        projected.append({"id": dec["id"], "statement": truncated})

    def _serialized_bytes(items: list[dict[str, str]]) -> int:
        return len(canonical_json(items).encode("utf-8"))

    if (
        len(projected) <= MAX_DECISION_PREVIEW_COUNT
        and _serialized_bytes(projected) <= MAX_DECISION_PREVIEWS_TOTAL_BYTES
    ):
        return projected

    # The total-byte ceiling is independent of the per-decision ceiling. Keep
    # the authoritative first/last ordering and an explicit middle-omission
    # sentinel; trim those two statements further only when their serialized
    # representation cannot coexist within the total projection budget.
    first = raw_decisions[0]
    last = raw_decisions[-1]
    omitted_count = max(0, len(raw_decisions) - 2)

    def _first_last_projection(statement_budget: int) -> list[dict[str, str]]:
        first_statement, _ = _truncate_decision_statement(
            first["statement"], maximum_bytes=statement_budget
        )
        last_statement, _ = _truncate_decision_statement(
            last["statement"], maximum_bytes=statement_budget
        )
        items = [
            {"id": first["id"], "statement": first_statement},
            {"id": last["id"], "statement": last_statement},
        ]
        if omitted_count:
            items.append(
                {
                    "id": f"decision-omitted-{omitted_count}",
                    "statement": _decision_omission_sentinel(omitted_count),
                }
            )
        return items

    low, high = 0, MAX_DECISION_PREVIEW_BYTES
    while low < high:
        candidate = (low + high + 1) // 2
        if (
            _serialized_bytes(_first_last_projection(candidate))
            <= MAX_DECISION_PREVIEWS_TOTAL_BYTES
        ):
            low = candidate
        else:
            high = candidate - 1
    projected = _first_last_projection(low)
    if _serialized_bytes(projected) > MAX_DECISION_PREVIEWS_TOTAL_BYTES:
        raise ContinuationContractError("decision_projection_over_budget")

    return projected
