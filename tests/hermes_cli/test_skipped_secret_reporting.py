"""A vault secret that never becomes an env var must say so.

BUILD-793. ``SourceReport`` has carried ``skipped_invalid`` ("bad env-var
name") since it was written, and nothing ever printed it. So a secret stored
with a hyphenated name is present in the vault, the startup banner reports a
healthy "applied N", and the value silently never arrives — which is exactly
what happened while wiring BUILD-603's GitHub tokens, where the capability
resolves by env-var name and would have kept using the old broad credential.
"""

import pytest

from hermes_cli import env_loader
from hermes_cli.worker_credentials import render_skipped_secret_audit

SECRET = "do-not-print-me"


class _Result:
    ok = True
    error = None
    warnings: tuple = ()


class _Src:
    """The shape ``_render_skipped`` reads off a SourceReport."""

    def __init__(self, **buckets):
        self.label = "Bitwarden Secrets Manager"
        self.result = _Result()
        self.skipped_invalid = buckets.get("invalid", [])
        self.skipped_protected = buckets.get("protected", [])
        self.skipped_claimed = buckets.get("claimed", [])
        self.skipped_existing = buckets.get("existing", [])


@pytest.fixture(autouse=True)
def _clean():
    env_loader.reset_secret_source_cache()
    yield
    env_loader.reset_secret_source_cache()


def test_invalid_env_name_is_reported_by_name():
    lines = env_loader._render_skipped(_Src(invalid=["HERMES-RELEASER-AHLNOS-INC"]))
    assert len(lines) == 1
    assert "HERMES-RELEASER-AHLNOS-INC" in lines[0]
    assert "not a valid environment variable name" in lines[0]
    assert SECRET not in lines[0]


def test_each_bucket_is_distinguishable():
    lines = env_loader._render_skipped(
        _Src(invalid=["A-B"], protected=["BWS_ACCESS_TOKEN"],
             claimed=["DUPE"], existing=["LOCAL_WINS"])
    )
    assert len(lines) == 4
    joined = "\n".join(lines)
    for name in ("A-B", "BWS_ACCESS_TOKEN", "DUPE", "LOCAL_WINS"):
        assert name in joined
    # Four distinct reasons, not the same sentence four times.
    reasons = {line.split(": ", 2)[1] for line in lines}
    assert len(reasons) == 4


def test_nothing_skipped_prints_nothing():
    assert env_loader._render_skipped(_Src()) == []


def test_skips_are_inspectable_after_startup():
    """The startup line scrolls away; the audit has to still answer for it."""
    env_loader._render_skipped(_Src(invalid=["A-B"], existing=["LOCAL_WINS"]))
    assert env_loader.skipped_secret_names()["A-B"].startswith("NOT APPLIED")

    audit = "\n".join(render_skipped_secret_audit())
    assert "A-B" in audit and "LOCAL_WINS" in audit
    assert "NOT APPLIED" in audit
    assert SECRET not in audit


def test_audit_says_none_when_everything_applied():
    assert render_skipped_secret_audit() == [
        "Vault secrets not applied to the environment: none"
    ]


def test_apply_path_prints_the_skip_using_the_real_report_types(
    monkeypatch, tmp_path, capsys
):
    """Drive the real ``_apply_external_secret_sources`` seam.

    Uses the registry's own ``ApplyReport``/``SourceReport``/``FetchResult``
    rather than a stub, so a rename of ``skipped_invalid`` breaks this test
    instead of silently reverting the behaviour.
    """
    from agent.secret_sources import registry as reg
    from agent.secret_sources.base import FetchResult

    report = reg.ApplyReport()
    sr = reg.SourceReport(
        name="bitwarden",
        label="Bitwarden Secrets Manager",
        result=FetchResult(secrets={}),
    )
    sr.applied.append("GOOD_NAME")
    sr.skipped_invalid.append("HERMES-RELEASER-AHLNOS-INC")
    report.sources.append(sr)

    monkeypatch.setattr(
        env_loader, "_load_secrets_config", lambda _p: {"bitwarden": {"enabled": True}}
    )
    monkeypatch.setattr(reg, "apply_all", lambda cfg, home: report)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)

    env_loader._apply_external_secret_sources(tmp_path)

    err = capsys.readouterr().err
    assert "applied 1 secret (GOOD_NAME)" in err
    assert "HERMES-RELEASER-AHLNOS-INC" in err
    assert "not a valid environment variable name" in err


def test_a_hyphenated_name_really_is_invalid():
    """Pin the premise: this is why the secret vanished (BUILD-603)."""
    from agent.secret_sources.registry import is_valid_env_name

    assert not is_valid_env_name("HERMES-RELEASER-AHLNOS-INC")
    assert is_valid_env_name("HERMES_RELEASER_AHLNOS_INC")


def test_reset_clears_the_record():
    env_loader._render_skipped(_Src(invalid=["A-B"]))
    assert env_loader.skipped_secret_names()
    env_loader.reset_secret_source_cache()
    assert env_loader.skipped_secret_names() == {}
