"""Network-outage classification of cron failure alerts (BUILD-873).

The gateway runs on a laptop that regularly leaves its network. Every outbound
cron then fails the same way — `getaddrinfo ENOTFOUND <host>` — and the
operator gets ⚠️ failure alerts that look like broken jobs but are really "the
machine is offline". Live trigger: 2026-07-30 17:12, both money crons and the
Telegram send failed simultaneously on a resolver blip.

The rules these tests pin down: an offline-signature failure is re-worded to a
🌐 "machine looks offline" notice ONLY when a neutral host also fails to
resolve (otherwise the job's target is genuinely broken and the real ⚠️ alert
must survive); non-network failures never pay for a probe; and classification
only re-words — failed jobs still always deliver (BUILD-837).
"""
import socket
from unittest.mock import patch

import cron.scheduler as s


JOB = {"id": "j1", "name": "Ahlnos Finance — Card/crypto capture alerts"}
DNS_ERROR = (
    "Script exited with code 2 stderr: vitatide-capture-alerts failed: "
    "getaddrinfo ENOTFOUND admin.vitatide.ca"
)


def test_offline_machine_gets_network_outage_message():
    with patch.object(s, "_network_looks_down", return_value=True):
        msg = s._summarize_cron_failure_for_delivery(JOB, DNS_ERROR)
    assert msg.startswith("🌐")
    assert "offline" in msg
    assert "not a real failure" in msg.lower()
    assert JOB["name"] in msg


def test_online_machine_keeps_real_failure_summary():
    with patch.object(s, "_network_looks_down", return_value=False):
        msg = s._summarize_cron_failure_for_delivery(JOB, DNS_ERROR)
    assert msg.startswith("⚠️")
    assert "ENOTFOUND" in msg


def test_non_network_failure_never_probes():
    with patch.object(
        s, "_network_looks_down", side_effect=AssertionError("probe must not run")
    ):
        msg = s._summarize_cron_failure_for_delivery(
            JOB, "Script exited with code 1 stderr: KeyError: 'order'"
        )
    assert msg.startswith("⚠️")


def test_errno8_connect_error_classifies_offline():
    err = (
        "delivery error: Telegram send failed: httpx.ConnectError: [Errno 8] "
        "nodename nor servname provided, or not known"
    )
    with patch.object(s, "_network_looks_down", return_value=True):
        msg = s._summarize_cron_failure_for_delivery(JOB, err)
    assert msg.startswith("🌐")


def test_probe_reports_down_on_resolver_error():
    with patch.object(
        s.socket, "getaddrinfo",
        side_effect=socket.gaierror(8, "nodename nor servname provided"),
    ):
        assert s._network_looks_down() is True


def test_probe_reports_up_when_resolution_succeeds():
    with patch.object(s.socket, "getaddrinfo", return_value=[]):
        assert s._network_looks_down() is False
