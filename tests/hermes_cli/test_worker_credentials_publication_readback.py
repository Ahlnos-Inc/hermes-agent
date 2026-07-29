from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import worker_credentials as wc


EXPECTED_SHA = "a" * 40
SENTINEL = "sentinel-publication-token-do-not-log"


@pytest.fixture(autouse=True)
def _reset_worker_context():
    wc.reset_worker_credential_context_for_tests()
    yield
    wc.reset_worker_credential_context_for_tests()


def _write_manifest(root: Path, *, with_token: bool) -> None:
    actions = "[github_write]" if with_token else "[]"
    (root / wc.MANIFEST_FILENAME).write_text(
        f"version: 1\nprofiles:\n  releaser:\n    actions: {actions}\n",
        encoding="utf-8",
    )


def _write_policy(root: Path, denied: dict[str, str] | None = None) -> None:
    rules = root / "profiles" / "releaser" / "rules"
    rules.mkdir(parents=True, exist_ok=True)
    (rules / "pr-target-repo-allowlist.json").write_text(
        json.dumps(
            {
                "version": 1,
                "allowed_owners": ["Ahlnos-Inc", "nlachica"],
                "denied_repos": denied or {},
            }
        ),
        encoding="utf-8",
    )


def _prepare_trusted_readback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    token: str | None = SENTINEL,
    publication_repo: str | None = "Ahlnos-Inc/hermes-agent",
    write_policy: bool = True,
) -> tuple[SimpleNamespace, str, int, Path]:
    """Create a real current task/run and bootstrap its sealed worker context."""
    root = tmp_path / "controller-home"
    root.mkdir()
    _write_manifest(root, with_token=token is not None)
    if write_policy:
        _write_policy(root)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = tmp_path / "kanban.db"
    kb._INITIALIZED_PATHS.clear()
    kb.init_db(db)
    with kb.connect_closing(db) as conn:
        task_id = kb.create_task(
            conn,
            title="publication",
            assignee="releaser",
            workspace_kind="dir",
            workspace_path=str(workspace),
            publication_expected_sha=EXPECTED_SHA,
            publication_remote="origin",
            publication_ref="refs/heads/main",
            publication_repo=publication_repo,
        )
        claimed = kb.claim_task(conn, task_id, claimer="test-releaser")
        assert claimed is not None and claimed.current_run_id is not None
        run_id = int(claimed.current_run_id)
        row = kb._read_publication_contract(conn, task_id)
        assert row is not None

    monkeypatch.setattr(wc, "get_default_hermes_root", lambda: root)
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_PROFILE", "releaser")
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    manifest = wc.load_manifest(root)
    monkeypatch.setenv(wc.MANIFEST_DIGEST_ENV, manifest.digest)
    if token is not None:
        monkeypatch.setenv(wc.GITHUB_WRITE_HANDOFF_ENV, token)
    runtime = wc.bootstrap_worker_credential_context()
    assert runtime is not None and runtime.manifest_verified

    return SimpleNamespace(**kb._publication_contract_payload(row)), task_id, run_id, db


def _git_result(
    returncode: int,
    *,
    stdout: str = "",
    stderr: str = "",
):
    return wc._GitProcessResult(
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        failure=None,
    )


def test_trusted_readback_is_public_first_and_credential_process_is_canonical(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract, task_id, run_id, _db = _prepare_trusted_readback(tmp_path, monkeypatch)
    for name in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_COUNT",
        "GIT_SSH_COMMAND",
        "GIT_ASKPASS",
        "SSH_AUTH_SOCK",
        "HTTPS_PROXY",
        "CURL_CA_BUNDLE",
        "LD_PRELOAD",
        "DYLD_INSERT_LIBRARIES",
        "PYTHONPATH",
    ):
        monkeypatch.setenv(name, f"adversarial-{name}")
    calls: list[tuple[list[str], dict[str, str], str]] = []
    outcomes = iter(
        [
            _git_result(0, stdout="https://github.com/Ahlnos-Inc/hermes-agent.git\n"),
            _git_result(128, stderr="remote: Repository not found.\n"),
            _git_result(0, stdout=f"{EXPECTED_SHA}\trefs/heads/main\n"),
        ]
    )

    def run(command, *, env, cwd, timeout):
        calls.append((list(command), dict(env), str(cwd)))
        return next(outcomes)

    monkeypatch.setattr(wc, "_run_git_process", run)

    result = wc.trusted_publication_readback(
        contract,
        task_id=task_id,
        run_id=run_id,
    )

    assert result == {"verified": True, "observed_sha": EXPECTED_SHA, "reason": None}
    assert len(calls) == 3
    assert "GH_TOKEN" not in calls[0][1]
    assert "GH_TOKEN" not in calls[1][1]
    assert calls[2][1]["GH_TOKEN"] == SENTINEL
    assert SENTINEL not in repr(calls[2][0])
    assert "-C" not in calls[1][0]
    assert "-C" not in calls[2][0]
    assert "https://github.com/Ahlnos-Inc/hermes-agent.git" in calls[2][0]
    assert set(calls[0][1]) == set(wc._HERMETIC_GIT_ENV_KEYS)
    assert set(calls[1][1]) == set(wc._HERMETIC_GIT_ENV_KEYS)
    assert set(calls[2][1]) == set(wc._HERMETIC_GIT_ENV_KEYS) | {"GH_TOKEN"}
    assert all(
        not any(value.startswith("adversarial-") for value in env.values())
        for _command, env, _cwd in calls
    )
    assert calls[1][2] != str(tmp_path / "workspace")
    assert calls[2][2] != calls[1][2]


def test_trusted_readback_definitive_ref_absence_never_reads_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract, task_id, run_id, _db = _prepare_trusted_readback(tmp_path, monkeypatch)
    outcomes = iter(
        [
            _git_result(0, stdout="https://github.com/Ahlnos-Inc/hermes-agent.git\n"),
            _git_result(2, stderr="fatal: no matching refs\n"),
        ]
    )
    monkeypatch.setattr(
        wc,
        "_run_git_process",
        lambda command, **kwargs: next(outcomes),
    )
    monkeypatch.setattr(
        wc,
        "get_trusted_worker_credential",
        lambda *_args, **_kwargs: pytest.fail("definitive absence read the token"),
    )

    assert wc.trusted_publication_readback(
        contract, task_id=task_id, run_id=run_id
    ) == {"verified": False, "observed_sha": None, "reason": "ref_absent"}


def test_trusted_readback_strict_policy_unavailable_fails_before_network_or_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract, task_id, run_id, _db = _prepare_trusted_readback(
        tmp_path,
        monkeypatch,
        write_policy=False,
    )
    calls: list[list[str]] = []

    def run(command, **kwargs):
        calls.append(list(command))
        return _git_result(
            0,
            stdout="https://github.com/Ahlnos-Inc/hermes-agent.git\n",
        )

    monkeypatch.setattr(wc, "_run_git_process", run)
    monkeypatch.setattr(
        wc,
        "get_trusted_worker_credential",
        lambda *_args, **_kwargs: pytest.fail("unavailable policy read the token"),
    )

    result = wc.trusted_publication_readback(
        contract, task_id=task_id, run_id=run_id
    )
    assert result == {
        "verified": False,
        "observed_sha": None,
        "reason": "policy_unavailable",
    }
    assert len(calls) == 1, "only the token-free local target probe may run"


def test_trusted_readback_bound_local_target_is_rejected_without_network_or_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract, task_id, run_id, _db = _prepare_trusted_readback(tmp_path, monkeypatch)
    local_remote = tmp_path / "remote.git"
    calls: list[list[str]] = []

    def run(command, **kwargs):
        calls.append(list(command))
        return _git_result(0, stdout=f"{local_remote}\n")

    monkeypatch.setattr(wc, "_run_git_process", run)
    monkeypatch.setattr(
        wc,
        "get_trusted_worker_credential",
        lambda *_args, **_kwargs: pytest.fail("bound local target read the token"),
    )

    result = wc.trusted_publication_readback(
        contract, task_id=task_id, run_id=run_id
    )
    assert result["reason"] == "target_mismatch"
    assert len(calls) == 1


def test_trusted_readback_reclaimed_run_fails_before_any_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract, task_id, run_id, db = _prepare_trusted_readback(tmp_path, monkeypatch)
    with kb.connect_closing(db) as conn, kb.write_txn(conn):
        conn.execute("UPDATE tasks SET current_run_id = NULL WHERE id = ?", (task_id,))
    monkeypatch.setattr(
        wc,
        "_run_git_process",
        lambda *_args, **_kwargs: pytest.fail("stale run launched a subprocess"),
    )

    result = wc.trusted_publication_readback(
        contract, task_id=task_id, run_id=run_id
    )
    assert result["reason"] == "identity_mismatch"


def test_trusted_readback_temp_creation_failure_never_mutates_shared_tmp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract, task_id, run_id, _db = _prepare_trusted_readback(tmp_path, monkeypatch)
    chmod_calls: list[tuple[object, int]] = []
    rmtree_calls: list[object] = []
    monkeypatch.setattr(
        wc.tempfile,
        "mkdtemp",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("full")),
    )
    monkeypatch.setattr(wc.os, "chmod", lambda path, mode: chmod_calls.append((path, mode)))
    monkeypatch.setattr(wc.shutil, "rmtree", lambda path: rmtree_calls.append(path))

    result = wc.trusted_publication_readback(
        contract, task_id=task_id, run_id=run_id
    )
    assert result["reason"] == "transport"
    assert chmod_calls == []
    assert rmtree_calls == []


def test_publication_readback_failure_codes_are_closed_and_complete() -> None:
    assert wc.PUBLICATION_READBACK_CODES == frozenset(
        {
            "contract_incomplete",
            "workspace_missing",
            "target_unbound",
            "target_mismatch",
            "target_denied",
            "policy_unavailable",
            "identity_mismatch",
            "identity_unavailable",
            "auth_missing",
            "remote_rejected",
            "transport",
            "timeout",
            "ref_absent",
            "malformed_response",
            "sha_mismatch",
            "git_unavailable",
        }
    )
