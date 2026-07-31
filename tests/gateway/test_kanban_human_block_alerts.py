"""Human-gated blocked events reach the operator's Kanban console topic.

Configured via ``kanban.human_block_alerts`` ({chat_id, thread_id}). The
sweep fires regardless of subscriptions, skips auto-resolving block kinds
(transient/dependency), dedups per event id via the notify_deliveries
ledger, and re-alerts on a re-block after an unblock (new event id).
"""

from gateway.kanban_watchers import (
    _collect_unsubscribed_failure_events,
    _collect_human_blocked_events,
    _human_block_event_is_current,
)
from gateway.kanban_notifications import render_kanban_event
from hermes_cli import kanban_db as kb


def _board(tmp_path):
    db = tmp_path / "kanban.db"
    kb.init_db(db)
    return kb.connect(db)


def _mktask(conn, title="t"):
    task = kb.create_task(conn, title=title)
    return task if isinstance(task, str) else task.id


def _set_blocked(conn, task_id):
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status='blocked', block_kind='needs_input' WHERE id=?",
            (task_id,),
        )


def test_human_blocked_event_is_collected_even_when_subscribed(tmp_path):
    conn = _board(tmp_path)
    task_id = _mktask(conn)
    kb.add_notify_sub(conn, task_id=task_id, platform="telegram", chat_id="123")
    _set_blocked(conn, task_id)
    kb._append_event(conn, task_id, "blocked", {"reason": "needs approval", "kind": "needs_input"})

    out = _collect_human_blocked_events(kb, conn)
    assert [o["task_id"] for o in out] == [task_id]
    assert out[0]["delivery_key"] == f"human-block/{task_id}/{out[0]['event'].id}"


def test_auto_resolving_block_kinds_are_skipped(tmp_path):
    conn = _board(tmp_path)
    task_id = _mktask(conn)
    _set_blocked(conn, task_id)
    kb._append_event(conn, task_id, "blocked", {"reason": "flaky", "kind": "transient"})
    kb._append_event(conn, task_id, "blocked", {"reason": "parents", "kind": "dependency"})
    assert _collect_human_blocked_events(kb, conn) == []


def test_untyped_block_and_triage_escalation_are_collected(tmp_path):
    conn = _board(tmp_path)
    task_id = _mktask(conn)
    _set_blocked(conn, task_id)
    kb._append_event(conn, task_id, "blocked", {"reason": "generic"})
    kb._append_event(conn, task_id, "block_loop_detected", {"reason": "loop", "recurrences": 2})
    out = _collect_human_blocked_events(kb, conn)
    assert [o["event"].kind for o in out] == ["block_loop_detected"]


def test_dashboard_triage_transition_does_not_heal_human_alert(tmp_path):
    """A later dashboard ``status: triage`` event is still human-gated."""
    from plugins.kanban.dashboard.plugin_api import _set_status_direct

    conn = _board(tmp_path)
    task_id = _mktask(conn)
    _set_blocked(conn, task_id)
    kb._append_event(
        conn, task_id, "blocked",
        {"reason": "needs approval", "kind": "needs_input"},
    )
    conn.commit()

    assert _set_status_direct(conn, task_id, "triage")
    task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.status == "triage"
    assert task.block_kind == "needs_input"
    status_event = kb.list_events(conn, task_id)[-1]
    assert status_event.kind == "status"
    assert status_event.payload == {
        "status": "triage", "block_kind": "needs_input",
    }

    items = _collect_human_blocked_events(kb, conn)
    assert [item["task_id"] for item in items] == [task_id]
    items[0]["db_path"] = str(tmp_path / "kanban.db")
    assert _human_block_event_is_current(kb, items[0])


def test_ledger_dedups_per_event_but_reblock_realerts(tmp_path):
    conn = _board(tmp_path)
    task_id = _mktask(conn)
    _set_blocked(conn, task_id)
    kb._append_event(conn, task_id, "blocked", {"reason": "one", "kind": "needs_input"})
    first = _collect_human_blocked_events(kb, conn)
    assert len(first) == 1
    kb.record_notify_delivery(
        conn,
        delivery_key=first[0]["delivery_key"],
        task_id=task_id, platform="telegram", chat_id="-100", thread_id="12483",
        first_event_id=first[0]["event"].id, last_event_id=first[0]["event"].id,
        status="delivered",
    )
    assert _collect_human_blocked_events(kb, conn) == []
    # Re-block after an unblock is a NEW human ask: new event id, new alert.
    kb._append_event(conn, task_id, "blocked", {"reason": "again", "kind": "needs_input"})
    again = _collect_human_blocked_events(kb, conn)
    assert len(again) == 1
    assert again[0]["event"].payload["reason"] == "again"


def test_gave_up_events_alert_the_console(tmp_path):
    conn = _board(tmp_path)
    task_id = _mktask(conn)
    _set_blocked(conn, task_id)
    kb._append_event(conn, task_id, "gave_up", {"error": "pid gone", "failures": 2})
    out = _collect_human_blocked_events(kb, conn)
    assert [o["event"].kind for o in out] == ["gave_up"]


def test_done_and_archived_tasks_are_skipped(tmp_path):
    conn = _board(tmp_path)
    task_id = _mktask(conn)
    _set_blocked(conn, task_id)
    kb._append_event(conn, task_id, "blocked", {"reason": "x", "kind": "needs_input"})
    conn.execute("UPDATE tasks SET status='done' WHERE id=?", (task_id,))
    conn.commit()
    assert _collect_human_blocked_events(kb, conn) == []


def test_alert_healed_before_send_is_suppressed(tmp_path):
    conn = _board(tmp_path)
    task_id = _mktask(conn)
    _set_blocked(conn, task_id)
    kb._append_event(conn, task_id, "blocked", {"reason": "needs approval", "kind": "needs_input"})
    conn.commit()
    items = _collect_human_blocked_events(kb, conn)
    assert len(items) == 1
    items[0]["db_path"] = str(tmp_path / "kanban.db")
    assert _human_block_event_is_current(kb, items[0])

    assert kb.unblock_task(conn, task_id)
    assert not _human_block_event_is_current(kb, items[0])


def test_rework_escalation_reaches_zero_subscription_home_sweep(tmp_path):
    conn = _board(tmp_path)
    task_id = _mktask(conn, title="human approval")
    payload = {
        "human_gate_task_id": task_id,
        "round_count": 5,
        "blocker_digest": "exact-sha-checkout: checkout is still wrong",
    }
    with kb.write_txn(conn):
        kb._append_event(
            conn,
            task_id,
            kb.REWORK_ESCALATION_EVENT_KIND,
            payload,
        )

    assert kb.REWORK_ESCALATION_EVENT_KIND in kb.FAILURE_KINDS
    items = _collect_unsubscribed_failure_events(kb, conn)
    assert len(items) == 1
    assert items[0]["event"].kind == kb.REWORK_ESCALATION_EVENT_KIND
    rendered = render_kanban_event(
        task_id=task_id,
        task=items[0]["task"],
        event=items[0]["event"],
    )
    assert rendered is not None
    assert "5 rounds" in rendered
    assert task_id in rendered
    assert "exact-sha-checkout" in rendered


# ---------------------------------------------------------------------------
# Unblock context rendering: a human-block alert must carry everything needed
# to act — the ask, content links, Jira key, and workspace — not just a reason.


def _render_event(task, payload, kind="blocked", task_id="t_ctx"):
    event = kb.Event(
        id=1, task_id=task_id, kind=kind, payload=payload,
        created_at=0, run_id=None,
    )
    return render_kanban_event(task_id=task_id, task=task, event=event)


def _task_stub(**kw):
    defaults = dict(
        title="review content", assignee=None, body=None,
        branch_name=None, workspace_path=None, result=None,
    )
    defaults.update(kw)
    return type("Task", (), defaults)()


def test_render_blocked_includes_title_ask_and_links():
    task = _task_stub(title="IG posts week 31")
    out = _render_event(task, {
        "reason": "needs approval",
        "kind": "needs_input",
        "recurrences": 1,
        "ask": "Approve the 3 posts or reply with edits",
        "links": ["https://drive.google.com/drive/folders/abc"],
    })
    assert "IG posts week 31" in out
    assert "needs approval" in out
    assert "❓ Approve the 3 posts or reply with edits" in out
    assert "🔗 https://drive.google.com/drive/folders/abc" in out


def test_render_blocked_scrapes_urls_from_reason_when_no_links():
    task = _task_stub()
    out = _render_event(task, {
        "reason": "review draft at https://docs.google.com/d/xyz please",
        "kind": "needs_input",
        "recurrences": 1,
    })
    assert "🔗 https://docs.google.com/d/xyz" in out


def test_render_blocked_surfaces_jira_key_from_task_body():
    # The Telegram adapter linkifies bare Jira keys; the renderer only has to
    # surface a key the message text doesn't already contain.
    task = _task_stub(body="Tracked in BUILD-999.")
    out = _render_event(task, {
        "reason": "needs approval", "kind": "needs_input", "recurrences": 1,
    })
    assert "🎫 BUILD-999" in out

    # Key already visible in the title → no duplicate ticket line.
    task2 = _task_stub(title="BUILD-999 content review", body="BUILD-999")
    out2 = _render_event(task2, {
        "reason": "needs approval", "kind": "needs_input", "recurrences": 1,
    })
    assert "🎫" not in out2


def test_render_blocked_shows_workspace_path():
    task = _task_stub(workspace_path="/tmp/worktrees/t_ctx")
    out = _render_event(task, {
        "reason": "needs approval", "kind": "needs_input", "recurrences": 1,
    })
    assert "📁 /tmp/worktrees/t_ctx" in out


def test_render_blocked_redacts_credentials_in_links():
    """Links pass the same force-redact boundary as reason/ask: a vendor
    credential embedded in a worker-supplied URL is masked. Generic query
    params deliberately pass through unmasked — the global redactor's
    web-URL policy (see ``agent/redact.py``): a block link may be a
    pre-signed/magic URL the operator must be able to click.
    """
    marker = "AKIA" + "IOSFODNN7EXAMPLE"  # canonical AWS docs example id
    task = _task_stub()
    out = _render_event(task, {
        "reason": "needs approval",
        "kind": "needs_input",
        "recurrences": 1,
        "links": [
            f"https://bucket.s3.example/post.png?X-Amz-Credential={marker}&sig=abc",
        ],
    })
    assert marker not in out
    assert "🔗 https://bucket.s3.example/post.png" in out

    # Scraped-from-reason fallback is redacted the same way.
    out2 = _render_event(task, {
        "reason": f"see https://x.example/f?cred={marker} for the draft",
        "kind": "needs_input",
        "recurrences": 1,
    })
    assert marker not in out2


def test_render_block_loop_detected_includes_context():
    task = _task_stub()
    out = _render_event(task, {
        "reason": "still needs approval",
        "kind": "needs_input",
        "recurrences": 3,
        "limit": 3,
        "ask": "Approve or kill this",
        "links": ["https://drive.google.com/x"],
    }, kind="block_loop_detected")
    assert "❓ Approve or kill this" in out
    assert "🔗 https://drive.google.com/x" in out
