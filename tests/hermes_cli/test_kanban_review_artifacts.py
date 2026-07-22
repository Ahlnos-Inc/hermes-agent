"""Hermetic contracts for review-scoped artifact rebinding."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    attachments = tmp_path / "attachments"
    db = home / "kanban.db"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_ATTACHMENTS_ROOT", str(attachments))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db(db)
    monkeypatch.setattr(kb.time, "time", lambda: 1_900_000_000)
    return db


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _attach(conn, task_id: str, filename: str, data: bytes) -> tuple[int, str]:
    attachment_id = kb.store_attachment_bytes(
        conn,
        task_id,
        filename,
        data,
        uploaded_by="test",
    )
    attachment = kb.get_attachment(conn, attachment_id)
    assert attachment is not None
    return attachment_id, attachment.stored_path


def _artifact_review(conn, *, initial: bytes = b"ADR v1\n") -> tuple[str, str, int, str]:
    source = kb.create_task(conn, title="architect output", assignee="architect")
    source_attachment, source_path = _attach(conn, source, "adr.md", initial)
    review = kb.create_task(
        conn,
        title="review ADR",
        assignee="reviewer",
        body=f"Review the pinned artifact at `{source_path}`.",
    )
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status = 'review' WHERE id = ?", (review,)
        )
    claimed = kb.claim_review_task(conn, review, claimer="reviewer")
    assert claimed is not None and claimed.current_run_id is not None
    return review, source, source_attachment, source_path


def _request_fix(conn, review: str, number: int = 1) -> tuple[str, int]:
    review_task = kb.get_task(conn, review)
    assert review_task is not None
    run_id = review_task.current_run_id
    if run_id is None:
        claimed = kb.claim_task(conn, review, claimer="reviewer")
        assert claimed is not None and claimed.current_run_id is not None
        run_id = claimed.current_run_id
    result = kb.request_rework(
        conn,
        review,
        finding=f"finding {number}",
        fix=kb.NewFixTask(
            title=f"fix {number}",
            body=f"write ADR v{number + 1}",
            assignee="coder",
        ),
        request_key=f"round-{number}",
        actor="reviewer",
        expected_run_id=int(run_id),
    )
    assert result.fix_task_id is not None
    return result.fix_task_id, result.request_event_id


def _complete_fix(
    conn,
    fix_id: str,
    review_id: str,
    filename: str,
    data: bytes,
    *,
    extra_outputs=None,
) -> int:
    attachment_id, _ = _attach(conn, fix_id, filename, data)
    outputs = [{"review_task_id": review_id, "attachment_id": attachment_id}]
    if extra_outputs:
        outputs.extend(extra_outputs)
    assert kb.complete_task(
        conn,
        fix_id,
        summary="revised ADR",
        review_outputs=outputs,
    )
    return attachment_id


def test_rebind_changes_authoritative_context_without_rewriting_review_body(board: Path) -> None:
    with kb.connect_closing(board) as conn:
        review, source, _source_attachment, original_path = _artifact_review(conn)
        before = kb.get_task(conn, review)
        assert before is not None
        original_body = before.body
        first_fix, first_event = _request_fix(conn, review, 1)
        first_attachment = _complete_fix(
            conn, first_fix, review, "adr-v2.md", b"ADR v2\n"
        )

        binding = kb.get_current_review_artifact(conn, review)
        assert binding is not None
        assert binding.generation == 2
        assert binding.attachment_id == first_attachment
        assert binding.sha256 == _sha(b"ADR v2\n")
        assert binding.source_task_id == first_fix
        assert binding.source_rework_event_id == first_event

        after = kb.get_task(conn, review)
        assert after is not None and after.body == original_body
        context = kb.build_worker_context(
            conn, review, _use_continuation=False, _now_override=1_900_000_001
        )
        assert context.index("## Current review artifact — authoritative") < context.index(
            "## Body"
        )
        assert f"Attachment: {first_attachment}" in context
        assert "SHA-256: " + _sha(b"ADR v2\n") in context
        assert original_path in context
        assert "Supersedes artifact paths preserved" in context


def test_multiple_revisions_advance_monotonically_and_late_replay_cannot_regress(
    board: Path,
) -> None:
    with kb.connect_closing(board) as conn:
        review, _source, _initial_attachment, _path = _artifact_review(conn)
        fix_one, event_one = _request_fix(conn, review, 1)
        attachment_one = _complete_fix(
            conn, fix_one, review, "adr-v2.md", b"ADR v2\n"
        )
        fix_two, event_two = _request_fix(conn, review, 2)
        attachment_two = _complete_fix(
            conn, fix_two, review, "adr-v3.md", b"ADR v3\n"
        )

        current = kb.get_current_review_artifact(conn, review)
        assert current is not None
        assert current.generation == 3
        assert current.attachment_id == attachment_two
        assert current.source_rework_event_id == event_two

        with kb.write_txn(conn):
            replay = kb.bind_review_artifact_in_txn(
                conn,
                review,
                attachment_one,
                fix_one,
                None,
                event_one,
                1,
                1_900_000_010,
            )
        assert replay.generation == 2
        current = kb.get_current_review_artifact(conn, review)
        assert current is not None
        assert current.generation == 3
        assert current.attachment_id == attachment_two
        assert conn.execute(
            "SELECT COUNT(*) FROM review_artifact_bindings WHERE review_task_id = ?",
            (review,),
        ).fetchone()[0] == 3


@pytest.mark.parametrize("failure", ["zero", "wrong-owner", "deleted", "tampered", "multiple"])
def test_completion_selection_fails_closed(board: Path, failure: str) -> None:
    with kb.connect_closing(board) as conn:
        review, _source, _initial_attachment, _path = _artifact_review(conn)
        fix, _event = _request_fix(conn, review)

        if failure == "zero":
            with pytest.raises(kb.ReviewArtifactError, match="selection_required"):
                kb.complete_task(conn, fix, summary="missing selection")
        elif failure == "wrong-owner":
            other = kb.create_task(conn, title="other owner", assignee="other")
            wrong_attachment, _ = _attach(conn, other, "wrong.md", b"wrong\n")
            with pytest.raises(kb.ReviewArtifactError, match="does not belong"):
                kb.complete_task(
                    conn,
                    fix,
                    summary="wrong owner",
                    review_outputs=[
                        {"review_task_id": review, "attachment_id": wrong_attachment}
                    ],
                )
        elif failure == "deleted":
            deleted_attachment, _ = _attach(conn, fix, "deleted.md", b"gone\n")
            assert kb.delete_attachment(conn, deleted_attachment) is not None
            with pytest.raises(kb.ReviewArtifactError, match="unknown attachment"):
                kb.complete_task(
                    conn,
                    fix,
                    summary="deleted",
                    review_outputs=[
                        {"review_task_id": review, "attachment_id": deleted_attachment}
                    ],
                )
        elif failure == "tampered":
            tampered_attachment, tampered_path = _attach(
                conn, fix, "tampered.md", b"before\n"
            )
            Path(tampered_path).write_bytes(b"after\n")
            with pytest.raises(kb.ReviewArtifactError, match="integrity mismatch"):
                kb.complete_task(
                    conn,
                    fix,
                    summary="tampered",
                    review_outputs=[
                        {"review_task_id": review, "attachment_id": tampered_attachment}
                    ],
                )
        else:
            first, _ = _attach(conn, fix, "one.md", b"one\n")
            second, _ = _attach(conn, fix, "two.md", b"two\n")
            with pytest.raises(kb.ReviewArtifactError, match="more than once"):
                kb.complete_task(
                    conn,
                    fix,
                    summary="multiple selections",
                    review_outputs=[
                        {"review_task_id": review, "attachment_id": first},
                        {"review_task_id": review, "attachment_id": second},
                    ],
                )

        task = kb.get_task(conn, fix)
        assert task is not None and task.status in {"ready", "todo"}
        assert kb.get_current_review_artifact(conn, review) is not None
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'completed'",
            (fix,),
        ).fetchone()[0] == 0


def test_referenced_attachment_cannot_be_deleted_and_cli_can_show_or_repair(
    board: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from hermes_cli import kanban as cli

    with kb.connect_closing(board) as conn:
        source = kb.create_task(conn, title="explicit repair source", assignee="coder")
        attachment, _path = _attach(conn, source, "repair.md", b"repair\n")
        review = kb.create_task(conn, title="repair review", assignee="reviewer")

        bind_args = argparse.Namespace(
            review_task_id=review,
            bind_attachment=attachment,
            expected_generation=0,
            json=True,
        )
        assert cli._cmd_review_artifact(bind_args) == 0
        bound = json.loads(capsys.readouterr().out)
        assert bound["generation"] == 1
        assert bound["attachment_id"] == attachment

        with pytest.raises(kb.ReviewArtifactError, match="referenced"):
            kb.delete_attachment(conn, attachment)

        show_args = argparse.Namespace(
            review_task_id=review,
            bind_attachment=None,
            expected_generation=None,
            json=True,
        )
        assert cli._cmd_review_artifact(show_args) == 0
        shown = json.loads(capsys.readouterr().out)
        assert shown["sha256"] == _sha(b"repair\n")


def test_completion_transaction_rolls_back_binding_status_and_events_on_failure(
    board: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with kb.connect_closing(board) as conn:
        review, _source, _initial_attachment, _path = _artifact_review(conn)
        fix, _event = _request_fix(conn, review)
        attachment, _ = _attach(conn, fix, "rollback.md", b"rollback\n")
        claimed = kb.claim_task(conn, fix, claimer="coder")
        assert claimed is not None and claimed.current_run_id is not None
        original_end_run = kb._end_run

        def fail_after_run_close(*args, **kwargs):
            original_end_run(*args, **kwargs)
            raise RuntimeError("injected completion failure")

        monkeypatch.setattr(kb, "_end_run", fail_after_run_close)
        with pytest.raises(RuntimeError, match="injected"):
            kb.complete_task(
                conn,
                fix,
                summary="must roll back",
                review_outputs=[
                    {"review_task_id": review, "attachment_id": attachment}
                ],
                expected_run_id=claimed.current_run_id,
            )

        current = kb.get_current_review_artifact(conn, review)
        assert current is not None and current.generation == 1
        fix_row = kb.get_task(conn, fix)
        assert fix_row is not None and fix_row.status == "running"
        assert conn.execute(
            "SELECT COUNT(*) FROM review_artifact_bindings "
            "WHERE review_task_id = ? AND generation = 2",
            (review,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'completed'",
            (fix,),
        ).fetchone()[0] == 0


def test_legacy_unique_backfill_is_exact_and_ambiguous_backfill_holds_review(
    board: Path,
) -> None:
    with kb.connect_closing(board) as conn:
        source = kb.create_task(conn, title="legacy source", assignee="architect")
        attachment, path = _attach(conn, source, "legacy.md", b"legacy\n")
        review = kb.create_task(
            conn,
            title="legacy review",
            assignee="reviewer",
            body=f"Pinned ADR: {path}",
        )
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (review,))
        original_body = kb.get_task(conn, review).body
        result = kb.reconcile_dependency_waits(conn, now=1_900_000_020)
        assert result.artifact_backfilled == 1
        binding = kb.get_current_review_artifact(conn, review)
        assert binding is not None
        assert binding.generation == 1
        assert binding.attachment_id == attachment
        assert binding.sha256 == _sha(b"legacy\n")
        assert kb.get_task(conn, review).body == original_body

        ambiguous = kb.create_task(
            conn,
            title="ambiguous review",
            assignee="reviewer",
            body=f"Pinned ADR: {path}",
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'review' WHERE id = ?", (ambiguous,)
            )
        kb.add_attachment(
            conn,
            source,
            filename="legacy-copy.md",
            stored_path=path,
            size=len(b"legacy\n"),
        )
        result = kb.reconcile_dependency_waits(conn, now=1_900_000_021)
        assert result.artifact_selection_required == 1
        held = kb.get_task(conn, ambiguous)
        assert held is not None
        assert held.status == "blocked"
        assert held.block_kind == "needs_input"
        assert kb.claim_review_task(conn, ambiguous, claimer="reviewer") is None


def _insert_awaiting_gate(conn, architect_task_id: str) -> str:
    gate_id = "g_review_artifact"
    with kb.write_txn(conn):
        conn.execute(
            """INSERT INTO architecture_gates (
                gate_id, board_key, creator_principal, creator_actor_type,
                creator_profile, architect_task_id, state, policy_version,
                canonicalization_version, accepted_run_id, accepted_snapshot,
                design_digest, enforcement_mode, row_version, created_at, updated_at
            ) VALUES (?, ?, ?, 'agent', 'architect', ?,
                      'validated_awaiting_approval', 'v1', 'v1', 1, '{}', ?,
                      'off', 0, ?, ?)""",
            (
                gate_id,
                kb.get_current_board(),
                "architect",
                architect_task_id,
                "d" * 64,
                1_900_000_030,
                1_900_000_030,
            ),
        )
    return gate_id


def _append_rework_event(conn, review: str, fix: str) -> int:
    with kb.write_txn(conn):
        return kb._append_event(
            conn,
            review,
            "rework_requested",
            {
                "review_task_id": review,
                "fix_task_id": fix,
                "request_key": f"manual-{fix}",
                "fix_action": "created",
            },
        )


def test_architecture_approval_subject_rejects_stale_generation_and_invalidates_after_rebind(
    board: Path,
) -> None:
    with kb.connect_closing(board) as conn:
        review, source, _initial_attachment, _path = _artifact_review(conn)
        architect = source
        claimed = kb.get_task(conn, review)
        assert claimed is not None and claimed.current_run_id is not None
        assert kb.complete_task(conn, review, summary="reviewed generation one")
        assert kb.complete_task(conn, architect, summary="architect output")
        kb.link_tasks(conn, architect, review)
        gate_id = _insert_awaiting_gate(conn, architect)
        context = kb.MutationContext(
            board_key=kb.get_current_board(),
            principal="human-1",
            actor_type="human",
            surface="cli",
        )

        old_subject = kb.architecture_review_approval_subject(conn, gate_id)
        assert old_subject["artifact_generation"] == 1
        fix_one = kb.create_task(conn, title="fix one", assignee="coder")
        attachment_one, _ = _attach(conn, fix_one, "adr-v2.md", b"ADR v2\n")
        old_rework = _append_rework_event(conn, review, fix_one)
        with kb.write_txn(conn):
            kb.bind_review_artifact_in_txn(
                conn, review, attachment_one, fix_one, None, old_rework, 1, 1_900_000_040
            )

        with pytest.raises(kb.ArchitectureGateError, match="approval_evidence_changed"):
            kb.approve_architecture_gate(
                conn,
                gate_id,
                context,
                old_subject["digest"],
                review_task_id=old_subject["review_task_id"],
                review_completion_event_id=old_subject["review_completion_event_id"],
                artifact_generation=old_subject["artifact_generation"],
                artifact_sha256=old_subject["artifact_sha256"],
                now=1_900_000_041,
            )

        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'ready', completed_at = NULL WHERE id = ?",
                (review,),
            )
        claimed_again = kb.claim_task(conn, review, claimer="reviewer")
        assert claimed_again is not None
        assert kb.complete_task(conn, review, summary="reviewed generation two")
        new_subject = kb.architecture_review_approval_subject(conn, gate_id)
        assert new_subject["artifact_generation"] == 2
        approved = kb.approve_architecture_gate(
            conn,
            gate_id,
            context,
            new_subject["digest"],
            review_task_id=new_subject["review_task_id"],
            review_completion_event_id=new_subject["review_completion_event_id"],
            artifact_generation=new_subject["artifact_generation"],
            artifact_sha256=new_subject["artifact_sha256"],
            now=1_900_000_051,
        )
        assert approved.state == "human_approved"

        fix_two = kb.create_task(conn, title="fix two", assignee="coder")
        attachment_two, _ = _attach(conn, fix_two, "adr-v3.md", b"ADR v3\n")
        second_rework = _append_rework_event(conn, review, fix_two)
        with kb.write_txn(conn):
            kb.bind_review_artifact_in_txn(
                conn,
                review,
                attachment_two,
                fix_two,
                None,
                second_rework,
                2,
                1_900_000_050,
            )
        invalidated = kb.get_architecture_gate(conn, gate_id)
        assert invalidated is not None and invalidated.state == "invalidated"
