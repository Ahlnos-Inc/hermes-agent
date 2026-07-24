"""BUILD-661: reject nonexistent-profile assignees at creation.

A verifier-created card once named a nonexistent profile ``publisher`` as its
assignee. Nothing validated it, so the dispatcher bucketed it
``skipped_nonspawnable`` on every tick forever (0-spawn "stuck" escalation +
a card that could never run). These tests pin the guard:

* ``assignee_is_dispatchable`` — real profiles and the known non-profile lane
  allowlist (terminal-pull lanes + human gates) are dispatchable; an invented
  role name is not.
* ``create_task(validate_assignee=True)`` fails fast on an unknown assignee and
  stays fully backward-compatible when the flag is off (the default the ~40
  existing tests using synthetic assignees rely on).
"""

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import profiles


def _no_real_profiles(monkeypatch):
    """Make ``profile_exists`` false for everything (sandbox has no profile dirs
    anyway, but pin it so the test does not depend on the host's profiles)."""
    monkeypatch.setattr(profiles, "profile_exists", lambda name: False)


def test_dispatchable_accepts_real_profile(monkeypatch):
    monkeypatch.setattr(profiles, "profile_exists", lambda name: name == "coder")
    assert kb.assignee_is_dispatchable("coder") is True


def test_dispatchable_accepts_known_non_profile_lanes(monkeypatch):
    _no_real_profiles(monkeypatch)
    # Terminal-pull lanes and human-gate assignees are valid though they are
    # not profile directories (case-insensitive).
    for a in ("orion-cc", "orion-research", "nicholas", "Nolan"):
        assert kb.assignee_is_dispatchable(a) is True, a


@pytest.mark.real_assignee_guard
def test_dispatchable_rejects_invented_role(monkeypatch):
    _no_real_profiles(monkeypatch)
    assert kb.assignee_is_dispatchable("publisher") is False


def test_dispatchable_fails_open_when_profiles_unimportable(monkeypatch):
    # If the profiles module cannot be introspected, do not block creation —
    # preserve legacy behavior (mirrors has_spawnable_ready).
    def _boom(name):
        raise ImportError("profiles unavailable")

    monkeypatch.setattr(profiles, "profile_exists", _boom)
    assert kb.assignee_is_dispatchable("publisher") is True


@pytest.mark.real_assignee_guard
def test_create_task_validate_rejects_unknown_assignee(tmp_path, monkeypatch):
    _no_real_profiles(monkeypatch)
    db = tmp_path / "kanban.db"
    kb.init_db(db)
    with kb.connect(db) as conn:
        with pytest.raises(ValueError) as ei:
            kb.create_task(
                conn, title="x", assignee="publisher", validate_assignee=True
            )
    msg = str(ei.value)
    assert "publisher" in msg  # names the offending assignee


def test_create_task_validate_allows_lane_and_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(profiles, "profile_exists", lambda name: name == "coder")
    db = tmp_path / "kanban.db"
    kb.init_db(db)
    with kb.connect(db) as conn:
        tid_lane = kb.create_task(
            conn, title="lane", assignee="orion-cc", validate_assignee=True
        )
        tid_prof = kb.create_task(
            conn, title="prof", assignee="coder", validate_assignee=True
        )
    assert tid_lane and tid_prof


def test_create_task_default_does_not_validate(tmp_path, monkeypatch):
    # Backward compat: without the flag, an unknown assignee still creates
    # (every existing synthetic-assignee test depends on this).
    _no_real_profiles(monkeypatch)
    db = tmp_path / "kanban.db"
    kb.init_db(db)
    with kb.connect(db) as conn:
        tid = kb.create_task(conn, title="x", assignee="publisher")
    assert tid
