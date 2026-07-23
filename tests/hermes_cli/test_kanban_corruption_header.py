"""BUILD-577: the SQLite-header corruption guard fails closed on a plain open.

Focused single-connection companion to ``test_kanban_corruption_build567.py``
(which drives the same guard through a concurrent spawn/claim race): overwriting
page one's header must make the very next ``connect()`` raise an actionable
``KanbanDbCorruptError`` naming the board, leave a forensic backup, and never
silently recreate the schema.
"""

from hermes_cli import kanban_db as kb


def _corrupt_header(db):
    # Zero the SQLite magic string so the fast-path header guard / first-open
    # integrity probe rejects the board instead of reading a partial page.
    with open(db, "r+b") as handle:
        handle.write(b"\x00" * 16)


def test_corrupt_header_fails_closed_and_backs_up(tmp_path):
    db = tmp_path / "kanban.db"
    kb.init_db(db)
    with kb.connect(db) as conn:
        kb.create_task(conn, title="claim-me")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    _corrupt_header(db)

    try:
        kb.connect(db).close()
        raised = None
    except Exception as exc:  # noqa: BLE001 - type asserted below
        raised = exc

    assert isinstance(raised, kb.KanbanDbCorruptError)
    assert "corrupt" in str(raised).lower()
    assert str(db) in str(raised)
    # The corrupt board was preserved for forensics, never recreated in place.
    assert list(tmp_path.glob("kanban.db.corrupt.*.bak"))
