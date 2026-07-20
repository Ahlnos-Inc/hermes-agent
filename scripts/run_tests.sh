#!/usr/bin/env bash
# Canonical test runner for hermes-agent. Run this instead of calling
# `pytest` directly to guarantee your local run matches CI behavior.
#
# What this script enforces:
#   * Per-file isolation via scripts/run_tests_parallel.py — each test
#     file runs in its own freshly-spawned `python -m pytest <file>`
#     subprocess. No xdist, no shared workers, no module-level leakage
#     between files.
#   * TZ=UTC, LANG=C.UTF-8, PYTHONHASHSEED=0 (deterministic)
#   * Env vars blanked (conftest.py also does this, but this
#     is belt-and-suspenders for anyone running pytest outside our
#     conftest path — e.g. on a single file)
#   * Proper venv activation (probes .venv, venv, then ~/.hermes/...)
#
# Usage:
#   scripts/run_tests.sh                            # full suite
#   scripts/run_tests.sh -j 4                       # cap parallelism
#   scripts/run_tests.sh tests/agent/               # discover only here
#   scripts/run_tests.sh tests/agent/ tests/acp/    # multiple roots
#   scripts/run_tests.sh tests/foo.py               # single file
#   scripts/run_tests.sh tests/foo.py -q            # path + bare pytest flag
#   scripts/run_tests.sh tests/foo.py -v --tb=long  # bare flags "just work"
#   scripts/run_tests.sh -k 'pattern'               # value flags pass through too
#   scripts/run_tests.sh tests/foo.py -- --tb=long  # explicit '--' still works
#
# Bare pytest flags (anything starting with '-' that isn't one of this
# runner's own options: -j/--jobs, --paths, --slice, --file-timeout, etc.)
# are forwarded to each per-file pytest invocation automatically — no '--'
# separator required. The explicit '--' form still works and stacks with
# bare flags. Positional path arguments override the default discovery
# root (tests/).

set -euo pipefail

# ── Locate repo root ────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Activate venv ───────────────────────────────────────────────────────────
# Probe order: repo-local .venv, repo-local venv, then the canonical venv
# shared by worktrees. A candidate that exists but lacks pytest, or whose
# python major.minor doesn't match the canonical venv's, is rejected loudly
# and skipped — a stray/stale venv here otherwise silently produces "0
# tests, 100% complete" green runs or phantom failures from a divergent
# dependency set (BUILD-412). If none qualifies, we fall back to the Nix
# devShell's editable venv via HERMES_PYTHON (see the selection block below).
CANONICAL_VENV="$HOME/.hermes/hermes-agent/venv"
CANONICAL_PYVER=""
if [ -x "$CANONICAL_VENV/bin/python" ]; then
  CANONICAL_PYVER="$("$CANONICAL_VENV/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)"
fi

CANDIDATES=("$REPO_ROOT/.venv" "$REPO_ROOT/venv" "$CANONICAL_VENV")
VENV=""
for candidate in "${CANDIDATES[@]}"; do
  if [ ! -f "$candidate/bin/activate" ]; then
    continue
  fi
  cand_python="$candidate/bin/python"
  if [ ! -x "$cand_python" ]; then
    echo "warning: skipping venv candidate $candidate: no python executable at $cand_python" >&2
    continue
  fi
  if ! "$cand_python" -c 'import pytest' >/dev/null 2>&1; then
    echo "warning: skipping venv candidate $candidate: pytest not importable" >&2
    continue
  fi
  if [ -n "$CANONICAL_PYVER" ] && [ "$candidate" != "$CANONICAL_VENV" ]; then
    cand_pyver="$("$cand_python" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)"
    if [ "$cand_pyver" != "$CANONICAL_PYVER" ]; then
      echo "warning: skipping venv candidate $candidate: python $cand_pyver != canonical $CANONICAL_PYVER ($CANONICAL_VENV)" >&2
      continue
    fi
  fi
  VENV="$candidate"
  break
done

if [ -n "$VENV" ]; then
  PYTHON="$VENV/bin/python"
elif [ -n "${HERMES_PYTHON:-}" ] && [ -x "$HERMES_PYTHON" ] \
    && "$HERMES_PYTHON" -c 'import pytest' 2>/dev/null; then
  # Guard with an import check: HERMES_PYTHON may point at the RELEASE
  # venv (no pytest) when inherited from a wrapped `hermes` binary rather
  # than the devShell hook.
  PYTHON="$HERMES_PYTHON"
  echo "▶ no local venv — using Nix dev venv via HERMES_PYTHON: $PYTHON"
else
  echo "error: no usable virtualenv found (needs a python executable + importable pytest, and non-canonical candidates must match the canonical python version). tried: ${CANDIDATES[*]}" >&2
  echo "       and HERMES_PYTHON is not a python with pytest (enter the Nix devShell or create a venv)" >&2
  exit 1
fi


# ── Live-gateway plugin (computed before we drop env) ───────────────────────
EXTRA_PYTHONPATH=""
EXTRA_PYTEST_PLUGINS=""
if [ -f "$HOME/.hermes/pytest_live_guard.py" ]; then
  EXTRA_PYTHONPATH="$HOME/.hermes"
  EXTRA_PYTEST_PLUGINS="pytest_live_guard"
fi


# ── Run in hermetic env ──────────────────────────────────────────────────────
# env -i: start with empty environment, opt-in only what we need.
# No credential var can leak — you'd have to explicitly add it here.
BOOTSTRAP_HERMES_HOME="$(mktemp -d "${TMPDIR:-/tmp}/hermes-pytest-bootstrap.XXXXXX")"
trap 'rm -rf "$BOOTSTRAP_HERMES_HOME"' EXIT
echo "▶ running per-file parallel test suite via run_tests_parallel.py"
echo "  (HERMES_HOME=$BOOTSTRAP_HERMES_HOME; TZ=UTC LANG=C.UTF-8 PYTHONHASHSEED=0; clean env)"

cd "$REPO_ROOT"

# ── Pre-compile .pyc bytecode cache ─────────────────────────────────────────
# Each test file runs in its own subprocess via run_tests_parallel.py.
# Pre-building the bytecode cache once here (instead of each subprocess
# compiling on first import) avoids redundant work across ~2000 processes.
# Uses git to list tracked .py files (skips venv, node_modules, etc).
echo "▶ pre-compiling bytecode cache"
"$PYTHON" -m compileall -q -j 0 -- $(git ls-files '*.py') >/dev/null 2>&1 || true

echo "▶ launching test runner"
# No exec: the EXIT trap must run to clean the bootstrap dir.
env -i \
  PATH="$PATH" \
  HOME="$HOME" \
  HERMES_HOME="$BOOTSTRAP_HERMES_HOME" \
  TZ=UTC \
  LANG=C.UTF-8 \
  LC_ALL=C.UTF-8 \
  PYTHONHASHSEED=0 \
  ${HERMES_RUN_SLOW_PET_TESTS:+HERMES_RUN_SLOW_PET_TESTS="$HERMES_RUN_SLOW_PET_TESTS"} \
  ${EXTRA_PYTHONPATH:+PYTHONPATH="$EXTRA_PYTHONPATH"} \
  ${EXTRA_PYTEST_PLUGINS:+PYTEST_PLUGINS="$EXTRA_PYTEST_PLUGINS"} \
  "$PYTHON" "$SCRIPT_DIR/run_tests_parallel.py" "$@"
exit $?
