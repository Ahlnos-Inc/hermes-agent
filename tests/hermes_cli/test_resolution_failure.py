from agent.runtime_target import RuntimeTargetError
from hermes_cli.auth import AuthError
from hermes_cli.resolution_failure import (
    classify_resolution_failure,
    format_resolution_failure,
)


def test_typed_temporary_auth_is_availability_and_fallback_eligible():
    failure = classify_resolution_failure(
        AuthError("ignored raw text", code="temporarily_unavailable")
    )

    assert failure.category == "availability"
    assert failure.retryable is True
    assert failure.fallback_eligible is True


def test_message_text_cannot_make_unknown_error_retryable_or_fallback_eligible():
    failure = classify_resolution_failure(RuntimeError("temporary auth failure"))

    assert failure.category == "internal"
    assert failure.retryable is False
    assert failure.fallback_eligible is False


def test_mismatch_is_configuration_and_never_fallback_eligible():
    failure = classify_resolution_failure(
        RuntimeTargetError("model/provider mismatch", code="model_provider_mismatch")
    )

    assert failure.category == "configuration"
    assert failure.fallback_eligible is False


def test_diagnostics_omit_secret_and_raw_exception_text():
    secret = "super-secret-token"
    failure = classify_resolution_failure(RuntimeError(f"raw failure {secret}"))

    diagnostic = format_resolution_failure(
        failure,
        surface="cli",
        phase="fatal",
        provider="anthropic",
        model=f"gpt-5\n{secret}",
    )

    assert secret not in diagnostic
    assert "raw failure" not in diagnostic
    assert "\n" not in diagnostic
