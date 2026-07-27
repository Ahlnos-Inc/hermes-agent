"""BUILD-598: Azure endpoint detection must parse the host, not substring it.

Every Azure short circuit routed on ``"azure.com" in <url>``. A lookalike such
as ``https://azure.com.attacker.test/v1`` contains that substring, so it entered
the Azure credential path and was handed Azure/Anthropic key material. So did a
path or query mention (``https://evil.test/azure.com/v1``) and a userinfo trick.

`utils.base_url_host_matches` already existed and is used correctly two lines
below one of the offending checks, for `palantirfoundry.com`, with a comment
warning about exactly this. These tests pin every site to it.
"""
from __future__ import annotations

import pytest

from agent import anthropic_adapter
from utils import base_url_host_matches

CANARY = "sk-canary-BUILD598-must-not-leak"

# Genuine Azure endpoints that must keep their behaviour.
AZURE_OK = [
    "https://azure.com/v1",
    "https://my-resource.azure.com/v1",
    "https://my-resource.openai.azure.com/openai/deployments/x",
    "https://models.inference.ai.azure.com",
    "https://AZURE.COM/v1",
    "https://My-Resource.Azure.Com/v1",
    "https://azure.com:443/v1",
    "azure.com",
]

# Lookalikes that must NOT be treated as Azure.
AZURE_LOOKALIKE = [
    "https://azure.com.attacker.test/v1",       # suffix confusion
    "https://evil.test/azure.com/v1",           # path mention
    "https://evil.test/v1?upstream=azure.com",  # query mention
    "https://user@azure.com.evil.test/v1",      # userinfo trick
    "https://azure.como/v1",                    # one-character suffix
    "https://notazure.com/v1",                  # missing label boundary
    "https://azure.com.evil.test:8443/v1",      # suffix confusion with a port
]


@pytest.mark.parametrize("url", AZURE_OK)
def test_genuine_azure_hosts_are_recognized(url):
    assert base_url_host_matches(url, "azure.com") is True


@pytest.mark.parametrize("url", AZURE_LOOKALIKE)
def test_lookalike_hosts_are_not_azure(url):
    assert base_url_host_matches(url, "azure.com") is False
    # The substring test these sites used to run would have said yes.
    assert "azure.com" in url.lower()


@pytest.mark.parametrize("url", AZURE_OK)
def test_bearer_auth_still_selected_for_real_azure(url):
    assert anthropic_adapter._requires_bearer_auth(url) is True


@pytest.mark.parametrize("url", AZURE_LOOKALIKE)
def test_lookalike_does_not_get_azure_bearer_auth(url):
    """`_requires_bearer_auth` decides the auth header shape, so a lookalike
    winning here changes which credential is put on the wire."""
    assert anthropic_adapter._requires_bearer_auth(url) is False


@pytest.mark.parametrize("url", AZURE_OK)
def test_context_1m_beta_gate_still_applies_to_real_azure(url):
    assert anthropic_adapter._base_url_needs_context_1m_beta(url) is True


@pytest.mark.parametrize("url", AZURE_LOOKALIKE)
def test_context_1m_beta_gate_skips_lookalikes(url):
    assert anthropic_adapter._base_url_needs_context_1m_beta(url) is False


@pytest.mark.parametrize("url", AZURE_LOOKALIKE)
def test_lookalike_endpoint_receives_no_azure_credential(url, monkeypatch):
    """Secret canary: the Azure short circuit must not hand key material to a
    host that merely contains the string `azure.com`."""
    from hermes_cli import runtime_provider

    monkeypatch.setenv("AZURE_ANTHROPIC_KEY", CANARY)
    monkeypatch.setenv("ANTHROPIC_API_KEY", CANARY)

    resolved = runtime_provider._resolve_runtime_provider(
        requested="anthropic",
        explicit_base_url=url,
        explicit_api_key=None,
    )
    assert resolved.get("source") != "azure-explicit", resolved
    assert CANARY not in repr(resolved), resolved


def test_real_azure_endpoint_still_takes_the_short_circuit(monkeypatch):
    """The negative tests above are only meaningful if the positive path works."""
    from hermes_cli import runtime_provider

    monkeypatch.setenv("AZURE_ANTHROPIC_KEY", CANARY)
    resolved = runtime_provider._resolve_runtime_provider(
        requested="anthropic",
        explicit_base_url="https://my-resource.azure.com/v1",
        explicit_api_key=None,
    )
    assert resolved["source"] == "azure-explicit"
    assert resolved["api_mode"] == "anthropic_messages"
    assert resolved["api_key"] == CANARY
