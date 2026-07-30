"""Held-payload re-delivery for cron output (BUILD-870).

A cron script commits its OWN "already announced" state as soon as it prints —
`vitatide-capture-alerts.cjs` marks a payment seen the moment the alert reaches
stdout — but delivery happens afterwards in the scheduler. Before this change a
failed delivery was recorded (`last_delivery_error`) and dropped, so the alert
was gone for good: money captured, nobody told. Live trigger: 2026-07-30
08:05-08:24, when the host lost DNS and every Telegram send failed with
`httpx.ConnectError: [Errno 8] nodename nor servname provided`.

The rule these tests pin down: hold a payload ONLY when the failure proves it
reached nobody, and re-send it at most once.
"""
import logging

import pytest

import cron.scheduler as s
from cron.jobs import get_job, save_jobs


CONNECT_ERROR = (
    "live adapter delivery to telegram:-1003907677753 failed: httpx.ConnectError: "
    "[Errno 8] nodename nor servname provided, or not known; "
    "delivery error: Telegram send failed: httpx.ConnectError: [Errno 8] "
    "nodename nor servname provided, or not known"
)


@pytest.fixture
def store(tmp_path, monkeypatch):
    """An isolated cron job store containing one recurring job."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "cron").mkdir(parents=True, exist_ok=True)
    save_jobs([{
        "id": "capture",
        "name": "Ahlnos Finance — Card/crypto capture alerts",
        "prompt": "",
        "script": "vitatide-capture-alerts.sh",
        "no_agent": True,
        "schedule": {"kind": "interval", "minutes": 5, "display": "every 5m"},
        "schedule_display": "every 5m",
        "repeat": {"times": None, "completed": 0},
        "enabled": True,
        "state": "scheduled",
        "next_run_at": "2999-01-01T00:00:00",
        "deliver": "origin",
        "origin": {"platform": "telegram", "chat_id": "-1003907677753"},
    }])
    return tmp_path


def _patch_pipeline(monkeypatch, deliver, *, final="💳 Card order paid · 33Y55Z"):
    """Patch run_one_job's primitives; `deliver` stands in for _deliver_result."""
    monkeypatch.setattr(s, "run_job", lambda job, *, defer_agent_teardown=None: (True, "out", final, None))
    monkeypatch.setattr(s, "save_job_output", lambda jid, out: f"/tmp/{jid}.md")
    monkeypatch.setattr(s, "mark_job_run", lambda *a, **kw: None)
    monkeypatch.setattr(s, "_deliver_result", deliver)


def _fail_connect(job, content, adapters=None, loop=None, delivered_targets=None):
    return CONNECT_ERROR


def _succeed(sent):
    def _deliver(job, content, adapters=None, loop=None, delivered_targets=None):
        sent.append(content)
        if delivered_targets is not None:
            delivered_targets.append("telegram:-1003907677753")
        return None
    return _deliver


# ---------------------------------------------------------------------------
# What gets held
# ---------------------------------------------------------------------------

def test_connect_phase_failure_holds_the_payload(store):
    """AC1 (half 1): a delivery that provably reached nobody is not lost."""
    with pytest.MonkeyPatch.context() as mp:
        _patch_pipeline(mp, _fail_connect)
        s.run_one_job({"id": "capture", "name": "capture"})

    held = get_job("capture")["pending_delivery"]
    assert len(held) == 1
    pending = held[0]
    assert "33Y55Z" in pending["content"]
    assert pending["attempts"] == 1
    assert pending["last_error"] == CONNECT_ERROR


def test_timeout_is_never_held(store):
    """A timed-out send may be in flight; re-sending it could double-post a
    money alert. BUILD-731 made the same call for the same reason."""
    def _timeout(job, content, adapters=None, loop=None, delivered_targets=None):
        return "delivery error: Telegram send failed: Timed out"

    with pytest.MonkeyPatch.context() as mp:
        _patch_pipeline(mp, _timeout)
        s.run_one_job({"id": "capture", "name": "capture"})

    assert get_job("capture").get("pending_delivery") is None


def test_ambiguity_vetoes_a_retryable_marker(store):
    """A message carrying BOTH a connect error and a timeout stays unheld —
    the timeout half might have landed."""
    def _mixed(job, content, adapters=None, loop=None, delivered_targets=None):
        return f"{CONNECT_ERROR}; delivery to telegram:-100 failed: Timed out"

    with pytest.MonkeyPatch.context() as mp:
        _patch_pipeline(mp, _mixed)
        s.run_one_job({"id": "capture", "name": "capture"})

    assert get_job("capture").get("pending_delivery") is None


def test_permanent_failure_is_not_held(store):
    """A misconfigured platform fails identically forever — holding it would
    just retry a doomed send until the age cap."""
    def _permanent(job, content, adapters=None, loop=None, delivered_targets=None):
        return "platform 'telegram' not configured/enabled"

    with pytest.MonkeyPatch.context() as mp:
        _patch_pipeline(mp, _permanent)
        s.run_one_job({"id": "capture", "name": "capture"})

    assert get_job("capture").get("pending_delivery") is None


def test_partial_delivery_is_not_held(store):
    """AC2: one target succeeded, another did not — re-sending the payload
    would duplicate it for the target that already has it."""
    def _partial(job, content, adapters=None, loop=None, delivered_targets=None):
        if delivered_targets is not None:
            delivered_targets.append("telegram:-1003907677753")
        return CONNECT_ERROR

    with pytest.MonkeyPatch.context() as mp:
        _patch_pipeline(mp, _partial)
        s.run_one_job({"id": "capture", "name": "capture"})

    assert get_job("capture").get("pending_delivery") is None


def test_successful_delivery_holds_nothing(store):
    """AC2: the happy path must not queue anything to re-send."""
    sent = []
    with pytest.MonkeyPatch.context() as mp:
        _patch_pipeline(mp, _succeed(sent))
        s.run_one_job({"id": "capture", "name": "capture"})

    assert len(sent) == 1
    assert get_job("capture").get("pending_delivery") is None


def test_oversized_payload_is_dropped_loudly(store, caplog):
    """jobs.json is rewritten under a lock on every run of every job — a huge
    report is dropped with an ERROR rather than parked in it."""
    big = "x" * (s._PENDING_DELIVERY_MAX_BYTES + 1)
    with pytest.MonkeyPatch.context() as mp, caplog.at_level(logging.ERROR):
        _patch_pipeline(mp, _fail_connect, final=big)
        s.run_one_job({"id": "capture", "name": "capture"})

    assert get_job("capture").get("pending_delivery") is None
    assert any("larger than" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# What gets re-delivered
# ---------------------------------------------------------------------------

def test_held_payload_is_redelivered_on_the_next_run_exactly_once(store):
    """AC1 + AC4: delivery fails on tick N, succeeds on tick N+1, and the
    channel receives the payload once — not twice, and not never."""
    with pytest.MonkeyPatch.context() as mp:
        _patch_pipeline(mp, _fail_connect)
        s.run_one_job({"id": "capture", "name": "capture"})

    held = get_job("capture")["pending_delivery"][0]["content"]
    assert "33Y55Z" in held

    sent = []
    with pytest.MonkeyPatch.context() as mp:
        # Tick N+1: the network is back and this run is silent (no new capture).
        _patch_pipeline(mp, _succeed(sent), final=s.SILENT_MARKER)
        s.run_one_job(get_job("capture"))

    assert sent == [held]
    assert get_job("capture").get("pending_delivery") is None

    # Tick N+2: nothing left to re-send.
    sent.clear()
    with pytest.MonkeyPatch.context() as mp:
        _patch_pipeline(mp, _succeed(sent), final=s.SILENT_MARKER)
        s.run_one_job(get_job("capture"))

    assert sent == []


def test_held_payload_goes_out_before_this_runs_own_output(store):
    """Chronological order in the channel: the alert held from the outage
    lands before the one this run just produced."""
    with pytest.MonkeyPatch.context() as mp:
        _patch_pipeline(mp, _fail_connect, final="first alert")
        s.run_one_job({"id": "capture", "name": "capture"})

    sent = []
    with pytest.MonkeyPatch.context() as mp:
        _patch_pipeline(mp, _succeed(sent), final="second alert")
        s.run_one_job(get_job("capture"))

    assert [c for c in sent] == ["first alert", "second alert"]


def test_every_alert_in_one_outage_survives(store):
    """An outage spans several firings. A single held slot would let each new
    alert overwrite the last, losing exactly the money alert this mechanism
    exists to protect — so the hold is a queue, flushed oldest first.
    """
    for alert in ("💳 order A", "💳 order B", "💳 order C"):
        with pytest.MonkeyPatch.context() as mp:
            _patch_pipeline(mp, _fail_connect, final=alert)
            s.run_one_job(get_job("capture"))

    assert [e["content"] for e in get_job("capture")["pending_delivery"]] == [
        "💳 order A", "💳 order B", "💳 order C",
    ]

    sent = []
    with pytest.MonkeyPatch.context() as mp:
        _patch_pipeline(mp, _succeed(sent), final=s.SILENT_MARKER)
        s.run_one_job(get_job("capture"))

    assert sent == ["💳 order A", "💳 order B", "💳 order C"]
    assert get_job("capture").get("pending_delivery") is None


def test_a_repeated_identical_alert_is_queued_once(store):
    """A job that emits the SAME notice every tick through an outage (a failing
    script, say) must not fill the queue with copies of it."""
    for _ in range(4):
        with pytest.MonkeyPatch.context() as mp:
            _patch_pipeline(mp, _fail_connect, final="same failure notice")
            s.run_one_job(get_job("capture"))

    queue = get_job("capture")["pending_delivery"]
    assert len(queue) == 1
    # 7 = 1 (run 1's own send) + 2 per later run (the flush retry, then that
    # run's own send of the same text). `attempts` counts real failed sends.
    assert queue[0]["attempts"] == 7


def test_queue_cap_drops_the_oldest_loudly(store, caplog):
    """AC3: bounded. jobs.json is rewritten on every run of every job, so the
    queue cannot grow without limit — and a drop is never silent."""
    with pytest.MonkeyPatch.context() as mp, caplog.at_level(logging.ERROR):
        for n in range(s._PENDING_DELIVERY_MAX_ENTRIES + 2):
            _patch_pipeline(mp, _fail_connect, final=f"alert {n}")
            s.run_one_job(get_job("capture"))

    queue = get_job("capture")["pending_delivery"]
    assert len(queue) == s._PENDING_DELIVERY_MAX_ENTRIES
    assert queue[0]["content"] == "alert 2"  # the two oldest were dropped
    assert len([r for r in caplog.records if "over its" in r.getMessage()]) == 2


def test_a_still_down_link_keeps_the_rest_of_the_queue_in_order(store):
    """The first re-delivery failure stops the flush: retrying the rest would
    pile up failures, and a later partial success could reorder the channel."""
    for alert in ("💳 order A", "💳 order B"):
        with pytest.MonkeyPatch.context() as mp:
            _patch_pipeline(mp, _fail_connect, final=alert)
            s.run_one_job(get_job("capture"))

    attempted = []

    def _still_down(job, content, adapters=None, loop=None, delivered_targets=None):
        attempted.append(content)
        return CONNECT_ERROR

    with pytest.MonkeyPatch.context() as mp:
        _patch_pipeline(mp, _still_down, final=s.SILENT_MARKER)
        s.run_one_job(get_job("capture"))

    assert attempted == ["💳 order A"]  # stopped after the first failure
    queue = get_job("capture")["pending_delivery"]
    assert [e["content"] for e in queue] == ["💳 order A", "💳 order B"]
    # A: sent once per run (3 runs). B: sent once, then only ever queued
    # behind A, which never clears — so it is never re-attempted.
    assert queue[0]["attempts"] == 3 and queue[1]["attempts"] == 1


def test_this_runs_output_queues_behind_the_held_ones(store):
    """Chronological order survives a flush that failed: the new alert goes to
    the BACK of the queue, not in front of the one still waiting."""
    with pytest.MonkeyPatch.context() as mp:
        _patch_pipeline(mp, _fail_connect, final="💳 order A")
        s.run_one_job(get_job("capture"))

    with pytest.MonkeyPatch.context() as mp:
        _patch_pipeline(mp, _fail_connect, final="💳 order B")
        s.run_one_job(get_job("capture"))

    sent = []
    with pytest.MonkeyPatch.context() as mp:
        _patch_pipeline(mp, _succeed(sent), final=s.SILENT_MARKER)
        s.run_one_job(get_job("capture"))

    assert sent == ["💳 order A", "💳 order B"]


def test_failed_retry_bumps_attempts_and_keeps_the_original_stamp(store):
    """AC3: the age cap measures the PAYLOAD's age. Refreshing queued_at on
    every retry would make it immortal for as long as the outage lasts."""
    with pytest.MonkeyPatch.context() as mp:
        _patch_pipeline(mp, _fail_connect)
        s.run_one_job({"id": "capture", "name": "capture"})
    first = get_job("capture")["pending_delivery"][0]

    with pytest.MonkeyPatch.context() as mp:
        _patch_pipeline(mp, _fail_connect, final=s.SILENT_MARKER)
        s.run_one_job(get_job("capture"))
    second = get_job("capture")["pending_delivery"][0]

    assert second["queued_at"] == first["queued_at"]
    assert second["attempts"] == 2
    assert second["content"] == first["content"]


def test_stale_payload_is_dropped_without_delivering(store, caplog):
    """AC3: past the age cap the payload is dropped with an actionable log
    line instead of being re-sent forever."""
    with pytest.MonkeyPatch.context() as mp:
        _patch_pipeline(mp, _fail_connect)
        s.run_one_job({"id": "capture", "name": "capture"})

    job = get_job("capture")
    job["pending_delivery"][0]["queued_at"] = "2020-01-01T00:00:00"

    sent = []
    with pytest.MonkeyPatch.context() as mp, caplog.at_level(logging.ERROR):
        _patch_pipeline(mp, _succeed(sent), final=s.SILENT_MARKER)
        s.run_one_job(job)

    assert sent == []
    assert get_job("capture").get("pending_delivery") is None
    dropped = [r for r in caplog.records if "held for re-delivery" in r.getMessage()]
    assert dropped and "capture" in dropped[0].getMessage()


def test_unparseable_stamp_is_treated_as_expired(store):
    """Hand-edited or legacy state must not pin a payload in jobs.json forever."""
    with pytest.MonkeyPatch.context() as mp:
        _patch_pipeline(mp, _fail_connect)
        s.run_one_job({"id": "capture", "name": "capture"})

    job = get_job("capture")
    job["pending_delivery"][0]["queued_at"] = "not-a-timestamp"

    with pytest.MonkeyPatch.context() as mp:
        _patch_pipeline(mp, _succeed([]), final=s.SILENT_MARKER)
        s.run_one_job(job)

    assert get_job("capture").get("pending_delivery") is None


def test_retry_failing_permanently_drops_the_payload(store, caplog):
    """Held for a connect error, retried into a permanent one: stop holding."""
    with pytest.MonkeyPatch.context() as mp:
        _patch_pipeline(mp, _fail_connect)
        s.run_one_job({"id": "capture", "name": "capture"})

    def _permanent(job, content, adapters=None, loop=None, delivered_targets=None):
        return "unknown platform 'telegram'"

    with pytest.MonkeyPatch.context() as mp, caplog.at_level(logging.ERROR):
        _patch_pipeline(mp, _permanent, final=s.SILENT_MARKER)
        s.run_one_job(get_job("capture"))

    assert get_job("capture").get("pending_delivery") is None
    assert any("non-retryable" in r.getMessage() for r in caplog.records)


def test_redelivery_failure_does_not_fail_the_run(store):
    """A re-delivery blow-up must not take down the run that is carrying it."""
    with pytest.MonkeyPatch.context() as mp:
        _patch_pipeline(mp, _fail_connect)
        s.run_one_job({"id": "capture", "name": "capture"})

    def _explode(job, content, adapters=None, loop=None, delivered_targets=None):
        raise RuntimeError("httpx.ConnectError: name resolution blew up")

    with pytest.MonkeyPatch.context() as mp:
        _patch_pipeline(mp, _explode, final=s.SILENT_MARKER)
        assert s.run_one_job(get_job("capture")) is True

    # Still held: the raise was a connect-phase failure like any other.
    assert get_job("capture")["pending_delivery"][0]["attempts"] == 2


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("error,retryable", [
    (CONNECT_ERROR, True),
    ("delivery to telegram:-100 skipped — interpreter is shutting down", True),
    ("delivery to email:x failed: [Errno 61] Connection refused", True),
    ("delivery to telegram:-100 failed: Name or service not known", True),
    ("delivery error: Telegram send failed: Timed out", False),
    ("live adapter confirmation timed out", False),
    ("platform 'telegram' not configured/enabled", False),
    ("unknown platform 'signal'", False),
    ("no delivery target resolved for deliver=origin", False),
    ("configured thread_id 7 for telegram:-100 was not found; delivered without thread_id", False),
    ("", False),
    (None, False),
])
def test_retryable_classification(error, retryable):
    assert s._is_retryable_delivery_error(error) is retryable
