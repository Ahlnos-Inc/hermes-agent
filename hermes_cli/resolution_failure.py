"""Typed, secret-safe provider-resolution failure classification."""

from __future__ import annotations

from dataclasses import dataclass
import traceback
from typing import Iterable, cast

from agent.runtime_target import ClaudeRoutePolicyError, RuntimeTargetError
from hermes_cli.auth import AuthError, CODEX_RATE_LIMITED_CODE


_MAX_IDENTIFIER_LENGTH = 96
_SAFE_CATEGORIES = frozenset({"configuration", "auth", "availability", "policy", "internal"})


@dataclass(frozen=True)
class ResolutionFailure:
    code: str
    category: str
    retryable: bool
    fallback_eligible: bool
    public_message: str | None = None
    frames: tuple[tuple[str, int, str], ...] = ()


def _safe_identifier(value: object, *, redact_secretish: bool = True) -> str:
    """Scrub a value for log output.

    ``redact_secretish`` drops anything that *names* a credential concept. It
    belongs on free-form values (a provider/model string or an exception's
    public message) that can carry caller input. It must stay OFF for values
    drawn from our own closed vocabulary — many real ``AuthError`` codes are
    themselves credential-shaped (``codex_auth_missing_access_token``,
    ``qwen_access_token_missing``), and redacting those would erase exactly
    the signal this line exists to carry.
    """

    text = str(value or "").replace("\r", " ").replace("\n", " ").replace("\t", " ")
    cleaned = "".join(char for char in text if char.isprintable())[:_MAX_IDENTIFIER_LENGTH]
    if redact_secretish and any(
        marker in cleaned.lower()
        for marker in ("secret", "token", "api_key", "authorization")
    ):
        return "<redacted>"
    return cleaned


def _safe_frames(error: BaseException, *, limit: int = 3) -> tuple[tuple[str, int, str], ...]:
    return tuple(
        (frame.filename.rsplit("/", 1)[-1], frame.lineno or 0, frame.name)
        for frame in traceback.extract_tb(error.__traceback__)[-limit:]
    )


def classify_resolution_failure(error: BaseException) -> ResolutionFailure:
    """Classify typed errors without inspecting raw exception messages."""

    if isinstance(error, RuntimeTargetError):
        target_error = cast(RuntimeTargetError, error)
        return ResolutionFailure(
            code=target_error.code,
            category=target_error.category,
            retryable=target_error.retryable,
            fallback_eligible=target_error.fallback_eligible,
            public_message=_safe_identifier(target_error),
            frames=_safe_frames(error),
        )

    if isinstance(error, ClaudeRoutePolicyError):
        # BUILD-573 deliberately degrades a governed Claude route to the
        # configured non-Claude fallback chain: the route is permanently
        # unavailable under ``max_only``, not permanently wrong. It is a policy
        # rejection that IS fallback-eligible — the one exception to the
        # "policy is terminal" rule applied to AuthError below.
        return ResolutionFailure(
            code="claude_route_policy",
            category="policy",
            retryable=False,
            fallback_eligible=True,
            public_message=_safe_identifier(error),
            frames=_safe_frames(error),
        )

    if isinstance(error, AuthError):
        auth_error = cast(AuthError, error)
        category = auth_error.category
        if category not in _SAFE_CATEGORIES:
            if auth_error.code == CODEX_RATE_LIMITED_CODE or auth_error.code == "temporarily_unavailable":
                category = "availability"
            elif auth_error.code == "invalid_provider":
                category = "configuration"
            else:
                category = "auth"
        retryable = auth_error.retryable
        if retryable is None:
            retryable = category == "availability"
        return ResolutionFailure(
            code=_safe_identifier(
                auth_error.code or "auth_error", redact_secretish=False
            ) or "auth_error",
            category=category,
            retryable=bool(retryable),
            # Preserve legacy AuthError fallback eligibility, including
            # invalid_provider, until BUILD-597 owns the migration.
            fallback_eligible=category not in {"policy", "internal"},
            public_message=_safe_identifier(auth_error.public_message) or None,
            frames=_safe_frames(error),
        )

    return ResolutionFailure(
        code="internal_error",
        category="internal",
        retryable=False,
        fallback_eligible=False,
        frames=_safe_frames(error),
    )


def format_resolution_failure(
    failure: ResolutionFailure,
    *,
    surface: str,
    phase: str,
    provider: object = "",
    model: object = "",
) -> str:
    """Return bounded structured diagnostics without exception text or secrets."""

    fields: Iterable[tuple[str, object]] = (
        ("surface", surface),
        ("phase", phase),
        ("provider", provider),
        ("model", model),
        ("category", failure.category),
        ("code", failure.code),
        ("retryable", failure.retryable),
        ("fallback_eligible", failure.fallback_eligible),
    )
    # Only ``provider``/``model`` can carry caller-supplied text; the rest come
    # from our own vocabulary and must not be word-redacted (see _safe_identifier).
    rendered = " ".join(
        f"{name}={_safe_identifier(value, redact_secretish=name in {'provider', 'model'})}"
        for name, value in fields
    )
    if failure.frames:
        rendered += " frames=" + ",".join(
            f"{filename}:{line}:{function}" for filename, line, function in failure.frames
        )
    return rendered
