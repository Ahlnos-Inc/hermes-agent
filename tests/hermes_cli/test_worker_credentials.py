from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

import pytest

from agent.secret_sources.base import FetchResult
from agent.secret_sources import bitwarden as bw
from hermes_cli import worker_credentials as wc


SENTINEL = "sentinel-worker-token-do-not-log"
# BUILD-603: github_write is owner-scoped, so every releaser resolution
# needs the owner of the repository the task publishes to.
RELEASE_OWNER = "Ahlnos-Inc"
RESOLVE_KEY = wc.GITHUB_WRITE_RESOLVE_KEYS["ahlnos-inc"]


@pytest.fixture(autouse=True)
def _reset_worker_credential_state():
    wc.reset_worker_credential_context_for_tests()
    yield
    wc.reset_worker_credential_context_for_tests()


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


def test_manifest_v2_supports_closed_controller_action_without_v1_regression(tmp_path):
    activation_digest = "a" * 64
    _write_manifest(
        tmp_path,
        """version: 2
profiles:
  marketing-operator:
    actions:
      google_ads_campaign_status_read:
        activation_sha256: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
  releaser:
    actions:
      github_write: {}
""",
    )
    manifest = wc.load_manifest(tmp_path)
    assert manifest.version == 2
    assert manifest.actions_for("marketing-operator") == (
        "google_ads_campaign_status_read",
    )
    assert manifest.config_for(
        "marketing-operator", "google_ads_campaign_status_read"
    ) == {"activation_sha256": activation_digest}
    assert manifest.actions_for("releaser") == ("github_write",)
    assert manifest.path == tmp_path / wc.MANIFEST_FILENAME


def test_v1_cannot_grant_controller_action(tmp_path):
    _write_manifest(
        tmp_path,
        """version: 1
profiles:
  marketing-operator:
    actions: [google_ads_campaign_status_read]
""",
    )
    with pytest.raises(wc.WorkerCredentialError, match="contract version 2"):
        wc.load_manifest(tmp_path)


def test_trusted_worker_identity_cannot_be_switched_by_environment_mutation(
    tmp_path, monkeypatch
):
    _write_manifest(tmp_path, "version: 1\nprofiles:\n  releaser:\n    actions: []\n")
    manifest = wc.load_manifest(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_PROFILE", "releaser")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_receipt_owner")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "42")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "owner.db"))
    monkeypatch.setenv(wc.MANIFEST_DIGEST_ENV, manifest.digest)

    assert wc.trusted_worker_identity() == ("t_receipt_owner", 42)
    assert wc.trusted_worker_receipt_context() == (
        "t_receipt_owner",
        42,
        str(tmp_path / "owner.db"),
    )

    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "other.db"))
    assert wc.trusted_worker_receipt_context() == (
        "t_receipt_owner",
        42,
        str(tmp_path / "owner.db"),
    )

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_other")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "99")
    assert wc.trusted_worker_identity() is None


@pytest.mark.parametrize(
    ("manifest", "message"),
    [
        ("version: 3\nprofiles: {}\n", "version is unsupported"),
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
        return FetchResult(secrets={RESOLVE_KEY: SENTINEL})

    monkeypatch.setattr(wc, "_fetch_bitwarden_result", fetch)
    with caplog.at_level(logging.INFO, logger=wc._log.name):
        plan = wc.resolve_worker_credentials(
            "Releaser", root=tmp_path, base_env=os.environ, github_owner=RELEASE_OWNER
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
    assert child_env[wc.MANIFEST_DIGEST_ENV] == plan.manifest_digest


@pytest.mark.parametrize(
    ("fetch_result", "expected_error"),
    [
        # Bitwarden reached the source but the GitHub write secret is absent
        # (e.g. rotated away / never provisioned) — the "unauthenticated"
        # publication case: gh/git would have no usable token.
        (
            FetchResult(secrets={}),
            "worker credential preflight GitHub write secret is missing",
        ),
        # The Bitwarden source itself failed (bootstrap wrong, network, adapter
        # error) — also leaves the publisher without credentials.
        (
            FetchResult(error="bitwarden adapter failed"),
            "worker credential preflight Bitwarden source failed",
        ),
    ],
)
def test_unauthenticated_publication_fails_closed_without_leaking_secrets(
    tmp_path, monkeypatch, caplog, fetch_result, expected_error
):
    """BUILD-568: a releaser/verifier publication with no usable GitHub write
    credential must fail closed with an ACTIONABLE, NON-SECRET diagnostic and
    project NO token into the worker environment — so an unauthenticated publish
    never silently proceeds and never discards work by pushing with a bogus or
    ambient token. Regression fixture for the unauthenticated publication path.
    """
    _github_manifest(tmp_path)
    _enable_bitwarden(tmp_path)
    monkeypatch.setenv("BWS_ACCESS_TOKEN", "controller-bootstrap")
    # Ambient tokens that MUST NOT be treated as an authorized action source
    # and MUST NOT leak into the diagnostic/log.
    monkeypatch.setenv("GH_TOKEN", SENTINEL)
    monkeypatch.setenv("GITHUB_TOKEN", SENTINEL)
    monkeypatch.setenv("GH_TOKEN_SECRET_WRITE", SENTINEL)

    monkeypatch.setattr(wc, "_fetch_bitwarden_result", lambda **_kw: fetch_result)
    with caplog.at_level(logging.INFO, logger=wc._log.name):
        plan = wc.resolve_worker_credentials(
            "Releaser", root=tmp_path, base_env=os.environ, github_owner=RELEASE_OWNER
        )

    # Fail closed with an actionable, non-secret diagnostic.
    assert not plan.ok
    assert plan.error == expected_error
    assert "github_write=missing" in plan.diagnostics
    # No secret value anywhere in the plan or its logs.
    assert SENTINEL not in repr(plan)
    assert SENTINEL not in (plan.error or "")
    assert SENTINEL not in caplog.text

    # No github handoff is projected and ambient tokens are stripped, so the
    # worker cannot publish with an unauthorized token (commit stays untouched).
    child_env = wc.build_worker_environment(dict(os.environ), plan)
    assert wc.GITHUB_WRITE_HANDOFF_ENV not in child_env
    assert "GH_TOKEN" not in child_env
    assert "GITHUB_TOKEN" not in child_env
    assert wc.GITHUB_WRITE_SOURCE_KEY not in child_env


def test_releaser_bootstrap_strips_ambient_credentials(tmp_path, monkeypatch):
    _github_manifest(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_PROFILE", "releaser")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-bootstrap-releaser")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "11")
    monkeypatch.setenv(wc.GITHUB_WRITE_HANDOFF_ENV, SENTINEL)
    monkeypatch.setenv("GH_TOKEN", "dotenv-value")
    monkeypatch.setenv("BWS_ACCESS_TOKEN", "dotenv-value")
    monkeypatch.setenv("GH_TOKEN_SECRET_WRITE", "dotenv-value")
    monkeypatch.setenv(
        wc.MANIFEST_DIGEST_ENV, wc.load_manifest(tmp_path).digest
    )

    runtime = wc.bootstrap_worker_credential_context()

    assert runtime is not None
    assert "GH_TOKEN" not in os.environ
    assert "BWS_ACCESS_TOKEN" not in os.environ
    assert "GH_TOKEN_SECRET_WRITE" not in os.environ
    assert wc.has_trusted_worker_action("github_write")


def test_worker_credential_strip_env_uses_configured_bitwarden_name(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(wc, "_bitwarden_config", lambda _root: None)
    default_strip_env = wc.worker_credential_strip_env(tmp_path)

    assert wc.BWS_BOOTSTRAP_ENV in default_strip_env
    assert wc.GOOGLE_ADS_CONTROLLER_BWS_TOKEN_ENV in default_strip_env
    assert "CUSTOM_BWS_TOKEN" not in default_strip_env

    monkeypatch.setattr(
        wc,
        "_bitwarden_config",
        lambda _root: {"access_token_env": "CUSTOM_BWS_TOKEN"},
    )
    assert "CUSTOM_BWS_TOKEN" in wc.worker_credential_strip_env(tmp_path)

    def fail_config(_root):
        raise RuntimeError("config read failure")

    monkeypatch.setattr(wc, "_bitwarden_config", fail_config)
    assert wc.worker_credential_strip_env(tmp_path) == frozenset(
        {
            *wc.UNCONDITIONAL_STRIP_ENV,
            *wc.CAPABILITY_SENSITIVE_ENV,
        }
    )


def test_worker_bootstrap_strips_custom_bitwarden_bootstrap_name(
    tmp_path, monkeypatch
):
    _write_manifest(tmp_path, "version: 1\nprofiles:\n  verifier:\n    actions: []\n")
    monkeypatch.setattr(
        wc,
        "_bitwarden_config",
        lambda _root: {
            "enabled": True,
            "access_token_env": "CUSTOM_BWS_TOKEN",
        },
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_PROFILE", "verifier")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-custom-bws")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "15")
    monkeypatch.setenv("CUSTOM_BWS_TOKEN", "custom-bootstrap")
    monkeypatch.setenv(wc.BWS_BOOTSTRAP_ENV, "canonical-bootstrap")
    monkeypatch.setenv(wc.MANIFEST_DIGEST_ENV, wc.load_manifest(tmp_path).digest)

    runtime = wc.bootstrap_worker_credential_context()

    assert runtime is not None
    assert "CUSTOM_BWS_TOKEN" not in os.environ
    assert wc.BWS_BOOTSTRAP_ENV not in os.environ


def test_worker_bootstrap_default_bitwarden_name_strips_canonical(
    tmp_path, monkeypatch
):
    _write_manifest(tmp_path, "version: 1\nprofiles:\n  verifier:\n    actions: []\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_PROFILE", "verifier")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-default-bws")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "16")
    monkeypatch.setenv(wc.BWS_BOOTSTRAP_ENV, "canonical-bootstrap")
    monkeypatch.setenv(wc.MANIFEST_DIGEST_ENV, wc.load_manifest(tmp_path).digest)

    runtime = wc.bootstrap_worker_credential_context()

    assert runtime is not None
    assert wc.BWS_BOOTSTRAP_ENV not in os.environ


def test_controller_bootstrap_keeps_custom_bitwarden_bootstrap_name(monkeypatch):
    monkeypatch.setattr(
        wc,
        "_bitwarden_config",
        lambda _root: {"access_token_env": "CUSTOM_BWS_TOKEN"},
    )
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    monkeypatch.setenv("CUSTOM_BWS_TOKEN", "controller-bootstrap")

    assert wc.bootstrap_worker_credential_context() is None
    assert os.environ["CUSTOM_BWS_TOKEN"] == "controller-bootstrap"


def test_marketing_bootstrap_reprojects_only_granted_bws_value(tmp_path, monkeypatch):
    _github_manifest(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_PROFILE", "marketing-operator")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-bootstrap-marketing")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "12")
    monkeypatch.setenv(wc.BWS_BOOTSTRAP_HANDOFF_ENV, "handoff-value")
    monkeypatch.setenv("BWS_ACCESS_TOKEN", "ambient-value")
    monkeypatch.setenv(
        wc.MANIFEST_DIGEST_ENV, wc.load_manifest(tmp_path).digest
    )

    runtime = wc.bootstrap_worker_credential_context()

    assert runtime is not None
    assert runtime.capabilities == ("bws_bootstrap",)
    assert os.environ[wc.BWS_BOOTSTRAP_ENV] == "handoff-value"


def test_non_worker_bootstrap_keeps_ambient_credentials(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "controller-token")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)

    assert wc.bootstrap_worker_credential_context() is None
    assert os.environ["GH_TOKEN"] == "controller-token"


def test_trusted_worker_state_is_visible_to_terminal_thread(tmp_path, monkeypatch):
    _github_manifest(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_PROFILE", "releaser")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-thread")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "7")
    monkeypatch.setenv(wc.GITHUB_WRITE_HANDOFF_ENV, SENTINEL)
    monkeypatch.setenv(
        wc.MANIFEST_DIGEST_ENV, wc.load_manifest(tmp_path).digest
    )

    runtime = wc.bootstrap_worker_credential_context()
    assert runtime is not None

    observed: dict[str, object] = {}

    def check_thread_state() -> None:
        observed["trusted"] = wc.has_trusted_worker_action("github_write")
        projected: dict[str, str] = {"PATH": "/usr/bin:/bin"}
        observed["projected"] = wc.project_github_write_terminal_environment(
            projected
        )
        observed["token"] = projected.get("GH_TOKEN")

    thread = threading.Thread(target=check_thread_state)
    thread.start()
    thread.join()

    assert observed == {
        "trusted": True,
        "projected": True,
        "token": SENTINEL,
    }


def test_manifest_digest_match_admits_grant(tmp_path, monkeypatch):
    _github_manifest(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_PROFILE", "releaser")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-digest-match")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "8")
    monkeypatch.setenv(wc.GITHUB_WRITE_HANDOFF_ENV, SENTINEL)
    monkeypatch.setenv(
        wc.MANIFEST_DIGEST_ENV, wc.load_manifest(tmp_path).digest
    )

    runtime = wc.bootstrap_worker_credential_context()

    assert runtime is not None
    assert runtime.capabilities == ("github_write",)
    assert wc.has_trusted_worker_action("github_write")


def test_manifest_digest_mismatch_admits_nothing(tmp_path, monkeypatch, caplog):
    _github_manifest(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_PROFILE", "releaser")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-digest-mismatch")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "9")
    monkeypatch.setenv(wc.GITHUB_WRITE_HANDOFF_ENV, SENTINEL)
    monkeypatch.setenv(wc.MANIFEST_DIGEST_ENV, "dispatcher-digest-is-wrong")

    with caplog.at_level(logging.WARNING, logger=wc._log.name):
        runtime = wc.bootstrap_worker_credential_context()

    assert runtime is not None
    assert runtime.capabilities == ()
    assert not wc.has_trusted_worker_action("github_write")
    assert wc.trusted_worker_identity() is None
    assert "worker credential manifest digest mismatch" in caplog.text
    assert SENTINEL not in caplog.text


def test_manifest_digest_missing_admits_nothing(tmp_path, monkeypatch, caplog):
    _github_manifest(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_PROFILE", "releaser")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-digest-missing")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "13")
    monkeypatch.setenv(wc.GITHUB_WRITE_HANDOFF_ENV, SENTINEL)
    monkeypatch.delenv(wc.MANIFEST_DIGEST_ENV, raising=False)

    with caplog.at_level(logging.WARNING, logger=wc._log.name):
        runtime = wc.bootstrap_worker_credential_context()

    assert runtime is not None
    assert runtime.capabilities == ()
    assert not wc.has_trusted_worker_action("github_write")
    assert wc.trusted_worker_identity() is None
    assert wc.GITHUB_WRITE_HANDOFF_ENV not in os.environ
    assert "worker credential manifest digest mismatch" in caplog.text


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


def test_google_ads_controller_source_token_is_never_projected_to_any_worker(
    tmp_path, monkeypatch
):
    _github_manifest(tmp_path)
    marker = "dedicated-google-ads-source-token-must-stay-controller-only"
    monkeypatch.setenv(wc.GOOGLE_ADS_CONTROLLER_BWS_TOKEN_ENV, marker)

    for profile in ("marketing-operator", "releaser", "verifier", "unknown"):
        plan = wc.resolve_worker_credentials(profile, root=tmp_path)
        worker_env = wc.build_worker_environment(dict(os.environ), plan)
        assert wc.GOOGLE_ADS_CONTROLLER_BWS_TOKEN_ENV not in worker_env
        assert marker not in worker_env.values()
    assert (
        wc.get_consumed_worker_credential("google_ads_campaign_status_read")
        is None
    )


def test_real_v2_co_grant_keeps_google_source_token_controller_only(
    tmp_path, monkeypatch
):
    _write_manifest(
        tmp_path,
        """version: 2
profiles:
  marketing-operator:
    actions:
      bws_bootstrap: {}
      google_ads_campaign_status_read:
        activation_sha256: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
""",
    )
    bootstrap = "unrelated-worker-bootstrap-token"
    google_source = "dedicated-google-source-token"
    base_env = {
        "SAFE": "yes",
        wc.BWS_BOOTSTRAP_ENV: bootstrap,
        wc.GOOGLE_ADS_CONTROLLER_BWS_TOKEN_ENV: google_source,
    }

    plan = wc.resolve_worker_credentials(
        "marketing-operator", root=tmp_path, base_env=base_env
    )
    worker_env = wc.build_worker_environment(base_env, plan)

    assert plan.ok
    assert plan.capabilities == (
        "bws_bootstrap",
        "google_ads_campaign_status_read",
    )
    assert worker_env["SAFE"] == "yes"
    assert worker_env[wc.BWS_BOOTSTRAP_ENV] == bootstrap
    assert wc.GOOGLE_ADS_CONTROLLER_BWS_TOKEN_ENV not in worker_env
    assert google_source not in worker_env.values()

    wc.consume_worker_credential_handoff(worker_env)
    assert wc.get_consumed_worker_credential("bws_bootstrap") == bootstrap
    assert (
        wc.get_consumed_worker_credential("google_ads_campaign_status_read")
        is None
    )


def test_missing_bootstrap_and_secret_are_safe_failures(tmp_path, monkeypatch):
    _github_manifest(tmp_path)
    _enable_bitwarden(tmp_path)
    monkeypatch.delenv("BWS_ACCESS_TOKEN", raising=False)
    missing_bootstrap = wc.resolve_worker_credentials(
        "releaser", root=tmp_path, github_owner=RELEASE_OWNER
    )
    assert not missing_bootstrap.ok
    assert "missing BWS bootstrap" in (missing_bootstrap.error or "")
    assert SENTINEL not in repr(missing_bootstrap)

    monkeypatch.setenv("BWS_ACCESS_TOKEN", "controller-bootstrap")
    monkeypatch.setattr(
        wc,
        "_fetch_bitwarden_result",
        lambda **_kwargs: FetchResult(secrets={}),
    )
    missing_secret = wc.resolve_worker_credentials(
        "releaser", root=tmp_path, github_owner=RELEASE_OWNER
    )
    assert not missing_secret.ok
    assert "GitHub write secret is missing" in (missing_secret.error or "")
    assert SENTINEL not in repr(missing_secret)


def test_action_value_present_only_in_dotenv_is_not_used(tmp_path, monkeypatch):
    _github_manifest(tmp_path)
    _enable_bitwarden(tmp_path)
    (tmp_path / ".env").write_text(
        f"{RESOLVE_KEY}={SENTINEL}\n", encoding="utf-8"
    )
    monkeypatch.setenv("BWS_ACCESS_TOKEN", "controller-bootstrap")
    monkeypatch.setenv(RESOLVE_KEY, SENTINEL)
    monkeypatch.setattr(
        wc,
        "_fetch_bitwarden_result",
        lambda **_kwargs: FetchResult(secrets={}),
    )

    plan = wc.resolve_worker_credentials(
        "releaser", root=tmp_path, github_owner=RELEASE_OWNER
    )
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
        plan = wc.resolve_worker_credentials(
        "releaser", root=tmp_path, github_owner=RELEASE_OWNER
    )

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
        return {RESOLVE_KEY: SENTINEL}, []

    monkeypatch.setattr(bw, "_run_bws_list", run_bws)
    bw._reset_cache_for_tests(tmp_path)
    try:
        first = wc.resolve_worker_credentials(
        "releaser", root=tmp_path, github_owner=RELEASE_OWNER
    )
        second = wc.resolve_worker_credentials(
        "releaser", root=tmp_path, github_owner=RELEASE_OWNER
    )
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
        failed = wc.resolve_worker_credentials(
        "releaser", root=tmp_path, github_owner=RELEASE_OWNER
    )
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


# ---------------------------------------------------------------------------
# BUILD-681: vault-sourced variables must not cross the worker boundary
# ambiently. The gateway's os.environ carries every secret the vault applied
# at startup (142 on the live install); keeping everything except a 13-name
# blocklist meant ~129 controller secrets reached every worker regardless of
# its manifest, which made the manifest a description rather than a control.
# ---------------------------------------------------------------------------


@pytest.fixture
def vault_env(monkeypatch):
    """Simulate a controller whose vault applied a realistic secret set."""
    applied = {
        # Action-plane secrets no worker profile is granted.
        "AWS_SECRET_ACCESS_KEY": "aws-" + SENTINEL,
        "DATABASE_URL": "postgres://user:" + SENTINEL + "@db/app",
        "SRV_JIRA_TOKEN": "jira-" + SENTINEL,
        "POSTHOG_PERSONAL_KEY": "posthog-" + SENTINEL,
        "R2_SECRET_ACCESS_KEY": "r2-" + SENTINEL,
        "VPS_SSH_KEY": "ssh-" + SENTINEL,
        # Model-provider control plane: the one class a worker still needs.
        "ANTHROPIC_TOKEN": "anthropic-provider-key",
        "OPENROUTER_API_KEY": "openrouter-provider-key",
    }
    for key, value in applied.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        "hermes_cli.env_loader.externally_sourced_env_names",
        lambda: frozenset(applied),
    )
    return applied


ACTION_PLANE_VAULT_VARS = (
    "AWS_SECRET_ACCESS_KEY",
    "DATABASE_URL",
    "SRV_JIRA_TOKEN",
    "POSTHOG_PERSONAL_KEY",
    "R2_SECRET_ACCESS_KEY",
    "VPS_SSH_KEY",
)


@pytest.mark.parametrize(
    "profile", ["verifier", "coder", "releaser", "marketing-operator", "not-listed"]
)
def test_no_worker_inherits_an_ungranted_vault_secret(
    tmp_path, monkeypatch, vault_env, profile
):
    """AC1/AC2/AC3 — including a `verifier`, whose contract is `actions: []`."""
    _github_manifest(tmp_path)
    monkeypatch.setenv("BWS_ACCESS_TOKEN", "controller-bootstrap")

    plan = wc.resolve_worker_credentials(profile, root=tmp_path)
    worker_env = wc.build_worker_environment(dict(os.environ), plan)

    for name in ACTION_PLANE_VAULT_VARS:
        assert name not in worker_env, f"{profile} still inherits {name}"
    # Not just absent by name — the value is nowhere in the environment.
    serialized = "\n".join(f"{k}={v}" for k, v in worker_env.items())
    assert SENTINEL not in serialized


def test_model_provider_credentials_still_reach_the_worker(
    tmp_path, monkeypatch, vault_env
):
    """AC4 — a worker that cannot authenticate to its own model is useless."""
    _github_manifest(tmp_path)
    plan = wc.resolve_worker_credentials("verifier", root=tmp_path)
    worker_env = wc.build_worker_environment(dict(os.environ), plan)

    assert worker_env["ANTHROPIC_TOKEN"] == "anthropic-provider-key"
    assert worker_env["OPENROUTER_API_KEY"] == "openrouter-provider-key"


def test_provider_allowlist_is_derived_from_the_code_owned_catalogs():
    """The allowlist must not drift into a hand-maintained list."""
    allowed = wc.model_provider_control_plane_env()
    for name in ("ANTHROPIC_TOKEN", "OPENROUTER_API_KEY", "GEMINI_API_KEY"):
        assert name in allowed
    # Deliberate exclusions, both verified against the live config:
    # omlx-local runs `api_key: no-key-required`, and GROQ_API_KEY is
    # speech-to-text rather than a dispatcher model provider.
    assert "oMLX_API_KEY" not in allowed
    assert "GROQ_API_KEY" not in allowed
    # Action-plane secrets must never be reachable through this door.
    for name in ACTION_PLANE_VAULT_VARS:
        assert name not in allowed


def test_a_granted_capability_still_receives_its_own_vault_source(
    tmp_path, monkeypatch
):
    """AC6 — the strip must not fight an explicit grant.

    `bws_bootstrap` resolves from a vault-sourced variable, so a naive
    "drop everything the vault applied" would delete the very value the
    manifest grants and break github_write's sibling capability.
    """
    _github_manifest(tmp_path)
    monkeypatch.setenv("BWS_ACCESS_TOKEN", "controller-bootstrap")
    monkeypatch.setattr(
        "hermes_cli.env_loader.externally_sourced_env_names",
        lambda: frozenset({"BWS_ACCESS_TOKEN"}),
    )

    marketing = wc.resolve_worker_credentials("marketing-operator", root=tmp_path)
    marketing_env = wc.build_worker_environment(dict(os.environ), marketing)
    assert marketing_env[wc.BWS_BOOTSTRAP_ENV] == "controller-bootstrap"

    verifier = wc.resolve_worker_credentials("verifier", root=tmp_path)
    verifier_env = wc.build_worker_environment(dict(os.environ), verifier)
    assert wc.BWS_BOOTSTRAP_ENV not in verifier_env


def test_withheld_variables_are_logged_by_name_and_never_by_value(
    tmp_path, monkeypatch, vault_env, caplog
):
    """AC7 — a silently missing variable is undiagnosable.

    Projection cannot prove a worker read a variable, so the honest
    substitute is an attributable record at the boundary: which profile,
    which manifest, which names.
    """
    _github_manifest(tmp_path)
    plan = wc.resolve_worker_credentials("verifier", root=tmp_path)

    with caplog.at_level(logging.INFO, logger="hermes_cli.worker_credentials"):
        wc.build_worker_environment(dict(os.environ), plan)

    withheld = [r for r in caplog.records if "withheld" in r.getMessage()]
    assert len(withheld) == 1
    message = withheld[0].getMessage()
    for name in ACTION_PLANE_VAULT_VARS:
        assert name in message
    assert plan.manifest_digest in message
    assert SENTINEL not in message


def test_nothing_is_stripped_when_no_vault_was_loaded(tmp_path, monkeypatch):
    """Back-compat: no provenance means the values are not present either."""
    _github_manifest(tmp_path)
    monkeypatch.setenv("SOME_SHELL_VAR", "from-the-shell")
    monkeypatch.setattr(
        "hermes_cli.env_loader.externally_sourced_env_names", lambda: frozenset()
    )

    plan = wc.resolve_worker_credentials("verifier", root=tmp_path)
    worker_env = wc.build_worker_environment(dict(os.environ), plan)
    assert worker_env["SOME_SHELL_VAR"] == "from-the-shell"


def test_audit_renderer_matches_the_manifest_and_prints_no_values(
    tmp_path, monkeypatch, vault_env
):
    """AC5 — the audit must be a join of the two authorities, names only."""
    _github_manifest(tmp_path)
    manifest = wc.load_manifest(root=tmp_path)

    report = wc.render_worker_credential_audit(root=tmp_path)

    # Every profile in the manifest appears, with its granted capability.
    for profile in manifest.profiles:
        assert profile in report
        for capability in manifest.actions_for(profile):
            assert capability in report
    # A profile with no action plane is rendered, not omitted — "verifier is
    # absent" and "verifier has no grants" must not look the same.
    assert "no action plane" in report
    assert manifest.digest in report
    # The vault partition is exhaustive and adds up.
    assert "AWS_SECRET_ACCESS_KEY" in report
    assert "ANTHROPIC_TOKEN" in report
    # Names only. Never values.
    assert SENTINEL not in report
    assert "anthropic-provider-key" not in report
    # BUILD-603: github_write lists two candidate source keys but delivers
    # exactly one. Both names on one row would otherwise read as "the releaser
    # holds both tokens" — the property this capability exists to deny.
    for name in wc.GITHUB_WRITE_RESOLVE_KEYS.values():
        assert name in report
    assert "1 of 2 by repository owner" in report


def test_a_grant_the_code_registry_does_not_define_is_rejected_at_load(tmp_path):
    """Why the audit renderer needs no unknown-capability branch.

    A YAML edit can only widen or narrow grants among names the code already
    knows — it can never inject one. That is what makes the rendered join
    trustworthy rather than a second, drifting authority.
    """
    _write_manifest(
        tmp_path,
        "version: 1\nprofiles:\n  releaser:\n    actions:\n"
        "      - not_a_real_capability\n",
    )
    with pytest.raises(wc.WorkerCredentialError, match="capability is unsupported"):
        wc.load_manifest(root=tmp_path)


# --- BUILD-603: owner-scoped github_write -----------------------------------
#
# A fine-grained PAT has exactly one resource owner and the release targets
# span two, so github_write cannot resolve from a single key. These cover the
# selection itself, the owner source, and the fail-closed edges.


def _git_repo(root: Path, remote_url: str | None, *, remote: str = "origin") -> Path:
    import subprocess

    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    if remote_url is not None:
        subprocess.run(
            ["git", "-C", str(root), "remote", "add", remote, remote_url],
            check=True,
        )
    return root


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/Ahlnos-Inc/hermes-agent.git", "Ahlnos-Inc"),
        ("https://github.com/nlachica/hermes-config", "nlachica"),
        ("git@github.com:Ahlnos-Inc/aldnoah.git", "Ahlnos-Inc"),
        ("ssh://git@github.com/nlachica/arivity.git", "nlachica"),
        # A remote may carry an embedded credential; the owner still parses and
        # the URL is never returned to the caller.
        ("https://x-access-token:secret@github.com/nlachica/arivity.git", "nlachica"),
        ("https://github.com:443/nlachica/arivity.git", "nlachica"),
        # Not GitHub: these tokens are GitHub credentials and must not be
        # selected for a remote pointing anywhere else. The last three are
        # host SPOOFS -- a substring match on "github.com" accepts all of
        # them and hands a real token to a worker publishing elsewhere.
        ("https://gitlab.com/Ahlnos-Inc/hermes-agent.git", None),
        ("git@example.invalid:Ahlnos-Inc/hermes-agent.git", None),
        ("https://notgithub.com/nlachica/arivity.git", None),
        ("https://evil.example/github.com/nlachica/arivity.git", None),
        ("git@example.invalid:x/github.com/Ahlnos-Inc/aldnoah.git", None),
        # A deeper path is not a repository URL.
        ("https://github.com/nlachica/arivity/extra.git", None),
    ],
)
def test_github_owner_is_read_from_the_workspace_remote(tmp_path, url, expected):
    workspace = _git_repo(tmp_path / "repo", url)
    assert wc.github_owner_for_workspace(workspace) == expected


def test_github_owner_is_none_when_the_workspace_cannot_name_a_remote(tmp_path):
    # A scratch workspace is not a repository at all.
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    assert wc.github_owner_for_workspace(scratch) is None
    assert wc.github_owner_for_workspace(None) is None

    # A repository with no such remote.
    repo = _git_repo(tmp_path / "no-remote", None)
    assert wc.github_owner_for_workspace(repo) is None

    # A named remote that exists is honoured; one that does not is not silently
    # replaced by origin.
    named = _git_repo(
        tmp_path / "named",
        "https://github.com/nlachica/hermes-config.git",
        remote="publish",
    )
    assert wc.github_owner_for_workspace(named, remote="publish") == "nlachica"
    assert wc.github_owner_for_workspace(named, remote="origin") is None

    # A remote name is task-controlled and must never become a git flag.
    assert wc.github_owner_for_workspace(named, remote="--upload-pack=touch") is None


def test_github_write_resolves_only_the_token_for_the_workspace_owner(
    tmp_path, monkeypatch, caplog
):
    _github_manifest(tmp_path)
    _enable_bitwarden(tmp_path)
    monkeypatch.setenv("BWS_ACCESS_TOKEN", "controller-bootstrap")
    ahlnos = "ahlnos-inc-fine-grained-token"
    nlachica = "nlachica-fine-grained-token"
    monkeypatch.setattr(
        wc,
        "_fetch_bitwarden_result",
        lambda **_kwargs: FetchResult(
            secrets={
                wc.GITHUB_WRITE_RESOLVE_KEYS["ahlnos-inc"]: ahlnos,
                wc.GITHUB_WRITE_RESOLVE_KEYS["nlachica"]: nlachica,
                # The classic broad-scoped token this ticket retires. It is
                # still in the vault and must never be selected again.
                "GITHUB_TOKEN": "classic-broad-scoped-token",
            }
        ),
    )

    for owner, expected, other in (
        ("Ahlnos-Inc", ahlnos, nlachica),
        ("nlachica", nlachica, ahlnos),
        # Owner comparison is case-insensitive: GitHub owners are.
        ("AHLNOS-INC", ahlnos, nlachica),
    ):
        base_env = {"BWS_ACCESS_TOKEN": "controller-bootstrap"}
        with caplog.at_level(logging.INFO, logger=wc._log.name):
            plan = wc.resolve_worker_credentials(
                "releaser", root=tmp_path, base_env=base_env, github_owner=owner
            )
        assert plan.ok, plan.error
        env = wc.build_worker_environment(base_env, plan)
        assert env[wc.GITHUB_WRITE_HANDOFF_ENV] == expected
        # The worker holds exactly one token: not the other owner's, and not
        # the classic token (BUILD-603 AC2).
        assert other not in env.values()
        assert "classic-broad-scoped-token" not in env.values()
        assert expected not in caplog.text


def test_github_write_fails_closed_when_the_owner_has_no_registered_token(
    tmp_path, monkeypatch
):
    _github_manifest(tmp_path)
    _enable_bitwarden(tmp_path)
    monkeypatch.setenv("BWS_ACCESS_TOKEN", "controller-bootstrap")
    fetched = []

    def fetch(**_kwargs):
        fetched.append(True)
        return FetchResult(secrets={RESOLVE_KEY: SENTINEL})

    monkeypatch.setattr(wc, "_fetch_bitwarden_result", fetch)

    # Every credential a fallback could reach is present in the environment,
    # so "no handoff" is a real assertion rather than one about an empty dict.
    base_env = {
        "BWS_ACCESS_TOKEN": "controller-bootstrap",
        "GITHUB_TOKEN": "classic-broad-scoped-token",
        "GH_TOKEN": "ambient-gh-token",
        **{name: f"ambient-{name}" for name in wc.GITHUB_WRITE_RESOLVE_KEYS.values()},
    }
    for owner, fragment in (
        (None, "yielded no GitHub release target"),
        ("", "yielded no GitHub release target"),
        ("some-other-org", "no release-target token is registered"),
    ):
        plan = wc.resolve_worker_credentials(
            "releaser", root=tmp_path, base_env=dict(base_env), github_owner=owner
        )
        assert not plan.ok
        assert fragment in (plan.error or "")
        assert "github_write=missing" in plan.diagnostics
        env = wc.build_worker_environment(dict(base_env), plan)
        assert wc.GITHUB_WRITE_HANDOFF_ENV not in env
        assert SENTINEL not in env.values()
        # No ambient GitHub credential survives as a substitute.
        assert not set(base_env.values()) & set(env.values())
        with pytest.raises(wc.WorkerCredentialError):
            plan.require_ok()

    # Owner selection is policy and needs no vault: an unresolvable owner must
    # not even reach the secret source.
    assert fetched == []


def test_both_release_target_tokens_are_stripped_from_every_worker(
    tmp_path, monkeypatch
):
    _github_manifest(tmp_path)
    _enable_bitwarden(tmp_path)
    monkeypatch.setenv("BWS_ACCESS_TOKEN", "controller-bootstrap")
    monkeypatch.setattr(
        wc,
        "_fetch_bitwarden_result",
        lambda **_kwargs: FetchResult(secrets={RESOLVE_KEY: SENTINEL}),
    )
    ambient = {
        name: f"ambient-{name}" for name in wc.GITHUB_WRITE_RESOLVE_KEYS.values()
    }
    ambient["GITHUB_TOKEN"] = "ambient-classic"
    base_env = {**ambient, "BWS_ACCESS_TOKEN": "controller-bootstrap"}

    for profile, owner in (
        ("releaser", RELEASE_OWNER),
        ("verifier", None),
        ("marketing-operator", None),
    ):
        plan = wc.resolve_worker_credentials(
            profile, root=tmp_path, base_env=dict(base_env), github_owner=owner
        )
        assert plan.ok, (profile, plan.error)
        env = wc.build_worker_environment(dict(base_env), plan)
        for name in (*wc.GITHUB_WRITE_RESOLVE_KEYS.values(), "GITHUB_TOKEN"):
            assert name not in env, (profile, name)
        assert not set(ambient.values()) & set(env.values()), profile


def test_owner_comes_from_the_push_url_not_the_fetch_url(tmp_path):
    """A remote can fetch from one owner and push to another.

    The fetch-upstream / push-fork setup is standard, and it is where the
    push LANDS that decides which fine-grained PAT is the correct one.
    Reading the fetch url hands out the wrong owner's token and the push then
    403s while the preflight reports ``github_write=present``.
    """
    import subprocess

    repo = _git_repo(tmp_path / "split", "https://github.com/nlachica/hermes-config.git")
    subprocess.run(
        [
            "git", "-C", str(repo), "remote", "set-url", "--push", "origin",
            "https://github.com/Ahlnos-Inc/aldnoah.git",
        ],
        check=True,
    )

    assert wc.github_owner_for_workspace(repo) == "Ahlnos-Inc"
    assert wc.github_write_resolve_key(
        wc.github_owner_for_workspace(repo)
    ) == wc.GITHUB_WRITE_RESOLVE_KEYS["ahlnos-inc"]


def test_ambient_git_env_cannot_redirect_the_owner_probe(tmp_path, monkeypatch):
    """The probe must answer about the WORKSPACE, not about GIT_DIR.

    The dispatcher's own environment is inherited by the probe, and GIT_DIR /
    GIT_WORK_TREE take precedence over ``git -C`` discovery -- so an ambient
    value would silently select a token for a repository the worker never
    touches.
    """
    workspace = _git_repo(
        tmp_path / "workspace", "https://github.com/nlachica/hermes-config.git"
    )
    elsewhere = _git_repo(
        tmp_path / "elsewhere", "https://github.com/Ahlnos-Inc/aldnoah.git"
    )
    monkeypatch.setenv("GIT_DIR", str(elsewhere / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(elsewhere))

    assert wc.github_owner_for_workspace(workspace) == "nlachica"


# --- BUILD-601: the credentials that replace marketing-operator's vault token -


MARKETING_READ_CAPABILITIES = (
    "meta_marketing_read",
    "instagram_graph_read",
    "posthog_read",
)


def _marketing_manifest(root: Path) -> None:
    actions = ", ".join(MARKETING_READ_CAPABILITIES)
    _write_manifest(
        root,
        "version: 1\n"
        "profiles:\n"
        "  marketing-operator:\n"
        f"    actions: [{actions}]\n"
        "  verifier:\n"
        "    actions: []\n",
    )


def test_vault_sourced_capabilities_is_derived_not_a_literal(tmp_path):
    """The set that needs a vault fetch comes from the registry.

    Before BUILD-601 this was the literal ``"github_write" in capabilities``,
    so a new capability silently resolved to nothing.
    """
    assert wc.vault_sourced_capabilities(("github_write",)) == ("github_write",)
    assert wc.vault_sourced_capabilities(MARKETING_READ_CAPABILITIES) == (
        MARKETING_READ_CAPABILITIES
    )
    # bws_bootstrap's source IS the controller bootstrap variable: no fetch.
    assert wc.vault_sourced_capabilities(("bws_bootstrap",)) == ()
    # A controller-action capability never projects into a worker at all.
    assert wc.vault_sourced_capabilities(("google_ads_campaign_status_read",)) == ()
    assert wc.vault_sourced_capabilities(()) == ()


def test_marketing_read_capabilities_resolve_each_from_its_own_record(
    tmp_path, monkeypatch, caplog
):
    """Each grant resolves its own Bitwarden RECORD name, hyphens included."""
    _marketing_manifest(tmp_path)
    _enable_bitwarden(tmp_path)
    values = {
        "meta-system-user-token": "meta-value",
        "INSTAGRAM_GRAPH_TOKEN": "instagram-value",
        "POSTHOG_PERSONAL_KEY": "posthog-value",
        # Present in the same vault and granted to nobody here.
        "WAVE_FULL_ACCESS_TOKEN": "unrelated-value",
    }
    monkeypatch.setattr(
        wc,
        "_fetch_bitwarden_result",
        lambda **_kwargs: FetchResult(secrets=dict(values)),
    )
    # The controller's OWN environment already carries these vault names, so
    # they must be in base_env or the "not in the worker env" assertions below
    # are about an absence that was never there to begin with.
    base_env = {
        "BWS_ACCESS_TOKEN": "controller-bootstrap",
        "INSTAGRAM_GRAPH_TOKEN": "ambient-instagram",
        "POSTHOG_PERSONAL_KEY": "ambient-posthog",
        "META_SYSTEM_USER_TOKEN": "ambient-meta",
        "SAFE": "yes",
    }

    with caplog.at_level(logging.INFO, logger=wc._log.name):
        plan = wc.resolve_worker_credentials(
            "marketing-operator", root=tmp_path, base_env=base_env
        )

    assert plan.ok, plan.error
    assert plan.capabilities == tuple(sorted(MARKETING_READ_CAPABILITIES))
    for capability in MARKETING_READ_CAPABILITIES:
        assert f"{capability}=present" in plan.diagnostics
    # Values never appear in the plan or its log line.
    for value in values.values():
        assert value not in repr(plan)
        assert value not in caplog.text

    env = wc.build_worker_environment(base_env, plan)
    handoffs = {
        wc.META_MARKETING_HANDOFF_ENV: "meta-value",
        wc.INSTAGRAM_GRAPH_HANDOFF_ENV: "instagram-value",
        wc.POSTHOG_READ_HANDOFF_ENV: "posthog-value",
    }
    assert {k: env[k] for k in handoffs} == handoffs
    # Terminal-only, like github_write: the projected names are NOT in the
    # worker's own environment, and no ungranted secret rides along.
    for projected in ("META_SYSTEM_USER_TOKEN", "INSTAGRAM_GRAPH_TOKEN",
                      "POSTHOG_PERSONAL_KEY"):
        assert projected not in env, projected
    # ...and the ambient controller copies are gone too, not merely the
    # resolved ones. A grant must not become a second, unaudited delivery path.
    for ambient in ("ambient-instagram", "ambient-posthog", "ambient-meta"):
        assert ambient not in env.values(), ambient
    assert env["SAFE"] == "yes"
    assert "unrelated-value" not in env.values()
    # The vault token itself is not granted here and must not be projected.
    assert wc.BWS_BOOTSTRAP_ENV not in env


def test_one_missing_marketing_secret_fails_the_whole_preflight(tmp_path, monkeypatch):
    """A partially-credentialed worker fails later and further from the cause."""
    _marketing_manifest(tmp_path)
    _enable_bitwarden(tmp_path)
    monkeypatch.setattr(
        wc,
        "_fetch_bitwarden_result",
        lambda **_kwargs: FetchResult(
            secrets={
                "meta-system-user-token": "meta-value",
                "INSTAGRAM_GRAPH_TOKEN": "instagram-value",
                # POSTHOG_PERSONAL_KEY absent.
            }
        ),
    )
    base_env = {"BWS_ACCESS_TOKEN": "controller-bootstrap"}

    plan = wc.resolve_worker_credentials(
        "marketing-operator", root=tmp_path, base_env=base_env
    )

    assert not plan.ok
    assert "posthog_read" in (plan.error or "")
    assert "posthog_read=missing" in plan.diagnostics
    env = wc.build_worker_environment(base_env, plan)
    # No partial handoff survives the failure.
    for name in (wc.META_MARKETING_HANDOFF_ENV, wc.INSTAGRAM_GRAPH_HANDOFF_ENV,
                 wc.POSTHOG_READ_HANDOFF_ENV):
        assert name not in env
    assert "meta-value" not in env.values()
    with pytest.raises(wc.WorkerCredentialError):
        plan.require_ok()


def test_every_capability_handoff_name_has_a_consumer():
    """A handoff name with no consumer leaks to a later shell hook.

    The consume map is derived from the registry so this cannot regress by
    forgetting to extend a literal.
    """
    declared = {
        definition.handoff_env
        for definition in wc.CAPABILITIES.values()
        if definition.handoff_env
    }
    assert declared == set(wc._HANDOFF_ENV_TO_CAPABILITY)
    for handoff_env, capability in wc._HANDOFF_ENV_TO_CAPABILITY.items():
        assert handoff_env.startswith(wc.PRIVATE_HANDOFF_PREFIX)
        assert wc.CAPABILITIES[capability].handoff_env == handoff_env

    consumed = wc.consume_worker_credential_handoff(
        {name: f"value-for-{name}" for name in declared}
    )
    assert set(consumed.capabilities) == set(wc._HANDOFF_ENV_TO_CAPABILITY.values())


def test_every_capability_source_and_projection_name_is_strip_listed():
    """The strip list is a property of the registry, not a parallel list.

    Asserted directly because the overlap between record names and projected
    names hides gaps: today two of the marketing record names happen to equal
    their projected names, so omitting singular ``source_key`` breaks no
    behaviour -- until a capability sources from a legal env name that differs
    from what it projects, at which point a granted worker silently inherits
    the controller's ambient copy.
    """
    for definition in wc.CAPABILITIES.values():
        names = (
            *((definition.source_key,) if definition.source_key else ()),
            *definition.source_keys,
            *definition.projection_env,
            *definition.ambient_strip_env,
        )
        assert names, definition.name
        for name in names:
            assert name in wc.CAPABILITY_SENSITIVE_ENV, (definition.name, name)
