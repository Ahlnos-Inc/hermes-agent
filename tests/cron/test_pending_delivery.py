"""Held-payload re-delivery for cron output (BUILD-870).

A cron script commits its OWN "already announced" state as soon as it prints —
`vitatide-capture-alerts.cjs` marks a payment seen the moment the alert reaches
stdout — but delivery happens afterwards in the scheduler. Before this change a
failed delivery was recorded (`last_delivery_error`) and dropped, so the alert
was gone for good: money captured, nobody told. Live trigger: 2026-07-30
08:05-08:24, when the host lost DNS and every Telegram send failed with
`httpx.ConnectError: [Errno 8] nodename nor servname provided`.

The rules these tests pin down: hold a payload ONLY when the failure proves it
reached nobody; re-send it in order; and mark every replay, because the
deliberate trade here (Nicholas, 2026-07-30) is never to lose an alert rather
than never to duplicate one.
"""
import logging

import pytest

import cron.scheduler as s
from cron.jobs import get_job, load_jobs, save_jobs


REPLAY_MARKER = "⏳ Held from"

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


def _bodies(sent):
    """Payloads as the channel sees them, minus the replay header.

    A re-delivery is prefixed so a replayed alert can never be mistaken for a
    second capture; a first-time send is not.
    """
    return [t.split("\n\n", 1)[1] if t.startswith(REPLAY_MARKER) else t for t in sent]


# ---------------------------------------------------------------------------
# What gets held
# ---------------------------------------------------------------------------

def test_connect_phase_failure_holds_the_payload(store):
    """AC1 (half 1): a delivery that provably reached nobody is not lost."""
    with pytest.MonkeyPatch.context() as mp:
        _patch_pipeline(mp, _fail_connect)
        s.run_one_job(get_job("capture"))

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
        s.run_one_job(get_job("capture"))

    assert get_job("capture").get("pending_delivery") is None


def test_ambiguity_vetoes_a_retryable_marker(store):
    """A message carrying BOTH a connect error and a timeout stays unheld —
    the timeout half might have landed."""
    def _mixed(job, content, adapters=None, loop=None, delivered_targets=None):
        return f"{CONNECT_ERROR}; delivery to telegram:-100 failed: Timed out"

    with pytest.MonkeyPatch.context() as mp:
        _patch_pipeline(mp, _mixed)
        s.run_one_job(get_job("capture"))

    assert get_job("capture").get("pending_delivery") is None


def test_permanent_failure_is_not_held(store):
    """A misconfigured platform fails identically forever — holding it would
    just retry a doomed send until the age cap."""
    def _permanent(job, content, adapters=None, loop=None, delivered_targets=None):
        return "platform 'telegram' not configured/enabled"

    with pytest.MonkeyPatch.context() as mp:
        _patch_pipeline(mp, _permanent)
        s.run_one_job(get_job("capture"))

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
        s.run_one_job(get_job("capture"))

    assert get_job("capture").get("pending_delivery") is None


def test_successful_delivery_holds_nothing(store):
    """AC2: the happy path must not queue anything to re-send."""
    sent = []
    with pytest.MonkeyPatch.context() as mp:
        _patch_pipeline(mp, _succeed(sent))
        s.run_one_job(get_job("capture"))

    assert len(sent) == 1
    assert get_job("capture").get("pending_delivery") is None


def test_a_long_payload_is_still_held(store):
    """The decision (Nicholas, 2026-07-30): never lose an alert. There is no
    "will this be chunked?" predicate any more — five attempts at one were each
    wrong in a different way, and every one of them silently duplicated when it
    was. A replay is marked instead (see the re-delivery tests)."""
    with pytest.MonkeyPatch.context() as mp:
        _patch_pipeline(mp, _fail_connect, final="x" * 3000)
        s.run_one_job(get_job("capture"))

    assert len(get_job("capture")["pending_delivery"]) == 1


def test_a_payload_with_media_is_still_held(store):
    """Same trade: attachments are separate sends, so a replay can repeat the
    text — visibly, which beats losing the alert."""
    with pytest.MonkeyPatch.context() as mp:
        _patch_pipeline(mp, _fail_connect, final="Daily chart\nMEDIA:/tmp/chart.png")
        s.run_one_job(get_job("capture"))

    assert len(get_job("capture")["pending_delivery"]) == 1


def test_a_payload_too_big_for_the_store_is_dropped_loudly(store, caplog):
    """jobs.json is rewritten under a lock on every run of every job, so there
    is still a size ceiling — a storage bound, not a safety predicate."""
    big = "x" * (s._PENDING_DELIVERY_MAX_PAYLOAD_CHARS + 1)
    with pytest.MonkeyPatch.context() as mp, caplog.at_level(logging.ERROR):
        _patch_pipeline(mp, _fail_connect, final=big)
        s.run_one_job(get_job("capture"))

    assert get_job("capture").get("pending_delivery") is None
    assert any("too large to park" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# What gets re-delivered
# ---------------------------------------------------------------------------

def test_held_payload_is_redelivered_on_the_next_run_exactly_once(store):
    """AC1 + AC4: delivery fails on tick N, succeeds on tick N+1, and the
    channel receives the payload once — not twice, and not never."""
    with pytest.MonkeyPatch.context() as mp:
        _patch_pipeline(mp, _fail_connect)
        s.run_one_job(get_job("capture"))

    held = get_job("capture")["pending_delivery"][0]["content"]
    assert "33Y55Z" in held

    sent = []
    with pytest.MonkeyPatch.context() as mp:
        # Tick N+1: the network is back and this run is silent (no new capture).
        _patch_pipeline(mp, _succeed(sent), final=s.SILENT_MARKER)
        s.run_one_job(get_job("capture"))

    assert _bodies(sent) == [held]
    assert get_job("capture").get("pending_delivery") is None

    # Tick N+2: nothing left to re-send.
    sent.clear()
    with pytest.MonkeyPatch.context() as mp:
        _patch_pipeline(mp, _succeed(sent), final=s.SILENT_MARKER)
        s.run_one_job(get_job("capture"))

    assert _bodies(sent) == []


def test_a_replay_is_marked_and_a_fresh_send_is_not(store):
    """The trade this design makes: a payload is never dropped for fear of
    duplicating it, so a replay has to be unmistakable. A second capture of the
    same amount must never be confusable with a re-sent one."""
    with pytest.MonkeyPatch.context() as mp:
        _patch_pipeline(mp, _fail_connect, final="💳 order A")
        s.run_one_job(get_job("capture"))

    sent = []
    with pytest.MonkeyPatch.context() as mp:
        _patch_pipeline(mp, _succeed(sent), final="💳 order B")
        s.run_one_job(get_job("capture"))

    replay, fresh = sent
    assert replay.startswith(REPLAY_MARKER) and replay.endswith("💳 order A")
    assert "delivery had failed" in replay
    assert fresh == "💳 order B"  # this run's own output is not marked


def test_the_replay_marker_names_when_it_was_held(store):
    """The header carries the time the payload was queued, so an operator can
    place the replay against the outage rather than against now."""
    from datetime import datetime

    queued = "2026-07-30T08:12:00-07:00"
    marker = s._replay_prefix({"queued_at": queued})
    # Rendered in hermes's timezone, matching every other timestamp in the
    # channel — not in whatever zone the string happened to carry.
    expected = (
        datetime.fromisoformat(queued)
        .astimezone(s._hermes_now().tzinfo)
        .strftime("%Y-%m-%d %H:%M")
    )
    assert expected in marker

    # A hand-edited or legacy stamp still produces a usable header. None of
    # these may raise: the flush would read the exception as a delivery failure
    # and drop the very alert this mechanism exists to save.
    for bad in (None, "", "not-a-timestamp", "9999-12-31T23:59:59-14:00", 12345):
        assert "Held from" in s._replay_prefix({"queued_at": bad})


def test_held_payload_goes_out_before_this_runs_own_output(store):
    """Chronological order in the channel: the alert held from the outage
    lands before the one this run just produced."""
    with pytest.MonkeyPatch.context() as mp:
        _patch_pipeline(mp, _fail_connect, final="first alert")
        s.run_one_job(get_job("capture"))

    sent = []
    with pytest.MonkeyPatch.context() as mp:
        _patch_pipeline(mp, _succeed(sent), final="second alert")
        s.run_one_job(get_job("capture"))

    assert _bodies(sent) == ["first alert", "second alert"]


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

    assert _bodies(sent) == ["💳 order A", "💳 order B", "💳 order C"]
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


def test_a_long_outage_overflow_keeps_the_newest_per_job(store, caplog):
    """BUILD-877: the 2026-07-30 outage overflowed this cap 6 times and the
    money alerts past it were dropped. `pending_delivery` lives on each job's
    OWN record (see mutate_job), so overflow can only ever evict that SAME
    job's older entries — a second job racing the same outage is a separate
    record and cannot be touched, no matter how deep the first job's queue
    goes. Every eviction still names the job and the reason, at WARNING+.
    """
    save_jobs(load_jobs() + [{
        "id": "other",
        "name": "another money cron",
        "prompt": "",
        "script": "y.sh",
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

    overflow_by = 3
    with pytest.MonkeyPatch.context() as mp, caplog.at_level(logging.WARNING):
        for n in range(s._PENDING_DELIVERY_MAX_ENTRIES + overflow_by):
            _patch_pipeline(mp, _fail_connect, final=f"capture alert {n}")
            s.run_one_job(get_job("capture"))
        _patch_pipeline(mp, _fail_connect, final="other alert")
        s.run_one_job(get_job("other"))

    capture_queue = get_job("capture")["pending_delivery"]
    other_queue = get_job("other")["pending_delivery"]

    # The newest capture alert survives the overflow...
    newest = s._PENDING_DELIVERY_MAX_ENTRIES + overflow_by - 1
    assert capture_queue[-1]["content"] == f"capture alert {newest}"
    assert len(capture_queue) == s._PENDING_DELIVERY_MAX_ENTRIES
    # ...and the other job's own entry was never touched by capture's overflow.
    assert [e["content"] for e in other_queue] == ["other alert"]

    drops = [r for r in caplog.records if "over its" in r.getMessage()]
    assert len(drops) == overflow_by
    assert all(r.levelno >= logging.WARNING for r in drops)
    assert all("Job 'capture'" in r.getMessage() for r in drops)


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

    assert _bodies(attempted) == ["💳 order A"]  # stopped after the first failure
    queue = get_job("capture")["pending_delivery"]
    assert [e["content"] for e in queue] == ["💳 order A", "💳 order B"]
    # A: sent once per run (3 runs). B: sent once, then only ever queued
    # behind A, which never clears — so it is never re-attempted.
    assert queue[0]["attempts"] == 3 and queue[1]["attempts"] == 1


def test_a_delivered_entry_is_cleared_before_the_next_one_is_tried(store):
    """The queue is persisted after EACH entry, so a crash part-way through a
    flush cannot re-send what already went out."""
    for alert in ("💳 order A", "💳 order B"):
        with pytest.MonkeyPatch.context() as mp:
            _patch_pipeline(mp, _fail_connect, final=alert)
            s.run_one_job(get_job("capture"))

    def _first_ok_then_down(job, content, adapters=None, loop=None, delivered_targets=None):
        if content.endswith("💳 order A"):
            if delivered_targets is not None:
                delivered_targets.append("telegram:-1003907677753")
            return None
        return CONNECT_ERROR

    with pytest.MonkeyPatch.context() as mp:
        _patch_pipeline(mp, _first_ok_then_down, final=s.SILENT_MARKER)
        s.run_one_job(get_job("capture"))

    # A is gone from the queue; only B is still waiting.
    assert [e["content"] for e in get_job("capture")["pending_delivery"]] == ["💳 order B"]


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

    assert _bodies(sent) == ["💳 order A", "💳 order B"]


def test_failed_retry_bumps_attempts_and_keeps_the_original_stamp(store):
    """AC3: the age cap measures the PAYLOAD's age. Refreshing queued_at on
    every retry would make it immortal for as long as the outage lasts."""
    with pytest.MonkeyPatch.context() as mp:
        _patch_pipeline(mp, _fail_connect)
        s.run_one_job(get_job("capture"))
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
        s.run_one_job(get_job("capture"))

    job = get_job("capture")
    job["pending_delivery"][0]["queued_at"] = "2020-01-01T00:00:00"

    sent = []
    with pytest.MonkeyPatch.context() as mp, caplog.at_level(logging.ERROR):
        _patch_pipeline(mp, _succeed(sent), final=s.SILENT_MARKER)
        s.run_one_job(job)

    assert _bodies(sent) == []
    assert get_job("capture").get("pending_delivery") is None
    dropped = [r for r in caplog.records if "held for re-delivery" in r.getMessage()]
    assert dropped and "capture" in dropped[0].getMessage()


def test_unparseable_stamp_is_treated_as_expired(store):
    """Hand-edited or legacy state must not pin a payload in jobs.json forever."""
    with pytest.MonkeyPatch.context() as mp:
        _patch_pipeline(mp, _fail_connect)
        s.run_one_job(get_job("capture"))

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
        s.run_one_job(get_job("capture"))

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
        s.run_one_job(get_job("capture"))

    def _explode(job, content, adapters=None, loop=None, delivered_targets=None):
        raise RuntimeError("httpx.ConnectError: name resolution blew up")

    with pytest.MonkeyPatch.context() as mp:
        _patch_pipeline(mp, _explode, final=s.SILENT_MARKER)
        assert s.run_one_job(get_job("capture")) is True

    # Still held: the raise was a connect-phase failure like any other.
    assert get_job("capture")["pending_delivery"][0]["attempts"] == 2


# ---------------------------------------------------------------------------
# Against the real job lifecycle (no mark_job_run stub)
# ---------------------------------------------------------------------------

def _patch_pipeline_real_lifecycle(monkeypatch, deliver, *, final):
    """Like _patch_pipeline but leaves the REAL mark_job_run in place, so the
    hold has to survive the same record rewrite production does."""
    monkeypatch.setattr(s, "run_job", lambda job, *, defer_agent_teardown=None: (True, "out", final, None))
    monkeypatch.setattr(s, "save_job_output", lambda jid, out: f"/tmp/{jid}.md")
    monkeypatch.setattr(s, "_deliver_result", deliver)


def test_hold_survives_the_real_mark_job_run(store):
    """mark_job_run rewrites the whole record right after the hold is written.
    A recurring job's held payload must still be there afterwards."""
    with pytest.MonkeyPatch.context() as mp:
        _patch_pipeline_real_lifecycle(mp, _fail_connect, final="💳 order A")
        s.run_one_job(get_job("capture"))

    queue = get_job("capture")["pending_delivery"]
    assert [e["content"] for e in queue] == ["💳 order A"]

    sent = []
    with pytest.MonkeyPatch.context() as mp:
        _patch_pipeline_real_lifecycle(mp, _succeed(sent), final=s.SILENT_MARKER)
        s.run_one_job(get_job("capture"))

    assert _bodies(sent) == ["💳 order A"]
    assert get_job("capture").get("pending_delivery") is None


def test_a_finished_one_shot_says_so_when_it_takes_a_payload_with_it(store, caplog):
    """Documented limit: mark_job_run removes a finite job at its repeat limit,
    taking any held payload with it — it never fires again, so nothing could
    flush it. That loss must be loud, not silent."""
    save_jobs([{
        "id": "oneshot",
        "name": "one-shot alert",
        "prompt": "",
        "script": "x.sh",
        "no_agent": True,
        "schedule": {"kind": "once", "run_at": "2026-07-30T12:00:00", "display": "once"},
        "schedule_display": "once",
        "repeat": {"times": 1, "completed": 0},
        "enabled": True,
        "state": "scheduled",
        "next_run_at": "2999-01-01T00:00:00",
        "deliver": "origin",
        "origin": {"platform": "telegram", "chat_id": "-1003907677753"},
    }])

    with pytest.MonkeyPatch.context() as mp, caplog.at_level(logging.ERROR):
        _patch_pipeline_real_lifecycle(mp, _fail_connect, final="💳 one-shot alert")
        s.run_one_job(get_job("oneshot"))

    assert get_job("oneshot") is None
    assert any("can no longer be re-delivered" in r.getMessage() for r in caplog.records)


def test_a_concurrent_hold_is_not_clobbered(store):
    """The queue is written from a snapshot that may be stale. Another run of
    the same job appending its own payload mid-flight must not be overwritten —
    the write re-reads and merges inside the store lock."""
    from cron.jobs import mutate_job

    with pytest.MonkeyPatch.context() as mp:
        _patch_pipeline(mp, _fail_connect, final="💳 order A")
        s.run_one_job(get_job("capture"))

    stale = get_job("capture")  # snapshot taken BEFORE the concurrent append

    # A second process holds its own payload while this run is working.
    mutate_job("capture", lambda rec: rec.__setitem__("pending_delivery", [
        *rec["pending_delivery"],
        {"content": "💳 order B", "queued_at": "2026-07-30T12:00:00", "attempts": 1, "last_error": "x"},
    ]))

    sent = []
    with pytest.MonkeyPatch.context() as mp:
        _patch_pipeline(mp, _succeed(sent), final=s.SILENT_MARKER)
        s.run_one_job(stale)

    # A was delivered from the stale snapshot; B survived the write and is
    # still queued for the next run.
    assert _bodies(sent) == ["💳 order A"]
    assert [e["content"] for e in get_job("capture")["pending_delivery"]] == ["💳 order B"]


def test_a_stale_run_cannot_resurrect_a_delivered_entry(store):
    """Two runs of the same job both hold snapshot [A]. One delivers A and
    clears it; the other fails retryably and writes its snapshot back. Storage
    — not the stale caller — decides: A stays gone, or the next firing would
    re-announce money that already went out."""
    from cron.jobs import mutate_job

    with pytest.MonkeyPatch.context() as mp:
        _patch_pipeline(mp, _fail_connect, final="💳 order A")
        s.run_one_job(get_job("capture"))

    stale = get_job("capture")  # run 2's snapshot, taken before run 1 delivers

    # Run 1 delivers A and clears the queue.
    mutate_job("capture", lambda rec: rec.__setitem__("pending_delivery", None))

    # Run 2 now flushes from its stale snapshot and fails.
    with pytest.MonkeyPatch.context() as mp:
        _patch_pipeline(mp, _fail_connect, final=s.SILENT_MARKER)
        s.run_one_job(stale)

    assert get_job("capture").get("pending_delivery") is None


def test_a_merge_keeps_the_queue_oldest_first(store):
    """A concurrent hold can be older than what this run is writing. The merge
    re-sorts, so recovery still replays in the order the alerts happened."""
    from cron.jobs import mutate_job

    stale = get_job("capture")  # snapshot with an empty queue

    mutate_job("capture", lambda rec: rec.__setitem__("pending_delivery", [{
        "id": "older", "content": "💳 order OLD",
        "queued_at": "2020-01-01T00:00:00", "attempts": 1, "last_error": "x",
    }]))

    with pytest.MonkeyPatch.context() as mp:
        _patch_pipeline(mp, _fail_connect, final="💳 order NEW")
        s.run_one_job(stale)

    assert [e["content"] for e in get_job("capture")["pending_delivery"]] == [
        "💳 order OLD", "💳 order NEW",
    ]


def test_two_concurrent_runs_holding_the_same_text_queue_it_once(store):
    """Each run mints its own entry id, so the id-keyed merge would keep both
    and announce the same thing twice on recovery. Content is the identity of
    record, and the merge honours it."""
    stale_one = get_job("capture")
    stale_two = get_job("capture")

    s._hold_undelivered_output(stale_one, "💳 order A", CONNECT_ERROR, [])
    s._hold_undelivered_output(stale_two, "💳 order A", CONNECT_ERROR, [])

    queue = get_job("capture")["pending_delivery"]
    assert len(queue) == 1
    assert queue[0]["attempts"] == 2


def test_the_merge_orders_by_instant_not_by_string(store):
    """Across a DST fold the ISO strings sort the wrong way round: 01:30-07:00
    reads as earlier than 01:15-08:00 but happened after it."""
    ordered = s._coalesce_identical_payloads(sorted([
        {"id": "b", "content": "second", "queued_at": "2026-11-01T01:30:00-07:00"},
        {"id": "a", "content": "first", "queued_at": "2026-11-01T01:15:00-07:00"},
        {"id": "c", "content": "third", "queued_at": "2026-11-01T01:15:00-08:00"},
    ], key=s._queued_at_order))
    assert [e["content"] for e in ordered] == ["first", "second", "third"]


def test_a_job_with_nowhere_to_deliver_drops_its_payload(store, caplog):
    """The job was switched to deliver=local (or lost its origin) before the
    retry. _deliver_result reaches nobody and reports no error — that is not a
    re-delivery, and the log must not claim it was."""
    with pytest.MonkeyPatch.context() as mp:
        _patch_pipeline(mp, _fail_connect, final="💳 order A")
        s.run_one_job(get_job("capture"))

    def _no_target(job, content, adapters=None, loop=None, delivered_targets=None):
        return None  # local-only job: nothing sent, nothing wrong

    with pytest.MonkeyPatch.context() as mp, caplog.at_level(logging.WARNING):
        _patch_pipeline(mp, _no_target, final=s.SILENT_MARKER)
        s.run_one_job(get_job("capture"))

    assert get_job("capture").get("pending_delivery") is None
    assert any("no longer resolves to any delivery target" in r.getMessage()
               for r in caplog.records)
    assert not any("re-delivered" in r.getMessage() for r in caplog.records)


def test_held_error_string_is_truncated(store):
    """A pathological adapter message must not bloat jobs.json."""
    def _huge_error(job, content, adapters=None, loop=None, delivered_targets=None):
        return "httpx.ConnectError: " + ("detail " * 5000)

    with pytest.MonkeyPatch.context() as mp:
        _patch_pipeline(mp, _huge_error)
        s.run_one_job(get_job("capture"))

    entry = get_job("capture")["pending_delivery"][0]
    assert len(entry["last_error"]) == s._PENDING_DELIVERY_MAX_ERROR_CHARS


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("error,retryable", [
    (CONNECT_ERROR, True),
    ("delivery to telegram:-100 skipped — interpreter is shutting down", True),
    ("delivery to email:x failed: [Errno 61] Connection refused", True),
    ("delivery to telegram:-100 failed: Name or service not known", True),
    # A CONNECT timeout means no connection was established — proof of
    # non-delivery, even though the word "timeout" is in it.
    ("delivery to telegram:-100 failed: httpx.ConnectTimeout", True),
    # ...but a bare timeout alongside it still vetoes: that send may be live.
    ("httpx.ConnectTimeout on target A; delivery error: Timed out on target B", False),
    # A reset can arrive AFTER the peer accepted and processed the request, so
    # it proves nothing about delivery.
    ("delivery to telegram:-100 failed: [Errno 54] Connection reset by peer", False),
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
