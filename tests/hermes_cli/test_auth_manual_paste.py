"""Surviving xAI OAuth coverage after the upstream device-code-only rewrite.

Upstream commit 5ef0b8acb (``feat(auth): make xAI Grok OAuth device-code-only,
drop loopback login``) removed the entire loopback/manual-paste path
(``_xai_oauth_loopback_login``, ``_parse_pasted_callback``,
``_prompt_manual_callback_paste``, ``_xai_start_callback_server``,
``_xai_wait_for_callback``, ``_print_loopback_ssh_hint``). The original
#26923 manual-paste regression coverage is therefore moot — device-code
has no 127.0.0.1 callback to paste. This file keeps the two things that
DID survive the rewrite:

* ``_is_remote_session`` still recognises cloud-shell / Codespaces envvars
  (it now decides whether to auto-open a browser for the device-code URL).
* The BUILD-456 fail-closed interactive gate, which moved onto
  ``_xai_oauth_device_code_login``: automated/non-TTY callers must be
  rejected before any network call or browser open, never popping a real
  xAI OAuth consent tab.
"""

from __future__ import annotations

import io

import pytest

from hermes_cli import auth as auth_mod


class _TTYStdin:
    """Stub stdin that reports itself as an interactive TTY."""

    def isatty(self):
        return True


# ---------------------------------------------------------------------------
# _is_remote_session — broadened detection (#26923, still live)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "envvar",
    [
        "SSH_CLIENT",
        "SSH_TTY",
        "CLOUD_SHELL",
        "CODESPACES",
        "CODESPACE_NAME",
        "GITPOD_WORKSPACE_ID",
        "REPL_ID",
        "STACKBLITZ",
    ],
)
def test_is_remote_session_detects_known_remote_envvar(monkeypatch, envvar):
    for name in (
        "SSH_CLIENT",
        "SSH_TTY",
        "CLOUD_SHELL",
        "CODESPACES",
        "CODESPACE_NAME",
        "GITPOD_WORKSPACE_ID",
        "REPL_ID",
        "STACKBLITZ",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(envvar, "1")
    assert auth_mod._is_remote_session() is True


def test_is_remote_session_false_when_no_remote_envvars(monkeypatch):
    for name in (
        "SSH_CLIENT",
        "SSH_TTY",
        "CLOUD_SHELL",
        "CODESPACES",
        "CODESPACE_NAME",
        "GITPOD_WORKSPACE_ID",
        "REPL_ID",
        "STACKBLITZ",
    ):
        monkeypatch.delenv(name, raising=False)
    assert auth_mod._is_remote_session() is False


# ---------------------------------------------------------------------------
# Interactive-only gate (BUILD-456) — automated/test/non-TTY callers must
# fail closed before any network call or browser open, never open a real
# xAI OAuth consent tab. The gate moved onto the device-code login when
# upstream dropped the loopback path.
# ---------------------------------------------------------------------------


def test_xai_oauth_device_code_login_default_noninteractive_fails_closed(monkeypatch):
    """The default (no ``interactive=True``) call must refuse to run at all.

    Fails the test if ``webbrowser.open`` OR the OAuth discovery HTTP call
    is ever reached — the guard must reject before any side effect.
    """
    monkeypatch.setattr(
        auth_mod.webbrowser,
        "open",
        lambda *_a, **_k: pytest.fail("webbrowser.open must not be called"),
    )
    monkeypatch.setattr(
        auth_mod,
        "_xai_oauth_discovery",
        lambda *_a, **_k: pytest.fail("OAuth discovery must not be reached"),
    )

    with pytest.raises(auth_mod.AuthError) as exc:
        auth_mod._xai_oauth_device_code_login()
    assert exc.value.code == "xai_oauth_interactive_required"
    assert exc.value.provider == "xai-oauth"


def test_xai_oauth_device_code_login_interactive_flag_alone_is_not_enough(monkeypatch):
    """``interactive=True`` without real TTYs must still fail closed."""
    monkeypatch.setattr(
        auth_mod.webbrowser,
        "open",
        lambda *_a, **_k: pytest.fail("webbrowser.open must not be called"),
    )
    monkeypatch.setattr(
        auth_mod,
        "_xai_oauth_discovery",
        lambda *_a, **_k: pytest.fail("OAuth discovery must not be reached"),
    )
    monkeypatch.setattr(auth_mod.sys, "stdin", _TTYStdin())
    # stdout intentionally left as pytest's default (non-TTY) capture target.
    with pytest.raises(auth_mod.AuthError) as exc:
        auth_mod._xai_oauth_device_code_login(interactive=True)
    assert exc.value.code == "xai_oauth_interactive_required"
