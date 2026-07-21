"""BUILD-655 regression coverage for task-local attachment source handoff."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _source_ref(source_task_id: str, attachment_id: int, digest: str | None) -> list[dict]:
    ref = {
        "ref": "approved-adr",
        "task_id": source_task_id,
        "attachment_id": attachment_id,
    }
    if digest is not None:
        ref["sha256"] = digest
    return [ref]


def test_materialize_sources_creates_verified_readonly_task_local_inbox(kanban_home):
    payload = b"approved source bytes\n"
    digest = hashlib.sha256(payload).hexdigest()

    with kb.connect() as conn:
        source_task_id = kb.create_task(conn, title="approved source")
        attachment_id = kb.store_attachment_bytes(conn, source_task_id, "source.md", payload)
        task_id = kb.create_task(
            conn,
            title="consumer",
            source_refs=_source_ref(source_task_id, attachment_id, digest),
        )
        task = kb.get_task(conn, task_id)
        attachment = kb.get_attachment(conn, attachment_id)
        assert task is not None
        assert attachment is not None
        workspace = kb.resolve_workspace(task)
        inbox = kb.materialize_sources(kb.resolve_source_refs(conn, task), workspace)

    manifest = json.loads((inbox / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == 1
    assert manifest["entries"] == [{
        "ref": "approved-adr",
        "filename": "source.md",
        "path": ".hermes-sources/approved-adr/source.md",
        "sha256": digest,
        "size": len(payload),
        "source_attachment_id": attachment_id,
    }]
    copied = inbox / "approved-adr" / "source.md"
    assert copied.read_bytes() == payload
    assert copied.stat().st_mode & 0o777 == 0o444
    assert kb.load_task_sources(workspace) == {"approved-adr": copied}
    assert attachment.sha256 == digest
    assert Path(attachment.stored_path).read_bytes() == payload


def test_materialize_sources_rejects_an_unpinned_ref_before_copy(kanban_home):
    with kb.connect() as conn:
        source_task_id = kb.create_task(conn, title="approved source")
        attachment_id = kb.store_attachment_bytes(conn, source_task_id, "source.md", b"x")
        task_id = kb.create_task(
            conn,
            title="consumer",
            source_refs=_source_ref(source_task_id, attachment_id, None),
        )
        task = kb.get_task(conn, task_id)
        workspace = kb.resolve_workspace(task)
        refs = kb.resolve_source_refs(conn, task)

    with pytest.raises(kb.SourceMaterializationError) as error:
        kb.materialize_sources(refs, workspace)
    assert error.value.code == "SOURCE_REF_UNPINNED"
    assert not (workspace / ".hermes-sources").exists()


def test_materialize_sources_rejects_digest_mismatch_without_mutating_attachment(kanban_home):
    payload = b"canonical source\n"
    with kb.connect() as conn:
        source_task_id = kb.create_task(conn, title="approved source")
        attachment_id = kb.store_attachment_bytes(conn, source_task_id, "source.md", payload)
        task_id = kb.create_task(
            conn,
            title="consumer",
            source_refs=_source_ref(source_task_id, attachment_id, "0" * 64),
        )
        task = kb.get_task(conn, task_id)
        attachment = kb.get_attachment(conn, attachment_id)
        workspace = kb.resolve_workspace(task)
        refs = kb.resolve_source_refs(conn, task)

    with pytest.raises(kb.SourceMaterializationError) as error:
        kb.materialize_sources(refs, workspace)
    assert error.value.code == "SOURCE_DIGEST_MISMATCH"
    assert Path(attachment.stored_path).read_bytes() == payload
    assert not (workspace / ".hermes-sources").exists()


def test_load_task_sources_rejects_manifest_path_escape(kanban_home):
    workspace = kanban_home / "workspace"
    sources = workspace / ".hermes-sources"
    sources.mkdir(parents=True)
    (sources / "manifest.json").write_text(json.dumps({
        "version": 1,
        "entries": [{
            "ref": "escape",
            "path": "../outside.txt",
            "sha256": hashlib.sha256(b"outside").hexdigest(),
        }],
    }), encoding="utf-8")
    (kanban_home / "outside.txt").write_bytes(b"outside")

    with pytest.raises(kb.SourceMaterializationError) as error:
        kb.load_task_sources(workspace)
    assert error.value.code == "SOURCE_PATH_ESCAPE"


def test_resolve_source_refs_rejects_ref_directory_traversal(kanban_home):
    payload = b"approved source bytes\n"
    digest = hashlib.sha256(payload).hexdigest()
    with kb.connect() as conn:
        source_task_id = kb.create_task(conn, title="approved source")
        attachment_id = kb.store_attachment_bytes(conn, source_task_id, "source.md", payload)
        task_id = kb.create_task(
            conn,
            title="consumer",
            source_refs=[{
                **_source_ref(source_task_id, attachment_id, digest)[0],
                "ref": "../escape",
            }],
        )
        task = kb.get_task(conn, task_id)
        assert task is not None

        with pytest.raises(kb.SourceMaterializationError) as error:
            kb.resolve_source_refs(conn, task)

    assert error.value.code == "SOURCE_PATH_ESCAPE"


def test_dispatch_blocks_without_spawning_on_source_digest_mismatch(
    kanban_home, all_assignees_spawnable,
):
    """The controller hook fails closed before handing control to a worker."""
    payload = b"approved attachment\n"
    spawned = []

    def fake_spawn(task, workspace):
        spawned.append((task.id, workspace))
        return kb.SpawnReceipt(
            pid=65_500,
            release=lambda: None,
            abort=lambda: None,
            process_started_at=1234.5,
            process_group_id=65_500,
            session_id=65_500,
        )

    with kb.connect() as conn:
        source_task_id = kb.create_task(conn, title="approved source")
        attachment_id = kb.store_attachment_bytes(conn, source_task_id, "source.md", payload)
        task_id = kb.create_task(
            conn,
            title="consumer",
            assignee="alice",
            source_refs=_source_ref(source_task_id, attachment_id, "f" * 64),
        )
        result = kb.dispatch_once(conn, spawn_fn=fake_spawn)
        blocked = kb.get_task(conn, task_id)
        blocked_events = [event for event in kb.list_events(conn, task_id) if event.kind == "blocked"]

    assert spawned == []
    assert task_id in result.auto_blocked
    assert blocked is not None
    assert blocked.status == "blocked"
    assert blocked_events
    assert blocked_events[-1].payload is not None
    assert "SOURCE_DIGEST_MISMATCH" in blocked_events[-1].payload["reason"]


def test_dispatch_feature_flag_disables_attachment_materialization(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """The staged rollback flag keeps legacy dispatch behavior when disabled."""
    monkeypatch.setattr(kb, "_source_materialization_config", lambda: {"attachments": False})
    spawned = []

    def fake_spawn(task, workspace):
        spawned.append((task.id, Path(workspace)))
        return kb.SpawnReceipt(
            pid=65_501,
            release=lambda: None,
            abort=lambda: None,
            process_started_at=1234.5,
            process_group_id=65_501,
            session_id=65_501,
        )

    with kb.connect() as conn:
        source_task_id = kb.create_task(conn, title="approved source")
        attachment_id = kb.store_attachment_bytes(conn, source_task_id, "source.md", b"source")
        task_id = kb.create_task(
            conn,
            title="legacy consumer",
            assignee="alice",
            source_refs=_source_ref(source_task_id, attachment_id, "0" * 64),
        )
        kb.dispatch_once(conn, spawn_fn=fake_spawn)
        dispatched = kb.get_task(conn, task_id)

    assert len(spawned) == 1
    assert not (spawned[0][1] / ".hermes-sources").exists()
    assert dispatched is not None
    assert dispatched.status == "running"
