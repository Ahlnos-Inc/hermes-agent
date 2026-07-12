"""Guard tests for Ahlnos-local cron additions (per-job fallback_providers + profile).

These params are repeatedly dropped when merging upstream NousResearch into the
fork — the git history is littered with "restore profile/fallback_providers params
lost in upstream merge". This suite turns that silent regression into a LOUD red
test: it fails if a merge drops the create_job/update_job params, or breaks the
cron.jobs re-export of the fork-owned validators. See cron/ahlnos_jobs_ext.py.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


@pytest.fixture
def cron_env(tmp_path, monkeypatch):
    """Isolated cron environment with temp HERMES_HOME (mirrors other cron tests)."""
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "cron").mkdir()
    (hermes_home / "cron" / "output").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    import cron.jobs as jobs_mod
    monkeypatch.setattr(jobs_mod, "HERMES_DIR", hermes_home)
    monkeypatch.setattr(jobs_mod, "CRON_DIR", hermes_home / "cron")
    monkeypatch.setattr(jobs_mod, "JOBS_FILE", hermes_home / "cron" / "jobs.json")
    monkeypatch.setattr(jobs_mod, "OUTPUT_DIR", hermes_home / "cron" / "output")
    return hermes_home


def test_reexport_contract():
    """cron.jobs must keep re-exporting the fork-owned validators (upstream callers
    like tools/cronjob_tools.py import them from cron.jobs)."""
    from cron.jobs import _normalize_fallback_providers, _normalize_profile
    from cron import ahlnos_jobs_ext

    assert _normalize_fallback_providers is ahlnos_jobs_ext._normalize_fallback_providers
    assert _normalize_profile is ahlnos_jobs_ext._normalize_profile


def test_create_job_persists_fallback_providers_and_profile(cron_env):
    from cron.jobs import create_job, get_job

    fb = [{"provider": "anthropic", "model": "claude-opus-4-8"}]
    job = create_job(prompt="guard", schedule="every 1h",
                     fallback_providers=fb, profile="default")
    loaded = get_job(job["id"])
    assert loaded["fallback_providers"] == fb
    assert loaded["profile"] == "default"


def test_create_job_fallback_none_vs_empty(cron_env):
    """None => inherit the profile chain; explicit [] => pinned 'no fallback'."""
    from cron.jobs import create_job, get_job

    j_none = create_job(prompt="a", schedule="every 1h", fallback_providers=None)
    j_empty = create_job(prompt="b", schedule="every 1h", fallback_providers=[])
    assert get_job(j_none["id"])["fallback_providers"] is None
    assert get_job(j_empty["id"])["fallback_providers"] == []


def test_create_job_rejects_malformed_fallback(cron_env):
    """A malformed value must raise, never silently normalize to None (= inherit)."""
    from cron.jobs import create_job

    with pytest.raises(ValueError):
        create_job(prompt="c", schedule="every 1h", fallback_providers="anthropic")


def test_update_job_persists_fallback_and_profile(cron_env):
    from cron.jobs import create_job, update_job, get_job

    job = create_job(prompt="d", schedule="every 1h")
    fb = [{"provider": "deepseek", "model": "deepseek-v4-pro"}]
    update_job(job["id"], {"fallback_providers": fb, "profile": "default"})
    loaded = get_job(job["id"])
    assert loaded["fallback_providers"] == fb
    assert loaded["profile"] == "default"
