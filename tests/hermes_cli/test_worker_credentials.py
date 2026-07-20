from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from agent.secret_sources.base import FetchResult
from agent.secret_sources import bitwarden as bw
from hermes_cli import worker_credentials as wc


SENTINEL = "sentinel-worker-token-do-not-log"


def _write_manifest(root: Path, body: str) -> None:
    (root / wc.MANIFEST_FILENAME).write_text(body, encoding="utf-8")


def _enable_bitwarden(root: Path) -> None:
    (root / "config.yaml").write_text(
        "secrets:\n"
        "  bitwarden:\n"
        "    enabled: true\n"
        "    project_id: worker-contract-test\n"
        "    auto_install: false\n",
        encoding="utf-8",
    )


def _github_manifest(root: Path) -> None:
    _write_manifest(
        root,
        "version: 1\n"
        "profiles:\n"
        "  Releaser:\n"
        "    actions: [github_write]\n"
        "  verifier:\n"
        "    actions: []\n"
        "  marketing-operator:\n"
        "    actions: [bws_bootstrap]\n",
    )


def test_manifest_normalizes_grants_and_digest_is_whitespace_stable(tmp_path):
    _write_manifest(
        tmp_path,
        "version: 1\nprofiles:\n  RELEASEr:\n    actions: [github_write]\n",
    )
    first = wc.load_manifest(tmp_path)

    _write_manifest(
        tmp_path,
        "version: 1\n\nprofiles:\n  releaser:\n    actions: [github_write]\n",
    )
    second = wc.load_manifest(tmp_path)

    assert first.actions_for("RELEASER") == ("github_write",)
    assert first.digest == second.digest
    assert first.profiles == {"releaser": ("github_write",)}
    assert wc.load_manifest(tmp_path / "missing").profiles == {}


@pytest.mark.parametrize(
    ("manifest", "message"),
    [
        ("version: 2\nprofiles: {}\n", "version is unsupported"),
        ("version: 1\nprofiles:\n  x:\n    actions: [not_a_capability]\n", "capability is unsupported"),
        ("version: 1\nprofiles:\n  x:\n    actions: [github_write, github_write]\n", "capability is duplicated"),
    ],
)
def test_manifest_rejects_malformed_version_or_capability(tmp_path, manifest, message):
    _write_manifest(tmp_path, manifest)

    with pytest.raises(wc.WorkerCredentialError, match=message):
        wc.load_manifest(tmp_path)


def test_authorized_releaser_gets_private_handoff_only(tmp_path, monkeypatch, caplog):
    _github_manifest(tmp_path)
    _enable_bitwarden(tmp_path)
    monkeypatch.setenv("BWS_ACCESS_TOKEN", "controller-bootstrap")
    monkeypatch.setenv("GH_TOKEN", "ambient-gh-token")
    monkeypatch.setenv("GITHUB_TOKEN", "ambient-github-token")
    monkeypatch.setenv("GH_TOKEN_SECRET_WRITE", "ambient-action-token")

    def fetch(**_kwargs):
        return FetchResult(secrets={wc.GITHUB_WRITE_SOURCE_KEY: SENTINEL})

    monkeypatch.setattr(wc, "_fetch_bitwarden_result", fetch)
    with caplog.at_level(logging.INFO, logger=wc._log.name):
        plan = wc.resolve_worker_credentials(
            "Releaser", root=tmp_path, base_env=os.environ
        )

    # Use the real process environment for this assertion without allowing
    # the test helper to accidentally pass a value from a .env file as the
    # authorized action source.
    assert plan.ok
    assert plan.capabilities == ("github_write",)
    assert "github_write=present" in plan.diagnostics
    assert SENTINEL not in repr(plan)
    assert SENTINEL not in caplog.text

    child_env = wc.build_worker_environment(dict(os.environ), plan)
    assert child_env[wc.GITHUB_WRITE_HANDOFF_ENV] == SENTINEL
    assert wc.GITHUB_WRITE_SOURCE_KEY not in child_env
    assert "GH_TOKEN" not in child_env
    assert "GITHUB_TOKEN" not in child_env
    assert wc.BWS_BOOTSTRAP_ENV not in child_env


def test_empty_or_unknown_profile_receives_no_capability_or_secret(tmp_path, monkeypatch):
    _github_manifest(tmp_path)
    monkeypatch.setenv("BWS_ACCESS_TOKEN", "controller-bootstrap")
    monkeypatch.setenv("GH_TOKEN", SENTINEL)
    monkeypatch.setenv("GITHUB_TOKEN", SENTINEL)
    monkeypatch.setenv("GH_TOKEN_SECRET_WRITE", SENTINEL)
    called = []
    monkeypatch.setattr(
        wc,
        "_fetch_bitwarden_result",
        lambda **_kwargs: called.append(True),
    )

    for profile in ("verifier", "unknown-profile"):
        plan = wc.resolve_worker_credentials(profile, root=tmp_path)
        child_env = wc.build_worker_environment(dict(os.environ), plan)
        assert plan.ok
        assert plan.capabilities == ()
        assert not called
        assert wc.GITHUB_WRITE_HANDOFF_ENV not in child_env
        assert wc.BWS_BOOTSTRAP_ENV not in child_env
        assert "GH_TOKEN" not in child_env
        assert "GITHUB_TOKEN" not in child_env


def test_bws_bootstrap_is_projected_only_for_granted_profile(tmp_path, monkeypatch):
    _github_manifest(tmp_path)
    monkeypatch.setenv("BWS_ACCESS_TOKEN", "controller-bootstrap")

    marketing = wc.resolve_worker_credentials("marketing-operator", root=tmp_path)
    marketing_env = wc.build_worker_environment(dict(os.environ), marketing)
    assert marketing.ok
    assert marketing_env[wc.BWS_BOOTSTRAP_ENV] == "controller-bootstrap"
    assert marketing_env[wc.BWS_BOOTSTRAP_HANDOFF_ENV] == "controller-bootstrap"

    verifier = wc.resolve_worker_credentials("verifier", root=tmp_path)
    verifier_env = wc.build_worker_environment(dict(os.environ), verifier)
    assert wc.BWS_BOOTSTRAP_ENV not in verifier_env
    assert wc.BWS_BOOTSTRAP_HANDOFF_ENV not in verifier_env

    unknown = wc.resolve_worker_credentials("not-listed", root=tmp_path)
    unknown_env = wc.build_worker_environment(dict(os.environ), unknown)
    assert wc.BWS_BOOTSTRAP_ENV not in unknown_env


def test_missing_bootstrap_and_secret_are_safe_failures(tmp_path, monkeypatch):
    _github_manifest(tmp_path)
    _enable_bitwarden(tmp_path)
    monkeypatch.delenv("BWS_ACCESS_TOKEN", raising=False)
    missing_bootstrap = wc.resolve_worker_credentials("releaser", root=tmp_path)
    assert not missing_bootstrap.ok
    assert "missing BWS bootstrap" in (missing_bootstrap.error or "")
    assert SENTINEL not in repr(missing_bootstrap)

    monkeypatch.setenv("BWS_ACCESS_TOKEN", "controller-bootstrap")
    monkeypatch.setattr(
        wc,
        "_fetch_bitwarden_result",
        lambda **_kwargs: FetchResult(secrets={}),
    )
    missing_secret = wc.resolve_worker_credentials("releaser", root=tmp_path)
    assert not missing_secret.ok
    assert "GitHub write secret is missing" in (missing_secret.error or "")
    assert SENTINEL not in repr(missing_secret)


def test_action_value_present_only_in_dotenv_is_not_used(tmp_path, monkeypatch):
    _github_manifest(tmp_path)
    _enable_bitwarden(tmp_path)
    (tmp_path / ".env").write_text(
        f"{wc.GITHUB_WRITE_SOURCE_KEY}={SENTINEL}\n", encoding="utf-8"
    )
    monkeypatch.setenv("BWS_ACCESS_TOKEN", "controller-bootstrap")
    monkeypatch.setenv(wc.GITHUB_WRITE_SOURCE_KEY, SENTINEL)
    monkeypatch.setattr(
        wc,
        "_fetch_bitwarden_result",
        lambda **_kwargs: FetchResult(secrets={}),
    )

    plan = wc.resolve_worker_credentials("releaser", root=tmp_path)
    assert not plan.ok
    assert "GitHub write secret is missing" in (plan.error or "")
    assert SENTINEL not in repr(plan)


def test_adapter_exception_is_redacted_from_result_exception_and_log(
    tmp_path, monkeypatch, caplog
):
    _github_manifest(tmp_path)
    _enable_bitwarden(tmp_path)
    monkeypatch.setenv("BWS_ACCESS_TOKEN", "controller-bootstrap")

    from agent.secret_sources import registry

    class FailingSource:
        name = "bitwarden"

        @staticmethod
        def fetch(_config, _home):
            raise RuntimeError(SENTINEL)

    monkeypatch.setattr(registry, "get_source", lambda _name: FailingSource())
    with caplog.at_level(logging.INFO, logger=wc._log.name):
        plan = wc.resolve_worker_credentials("releaser", root=tmp_path)

    assert not plan.ok
    assert SENTINEL not in repr(plan)
    assert SENTINEL not in (plan.error or "")
    assert SENTINEL not in caplog.text
    with pytest.raises(wc.WorkerCredentialError) as caught:
        plan.require_ok()
    assert SENTINEL not in repr(caught.value)


def test_existing_bitwarden_cache_is_used_and_failures_do_not_become_credentials(
    tmp_path, monkeypatch
):
    _github_manifest(tmp_path)
    _enable_bitwarden(tmp_path)
    monkeypatch.setenv("BWS_ACCESS_TOKEN", "controller-bootstrap")
    calls = []

    monkeypatch.setattr(bw, "find_bws", lambda install_if_missing=True: Path("/bin/bws"))

    def run_bws(*_args):
        calls.append(True)
        return {wc.GITHUB_WRITE_SOURCE_KEY: SENTINEL}, []

    monkeypatch.setattr(bw, "_run_bws_list", run_bws)
    bw._reset_cache_for_tests(tmp_path)
    try:
        first = wc.resolve_worker_credentials("releaser", root=tmp_path)
        second = wc.resolve_worker_credentials("releaser", root=tmp_path)
        assert first.ok and second.ok
        assert len(calls) == 1

        def fail_bws(*_args):
            calls.append(True)
            raise RuntimeError("provider unavailable")

        # A distinct project avoids the successful cache entry and proves a
        # source failure is a safe preflight result, not an ambient fallback.
        (tmp_path / "config.yaml").write_text(
            "secrets:\n  bitwarden:\n    enabled: true\n"
            "    project_id: worker-contract-failure\n    auto_install: false\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(bw, "_run_bws_list", fail_bws)
        failed = wc.resolve_worker_credentials("releaser", root=tmp_path)
        assert not failed.ok
        assert "Bitwarden source failed" in (failed.error or "")
        assert len(calls) == 2
    finally:
        bw._reset_cache_for_tests(tmp_path)


def test_private_handoff_is_consumed_and_removed_from_os_environ(monkeypatch):
    monkeypatch.setenv(wc.GITHUB_WRITE_HANDOFF_ENV, SENTINEL)
    monkeypatch.setenv("HERMES_WORKER_CREDENTIAL_UNKNOWN", SENTINEL)

    consumed = wc.consume_worker_credential_handoff()

    assert wc.GITHUB_WRITE_HANDOFF_ENV not in os.environ
    assert "HERMES_WORKER_CREDENTIAL_UNKNOWN" not in os.environ
    assert consumed.capabilities == ("github_write",)
    assert SENTINEL not in repr(consumed)
    assert wc.get_consumed_worker_credential("github_write") == SENTINEL
