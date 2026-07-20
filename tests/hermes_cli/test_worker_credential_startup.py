from __future__ import annotations

import os


def test_early_startup_consumes_private_worker_handoff(monkeypatch, tmp_path):
    from hermes_cli import worker_credentials as wc

    (tmp_path / wc.MANIFEST_FILENAME).write_text(
        "version: 1\nprofiles:\n  releaser:\n    actions: []\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_PROFILE", "releaser")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-early-bootstrap")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "10")
    monkeypatch.setenv(wc.GITHUB_WRITE_HANDOFF_ENV, "private-handoff")
    monkeypatch.setenv("HERMES_WORKER_CREDENTIAL_UNKNOWN", "future-handoff")
    wc.reset_worker_credential_context_for_tests()

    try:
        from hermes_cli import main as main_mod

        main_mod._bootstrap_worker_credentials_early()

        assert not any(
            name.startswith(wc.PRIVATE_HANDOFF_PREFIX) for name in os.environ
        )
    finally:
        wc.reset_worker_credential_context_for_tests()
