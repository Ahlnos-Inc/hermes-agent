"""Worker-supplied unblock context (ask/links/artifacts) rides the blocked event.

A ``needs_input`` block is a queue item on a human: the alert it produces
must carry what is being asked, where the content lives, and any files to
preview — otherwise the operator has to open a terminal to find out why the
board is waiting on them. Workers pass the context via the existing
``kanban_block(metadata=...)`` dict; ``block_task`` copies the three
recognized keys into the blocked event payload so the notifier renders them
without chasing run rows.
"""

import json

from hermes_cli import kanban_db as kb


def _board(tmp_path):
    db = tmp_path / "kanban.db"
    kb.init_db(db)
    return kb.connect(db)


def _mktask(conn, title="t"):
    task = kb.create_task(conn, title=title)
    return task if isinstance(task, str) else task.id


def _blocked_payload(conn, task_id):
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='blocked' "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    assert row is not None, "no blocked event recorded"
    return json.loads(row["payload"])


def test_block_metadata_context_lands_in_event_payload(tmp_path):
    conn = _board(tmp_path)
    tid = _mktask(conn, title="review IG posts")
    ok = kb.block_task(
        conn, tid,
        reason="needs approval",
        kind="needs_input",
        metadata={
            "ask": "Approve the 3 IG posts or reply with edits",
            "links": ["https://drive.google.com/drive/folders/abc"],
            "artifacts": ["/tmp/post1.png"],
            "changed_files": ["not-context.py"],
        },
    )
    assert ok
    p = _blocked_payload(conn, tid)
    assert p["ask"] == "Approve the 3 IG posts or reply with edits"
    assert p["links"] == ["https://drive.google.com/drive/folders/abc"]
    assert p["artifacts"] == ["/tmp/post1.png"]
    # Only the recognized context keys cross over; run-row facts stay there.
    assert "changed_files" not in p


def test_block_metadata_context_is_sanitized(tmp_path):
    conn = _board(tmp_path)
    tid = _mktask(conn)
    ok = kb.block_task(
        conn, tid,
        reason="needs approval",
        kind="needs_input",
        metadata={
            "ask": {"not": "a string"},
            "links": [
                "javascript:alert(1)",
                "ftp://nope",
                42,
                "  https://ok.example/a  ",
            ] + [f"https://ok.example/{i}" for i in range(20)],
            "artifacts": [None, "", "/tmp/real.png"] + [f"/tmp/{i}.png" for i in range(20)],
        },
    )
    assert ok
    p = _blocked_payload(conn, tid)
    assert "ask" not in p
    assert all(l.startswith("https://ok.example/") for l in p["links"])
    assert len(p["links"]) == 10
    assert p["links"][0] == "https://ok.example/a"
    assert p["artifacts"][0] == "/tmp/real.png"
    assert len(p["artifacts"]) == 10


def test_block_without_context_keys_keeps_payload_shape(tmp_path):
    conn = _board(tmp_path)
    tid = _mktask(conn)
    assert kb.block_task(conn, tid, reason="waiting", kind="needs_input")
    p = _blocked_payload(conn, tid)
    assert set(p) == {"reason", "kind", "recurrences"}


def test_operator_block_carries_context_too(tmp_path):
    conn = _board(tmp_path)
    tid = _mktask(conn)
    kb.operator_block_task(
        conn, tid,
        reason="hold for review",
        kind="needs_input",
        metadata={"ask": "Confirm the refund", "links": ["https://x.example/r"]},
    )
    p = _blocked_payload(conn, tid)
    assert p["ask"] == "Confirm the refund"
    assert p["links"] == ["https://x.example/r"]
