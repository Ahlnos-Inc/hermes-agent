"""Isolated one-shot helper for the fixed Google Ads campaign-status action.

The dispatcher executes this file with ``python -I -S`` before it starts the
protected worker.  The protocol is deliberately closed: stdin is one bounded
canonical request and stdout is one bounded canonical response.  The module
uses only the standard library so isolated mode cannot import user site code.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping

try:
    import resource
except ImportError:  # pragma: no cover - the capability is local-Darwin only
    resource = None  # type: ignore[assignment]

CAPABILITY_NAME = "google_ads_campaign_status_read"
OPERATION = "campaign_status_read_v1"
API_MAJOR = "v24"
PROTOCOL_VERSION = 1
MAX_PROTOCOL_BYTES = 16 * 1024
MAX_SOURCE_BYTES = 1024 * 1024
OAUTH_URL = "https://oauth2.googleapis.com/token"
GOOGLE_ADS_ROOT = "https://googleads.googleapis.com"
SOURCE_KEYS = (
    "google-ads-developer-token",
    "google-ads-manager-customer-id",
    "vitatide-marketing-oauth-client-id",
    "vitatide-marketing-oauth-client-secret",
    "vitatide-marketing-oauth-refresh-token",
)
PRIVATE_SOURCE_KEYS = frozenset(SOURCE_KEYS) - {"google-ads-manager-customer-id"}
RESULT_FIELDS = (
    "campaign_resource_name",
    "campaign_id",
    "name",
    "status",
    "serving_status",
    "provider_request_id",
)
ERROR_CATEGORIES = frozenset(
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
_REQUEST_FIELDS = frozenset(
    {
        "version",
        "capability",
        "operation",
        "api_major",
        "customer_id",
        "campaign_resource_name",
        "source_project_id",
        "bws_path",
        "activation_digest",
        "contract_digest",
        "task_principal_digest",
        "response_schema_digest",
    }
)
_DIGEST_FIELDS = (
    "activation_digest",
    "contract_digest",
    "task_principal_digest",
    "response_schema_digest",
)
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_CUSTOMER_RE = re.compile(r"^[0-9]{1,20}$")
_CAMPAIGN_RE = re.compile(r"^customers/([0-9]{1,20})/campaigns/([0-9]{1,20})$")


class ActionFailure(RuntimeError):
    """A fixed, secret-free helper failure category."""

    def __init__(self, category: str):
        if category not in ERROR_CATEGORIES:
            category = "capability_internal_error"
        self.category = category
        super().__init__(category)


class MandatoryRedactor:
    """Helper-local value redactor which has no configuration off-switch."""

    def __init__(self, values: Mapping[str, str]):
        ordered = {
            value
            for value in values.values()
            if isinstance(value, str) and value
        }
        self._values = tuple(sorted(ordered, key=len, reverse=True))

    def scrub(self, value: str) -> str:
        scrubbed = value
        for secret in self._values:
            scrubbed = scrubbed.replace(secret, "***REDACTED***")
        return scrubbed

    def assert_clean(self, value: str) -> None:
        if self.scrub(value) != value:
            raise ActionFailure("capability_internal_error")


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes
    headers: Mapping[str, str]


SourceFetcher = Callable[[Mapping[str, Any]], Mapping[str, str]]
HttpClient = Callable[[str, str, Mapping[str, str], bytes, float], HttpResponse]


def response_schema_digest() -> str:
    payload = json.dumps(
        {"version": PROTOCOL_VERSION, "result_fields": list(RESULT_FIELDS)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _validate_request(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != _REQUEST_FIELDS:
        raise ActionFailure("capability_not_authorized")
    if raw.get("version") != PROTOCOL_VERSION:
        raise ActionFailure("capability_not_authorized")
    if raw.get("capability") != CAPABILITY_NAME or raw.get("operation") != OPERATION:
        raise ActionFailure("capability_not_authorized")
    if raw.get("api_major") != API_MAJOR:
        raise ActionFailure("capability_not_authorized")
    if not isinstance(raw.get("source_project_id"), str) or not raw["source_project_id"]:
        raise ActionFailure("capability_not_authorized")
    if not isinstance(raw.get("bws_path"), str) or not os.path.isabs(raw["bws_path"]):
        raise ActionFailure("capability_not_authorized")
    if any(
        not isinstance(raw.get(field), str) or not _DIGEST_RE.fullmatch(raw[field])
        for field in _DIGEST_FIELDS
    ):
        raise ActionFailure("capability_not_authorized")
    customer_id = raw.get("customer_id")
    resource_name = raw.get("campaign_resource_name")
    if not isinstance(customer_id, str) or not _CUSTOMER_RE.fullmatch(customer_id):
        raise ActionFailure("capability_not_authorized")
    if not isinstance(resource_name, str):
        raise ActionFailure("capability_not_authorized")
    match = _CAMPAIGN_RE.fullmatch(resource_name)
    if match is None or match.group(1) != customer_id:
        raise ActionFailure("capability_not_authorized")
    if raw["response_schema_digest"] != response_schema_digest():
        raise ActionFailure("capability_not_authorized")
    return dict(raw)


def _fetch_bws_bundle(request: Mapping[str, Any]) -> Mapping[str, str]:
    token = os.environ.get("BWS_ACCESS_TOKEN", "")
    if not token:
        raise ActionFailure("capability_source_unavailable")
    env = {
        "BWS_ACCESS_TOKEN": token,
        "NO_COLOR": "1",
        "PATH": os.path.dirname(str(request["bws_path"])),
    }
    try:
        proc = subprocess.run(
            [
                str(request["bws_path"]),
                "secret",
                "list",
                str(request["source_project_id"]),
                "--output",
                "json",
            ],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ActionFailure("capability_source_unavailable") from None
    if proc.returncode != 0 or len(proc.stdout) > MAX_SOURCE_BYTES:
        raise ActionFailure("capability_source_unavailable")
    try:
        raw = json.loads(proc.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ActionFailure("capability_source_unavailable") from None
    if not isinstance(raw, list):
        raise ActionFailure("capability_source_unavailable")
    values: dict[str, str] = {}
    duplicates: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        key = item.get("key")
        value = item.get("value")
        if key in SOURCE_KEYS and isinstance(value, str) and value:
            if key in values:
                duplicates.add(key)
            values[key] = value
    if duplicates:
        raise ActionFailure("capability_source_unavailable")
    return values


def _stdlib_http_client(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes,
    timeout: float,
) -> HttpResponse:
    request = urllib.request.Request(
        url,
        data=body,
        headers=dict(headers),
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return HttpResponse(
                status=int(response.status),
                body=response.read(MAX_SOURCE_BYTES + 1),
                headers={key.lower(): value for key, value in response.headers.items()},
            )
    except urllib.error.HTTPError as exc:
        return HttpResponse(
            status=int(exc.code),
            body=exc.read(MAX_SOURCE_BYTES + 1),
            headers={key.lower(): value for key, value in exc.headers.items()},
        )
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ActionFailure("google_ads_transient") from None


def _parse_json_response(response: HttpResponse, failure_category: str) -> Mapping[str, Any]:
    if len(response.body) > MAX_SOURCE_BYTES:
        raise ActionFailure("response_invalid")
    if response.status in {401, 403} or (
        response.status == 400 and failure_category == "oauth_authorization_failed"
    ):
        raise ActionFailure(failure_category)
    if response.status == 429 or response.status >= 500:
        raise ActionFailure("google_ads_transient")
    if response.status < 200 or response.status >= 300:
        raise ActionFailure("response_invalid")
    try:
        value = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ActionFailure("response_invalid") from None
    if not isinstance(value, dict):
        raise ActionFailure("response_invalid")
    return value


def _fixed_query(resource_name: str) -> str:
    return (
        "SELECT campaign.resource_name, campaign.id, campaign.name, "
        "campaign.status, campaign.serving_status "
        f"FROM campaign WHERE campaign.resource_name = '{resource_name}' LIMIT 1"
    )


def run_action(
    raw_request: Mapping[str, Any],
    *,
    source_fetcher: SourceFetcher = _fetch_bws_bundle,
    http_client: HttpClient = _stdlib_http_client,
) -> dict[str, Any]:
    """Execute the closed action with injected adapters for hermetic tests."""
    request = _validate_request(raw_request)
    bootstrap_token = os.environ.get("BWS_ACCESS_TOKEN", "")
    try:
        fetched = source_fetcher(request)
    except ActionFailure:
        raise
    except Exception:
        raise ActionFailure("capability_source_unavailable") from None
    if not isinstance(fetched, Mapping):
        raise ActionFailure("capability_source_unavailable")
    bundle = {key: fetched.get(key) for key in SOURCE_KEYS}
    if any(not isinstance(bundle[key], str) or not bundle[key] for key in SOURCE_KEYS):
        raise ActionFailure("capability_source_missing")
    values = {key: str(value) for key, value in bundle.items()}
    private_values = {key: values[key] for key in PRIVATE_SOURCE_KEYS}
    redactor = MandatoryRedactor(
        {**private_values, "bitwarden-machine-token": bootstrap_token}
    )

    oauth_body = urllib.parse.urlencode(
        {
            "client_id": values["vitatide-marketing-oauth-client-id"],
            "client_secret": values["vitatide-marketing-oauth-client-secret"],
            "refresh_token": values["vitatide-marketing-oauth-refresh-token"],
            "grant_type": "refresh_token",
        }
    ).encode("ascii")
    try:
        oauth_response = http_client(
            "POST",
            OAUTH_URL,
            {"Content-Type": "application/x-www-form-urlencoded"},
            oauth_body,
            30.0,
        )
        oauth = _parse_json_response(oauth_response, "oauth_authorization_failed")
        if "refresh_token" in oauth:
            raise ActionFailure("response_invalid")
        access_token = oauth.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise ActionFailure("response_invalid")
        redactor = MandatoryRedactor(
            {
                **private_values,
                "bitwarden-machine-token": bootstrap_token,
                "vitatide-marketing-oauth-access-token": access_token,
            }
        )

        ads_url = (
            f"{GOOGLE_ADS_ROOT}/{API_MAJOR}/customers/"
            f"{request['customer_id']}:search"
        )
        ads_body = _canonical_json(
            {"query": _fixed_query(request["campaign_resource_name"])}
        ).encode("ascii")
        ads_response = http_client(
            "POST",
            ads_url,
            {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "developer-token": values["google-ads-developer-token"],
                "login-customer-id": values["google-ads-manager-customer-id"],
            },
            ads_body,
            30.0,
        )
        ads = _parse_json_response(
            ads_response, "google_ads_authorization_failed"
        )
        results = ads.get("results")
        if not isinstance(results, list) or len(results) != 1:
            raise ActionFailure(
                "campaign_not_found" if results == [] else "response_invalid"
            )
        row = results[0]
        campaign = row.get("campaign") if isinstance(row, dict) else None
        if not isinstance(campaign, dict):
            raise ActionFailure("response_invalid")
        required = {
            "resourceName": "campaign_resource_name",
            "id": "campaign_id",
            "name": "name",
            "status": "status",
            "servingStatus": "serving_status",
        }
        if set(campaign) != set(required):
            raise ActionFailure("response_invalid")
        receipt: dict[str, Any] = {
            output: campaign[source] for source, output in required.items()
        }
        if receipt["campaign_resource_name"] != request["campaign_resource_name"]:
            raise ActionFailure("response_invalid")
        if any(not isinstance(receipt[key], str) for key in receipt):
            raise ActionFailure("response_invalid")
        request_id = ads_response.headers.get("request-id") or ads_response.headers.get(
            "x-request-id"
        )
        receipt["provider_request_id"] = (
            request_id if isinstance(request_id, str) and len(request_id) <= 256 else None
        )
        result = {
            "version": PROTOCOL_VERSION,
            "ok": True,
            "receipt": receipt,
            "bindings": {field: request[field] for field in _DIGEST_FIELDS},
        }
        encoded = _canonical_json(result)
        redactor.assert_clean(encoded)
        if len(encoded.encode("utf-8")) > MAX_PROTOCOL_BYTES:
            raise ActionFailure("response_invalid")
        return result
    except ActionFailure:
        raise
    except Exception:
        # Never surface exception reprs from credential-bearing code.
        raise ActionFailure("capability_internal_error") from None


def _error_result(category: str) -> dict[str, Any]:
    if category not in ERROR_CATEGORIES:
        category = "capability_internal_error"
    return {"version": PROTOCOL_VERSION, "ok": False, "category": category}


def main() -> int:
    try:
        try:
            if resource is None:
                raise OSError("resource limits unavailable")
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        except (ValueError, OSError):
            raise ActionFailure("capability_internal_error") from None
        raw = sys.stdin.buffer.read(MAX_PROTOCOL_BYTES + 1)
        if len(raw) > MAX_PROTOCOL_BYTES:
            raise ActionFailure("capability_not_authorized")
        try:
            request = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ActionFailure("capability_not_authorized") from None
        result = run_action(request)
        rc = 0
    except ActionFailure as exc:
        result = _error_result(exc.category)
        rc = 1
    except Exception:
        result = _error_result("capability_internal_error")
        rc = 1
    encoded = _canonical_json(result)
    if len(encoded.encode("utf-8")) > MAX_PROTOCOL_BYTES:
        encoded = _canonical_json(_error_result("capability_internal_error"))
        rc = 1
    sys.stdout.write(encoded)
    sys.stdout.flush()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
