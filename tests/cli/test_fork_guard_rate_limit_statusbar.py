"""Merge-survival guard: status bar wires agent.get_rate_limit_state() through.

``HermesCLI._get_status_bar_snapshot`` calls ``agent.get_rate_limit_state()``
and, when the returned ``RateLimitState`` has data, formats it via
``agent.rate_limit_tracker.format_rate_limit_statusbar`` into the
``rate_limit_compact`` snapshot key, which ``_build_status_bar_text`` splices
into the rendered line (see cli.py ~4589-4598 and ~5075-5077).

``tests/cli/test_cli_status_bar.py``'s ``_attach_agent`` helper always sets
``get_rate_limit_state=lambda: None``, so that suite never exercises this
wiring — a fork could silently drop the rate-limit read or the
``rate_limit_compact`` splice and every existing test would stay green. This
guard attaches an agent whose ``get_rate_limit_state()`` returns a state WITH
data and asserts the formatted RPM/TPM fragment reaches the rendered text.
"""

from agent.rate_limit_tracker import (
    RateLimitBucket,
    RateLimitState,
    format_rate_limit_statusbar,
)
from tests.cli.test_cli_status_bar import _attach_agent, _make_cli


def _rate_limit_state_with_data() -> RateLimitState:
    return RateLimitState(
        requests_min=RateLimitBucket(limit=800, remaining=795),
        tokens_min=RateLimitBucket(limit=100_000, remaining=95_000),
        captured_at=1.0,
    )


class TestForkGuardRateLimitStatusBar:
    def test_snapshot_carries_rate_limit_compact_when_state_has_data(self):
        cli_obj = _attach_agent(
            _make_cli(),
            prompt_tokens=10_230,
            completion_tokens=2_220,
            total_tokens=12_450,
            api_calls=7,
            context_tokens=12_450,
            context_length=200_000,
        )
        state = _rate_limit_state_with_data()
        cli_obj.agent.get_rate_limit_state = lambda: state

        snapshot = cli_obj._get_status_bar_snapshot()

        expected = format_rate_limit_statusbar(state)
        assert expected  # sanity: the fixture actually has data
        assert snapshot["rate_limit_compact"] == expected
        assert "RPM 795/800" in snapshot["rate_limit_compact"]
        assert "TPM" in snapshot["rate_limit_compact"]

    def test_build_status_bar_text_includes_rate_limit_fragment(self):
        cli_obj = _attach_agent(
            _make_cli(),
            prompt_tokens=10_230,
            completion_tokens=2_220,
            total_tokens=12_450,
            api_calls=7,
            context_tokens=12_450,
            context_length=200_000,
        )
        state = _rate_limit_state_with_data()
        cli_obj.agent.get_rate_limit_state = lambda: state

        text = cli_obj._build_status_bar_text(width=160)

        assert format_rate_limit_statusbar(state) in text
