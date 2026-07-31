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
import time
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


def test_wedged_resolver_does_not_hang_the_probe():
    """#63309-style: a wedged OS resolver must not stall the caller — the
    scheduler's pool is sequential (max_workers=1), so a hang here blocks
    every other cron job behind it. Mirrors
    tests/gateway/test_telegram_network.py::test_hung_system_dns_does_not_gate_doh_results.
    """
    def _hung_getaddrinfo(*args, **kwargs):
        time.sleep(5.0)  # far beyond the probe's join bound
        raise OSError("resolver wedged")

    with patch.object(s.socket, "getaddrinfo", side_effect=_hung_getaddrinfo):
        start = time.monotonic()
        result = s._network_looks_down()
        elapsed = time.monotonic() - start

    assert elapsed < 2.5, f"probe blocked on hung resolver ({elapsed:.2f}s)"
    # Inconclusive (still running past the bound) must never read as
    # "confirmed offline" — that would replace a real ⚠️ alert with a 🌐 one.
    assert result is False


def test_message_is_byte_stable_across_calls():
    """The held-queue's coalescing (_hold_undelivered_output) keys on content
    equality — two 🌐 notices for the same job must be identical, not just
    similar."""
    with patch.object(s, "_network_looks_down", return_value=True):
        msg1 = s._summarize_cron_failure_for_delivery(JOB, DNS_ERROR)
        msg2 = s._summarize_cron_failure_for_delivery(JOB, DNS_ERROR)
    assert msg1 == msg2


def test_connect_timeout_phrasing_classifies_offline():
    """A roaming laptop can fail by connect-timeout instead of DNS — the
    machine-side connect-phase phrasing added to _NETWORK_OUTAGE_SIGNATURES
    (mirroring _RETRYABLE_DELIVERY_MARKERS) must classify too."""
    err = (
        "delivery error: Telegram send failed: httpx.ConnectTimeout: "
        "timed out connecting to api.telegram.org"
    )
    with patch.object(s, "_network_looks_down", return_value=True):
        msg = s._summarize_cron_failure_for_delivery(JOB, err)
    assert msg.startswith("🌐")


def test_rate_limit_summary_wins_when_no_network_signature_present():
    """Ordering safety: a rate-limit error with no network signature must
    keep the real rate-limit summary even if the probe would say the network
    is down — the signature gate must short-circuit before the probe runs."""
    err = "Script exited with code 1 stderr: HTTP 429 Too Many Requests from provider"
    with patch.object(
        s, "_network_looks_down", side_effect=AssertionError("probe must not run")
    ):
        msg = s._summarize_cron_failure_for_delivery(JOB, err)
    assert msg.startswith("⚠️")
    assert "rate limit" in msg.lower()
