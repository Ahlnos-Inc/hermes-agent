"""Auto-generate short session titles from the first user/assistant exchange.

Runs asynchronously after the first response is delivered so it never
adds latency to the user-facing reply.
"""

import logging
import re
import threading
from typing import Any, Callable, Optional, Union

from agent.auxiliary_client import call_llm

logger = logging.getLogger(__name__)

# Callback signature: (task_name, exception) -> None. Used to surface
# auxiliary failures to the user through AIAgent._emit_auxiliary_failure
# so silent-drops (e.g. OpenRouter 402 exhausting the fallback chain)
# become visible instead of piling up as NULL session titles.
FailureCallback = Callable[[str, BaseException], None]
TitleCallback = Callable[[str], None]

_TITLE_PROMPT = (
    "Generate a short, descriptive title (3-7 words) for a conversation that starts with the "
    "following exchange. The title should capture the main topic or intent. "
    "Return ONLY the title text, nothing else. No quotes, no punctuation at the end, no prefixes."
)

# ── Default caps for title-generation input ──────────────────────────
_DEFAULT_MAX_INPUT_CHARS = 2000

# Pattern to detect data URIs and long base64 strings so they never leak
# into an auxiliary provider prompt.
_DATA_URI_RE = re.compile(r"data:[^;]*;base64,[A-Za-z0-9+/=]{200,}")
_LONG_BASE64_RE = re.compile(r"[A-Za-z0-9+/=]{300,}")


def _normalize_text_content(raw: Any) -> str:
    """Extract plain text from user/assistant content for title generation.

    Handles str, list (multimodal content array), and dict (single block).
    - Concatenates text parts only.
    - Replaces image/file/audio/non-text blocks with compact markers.
    - Strips data URIs and long base64 strings so they never leak into
      an auxiliary provider prompt.

    Returns a plain string safe for title-generation input.
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        return _redact_sensitive_patterns(raw)
    return _flatten_content_blocks(raw)


def _flatten_content_blocks(raw: Any) -> str:
    """Walk a multimodal content structure and return text-only concatenation."""
    if isinstance(raw, str):
        return _redact_sensitive_patterns(raw)
    if isinstance(raw, dict):
        return _flatten_single_block(raw)
    if isinstance(raw, list):
        parts: list[str] = []
        for item in raw:
            if isinstance(item, str):
                parts.append(_redact_sensitive_patterns(item))
            elif isinstance(item, dict):
                parts.append(_flatten_single_block(item))
        return " ".join(part for part in parts if part)
    # Fallback for unexpected types — don't stringify arbitrary objects
    return ""


# Map of block types → compact placeholder when the block carries no
# extractable text.  Non-text blocks get a short marker; text blocks
# contribute their text.
_BLOCK_TYPE_MARKERS: dict[str, str] = {
    "image_url": "[image attached]",
    "image": "[image attached]",
    "video_url": "[video attached]",
    "video": "[video attached]",
    "audio": "[audio attached]",
    "file": "[file attached]",
}


def _flatten_single_block(block: dict) -> str:
    """Extract usable text from a single multimodal content block."""
    block_type = block.get("type", "")
    if block_type == "text":
        text = block.get("text", "")
        if isinstance(text, str):
            return _redact_sensitive_patterns(text)
        return ""
    if block_type in _BLOCK_TYPE_MARKERS:
        return _BLOCK_TYPE_MARKERS[block_type]
    # Unknown block — return empty, don't risk stringifying raw dicts
    return ""


def _redact_sensitive_patterns(text: str) -> str:
    """Strip data URIs and long base64 runs from *text*.

    These patterns should never appear in the source text (they're a
    defence-in-depth layer in case a caller accidentally passes raw
    multimodal JSON through a string path).
    """
    text = _DATA_URI_RE.sub("[data URI redacted]", text)
    text = _LONG_BASE64_RE.sub("[base64 redacted]", text)
    return text


def _get_max_input_chars() -> int:
    """Read ``auxiliary.title_generation.max_input_chars`` from config.

    Falls back to :data:`_DEFAULT_MAX_INPUT_CHARS` when the config key is
    absent, unparseable, or zero/negative.
    """
    try:
        from hermes_cli.config import load_config
        config = load_config()
        aux = config.get("auxiliary", {}) if isinstance(config, dict) else {}
        task_cfg = aux.get("title_generation", {}) if isinstance(aux, dict) else {}
        if isinstance(task_cfg, dict):
            val = task_cfg.get("max_input_chars")
            if val is not None:
                parsed = int(val)
                if parsed > 0:
                    return parsed
    except Exception:
        pass
    return _DEFAULT_MAX_INPUT_CHARS


def _bound_input_text(text: str) -> str:
    """Apply the configurable character cap to title-generation input."""
    max_chars = _get_max_input_chars()
    if len(text) <= max_chars:
        return text
    # Truncate at a word boundary when possible
    snippet = text[:max_chars]
    last_space = snippet.rfind(" ")
    if last_space > max_chars // 2:
        snippet = snippet[:last_space]
    return snippet


def generate_title(
    user_message: Any,
    assistant_response: Any,
    timeout: float = 30.0,
    failure_callback: Optional[FailureCallback] = None,
    main_runtime: dict = None,
) -> Optional[str]:
    """Generate a session title from the first exchange.

    Uses the main runtime's model when available, falling back to the
    auxiliary LLM client (cheapest/fastest available model).
    Returns the title string or None on failure.

    ``failure_callback`` is invoked with ``(task, exception)`` when the
    auxiliary call raises — the caller typically wires this to
    ``AIAgent._emit_auxiliary_failure`` so the user sees a warning instead
    of silently accumulating untitled sessions.
    """
    # ── Normalize & bound input ─────────────────────────────────────
    user_text = _normalize_text_content(user_message) if user_message else ""
    assistant_text = _normalize_text_content(assistant_response) if assistant_response else ""

    max_chars = _get_max_input_chars()
    # Per-field soft truncation so one giant field doesn't starve the other
    per_field_limit = max(200, max_chars // 2)
    if len(user_text) > per_field_limit:
        user_text = user_text[:per_field_limit]
    if len(assistant_text) > per_field_limit:
        assistant_text = assistant_text[:per_field_limit]

    input_text = f"User: {user_text}\n\nAssistant: {assistant_text}"
    if len(input_text) > max_chars:
        input_text = _bound_input_text(input_text)

    # PII-safe debug log: size and any skip reason, never raw prompt text
    logger.debug(
        "Title generation input: user_chars=%d assistant_chars=%d total_chars=%d cap=%d",
        len(user_text), len(assistant_text), len(input_text), max_chars,
    )

    # ── Guard: skip when no usable text ─────────────────────────────
    if not user_text.strip() and not assistant_text.strip():
        logger.info("Title generation skipped: no usable text after normalization")
        return None

    messages = [
        {"role": "system", "content": _TITLE_PROMPT},
        {"role": "user", "content": input_text},
    ]

    try:
        response = call_llm(
            task="title_generation",
            messages=messages,
            max_tokens=500,
            temperature=0.3,
            timeout=timeout,
            main_runtime=main_runtime,
        )
        title = (response.choices[0].message.content or "").strip()
        # Clean up: remove quotes, trailing punctuation, prefixes like "Title: "
        title = title.strip('"\'')
        if title.lower().startswith("title:"):
            title = title[6:].strip()
        # Enforce reasonable length
        if len(title) > 80:
            title = title[:77] + "..."
        return title if title else None
    except Exception as e:
        # Log at WARNING so this shows up in agent.log without debug mode.
        # Full detail at debug level for operators who need the stack.
        logger.warning("Title generation failed: %s", e)
        logger.debug("Title generation traceback", exc_info=True)
        if failure_callback is not None:
            try:
                failure_callback("title generation", e)
            except Exception:
                logger.debug("Title generation failure_callback raised", exc_info=True)
        return None


def auto_title_session(
    session_db,
    session_id: str,
    user_message: Any,
    assistant_response: Any,
    failure_callback: Optional[FailureCallback] = None,
    main_runtime: dict = None,
    title_callback: Optional[TitleCallback] = None,
) -> None:
    """Generate and set a session title if one doesn't already exist.

    Called in a background thread after the first exchange completes.
    Silently skips if:
    - session_db is None
    - session already has a title (user-set or previously auto-generated)
    - title generation fails
    """
    if not session_db or not session_id:
        return

    # Check if title already exists (user may have set one via /title before first response)
    try:
        existing = session_db.get_session_title(session_id)
        if existing:
            return
    except Exception:
        return

    title = generate_title(
        user_message, assistant_response, failure_callback=failure_callback, main_runtime=main_runtime
    )
    if not title:
        return

    try:
        session_db.set_session_title(session_id, title)
        logger.debug("Auto-generated session title: %s", title)
        if title_callback is not None:
            try:
                title_callback(title)
            except Exception:
                logger.debug("Auto-title callback failed", exc_info=True)
    except Exception as e:
        logger.debug("Failed to set auto-generated title: %s", e)


def maybe_auto_title(
    session_db,
    session_id: str,
    user_message: Any,
    assistant_response: Any,
    conversation_history: list,
    failure_callback: Optional[FailureCallback] = None,
    main_runtime: dict = None,
    title_callback: Optional[TitleCallback] = None,
) -> None:
    """Fire-and-forget title generation after the first exchange.

    Only generates a title when:
    - This appears to be the first user→assistant exchange
    - No title is already set
    """
    if not session_db or not session_id or not user_message or not assistant_response:
        return

    # Count user messages in history to detect first exchange.
    # conversation_history includes the exchange that just happened,
    # so for a first exchange we expect exactly 1 user message
    # (or 2 counting system). Be generous: generate on first 2 exchanges.
    user_msg_count = sum(1 for m in (conversation_history or []) if m.get("role") == "user")
    if user_msg_count > 2:
        return

    thread = threading.Thread(
        target=auto_title_session,
        args=(session_db, session_id, user_message, assistant_response),
        kwargs={
            "failure_callback": failure_callback,
            "main_runtime": main_runtime,
            "title_callback": title_callback,
        },
        daemon=True,
        name="auto-title",
    )
    thread.start()
