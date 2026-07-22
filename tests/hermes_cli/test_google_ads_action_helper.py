from __future__ import annotations

import json
from types import SimpleNamespace
import urllib.parse

import pytest

from hermes_cli import google_ads_action_helper as helper


def _request():
    return {
        "version": 1,
        "capability": helper.CAPABILITY_NAME,
        "operation": helper.OPERATION,
        "api_major": helper.API_MAJOR,
        "customer_id": "1234567890",
        "campaign_resource_name": "customers/1234567890/campaigns/77",
        "source_project_id": "project-id",
        "bws_path": "/usr/local/bin/bws",
        "activation_digest": "a" * 64,
        "contract_digest": "b" * 64,
        "task_principal_digest": "c" * 64,
        "response_schema_digest": helper.response_schema_digest(),
    }


def _bundle():
    return {
        "google-ads-developer-token": "developer-secret",
        "google-ads-manager-customer-id": "998877",
        "vitatide-marketing-oauth-client-id": "client-id-secret",
        "vitatide-marketing-oauth-client-secret": "client-secret",
        "vitatide-marketing-oauth-refresh-token": "refresh-secret",
    }


def test_fixed_oauth_and_google_ads_requests_return_allowlisted_receipt():
    calls = []

    def http(method, url, headers, body, timeout):
        calls.append((method, url, dict(headers), body, timeout))
        if url == helper.OAUTH_URL:
            return helper.HttpResponse(
                200,
                json.dumps({"access_token": "access-secret", "expires_in": 3600}).encode(),
                {},
            )
        return helper.HttpResponse(
            200,
            json.dumps(
                {
                    "results": [
                        {
                            "campaign": {
                                "resourceName": "customers/1234567890/campaigns/77",
                                "id": "77",
                                "name": "Search Brand",
                                "status": "ENABLED",
                                "servingStatus": "SERVING",
                            }
                        }
                    ]
                }
            ).encode(),
            {"request-id": "req-123"},
        )

    result = helper.run_action(
        _request(), source_fetcher=lambda _request: _bundle(), http_client=http
    )

    assert result["ok"] is True
    assert set(result["receipt"]) == set(helper.RESULT_FIELDS)
    assert result["receipt"]["provider_request_id"] == "req-123"
    assert len(calls) == 2
    oauth = urllib.parse.parse_qs(calls[0][3].decode())
    assert oauth == {
        "client_id": ["client-id-secret"],
        "client_secret": ["client-secret"],
        "refresh_token": ["refresh-secret"],
        "grant_type": ["refresh_token"],
    }
    assert calls[1][0:2] == (
        "POST",
        "https://googleads.googleapis.com/v24/customers/1234567890:search",
    )
    assert calls[1][2]["developer-token"] == "developer-secret"
    assert calls[1][2]["login-customer-id"] == "998877"
    assert calls[1][2]["Authorization"] == "Bearer access-secret"
    query = json.loads(calls[1][3])["query"]
    assert query.endswith(
        "WHERE campaign.resource_name = 'customers/1234567890/campaigns/77' LIMIT 1"
    )
    encoded = json.dumps(result)
    for secret in (*_bundle().values(), "access-secret"):
        if secret != "998877":
            assert secret not in encoded


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("operation", "arbitrary_query"),
        ("api_major", "v23"),
        ("customer_id", "123 OR 1=1"),
        ("campaign_resource_name", "customers/1234567890/campaigns/77' OR '1'='1"),
        ("response_schema_digest", "0" * 64),
    ],
)
def test_request_binding_tamper_fails_before_source_or_network(field, value):
    request = _request()
    request[field] = value
    called = False

    def source(_request):
        nonlocal called
        called = True
        return _bundle()

    with pytest.raises(helper.ActionFailure) as exc:
        helper.run_action(request, source_fetcher=source)
    assert exc.value.category == "capability_not_authorized"
    assert called is False


@pytest.mark.parametrize("oauth_status", [400, 401, 403])
def test_missing_source_and_provider_authorization_are_fixed_categories(oauth_status):
    with pytest.raises(helper.ActionFailure) as missing:
        helper.run_action(_request(), source_fetcher=lambda _request: {})
    assert missing.value.category == "capability_source_missing"

    def denied(_method, _url, _headers, _body, _timeout):
        return helper.HttpResponse(oauth_status, b"contains client-secret", {})

    with pytest.raises(helper.ActionFailure) as denied_exc:
        helper.run_action(
            _request(), source_fetcher=lambda _request: _bundle(), http_client=denied
        )
    assert denied_exc.value.category == "oauth_authorization_failed"
    assert "client-secret" not in str(denied_exc.value)


def test_oauth_response_cannot_return_refresh_token():
    def bad_oauth(_method, _url, _headers, _body, _timeout):
        return helper.HttpResponse(
            200,
            json.dumps(
                {"access_token": "access-secret", "refresh_token": "rotated-secret"}
            ).encode(),
            {},
        )

    with pytest.raises(helper.ActionFailure) as exc:
        helper.run_action(
            _request(), source_fetcher=lambda _request: _bundle(), http_client=bad_oauth
        )
    assert exc.value.category == "response_invalid"
    assert "rotated-secret" not in str(exc.value)


def test_mandatory_redactor_rejects_secret_in_provider_controlled_name():
    def http(_method, url, _headers, _body, _timeout):
        if url == helper.OAUTH_URL:
            return helper.HttpResponse(200, b'{"access_token":"access-secret"}', {})
        return helper.HttpResponse(
            200,
            json.dumps(
                {
                    "results": [
                        {
                            "campaign": {
                                "resourceName": "customers/1234567890/campaigns/77",
                                "id": "77",
                                "name": "client-secret",
                                "status": "ENABLED",
                                "servingStatus": "SERVING",
                            }
                        }
                    ]
                }
            ).encode(),
            {},
        )

    with pytest.raises(helper.ActionFailure) as exc:
        helper.run_action(
            _request(), source_fetcher=lambda _request: _bundle(), http_client=http
        )
    assert exc.value.category == "capability_internal_error"


def test_mandatory_redactor_also_rejects_bitwarden_machine_token(
    monkeypatch,
):
    machine_token = "synthetic-machine-token-must-not-escape"
    monkeypatch.setenv("BWS_ACCESS_TOKEN", machine_token)

    def http(_method, url, _headers, _body, _timeout):
        if url == helper.OAUTH_URL:
            return helper.HttpResponse(200, b'{"access_token":"access-secret"}', {})
        return helper.HttpResponse(
            200,
            json.dumps(
                {
                    "results": [
                        {
                            "campaign": {
                                "resourceName": "customers/1234567890/campaigns/77",
                                "id": "77",
                                "name": machine_token,
                                "status": "ENABLED",
                                "servingStatus": "SERVING",
                            }
                        }
                    ]
                }
            ).encode(),
            {},
        )

    with pytest.raises(helper.ActionFailure) as exc:
        helper.run_action(
            _request(), source_fetcher=lambda _request: _bundle(), http_client=http
        )
    assert exc.value.category == "capability_internal_error"
    assert machine_token not in str(exc.value)


def test_duplicate_bitwarden_source_key_fails_closed(monkeypatch):
    values = [
        {"key": key, "value": f"synthetic-value-{index}"}
        for index, key in enumerate(helper.SOURCE_KEYS)
    ]
    values.append({"key": helper.SOURCE_KEYS[0], "value": "ambiguous-second-value"})
    monkeypatch.setenv("BWS_ACCESS_TOKEN", "synthetic-machine-token")
    monkeypatch.setattr(
        helper.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(values).encode(),
            stderr=b"",
        ),
    )

    with pytest.raises(helper.ActionFailure) as exc:
        helper._fetch_bws_bundle(_request())
    assert exc.value.category == "capability_source_unavailable"


@pytest.mark.parametrize(
    ("phase", "expected_category"),
    [
        ("source_exception", "capability_source_unavailable"),
        ("partial_bundle", "capability_source_missing"),
        ("oauth_exception", "capability_internal_error"),
        ("oauth_parse", "response_invalid"),
        ("provider_exception", "capability_internal_error"),
        ("provider_parse", "response_invalid"),
        ("secret_output", "capability_internal_error"),
    ],
)
def test_private_values_never_escape_across_fault_phases(
    phase, expected_category, monkeypatch, caplog, capfd
):
    marker = f"private-fixture-{phase}-must-not-escape"
    machine_token = f"machine-{marker}"
    access_token = f"access-{marker}"
    bundle = {
        "google-ads-developer-token": f"developer-{marker}",
        "google-ads-manager-customer-id": "998877",
        "vitatide-marketing-oauth-client-id": f"client-id-{marker}",
        "vitatide-marketing-oauth-client-secret": f"client-secret-{marker}",
        "vitatide-marketing-oauth-refresh-token": f"refresh-{marker}",
    }
    monkeypatch.setenv("BWS_ACCESS_TOKEN", machine_token)

    def source(_request):
        if phase == "source_exception":
            raise RuntimeError(marker)
        if phase == "partial_bundle":
            return {
                key: value
                for key, value in bundle.items()
                if key != "vitatide-marketing-oauth-refresh-token"
            }
        return bundle

    def http(_method, url, _headers, _body, _timeout):
        if url == helper.OAUTH_URL:
            if phase == "oauth_exception":
                raise RuntimeError(marker)
            if phase == "oauth_parse":
                return helper.HttpResponse(200, marker.encode(), {})
            return helper.HttpResponse(
                200,
                json.dumps({"access_token": access_token}).encode(),
                {},
            )
        if phase == "provider_exception":
            raise RuntimeError(marker)
        if phase == "provider_parse":
            return helper.HttpResponse(200, marker.encode(), {})
        campaign_name = (
            bundle["vitatide-marketing-oauth-client-secret"]
            if phase == "secret_output"
            else "Synthetic Campaign"
        )
        return helper.HttpResponse(
            200,
            json.dumps(
                {
                    "results": [
                        {
                            "campaign": {
                                "resourceName": "customers/1234567890/campaigns/77",
                                "id": "77",
                                "name": campaign_name,
                                "status": "ENABLED",
                                "servingStatus": "SERVING",
                            }
                        }
                    ]
                }
            ).encode(),
            {},
        )

    with pytest.raises(helper.ActionFailure) as exc:
        helper.run_action(_request(), source_fetcher=source, http_client=http)
    captured = capfd.readouterr()
    observable = "\n".join(
        [str(exc.value), repr(exc.value), caplog.text, captured.out, captured.err]
    )
    assert exc.value.category == expected_category
    assert marker not in observable
    assert machine_token not in observable
    assert access_token not in observable
