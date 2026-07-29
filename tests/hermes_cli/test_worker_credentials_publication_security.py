from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import worker_credentials as wc
from tests.hermes_cli import test_worker_credentials_publication_readback as base


REMOTE_REF = "refs/heads/main"
_prepare_trusted_readback = getattr(base, "_prepare_trusted_readback")


def _process(
    returncode: int,
    *,
    stdout: str = "",
    stderr: str = "",
    failure: str | None = None,
) -> wc._GitProcessResult:
    return wc._GitProcessResult(returncode, stdout, stderr, failure)


@pytest.fixture(autouse=True)
def _reset_runtime() -> Iterator[None]:
    wc.reset_worker_credential_context_for_tests()
    yield
    wc.reset_worker_credential_context_for_tests()


def test_bootstrap_seals_system_git_runtime_and_action_never_rediscovers_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract, task_id, run_id, _db = _prepare_trusted_readback(tmp_path, monkeypatch)
    runtime = wc._TRUSTED_WORKER_RUNTIME
    assert runtime is not None and runtime.git_runtime is not None
    assert runtime.git_runtime.git.path.startswith("/")
    assert runtime.git_runtime.exec_path.path.startswith("/")
    assert runtime.git_runtime.shell.path.startswith("/")
    # PATH must be non-empty, include git's parent, and contain only
    # verified directory strings — not OS-specific resolved symlink paths.
    git_parent = str(Path(runtime.git_runtime.git.path).parent)
    path_parts = runtime.git_runtime.path.split(":")
    assert runtime.git_runtime.path != ""
    assert git_parent in path_parts, f"{git_parent!r} not in {path_parts!r}"
    assert oct(Path(runtime.git_runtime.temp_root.path).stat().st_mode & 0o777) == "0o700"

    monkeypatch.setattr(
        wc.shutil,
        "which",
        lambda *_args, **_kwargs: pytest.fail("action-time PATH discovery attempted"),
    )
    processes = iter(
        [
            _process(
                0, stdout="https://github.com/Ahlnos-Inc/hermes-agent.git\n"
            ),
            _process(
                0, stdout=f"{base.EXPECTED_SHA}\t{REMOTE_REF}\n"
            ),
        ]
    )
    monkeypatch.setattr(
        wc, "_run_git_process", lambda *_args, **_kwargs: next(processes)
    )
    result = wc.trusted_publication_readback(
        contract, task_id=task_id, run_id=run_id
    )
    assert result == {
        "verified": True,
        "observed_sha": base.EXPECTED_SHA,
        "reason": None,
    }


def test_git_runtime_is_revalidated_before_secret_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract, task_id, run_id, _db = _prepare_trusted_readback(tmp_path, monkeypatch)
    processes = iter(
        [
            _process(
                0, stdout="https://github.com/Ahlnos-Inc/hermes-agent.git\n"
            ),
            _process(128, stderr="remote: Repository not found.\n"),
        ]
    )
    monkeypatch.setattr(
        wc, "_run_git_process", lambda *_args, **_kwargs: next(processes)
    )
    monkeypatch.setattr(
        wc,
        "_git_runtime_is_current",
        lambda _runtime, *, rehash_git: not rehash_git,
    )
    monkeypatch.setattr(
        wc,
        "get_trusted_worker_credential",
        lambda *_args: pytest.fail("secret accessed after Git runtime changed"),
    )

    result = wc.trusted_publication_readback(
        contract, task_id=task_id, run_id=run_id
    )
    assert result["reason"] == "git_unavailable"


def test_symlinked_private_directory_is_never_chmodded_or_deleted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract, task_id, run_id, _db = _prepare_trusted_readback(tmp_path, monkeypatch)
    runtime = wc._TRUSTED_WORKER_RUNTIME
    assert runtime is not None and runtime.git_runtime is not None
    outside = tmp_path / "shared-outside"
    outside.mkdir()
    link = Path(runtime.git_runtime.temp_root.path) / "attempt-malicious"
    link.symlink_to(outside, target_is_directory=True)
    chmod_calls: list[object] = []
    rmtree_calls: list[object] = []
    monkeypatch.setattr(wc.tempfile, "mkdtemp", lambda **_kwargs: str(link))
    monkeypatch.setattr(
        wc.os,
        "chmod",
        lambda path, _mode: chmod_calls.append(path),
    )
    monkeypatch.setattr(wc.shutil, "rmtree", lambda path: rmtree_calls.append(path))

    result = wc.trusted_publication_readback(
        contract, task_id=task_id, run_id=run_id
    )
    assert result["reason"] == "transport"
    assert chmod_calls == []
    assert rmtree_calls == []
    assert outside.is_dir()


@pytest.mark.parametrize(
    ("policy_document", "expected_reason"),
    [
        (None, "policy_unavailable"),
        ("not-a-policy-object", "policy_unavailable"),
        (
            {
                "version": 2,
                "allowed_owners": ["Ahlnos-Inc"],
                "denied_repos": {},
            },
            "policy_unavailable",
        ),
        (
            {
                "version": 1,
                "allowed_owners": ["Other-Inc"],
                "denied_repos": {},
            },
            "target_denied",
        ),
        (
            {
                "version": 1,
                "allowed_owners": ["Ahlnos-Inc"],
                "denied_repos": {"Ahlnos-Inc/hermes-agent": "sensitive"},
            },
            "target_denied",
        ),
        (
            {
                "version": 1,
                "allowed_owners": ["Ahlnos-Inc"],
                "denied_repos": {},
                "unexpected": True,
            },
            "policy_unavailable",
        ),
    ],
)
def test_bound_target_policy_is_strict_and_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    policy_document: object | None,
    expected_reason: str,
) -> None:
    contract, task_id, run_id, _db = _prepare_trusted_readback(tmp_path, monkeypatch)
    policy_path = (
        tmp_path
        / "controller-home"
        / "profiles"
        / "releaser"
        / "rules"
        / "pr-target-repo-allowlist.json"
    )
    if policy_document is None:
        policy_path.unlink()
    else:
        policy_path.write_text(json.dumps(policy_document), encoding="utf-8")
    monkeypatch.setattr(
        wc,
        "_run_git_process",
        lambda *_args, **_kwargs: _process(
            0, stdout="https://github.com/Ahlnos-Inc/hermes-agent.git\n"
        ),
    )
    monkeypatch.setattr(
        wc,
        "get_trusted_worker_credential",
        lambda *_args: pytest.fail("policy failure unlocked a credential"),
    )

    result = wc.trusted_publication_readback(
        contract, task_id=task_id, run_id=run_id
    )
    assert result["reason"] == expected_reason


@pytest.mark.parametrize(
    "remote_url",
    [
        "https://user@github.com/Ahlnos-Inc/hermes-agent.git",
        "https://github.com:444/Ahlnos-Inc/hermes-agent.git",
        "ssh://git@github.com:443/Ahlnos-Inc/hermes-agent.git",
        "https://github.com/Ahlnos-Inc/hermes-agent.git?ref=main",
        "https://github.com/Ahlnos-Inc/%68ermes-agent.git",
        "file://attacker.example/private/repo.git",
        "git+ssh://github.com/Ahlnos-Inc/hermes-agent.git",
    ],
)
def test_ambiguous_or_noncanonical_bound_targets_fail_before_secret_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    remote_url: str,
) -> None:
    contract, task_id, run_id, _db = _prepare_trusted_readback(tmp_path, monkeypatch)
    monkeypatch.setattr(
        wc,
        "_run_git_process",
        lambda *_args, **_kwargs: _process(0, stdout=f"{remote_url}\n"),
    )
    monkeypatch.setattr(
        wc,
        "get_trusted_worker_credential",
        lambda *_args: pytest.fail("ambiguous target unlocked a credential"),
    )
    result = wc.trusted_publication_readback(
        contract, task_id=task_id, run_id=run_id
    )
    assert result["reason"] == "target_mismatch"


def test_non_auth_public_failure_does_not_unlock_private_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract, task_id, run_id, _db = _prepare_trusted_readback(tmp_path, monkeypatch)
    processes = iter(
        [
            _process(
                0, stdout="https://github.com/Ahlnos-Inc/hermes-agent.git\n"
            ),
            _process(
                128,
                stderr="fatal: unable to access remote: Could not resolve host\n",
            ),
        ]
    )
    monkeypatch.setattr(
        wc, "_run_git_process", lambda *_args, **_kwargs: next(processes)
    )
    monkeypatch.setattr(
        wc,
        "get_trusted_worker_credential",
        lambda *_args: pytest.fail("transport failure unlocked a credential"),
    )
    result = wc.trusted_publication_readback(
        contract, task_id=task_id, run_id=run_id
    )
    assert result["reason"] == "transport"


@pytest.mark.parametrize("invalid_field", ["expected_sha", "remote", "ref"])
def test_invalid_contract_fields_fail_before_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_field: str,
) -> None:
    contract, task_id, run_id, _db = _prepare_trusted_readback(tmp_path, monkeypatch)
    setattr(contract, invalid_field, "invalid value")
    monkeypatch.setattr(
        wc,
        "_run_git_process",
        lambda *_args, **_kwargs: pytest.fail("invalid contract launched subprocess"),
    )
    result = wc.trusted_publication_readback(
        contract, task_id=task_id, run_id=run_id
    )
    assert result["reason"] == "contract_incomplete"


def test_missing_workspace_fails_before_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract, task_id, run_id, _db = _prepare_trusted_readback(tmp_path, monkeypatch)
    Path(contract.workspace_path).rmdir()
    monkeypatch.setattr(
        wc,
        "_run_git_process",
        lambda *_args, **_kwargs: pytest.fail("missing workspace launched subprocess"),
    )
    result = wc.trusted_publication_readback(
        contract, task_id=task_id, run_id=run_id
    )
    assert result["reason"] == "workspace_missing"


def test_unknown_non_https_unbound_target_fails_without_public_network_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract, task_id, run_id, _db = _prepare_trusted_readback(
        tmp_path,
        monkeypatch,
        publication_repo=None,
    )
    calls = 0

    def fake_process(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return _process(0, stdout="ssh://git@example.com/private/repo.git\n")

    monkeypatch.setattr(wc, "_run_git_process", fake_process)
    result = wc.trusted_publication_readback(
        contract, task_id=task_id, run_id=run_id
    )
    assert result["reason"] == "target_unbound"
    assert calls == 1


def test_credentialed_remote_rejection_has_closed_failure_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract, task_id, run_id, _db = _prepare_trusted_readback(tmp_path, monkeypatch)
    processes = iter(
        [
            _process(
                0, stdout="https://github.com/Ahlnos-Inc/hermes-agent.git\n"
            ),
            _process(128, stderr="remote: Repository not found.\n"),
            _process(128, stderr="fatal: Authentication failed\n"),
        ]
    )
    monkeypatch.setattr(
        wc, "_run_git_process", lambda *_args, **_kwargs: next(processes)
    )
    result = wc.trusted_publication_readback(
        contract, task_id=task_id, run_id=run_id
    )
    assert result["reason"] == "remote_rejected"


def test_auth_missing_is_reported_only_after_exact_public_denial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract, task_id, run_id, _db = _prepare_trusted_readback(
        tmp_path,
        monkeypatch,
        token=None,
    )
    processes = iter(
        [
            _process(
                0, stdout="git@github.com:Ahlnos-Inc/hermes-agent.git\n"
            ),
            _process(128, stderr="remote: Repository not found.\n"),
        ]
    )
    monkeypatch.setattr(
        wc, "_run_git_process", lambda *_args, **_kwargs: next(processes)
    )
    result = wc.trusted_publication_readback(
        contract, task_id=task_id, run_id=run_id
    )
    assert result["reason"] == "auth_missing"


@pytest.mark.parametrize(
    ("completed", "expected_reason"),
    [
        (_process(2), "ref_absent"),
        (
            _process(0, stdout="not-a-sha\trefs/heads/main\n"),
            "malformed_response",
        ),
        (
            _process(
                0, stdout=f"{'b' * 40}\t{REMOTE_REF}\n"
            ),
            "sha_mismatch",
        ),
        (_process(-1, failure="timeout"), "timeout"),
        (_process(-1, failure="git_unavailable"), "git_unavailable"),
    ],
)
def test_public_attempt_failure_taxonomy_is_behavioral(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    completed: wc._GitProcessResult,
    expected_reason: str,
) -> None:
    contract, task_id, run_id, _db = _prepare_trusted_readback(
        tmp_path,
        monkeypatch,
        publication_repo=None,
    )
    processes = iter([_process(0, stdout="../remote.git\n"), completed])
    monkeypatch.setattr(
        wc, "_run_git_process", lambda *_args, **_kwargs: next(processes)
    )
    result = wc.trusted_publication_readback(
        contract, task_id=task_id, run_id=run_id
    )
    assert result["reason"] == expected_reason


def test_private_retry_and_total_action_budgets_are_controller_owned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract, task_id, run_id, _db = _prepare_trusted_readback(tmp_path, monkeypatch)
    calls: list[dict[str, str]] = []
    processes = iter(
        [
            _process(
                0, stdout="https://github.com/Ahlnos-Inc/hermes-agent.git\n"
            ),
            _process(128, stderr="remote: Repository not found.\n"),
            _process(
                0, stdout=f"{base.EXPECTED_SHA}\t{REMOTE_REF}\n"
            ),
            _process(
                0, stdout="https://github.com/Ahlnos-Inc/hermes-agent.git\n"
            ),
            _process(128, stderr="remote: Repository not found.\n"),
        ]
    )

    def fake_process(*_args, **kwargs):
        calls.append(kwargs["env"])
        return next(processes)

    monkeypatch.setattr(wc, "_run_git_process", fake_process)
    first = wc.trusted_publication_readback(contract, task_id=task_id, run_id=run_id)
    second = wc.trusted_publication_readback(contract, task_id=task_id, run_id=run_id)
    third = wc.trusted_publication_readback(contract, task_id=task_id, run_id=run_id)

    assert first["verified"] is True
    assert second["reason"] == "transport"
    assert third["reason"] == "transport"
    assert len(calls) == 5
    assert sum("GH_TOKEN" in env for env in calls) == 1


def test_identity_read_failure_and_ended_run_launch_no_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract, task_id, run_id, db = _prepare_trusted_readback(tmp_path, monkeypatch)
    runtime = wc._TRUSTED_WORKER_RUNTIME
    assert runtime is not None
    wc._TRUSTED_WORKER_RUNTIME = replace(
        runtime,
        kanban_db_path=str(tmp_path / "missing.db"),
    )
    monkeypatch.setattr(
        wc,
        "_run_git_process",
        lambda *_args, **_kwargs: pytest.fail(
            "invalid current-run identity launched subprocess"
        ),
    )
    unavailable = wc.trusted_publication_readback(
        contract, task_id=task_id, run_id=run_id
    )
    assert unavailable["reason"] == "identity_unavailable"

    wc._TRUSTED_WORKER_RUNTIME = runtime
    with kb.connect_closing(db) as conn, kb.write_txn(conn):
        conn.execute(
            "UPDATE task_runs SET profile = 'coder' WHERE id = ?",
            (run_id,),
        )
    wrong_profile = wc.trusted_publication_readback(
        contract, task_id=task_id, run_id=run_id
    )
    assert wrong_profile["reason"] == "identity_mismatch"

    with kb.connect_closing(db) as conn, kb.write_txn(conn):
        conn.execute(
            "UPDATE task_runs SET profile = 'releaser', status = 'completed', "
            "ended_at = 1 WHERE id = ?",
            (run_id,),
        )
    ended = wc.trusted_publication_readback(
        contract, task_id=task_id, run_id=run_id
    )
    assert ended["reason"] == "identity_mismatch"


def test_process_runner_caps_output_and_kills_timeout_process_group() -> None:
    runtime = wc._seal_git_runtime()
    assert runtime is not None
    wc._TRUSTED_WORKER_RUNTIME = wc.TrustedWorkerCredentialRuntime(
        profile="releaser",
        task_id="cleanup",
        run_id="1",
        manifest_digest="digest",
        manifest_verified=True,
        kanban_db_path="/missing",
        capabilities=(),
        git_runtime=runtime,
        _values=(),
    )
    started = time.monotonic()
    result = wc._run_git_process(
        [
            runtime.shell.path,
            "-c",
            "(sleep 30) & i=0; while [ $i -lt 20000 ]; "
            "do printf 1234567890; i=$((i+1)); done; wait",
        ],
        env={"PATH": runtime.path, "LANG": "C", "LC_ALL": "C"},
        cwd=runtime.temp_root.path,
        timeout=1,
    )
    elapsed = time.monotonic() - started
    assert result.failure == "timeout"
    assert len(result.stdout.encode("utf-8")) <= wc._MAX_GIT_OUTPUT_BYTES
    assert elapsed < 10


def test_git_process_cleanup_uses_taskkill_tree_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows timeout cleanup must kill descendants as well as the Git parent."""
    calls: list[object] = []
    process = SimpleNamespace(
        pid=12345,
        kill=lambda: calls.append("process.kill"),
    )

    def taskkill(command, **kwargs):
        calls.append((command, kwargs))

    monkeypatch.setattr(wc.subprocess, "run", taskkill)

    wc._terminate_git_process_tree(process, is_windows=True)

    assert calls == [
        (
            ["taskkill", "/F", "/T", "/PID", "12345"],
            {
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
                "stdin": subprocess.DEVNULL,
                "timeout": 5,
                "check": False,
            },
        )
    ]


def test_seal_git_runtime_returns_none_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows gate: _seal_git_runtime must return None immediately on win32.

    The bootstrap POSIX primitives (fchmod, /bin/sh, POSIX candidate paths)
    have no Windows equivalent.  Returning None early keeps the entire
    POSIX-only code path unreachable on Windows and ensures that the
    publication action fails closed (git_unavailable) without crashing.
    """
    monkeypatch.setattr(wc.sys, "platform", "win32")
    result = wc._seal_git_runtime()
    assert result is None, "_seal_git_runtime must return None on Windows"


def test_windows_bootstrap_propagates_none_git_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bootstrapping a releaser on Windows sets git_runtime=None (no crash).

    _seal_git_runtime returns None when sys.platform=="win32"; this
    propagates cleanly through bootstrap_worker_credential_context so
    trusted_publication_readback can inspect it and fail closed.
    """
    monkeypatch.setattr(wc.sys, "platform", "win32")
    # _prepare_trusted_readback bootstraps the releaser context.
    _prepare_trusted_readback(tmp_path, monkeypatch)
    runtime = wc._TRUSTED_WORKER_RUNTIME
    assert runtime is not None
    assert runtime.git_runtime is None, (
        "Windows bootstrap must propagate None git_runtime from _seal_git_runtime"
    )


def test_publication_readback_fails_closed_on_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On Windows, trusted_publication_readback returns git_unavailable (no crash).

    This covers the full action code path: Windows bootstrap yields
    git_runtime=None, which the readback guard converts to git_unavailable
    before launching any subprocess.  No POSIX-only call is reached.
    """
    monkeypatch.setattr(wc.sys, "platform", "win32")
    contract, task_id, run_id, _db = _prepare_trusted_readback(tmp_path, monkeypatch)
    runtime = wc._TRUSTED_WORKER_RUNTIME
    assert runtime is not None and runtime.git_runtime is None
    monkeypatch.setattr(
        wc,
        "_run_git_process",
        lambda *_args, **_kwargs: pytest.fail(
            "_run_git_process must not be called when git_runtime is None"
        ),
    )
    result = wc.trusted_publication_readback(
        contract, task_id=task_id, run_id=run_id
    )
    assert result["reason"] == "git_unavailable"
    assert result.get("verified") is not True




def test_kanban_adapter_delegates_to_canonical_action_and_sanitizes_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[tuple[str, int | None]] = []

    def fake_action(contract, *, task_id, run_id):
        called.append((task_id, run_id))
        return {
            "verified": False,
            "observed_sha": "secret-not-a-sha",
            "reason": "exception with secret",
            "stderr": "credential material",
        }

    monkeypatch.setattr(wc, "trusted_publication_readback", fake_action)
    result = kb._read_publication_remote_ref(
        object(),
        task_id="t_pub",
        run_id=7,
    )
    assert called == [("t_pub", 7)]
    assert result == {
        "verified": False,
        "observed_sha": None,
        "reason": "transport",
    }
