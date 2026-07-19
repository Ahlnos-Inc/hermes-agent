"""Regression tests for issue #47237.

When the gateway persists a user message after a transient provider
failure (429/timeout/auth error), subsequent retries of the same
Telegram message must not stack duplicate user turns in the transcript.
The dedupe guard checks has_platform_message_id before persisting.
"""

from gateway.session import SessionStore
from hermes_state import SessionDB


class TestHasPlatformMessageId:
    """SessionDB.has_platform_message_id and SessionStore wrapper."""

    def _make_db(self, tmp_path):
        db = SessionDB(tmp_path / "state.db")
        db.create_session("s1", "cli")
        return db

    def test_returns_false_when_not_present(self, tmp_path):
        db = self._make_db(tmp_path)
        assert not db.has_platform_message_id("s1", "msg-999")

    def test_returns_true_after_append(self, tmp_path):
        db = self._make_db(tmp_path)
        db.append_message(
            session_id="s1",
            role="user",
            content="hello",
            platform_message_id="msg-123",
        )
        assert db.has_platform_message_id("s1", "msg-123")

    def test_returns_false_for_different_session(self, tmp_path):
        db = self._make_db(tmp_path)
        db.create_session("s2", "cli")
        db.append_message(
            session_id="s1",
            role="user",
            content="hello",
            platform_message_id="msg-123",
        )
        assert not db.has_platform_message_id("s2", "msg-123")

    def test_session_store_wrapper_returns_false_without_db(self, tmp_path):
        store = SessionStore.__new__(SessionStore)
        store._db = None
        assert not store.has_platform_message_id("s1", "msg-123")

    def test_session_store_wrapper_proxies_to_db(self, tmp_path):
        db = self._make_db(tmp_path)
        db.append_message(
            session_id="s1",
            role="user",
            content="hello",
            platform_message_id="msg-456",
        )
        store = SessionStore.__new__(SessionStore)
        store._db = db
        assert store.has_platform_message_id("s1", "msg-456")
        assert not store.has_platform_message_id("s1", "msg-000")


class TestDedupeOnTransientFailure:
    """The gateway's transient-failure path must not persist duplicates."""

    @staticmethod
    def _make_db(tmp_path):
        db = SessionDB(tmp_path / "state.db")
        db.create_session("s1", "cli")
        return db

    def test_duplicate_message_id_skipped(self, tmp_path):
        """When has_platform_message_id returns True, the append is skipped."""
        db = self._make_db(tmp_path)
        db.append_message(
            session_id="s1",
            role="user",
            content="hello",
            platform_message_id="msg-789",
        )
        store = SessionStore.__new__(SessionStore)
        store._db = db

        # Simulate a second attempt to persist the same message
        assert store.has_platform_message_id("s1", "msg-789")
        # The gateway code checks this before calling append_to_transcript,
        # so the second append should never fire.

    def test_different_message_id_persists(self, tmp_path):
        """A new message_id should always be persisted."""
        db = self._make_db(tmp_path)
        db.append_message(
            session_id="s1",
            role="user",
            content="first",
            platform_message_id="msg-001",
        )
        assert not db.has_platform_message_id("s1", "msg-002")
        db.append_message(
            session_id="s1",
            role="user",
            content="second",
            platform_message_id="msg-002",
        )
        assert db.has_platform_message_id("s1", "msg-002")


class TestStampPlatformMessageId:
    """BUILD-532: post-turn backfill of the platform id onto agent-persisted
    user rows, so the pre-dispatch redelivery guard has a key to match."""

    def _make_db(self, tmp_path):
        db = SessionDB(tmp_path / "state.db")
        db.create_session("s1", "telegram")
        return db

    def test_stamps_latest_empty_user_row(self, tmp_path):
        db = self._make_db(tmp_path)
        # Agent runtime persists the user turn without a platform id.
        db.append_message(session_id="s1", role="user", content="hi")
        db.append_message(session_id="s1", role="assistant", content="yo")
        assert db.stamp_platform_message_id("s1", "msg-1") is True
        assert db.has_platform_message_id("s1", "msg-1")

    def test_never_overwrites_existing_id(self, tmp_path):
        db = self._make_db(tmp_path)
        db.append_message(
            session_id="s1", role="user", content="hi",
            platform_message_id="msg-1",
        )
        assert db.stamp_platform_message_id("s1", "msg-2") is False
        assert db.has_platform_message_id("s1", "msg-1")
        assert not db.has_platform_message_id("s1", "msg-2")

    def test_only_touches_user_rows(self, tmp_path):
        db = self._make_db(tmp_path)
        db.append_message(session_id="s1", role="assistant", content="only")
        assert db.stamp_platform_message_id("s1", "msg-1") is False

    def test_enables_redelivery_guard_round_trip(self, tmp_path):
        # The incident shape: turn persisted without id, redelivery arrives.
        db = self._make_db(tmp_path)
        db.append_message(session_id="s1", role="user", content="do the thing")
        assert not db.has_platform_message_id("s1", "msg-7")  # guard misses
        db.stamp_platform_message_id("s1", "msg-7")           # turn-end stamp
        assert db.has_platform_message_id("s1", "msg-7")      # guard now hits
