from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_start_gate import (
    KanbanStartGateError,
    enforce_kanban_start_gate,
)


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _set_gate_env(monkeypatch, *, path, token: str, task) -> None:
    monkeypatch.setenv("HERMES_KANBAN_START_GATE_PATH", str(path))
    monkeypatch.setenv("HERMES_KANBAN_START_GATE_TOKEN", token)
    monkeypatch.setenv("HERMES_KANBAN_START_GATE_TIMEOUT_SECONDS", "0.2")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(kb.kanban_db_path()))
    monkeypatch.setenv("HERMES_KANBAN_TASK", task.id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(task.current_run_id))
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", str(task.claim_lock))


def test_start_gate_attests_exact_attached_run(kanban_home, monkeypatch, tmp_path):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="gated", assignee="worker")
        task = kb.claim_task(conn, task_id)
        assert task is not None
        kb._set_worker_pid(
            conn,
            task.id,
            os.getpid(),
            run_id=task.current_run_id,
            claim_lock=task.claim_lock,
        )

    gate_path = tmp_path / "gate"
    gate_path.write_text("release-token", encoding="utf-8")
    _set_gate_env(monkeypatch, path=gate_path, token="release-token", task=task)

    enforce_kanban_start_gate()

    assert not gate_path.exists()
    assert "HERMES_KANBAN_START_GATE_TOKEN" not in os.environ


def test_start_gate_token_is_atomically_published(monkeypatch, tmp_path):
    gate_path = tmp_path / "gate"
    write_started = threading.Event()
    permit_write = threading.Event()
    real_write = os.write

    def delayed_write(fd, payload):
        write_started.set()
        assert permit_write.wait(timeout=2.0)
        return real_write(fd, payload)

    monkeypatch.setattr(kb.os, "write", delayed_write)
    publisher = threading.Thread(
        target=kb._publish_start_gate_token,
        args=(gate_path, "release-token"),
    )
    publisher.start()
    assert write_started.wait(timeout=2.0)

    # Publication is a commit point: observers see no final gate until the
    # complete token has been written and fsynced elsewhere.
    assert not gate_path.exists()
    permit_write.set()
    publisher.join(timeout=2.0)

    assert not publisher.is_alive()
    assert gate_path.read_text(encoding="utf-8") == "release-token"


def test_start_gate_publication_never_replaces_existing_peer(tmp_path):
    gate_path = tmp_path / "gate"
    gate_path.write_text("peer-token", encoding="utf-8")

    with pytest.raises(FileExistsError):
        kb._publish_start_gate_token(gate_path, "release-token")

    assert gate_path.read_text(encoding="utf-8") == "peer-token"


def test_start_gate_rejects_pid_mismatch(kanban_home, monkeypatch, tmp_path):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="wrong pid", assignee="worker")
        task = kb.claim_task(conn, task_id)
        assert task is not None
        kb._set_worker_pid(
            conn,
            task.id,
            os.getpid() + 10_000,
            run_id=task.current_run_id,
            claim_lock=task.claim_lock,
        )

    gate_path = tmp_path / "gate"
    gate_path.write_text("release-token", encoding="utf-8")
    _set_gate_env(monkeypatch, path=gate_path, token="release-token", task=task)

    with pytest.raises(KanbanStartGateError, match="ownership attestation mismatch"):
        enforce_kanban_start_gate()

    assert not gate_path.exists()


def test_start_gate_times_out_without_parent_release(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_START_GATE_PATH", str(tmp_path / "missing"))
    monkeypatch.setenv("HERMES_KANBAN_START_GATE_TOKEN", "never-written")
    monkeypatch.setenv("HERMES_KANBAN_START_GATE_TIMEOUT_SECONDS", "0.01")

    with pytest.raises(KanbanStartGateError, match="timed out"):
        enforce_kanban_start_gate()


def test_dispatch_releases_gate_only_after_pid_readback(
    kanban_home,
    all_assignees_spawnable,
):
    release_observed = []
    aborted = []

    def spawn(task, workspace):
        def release():
            with kb.connect() as observer:
                task_row = kb.get_task(observer, task.id)
                run_row = kb.get_run(observer, int(task.current_run_id))
            release_observed.append((
                task_row.worker_pid,
                run_row.worker_pid,
                task_row.claim_lock,
            ))

        return kb.SpawnReceipt(
            pid=31_001,
            release=release,
            abort=lambda: aborted.append(True),
            process_started_at=1234.5,
            process_group_id=31_001,
            session_id=31_001,
        )

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="attach", assignee="worker")
        result = kb.dispatch_once(conn, spawn_fn=spawn)

    assert [item[0] for item in result.spawned] == [task_id]
    assert release_observed[0][:2] == (31_001, 31_001)
    assert release_observed[0][2]
    assert aborted == []


def test_dispatch_persists_exact_worker_process_identity(
    kanban_home,
    all_assignees_spawnable,
):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="typed worker", assignee="worker")

    def spawn(_task, _workspace):
        return kb.SpawnReceipt(
            pid=42_424,
            process_started_at=1234.5,
            process_group_id=42_424,
            session_id=42_424,
            release=lambda: None,
            abort=lambda: None,
        )

    with kb.connect() as conn:
        kb.dispatch_once(conn, spawn_fn=spawn)

    with kb.connect() as conn:
        task_row = conn.execute(
            "SELECT worker_pid, worker_started_at, worker_pgid, worker_sid "
            "FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        run_row = conn.execute(
            "SELECT worker_pid, worker_started_at, worker_pgid, worker_sid "
            "FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()

    assert tuple(task_row) == (42_424, 1234.5, 42_424, 42_424)
    assert tuple(run_row) == (42_424, 1234.5, 42_424, 42_424)


def test_dispatch_aborts_and_releases_claim_when_gate_release_fails(
    kanban_home,
    all_assignees_spawnable,
):
    aborted = []

    def spawn(task, workspace):
        def release():
            raise OSError("gate write failed")

        return kb.SpawnReceipt(
            pid=31_002,
            release=release,
            abort=lambda: aborted.append(True),
            process_started_at=1234.5,
            process_group_id=31_002,
            session_id=31_002,
        )

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="gate failure", assignee="worker")
        result = kb.dispatch_once(conn, spawn_fn=spawn)
        task = kb.get_task(conn, task_id)

    assert result.spawned == []
    assert result.spawn_errors == [(task_id, "gate write failed")]
    assert aborted == [True]
    assert task.status == "ready"
    assert task.worker_pid is None
    assert task.claim_lock is None


def test_dispatch_rejects_missing_spawn_receipt(
    kanban_home,
    all_assignees_spawnable,
):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="missing receipt", assignee="worker")
        result = kb.dispatch_once(conn, spawn_fn=lambda task, workspace: None)
        task = kb.get_task(conn, task_id)

    assert result.spawned == []
    assert result.spawn_errors == [
        (task_id, "spawn function must return an owned SpawnReceipt")
    ]
    assert task.status == "ready"


def test_dispatch_rejects_legacy_integer_pid_without_signalling_it(
    kanban_home,
    all_assignees_spawnable,
    monkeypatch,
):
    signalled = []
    monkeypatch.setattr(kb.os, "kill", lambda pid, sig: signalled.append((pid, sig)))

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="unowned pid", assignee="worker")
        result = kb.dispatch_once(conn, spawn_fn=lambda task, workspace: 31_004)
        task = kb.get_task(conn, task_id)

    assert result.spawned == []
    assert result.spawn_errors == [
        (task_id, "spawn function must return an owned SpawnReceipt")
    ]
    assert task.status == "ready"
    assert signalled == []


def _write_worker_contract(home: Path, *, actions: str = "[github_write]") -> None:
    (home / "worker-credential-contract.yaml").write_text(
        "version: 1\nprofiles:\n  releaser:\n    actions: "
        f"{actions}\n",
        encoding="utf-8",
    )
    (home / "config.yaml").write_text(
        "secrets:\n  bitwarden:\n    enabled: true\n"
        "    project_id: kanban-spawn-test\n    auto_install: false\n",
        encoding="utf-8",
    )


def _spawn_test_task(*, assignee: str = "releaser") -> kb.Task:
    return kb.Task(
        id="t_credential_spawn",
        title="credential spawn",
        body=None,
        assignee=assignee,
        status="ready",
        priority=0,
        created_by="test",
        created_at=0,
        started_at=None,
        completed_at=None,
        workspace_kind="scratch",
        workspace_path=None,
        claim_lock=None,
        claim_expires=None,
        tenant=None,
    )


def test_default_spawn_preflights_and_handoffs_only_authorized_action(
    kanban_home, monkeypatch, caplog
):
    from agent.secret_sources.base import FetchResult
    from hermes_cli import worker_credentials as wc

    sentinel = "spawn-sentinel-token"
    _write_worker_contract(kanban_home)
    monkeypatch.setenv("BWS_ACCESS_TOKEN", "controller-bootstrap")
    monkeypatch.setenv("GH_TOKEN", "ambient-gh-token")
    monkeypatch.setenv("GITHUB_TOKEN", "ambient-github-token")
    monkeypatch.setenv("GH_TOKEN_SECRET_WRITE", "ambient-secret-token")
    monkeypatch.setattr(
        wc,
        "_fetch_bitwarden_result",
        lambda **_kwargs: FetchResult(secrets={wc.GITHUB_WRITE_SOURCE_KEY: sentinel}),
    )
    captured = {}

    class FakeProc:
        pid = 55_501

    def fake_popen(_cmd, *args, **kwargs):
        captured["env"] = kwargs["env"]
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    task = _spawn_test_task()
    with caplog.at_level("INFO", logger=wc._log.name):
        receipt = kb._default_spawn(task, str(kanban_home / "workspace"))

    env = captured["env"]
    assert receipt.pid == 55_501
    assert env[wc.GITHUB_WRITE_HANDOFF_ENV] == sentinel
    assert wc.BWS_BOOTSTRAP_ENV not in env
    assert "GH_TOKEN" not in env
    assert "GITHUB_TOKEN" not in env
    assert wc.GITHUB_WRITE_SOURCE_KEY not in env
    assert sentinel not in caplog.text
    assert sentinel not in repr(receipt)


def test_default_spawn_does_not_call_popen_when_credential_preflight_fails(
    kanban_home, monkeypatch
):
    _write_worker_contract(kanban_home)
    monkeypatch.delenv("BWS_ACCESS_TOKEN", raising=False)
    called = []

    def fake_popen(*_args, **_kwargs):
        called.append(True)
        raise AssertionError("Popen must not run after credential preflight failure")

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    with pytest.raises(RuntimeError, match="missing BWS bootstrap"):
        kb._default_spawn(_spawn_test_task(), str(kanban_home / "workspace"))

    assert called == []


def test_pid_attach_rejects_stale_run_without_partial_write(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="stale attach", assignee="worker")
        task = kb.claim_task(conn, task_id)
        assert task is not None
        with pytest.raises(RuntimeError, match="active run changed"):
            kb._set_worker_pid(
                conn,
                task.id,
                31_003,
                run_id=int(task.current_run_id) + 1,
                claim_lock=task.claim_lock,
            )
        task_after = kb.get_task(conn, task_id)
        run_after = kb.get_run(conn, int(task.current_run_id))
        spawned_events = [
            event for event in kb.list_events(conn, task_id) if event.kind == "spawned"
        ]

    assert task_after.worker_pid is None
    assert run_after.worker_pid is None
    assert spawned_events == []


def test_orphan_canary_scans_only_exact_run_identity(monkeypatch):
    class FakeProcess:
        def __init__(self, pid, environ=None, error=None):
            self.info = {"pid": pid}
            self._environ = environ or {}
            self._error = error

        def environ(self):
            if self._error:
                raise self._error
            return self._environ

    exact = {
        "HERMES_KANBAN_TASK": "t_exact",
        "HERMES_KANBAN_RUN_ID": "17",
        "HERMES_KANBAN_CLAIM_LOCK": "host:claim",
    }
    processes = [
        FakeProcess(303, exact),
        FakeProcess(101, {**exact, "HERMES_KANBAN_RUN_ID": "18"}),
        FakeProcess(202, exact),
        FakeProcess(404, error=RuntimeError("process vanished")),
    ]
    fake_psutil = types.SimpleNamespace(process_iter=lambda _attrs: iter(processes))
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

    assert kb._scan_exact_kanban_workers("t_exact", 17, "host:claim") == [202, 303]


def test_reclaim_never_signals_a_verified_pid_identity_mismatch(monkeypatch):
    host = kb._claimer_id().split(":", 1)[0]
    signalled = []
    monkeypatch.setattr(
        kb,
        "_attest_reclaim_process_identity",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(kb.os, "kill", lambda pid, sig: signalled.append((pid, sig)))

    result = kb._terminate_reclaimed_worker(
        42_001,
        f"{host}:claim",
        worker_started_at=1234.5,
        worker_pgid=42_001,
        worker_sid=42_001,
    )

    assert result["identity_mismatch"] is True
    assert result["terminated"] is True
    assert signalled == []


def test_reclaim_identity_attestation_binds_environment_and_birth_time(monkeypatch):
    class FakeProcess:
        def __init__(self, pid):
            self.pid = pid

        def create_time(self):
            return 1234.5

        def environ(self):
            return {"HERMES_KANBAN_CLAIM_LOCK": "host:exact-claim"}

        def is_running(self):
            return True

    fake_psutil = types.SimpleNamespace(
        Process=FakeProcess,
        NoSuchProcess=type("NoSuchProcess", (Exception,), {}),
        AccessDenied=type("AccessDenied", (Exception,), {}),
        ZombieProcess=type("ZombieProcess", (Exception,), {}),
    )
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

    identity = {
        "worker_started_at": 1234.5,
        "worker_pgid": 42_010,
        "worker_sid": 42_010,
    }
    monkeypatch.setattr(kb.os, "getpgid", lambda _pid: 42_010)
    monkeypatch.setattr(kb.os, "getsid", lambda _pid: 42_010)
    assert (
        kb._attest_reclaim_process_identity(
            42_010, "host:exact-claim", **identity
        )
        is True
    )
    assert (
        kb._attest_reclaim_process_identity(42_010, "host:other", **identity)
        is False
    )


def test_reclaim_identity_attestation_binds_exact_task_and_run(monkeypatch):
    class FakeProcess:
        def create_time(self):
            return 1234.5

        def environ(self):
            return {
                "HERMES_KANBAN_TASK": "task-exact",
                "HERMES_KANBAN_RUN_ID": "17",
                "HERMES_KANBAN_CLAIM_LOCK": "host:exact-claim",
            }

        def is_running(self):
            return True

    fake_psutil = types.SimpleNamespace(
        Process=lambda _pid: FakeProcess(),
        NoSuchProcess=type("NoSuchProcess", (Exception,), {}),
        AccessDenied=type("AccessDenied", (Exception,), {}),
        ZombieProcess=type("ZombieProcess", (Exception,), {}),
    )
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)
    monkeypatch.setattr(kb.os, "getpgid", lambda _pid: 42_010)
    monkeypatch.setattr(kb.os, "getsid", lambda _pid: 42_010)

    identity = {
        "worker_started_at": 1234.5,
        "worker_pgid": 42_010,
        "worker_sid": 42_010,
        "task_id": "task-exact",
        "run_id": 17,
    }
    assert (
        kb._attest_reclaim_process_identity(
            42_010, "host:exact-claim", **identity
        )
        is True
    )
    assert (
        kb._attest_reclaim_process_identity(
            42_010, "host:exact-claim", **{**identity, "run_id": 18}
        )
        is False
    )


def test_end_run_clears_task_and_run_worker_identity(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="clear identity", assignee="worker")
        task = kb.claim_task(conn, task_id)
        assert task is not None
        kb._set_worker_pid(
            conn,
            task_id,
            42_011,
            run_id=task.current_run_id,
            claim_lock=task.claim_lock,
            worker_started_at=1234.5,
            worker_pgid=42_011,
            worker_sid=42_011,
        )

        assert kb._end_run(conn, task_id, outcome="completed") == task.current_run_id
        task_after = kb.get_task(conn, task_id)
        run_after = kb.get_run(conn, int(task.current_run_id))

    assert task_after.current_run_id is None
    assert task_after.claim_lock is None
    assert task_after.claim_expires is None
    assert task_after.worker_pid is None
    assert task_after.worker_started_at is None
    assert task_after.worker_pgid is None
    assert task_after.worker_sid is None
    assert run_after.worker_pid is None
    assert run_after.worker_started_at is None
    assert run_after.worker_pgid is None
    assert run_after.worker_sid is None


def test_reclaim_holds_an_unverifiable_live_pid_without_signalling(monkeypatch):
    host = kb._claimer_id().split(":", 1)[0]
    signalled = []
    monkeypatch.setattr(
        kb, "_attest_reclaim_process_identity", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(kb.os, "kill", lambda pid, sig: signalled.append((pid, sig)))

    result = kb._terminate_reclaimed_worker(42_002, f"{host}:claim")

    assert result["identity_unverifiable"] is True
    assert kb._worker_survived_termination(result) is True
    assert signalled == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group contract")
def test_reclaim_terminates_attested_worker_process_group():
    import psutil

    host = kb._claimer_id().split(":", 1)[0]
    claim_lock = f"{host}:real-process-tree"
    env = dict(os.environ)
    env["HERMES_KANBAN_CLAIM_LOCK"] = claim_lock
    leader = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import subprocess,sys,time; "
                "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
                "print(child.pid, flush=True); time.sleep(60)"
            ),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    )
    assert leader.stdout is not None
    child_pid = int(leader.stdout.readline().strip())
    started_at = psutil.Process(leader.pid).create_time()
    pgid = os.getpgid(leader.pid)
    sid = os.getsid(leader.pid)
    try:
        result = kb._terminate_reclaimed_worker(
            leader.pid,
            claim_lock,
            worker_started_at=started_at,
            worker_pgid=pgid,
            worker_sid=sid,
        )
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and (
            kb._pid_alive(leader.pid) or kb._pid_alive(child_pid)
        ):
            time.sleep(0.05)

        assert result["identity_verified"] is True
        assert result["terminated"] is True
        assert not kb._pid_alive(leader.pid)
        assert not kb._pid_alive(child_pid)
    finally:
        try:
            os.killpg(pgid, signal.SIGKILL)  # windows-footgun: ok — POSIX-only test
        except (ProcessLookupError, PermissionError):
            pass
        leader.wait(timeout=3.0)


def test_manual_reclaim_keeps_claim_when_live_pid_identity_is_unverifiable(
    kanban_home,
    monkeypatch,
):
    host = kb._claimer_id().split(":", 1)[0]
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="hold unsafe reclaim", assignee="worker")
        task = kb.claim_task(conn, task_id, claimer=f"{host}:claim")
        assert task is not None
        kb._set_worker_pid(
            conn,
            task_id,
            42_003,
            run_id=task.current_run_id,
            claim_lock=task.claim_lock,
        )
        monkeypatch.setattr(
            kb,
            "_attest_reclaim_process_identity",
            lambda *_args, **_kwargs: None,
        )
        monkeypatch.setattr(kb, "_pid_alive", lambda _pid: True)

        assert kb.reclaim_task(conn, task_id, reason="operator request") is False
        current = kb.get_task(conn, task_id)
        events = kb.list_events(conn, task_id)

    assert current.status == "running"
    assert current.worker_pid == 42_003
    assert any(
        event.kind == "reclaim_deferred"
        and event.payload["reason"] == "manual_reclaim_identity_unverifiable"
        for event in events
    )


def test_manual_reclaim_observes_missing_pid_without_killing_scan_match(
    kanban_home,
    monkeypatch,
    caplog,
):
    host = kb._claimer_id().split(":", 1)[0]
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="orphan canary", assignee="worker")
        task = kb.claim_task(conn, task_id, claimer=f"{host}:claim")
        assert task is not None

        monkeypatch.setattr(
            kb,
            "_scan_exact_kanban_workers",
            lambda task_id, run_id, claim_lock: [42_424],
        )
        signalled = []
        with caplog.at_level("WARNING", logger=kb.__name__):
            assert kb.reclaim_task(
                conn,
                task_id,
                reason="canary test",
                signal_fn=lambda pid, sig: signalled.append((pid, sig)),
            )

    assert signalled == []
    assert "orphan_worker_canary action=observe_only" in caplog.text
    assert "worker_pids=[42424]" in caplog.text
    assert "host:claim" not in caplog.text
