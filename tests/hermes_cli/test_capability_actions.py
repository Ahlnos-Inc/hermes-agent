from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import sys
import threading
import time

import pytest

from hermes_cli import capability_actions as actions
from hermes_cli import google_ads_action_helper as helper
from hermes_cli import kanban_db as kb
from hermes_cli import worker_credentials as wc


def _manifest_for_activation(activation):
    return wc._normalize_manifest(
        {
            "version": 2,
            "profiles": {
                "marketing-operator": {
                    "actions": {
                        actions.CAPABILITY_NAME: {
                            "activation_sha256": actions.activation_sha256(activation)
                        }
                    }
                }
            },
        },
        None,
    )


def _task_and_activation(conn, tmp_path: Path, *, attempts: int = 3, ttl: int = 300):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    fake_bws = tmp_path / "bin" / "bws"
    fake_bws.parent.mkdir(exist_ok=True)
    fake_bws.write_text("#!/bin/sh\nexit 1\n")
    fake_bws.chmod(0o700)
    task_id = kb.create_task(
        conn,
        title="read exact campaign status",
        body="Approved fixed read",
        assignee="marketing-operator",
        created_by="orchestrator",
        workspace_kind="dir",
        workspace_path=str(workspace),
    )
    task = kb.claim_task(conn, task_id)
    assert task is not None and task.current_run_id is not None
    principal = actions._task_principal(conn, task, "default")
    activation = {
        "schema_version": 1,
        "activation_id": "synthetic-test",
        "capability": actions.CAPABILITY_NAME,
        "profile": "marketing-operator",
        "live_activation_authorized": False,
        "synthetic_only": True,
        "task_principal": principal,
        "operation": "campaign_status_read_v1",
        "customer_id": "1234567890",
        "campaign_resource_name": "customers/1234567890/campaigns/77",
        "api_major": "v24",
        "backend": "synthetic",
        "source_project_id": "synthetic-project",
        "source_key_names": list(wc.CAPABILITIES[actions.CAPABILITY_NAME].source_keys),
        "response_schema_sha256": actions.response_schema_sha256(),
        "implementation_sha256": actions.implementation_sha256(),
        "runtime_sha256": actions.runtime_sha256(),
        "core_commit_sha": "a" * 40,
        "config_commit_sha": "b" * 40,
        "installed_runtime_sha": "a" * 40,
        "test_commands_sha256": "c" * 64,
        "test_results_sha256": "d" * 64,
        "leak_scan_sha256": "e" * 64,
        "helper_toolchain": actions.build_toolchain_manifest(fake_bws),
        "action_budget": {"successful_receipts": 1, "provider_attempts": attempts},
        "receipt_ttl_seconds": ttl,
        "google_account_role": "READ_ONLY",
        "approved_by": "test-controller",
        "approved_at": "2026-07-22T00:00:00Z",
        "approval_surface": "synthetic-test",
    }
    manifest = _manifest_for_activation(activation)
    return task, activation, manifest, workspace


def _success(request, _activation, _workspace):
    return {
        "version": 1,
        "ok": True,
        "receipt": {
            "campaign_resource_name": request["campaign_resource_name"],
            "campaign_id": "77",
            "name": "Search Brand",
            "status": "ENABLED",
            "serving_status": "SERVING",
            "provider_request_id": "req-1",
        },
        "bindings": {
            "activation_digest": request["activation_digest"],
            "contract_digest": request["contract_digest"],
            "task_principal_digest": request["task_principal_digest"],
            "response_schema_digest": request["response_schema_digest"],
        },
    }


def test_controller_action_persists_receipt_delivery_and_reuses_it(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    with contextlib.closing(kb.connect(db_path)) as conn:
        task, activation, manifest, workspace = _task_and_activation(conn, tmp_path)
        calls = []

        def runner(request, active, cwd):
            calls.append((request, active, cwd))
            return _success(request, active, cwd)

        first = actions.prepare_controller_action(
            conn,
            task,
            board_identity="default",
            workspace=str(workspace),
            manifest=manifest,
            activation=activation,
            helper_runner=runner,
            synthetic=True,
            now=1000,
        )
        assert first is not None and first.reused is False
        assert first.receipt["status"] == "ENABLED"
        assert len(calls) == 1
        assert conn.execute("SELECT COUNT(*) AS n FROM capability_action_uses").fetchone()["n"] == 1
        assert conn.execute("SELECT COUNT(*) AS n FROM capability_action_receipts").fetchone()["n"] == 1
        assert conn.execute("SELECT COUNT(*) AS n FROM capability_receipt_deliveries").fetchone()["n"] == 1
        use = conn.execute("SELECT * FROM capability_action_uses").fetchone()
        receipt_row = conn.execute("SELECT * FROM capability_action_receipts").fetchone()
        assert use["implementation_digest"] == activation["implementation_sha256"]
        assert use["runtime_digest"] == activation["runtime_sha256"]
        assert receipt_row["implementation_digest"] == activation["implementation_sha256"]
        assert receipt_row["runtime_digest"] == activation["runtime_sha256"]
        context = kb.build_worker_context(conn, task.id)
        assert "Controller action receipts" in context
        assert first.receipt_digest in context

        from tools import google_ads_receipt_tool as receipt_tool

        delivered_run_id = int(task.current_run_id or 0)
        monkeypatch.setattr(
            receipt_tool,
            "_delivery_context",
            lambda: (task.id, delivered_run_id, str(db_path)),
        )
        assert receipt_tool._check_receipt_available() is True
        tool_result = json.loads(receipt_tool._handle_receipt({}))
        assert tool_result["ok"] is True
        assert tool_result["receipt_digest"] == first.receipt_digest
        assert tool_result["receipt"]["status"] == "ENABLED"

        same_run = actions.prepare_controller_action(
            conn,
            task,
            board_identity="default",
            workspace=str(workspace),
            manifest=manifest,
            activation=activation,
            helper_runner=runner,
            synthetic=True,
            now=1001,
        )
        assert same_run.delivery_id == first.delivery_id
        assert len(calls) == 1

        assert kb.block_task(conn, task.id, reason="retry")
        assert kb.unblock_task(conn, task.id)
        next_task = kb.claim_task(conn, task.id)
        assert next_task is not None
        reused = actions.prepare_controller_action(
            conn,
            next_task,
            board_identity="default",
            workspace=str(workspace),
            manifest=manifest,
            activation=activation,
            helper_runner=runner,
            synthetic=True,
            now=1002,
        )
        assert reused is not None and reused.reused is True
        assert reused.receipt_id == first.receipt_id
        assert len(calls) == 1
        assert conn.execute("SELECT COUNT(*) AS n FROM capability_action_uses").fetchone()["n"] == 1


def test_transient_attempts_are_bounded_then_succeed(tmp_path):
    with contextlib.closing(kb.connect(tmp_path / "kanban.db")) as conn:
        task, activation, manifest, workspace = _task_and_activation(conn, tmp_path)
        count = 0

        def runner(request, active, cwd):
            nonlocal count
            count += 1
            if count < 3:
                return {"version": 1, "ok": False, "category": "google_ads_transient"}
            return _success(request, active, cwd)

        delivery = actions.prepare_controller_action(
            conn,
            task,
            board_identity="default",
            workspace=str(workspace),
            manifest=manifest,
            activation=activation,
            helper_runner=runner,
            synthetic=True,
            now=1000,
        )
        assert delivery is not None
        assert count == 3
        outcomes = [
            row["outcome_category"]
            for row in conn.execute(
                "SELECT outcome_category FROM capability_action_uses ORDER BY attempt_number"
            ).fetchall()
        ]
        assert outcomes == ["google_ads_transient", "google_ads_transient", "success"]


def test_stale_receipt_is_not_reused_after_success_budget_is_spent(tmp_path):
    with contextlib.closing(kb.connect(tmp_path / "stale.db")) as conn:
        task, activation, manifest, workspace = _task_and_activation(
            conn, tmp_path, attempts=3, ttl=1
        )
        first = actions.prepare_controller_action(
            conn,
            task,
            board_identity="default",
            workspace=str(workspace),
            manifest=manifest,
            activation=activation,
            helper_runner=_success,
            synthetic=True,
            now=1000,
        )
        assert first is not None
        assert kb.block_task(conn, task.id, reason="redispatch after ttl")
        assert kb.unblock_task(conn, task.id)
        next_task = kb.claim_task(conn, task.id)
        assert next_task is not None

        with pytest.raises(actions.ControllerActionFailure) as exc:
            actions.prepare_controller_action(
                conn,
                next_task,
                board_identity="default",
                workspace=str(workspace),
                manifest=manifest,
                activation=activation,
                helper_runner=lambda *_args: pytest.fail("spent budget must not call helper"),
                synthetic=True,
                now=1002,
            )
        assert exc.value.category == "action_budget_exhausted"
        assert conn.execute("SELECT COUNT(*) AS n FROM capability_action_uses").fetchone()["n"] == 1


def test_mismatched_activation_never_reuses_an_old_receipt(tmp_path):
    with contextlib.closing(kb.connect(tmp_path / "mismatch.db")) as conn:
        task, activation, manifest, workspace = _task_and_activation(conn, tmp_path)
        first = actions.prepare_controller_action(
            conn,
            task,
            board_identity="default",
            workspace=str(workspace),
            manifest=manifest,
            activation=activation,
            helper_runner=_success,
            synthetic=True,
            now=1000,
        )
        assert first is not None
        assert kb.block_task(conn, task.id, reason="approved activation changed")
        assert kb.unblock_task(conn, task.id)
        next_task = kb.claim_task(conn, task.id)
        assert next_task is not None

        changed = json.loads(json.dumps(activation))
        changed["test_results_sha256"] = "f" * 64
        changed_manifest = _manifest_for_activation(changed)
        calls = 0

        def runner(request, active, cwd):
            nonlocal calls
            calls += 1
            return _success(request, active, cwd)

        second = actions.prepare_controller_action(
            conn,
            next_task,
            board_identity="default",
            workspace=str(workspace),
            manifest=changed_manifest,
            activation=changed,
            helper_runner=runner,
            synthetic=True,
            now=1001,
        )
        assert second is not None and second.reused is False
        assert second.receipt_id != first.receipt_id
        assert calls == 1


def test_failure_has_no_receipt_and_can_open_exact_incident(tmp_path):
    with contextlib.closing(kb.connect(tmp_path / "kanban.db")) as conn:
        task, activation, manifest, workspace = _task_and_activation(conn, tmp_path)

        with pytest.raises(actions.ControllerActionFailure) as caught:
            actions.prepare_controller_action(
                conn,
                task,
                board_identity="default",
                workspace=str(workspace),
                manifest=manifest,
                activation=activation,
                helper_runner=lambda *_args: {
                    "version": 1,
                    "ok": False,
                    "category": "capability_source_missing",
                },
                synthetic=True,
                now=1000,
            )
        assert caught.value.incident_class == "missing_secret"
        assert conn.execute("SELECT COUNT(*) AS n FROM capability_action_receipts").fetchone()["n"] == 0
        incident = kb.open_capability_incident(
            conn,
            task.id,
            run_id=task.current_run_id,
            capability_name=caught.value.capability_name,
            incident_class=caught.value.incident_class,
            grant_digest=caught.value.grant_digest,
        )
        assert incident.state == "open"
        assert kb.get_task(conn, task.id).status == "blocked"


def test_closed_v2_action_never_projects_source_values_to_worker(tmp_path):
    with contextlib.closing(kb.connect(tmp_path / "kanban.db")) as conn:
        task, _activation, manifest, _workspace = _task_and_activation(conn, tmp_path)
        ambient = {
            "SAFE": "yes",
            "BWS_ACCESS_TOKEN": "bootstrap-secret",
            "GOOGLE_ADS_DEVELOPER_TOKEN": "ambient-developer-secret",
            "VITATIDE_MARKETING_OAUTH_CLIENT_SECRET": "ambient-client-secret",
            **{key: f"secret-{index}" for index, key in enumerate(
                wc.CAPABILITIES[actions.CAPABILITY_NAME].source_keys
            )},
        }
        plan = wc.prepare_worker_credentials(
            "marketing-operator",
            base_env=ambient,
            manifest=manifest,
            run_id=task.current_run_id,
        )
        worker_env = wc.build_worker_environment(ambient, plan)
        assert worker_env["SAFE"] == "yes"
        assert actions.CAPABILITY_NAME in plan.capabilities
        assert "BWS_ACCESS_TOKEN" not in worker_env
        for source_key in wc.CAPABILITIES[actions.CAPABILITY_NAME].source_keys:
            assert source_key not in worker_env
        assert "GOOGLE_ADS_DEVELOPER_TOKEN" not in worker_env
        assert "VITATIDE_MARKETING_OAUTH_CLIENT_SECRET" not in worker_env
        assert all("secret" not in value for value in worker_env.values())


def test_synthetic_leak_matrix_keeps_fixture_values_out_of_durable_surfaces(
    tmp_path, monkeypatch, caplog, capfd
):
    marker = os.urandom(12).hex()
    bundle = {
        "google-ads-developer-token": f"developer-{marker}",
        "google-ads-manager-customer-id": "998877",
        "vitatide-marketing-oauth-client-id": f"client-id-{marker}",
        "vitatide-marketing-oauth-client-secret": f"client-secret-{marker}",
        "vitatide-marketing-oauth-refresh-token": f"refresh-{marker}",
    }
    access_token = f"access-{marker}"
    bootstrap_token = f"bootstrap-{marker}"
    private_values = [
        value
        for key, value in bundle.items()
        if key != "google-ads-manager-customer-id"
    ] + [access_token, bootstrap_token]
    monkeypatch.setenv("BWS_ACCESS_TOKEN", bootstrap_token)

    def http(_method, url, _headers, _body, _timeout):
        if url == helper.OAUTH_URL:
            return helper.HttpResponse(
                200,
                json.dumps({"access_token": access_token}).encode(),
                {},
            )
        return helper.HttpResponse(
            200,
            json.dumps(
                {
                    "results": [
                        {
                            "campaign": {
                                "resourceName": "customers/1234567890/campaigns/77",
                                "id": "77",
                                "name": "Synthetic Campaign",
                                "status": "ENABLED",
                                "servingStatus": "SERVING",
                            }
                        }
                    ]
                }
            ).encode(),
            {"request-id": "synthetic-request"},
        )

    with contextlib.closing(kb.connect(tmp_path / "kanban.db")) as conn:
        task, activation, manifest, workspace = _task_and_activation(conn, tmp_path)

        def runner(request, _activation, _workspace):
            return helper.run_action(
                request,
                source_fetcher=lambda _request: bundle,
                http_client=http,
            )

        delivery = actions.prepare_controller_action(
            conn,
            task,
            board_identity="default",
            workspace=str(workspace),
            manifest=manifest,
            activation=activation,
            helper_runner=runner,
            synthetic=True,
            now=1000,
        )
        assert delivery is not None

        ambient = {"SAFE": "yes", "BWS_ACCESS_TOKEN": bootstrap_token}
        for index, name in enumerate(sorted(wc.CAPABILITY_SENSITIVE_ENV)):
            ambient[name] = private_values[index % len(private_values)]
        plan = wc.prepare_worker_credentials(
            "marketing-operator",
            base_env=ambient,
            manifest=manifest,
            run_id=task.current_run_id,
        )
        worker_env = wc.build_worker_environment(ambient, plan)
        (workspace / "worker-env.json").write_text(
            json.dumps(worker_env, sort_keys=True), encoding="utf-8"
        )
        (workspace / "receipt.json").write_text(
            json.dumps(delivery.receipt, sort_keys=True), encoding="utf-8"
        )

        durable = "\n".join(conn.iterdump())
        context = kb.build_worker_context(conn, task.id)
        workspace_bytes = b"".join(
            path.read_bytes() for path in workspace.iterdir() if path.is_file()
        )

    captured = capfd.readouterr()
    observable = "\n".join(
        [
            durable,
            context,
            caplog.text,
            captured.out,
            captured.err,
            workspace_bytes.decode("utf-8"),
        ]
    )
    for value in private_values:
        assert value not in observable


def test_activation_tamper_fails_before_helper(tmp_path):
    with contextlib.closing(kb.connect(tmp_path / "kanban.db")) as conn:
        task, activation, manifest, workspace = _task_and_activation(conn, tmp_path)
        tampered = json.loads(json.dumps(activation))
        tampered["operation"] = "arbitrary_query"
        called = False

        def runner(*_args):
            nonlocal called
            called = True
            return {}

        with pytest.raises(actions.ControllerActionFailure) as exc:
            actions.prepare_controller_action(
                conn,
                task,
                board_identity="default",
                workspace=str(workspace),
                manifest=manifest,
                activation=tampered,
                helper_runner=runner,
                synthetic=True,
            )
        assert exc.value.category == "capability_not_authorized"
        assert called is False
        assert conn.execute("SELECT COUNT(*) AS n FROM capability_action_uses").fetchone()["n"] == 0


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("profile",), "verifier"),
        (("operation",), "arbitrary_query"),
        (("customer_id",), "*"),
        (("campaign_resource_name",), "customers/999/campaigns/77"),
        (("api_major",), "v23"),
        (("backend",), "local-darwin"),
        (("source_project_id",), ""),
        (("source_key_names",), ["*"]),
        (("google_account_role",), "STANDARD"),
        (("core_commit_sha",), "a" * 39),
        (("config_commit_sha",), "b" * 39),
        (("installed_runtime_sha",), "a" * 39),
        (("test_results_sha256",), None),
        (("action_budget", "successful_receipts"), 2),
        (("action_budget", "provider_attempts"), 4),
        (("receipt_ttl_seconds",), 301),
        (("task_principal", "task_id"), "*"),
        (("task_principal", "board_identity"), "*"),
        (("live_activation_authorized",), True),
        (("synthetic_only",), False),
    ],
)
def test_activation_scope_and_evidence_fields_fail_closed_before_helper(
    tmp_path, path, value
):
    with contextlib.closing(kb.connect(tmp_path / "activation-field.db")) as conn:
        task, activation, _manifest, workspace = _task_and_activation(conn, tmp_path)
        malformed = json.loads(json.dumps(activation))
        target = malformed
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        manifest = _manifest_for_activation(malformed)
        called = False

        def runner(*_args):
            nonlocal called
            called = True
            return {}

        with pytest.raises(actions.ControllerActionFailure) as exc:
            actions.prepare_controller_action(
                conn,
                task,
                board_identity="default",
                workspace=str(workspace),
                manifest=manifest,
                activation=malformed,
                helper_runner=runner,
                synthetic=True,
            )
        assert exc.value.category == "capability_not_authorized"
        assert called is False
        assert conn.execute("SELECT COUNT(*) AS n FROM capability_action_uses").fetchone()["n"] == 0


def test_self_created_marketing_task_cannot_authorize_action(tmp_path):
    with contextlib.closing(kb.connect(tmp_path / "self-created.db")) as conn:
        task, activation, _manifest, workspace = _task_and_activation(conn, tmp_path)
        conn.execute(
            "UPDATE tasks SET created_by = 'marketing-operator' WHERE id = ?",
            (task.id,),
        )
        activation["task_principal"] = actions._task_principal(conn, task, "default")
        manifest = _manifest_for_activation(activation)

        with pytest.raises(actions.ControllerActionFailure) as exc:
            actions.prepare_controller_action(
                conn,
                task,
                board_identity="default",
                workspace=str(workspace),
                manifest=manifest,
                activation=activation,
                helper_runner=lambda *_args: pytest.fail("helper must not start"),
                synthetic=True,
            )
        assert exc.value.category == "capability_not_authorized"


def test_writable_or_workspace_injected_toolchain_fails_before_helper(tmp_path):
    with contextlib.closing(kb.connect(tmp_path / "toolchain.db")) as conn:
        task, activation, _manifest, workspace = _task_and_activation(conn, tmp_path)
        bws_path = Path(activation["helper_toolchain"]["bws_path"])
        bws_path.chmod(0o722)
        manifest = _manifest_for_activation(activation)

        with pytest.raises(actions.ControllerActionFailure):
            actions.prepare_controller_action(
                conn,
                task,
                board_identity="default",
                workspace=str(workspace),
                manifest=manifest,
                activation=activation,
                helper_runner=lambda *_args: pytest.fail("helper must not start"),
                synthetic=True,
            )

        bws_path.chmod(0o700)
        injected = workspace / "helper.py"
        injected.write_text("raise SystemExit(1)\n", encoding="utf-8")
        injected.chmod(0o500)
        activation["helper_toolchain"]["helper_path"] = str(injected)
        activation["helper_toolchain"]["helper_sha256"] = actions._sha256_file(injected)
        manifest = _manifest_for_activation(activation)
        with pytest.raises(actions.ControllerActionFailure):
            actions.prepare_controller_action(
                conn,
                task,
                board_identity="default",
                workspace=str(workspace),
                manifest=manifest,
                activation=activation,
                helper_runner=lambda *_args: pytest.fail("helper must not start"),
                synthetic=True,
            )


@pytest.mark.parametrize(
    "field",
    [
        "implementation_sha256",
        "runtime_sha256",
        "response_schema_sha256",
        "test_commands_sha256",
        "test_results_sha256",
        "leak_scan_sha256",
    ],
)
def test_activation_digest_bindings_fail_closed_before_helper(tmp_path, field):
    with contextlib.closing(kb.connect(tmp_path / f"{field}.db")) as conn:
        task, activation, manifest, workspace = _task_and_activation(conn, tmp_path)
        tampered = json.loads(json.dumps(activation))
        tampered[field] = "0" * 64
        called = False

        def runner(*_args):
            nonlocal called
            called = True
            return {}

        with pytest.raises(actions.ControllerActionFailure) as exc:
            actions.prepare_controller_action(
                conn,
                task,
                board_identity="default",
                workspace=str(workspace),
                manifest=manifest,
                activation=tampered,
                helper_runner=runner,
                synthetic=True,
            )
        assert exc.value.category == "capability_not_authorized"
        assert called is False


@pytest.mark.parametrize(
    ("program", "expected"),
    [
        (
            "import sys;sys.stdout.write('{\"version\":1,\"ok\":true}')",
            {"version": 1, "ok": True},
        ),
        (
            "import sys;sys.stdout.write('{\"version\":1,\"ok\":false,"
            "\"category\":\"capability_source_missing\"}');sys.exit(1)",
            {"version": 1, "ok": False, "category": "capability_source_missing"},
        ),
        (
            "import sys;sys.stderr.write('unexpected');"
            "sys.stdout.write('{\"version\":1,\"ok\":true}')",
            {"version": 1, "ok": False, "category": "response_invalid"},
        ),
        (
            "import sys;sys.stdout.write('{\"version\":1,\"ok\":true}');sys.exit(1)",
            {"version": 1, "ok": False, "category": "response_invalid"},
        ),
    ],
)
def test_helper_launcher_enforces_exit_and_stderr_protocol(
    tmp_path, monkeypatch, program, expected
):
    script = tmp_path / "synthetic_helper.py"
    script.write_text(program, encoding="utf-8")
    monkeypatch.setenv("BWS_ACCESS_TOKEN", "synthetic-bootstrap")
    activation = {
        "helper_toolchain": {
            "interpreter_path": sys.executable,
            "helper_path": str(script),
        }
    }

    assert actions._launch_helper({}, activation, str(tmp_path)) == expected


def test_helper_launcher_timeout_reaps_and_returns_fixed_category(tmp_path, monkeypatch):
    script = tmp_path / "sleeping_helper.py"
    script.write_text("import time;time.sleep(60)", encoding="utf-8")
    monkeypatch.setenv("BWS_ACCESS_TOKEN", "synthetic-bootstrap")
    monkeypatch.setattr(actions, "HELPER_TIMEOUT_SECONDS", 0.05)
    activation = {
        "helper_toolchain": {
            "interpreter_path": sys.executable,
            "helper_path": str(script),
        }
    }

    assert actions._launch_helper({}, activation, str(tmp_path)) == {
        "version": 1,
        "ok": False,
        "category": "capability_source_unavailable",
    }


@pytest.mark.skipif(os.name != "posix", reason="controller helper backend is POSIX-only")
def test_helper_launcher_disables_core_dumps(tmp_path, monkeypatch):
    script = tmp_path / "core_limit_helper.py"
    script.write_text(
        "import json,os,resource,sys;"
        "print(json.dumps({'version':1,'ok':True,"
        "'core_limit':list(resource.getrlimit(resource.RLIMIT_CORE)),"
        "'cwd':os.getcwd(),'env':sorted(os.environ),"
        "'isolated':sys.flags.isolated,'no_site':sys.flags.no_site}))",
        encoding="utf-8",
    )
    monkeypatch.setenv("BWS_ACCESS_TOKEN", "synthetic-bootstrap")
    activation = {
        "helper_toolchain": {
            "interpreter_path": sys.executable,
            "helper_path": str(script),
        }
    }

    result = actions._launch_helper({}, activation, str(tmp_path))
    assert result["core_limit"] == [0, 0]
    assert result["cwd"] == "/"
    assert result["isolated"] == 1
    assert result["no_site"] == 1
    assert "BWS_ACCESS_TOKEN" in result["env"]
    assert "PYTHONNOUSERSITE" in result["env"]
    assert not {"HOME", "PATH", "PYTHONHOME", "PYTHONPATH"} & set(result["env"])


def test_helper_launcher_discards_secret_bearing_protocol_noise(
    tmp_path, monkeypatch, capfd, caplog
):
    marker = "synthetic-bootstrap-must-not-escape-launcher"
    script = tmp_path / "noisy_helper.py"
    script.write_text(
        "import os,sys;"
        "secret=os.environ['BWS_ACCESS_TOKEN'];"
        "sys.stderr.write(secret);sys.stdout.write(secret)",
        encoding="utf-8",
    )
    monkeypatch.setenv("BWS_ACCESS_TOKEN", marker)
    activation = {
        "helper_toolchain": {
            "interpreter_path": sys.executable,
            "helper_path": str(script),
        }
    }

    assert actions._launch_helper({}, activation, str(tmp_path)) == {
        "version": 1,
        "ok": False,
        "category": "response_invalid",
    }
    captured = capfd.readouterr()
    assert marker not in captured.out
    assert marker not in captured.err
    assert marker not in caplog.text


def test_concurrent_preparation_issues_one_provider_action(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    with contextlib.closing(kb.connect(db_path)) as conn:
        task, activation, manifest, workspace = _task_and_activation(conn, tmp_path)

    entered = threading.Event()
    release = threading.Event()
    duplicate = threading.Event()
    lock = threading.Lock()
    calls = 0
    results = []
    failures = []
    monkeypatch.setattr(actions, "ACTION_RESERVATION_WAIT_SECONDS", 2)

    def runner(request, _activation, _workspace):
        nonlocal calls
        with lock:
            calls += 1
            if calls > 1:
                duplicate.set()
        entered.set()
        assert release.wait(2)
        return _success(request, _activation, _workspace)

    def prepare():
        try:
            with contextlib.closing(kb.connect(db_path)) as conn:
                results.append(
                    actions.prepare_controller_action(
                        conn,
                        task,
                        board_identity="default",
                        workspace=str(workspace),
                        manifest=manifest,
                        activation=activation,
                        helper_runner=runner,
                        synthetic=True,
                        now=1000,
                    )
                )
        except Exception as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    first = threading.Thread(target=prepare)
    second = threading.Thread(target=prepare)
    first.start()
    assert entered.wait(1)
    second.start()
    time.sleep(0.15)
    assert duplicate.is_set() is False
    release.set()
    first.join(3)
    second.join(3)

    assert failures == []
    assert len(results) == 2
    assert calls == 1
    assert {result.receipt_id for result in results if result is not None} == {1}
    with contextlib.closing(kb.connect(db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM capability_action_uses").fetchone()["n"] == 1
        assert conn.execute("SELECT COUNT(*) AS n FROM capability_action_receipts").fetchone()["n"] == 1


def test_stale_reservation_is_closed_and_counts_against_retry_budget(tmp_path):
    with contextlib.closing(kb.connect(tmp_path / "stale-reservation.db")) as conn:
        task, activation, manifest, workspace = _task_and_activation(conn, tmp_path)
        activation_digest = actions.activation_sha256(activation)
        parsed = actions._validate_activation(
            conn,
            task,
            activation,
            manifest=manifest,
            activation_digest=activation_digest,
            board_identity="default",
            workspace=str(workspace),
            synthetic=True,
        )
        bindings = actions._binding_payload(
            parsed,
            contract_digest=manifest.digest,
            activation_digest=activation_digest,
        )
        binding_digest = actions._sha256_bytes(actions.activation_bytes(bindings))
        assert task.current_run_id is not None
        delivery, abandoned_use_id = actions._reserve_or_reuse(
            conn,
            task_id=task.id,
            run_id=int(task.current_run_id),
            binding_digest=binding_digest,
            bindings=bindings,
            activation=parsed,
            now=1000,
            grant_digest=manifest.digest,
        )
        assert delivery is None and abandoned_use_id > 0

        recovered = actions.prepare_controller_action(
            conn,
            task,
            board_identity="default",
            workspace=str(workspace),
            manifest=manifest,
            activation=activation,
            helper_runner=_success,
            synthetic=True,
            now=1000 + actions.ACTION_RESERVATION_STALE_SECONDS + 1,
        )
        assert recovered is not None
        uses = conn.execute(
            "SELECT attempt_number, outcome_category FROM capability_action_uses "
            "ORDER BY attempt_number"
        ).fetchall()
        assert [(row["attempt_number"], row["outcome_category"]) for row in uses] == [
            (1, "capability_source_unavailable"),
            (2, "success"),
        ]
