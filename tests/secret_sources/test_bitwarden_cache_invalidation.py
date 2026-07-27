"""BUILD-816: what a changed Bitwarden value actually does, not what a comment claims.

The scheduler used to say its per-run ``reset_secret_source_cache()`` "forces
the re-pull".  It does not — that call clears only which HERMES_HOME paths have
had secrets applied; the VALUES keep coming from the two-layer value cache until
``cache_ttl_seconds`` expires.  These tests pin the real semantics so the next
person reads behaviour instead of prose:

* a value changed at the source is NOT visible while the cache entry is fresh;
* it IS visible after the documented invalidation — deleting the disk cache,
  which is the only channel an operator has into a running gateway.
"""

from __future__ import annotations

import pytest

TOKEN = "0.deadbeef.notarealtoken"
PROJECT = "00000000-0000-0000-0000-000000000000"


@pytest.fixture
def bw(tmp_path):
    """Bitwarden module with both cache layers scoped to a tmp home."""
    import agent.secret_sources.bitwarden as module

    module._reset_cache_for_tests(tmp_path)
    try:
        yield module
    finally:
        module._reset_cache_for_tests(tmp_path)


def _pin_source(module, monkeypatch, values: dict) -> list[int]:
    """Make ``_run_bws_list`` return ``values``; return a one-slot pull counter."""
    pulls = [0]

    def fake_list(*_args, **_kwargs):
        pulls[0] += 1
        return dict(values), []

    monkeypatch.setattr(module, "_run_bws_list", fake_list)
    return pulls


def test_changed_value_is_not_visible_while_the_cache_entry_is_fresh(
    bw, tmp_path, monkeypatch
):
    values = {"MEDUSA_BASE_URL": "https://vitatide.ca"}
    pulls = _pin_source(bw, monkeypatch, values)

    first, _ = bw.fetch_bitwarden_secrets(
        access_token=TOKEN, project_id=PROJECT, home_path=tmp_path
    )
    assert first["MEDUSA_BASE_URL"] == "https://vitatide.ca"
    assert pulls[0] == 1

    # Someone edits the record in Bitwarden.
    values["MEDUSA_BASE_URL"] = "https://admin.vitatide.ca"

    second, _ = bw.fetch_bitwarden_secrets(
        access_token=TOKEN, project_id=PROJECT, home_path=tmp_path
    )
    # Still the old value, and no network call was made. This is the design,
    # not the bug — but it is why "add the record, watch a tick" proves nothing.
    assert second["MEDUSA_BASE_URL"] == "https://vitatide.ca"
    assert pulls[0] == 1


def test_deleting_the_disk_cache_forces_the_re_pull(bw, tmp_path, monkeypatch):
    values = {"MEDUSA_BASE_URL": "https://vitatide.ca"}
    pulls = _pin_source(bw, monkeypatch, values)

    bw.fetch_bitwarden_secrets(
        access_token=TOKEN, project_id=PROJECT, home_path=tmp_path
    )
    values["MEDUSA_BASE_URL"] = "https://admin.vitatide.ca"

    cache_file = bw._disk_cache_path(tmp_path)
    assert cache_file.exists(), "L2 cache should have been written by the first fetch"
    cache_file.unlink()

    fresh, _ = bw.fetch_bitwarden_secrets(
        access_token=TOKEN, project_id=PROJECT, home_path=tmp_path
    )
    # Deleting L2 must also drop L1, or the in-process cache keeps serving the
    # old value for up to cache_ttl_seconds and the deletion is a no-op — the
    # exact failure measured during the 2026-07-27 admin-origin cutover.
    assert fresh["MEDUSA_BASE_URL"] == "https://admin.vitatide.ca"
    assert pulls[0] == 2


def test_expired_ttl_re_pulls_without_any_operator_action(bw, tmp_path, monkeypatch):
    """The automatic path: no deletion, no restart — just wait out the TTL."""
    values = {"MEDUSA_BASE_URL": "https://vitatide.ca"}
    pulls = _pin_source(bw, monkeypatch, values)

    bw.fetch_bitwarden_secrets(
        access_token=TOKEN, project_id=PROJECT, home_path=tmp_path, cache_ttl_seconds=300
    )
    values["MEDUSA_BASE_URL"] = "https://admin.vitatide.ca"

    later, _ = bw.fetch_bitwarden_secrets(
        access_token=TOKEN, project_id=PROJECT, home_path=tmp_path, cache_ttl_seconds=0
    )
    assert later["MEDUSA_BASE_URL"] == "https://admin.vitatide.ca"
    assert pulls[0] == 2
