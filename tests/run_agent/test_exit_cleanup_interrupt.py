"""Tests for KeyboardInterrupt handling in exit cleanup paths.

``except Exception`` does not catch ``KeyboardInterrupt`` (which inherits
from ``BaseException``).  A second Ctrl+C during exit cleanup must not
abort remaining cleanup steps.  These tests exercise the actual production
code paths — not a copy of the try/except pattern.
"""

import logging
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _silence_expected_job_failure_log():
    """These tests deliberately drive ``run_job`` into its failure path — the
    mocked agent raises ``RuntimeError("boom")`` purely to reach the
    cleanup/``finally`` block. ``run_job`` logs that failure via the
    ``cron.scheduler`` logger at ERROR (``logger.exception``), which is
    intentional fixture output, NOT a real incident. Raise the logger level for
    the duration so the expected ERROR is never emitted to any shared handler
    and cannot be ingested by the sentinel ``errors.log`` scan as an
    uncategorized production incident (BUILD-622; hardens the source so the test
    no longer relies solely on the BUILD-614 ``errors.log`` path isolation)."""
    log = logging.getLogger("cron.scheduler")
    prev = log.level
    log.setLevel(logging.CRITICAL)
    try:
        yield
    finally:
        log.setLevel(prev)


@pytest.fixture(autouse=True)
def _mock_runtime_provider(monkeypatch):
    """run_job calls resolve_runtime_provider which can try real network
    auto-detection (~4s of socket timeouts in hermetic CI). Mock it out
    since these tests don't care about provider resolution — the agent
    is mocked too."""
    import hermes_cli.runtime_provider as rp
    def _fake_resolve(*args, **kwargs):
        return {
            "provider": "openrouter",
            "api_key": "test-key",
            "base_url": "https://openrouter.ai/api/v1",
            "model": "test/model",
            "api_mode": "chat_completions",
        }
    monkeypatch.setattr(rp, "resolve_runtime_provider", _fake_resolve)


class TestCronJobCleanup:
    """cron/scheduler.py — end_session + close in the finally block."""

    def test_keyboard_interrupt_in_end_session_does_not_skip_close(self):
        """If end_session raises KeyboardInterrupt, close() must still run."""
        mock_db = MagicMock()
        mock_db.end_session.side_effect = KeyboardInterrupt

        from cron import scheduler

        job = {
            "id": "test-job-1",
            "name": "test cleanup",
            "prompt": "hello",
            "schedule": "0 9 * * *",
            "model": "test/model",
        }

        with patch("hermes_state.SessionDB", return_value=mock_db), \
             patch.object(scheduler, "_build_job_prompt", return_value="hello"), \
             patch.object(scheduler, "_resolve_origin", return_value=None), \
             patch.object(scheduler, "_resolve_delivery_target", return_value=None), \
             patch("dotenv.load_dotenv", return_value=None), \
             patch("run_agent.AIAgent") as MockAgent:
            # Make the agent raise immediately so we hit the finally block
            MockAgent.return_value.run_conversation.side_effect = RuntimeError("boom")
            scheduler.run_job(job)

        mock_db.end_session.assert_called_once()
        mock_db.close.assert_called_once()

    def test_failed_job_records_error_and_preserves_registry(self):
        """BUILD-622 AC1/AC2: a job whose agent raises is recorded as a failed
        run carrying the exact error, ``run_job`` returns the failure contract
        (no propagation), and cleanup still runs so the scheduler state/registry
        is not left dangling or corrupted."""
        mock_db = MagicMock()

        from cron import scheduler

        job = {
            "id": "test-job-boom",
            "name": "test cleanup",
            "prompt": "hello",
            "schedule": "0 9 * * *",
            "model": "test/model",
        }

        with patch("hermes_state.SessionDB", return_value=mock_db), \
             patch.object(scheduler, "_build_job_prompt", return_value="hello"), \
             patch.object(scheduler, "_resolve_origin", return_value=None), \
             patch.object(scheduler, "_resolve_delivery_target", return_value=None), \
             patch("dotenv.load_dotenv", return_value=None), \
             patch("run_agent.AIAgent") as MockAgent:
            MockAgent.return_value.run_conversation.side_effect = RuntimeError("boom")
            result = scheduler.run_job(job)

        # run_job's failure contract: (success=False, output, response, error_msg).
        assert result[0] is False
        assert result[-1] == "RuntimeError: boom"
        assert "FAILED" in result[1]
        # Cleanup still ran → session/registry not left dangling.
        mock_db.end_session.assert_called_once()
        mock_db.close.assert_called_once()

    def test_keyboard_interrupt_in_close_does_not_propagate(self):
        """If close() raises KeyboardInterrupt, it must not escape run_job."""
        mock_db = MagicMock()
        mock_db.close.side_effect = KeyboardInterrupt

        from cron import scheduler

        job = {
            "id": "test-job-2",
            "name": "test close interrupt",
            "prompt": "hello",
            "schedule": "0 9 * * *",
            "model": "test/model",
        }

        with patch("hermes_state.SessionDB", return_value=mock_db), \
             patch.object(scheduler, "_build_job_prompt", return_value="hello"), \
             patch.object(scheduler, "_resolve_origin", return_value=None), \
             patch.object(scheduler, "_resolve_delivery_target", return_value=None), \
             patch("dotenv.load_dotenv", return_value=None), \
             patch("run_agent.AIAgent") as MockAgent:
            MockAgent.return_value.run_conversation.side_effect = RuntimeError("boom")
            # Must not raise
            scheduler.run_job(job)

        mock_db.end_session.assert_called_once()
        mock_db.close.assert_called_once()
