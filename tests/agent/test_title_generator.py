"""Tests for agent.title_generator — auto-generated session titles."""

from unittest.mock import MagicMock, patch


from agent.title_generator import (
    generate_title,
    auto_title_session,
    maybe_auto_title,
)


class TestGenerateTitle:
    """Unit tests for generate_title()."""

    def test_returns_title_on_success(self):
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "Debugging Python Import Errors"

        with patch("agent.title_generator.call_llm", return_value=mock_response):
            title = generate_title("help me fix this import", "Sure, let me check...")
            assert title == "Debugging Python Import Errors"

    def test_strips_quotes(self):
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = '"Setting Up Docker Environment"'

        with patch("agent.title_generator.call_llm", return_value=mock_response):
            title = generate_title("how do I set up docker", "First install...")
            assert title == "Setting Up Docker Environment"

    def test_strips_title_prefix(self):
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "Title: Kubernetes Pod Debugging"

        with patch("agent.title_generator.call_llm", return_value=mock_response):
            title = generate_title("my pod keeps crashing", "Let me look...")
            assert title == "Kubernetes Pod Debugging"

    def test_truncates_long_titles(self):
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "A" * 100

        with patch("agent.title_generator.call_llm", return_value=mock_response):
            title = generate_title("question", "answer")
            assert len(title) == 80
            assert title.endswith("...")

    def test_returns_none_on_empty_response(self):
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = ""

        with patch("agent.title_generator.call_llm", return_value=mock_response):
            assert generate_title("question", "answer") is None

    def test_returns_none_on_exception(self):
        with patch("agent.title_generator.call_llm", side_effect=RuntimeError("no provider")):
            assert generate_title("question", "answer") is None

    def test_invokes_failure_callback_on_exception(self):
        """failure_callback must fire so the user sees a warning (issue #15775)."""
        captured = []

        def _cb(task, exc):
            captured.append((task, exc))

        exc = RuntimeError("openrouter 402: credits exhausted")
        with patch("agent.title_generator.call_llm", side_effect=exc):
            result = generate_title("question", "answer", failure_callback=_cb)

        assert result is None
        assert len(captured) == 1
        assert captured[0][0] == "title generation"
        assert captured[0][1] is exc

    def test_failure_callback_errors_are_swallowed(self):
        """A broken callback must not crash title generation."""

        def _bad_cb(task, exc):
            raise ValueError("callback bug")

        with patch("agent.title_generator.call_llm", side_effect=RuntimeError("nope")):
            # Should return None without re-raising the callback error
            assert generate_title("q", "a", failure_callback=_bad_cb) is None

    def test_no_callback_matches_legacy_behavior(self):
        """Omitting failure_callback preserves the silent-None return."""
        with patch("agent.title_generator.call_llm", side_effect=RuntimeError("nope")):
            assert generate_title("q", "a") is None

    def test_truncates_long_messages(self):
        """Long user/assistant messages should be truncated in the LLM request."""
        captured_kwargs = {}

        def mock_call_llm(**kwargs):
            captured_kwargs.update(kwargs)
            resp = MagicMock()
            resp.choices = [MagicMock()]
            resp.choices[0].message.content = "Short Title"
            return resp

        with patch("agent.title_generator.call_llm", side_effect=mock_call_llm):
            generate_title("x" * 1000, "y" * 1000)

        # The user content in the messages should be truncated
        user_content = captured_kwargs["messages"][1]["content"]
        assert len(user_content) < 1100  # 500 + 500 + formatting


class TestAutoTitleSession:
    """Tests for auto_title_session() — the sync worker function."""

    def test_skips_if_no_session_db(self):
        auto_title_session(None, "sess-1", "hi", "hello")  # should not crash

    def test_skips_if_title_exists(self):
        db = MagicMock()
        db.get_session_title.return_value = "Existing Title"

        with patch("agent.title_generator.generate_title") as gen:
            auto_title_session(db, "sess-1", "hi", "hello")
            gen.assert_not_called()

    def test_generates_and_sets_title(self):
        db = MagicMock()
        db.get_session_title.return_value = None

        with patch("agent.title_generator.generate_title", return_value="New Title"):
            auto_title_session(db, "sess-1", "hi", "hello")
            db.set_session_title.assert_called_once_with("sess-1", "New Title")

    def test_invokes_title_callback_after_setting_title(self):
        db = MagicMock()
        db.get_session_title.return_value = None
        seen = []
        with patch("agent.title_generator.generate_title", return_value="Readable Session"):
            auto_title_session(
                db,
                "sess-1",
                "hello",
                "hi there",
                title_callback=seen.append,
            )
        db.set_session_title.assert_called_once_with("sess-1", "Readable Session")
        assert seen == ["Readable Session"]

    def test_skips_if_generation_fails(self):
        db = MagicMock()
        db.get_session_title.return_value = None

        with patch("agent.title_generator.generate_title", return_value=None):
            auto_title_session(db, "sess-1", "hi", "hello")
            db.set_session_title.assert_not_called()


class TestMaybeAutoTitle:
    """Tests for maybe_auto_title() — the fire-and-forget entry point."""

    def test_skips_if_not_first_exchange(self):
        """Should not fire for conversations with more than 2 user messages."""
        db = MagicMock()
        history = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "response 1"},
            {"role": "user", "content": "second"},
            {"role": "assistant", "content": "response 2"},
            {"role": "user", "content": "third"},
            {"role": "assistant", "content": "response 3"},
        ]

        with patch("agent.title_generator.auto_title_session") as mock_auto:
            maybe_auto_title(db, "sess-1", "third", "response 3", history)
            # Wait briefly for any thread to start
            import time
            time.sleep(0.1)
            mock_auto.assert_not_called()

    def test_fires_on_first_exchange(self):
        """Should fire a background thread for the first exchange."""
        db = MagicMock()
        db.get_session_title.return_value = None
        history = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]

        with patch("agent.title_generator.auto_title_session") as mock_auto:
            maybe_auto_title(db, "sess-1", "hello", "hi there", history)
            # Wait for the daemon thread to complete
            import time
            time.sleep(0.3)
            mock_auto.assert_called_once_with(
                db,
                "sess-1",
                "hello",
                "hi there",
                failure_callback=None,
                main_runtime=None,
                title_callback=None,
            )

    def test_forwards_failure_callback_to_worker(self):
        """maybe_auto_title must forward failure_callback into the thread."""
        db = MagicMock()
        db.get_session_title.return_value = None
        history = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]

        def _cb(task, exc):
            pass

        with patch("agent.title_generator.auto_title_session") as mock_auto:
            maybe_auto_title(db, "sess-1", "hello", "hi there", history, failure_callback=_cb)
            import time
            time.sleep(0.3)
            mock_auto.assert_called_once_with(
                db,
                "sess-1",
                "hello",
                "hi there",
                failure_callback=_cb,
                main_runtime=None,
                title_callback=None,
            )

    def test_skips_if_no_response(self):
        db = MagicMock()
        maybe_auto_title(db, "sess-1", "hello", "", [])  # empty response

    def test_skips_if_no_session_db(self):
        maybe_auto_title(None, "sess-1", "hello", "response", [])  # no db


# ── Multimodal-safe normalization tests ──────────────────────────────

class TestNormalizeTextContent:
    """Tests for _normalize_text_content() — multimodal input safety."""

    def test_string_passthrough(self):
        from agent.title_generator import _normalize_text_content
        result = _normalize_text_content("hello world")
        assert result == "hello world"

    def test_none_returns_empty(self):
        from agent.title_generator import _normalize_text_content
        assert _normalize_text_content(None) == ""

    def test_list_of_text_blocks_concatenated(self):
        from agent.title_generator import _normalize_text_content
        content = [
            {"type": "text", "text": "What is this image?"},
            {"type": "text", "text": "I need help."},
        ]
        result = _normalize_text_content(content)
        assert "What is this image?" in result
        assert "I need help." in result

    def test_image_block_replaced_with_marker(self):
        from agent.title_generator import _normalize_text_content
        content = [
            {"type": "text", "text": "Look at this:"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="}},
        ]
        result = _normalize_text_content(content)
        assert "Look at this:" in result
        assert "[image attached]" in result
        # The data URI must not appear
        assert "base64" not in result
        assert "iVBOR" not in result

    def test_data_uri_redacted_in_string_input(self):
        from agent.title_generator import _normalize_text_content
        # Base64 payload must be >=200 chars to match _DATA_URI_RE
        long_payload = "A" * 250
        nasty = f"Check this: data:image/png;base64,{long_payload}=="
        result = _normalize_text_content(nasty)
        assert "[data URI redacted]" in result
        assert long_payload not in result

    def test_long_base64_redacted(self):
        from agent.title_generator import _normalize_text_content
        # 300+ chars of base64-looking text
        fake_b64 = "AAAA" * 80  # 320 chars
        result = _normalize_text_content(f"prefix {fake_b64} suffix")
        assert "prefix" in result
        assert "suffix" in result
        assert "[base64 redacted]" in result

    def test_mixed_multimodal_list(self):
        from agent.title_generator import _normalize_text_content
        content = [
            {"type": "text", "text": "Hello"},
            {"type": "image_url", "image_url": {"url": "https://example.com/img.png"}},
            {"type": "text", "text": "World"},
            {"type": "video", "source": "..."},
        ]
        result = _normalize_text_content(content)
        assert "Hello" in result
        assert "World" in result
        assert "[image attached]" in result
        assert "[video attached]" in result

    def test_unknown_block_type_ignored(self):
        from agent.title_generator import _normalize_text_content
        content = [
            {"type": "text", "text": "Hi"},
            {"type": "weird_custom_block", "payload": "sensitive stuff"},
        ]
        result = _normalize_text_content(content)
        assert "Hi" in result
        assert "sensitive stuff" not in result  # Never stringify raw dicts

    def test_single_dict_block(self):
        from agent.title_generator import _normalize_text_content
        result = _normalize_text_content({"type": "text", "text": "single block"})
        assert result == "single block"

    def test_empty_list_returns_empty(self):
        from agent.title_generator import _normalize_text_content
        assert _normalize_text_content([]) == ""

    def test_non_string_non_list_non_dict_returns_empty(self):
        from agent.title_generator import _normalize_text_content
        # Should never crash on unexpected types
        assert _normalize_text_content(42) == ""


class TestBoundedTitleInput:
    """Tests for configurable caps and input bounding."""

    def test_normal_text_title_still_works(self):
        """Smoke test: normal string input still produces a title."""
        from agent.title_generator import generate_title
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "A Normal Title"

        with patch("agent.title_generator.call_llm", return_value=mock_response):
            title = generate_title("help me", "sure thing")
            assert title == "A Normal Title"

    def test_multimodal_input_normalized_before_llm_call(self):
        """Image content must not reach the provider."""
        from agent.title_generator import generate_title

        captured_messages = []

        def capture_call(**kwargs):
            captured_messages.append(kwargs["messages"])
            resp = MagicMock()
            resp.choices = [MagicMock()]
            resp.choices[0].message.content = "Image Discussion"
            return resp

        multimodal_user = [
            {"type": "text", "text": "What is in this image?"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="}},
        ]
        with patch("agent.title_generator.call_llm", side_effect=capture_call):
            generate_title(multimodal_user, "It's a red pixel.")

        assert len(captured_messages) == 1
        llm_input = captured_messages[0][1]["content"]
        # Image content must be a marker, not the raw multimodal dict or base64
        assert "[image attached]" in llm_input
        assert "base64" not in llm_input
        assert "iVBOR" not in llm_input
        # Text content preserved
        assert "What is in this image?" in llm_input
        assert "red pixel" in llm_input

    def test_input_bounded_by_config_cap(self):
        """Input should be truncated when it exceeds max_input_chars."""
        from agent.title_generator import generate_title

        captured_messages = []

        def capture_call(**kwargs):
            captured_messages.append(kwargs["messages"])
            resp = MagicMock()
            resp.choices = [MagicMock()]
            resp.choices[0].message.content = "Short"
            return resp

        # Set a low cap in config
        mock_config = {"auxiliary": {"title_generation": {"max_input_chars": 100}}}
        with patch("agent.title_generator._get_max_input_chars", return_value=100):
            with patch("agent.title_generator.call_llm", side_effect=capture_call):
                generate_title("x" * 3000, "y" * 3000)

        assert len(captured_messages) == 1
        llm_input = captured_messages[0][1]["content"]
        # Input must be within the cap (plus some formatting overhead)
        assert len(llm_input) <= 200  # generous upper bound

    def test_image_only_input_still_calls_with_markers(self):
        """Pure image input should generate a title from markers (not skip)."""
        from agent.title_generator import generate_title
        multimodal_only = [
            {"type": "image_url", "image_url": {"url": "https://example.com/img.png"}},
        ]
        with patch("agent.title_generator.call_llm") as mock_call:
            mock_response = MagicMock()
            mock_response.choices = [MagicMock()]
            mock_response.choices[0].message.content = "Image Discussion"
            mock_call.return_value = mock_response
            result = generate_title(multimodal_only, multimodal_only)
            # Call should proceed with markers, not raw data
            mock_call.assert_called_once()
            messages = mock_call.call_args[1]["messages"]
            llm_input = messages[1]["content"]
            assert "[image attached]" in llm_input
            assert "https://example.com" not in llm_input  # URL not leaked
            assert result == "Image Discussion"

    def test_debug_logging_includes_input_sizes(self, caplog):
        """Debug logging should show char counts but never raw prompt text."""
        from agent.title_generator import generate_title
        import logging

        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "Logged Title"

        with patch("agent.title_generator.call_llm", return_value=mock_response):
            with caplog.at_level(logging.DEBUG, logger="agent.title_generator"):
                generate_title("hello", "world")

        # Find the debug log entry
        debug_logs = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
        size_log = [m for m in debug_logs if "user_chars=" in m]
        assert len(size_log) == 1
        # Must contain size info
        assert "user_chars=5" in size_log[0] or "user_chars=5" in size_log[0]
        # Must NOT contain the raw prompt text
        assert "hello" not in size_log[0]
