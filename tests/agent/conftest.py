"""Shared fixtures for tests/agent/.

Guards every test in this directory against silently reading the *real*
macOS Keychain "Claude Code-credentials" entry on the host machine
(BUILD-416). ``agent.anthropic_adapter._read_claude_code_credentials_from_keychain``
shells out to ``security find-generic-password ...`` whenever
``platform.system() == "Darwin"`` — a call the top-level ``tests/conftest.py``
hermetic-environment fixture does not (and should not; it only blanks env
vars / redirects HERMES_HOME) cover.

On a host with a live ``claude login`` (e.g. any Ahlnos dev machine), a test
that only sets env vars / mocks ``Path.home()`` for the JSON-file fallback
still gets this machine's real ``sk-ant-oat01-...`` token back from the
Keychain, ahead of whatever fixture it set up — assertions then fail while
comparing against real token bytes.

The default here intercepts *only* that one exact ``security
find-generic-password -s "Claude Code-credentials" -w`` invocation and
answers "not found" (returncode 1), matching what a non-Darwin CI host
already sees. Every other ``subprocess.run`` call (docker, git, terminal
commands, the ``claude setup-token`` subprocess in
``run_oauth_setup_token()``) passes straight through untouched — this is
deliberately narrower than patching ``platform.system`` or ``Path.home``
wholesale, both of which are shared stdlib singletons also read by
unrelated code elsewhere in this test directory (git-repo detection in
test_coding_context.py, macOS sandbox detection in
test_claude_sdk_session.py / test_runtime_target.py / test_system_prompt.py)
and broke ~19 unrelated tests when tried directory-wide.

Tests that exercise the real Keychain path on purpose
(tests/agent/test_anthropic_keychain.py) already wrap their own calls in
``patch("agent.anthropic_adapter.subprocess.run", ...)`` — that fully
replaces this default for the span of their own ``with`` block, same as any
other mock. Tests that additionally need the JSON-file fallback
(``~/.claude/.credentials.json``) isolated patch ``Path.home()``
per-test, same as the rest of this test file already does.
"""

import subprocess as _subprocess

import pytest


def _is_keychain_lookup(argv) -> bool:
    """True for the exact security(1) invocation the Keychain reader issues."""
    try:
        return (
            list(argv[:2]) == ["security", "find-generic-password"]
            and "Claude Code-credentials" in argv
        )
    except TypeError:
        return False


@pytest.fixture(autouse=True)
def _no_host_claude_keychain(monkeypatch):
    real_run = _subprocess.run

    def _guarded_run(argv, *args, **kwargs):
        if _is_keychain_lookup(argv):
            return _subprocess.CompletedProcess(argv, returncode=1, stdout="", stderr="")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr("agent.anthropic_adapter.subprocess.run", _guarded_run)


@pytest.fixture(autouse=True)
def _reset_aux_health_cache():
    """Isolate the auxiliary-provider health blacklist between tests (BUILD-569).

    ``agent.auxiliary_client`` keeps a PROCESS-GLOBAL unhealthy-provider cache
    (``_aux_unhealthy_until`` / ``_aux_unhealthy_logged_at``, 120s TTL). A test
    that marks openrouter/nous unhealthy (e.g. pool-exhaustion cases in
    test_auxiliary_client.py) poisons it for the whole run, so a later file's
    ``_resolve_auto`` sees those providers as unhealthy and returns ``None`` —
    the order-dependent ``TestResolveAutoMainFirst`` failures that pass in
    isolation. Clearing the cache before every test in this directory makes
    aux-provider resolution deterministic regardless of collection order. No
    test relies on the blacklist surviving across tests (the ones that exercise
    it reset it themselves via ``_reset_aux_unhealthy_cache``).
    """
    from agent.auxiliary_client import _reset_aux_unhealthy_cache

    _reset_aux_unhealthy_cache()
    yield
    _reset_aux_unhealthy_cache()
