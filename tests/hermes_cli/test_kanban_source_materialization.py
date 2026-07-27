"""BUILD-655 regression coverage for task-local attachment source handoff."""

from __future__ import annotations

import hashlib
import json
import subprocess
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


def _git_bundle(tmp_path: Path, ref: str = "refs/heads/source") -> tuple[bytes, str, str]:
    """Create a self-contained bundle with one named ref for identity tests."""
    repo = tmp_path / "source-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "source.txt").write_text("source\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "source.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "source"], check=True)
    subprocess.run(["git", "-C", str(repo), "branch", "-M", ref.removeprefix("refs/heads/")], check=True)
    commit = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True,
    ).strip()
    bundle = tmp_path / "source.bundle"
    subprocess.run(["git", "-C", str(repo), "bundle", "create", str(bundle), ref], check=True)
    return bundle.read_bytes(), commit, ref


def _approve_source_task(conn, task_id: str) -> None:
    conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (task_id,))


def test_materialize_sources_rejects_mismatched_git_bundle_ref(kanban_home, tmp_path):
    payload, commit, _ = _git_bundle(tmp_path)
    digest = hashlib.sha256(payload).hexdigest()

    with kb.connect() as conn:
        source_task_id = kb.create_task(conn, title="approved source")
        attachment_id = kb.store_attachment_bytes(conn, source_task_id, "source.bundle", payload)
        _approve_source_task(conn, source_task_id)
        task_id = kb.create_task(
            conn,
            title="consumer",
            parents=[source_task_id],
            source_refs=[{
                **_source_ref(source_task_id, attachment_id, digest)[0],
                "git_commit": commit,
                "git_ref": "refs/heads/not-the-bundle-ref",
            }],
        )
        task = kb.get_task(conn, task_id)
        assert task is not None
        workspace = kb.resolve_workspace(task)

        with pytest.raises(kb.SourceMaterializationError) as error:
            kb.materialize_sources(kb.resolve_source_refs(conn, task), workspace)

    assert error.value.code == "SOURCE_BUNDLE_REF_MISMATCH"


def test_create_task_rejects_unbounded_source_declarations(kanban_home):
    source_refs = [{
        "ref": f"bundle-{index}",
        "task_id": "t_source",
        "attachment_id": 1,
    } for index in range(17)]
    with kb.connect() as conn:
        with pytest.raises(ValueError, match="at most 16"):
            kb.create_task(conn, title="consumer", source_refs=source_refs)


def test_materialize_sources_creates_verified_readonly_task_local_inbox(kanban_home, tmp_path):
    payload, commit, git_ref = _git_bundle(tmp_path)
    digest = hashlib.sha256(payload).hexdigest()

    with kb.connect() as conn:
        source_task_id = kb.create_task(conn, title="approved source")
        attachment_id = kb.store_attachment_bytes(conn, source_task_id, "source.bundle", payload)
        _approve_source_task(conn, source_task_id)
        task_id = kb.create_task(
            conn,
            title="consumer",
            parents=[source_task_id],
            source_refs=[{
                **_source_ref(source_task_id, attachment_id, digest)[0],
                "git_commit": commit,
                "git_ref": git_ref,
            }],
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
        "filename": "source.bundle",
        "path": ".hermes-sources/approved-adr/source.bundle",
        "sha256": digest,
        "size": len(payload),
        "source_attachment_id": attachment_id,
    }]
    copied = inbox / "approved-adr" / "source.bundle"
    assert copied.read_bytes() == payload
    assert copied.stat().st_mode & 0o777 == 0o444
    assert kb.load_task_sources(workspace) == {"approved-adr": copied}
    assert attachment.sha256 == digest
    assert Path(attachment.stored_path).read_bytes() == payload


def test_materialize_sources_rejects_an_unpinned_ref_before_copy(kanban_home):
    with kb.connect() as conn:
        source_task_id = kb.create_task(conn, title="approved source")
        attachment_id = kb.store_attachment_bytes(conn, source_task_id, "source.md", b"x")
        _approve_source_task(conn, source_task_id)
        task_id = kb.create_task(
            conn,
            title="consumer",
            parents=[source_task_id],
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
        _approve_source_task(conn, source_task_id)
        task_id = kb.create_task(
            conn,
            title="consumer",
            parents=[source_task_id],
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


def test_dispatch_type_blocks_corrupt_persisted_source_declaration(kanban_home, all_assignees_spawnable):
    spawned = []

    def fake_spawn(task, workspace):
        spawned.append((task.id, workspace))
        raise AssertionError("corrupt source declaration must block before spawn")

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="consumer", assignee="alice")
        conn.execute(
            "UPDATE tasks SET source_refs_json = ? WHERE id = ?",
            ("{not-json", task_id),
        )
        result = kb.dispatch_once(conn, spawn_fn=fake_spawn)
        blocked = kb.get_task(conn, task_id)

    assert spawned == []
    assert task_id in result.auto_blocked
    assert blocked is not None
    assert blocked.status == "blocked"


def test_dispatch_blocks_cross_tenant_attachment_before_spawn(
    kanban_home, all_assignees_spawnable, tmp_path,
):
    payload, commit, git_ref = _git_bundle(tmp_path)
    digest = hashlib.sha256(payload).hexdigest()
    spawned = []

    def fake_spawn(task, workspace):
        spawned.append((task.id, workspace))
        raise AssertionError("cross-tenant source must block before spawn")

    with kb.connect() as conn:
        source_task_id = kb.create_task(conn, title="approved source", tenant="tenant-a")
        attachment_id = kb.store_attachment_bytes(conn, source_task_id, "source.bundle", payload)
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (source_task_id,))
        task_id = kb.create_task(
            conn,
            title="consumer",
            assignee="alice",
            tenant="tenant-b",
            parents=[source_task_id],
            source_refs=[{
                **_source_ref(source_task_id, attachment_id, digest)[0],
                "git_commit": commit,
                "git_ref": git_ref,
            }],
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
    assert "SOURCE_REF_UNAUTHORIZED" in blocked_events[-1].payload["reason"]


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
        _approve_source_task(conn, source_task_id)
        task_id = kb.create_task(
            conn,
            title="consumer",
            assignee="alice",
            parents=[source_task_id],
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


@pytest.mark.skipif(not Path("/usr/bin/sandbox-exec").exists(), reason="macOS sandbox-exec")
def test_dispatch_materializes_bundle_for_seatbelt_while_denies_attachment_store(
    kanban_home, all_assignees_spawnable, tmp_path,
):
    """The controller copies approved sources into the only Seatbelt-readable root."""
    from agent.claude_workspace_terminal import build_workspace_seatbelt_profile

    payload, commit, git_ref = _git_bundle(tmp_path)
    digest = hashlib.sha256(payload).hexdigest()
    seatbelt_results = []
    attachment_path = ""

    def fake_spawn(task, workspace):
        source_path = Path(workspace) / ".hermes-sources" / "approved-adr" / "source.bundle"
        profile = build_workspace_seatbelt_profile(
            workspace=workspace,
            host_home=tmp_path / "host",
            allow_network=False,
        )
        command = (
            f"cat {source_path!s} >/dev/null && "
            f"! cat {attachment_path!s} >/dev/null 2>&1"
        )
        seatbelt_results.append(subprocess.run(
            ["/usr/bin/sandbox-exec", "-p", profile, "/bin/sh", "-c", command],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=20,
        ))
        return kb.SpawnReceipt(
            pid=65_502,
            release=lambda: None,
            abort=lambda: None,
            process_started_at=1234.5,
            process_group_id=65_502,
            session_id=65_502,
        )

    with kb.connect() as conn:
        source_task_id = kb.create_task(conn, title="approved source")
        attachment_id = kb.store_attachment_bytes(conn, source_task_id, "source.bundle", payload)
        attachment = kb.get_attachment(conn, attachment_id)
        assert attachment is not None
        attachment_path = attachment.stored_path
        _approve_source_task(conn, source_task_id)
        task_id = kb.create_task(
            conn,
            title="seatbelt consumer",
            assignee="alice",
            parents=[source_task_id],
            source_refs=[{
                **_source_ref(source_task_id, attachment_id, digest)[0],
                "git_commit": commit,
                "git_ref": git_ref,
            }],
        )
        result = kb.dispatch_once(conn, spawn_fn=fake_spawn)

    assert task_id in [task[0] for task in result.spawned]
    assert len(seatbelt_results) == 1
    assert seatbelt_results[0].returncode == 0, seatbelt_results[0].stderr
