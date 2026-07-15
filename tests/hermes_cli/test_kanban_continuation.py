"""Durable continuation integration for the existing Kanban kernel."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_continuation import (
    ContinuationContractError,
    compile_context,
)


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    kb.init_db()
    return home


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "-b", "main", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@example.com"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Hermes Test"],
        check=True,
        capture_output=True,
        text=True,
    )
    (path / "README.md").write_text("initial\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(path), "add", "README.md"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "initial"],
        check=True,
        capture_output=True,
        text=True,
    )
    return path


def _config(*, deny=None):
    return {
        "enabled": True,
        "max_core_bytes": 16 * 1024,
        "max_total_bytes": 48 * 1024,
        "provider_policy": {
            "allow": [],
            "deny": list(deny or ["openrouter"]),
        },
    }


def _claim_with_manifest(
    conn,
    *,
    repo: Path,
    title: str = "BUILD-487 durable continuation",
    body: str = "- [ ] Existing Kanban workflow remains authoritative",
    assignee: str = "coder",
):
    task_id = kb.create_task(
        conn,
        title=title,
        body=body,
        assignee=assignee,
        workspace_kind="dir",
        workspace_path=str(repo),
    )
    claimed = kb.claim_task(conn, task_id, claimer="test-host:worker")
    assert claimed is not None and claimed.current_run_id is not None
    manifest = kb.prepare_run_continuation(
        conn,
        task_id,
        claimed.current_run_id,
        config=_config(),
    )
    return task_id, claimed, manifest


def _receipt(pid: int) -> kb.SpawnReceipt:
    return kb.SpawnReceipt(
        pid=pid,
        release=lambda: None,
        abort=lambda: None,
        process_started_at=1234.5,
        process_group_id=pid,
        session_id=pid,
    )


def test_manifest_is_immutable_bounded_and_keeps_authority_refs(
    kanban_home, tmp_path,
):
    repo = _init_repo(tmp_path / "repo")
    body = (
        "Jira BUILD-487\n- [ ] Keep orchestrator authority\n"
        + ("large working evidence\n" * 5000)
    )
    with kb.connect() as conn:
        task_id, claimed, first = _claim_with_manifest(
            conn, repo=repo, body=body,
        )
        second = kb.prepare_run_continuation(
            conn,
            task_id,
            claimed.current_run_id,
            config=_config(),
        )

        assert second.manifest_digest == first.manifest_digest
        assert second.context_digest == first.context_digest
        assert second.compiled_context["bytes"]["total"] <= 48 * 1024
        assert second.compiled_context["working_set_source_digest"]
        assert "truncated" in second.compiled_context["rendered"]
        assert kb.build_worker_context(conn, task_id) == second.compiled_context["rendered"]
        assert "Keep orchestrator authority" in second.manifest["acceptance_criteria"]
        assert any(
            ref["uri"] == "jira://ahlnos/BUILD-487"
            for ref in second.manifest["references"]
        )
        assert any(
            ref["uri"].endswith(f"/{task_id}/body") and ref["digest"]
            for ref in second.manifest["references"]
        )
        assert second.manifest["repository"]["path"] == str(repo.resolve())


def test_dispatch_opt_in_preserves_orchestrator_graph_and_profiles(
    kanban_home,
    all_assignees_spawnable,
    monkeypatch,
):
    monkeypatch.setattr(kb, "_continuation_config", _config)
    with kb.connect() as conn:
        graph = kb.compile_workflow_graph(
            conn,
            workflow_key="orchestrator-existing-flow",
            idempotency_key="request-487",
            created_by="orchestrator",
            steps=[
                {
                    "key": "implement",
                    "title": "Implement through existing queue",
                    "assignee": "coder",
                },
                {
                    "key": "review",
                    "title": "Review independently",
                    "assignee": "reviewer",
                },
                {
                    "key": "finish",
                    "title": "Finalize existing workflow",
                    "assignee": "orchestrator",
                    "parents": ["implement", "review"],
                    "role": "finalizer",
                    "terminal": True,
                },
            ],
        )
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, _workspace: _receipt(
                40_000 + len(kb.list_runs(conn, task.id))
            ),
            max_spawn=2,
        )

        assert {item[1] for item in result.spawned} == {"coder", "reviewer"}
        assert kb.get_task(conn, graph.terminal_task_id).status == "todo"
        for key in ("implement", "review"):
            task = kb.get_task(conn, graph.task_ids[key])
            assert task.current_run_id is not None
            manifest = kb.get_continuation_manifest(
                conn, task.current_run_id, task_id=task.id,
            )
            assert manifest is not None
            assert manifest.task_id == task.id
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 3


def test_dispatch_feature_disabled_keeps_legacy_run(
    kanban_home,
    all_assignees_spawnable,
):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="legacy", assignee="coder")
        result = kb.dispatch_once(conn, spawn_fn=lambda *_args: _receipt(41_000))
        task = kb.get_task(conn, task_id)

        assert result.spawned[0][0] == task_id
        assert kb.get_continuation_manifest(conn, task.current_run_id) is None


def test_late_critical_findings_keep_completion_closed_until_all_resolved(
    kanban_home, tmp_path,
):
    repo = _init_repo(tmp_path / "repo")
    with kb.connect() as conn:
        task_id, claimed, _manifest = _claim_with_manifest(conn, repo=repo)
        assert kb.record_runtime_observation(
            conn,
            task_id,
            claimed.current_run_id,
            {
                "version": 1,
                "phase": "initial",
                "provider": "openai-codex",
                "model": "gpt-5.6-luna",
                "reasoning_effort": "xhigh",
                "runtime": "hermes",
                "api_mode": "responses",
            },
        )
        first = kb.record_continuation_blocker(
            conn,
            task_id,
            severity="P0",
            title="data loss",
            details="completion would drop state",
            evidence_ref="review:first",
            discovered_by="reviewer",
            discovered_run_id=claimed.current_run_id,
        )
        second = kb.record_continuation_blocker(
            conn,
            task_id,
            severity="P1",
            title="claim race",
            details="two workers can own the run",
            evidence_ref="review:second",
            discovered_by="reviewer",
            discovered_run_id=claimed.current_run_id,
        )
        kb.resolve_continuation_blocker(
            conn,
            task_id,
            first.id,
            resolved_by="coder",
            resolution_evidence_ref="commit:abc+test:p0",
            resolved_run_id=claimed.current_run_id,
        )

        assert kb.complete_task(
            conn,
            task_id,
            summary="first repair",
            expected_run_id=claimed.current_run_id,
        ) is False

        late = kb.record_continuation_blocker(
            conn,
            task_id,
            severity="P1",
            title="late mixed-domain finding",
            details="found after implementation reopened",
            evidence_ref="review:late",
            discovered_by="independent-reviewer",
            discovered_run_id=claimed.current_run_id,
        )
        for blocker in (second, late):
            kb.resolve_continuation_blocker(
                conn,
                task_id,
                blocker.id,
                resolved_by="coder",
                resolution_evidence_ref=f"commit:def+test:B{blocker.id}",
                resolved_run_id=claimed.current_run_id,
            )

        assert kb.complete_task(
            conn,
            task_id,
            summary="all critical findings resolved",
            expected_run_id=claimed.current_run_id,
        ) is True


def test_kanban_tools_persist_redacted_typed_finding_and_explain_gate(
    kanban_home, tmp_path, monkeypatch,
):
    repo = _init_repo(tmp_path / "repo")
    with kb.connect() as conn:
        task_id, claimed, _manifest = _claim_with_manifest(conn, repo=repo)
        assert kb.record_runtime_observation(
            conn,
            task_id,
            claimed.current_run_id,
            {
                "version": 1,
                "phase": "initial",
                "provider": "openai-codex",
                "model": "gpt-5.6-luna",
                "reasoning_effort": "xhigh",
                "runtime": "hermes",
                "api_mode": "responses",
            },
        )
    monkeypatch.setenv("HERMES_PROFILE", "reviewer")
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))

    from tools import kanban_tools as kt

    opened = json.loads(
        kt._handle_comment(
            {
                "task_id": task_id,
                "body": "P1 finding",
                "blocker": {
                    "severity": "P1",
                    "title": "leaks api_key=sk-secret-that-must-not-persist",
                    "details": "secret is api_key=sk-secret-that-must-not-persist",
                    "evidence_ref": "log:api_key=sk-secret-that-must-not-persist",
                },
            }
        )
    )
    assert opened["blocker"]["status"] == "open"

    rejected = json.loads(
        kt._handle_complete(
            {"task_id": task_id, "summary": "attempted completion"}
        )
    )
    assert "unresolved critical findings" in rejected["error"]

    with kb.connect() as conn:
        blocker = kb.list_continuation_blockers(conn, task_id)[0]
        persisted = json.dumps(
            {
                "title": blocker.title,
                "details": blocker.details,
                "evidence": blocker.evidence_ref,
            }
        )
    assert "sk-secret-that-must-not-persist" not in persisted
    assert "sk-sec...sist" in persisted


def test_provider_deny_and_repo_drift_fail_before_runtime_and_persist(
    kanban_home, tmp_path, monkeypatch,
):
    repo = _init_repo(tmp_path / "repo")
    with kb.connect() as conn:
        task_id, claimed, manifest = _claim_with_manifest(conn, repo=repo)
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))
    monkeypatch.setenv("HERMES_KANBAN_CONTINUATION_DIGEST", manifest.manifest_digest)

    from hermes_cli.kanban_runtime_contract import (
        RunRouteMismatch,
        preflight_kanban_cli_route,
    )

    with pytest.raises(RunRouteMismatch, match="provider_policy_denied"):
        preflight_kanban_cli_route(
            model="anything",
            provider="openrouter",
            reasoning_config=None,
            toolsets=manifest.manifest.get("toolsets") or ["terminal", "file"],
        )

    (repo / "README.md").write_text("drifted\n", encoding="utf-8")
    with pytest.raises(RunRouteMismatch, match="repository"):
        preflight_kanban_cli_route(
            model="anything",
            provider="openai-codex",
            reasoning_config=None,
            toolsets=["terminal", "file"],
        )

    with kb.connect() as conn:
        status = kb.continuation_status(conn, task_id)
        failures = [
            event for event in kb.list_events(conn, task_id)
            if event.kind == "continuation_bootstrap_failed"
        ]
    assert status["last_bootstrap_failure"] is not None
    assert len(failures) == 2


def test_fallback_chain_cannot_smuggle_denied_provider(
    kanban_home, tmp_path, monkeypatch,
):
    repo = _init_repo(tmp_path / "repo")
    with kb.connect() as conn:
        task_id, claimed, manifest = _claim_with_manifest(conn, repo=repo)
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))
    monkeypatch.setenv("HERMES_KANBAN_CONTINUATION_DIGEST", manifest.manifest_digest)

    from hermes_cli.kanban_runtime_contract import (
        RuntimeObservationError,
        attach_kanban_runtime_observer,
    )

    agent = SimpleNamespace(
        provider="openai-codex",
        model="gpt",
        reasoning_config=None,
        runtime="hermes",
        api_mode="responses",
        _fallback_chain=[{"provider": "openrouter", "model": "fallback"}],
    )
    with pytest.raises(RuntimeObservationError, match="provider policy"):
        attach_kanban_runtime_observer(agent)


def test_denied_explicit_provider_fails_before_worker_popen(
    kanban_home, tmp_path, monkeypatch,
):
    repo = _init_repo(tmp_path / "repo")
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="deny before spawn",
            assignee="coder",
            workspace_kind="dir",
            workspace_path=str(repo),
            model_provider_override="openrouter",
            model_override="forbidden-model",
        )
        claimed = kb.claim_task(conn, task_id, claimer="test-host:worker")
        assert claimed is not None
        kb.prepare_run_continuation(
            conn, task_id, claimed.current_run_id, config=_config(),
        )

    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "resolve_profile_env", lambda _name: str(kanban_home))
    popen_calls = []
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_args, **_kwargs: popen_calls.append(True),
    )
    with pytest.raises(ContinuationContractError, match="pre_spawn_primary"):
        kb._default_spawn(claimed, str(repo))
    assert popen_calls == []


def test_profile_fallback_policy_is_checked_pre_spawn(monkeypatch, tmp_path):
    from hermes_cli import config as config_module

    monkeypatch.setattr(
        config_module,
        "load_config",
        lambda: {
            "model": {"provider": "openai-codex"},
            "fallback_providers": [
                {"provider": "openrouter", "model": "forbidden-fallback"},
            ],
        },
    )
    with pytest.raises(ContinuationContractError, match=r"pre_spawn_fallback\[0\]"):
        kb._assert_worker_continuation_provider_policy(
            str(tmp_path),
            {"provider": None, "model": None, "reasoning_effort": None},
            {"version": 1, "allow": [], "deny": ["openrouter"]},
        )


def test_productive_epochs_are_unlimited_but_three_identical_states_block(
    kanban_home, tmp_path,
):
    repo = _init_repo(tmp_path / "repo")
    with kb.connect() as conn:
        task_id, task, _manifest = _claim_with_manifest(conn, repo=repo)

        for index in range(4):
            assert kb.checkpoint_execution_epoch(
                conn,
                task_id,
                reason="bounded iteration budget",
                summary=f"productive checkpoint {index}",
                expected_run_id=task.current_run_id,
            ) == "ready"
            task = kb.claim_task(conn, task_id, claimer=f"test-host:epoch-{index}")
            assert task is not None
            kb.prepare_run_continuation(
                conn, task_id, task.current_run_id, config=_config(),
            )

        for repeat in range(3):
            outcome = kb.checkpoint_execution_epoch(
                conn,
                task_id,
                reason=f"volatile runtime reason {repeat}",
                summary="no semantic progress",
                metadata={"pid": 50_000 + repeat, "elapsed_seconds": 100 + repeat},
                expected_run_id=task.current_run_id,
            )
            if repeat < 2:
                assert outcome == "ready"
                task = kb.claim_task(
                    conn, task_id, claimer=f"test-host:repeat-{repeat}",
                )
                assert task is not None
                kb.prepare_run_continuation(
                    conn, task_id, task.current_run_id, config=_config(),
                )
            else:
                assert outcome == "blocked_nonconvergent"

        final = kb.get_task(conn, task_id)
        assert final.status == "blocked"
        assert final.consecutive_failures == 0
        assert len(kb.list_runs(conn, task_id)) == 7


def test_exact_owned_tmux_cleanup_refuses_identity_mismatch(
    kanban_home, tmp_path, monkeypatch,
):
    repo = _init_repo(tmp_path / "repo")
    with kb.connect() as conn:
        task_id, claimed, _manifest = _claim_with_manifest(conn, repo=repo)
        resource = kb.register_owned_run_resource(
            conn,
            task_id,
            claimed.current_run_id,
            claimed.claim_lock,
            kind="tmux_session",
            identity={
                "session_name": "worker-exact",
                "session_id": "$9",
                "session_created": "111",
            },
        )
        calls = []

        def fake_run(argv, **_kwargs):
            calls.append(argv)
            return SimpleNamespace(returncode=0, stdout="$10\t222\n", stderr="")

        monkeypatch.setattr(kb.subprocess, "run", fake_run)
        results = kb.cleanup_owned_run_resources(
            conn, task_id, claimed.current_run_id,
        )
        stored = kb.list_owned_run_resources(conn, claimed.current_run_id)[0]

    assert resource.kind == "tmux_session"
    assert results[0]["status"] == "identity_mismatch"
    assert stored.state == "identity_mismatch"
    assert len(calls) == 1
    assert calls[0][:4] == ["tmux", "display-message", "-p", "-t"]


def test_epoch_checkpoint_cleans_only_exact_matching_owned_tmux(
    kanban_home, tmp_path, monkeypatch,
):
    repo = _init_repo(tmp_path / "repo")
    with kb.connect() as conn:
        task_id, claimed, _manifest = _claim_with_manifest(conn, repo=repo)
        kb.register_owned_run_resource(
            conn,
            task_id,
            claimed.current_run_id,
            claimed.claim_lock,
            kind="tmux_session",
            identity={
                "session_name": "worker-exact",
                "session_id": "$9",
                "session_created": "111",
            },
        )
        calls = []

        def fake_run(argv, **_kwargs):
            calls.append(argv)
            if argv[1] == "display-message":
                return SimpleNamespace(
                    returncode=0, stdout="$9\t111\n", stderr="",
                )
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(kb.subprocess, "run", fake_run)
        assert kb.checkpoint_execution_epoch(
            conn,
            task_id,
            reason="bounded epoch",
            summary="safe handoff",
            expected_run_id=claimed.current_run_id,
        ) == "ready"
        resource = kb.list_owned_run_resources(conn, claimed.current_run_id)[0]

    assert resource.state == "cleaned"
    tmux_calls = [call for call in calls if call[0] == "tmux"]
    assert [call[1] for call in tmux_calls] == [
        "display-message",
        "kill-session",
    ]


def test_required_context_core_fails_loudly_instead_of_truncating():
    manifest = {
        "version": 1,
        "task_id": "t_core",
        "run_id": 1,
        "objective": "bounded core",
        "acceptance_criteria": ["criterion " + ("x" * 300)],
        "decisions": [],
        "references": [],
        "provider_policy": {"allow": [], "deny": ["openrouter"]},
        "repository": None,
        "created_at": 1,
    }
    with pytest.raises(ContinuationContractError, match="required context"):
        compile_context(
            manifest,
            "working set",
            max_core_bytes=128,
            max_total_bytes=512,
        )


def test_goal_budget_uses_epoch_checkpoint_when_available(monkeypatch):
    from hermes_cli import goals

    monkeypatch.setattr(
        goals,
        "judge_goal",
        lambda *_args, **_kwargs: ("continue", "more", False, None),
    )
    checkpoints = []
    result = goals.run_kanban_goal_loop(
        task_id="t_checkpoint",
        goal_text="continue safely",
        run_turn=lambda _prompt: "still working",
        task_status_fn=lambda: "running",
        block_fn=lambda _reason: pytest.fail("must not block the task"),
        checkpoint_fn=lambda reason, handoff: checkpoints.append((reason, handoff)),
        max_turns=2,
        first_response="started",
    )

    assert result["outcome"] == "checkpointed_budget"
    assert len(checkpoints) == 1


def test_goal_wrapper_does_not_continue_after_first_turn_checkpoint(
    kanban_home, tmp_path, monkeypatch,
):
    repo = _init_repo(tmp_path / "repo")
    with kb.connect() as conn:
        task_id, claimed, _manifest = _claim_with_manifest(conn, repo=repo)
        assert kb.checkpoint_execution_epoch(
            conn,
            task_id,
            reason="first turn used its bounded epoch",
            summary="resume in a new run",
            expected_run_id=claimed.current_run_id,
        ) == "ready"
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))

    from hermes_cli import goals
    from cli import _run_kanban_goal_loop_q

    loop = pytest.fail
    monkeypatch.setattr(goals, "run_kanban_goal_loop", loop)
    _run_kanban_goal_loop_q(SimpleNamespace(), "checkpoint response")
