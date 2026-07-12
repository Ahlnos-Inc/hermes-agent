"""Merge-survival guard: _get_usage backend populates usage["native_quota"].

``tui_gateway/server.py::_get_usage(agent)`` locally imports
``agent.native_quota.get_native_quota_statusbar_for_model`` and writes its
result into ``usage["native_quota"]`` (see server.py ~3080-3090). This is the
same backend wiring the classic CLI status bar exercises
(``cli.py::_get_status_bar_snapshot`` -> ``native_quota_compact``), but no
test in ``tests/test_tui_gateway_server.py`` / ``tests/tui_gateway/`` asserts
this *backend* population — only the frontend render is covered. A fork could
drop this block (or break the call signature) and every existing test would
stay green.

This guard calls the real ``server._get_usage`` with
``agent.native_quota.get_native_quota_statusbar_for_model`` patched at its
import site (the only place ``tui_gateway.server`` refers to it — the import
is local to ``_get_usage``, mirroring how ``tests/cli/test_cli_status_bar.py``
patches the same function) and asserts ``usage["native_quota"]`` equals the
known return value.
"""

from unittest.mock import patch

import tui_gateway.server as server


class _AgentStub:
    """Minimal agent shape _get_usage needs for the native_quota path."""

    model = "gpt-5.5"
    provider = "openai-codex"
    base_url = ""
    api_key = "sk-test"
    account_id = None


class TestForkGuardNativeQuotaUsage:
    def test_get_usage_populates_native_quota_from_backend(self):
        with patch(
            "agent.native_quota.get_native_quota_statusbar_for_model",
            return_value="cdx 5h 12%↻2h 7d 4%↻5d",
        ) as mock_quota:
            usage = server._get_usage(_AgentStub())

        assert usage["native_quota"] == "cdx 5h 12%↻2h 7d 4%↻5d"
        mock_quota.assert_called_once()

    def test_get_usage_native_quota_empty_when_backend_returns_empty(self):
        with patch(
            "agent.native_quota.get_native_quota_statusbar_for_model",
            return_value="",
        ):
            usage = server._get_usage(_AgentStub())

        assert usage["native_quota"] == ""
