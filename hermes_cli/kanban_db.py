"""SQLite-backed Kanban board for multi-profile, multi-project collaboration.

In a fresh install the board lives at ``<root>/kanban.db`` where
``<root>`` is the **shared Hermes root** (the parent of any active
profile). Profiles intentionally collapse onto a shared board: it IS
the cross-profile coordination primitive. A worker spawned with
``hermes -p <profile>`` joins the same board as the dispatcher that
claimed the task. The same applies to ``<root>/kanban/workspaces/`` and
``<root>/kanban/logs/``.

**Multiple boards (projects):** users can create additional boards to
separate unrelated streams of work (e.g. one per project / repo / domain).
Each board is a directory under ``<root>/kanban/boards/<slug>/`` with
its own ``kanban.db``, ``workspaces/``, and ``logs/``. All boards share
the profile's Hermes home but are otherwise isolated: a worker spawned
for a task on board ``atm10-server`` sees only that board's tasks,
cannot enumerate other boards, and its dispatcher ticks don't touch
other boards' DBs.

The first (and for single-project users, only) board is ``default``.
For back-compat its on-disk DB is ``<root>/kanban.db`` (not
``boards/default/kanban.db``), so installs that predate the boards
feature keep working with zero migration. See :func:`kanban_db_path`.

Board resolution order (highest precedence first, all optional):

* ``board=`` argument passed directly to :func:`connect` / :func:`init_db`
  (explicit — used by the CLI ``--board`` flag and the dashboard
  ``?board=...`` query param).
* ``HERMES_KANBAN_BOARD`` env var (used by the dispatcher to pin workers
  to the board their task lives on — workers cannot see other boards).
* ``HERMES_KANBAN_DB`` env var (pins the DB file path directly — legacy
  override still honoured; highest precedence when the file path itself
  is what the caller wants to force).
* ``<root>/kanban/current`` — a one-line text file holding the slug of
  the "currently selected" board. Written by ``hermes kanban boards
  switch <slug>``. When absent, the active board is ``default``.

In standard installs ``<root>`` is ``~/.hermes``. In Docker / custom
deployments where ``HERMES_HOME`` points outside ``~/.hermes`` (e.g.
``/opt/hermes``), ``<root>`` is ``HERMES_HOME``. Legacy env-var
overrides still work:

* ``HERMES_KANBAN_DB`` — pin the database file path directly.
* ``HERMES_KANBAN_WORKSPACES_ROOT`` — pin the workspaces root directly.
* ``HERMES_KANBAN_HOME`` — pin the umbrella root that anchors kanban
  paths. Useful for tests and unusual deployments.

The dispatcher injects ``HERMES_KANBAN_DB``,
``HERMES_KANBAN_WORKSPACES_ROOT``, and ``HERMES_KANBAN_BOARD`` into
worker subprocess env so workers converge on the exact DB the
dispatcher used to claim their task — even under unusual symlink or
Docker layouts.

Schema is intentionally small: tasks, task_links, task_comments,
task_events.  The ``workspace_kind`` field decouples coordination from git
worktrees so that research / ops / digital-twin workloads work alongside
coding workloads.  See ``docs/hermes-kanban-v1-spec.pdf`` for the full
design specification.

Concurrency strategy: WAL mode + ``BEGIN IMMEDIATE`` for write
transactions + compare-and-swap (CAS) updates on ``tasks.status`` and
``tasks.claim_lock``.  SQLite serializes writers via its WAL lock, so at
most one claimer can win any given task.  Losers observe zero affected
rows and move on -- no retry loops, no distributed-lock machinery.
The CAS coordination is **per-board** — each board is a separate DB,
so multi-board installs get the same atomicity guarantees without any
new locking.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import random
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import logging
import time
from contextvars import ContextVar, Token
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Optional

from hermes_constants import VALID_REASONING_EFFORTS
from hermes_cli.sqlite_util import add_column_if_missing as _add_column_if_missing
from toolsets import get_toolset_names

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_STATUSES = {"triage", "todo", "scheduled", "ready", "running", "blocked", "review", "done", "archived"}
VALID_INITIAL_STATUSES = {"running", "blocked"}

# Typed block reasons. Distinguishes the two fundamentally different things a
# worker (or human) means by "blocked", so each can be routed differently
# instead of all landing in one undifferentiated ``blocked`` bucket that a cron
# unblocks → worker re-blocks → cron unblocks … forever.
#
#   * ``dependency``   — can't proceed until another task finishes. Routed to
#                        ``todo`` (NOT ``blocked``) so the existing
#                        parent-gating / ``recompute_ready`` machinery promotes
#                        it automatically once parents are done. No human, no
#                        cron, no retry storm.
#   * ``needs_input``  — needs a human decision/answer it cannot derive.
#   * ``capability``   — hit a hard wall (no access, missing creds, an action no
#                        AI agent can perform). Genuinely human-only.
#   * ``transient``    — a flaky/temporary failure that may clear on retry.
#
# ``needs_input`` and ``capability`` are "truly blocked": they go to ``blocked``
# for a human, and the unblock-loop breaker (see ``block_task`` /
# ``BLOCK_RECURRENCE_LIMIT``) escalates them to ``triage`` if a cron keeps
# unblocking them only to have the worker re-block for the same reason.
# ``None`` = legacy/un-typed block (treated as a generic human blocker).
VALID_BLOCK_KINDS = {"dependency", "needs_input", "capability", "transient"}
# ``dependency_pending`` is a kernel-owned projection used while a dependency
# declaration is waiting for its parent card to materialize.  It deliberately
# does not belong to ``VALID_BLOCK_KINDS``: callers must never be able to
# select it as a shortcut around the dependency/rework protocol.
PERSISTED_BLOCK_KINDS = VALID_BLOCK_KINDS | {"dependency_pending"}
DEFAULT_DEPENDENCY_MATERIALIZATION_SLA_SECONDS = 900
CONTINUATION_BLOCKER_SEVERITIES = {"P0", "P1", "P2", "P3"}
CONTINUATION_CRITICAL_SEVERITIES = {"P0", "P1"}
CONTINUATION_RESOURCE_KINDS = {"tmux_session", "worktree", "child_process"}
CONTINUATION_RESOURCE_CLEANUP_POLICIES = {"on_terminal", "manual", "preserve"}
CONTINUATION_NONPROGRESS_LIMIT = 3

# Reviewer rework is a separate bounded saga from dispatcher failures and the
# block/unblock recurrence breaker. The event log is the source of truth for
# both limits; these constants deliberately are not user-configurable.
REWORK_NONPROGRESS_LIMIT = 2
REWORK_ABSOLUTE_LIMIT = 5
REWORK_ESCALATION_RESULT = "autonomous review escalated; not approved."
REWORK_ESCALATION_EVENT_KIND = "rework_loop_escalated"
REWORK_BLOCKER_DIGEST_MAX_CHARS = 4000

# After a task has been blocked, unblocked, and re-blocked this many times for
# the same (truly-blocked) reason, the unblock-loop breaker stops trusting the
# unblocker (usually a cron) and routes the task to ``triage`` instead of back
# to ``blocked`` — breaking the infinite unblock↔re-block loop and forcing a
# human-in-the-loop decision. Mirrors the dispatcher's ``DEFAULT_FAILURE_LIMIT``
# spirit (default 2) but counts a different signal: manual unblock recurrences,
# not dispatcher spawn/crash/timeout failures.
BLOCK_RECURRENCE_LIMIT = 2

# Event kinds the notify-sub consumers (gateway/kanban_watchers.py,
# tui_gateway/server.py) poll ``task_events`` for. Shared here so the two
# consumers can't drift out of sync (BUILD-443) — each imports this tuple
# instead of keeping its own copy. ``status`` is emitted by the dashboard's
# direct status writes (``plugins/kanban/dashboard/plugin_api.py::
# _set_status_direct``), not by anything in this module.
TERMINAL_KINDS = (
    "completed", "blocked", "gave_up", "crashed", "timed_out",
    "spawn_failed", "block_loop_detected", "dependency_loop_detected",
    "rework_loop_escalated", "status", "archived", "unblocked",
)
# The subset of TERMINAL_KINDS that represents a step actually going wrong,
# as opposed to progressing normally (``completed``) or being administratively
# reclassified (``status`` / ``archived`` / ``unblocked``). Defined ONCE here
# (BUILD-508) so it can't drift from TERMINAL_KINDS by construction; consumed
# by compile_workflow_graph's per-step kinds_json filter and aliased by
# gateway/kanban_watchers.py::FAILURE_KINDS for its Telegram-home-fallback and
# tui-orphan-sweep routing — the same anti-drift move BUILD-443 used for
# TERMINAL_KINDS itself.
FAILURE_KINDS = frozenset(TERMINAL_KINDS) - {
    "completed", "status", "archived", "unblocked",
}
VALID_WORKSPACE_KINDS = {"scratch", "worktree", "dir"}
KNOWN_TOOLSET_NAMES = frozenset(name.casefold() for name in get_toolset_names())
_IS_WINDOWS = sys.platform == "win32"
KANBAN_ATTACHMENT_MAX_BYTES = 25 * 1024 * 1024

# The Architecture-First gate is deliberately an additive projection rather
# than another task status. Task statuses are scheduler-owned and cannot be
# the durable authorization source for graph writes.
ARCHITECTURE_GATE_STATES = {
    "open", "validated_awaiting_approval", "policy_accepted",
    "human_approved", "invalidated", "rejected",
}
ARCHITECTURE_GATE_ACTIVE_STATES = {
    "open", "validated_awaiting_approval", "policy_accepted", "human_approved",
}
ARCHITECTURE_GATE_APPROVED_STATES = {"policy_accepted", "human_approved"}
ARCHITECTURE_GATE_POLICY_VERSION = "v1"
ARCHITECTURE_GATE_CANONICALIZATION_VERSION = "v1"
# Rollout is deliberately one-way in authority: disabled, observability-only,
# then enforced only by the trusted orchestrator boundary.  ``enforce`` remains
# as a legacy persisted value so old boards can be read, but new callers must
# use the explicit orchestrator-only mode.
ARCHITECTURE_GATE_MODES = {"off", "shadow", "orchestrator_only", "enforce"}
ARCHITECTURE_GATE_ENFORCING_MODES = {"orchestrator_only", "enforce"}
ARCHITECTURE_GATE_REASON_OPEN = "architecture_gate_open"
AUTHENTICATED_APPROVAL_SURFACES = frozenset({"cli", "dashboard", "api", "acp", "gateway"})
READ_ONLY_DISCOVERY_PROFILES = frozenset({"scout", "researcher"})
DISCOVERY_CAPABILITY_TTL_SECONDS = 300


def _fire_kanban_lifecycle_hook(event: str, task_id: str, **fields: Any) -> None:
    """Fire a kanban lifecycle plugin hook, fully best-effort.

    Called by the claim/complete/block transitions AFTER their write txn has
    committed, so plugin code never runs while a SQLite write lock is held and
    always observes durable board state. Any failure (plugins unavailable,
    a plugin raising, import error) is swallowed — a misbehaving observer must
    never break a board state transition.

    ``profile_name`` is resolved from the active HERMES_HOME so dispatcher- and
    worker-side hooks both carry the right profile without the caller plumbing
    it through.
    """
    try:
        from hermes_cli.plugins import invoke_hook
        from hermes_cli.profiles import get_active_profile_name
        try:
            profile_name = get_active_profile_name()
        except Exception:
            profile_name = "default"
        invoke_hook(event, task_id=task_id, profile_name=profile_name, **fields)
    except Exception as exc:  # pragma: no cover - defensive
        _log.debug("kanban lifecycle hook %s failed: %s", event, exc)


# A running task's claim is valid for 15 minutes by default; after that the
# next dispatcher tick reclaims it. Workers that outlive this window should
# call ``heartbeat_claim(task_id)`` periodically. In practice most kanban
# workloads either finish within 15m, set a longer claim explicitly, or use
# ``HERMES_KANBAN_CLAIM_TTL_SECONDS`` to raise the default claim window for
# long single-call MCP workflows.
DEFAULT_CLAIM_TTL_SECONDS = 15 * 60

# If a worker's PID is still alive but its semantic-progress clock is
# older than this when ``release_stale_claims`` runs, treat the worker
# as wedged and reclaim regardless of PID liveness (#29747 gap 3).
# This catches the logic-loop case where the process is technically
# running but not making observable progress. Process keepalives and transport
# traffic deliberately do not advance this clock; model output, tool-call
# generation, and explicit durable checkpoints do.
DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS = 60 * 60

# Grace added to a claim when a reclaim is deferred because the previous
# host-local worker is still alive after a termination attempt. Releasing the
# claim in that state would spawn a duplicate alongside the surviving worker —
# the runaway seen when a cgroup memory.high throttle parks a worker in
# uninterruptible (D) state, where a pending SIGKILL cannot be delivered until
# the throttle lifts. Holding the claim a short grace and retrying next tick
# stops the duplication; once no duplicate is spawned the pressure eases, the
# signal lands, and the following tick reclaims cleanly.
RECLAIM_DEFER_GRACE_SECONDS = 120


def _resolve_claim_ttl_seconds(ttl_seconds: Optional[int] = None) -> int:
    """Return the effective claim TTL, honoring the kanban env override.

    Explicit call-site values win. Otherwise a positive integer from
    ``HERMES_KANBAN_CLAIM_TTL_SECONDS`` overrides the built-in default.
    Invalid or non-positive env values fall back silently so existing
    installs keep working.
    """
    if ttl_seconds is not None:
        return max(1, int(ttl_seconds))

    raw = os.environ.get("HERMES_KANBAN_CLAIM_TTL_SECONDS", "").strip()
    if raw:
        try:
            parsed = int(raw)
        except ValueError:
            parsed = 0
        if parsed > 0:
            return parsed

    return DEFAULT_CLAIM_TTL_SECONDS


# Grace period after a task transitions to ``running`` during which
# ``detect_crashed_workers`` skips the ``_pid_alive`` check. Covers the
# fork() → /proc-visibility window where liveness can transiently report
# False for a freshly-spawned worker. The 15-minute claim TTL still
# catches genuinely-crashed workers; this only suppresses false positives
# during the launch window.
DEFAULT_CRASH_GRACE_SECONDS = 30


# Sentinel exit code a kanban worker uses to signal "I bailed because the
# provider rate-limited / exhausted quota, not because the task failed."
# The dispatcher's reap classifier maps this to a ``rate_limited`` exit kind
# so ``detect_crashed_workers`` can release the task back to ``ready``
# WITHOUT counting a failure (the circuit breaker must never trip on a
# transient throttle). 75 == BSD ``EX_TEMPFAIL`` (sysexits.h) — the
# conventional "temporary failure, retry later" code, and well clear of the
# 0/1/2 codes the worker uses for success / generic failure / usage error.
KANBAN_RATE_LIMIT_EXIT_CODE = 75


def _resolve_crash_grace_seconds() -> int:
    """Return the crash-detection grace period in seconds.

    Reads ``HERMES_KANBAN_CRASH_GRACE_SECONDS`` from the environment;
    falls back to ``DEFAULT_CRASH_GRACE_SECONDS`` when absent, empty,
    non-integer, or negative. A value of 0 restores immediate-reclaim
    behaviour (useful for tests).
    """
    raw = os.environ.get("HERMES_KANBAN_CRASH_GRACE_SECONDS", "").strip()
    if raw:
        try:
            parsed = int(raw)
        except ValueError:
            parsed = -1
        if parsed >= 0:
            return parsed
    return DEFAULT_CRASH_GRACE_SECONDS


def _resolve_rate_limit_cooldown_seconds() -> int:
    """Return the rate-limit requeue cooldown in seconds.

    Reads ``HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS`` from the environment;
    falls back to ``DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS`` when absent, empty,
    non-integer, or negative. A value of 0 disables the cooldown (re-spawn on
    the next tick) — useful for tests that want to assert the task becomes
    spawnable again immediately.
    """
    raw = os.environ.get(
        "HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", ""
    ).strip()
    if raw:
        try:
            parsed = int(raw)
        except ValueError:
            parsed = -1
        if parsed >= 0:
            return parsed
    return DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS


# Worker-context caps so build_worker_context() stays bounded on
# pathological boards (retry-heavy tasks, comment storms, giant
# summaries). Values chosen to fit a typical 100k-char LLM prompt with
# plenty of headroom. Each constant is tuned independently so users
# who need to relax one don't have to relax all of them.
_CTX_MAX_PRIOR_ATTEMPTS = 10      # most recent N prior runs shown in full
_CTX_MAX_COMMENTS       = 30      # most recent N comments shown in full
_CTX_MAX_FIELD_BYTES    = 4 * 1024   # 4 KB per summary/error/metadata/result
_CTX_MAX_BODY_BYTES     = 8 * 1024   # 8 KB per task.body (opening post)
_CTX_MAX_COMMENT_BYTES  = 2 * 1024   # 2 KB per comment
_CTX_MAX_TOTAL_BYTES    = 48 * 1024  # hard aggregate prompt boundary
_CTX_MAX_ATTACHMENTS_BYTES = 3 * 1024
_CTX_MAX_DOWNSTREAM_TASKS = 32
_CTX_MAX_DOWNSTREAM_BYTES = 4 * 1024
_CTX_MAX_ATTEMPTS_BYTES = 7 * 1024
_CTX_MAX_PARENTS_BYTES  = 14 * 1024
_CTX_MAX_COMMENTS_BYTES = 7 * 1024


def _relative_age(ts: Optional[int], now: Optional[int] = None) -> str:
    """Render the age of an epoch-seconds timestamp as a coarse, human-
    readable string like ``just now``, ``18h ago``, ``3d ago``.

    Workers read parent handoffs, comments, and prior-attempt summaries as
    if they describe *current* state. A bare absolute timestamp
    (``2026-06-25 14:30``) doesn't make an LLM reason about staleness — it
    reads the content as fact regardless of how old it is. A relative age
    ("18h ago") is the signal that prompts the worker to re-verify against
    the live source before acting on stale sibling work. Returns an empty
    string for missing/invalid timestamps so callers can append
    unconditionally.
    """
    if ts is None:
        return ""
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return ""
    if now is None:
        now = int(time.time())
    delta = now - ts
    if delta < 0:
        # Clock skew across machines/profiles — don't claim "in the future".
        return "just now"
    if delta < 60:
        return "just now"
    if delta < 3600:
        m = delta // 60
        return f"{m}m ago"
    if delta < 86400:
        h = delta // 3600
        return f"{h}h ago"
    d = delta // 86400
    return f"{d}d ago"


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

DEFAULT_BOARD = "default"
_CURRENT_BOARD_OVERRIDE: ContextVar[str | None] = ContextVar(
    "hermes_kanban_current_board_override",
    default=None,
)


@contextlib.contextmanager
def scoped_current_board(slug: str):
    """Temporarily pin the active board for the current context only."""
    token: Token[str | None] = _CURRENT_BOARD_OVERRIDE.set(slug)
    try:
        yield
    finally:
        _CURRENT_BOARD_OVERRIDE.reset(token)

# Slug validator: lowercase alphanumerics, digits, hyphens; 1–64 chars.
# Strict enough to stop traversal (`..`) and embedded path separators, loose
# enough that kebab-case names like ``atm10-server`` or ``hermes-agent``
# pass without fuss. Board names with display formatting (spaces, emoji)
# live in ``board.json``; the slug is just the directory name.
_BOARD_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-_]{0,63}$")


def _normalize_board_slug(slug: Optional[str]) -> Optional[str]:
    """Lowercase + strip a slug; validate; return ``None`` for empty."""
    if slug is None:
        return None
    s = str(slug).strip().lower()
    if not s:
        return None
    if not _BOARD_SLUG_RE.match(s):
        raise ValueError(
            f"invalid board slug {slug!r}: must be 1-64 chars, lowercase "
            f"alphanumerics / hyphens / underscores, not starting with '-' or '_'"
        )
    return s


def kanban_home() -> Path:
    """Return the shared Hermes root that anchors the kanban board.

    Resolution order:

    1. ``HERMES_KANBAN_HOME`` env var when set and non-empty (explicit
       override for tests and unusual deployments).
    2. ``get_default_hermes_root()``, which already returns ``<root>``
       when ``HERMES_HOME`` is ``<root>/profiles/<name>``, and returns
       ``HERMES_HOME`` directly for Docker / custom deployments.

    The kanban board is shared across profiles **by design** (see the
    module docstring). Resolving the kanban paths through the active
    profile's ``HERMES_HOME`` would silently fork the board per profile,
    which breaks the dispatcher / worker handoff.
    """
    override = os.environ.get("HERMES_KANBAN_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    from hermes_constants import get_default_hermes_root
    return get_default_hermes_root()


def boards_root() -> Path:
    """Return ``<root>/kanban/boards`` — the parent of non-default board dirs.

    ``default`` is intentionally NOT under this directory — its DB lives at
    ``<root>/kanban.db`` for back-compat with pre-boards installs. This
    function returns the directory where *additional* named boards live,
    used by :func:`list_boards` to enumerate them.
    """
    return kanban_home() / "kanban" / "boards"


def current_board_path() -> Path:
    """Return the path to ``<root>/kanban/current``.

    One-line text file written by ``hermes kanban boards switch <slug>``
    to persist the user's board selection across CLI invocations. Absent
    by default (meaning: active board is ``default``).
    """
    return kanban_home() / "kanban" / "current"


def get_current_board() -> str:
    """Return the active board slug, honouring the resolution chain.

    Order (highest precedence first):

    1. ``HERMES_KANBAN_BOARD`` env var (set by the dispatcher on worker
       spawn, or manually for ad-hoc overrides).
    2. ``<root>/kanban/current`` on disk (set by ``hermes kanban boards
       switch``), but only when that board still exists.
    3. ``DEFAULT_BOARD`` (``"default"``).

    A malformed or stale slug at any step falls through to the next layer
    with a best-effort warning — the dispatcher must never crash because a
    user hand-edited a file or removed a board directory.
    """
    scoped = (_CURRENT_BOARD_OVERRIDE.get() or "").strip()
    if scoped:
        try:
            normed = _normalize_board_slug(scoped)
            if normed and board_exists(normed):
                return normed
        except ValueError:
            pass

    env = os.environ.get("HERMES_KANBAN_BOARD", "").strip()
    if env:
        try:
            normed = _normalize_board_slug(env)
            if normed and board_exists(normed):
                return normed
        except ValueError:
            pass
    try:
        f = current_board_path()
        if f.exists():
            val = f.read_text(encoding="utf-8").strip()
            if val:
                try:
                    normed = _normalize_board_slug(val)
                    if normed and board_exists(normed):
                        return normed
                except ValueError:
                    pass
    except OSError:
        pass
    return DEFAULT_BOARD


def set_current_board(slug: str) -> Path:
    """Persist ``slug`` as the active board. Returns the file written.

    Writes ``<root>/kanban/current``. The caller should validate the slug
    exists first (via :func:`board_exists`) — this function does not —
    so that ``hermes kanban boards switch <typo>`` returns an error
    instead of silently pointing at nothing.
    """
    normed = _normalize_board_slug(slug)
    if not normed:
        raise ValueError("board slug is required")
    path = current_board_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(normed + "\n", encoding="utf-8")
    return path


def clear_current_board() -> None:
    """Remove ``<root>/kanban/current`` so the active board reverts to ``default``."""
    try:
        current_board_path().unlink()
    except FileNotFoundError:
        pass


def board_dir(board: Optional[str] = None) -> Path:
    """Return the on-disk directory for ``board``.

    ``default`` is ``<root>/kanban/boards/default/`` **for metadata only**
    (board.json + workspaces/ + logs/). Its DB file stays at
    ``<root>/kanban.db`` for back-compat — see :func:`kanban_db_path`.

    All other boards live at ``<root>/kanban/boards/<slug>/`` with
    everything inside that directory including the ``kanban.db``.
    """
    slug = _normalize_board_slug(board) or DEFAULT_BOARD
    return boards_root() / slug


def board_exists(board: Optional[str] = None) -> bool:
    """Return True if the board has persisted metadata or a DB on disk.

    ``default`` is considered to always exist — its DB is created
    on first :func:`connect` and there's no way for it to be missing
    in a configuration where the kanban feature is usable at all.
    """
    slug = _normalize_board_slug(board) or DEFAULT_BOARD
    if slug == DEFAULT_BOARD:
        return True
    d = board_dir(slug)
    return (d / "board.json").exists() or (d / "kanban.db").exists()


def kanban_db_path(board: Optional[str] = None) -> Path:
    """Return the path to the ``kanban.db`` for ``board``.

    Resolution (highest precedence first):

    1. ``HERMES_KANBAN_DB`` env var — pins the path directly. Honoured for
       back-compat and for the dispatcher→worker handoff (defense in
       depth: dispatcher injects this into worker env so workers are
       immune to any path-resolution disagreement).
    2. When ``board`` arg is None, the active board from
       :func:`get_current_board` is used.
    3. Board ``default`` → ``<root>/kanban.db`` (back-compat path).
       Other boards → ``<root>/kanban/boards/<slug>/kanban.db``.
    """
    override = os.environ.get("HERMES_KANBAN_DB", "").strip()
    if override:
        return Path(override).expanduser()
    slug = _normalize_board_slug(board)
    if slug is None:
        slug = get_current_board()
    if slug == DEFAULT_BOARD:
        return kanban_home() / "kanban.db"
    return board_dir(slug) / "kanban.db"


def workspaces_root(board: Optional[str] = None) -> Path:
    """Return the directory under which ``scratch`` workspaces are created.

    Anchored per-board so workspaces don't leak between projects.
    ``HERMES_KANBAN_WORKSPACES_ROOT`` pins the path directly (highest
    precedence) — the dispatcher injects this into worker env.

    ``default`` keeps the legacy path ``<root>/kanban/workspaces/`` so
    that existing scratch workspaces from before the boards feature are
    preserved. Other boards use ``<root>/kanban/boards/<slug>/workspaces/``.
    """
    override = os.environ.get("HERMES_KANBAN_WORKSPACES_ROOT", "").strip()
    if override:
        return Path(override).expanduser()
    slug = _normalize_board_slug(board)
    if slug is None:
        slug = get_current_board()
    if slug == DEFAULT_BOARD:
        return kanban_home() / "kanban" / "workspaces"
    return board_dir(slug) / "workspaces"


def attachments_root(board: Optional[str] = None) -> Path:
    """Return the directory under which task file attachments are stored.

    Mirrors :func:`worker_logs_dir` / :func:`workspaces_root`: anchored
    per-board so attachments don't leak between projects. Each task gets
    its own ``<root>/.../attachments/<task_id>/`` subdirectory.

    ``HERMES_KANBAN_ATTACHMENTS_ROOT`` pins the path directly (highest
    precedence) for tests and unusual deployments.

    ``default`` uses ``<root>/kanban/attachments/``; other boards use
    ``<root>/kanban/boards/<slug>/attachments/``.

    Workers (which run with full file-tool access) read attached files
    by the absolute path surfaced in :func:`build_worker_context`. On the
    local terminal backend — the default for kanban — that path resolves
    directly. Remote backends (Docker/Modal) need this directory mounted;
    see the kanban docs.
    """
    override = os.environ.get("HERMES_KANBAN_ATTACHMENTS_ROOT", "").strip()
    if override:
        return Path(override).expanduser()
    slug = _normalize_board_slug(board)
    if slug is None:
        slug = get_current_board()
    if slug == DEFAULT_BOARD:
        return kanban_home() / "kanban" / "attachments"
    return board_dir(slug) / "attachments"


# Kanban task ids are canonically ``t_<hex>`` (see _new_task_id). Any other
# shape -- a path separator, ``..``, an empty string -- must never be joined
# onto a filesystem root, because it could move the attachment/log boundary
# outside its owning directory (path traversal). Enforced at every point where
# a task id becomes a filesystem path.
_SAFE_TASK_ID_RE = re.compile(r"^t_[0-9a-f]+$")


def _validate_task_id_component(task_id: Any) -> str:
    """Return ``task_id`` if it is a single safe path component, else raise.

    Rejects traversal (``..``), separators, and empty ids so an untrusted id
    can never relocate a filesystem boundary. See BUILD-711.
    """
    tid = str(task_id)
    if not _SAFE_TASK_ID_RE.match(tid):
        raise ValueError(f"unsafe kanban task id for filesystem use: {task_id!r}")
    return tid


def task_attachments_dir(task_id: str, board: Optional[str] = None) -> Path:
    """Return the per-task attachment directory ``<root>/<task_id>/``."""
    return attachments_root(board=board) / _validate_task_id_component(task_id)


def worker_logs_dir(board: Optional[str] = None) -> Path:
    """Return the directory under which per-task worker logs are written.

    ``default`` keeps the legacy path ``<root>/kanban/logs/``. Other
    boards use ``<root>/kanban/boards/<slug>/logs/``. Logs follow the
    board — makes ``hermes kanban log`` unambiguous even when multiple
    boards have tasks with the same id.
    """
    slug = _normalize_board_slug(board)
    if slug is None:
        slug = get_current_board()
    if slug == DEFAULT_BOARD:
        return kanban_home() / "kanban" / "logs"
    return board_dir(slug) / "logs"


def board_metadata_path(board: Optional[str] = None) -> Path:
    """Return the path to ``board.json`` for ``board``.

    Stores display metadata (display name, description, icon, color,
    created_at). The on-disk slug is the canonical identity; this file
    is purely for presentation in the CLI / dashboard.
    """
    slug = _normalize_board_slug(board) or DEFAULT_BOARD
    return board_dir(slug) / "board.json"


def _default_board_display_name(slug: str) -> str:
    """Turn a slug into a reasonable default display name.

    ``atm10-server`` → ``Atm10 Server``. Users can override via
    ``board.json`` but the default should look presentable in the
    dashboard without any follow-up editing.
    """
    return " ".join(part.capitalize() for part in slug.replace("_", "-").split("-") if part) or slug


def read_board_metadata(board: Optional[str] = None) -> dict:
    """Return ``board.json`` contents (or synthesized defaults).

    Never raises — a missing / malformed ``board.json`` falls back to a
    synthesised entry so the dashboard always has something to render.
    Includes the canonical ``slug`` and ``db_path`` so the caller
    doesn't need to reconstruct them.
    """
    slug = _normalize_board_slug(board) or DEFAULT_BOARD
    meta: dict[str, Any] = {
        "slug": slug,
        "name": _default_board_display_name(slug),
        "description": "",
        "icon": "",
        "color": "",
        "default_workdir": None,
        "created_at": None,
        "archived": False,
    }
    try:
        p = board_metadata_path(slug)
        if p.exists():
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                # Never let the metadata file claim a different slug than
                # its directory — trust the filesystem.
                raw["slug"] = slug
                meta.update(raw)
    except (OSError, json.JSONDecodeError):
        pass
    meta["db_path"] = str(kanban_db_path(slug))
    return meta


def write_board_metadata(
    board: Optional[str],
    *,
    name: Optional[str] = None,
    description: Optional[str] = None,
    icon: Optional[str] = None,
    color: Optional[str] = None,
    archived: Optional[bool] = None,
    default_workdir: Optional[str] = None,
) -> dict:
    """Create / update ``board.json`` for ``board``.

    Preserves any existing fields not mentioned in the call. Sets
    ``created_at`` on first write. Returns the resulting metadata dict.
    """
    slug = _normalize_board_slug(board) or DEFAULT_BOARD
    meta = read_board_metadata(slug)
    # Preserve existing DB-derived fields — they get re-computed each
    # read but shouldn't be written into board.json.
    meta.pop("db_path", None)
    if name is not None:
        meta["name"] = str(name).strip() or _default_board_display_name(slug)
    if description is not None:
        meta["description"] = str(description)
    if icon is not None:
        meta["icon"] = str(icon)
    if color is not None:
        meta["color"] = str(color)
    if archived is not None:
        meta["archived"] = bool(archived)
    if default_workdir is not None:
        meta["default_workdir"] = str(default_workdir) if default_workdir else None
    if not meta.get("created_at"):
        meta["created_at"] = int(time.time())
    path = board_metadata_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    meta["db_path"] = str(kanban_db_path(slug))
    return meta


def create_board(
    slug: str,
    *,
    name: Optional[str] = None,
    description: Optional[str] = None,
    icon: Optional[str] = None,
    color: Optional[str] = None,
    default_workdir: Optional[str] = None,
) -> dict:
    """Create a new board directory + DB + metadata. Idempotent.

    Returns the resulting metadata. Raises :class:`ValueError` for a
    malformed slug; returns the existing metadata (not an error) if the
    board already exists — matching ``mkdir -p`` semantics.
    """
    normed = _normalize_board_slug(slug)
    if not normed:
        raise ValueError("board slug is required")
    meta = write_board_metadata(
        normed,
        name=name,
        description=description,
        icon=icon,
        color=color,
        default_workdir=default_workdir,
    )
    # Touch the DB so list_boards() sees it immediately.
    init_db(board=normed)
    return meta


def list_boards(*, include_archived: bool = True) -> list[dict]:
    """Enumerate all boards that exist on disk.

    Always includes ``default`` (even when the ``boards/default/``
    metadata dir doesn't exist, because its DB is at the legacy path).
    Other boards are discovered by scanning ``boards/`` for subdirectories
    that either contain a ``kanban.db`` or a ``board.json``.

    Returns a list of metadata dicts, sorted with ``default`` first and
    the rest alphabetically.
    """
    entries: list[dict] = []
    seen: set[str] = set()

    # Default board is always first.
    entries.append(read_board_metadata(DEFAULT_BOARD))
    seen.add(DEFAULT_BOARD)

    root = boards_root()
    if root.is_dir():
        for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            if not child.is_dir():
                continue
            slug = child.name
            # Keep slug normalisation soft for discovery — but skip dirs
            # that don't parse as valid slugs so we don't surface junk.
            try:
                normed = _normalize_board_slug(slug)
            except ValueError:
                continue
            if not normed or normed in seen:
                continue
            has_db = (child / "kanban.db").exists()
            has_meta = (child / "board.json").exists()
            if not (has_db or has_meta):
                continue
            meta = read_board_metadata(normed)
            if meta.get("archived") and not include_archived:
                continue
            entries.append(meta)
            seen.add(normed)
    return entries


def remove_board(slug: str, *, archive: bool = True) -> dict:
    """Remove or archive a board.

    ``archive=True`` (default) moves the board's directory to
    ``<root>/kanban/boards/_archived/<slug>-<timestamp>/`` so the data
    is recoverable. ``archive=False`` deletes the directory outright.

    The ``default`` board cannot be removed — raises :class:`ValueError`.
    Returns a summary dict describing what happened (``{"slug", "action",
    "new_path"}``).
    """
    normed = _normalize_board_slug(slug)
    if not normed:
        raise ValueError("board slug is required")
    if normed == DEFAULT_BOARD:
        raise ValueError("the 'default' board cannot be removed")
    d = board_dir(normed)
    if not d.exists():
        raise ValueError(f"board {normed!r} does not exist")

    # If the user removed the currently-active board, revert to default.
    if get_current_board() == normed:
        clear_current_board()

    # A concurrent connect(board=normed) after the rename/delete recreates
    # an empty sqlite file via mkdir(exist_ok=True); the cache entry must be
    # dropped first so the schema init pass re-runs on that fresh file.
    _INITIALIZED_PATHS.discard(str((d / "kanban.db").resolve()))

    if archive:
        archive_root = boards_root() / "_archived"
        archive_root.mkdir(parents=True, exist_ok=True)
        ts = int(time.time())
        target = archive_root / f"{normed}-{ts}"
        # Avoid collision on rapid double-archives.
        suffix = 1
        while target.exists():
            target = archive_root / f"{normed}-{ts}-{suffix}"
            suffix += 1
        d.rename(target)
        return {"slug": normed, "action": "archived", "new_path": str(target)}
    else:
        import shutil
        shutil.rmtree(d)
        return {"slug": normed, "action": "deleted", "new_path": ""}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OperatorBlockResult:
    """Outcome of an operator block that may need to stop an active worker."""

    accepted: bool
    finalized: bool
    termination: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExistingFixTask:
    """An already-created remediation card to bind to a review card."""

    task_id: str


@dataclass(frozen=True)
class NewFixTask:
    """The caller-visible fields needed to create a remediation card."""

    title: str
    body: Optional[str]
    assignee: str
    workspace_kind: Optional[str] = None
    workspace_path: Optional[str] = None
    project_id: Optional[str] = None
    branch_name: Optional[str] = None
    priority: Optional[int] = None
    skills: Optional[tuple[str, ...]] = None
    toolsets: Optional[tuple[str, ...]] = None
    max_runtime_seconds: Optional[int] = None


@dataclass(frozen=True)
class ExistingPublicationTask:
    """An already-created publication card to bind to a requester."""

    task_id: str


@dataclass(frozen=True)
class NewPublicationTask:
    """The immutable source details needed for a releaser card.

    ``workspace_path`` is the coder's existing checkout, not a new worker
    scratch directory. The publication card stores the remote name and fully
    qualified ref separately because those are the exact arguments used by
    the completion readback.
    """

    title: str = "Publish committed change"
    body: Optional[str] = None
    assignee: str = "releaser"
    workspace_path: str = ""
    expected_sha: str = ""
    remote_ref: str = ""
    remote: str = "origin"


@dataclass(frozen=True)
class _PublicationContract:
    """Raw publication fields captured for one completion attempt.

    The values intentionally remain un-normalized so the contract can be
    compared byte-for-byte with a second read inside the completion
    transaction.  Normalization belongs only to the git readback command.
    """

    expected_sha: Any
    remote: Any
    ref: Any
    workspace_path: Any

    @property
    def has_publication_fields(self) -> bool:
        return any(
            value is not None
            for value in (
                self.expected_sha,
                self.remote,
                self.ref,
            )
        )


@dataclass(frozen=True)
class _PreparedTaskCreate:
    """Validated, transaction-ready task fields.

    Project lookup and skill/workspace validation happen before the write lock;
    parent existence, architecture gates, cycle checks, and the actual INSERT
    happen in ``_insert_task_in_txn``. Keeping this boundary explicit prevents
    the rework path from calling a public writer while it already owns the
    transaction.
    """

    title: str
    body: Optional[str]
    assignee: Optional[str]
    created_by: Optional[str]
    workspace_kind: str
    workspace_path: Optional[str]
    branch_name: Optional[str]
    tenant: Optional[str]
    priority: int
    parents: tuple[str, ...]
    triage: bool
    initial_status: str
    max_runtime_seconds: Optional[int]
    skills_list: Optional[list[str]]
    toolsets_list: Optional[list[str]]
    model_override: Optional[str]
    model_provider_override: Optional[str]
    model_reasoning_effort: Optional[str]
    max_retries: Optional[int]
    goal_mode: bool
    goal_max_turns: Optional[int]
    session_id: Optional[str]
    workflow_key: Optional[str]
    workflow_template_id: Optional[str]
    current_step_key: Optional[str]
    project_id: Optional[str]
    publication_expected_sha: Optional[str]
    publication_remote: Optional[str]
    publication_ref: Optional[str]
    project_obj: Any = None
    project_repo: Optional[str] = None


@dataclass(frozen=True)
class ReworkResult:
    """Committed outcome of one idempotent review-card rework request."""

    review_task_id: str
    fix_task_id: Optional[str]
    fix_action: Literal["created", "adopted", "replayed", "escalated"]
    review_status: str
    request_event_id: int
    escalated: bool = False
    escalation_target_task_id: Optional[str] = None
    escalation_reason: Optional[str] = None
    # Only a replay from the run that originally committed the request may
    # end the caller's worker.  A later run using the same stable key gets the
    # stored result, but remains responsible for closing itself.
    replayed_same_run: bool = False


@dataclass(frozen=True)
class PublicationHandoffResult:
    """Committed outcome of one idempotent coder-to-releaser handoff."""

    requester_task_id: str
    publication_task_id: str
    publication_action: Literal["created", "adopted", "replayed"]
    requester_status: str
    request_event_id: int
    replayed_same_run: bool = False

    @property
    def publisher_task_id(self) -> str:
        """Compatibility/readability alias for callers using publisher terminology."""
        return self.publication_task_id


@dataclass(frozen=True)
class DependencyReconcileResult:
    """Bounded projection repair performed before ready-task promotion."""

    links_restored: int = 0
    waits_materialized: int = 0
    waits_rearmed: int = 0
    legacy_recovered: int = 0
    timed_out: int = 0
    artifact_backfilled: int = 0
    artifact_selection_required: int = 0

    # Descriptive aliases keep the result pleasant for callers that prefer
    # verb-first names while the compact field names remain stable for
    # dispatch telemetry.
    @property
    def restored_links(self) -> int:
        return self.links_restored

    @property
    def materialized(self) -> int:
        return self.waits_materialized

    @property
    def rearmed(self) -> int:
        return self.waits_rearmed

    @property
    def expired(self) -> int:
        return self.timed_out


@dataclass
class Task:
    """In-memory view of a row from the ``tasks`` table."""

    id: str
    title: str
    body: Optional[str]
    assignee: Optional[str]
    status: str
    priority: int
    created_by: Optional[str]
    created_at: int
    started_at: Optional[int]
    completed_at: Optional[int]
    workspace_kind: str
    workspace_path: Optional[str]
    claim_lock: Optional[str]
    claim_expires: Optional[int]
    tenant: Optional[str]
    branch_name: Optional[str] = None
    project_id: Optional[str] = None
    result: Optional[str] = None
    idempotency_key: Optional[str] = None
    # Unified non-success counter. Incremented on any of:
    #   * spawn failure (dispatcher couldn't launch the worker)
    #   * timed_out outcome (worker exceeded max_runtime_seconds)
    #   * crashed outcome (worker PID vanished)
    # Reset to 0 only on a successful completion. See
    # ``_record_task_failure`` for the circuit-breaker trip rule.
    # (Pre-rename column: ``spawn_failures``.)
    consecutive_failures: int = 0
    worker_pid: Optional[int] = None
    worker_started_at: Optional[float] = None
    worker_pgid: Optional[int] = None
    worker_sid: Optional[int] = None
    # Short excerpt of the last failure's error text (any outcome, not
    # just spawn). Pre-rename column: ``last_spawn_error``.
    last_failure_error: Optional[str] = None
    max_runtime_seconds: Optional[int] = None
    last_heartbeat_at: Optional[int] = None
    current_run_id: Optional[int] = None
    workflow_template_id: Optional[str] = None
    current_step_key: Optional[str] = None
    # Force-loaded skills for the worker on this task (passed via
    # --skills). Stored as a JSON array of skill names. None = use only
    # the defaults; empty list = explicitly no extra skills.
    skills: Optional[list] = None
    # Task-scoped runtime capability list. None preserves the assignee
    # profile; non-empty lists are exact overrides plus lifecycle tools.
    toolsets: Optional[list] = None
    model_override: Optional[str] = None
    model_provider_override: Optional[str] = None
    model_reasoning_effort: Optional[str] = None
    # Per-task override for the consecutive-failure circuit breaker.
    # The value is the failure count at which the breaker trips — e.g.
    # ``max_retries=1`` blocks on the first failure (zero retries),
    # ``max_retries=3`` blocks on the third (two retries allowed).
    # ``None`` (the common case) falls through to the dispatcher-level
    # ``kanban.failure_limit`` config, and then to ``DEFAULT_FAILURE_LIMIT``.
    # Name matches the ``--max-retries`` CLI flag on ``kanban create``.
    max_retries: Optional[int] = None
    # When True, the dispatched worker runs in a Ralph-style goal loop
    # (the same engine behind the ``/goal`` slash command): after each
    # turn an auxiliary judge model evaluates the worker's response
    # against this card's title/body (treated as the goal). If the judge
    # says "not done" and budget remains, the worker is fed a
    # continuation prompt IN THE SAME SESSION and keeps working until the
    # judge agrees, the goal-turn budget is exhausted (→ kanban_block),
    # or the worker explicitly blocks/completes. ``False`` (default) =
    # the classic single-shot worker. ``goal_max_turns`` bounds the loop.
    goal_mode: bool = False
    # Goal-loop turn budget for ``goal_mode`` workers. ``None`` falls
    # through to the goals engine default (``goals.DEFAULT_MAX_TURNS``).
    goal_max_turns: Optional[int] = None
    # Originating chat/agent session id, when the task was created from
    # within an agent loop that propagated ``HERMES_SESSION_ID``. NULL for
    # tasks created from the CLI, the dashboard, or any path that doesn't
    # set the env var. Lets clients render a per-session board without
    # relying on tenant + time-window heuristics.
    session_id: Optional[str] = None
    # Lightweight grouping key for tasks in one orchestrator workflow.
    workflow_key: Optional[str] = None
    # Typed block reason (one of VALID_BLOCK_KINDS), the kernel-owned
    # ``dependency_pending`` projection, or None for legacy/un-typed blocks.
    # Set by ``block_task``; preserved across unblock so a re-block for the
    # same kind is recognisable as an unblock↔re-block loop.
    block_kind: Optional[str] = None
    # Unblock-loop counter. See the column comment in SCHEMA_SQL and
    # ``BLOCK_RECURRENCE_LIMIT``. Reset only on successful completion.
    block_recurrences: int = 0
    # Orthogonal to scheduler status: a policy quarantine cannot be released by
    # recompute_ready, manual promotion, or dependency completion.
    policy_quarantined: bool = False
    policy_invalidated: bool = False
    policy_quarantine_reason: Optional[str] = None
    publication_expected_sha: Optional[str] = None
    publication_remote: Optional[str] = None
    publication_ref: Optional[str] = None
    # Worktree lifecycle ownership. ``True`` means Hermes created the linked
    # worktree for this task; a pre-existing checkout is borrowed and is never
    # removed automatically.
    workspace_managed: bool = False
    workspace_repo_root: Optional[str] = None
    workspace_repo_common_dir: Optional[str] = None
    workspace_cleanup_lease: Optional[str] = None
    workspace_cleanup_lease_expires: Optional[int] = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Task":
        keys = set(row.keys())
        # Parse skills JSON blob if present
        skills_value: Optional[list] = None
        if "skills" in keys and row["skills"]:
            try:
                parsed = json.loads(row["skills"])
                if isinstance(parsed, list):
                    skills_value = [str(s) for s in parsed if s]
            except Exception:
                skills_value = None
        toolsets_value: Optional[list] = None
        if "toolsets" in keys and row["toolsets"] is not None:
            try:
                parsed = json.loads(row["toolsets"])
                if isinstance(parsed, list):
                    toolsets_value = [str(value) for value in parsed if value]
            except Exception:
                toolsets_value = None
        return cls(
            id=row["id"],
            title=row["title"],
            body=row["body"],
            assignee=row["assignee"],
            status=row["status"],
            priority=row["priority"],
            created_by=row["created_by"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            workspace_kind=row["workspace_kind"],
            workspace_path=row["workspace_path"],
            branch_name=row["branch_name"] if "branch_name" in keys else None,
            project_id=row["project_id"] if "project_id" in keys else None,
            claim_lock=row["claim_lock"],
            claim_expires=row["claim_expires"],
            tenant=row["tenant"] if "tenant" in keys else None,
            result=row["result"] if "result" in keys else None,
            idempotency_key=row["idempotency_key"] if "idempotency_key" in keys else None,
            consecutive_failures=(
                row["consecutive_failures"] if "consecutive_failures" in keys
                # Pre-migration fallback: ``_migrate_add_optional_columns`` always
                # adds ``consecutive_failures`` now, so this branch is only reachable
                # on a DB that was never opened since pre-#20410 code ran. Keep for
                # belt-and-suspenders safety; in practice it is dead code post-migration.
                else (row["spawn_failures"] if "spawn_failures" in keys else 0)
            ),
            worker_pid=row["worker_pid"] if "worker_pid" in keys else None,
            worker_started_at=(
                row["worker_started_at"] if "worker_started_at" in keys else None
            ),
            worker_pgid=row["worker_pgid"] if "worker_pgid" in keys else None,
            worker_sid=row["worker_sid"] if "worker_sid" in keys else None,
            last_failure_error=(
                row["last_failure_error"] if "last_failure_error" in keys
                # Same belt-and-suspenders fallback as consecutive_failures above.
                else (row["last_spawn_error"] if "last_spawn_error" in keys else None)
            ),
            max_runtime_seconds=(
                row["max_runtime_seconds"] if "max_runtime_seconds" in keys else None
            ),
            last_heartbeat_at=(
                row["last_heartbeat_at"] if "last_heartbeat_at" in keys else None
            ),
            current_run_id=(
                row["current_run_id"] if "current_run_id" in keys else None
            ),
            workflow_template_id=(
                row["workflow_template_id"] if "workflow_template_id" in keys else None
            ),
            current_step_key=(
                row["current_step_key"] if "current_step_key" in keys else None
            ),
            skills=skills_value,
            toolsets=toolsets_value,
            model_override=row["model_override"] if "model_override" in keys and row["model_override"] else None,
            model_provider_override=(
                row["model_provider_override"]
                if "model_provider_override" in keys and row["model_provider_override"]
                else None
            ),
            model_reasoning_effort=(
                row["model_reasoning_effort"]
                if "model_reasoning_effort" in keys and row["model_reasoning_effort"]
                else None
            ),
            max_retries=(
                row["max_retries"] if "max_retries" in keys else None
            ),
            goal_mode=(
                bool(row["goal_mode"]) if "goal_mode" in keys and row["goal_mode"] else False
            ),
            goal_max_turns=(
                row["goal_max_turns"] if "goal_max_turns" in keys and row["goal_max_turns"] else None
            ),
            session_id=(
                row["session_id"] if "session_id" in keys else None
            ),
            workflow_key=(
                row["workflow_key"] if "workflow_key" in keys else None
            ),
            block_kind=(
                row["block_kind"] if "block_kind" in keys and row["block_kind"] else None
            ),
            block_recurrences=(
                int(row["block_recurrences"])
                if "block_recurrences" in keys and row["block_recurrences"] is not None
                else 0
            ),
            policy_quarantined=bool(row["policy_quarantined"]) if "policy_quarantined" in keys else False,
            policy_invalidated=bool(row["policy_invalidated"]) if "policy_invalidated" in keys else False,
            policy_quarantine_reason=(
                row["policy_quarantine_reason"] if "policy_quarantine_reason" in keys else None
            ),
            publication_expected_sha=(
                row["publication_expected_sha"]
                if "publication_expected_sha" in keys else None
            ),
            publication_remote=(
                row["publication_remote"] if "publication_remote" in keys else None
            ),
            publication_ref=(
                row["publication_ref"] if "publication_ref" in keys else None
            ),
            workspace_managed=(
                bool(row["workspace_managed"])
                if "workspace_managed" in keys
                else False
            ),
            workspace_repo_root=(
                row["workspace_repo_root"] if "workspace_repo_root" in keys else None
            ),
            workspace_repo_common_dir=(
                row["workspace_repo_common_dir"]
                if "workspace_repo_common_dir" in keys
                else None
            ),
            workspace_cleanup_lease=(
                row["workspace_cleanup_lease"]
                if "workspace_cleanup_lease" in keys
                else None
            ),
            workspace_cleanup_lease_expires=(
                row["workspace_cleanup_lease_expires"]
                if "workspace_cleanup_lease_expires" in keys
                else None
            ),
        )

    @property
    def is_publication(self) -> bool:
        """Whether this row carries the immutable publication contract."""
        return any(
            value is not None
            for value in (
                self.publication_expected_sha,
                self.publication_remote,
                self.publication_ref,
            )
        )


@dataclass
class Run:
    """In-memory view of a ``task_runs`` row.

    A run is one attempt to execute a task — created on claim, closed
    on complete/block/crash/timeout/spawn_failure/reclaim. Multiple runs
    per task when retries happen. Carries the claim machinery, PID,
    heartbeat, and the structured handoff summary that downstream workers
    read via ``build_worker_context``.
    """

    id: int
    task_id: str
    profile: Optional[str]
    step_key: Optional[str]
    status: str
    claim_lock: Optional[str]
    claim_expires: Optional[int]
    worker_pid: Optional[int]
    worker_started_at: Optional[float]
    worker_pgid: Optional[int]
    worker_sid: Optional[int]
    max_runtime_seconds: Optional[int]
    last_heartbeat_at: Optional[int]
    last_transport_activity_at: Optional[int]
    last_semantic_progress_at: Optional[int]
    last_durable_progress_at: Optional[int]
    run_spec: Optional[dict]
    started_at: int
    ended_at: Optional[int]
    outcome: Optional[str]
    summary: Optional[str]
    metadata: Optional[dict]
    error: Optional[str]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Run":
        try:
            meta = json.loads(row["metadata"]) if row["metadata"] else None
        except Exception:
            meta = None
        try:
            run_spec = (
                json.loads(row["run_spec_json"])
                if "run_spec_json" in row.keys() and row["run_spec_json"]
                else None
            )
        except Exception:
            run_spec = None
        return cls(
            id=int(row["id"]),
            task_id=row["task_id"],
            profile=row["profile"],
            step_key=row["step_key"],
            status=row["status"],
            claim_lock=row["claim_lock"],
            claim_expires=row["claim_expires"],
            worker_pid=row["worker_pid"],
            worker_started_at=(
                row["worker_started_at"]
                if "worker_started_at" in row.keys()
                else None
            ),
            worker_pgid=row["worker_pgid"] if "worker_pgid" in row.keys() else None,
            worker_sid=row["worker_sid"] if "worker_sid" in row.keys() else None,
            max_runtime_seconds=row["max_runtime_seconds"],
            last_heartbeat_at=row["last_heartbeat_at"],
            last_transport_activity_at=row["last_transport_activity_at"],
            last_semantic_progress_at=row["last_semantic_progress_at"],
            last_durable_progress_at=row["last_durable_progress_at"],
            run_spec=run_spec,
            started_at=int(row["started_at"]),
            ended_at=(int(row["ended_at"]) if row["ended_at"] is not None else None),
            outcome=row["outcome"],
            summary=row["summary"],
            metadata=meta,
            error=row["error"],
        )


@dataclass
class Comment:
    id: int
    task_id: str
    author: str
    body: str
    created_at: int


@dataclass
class Attachment:
    """In-memory view of a row from the ``task_attachments`` table."""

    id: int
    task_id: str
    filename: str
    stored_path: str
    content_type: Optional[str]
    size: int
    uploaded_by: Optional[str]
    created_at: int


@dataclass(frozen=True)
class ReviewArtifactBinding:
    """Authoritative review-scoped pointer to one attachment generation.

    ``stored_path`` and the attachment metadata are denormalized into this
    read model for context/claim callers.  The binding table remains the
    authority for the generation and digest; the path is only usable after a
    fresh integrity check.
    """

    review_task_id: str
    generation: int
    attachment_id: int
    sha256: str
    source_task_id: str
    source_run_id: Optional[int]
    source_rework_event_id: int
    created_at: int
    filename: Optional[str]
    stored_path: Optional[str]
    attachment_task_id: Optional[str]
    size: Optional[int]

    @property
    def attachment_exists(self) -> bool:
        return self.stored_path is not None


@dataclass
class Event:
    id: int
    task_id: str
    kind: str
    payload: Optional[dict]
    created_at: int
    run_id: Optional[int] = None


@dataclass(frozen=True)
class ContinuationManifest:
    run_id: int
    task_id: str
    version: int
    manifest_digest: str
    manifest: dict[str, Any]
    context_digest: str
    compiled_context: dict[str, Any]
    created_at: int


@dataclass(frozen=True)
class ContinuationBlocker:
    id: int
    task_id: str
    severity: str
    title: str
    details: Optional[str]
    evidence_ref: Optional[str]
    fingerprint: str
    status: str
    discovered_run_id: Optional[int]
    discovered_by: str
    discovered_at: int
    resolved_run_id: Optional[int]
    resolved_by: Optional[str]
    resolution_evidence_ref: Optional[str]
    resolved_at: Optional[int]


@dataclass(frozen=True)
class OwnedRunResource:
    id: int
    task_id: str
    run_id: int
    claim_lock: str
    kind: str
    identity: dict[str, Any]
    identity_digest: str
    cleanup_policy: str
    state: str
    created_at: int
    cleaned_at: Optional[int]
    cleanup_error: Optional[str]


@dataclass(frozen=True)
class MutationContext:
    """Trusted runtime identity for a Kanban mutation.

    Boundary adapters construct this object; model tool arguments never do.
    Unknown phases are intentionally protected while a gate is unresolved.
    """

    board_key: str
    principal: str
    actor_type: str
    session_id: Optional[str] = None
    request_scope_id: Optional[str] = None
    workflow_key: Optional[str] = None
    gate_id: Optional[str] = None
    mode: str = "off"
    phase: str = "protected"
    # Runtime-owned provenance. These fields are only accepted from boundary
    # adapters, never from model-visible Kanban tool schemas.
    surface: Optional[str] = None
    profile: Optional[str] = None
    discovery_capability: Optional[str] = None


@dataclass(frozen=True)
class ArchitectureGate:
    gate_id: str
    board_key: str
    creator_principal: str
    creator_actor_type: Optional[str]
    creator_profile: Optional[str]
    request_scope_id: Optional[str]
    session_id: Optional[str]
    workflow_key: Optional[str]
    architect_task_id: str
    accepted_run_id: Optional[int]
    state: str
    policy_version: str
    canonicalization_version: str
    accepted_snapshot: Optional[str]
    design_digest: Optional[str]
    approval_actor_id: Optional[str]
    approval_actor_type: Optional[str]
    approval_surface: Optional[str]
    approved_digest: Optional[str]
    approved_at: Optional[int]
    approval_review_task_id: Optional[str]
    approval_review_completion_event_id: Optional[int]
    approval_artifact_generation: Optional[int]
    approval_artifact_sha256: Optional[str]
    authorization_event_id: Optional[int]
    enforcement_mode: str
    row_version: int
    created_at: int
    updated_at: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ArchitectureGate":
        return cls(
            gate_id=row["gate_id"], board_key=row["board_key"],
            creator_principal=row["creator_principal"],
            creator_actor_type=row["creator_actor_type"] if "creator_actor_type" in row.keys() else None,
            creator_profile=row["creator_profile"] if "creator_profile" in row.keys() else None,
            request_scope_id=row["request_scope_id"], session_id=row["session_id"],
            workflow_key=row["workflow_key"], architect_task_id=row["architect_task_id"],
            accepted_run_id=row["accepted_run_id"], state=row["state"],
            policy_version=row["policy_version"],
            canonicalization_version=row["canonicalization_version"],
            accepted_snapshot=row["accepted_snapshot"], design_digest=row["design_digest"],
            approval_actor_id=row["approval_actor_id"] if "approval_actor_id" in row.keys() else None,
            approval_actor_type=row["approval_actor_type"] if "approval_actor_type" in row.keys() else None,
            approval_surface=row["approval_surface"] if "approval_surface" in row.keys() else None,
            approved_digest=row["approved_digest"] if "approved_digest" in row.keys() else None,
            approved_at=(int(row["approved_at"]) if "approved_at" in row.keys() and row["approved_at"] is not None else None),
            approval_review_task_id=(
                row["approval_review_task_id"]
                if "approval_review_task_id" in row.keys() else None
            ),
            approval_review_completion_event_id=(
                int(row["approval_review_completion_event_id"])
                if (
                    "approval_review_completion_event_id" in row.keys()
                    and row["approval_review_completion_event_id"] is not None
                ) else None
            ),
            approval_artifact_generation=(
                int(row["approval_artifact_generation"])
                if (
                    "approval_artifact_generation" in row.keys()
                    and row["approval_artifact_generation"] is not None
                ) else None
            ),
            approval_artifact_sha256=(
                row["approval_artifact_sha256"]
                if "approval_artifact_sha256" in row.keys() else None
            ),
            authorization_event_id=(
                int(row["authorization_event_id"])
                if "authorization_event_id" in row.keys() and row["authorization_event_id"] is not None
                else None
            ),
            enforcement_mode=row["enforcement_mode"], row_version=int(row["row_version"]),
            created_at=int(row["created_at"]), updated_at=int(row["updated_at"]),
        )


class ArchitectureGateError(ValueError):
    """Stable policy denial safe to expose to callers and audit logs."""

    def __init__(self, code: str = ARCHITECTURE_GATE_REASON_OPEN):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class DiscoveryCapability:
    token: str
    gate_id: str
    board_key: str
    principal: str
    session_id: str
    request_scope_id: str
    profile: str
    issued_at: int
    expires_at: int
    used_at: Optional[int]


@dataclass(frozen=True)
class PolicyQuarantineClassification:
    task_id: str
    reason: str = "architecture_gate_premature_card"


class WorkflowGraphError(ValueError):
    """Stable workflow-compilation conflict or topology error."""


class WorkspaceContractError(ValueError):
    """A task's workspace contract cannot be satisfied (BUILD-496).

    Raised fail-closed at creation (no task row / compilation persisted) and
    at dispatch for a deterministic, structurally-impossible worktree anchor
    (invariants 1-3/7). ``code`` is a stable machine-readable classifier:
    ``unknown_project`` (an explicitly-requested project that does not resolve
    in the creating profile), ``worktree_no_anchor`` (worktree with no path,
    no project repo, and no board default_workdir), or ``worktree_bad_anchor``
    (a worktree path/anchor that is non-absolute or not a git repo). A
    ValueError subclass so existing ``except ValueError`` surfaces (the public
    tool handlers, the compiler handler) return it as a typed tool error.
    Deterministic by construction: retrying the spawn can only reproduce it,
    so dispatch blocks instead of counting a retryable spawn failure.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CompiledWorkflowGraph:
    """Task ids produced by one atomic workflow compilation."""

    workflow_key: str
    task_ids: dict[str, str]
    terminal_task_id: str


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
    id                   TEXT PRIMARY KEY,
    title                TEXT NOT NULL,
    body                 TEXT,
    assignee             TEXT,
    status               TEXT NOT NULL,
    priority             INTEGER DEFAULT 0,
    created_by           TEXT,
    created_at           INTEGER NOT NULL,
    started_at           INTEGER,
    completed_at         INTEGER,
    workspace_kind       TEXT NOT NULL DEFAULT 'scratch',
    workspace_path       TEXT,
    -- Exact ownership and repository identity for a materialized linked
    -- worktree. Borrowed worktrees keep workspace_managed=0.
    workspace_managed    INTEGER NOT NULL DEFAULT 0,
    workspace_repo_root  TEXT,
    workspace_repo_common_dir TEXT,
    workspace_cleanup_lease TEXT,
    workspace_cleanup_lease_expires INTEGER,
    branch_name          TEXT,
    -- Optional link to a first-class Project (hermes_cli/projects_db). When set,
    -- the task's worktree is anchored under the project's primary repo with a
    -- deterministic branch name instead of a random wt/<task-id> fallback.
    project_id           TEXT,
    claim_lock           TEXT,
    claim_expires        INTEGER,
    tenant               TEXT,
    result               TEXT,
    idempotency_key      TEXT,
    -- Unified consecutive-failure counter. Incremented on spawn
    -- failure, timeout, or crash; reset only on successful completion.
    -- The circuit breaker in _record_task_failure trips when this
    -- exceeds DEFAULT_FAILURE_LIMIT consecutive non-successes.
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    worker_pid           INTEGER,
    worker_started_at    REAL,
    worker_pgid          INTEGER,
    worker_sid           INTEGER,
    -- Short excerpt of the most recent failure's error text.
    last_failure_error   TEXT,
    max_runtime_seconds  INTEGER,
    last_heartbeat_at    INTEGER,
    -- Pointer into task_runs for the currently-active run (NULL if no
    -- run is in-flight). Denormalised for cheap reads.
    current_run_id       INTEGER,
    -- Forward-compat for v2 workflow routing. In v1 the kernel writes
    -- these when the task is opted into a template but otherwise ignores
    -- them; the dispatcher doesn't consult them for routing yet.
    workflow_template_id TEXT,
    current_step_key     TEXT,
    -- Force-loaded skills for the worker on this task, stored as JSON.
    -- Passed to the worker via `--skills`. NULL or empty array = no extras.
    skills               TEXT,
    -- Task-scoped CLI capabilities. NULL = profile fallback; non-empty JSON
    -- list = exact toolset override plus mandatory lifecycle tools.
    toolsets             TEXT,
    -- Per-task model override. When set, the dispatcher passes -m <model>
    -- to the worker, overriding the profile's default model. NULL = use
    -- the profile default.
    model_override       TEXT,
    -- Per-task provider / reasoning overrides resolved from model_routing.
    -- These travel with model_override so a routed task can pin the full
    -- provider/model/reasoning tuple instead of only the model id.
    model_provider_override TEXT,
    model_reasoning_effort  TEXT,
    -- Per-task override for the consecutive-failure circuit breaker.
    -- The value is the failure count at which the breaker trips — e.g.
    -- ``max_retries=1`` blocks on the first failure. NULL (the common
    -- case) falls through to the dispatcher-level ``kanban.failure_limit``
    -- config and then ``DEFAULT_FAILURE_LIMIT``.
    max_retries          INTEGER,
    -- When 1, the dispatched worker runs in a Ralph-style goal loop: an
    -- auxiliary judge re-evaluates the worker's response against the
    -- card title/body after each turn and feeds a continuation prompt
    -- back into the SAME session until the judge agrees the work is done
    -- or ``goal_max_turns`` is exhausted. NULL/0 = classic single-shot
    -- worker (the default).
    goal_mode            INTEGER NOT NULL DEFAULT 0,
    -- Goal-loop turn budget for ``goal_mode`` workers. NULL = use the
    -- goals-engine default.
    goal_max_turns       INTEGER,
    -- Originating chat/agent session id when the task was created from
    -- inside an agent loop that propagated ``HERMES_SESSION_ID``. NULL
    -- for tasks created from the CLI, dashboard, or any path that doesn't
    -- set the env var. Indexed so per-session list queries stay cheap on
    -- larger boards.
    session_id           TEXT,
    -- Lightweight grouping key for tasks that belong to the same
    -- orchestrator-created workflow. NULL for one-off tasks.
    workflow_key         TEXT,
    -- Typed block reason set by ``block_task`` (one of VALID_BLOCK_KINDS, or
    -- NULL for legacy/un-typed blocks). The kernel may additionally persist
    -- ``dependency_pending`` while a dependency's fix card is unbound; callers
    -- cannot select that projection. Drives routing: ``dependency`` never
    -- sits in ``blocked`` (goes to ``todo`` for parent-gating); the others go
    -- to ``blocked`` for a human. Preserved across unblock so a re-block for
    -- the SAME kind can be recognised as a loop.
    block_kind           TEXT,
    -- Unblock-loop counter. Incremented each time a task is re-blocked for the
    -- same truly-blocked reason after having been unblocked. When it reaches
    -- BLOCK_RECURRENCE_LIMIT the task is routed to ``triage`` instead of
    -- ``blocked`` so a cron can't spin it forever. Reset to 0 only on a
    -- successful completion — NOT on unblock (resetting on unblock is exactly
    -- the amnesia that let the loop run unbounded).
    block_recurrences    INTEGER NOT NULL DEFAULT 0,
    -- Sticky policy containment is independent of scheduler state. A nonzero
    -- value is never released by automatic promotion or ordinary unblock.
    policy_quarantined   INTEGER NOT NULL DEFAULT 0,
    policy_invalidated   INTEGER NOT NULL DEFAULT 0,
    policy_quarantine_reason TEXT,
    -- Immutable publication contract. A row with any of these fields set is
    -- a publication card and cannot complete without a remote readback that
    -- observes ``publication_expected_sha`` at ``publication_ref``.
    publication_expected_sha TEXT,
    publication_remote    TEXT,
    publication_ref       TEXT
);

CREATE TABLE IF NOT EXISTS task_links (
    parent_id  TEXT NOT NULL,
    child_id   TEXT NOT NULL,
    PRIMARY KEY (parent_id, child_id)
);

CREATE TABLE IF NOT EXISTS task_comments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    author     TEXT NOT NULL,
    body       TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS task_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    run_id     INTEGER,
    kind       TEXT NOT NULL,
    payload    TEXT,
    created_at INTEGER NOT NULL
);

-- Historical attempt record. Each time the dispatcher claims a task, a
-- new row is created here; claim state, PID, heartbeat, runtime cap,
-- and structured summary all live on the run, not the task. Multiple
-- rows per task id when the task was retried after crash/timeout/block.
-- v2 of the kanban schema will use ``step_key`` to drive per-stage
-- workflow routing; in v1 the column is nullable and unused (kernel
-- ignores it).
CREATE TABLE IF NOT EXISTS task_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id             TEXT NOT NULL,
    profile             TEXT,
    step_key            TEXT,
    status              TEXT NOT NULL,
    -- status: running | done | blocked | crashed | timed_out | failed | released
    claim_lock          TEXT,
    claim_expires       INTEGER,
    worker_pid          INTEGER,
    worker_started_at   REAL,
    worker_pgid         INTEGER,
    worker_sid          INTEGER,
    max_runtime_seconds INTEGER,
    last_heartbeat_at   INTEGER,
    last_transport_activity_at INTEGER,
    last_semantic_progress_at INTEGER,
    last_durable_progress_at INTEGER,
    -- Immutable, versioned, secret-free requested runtime contract for this
    -- attempt. NULL identifies a legacy/synthetic run.
    run_spec_json       TEXT,
    started_at          INTEGER NOT NULL,
    ended_at            INTEGER,
    outcome             TEXT,
    -- outcome: completed | blocked | crashed | timed_out | spawn_failed |
    --          gave_up | reclaimed | (null while still running)
    summary             TEXT,
    metadata            TEXT,
    error               TEXT
);

-- Files attached to a task (PDFs, images, source documents). The blob
-- lives on disk under ``attachments_root(board)/<task_id>/<stored_name>``;
-- this row carries metadata + the absolute ``stored_path`` so the
-- dashboard can list/download and ``build_worker_context`` can surface
-- the absolute path to the worker (which has full file-tool access). See
-- #35338.
CREATE TABLE IF NOT EXISTS task_attachments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      TEXT NOT NULL,
    filename     TEXT NOT NULL,
    stored_path  TEXT NOT NULL,
    content_type TEXT,
    size         INTEGER NOT NULL DEFAULT 0,
    uploaded_by  TEXT,
    created_at   INTEGER NOT NULL
);

-- The current artifact for a review is a review-contract projection, not a
-- global mutable filename.  Each completed fix appends one generation.  A
-- source rework event is unique so retrying the same completion is harmless.
CREATE TABLE IF NOT EXISTS review_artifact_bindings (
    review_task_id       TEXT NOT NULL,
    generation           INTEGER NOT NULL,
    attachment_id        INTEGER NOT NULL,
    sha256               TEXT NOT NULL,
    source_task_id       TEXT NOT NULL,
    source_run_id        INTEGER,
    source_rework_event_id INTEGER NOT NULL,
    created_at           INTEGER NOT NULL,
    PRIMARY KEY (review_task_id, generation),
    UNIQUE (review_task_id, source_rework_event_id)
);

-- Subscription from a gateway source (platform + chat + thread) to a
-- task. The gateway's kanban-notifier watcher tails task_events and
-- pushes ``completed`` / ``blocked`` / ``spawn_auto_blocked`` events to
-- the original requester so human-in-the-loop workflows close the loop.
CREATE TABLE IF NOT EXISTS kanban_notify_subs (
    task_id       TEXT NOT NULL,
    platform      TEXT NOT NULL,
    chat_id       TEXT NOT NULL,
    thread_id     TEXT NOT NULL DEFAULT '',
    user_id       TEXT,
    notifier_profile TEXT,
    created_at    INTEGER NOT NULL,
    last_event_id INTEGER NOT NULL DEFAULT 0,
    -- BUILD-508: per-subscription event-kind filter, a sorted JSON array
    -- (e.g. '["blocked","crashed",...]') or NULL. NULL means "all kinds" —
    -- the behavior every subscription had before this column existed, so
    -- every pre-BUILD-508 row keeps working unchanged. See
    -- add_notify_sub()/notify_sub_kinds() below.
    kinds_json    TEXT,
    PRIMARY KEY (task_id, platform, chat_id, thread_id)
);

-- BUILD-503: delivery accounting for notification events. The cursor in
-- kanban_notify_subs proves a subscription EXISTS and dedupes replays, but a
-- present subscription is not proof a message was ever delivered (the
-- 2026-07-16 incident). This ledger records the queued→consumed→delivered
-- trail keyed by the stable notify_delivery_key() range so delivery is
-- verifiable and re-recording on watcher restart is idempotent.
CREATE TABLE IF NOT EXISTS notify_deliveries (
    delivery_key   TEXT PRIMARY KEY,
    task_id        TEXT NOT NULL,
    platform       TEXT NOT NULL,
    chat_id        TEXT NOT NULL,
    thread_id      TEXT NOT NULL DEFAULT '',
    first_event_id INTEGER NOT NULL,
    last_event_id  INTEGER NOT NULL,
    status         TEXT NOT NULL,
    attempts       INTEGER NOT NULL DEFAULT 1,
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL
);

-- Authoritative authorization projection for Architecture-First workflows.
-- The accepted snapshot is canonical JSON, not mutable task metadata.
CREATE TABLE IF NOT EXISTS architecture_gates (
    gate_id                  TEXT PRIMARY KEY,
    board_key                TEXT NOT NULL,
    creator_principal        TEXT NOT NULL,
    creator_actor_type       TEXT,
    creator_profile          TEXT,
    request_scope_id         TEXT,
    session_id               TEXT,
    workflow_key             TEXT,
    architect_task_id        TEXT NOT NULL,
    accepted_run_id          INTEGER,
    state                    TEXT NOT NULL,
    policy_version           TEXT NOT NULL,
    canonicalization_version TEXT NOT NULL,
    accepted_snapshot        TEXT,
    design_digest            TEXT,
    approval_actor_id        TEXT,
    approval_actor_type      TEXT,
    approval_surface         TEXT,
    approved_digest          TEXT,
    approved_at               INTEGER,
    approval_review_task_id  TEXT,
    approval_review_completion_event_id INTEGER,
    approval_artifact_generation INTEGER,
    approval_artifact_sha256 TEXT,
    authorization_event_id    INTEGER,
    enforcement_mode         TEXT NOT NULL DEFAULT 'off',
    row_version              INTEGER NOT NULL DEFAULT 0,
    created_at               INTEGER NOT NULL,
    updated_at               INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS discovery_capabilities (
    token             TEXT PRIMARY KEY,
    gate_id           TEXT NOT NULL,
    board_key         TEXT NOT NULL,
    principal         TEXT NOT NULL,
    session_id        TEXT NOT NULL,
    request_scope_id  TEXT NOT NULL,
    profile           TEXT NOT NULL,
    issued_at         INTEGER NOT NULL,
    expires_at        INTEGER NOT NULL,
    used_at           INTEGER
);

-- A human-approved architecture gate may issue exactly one implementation
-- graph. Store the immutable issued IDs so a retry with the same key is
-- idempotent while a second graph fails before it writes tasks or links.
CREATE TABLE IF NOT EXISTS architecture_graph_issuances (
    gate_id          TEXT PRIMARY KEY,
    idempotency_key  TEXT NOT NULL,
    task_ids         TEXT NOT NULL,
    issued_by        TEXT NOT NULL,
    issued_at        INTEGER NOT NULL
);

-- One immutable, idempotent compilation per workflow. The graph specification
-- digest lets retries return the original ids while rejecting a request that
-- reuses the workflow identity for different work.
CREATE TABLE IF NOT EXISTS workflow_graph_compilations (
    workflow_key      TEXT PRIMARY KEY,
    idempotency_key   TEXT NOT NULL,
    spec_digest       TEXT NOT NULL,
    request_digest    TEXT NOT NULL,
    task_ids          TEXT NOT NULL,
    terminal_step_key TEXT NOT NULL,
    created_by        TEXT NOT NULL,
    created_at        INTEGER NOT NULL
);

-- BUILD-487: immutable, content-addressed bootstrap contract for one worker
-- epoch. Presence opts that run into the continuation architecture; legacy
-- runs without a row retain their existing behavior.
CREATE TABLE IF NOT EXISTS continuation_manifests (
    run_id                INTEGER PRIMARY KEY,
    task_id               TEXT NOT NULL,
    version               INTEGER NOT NULL,
    manifest_digest       TEXT NOT NULL,
    manifest_json         TEXT NOT NULL,
    context_digest        TEXT NOT NULL,
    compiled_context_json TEXT NOT NULL,
    created_at            INTEGER NOT NULL
);

-- Findings survive review/implementation epochs. Completion reads this live
-- ledger instead of trusting a stale prompt snapshot; any open P0/P1 blocks.
CREATE TABLE IF NOT EXISTS continuation_blockers (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id                 TEXT NOT NULL,
    severity                TEXT NOT NULL,
    title                   TEXT NOT NULL,
    details                 TEXT,
    evidence_ref            TEXT,
    fingerprint             TEXT NOT NULL,
    status                  TEXT NOT NULL,
    discovered_run_id       INTEGER,
    discovered_by           TEXT NOT NULL,
    discovered_at           INTEGER NOT NULL,
    resolved_run_id         INTEGER,
    resolved_by             TEXT,
    resolution_evidence_ref TEXT,
    resolved_at             INTEGER
);

-- Destructive cleanup is permitted only for resources registered to the
-- exact task/run/claim owner. No session-name or path inference is authority.
CREATE TABLE IF NOT EXISTS continuation_owned_resources (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         TEXT NOT NULL,
    run_id          INTEGER NOT NULL,
    claim_lock      TEXT NOT NULL,
    kind            TEXT NOT NULL,
    identity_json   TEXT NOT NULL,
    identity_digest TEXT NOT NULL,
    cleanup_policy  TEXT NOT NULL,
    state           TEXT NOT NULL,
    created_at      INTEGER NOT NULL,
    cleaned_at      INTEGER,
    cleanup_error   TEXT,
    UNIQUE(run_id, kind, identity_digest)
);

CREATE INDEX IF NOT EXISTS idx_tasks_assignee_status ON tasks(assignee, status);
CREATE INDEX IF NOT EXISTS idx_tasks_status          ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_links_child           ON task_links(child_id);
CREATE INDEX IF NOT EXISTS idx_links_parent          ON task_links(parent_id);
CREATE INDEX IF NOT EXISTS idx_comments_task         ON task_comments(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_events_task           ON task_events(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_runs_task             ON task_runs(task_id, started_at);
CREATE INDEX IF NOT EXISTS idx_runs_status           ON task_runs(status);
CREATE INDEX IF NOT EXISTS idx_attachments_task      ON task_attachments(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_review_artifact_bindings_attachment
    ON review_artifact_bindings(attachment_id);
CREATE INDEX IF NOT EXISTS idx_review_artifact_bindings_current
    ON review_artifact_bindings(review_task_id, generation DESC);
CREATE INDEX IF NOT EXISTS idx_notify_task           ON kanban_notify_subs(task_id);
CREATE INDEX IF NOT EXISTS idx_notify_deliveries_task ON notify_deliveries(task_id, last_event_id);
CREATE INDEX IF NOT EXISTS idx_architecture_gates_architect
    ON architecture_gates(architect_task_id);
CREATE INDEX IF NOT EXISTS idx_architecture_gates_workflow
    ON architecture_gates(board_key, workflow_key);
CREATE INDEX IF NOT EXISTS idx_continuation_manifests_task
    ON continuation_manifests(task_id, run_id);
CREATE INDEX IF NOT EXISTS idx_continuation_blockers_open
    ON continuation_blockers(task_id, status, severity);
CREATE INDEX IF NOT EXISTS idx_continuation_resources_run
    ON continuation_owned_resources(run_id, state);
CREATE UNIQUE INDEX IF NOT EXISTS idx_architecture_gates_active_scope
    ON architecture_gates(board_key, creator_principal, request_scope_id)
    WHERE state IN ('open', 'validated_awaiting_approval', 'policy_accepted', 'human_approved')
      AND request_scope_id IS NOT NULL;
"""


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

_INITIALIZED_PATHS: set[str] = set()
_INIT_LOCK = threading.RLock()
_SQLITE_HEADER = b"SQLite format 3\x00"
DEFAULT_BUSY_TIMEOUT_MS = 120_000

# Bounded acquire for the cross-process init lock (#36644). The original bare
# blocking flock had no timeout, so a wedged holder blocked the dispatcher's
# next-tick connect forever. We retry a non-blocking acquire up to this
# deadline, polling at this interval, then proceed without the cross-process
# lock (the in-process _INIT_LOCK + idempotent init remain the backstop).
_INIT_LOCK_TIMEOUT_SECONDS = 10.0
_INIT_LOCK_POLL_SECONDS = 0.05


def _resolve_busy_timeout_ms() -> int:
    """Return the SQLite busy timeout for Kanban connections.

    Kanban is the shared cross-profile dispatch bus, so worker stampedes are
    expected.  A long busy timeout lets SQLite serialize writers via WAL rather
    than surfacing transient ``database is locked`` failures during bursts.
    """
    raw = os.environ.get("HERMES_KANBAN_BUSY_TIMEOUT_MS", "").strip()
    if raw:
        try:
            parsed = int(raw)
        except ValueError:
            parsed = 0
        if parsed > 0:
            return parsed
    return DEFAULT_BUSY_TIMEOUT_MS


def _sqlite_connect(path: Path) -> sqlite3.Connection:
    """Open a Kanban SQLite connection with consistent lock waiting."""
    busy_timeout_ms = _resolve_busy_timeout_ms()
    conn = sqlite3.connect(
        str(path),
        isolation_level=None,
        timeout=busy_timeout_ms / 1000.0,
    )
    # ``sqlite3.connect(timeout=...)`` normally maps to busy_timeout, but set
    # the PRAGMA explicitly so it is observable and survives future wrapper
    # changes. Parameter binding is not supported for PRAGMA assignments.
    conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
    return conn


@contextlib.contextmanager
def _cross_process_init_lock(path: Path):
    """Serialize first-connect WAL/schema/integrity setup across processes.

    ``_INIT_LOCK`` only protects threads inside one Python process. During a
    dispatcher burst, many worker processes can all hit a fresh/legacy board at
    once and each process has an empty ``_INITIALIZED_PATHS`` cache. This file
    lock keeps header validation, integrity probing, WAL activation, and
    additive migrations single-file/single-writer across the whole host while
    leaving normal post-init DB usage concurrent under SQLite WAL.

    The acquire is **bounded** (issue #36644): the original bare blocking
    ``flock(LOCK_EX)`` had no timeout, so a single process stalled inside the
    critical section (or a stale lock held by a wedged worker) blocked every
    other ``connect()`` — including the long-lived gateway dispatcher's
    next-tick connect — forever, with no traceback and no recovery short of a
    restart. We now retry a non-blocking acquire up to a deadline; on timeout
    we log a WARNING and proceed WITHOUT the cross-process lock. That is safe:
    the in-process ``_INIT_LOCK`` still serializes same-process threads, and
    the init work itself is idempotent (``CREATE TABLE IF NOT EXISTS`` +
    additive migrations), so the worst case of two processes racing first-init
    is redundant work, not corruption. A bounded "proceed anyway" beats an
    unbounded hang that silently stops the board.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".init.lock")
    handle = lock_path.open("a+b")
    acquired = False
    try:
        deadline = time.monotonic() + _INIT_LOCK_TIMEOUT_SECONDS
        if _IS_WINDOWS:
            import msvcrt

            locking = getattr(msvcrt, "locking")
            nb_lock = getattr(msvcrt, "LK_NBLCK")
            while True:
                try:
                    handle.seek(0)
                    locking(handle.fileno(), nb_lock, 1)
                    acquired = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(_INIT_LOCK_POLL_SECONDS)
        else:
            import fcntl

            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except (BlockingIOError, OSError):
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(_INIT_LOCK_POLL_SECONDS)
        if not acquired:
            _log.warning(
                "kanban init lock for %s not acquired within %.0fs — proceeding "
                "without the cross-process lock (in-process lock + idempotent "
                "init are the correctness backstop). A stuck holder is no longer "
                "able to block this connect indefinitely (#36644).",
                lock_path, _INIT_LOCK_TIMEOUT_SECONDS,
            )
        yield
    finally:
        try:
            if acquired:
                if _IS_WINDOWS:
                    import msvcrt

                    handle.seek(0)
                    locking = getattr(msvcrt, "locking")
                    unlock_mode = getattr(msvcrt, "LK_UNLCK")
                    locking(handle.fileno(), unlock_mode, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


@contextlib.contextmanager
def _dispatch_tick_lock(db_path: Path):
    """Non-blocking single-writer guard around one dispatcher tick.

    Yields ``True`` when this process holds the board's dispatch lock and
    may proceed with the tick, or ``False`` when another process already
    holds it (the caller should skip the tick this round).

    Motivation (issue #35240): a ``hermes gateway run --replace`` /
    ``gateway restart`` invoked from a shell on a systemd/launchd host can
    leave an orphan gateway whose dispatcher escapes the service cgroup,
    survives ``systemctl restart``, and becomes a *second* long-lived
    writer on the same ``kanban.db``. Two dispatchers that each believe
    they own the file both pass SQLite ``busy_timeout`` and then race on
    WAL frames — the documented root cause of multi-writer corruption.
    The startup guard (``_guard_supervised_gateway_conflict``) blocks the
    common way an orphan is born, but this lock is the defense-in-depth
    that prevents two dispatchers from ever writing concurrently
    *regardless of how the second one got there*.

    The lock is **non-blocking** on purpose: the gateway's async watcher
    must never stall on a held lock. A losing dispatcher simply skips its
    tick (the winner is making progress on the same board), and tries
    again next interval.

    Board-scoped: the lock file is a ``.dispatch.lock`` sibling of the
    board's ``kanban.db``, so unrelated boards tick independently. On
    platforms without ``fcntl``/``msvcrt`` the guard degrades to a no-op
    (yields ``True``) — single-writer enforcement is best-effort and the
    orphan-dispatcher scenario is specific to POSIX service managers.
    """
    lock_path = db_path.with_name(db_path.name + ".dispatch.lock")
    handle = None
    acquired = False
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")
        if _IS_WINDOWS:
            try:
                import msvcrt

                handle.seek(0)
                locking = getattr(msvcrt, "locking")
                # LK_NBLCK = non-blocking exclusive byte-range lock.
                nb_lock = getattr(msvcrt, "LK_NBLCK")
                locking(handle.fileno(), nb_lock, 1)
                acquired = True
            except (OSError, AttributeError):
                acquired = False
        else:
            try:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except (BlockingIOError, OSError):
                acquired = False
    except OSError:
        # Could not even open the lock file (permissions, read-only FS).
        # Degrade to a no-op so a probe failure never blocks dispatch.
        acquired = True
        handle = None
    try:
        yield acquired
    finally:
        if handle is not None:
            try:
                if acquired:
                    if _IS_WINDOWS:
                        import msvcrt

                        handle.seek(0)
                        locking = getattr(msvcrt, "locking")
                        unlock_mode = getattr(msvcrt, "LK_UNLCK")
                        locking(handle.fileno(), unlock_mode, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except (OSError, AttributeError):
                pass
            finally:
                handle.close()


def _looks_like_tls_record_at(data: bytes, offset: int) -> bool:
    """Return True for a TLS record header at ``data[offset:]``."""
    if len(data) < offset + 5:
        return False
    content_type = data[offset]
    major = data[offset + 1]
    minor = data[offset + 2]
    length = int.from_bytes(data[offset + 3:offset + 5], "big")
    return (
        content_type in {0x14, 0x15, 0x16, 0x17}
        and major == 0x03
        and minor in {0x00, 0x01, 0x02, 0x03, 0x04}
        and 0 < length <= 18432
    )


# Never-closed read-only descriptors for header validation, one per DB path.
# BUILD-575: closing ANY fd that references a file releases every POSIX fcntl
# lock the whole process holds on it (SQLite "How To Corrupt", §2.2). SQLite's
# unix VFS defends against that only for descriptors it owns, so the previous
# ``path.open("rb")`` here — running on EVERY fast-path connect() — silently
# unlocked mid-checkpoint databases for other processes and let two
# checkpointers backfill WAL frames concurrently (the 2026-07-19 notify-page
# corruption). Reading through a descriptor that is never closed cannot drop
# locks. Cache is keyed by realpath; a swapped file (board recovery uses
# os.replace) is detected by dev/ino mismatch and reopened — closing the stale
# descriptor then only touches the abandoned inode.
_HEADER_FD_LOCK = threading.Lock()
_HEADER_FD_CACHE: "dict[str, tuple[int, int, int]]" = {}


def _read_db_header(path: Path, size: int = 64) -> bytes:
    if os.name == "nt":
        # Windows has no POSIX fcntl close-drops-locks hazard; plain read.
        with path.open("rb") as handle:
            return handle.read(size)
    key = os.path.realpath(str(path))
    with _HEADER_FD_LOCK:
        st = os.stat(key)
        cached = _HEADER_FD_CACHE.get(key)
        if cached is not None and (cached[1], cached[2]) != (st.st_dev, st.st_ino):
            os.close(cached[0])
            del _HEADER_FD_CACHE[key]
            cached = None
        if cached is None:
            fd = os.open(key, os.O_RDONLY)
            fst = os.fstat(fd)
            cached = (fd, fst.st_dev, fst.st_ino)
            _HEADER_FD_CACHE[key] = cached
        return os.pread(cached[0], size, 0)


def _validate_sqlite_header(path: Path) -> None:
    """Fail early with an actionable error for non-SQLite Kanban DB files.

    ``sqlite3.connect()`` creates missing and zero-byte files, so those are
    allowed. Existing non-empty files must have the SQLite header before we
    hand them to SQLite/WAL setup. This keeps corrupted page-0 failures from
    being collapsed into a generic PRAGMA error and lets the gateway's corrupt
    board handling identify the durable incident.

    Must never open-and-close the DB file with an ordinary descriptor — see
    :func:`_read_db_header`.
    """
    try:
        stat = path.stat()
    except FileNotFoundError:
        return
    except OSError:
        return
    if stat.st_size == 0:
        return
    try:
        head = _read_db_header(path)
    except OSError:
        return
    if head.startswith(_SQLITE_HEADER):
        return
    signature = ""
    if head.startswith(b"SQLit") and _looks_like_tls_record_at(head, 5):
        signature = " (TLS record header detected at byte offset 5)"
    elif _looks_like_tls_record_at(head, 0):
        signature = " (TLS record header detected at byte offset 0)"
    raise sqlite3.DatabaseError(
        "file is not a database: invalid SQLite header for "
        f"{path}{signature}; first_32={head[:32].hex(' ')}"
    )


class KanbanDbCorruptError(RuntimeError):
    """Raised when an existing kanban DB file fails integrity checks.

    Fail-closed guard against silent recreation of a corrupt board file,
    which would otherwise destroy the user's tasks. Carries both the
    original path and the canonical forensic backup made before refusing.
    """

    def __init__(
        self,
        db_path: Path,
        backup_path: Optional[Path],
        reason: str,
        *,
        incident_id: Optional[str] = None,
        incident: Optional["CorruptionIncident"] = None,
    ):
        self.db_path = db_path
        self.backup_path = backup_path
        self.reason = reason
        self.incident_id = incident_id or (
            incident.incident_id if incident is not None else None
        )
        self.incident = incident
        backup_str = str(backup_path) if backup_path is not None else "<backup failed>"
        preservation = (
            f"Original preserved; backup at {backup_str}."
            if backup_path is not None
            else "Original remains in place; forensic backup unavailable."
        )
        super().__init__(
            f"Refusing to open corrupt kanban DB at {db_path}: {reason}. "
            + preservation
            + (
                f" Incident: {self.incident_id}."
                if self.incident_id
                else ""
            )
        )


@dataclass(frozen=True)
class CorruptionIncident:
    """Durable identity for one corrupt DB file generation.

    The incident is intentionally independent of the bytes currently in the
    corrupt file. A board may continue to receive claim/crash events after a
    notifier detects corruption; those legitimate writes must not manufacture
    a new forensic epoch or backup for every changed digest.
    """

    incident_id: str
    db_path: Path
    dev: Optional[int]
    ino: Optional[int]
    reason: str
    detected_at: float
    backup_path: Optional[Path]
    preservation_status: str
    backup_sha256: Optional[str] = None

    @property
    def generation(self) -> "tuple[Optional[int], Optional[int]]":
        return self.dev, self.ino


CORRUPTION_MARKER_SUFFIX = ".corrupt.incident.json"
CORRUPTION_BACKUP_PREFIX = ".corrupt."
CORRUPTION_PRESERVATION_PENDING = "pending"
CORRUPTION_PRESERVATION_PUBLISHED = "published"
CORRUPTION_PRESERVATION_FAILED = "failed"


def _corruption_wall_clock() -> float:
    """Clock seam for incident timestamps and hermetic recovery tests."""
    return time.time()


def _resolved_db_path(path: Path) -> Path:
    """Resolve a DB path once and keep all incident artifacts beside it."""
    try:
        return path.expanduser().resolve()
    except OSError:
        return path.expanduser()


def _db_file_generation(path: Path) -> "tuple[Optional[int], Optional[int]]":
    """Return the file generation used to distinguish atomic replacements."""
    try:
        stat = path.stat()
    except OSError:
        return None, None
    return stat.st_dev, stat.st_ino


def corruption_incident_path(
    db_path: Optional[Path] = None,
    *,
    board: Optional[str] = None,
) -> Path:
    """Return the atomic incident marker path beside a board DB."""
    path = db_path if db_path is not None else kanban_db_path(board=board)
    resolved = _resolved_db_path(Path(path))
    return resolved.with_name(resolved.name + CORRUPTION_MARKER_SUFFIX)


def _incident_backup_path(path: Path, incident_id: str) -> Path:
    """Return the canonical main-file backup for ``incident_id``."""
    return path.with_name(
        path.name + f"{CORRUPTION_BACKUP_PREFIX}{incident_id}.bak"
    )


def _incident_to_payload(incident: CorruptionIncident) -> dict[str, Any]:
    return {
        "version": 1,
        "incident_id": incident.incident_id,
        "db_path": str(incident.db_path),
        "dev": incident.dev,
        "ino": incident.ino,
        "reason": incident.reason,
        "detected_at": incident.detected_at,
        "backup_path": str(incident.backup_path) if incident.backup_path else None,
        "preservation_status": incident.preservation_status,
        "backup_sha256": incident.backup_sha256,
    }


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Publish a small JSON marker without exposing a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = -1
    staged: Optional[Path] = None
    try:
        fd, name = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
        )
        staged = Path(name)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staged, path)
        staged = None
        try:
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
        except OSError:
            dir_fd = -1
        if dir_fd != -1:
            try:
                os.fsync(dir_fd)
            except OSError:
                pass
            finally:
                os.close(dir_fd)
    except OSError:
        if fd != -1:
            try:
                os.close(fd)
            except OSError:
                pass
        if staged is not None:
            try:
                staged.unlink()
            except OSError:
                pass
        raise


def _read_corruption_incident(path: Path) -> Optional[CorruptionIncident]:
    """Read and validate an incident marker; malformed markers are ignored."""
    resolved = _resolved_db_path(path)
    marker = corruption_incident_path(resolved)
    try:
        raw = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        incident_id = str(raw["incident_id"]).strip()
        marker_db_path = _resolved_db_path(Path(str(raw["db_path"])))
        dev = raw.get("dev")
        ino = raw.get("ino")
        dev = int(dev) if dev is not None else None
        ino = int(ino) if ino is not None else None
        reason = str(raw["reason"])
        detected_at = float(raw["detected_at"])
        backup_raw = raw.get("backup_path")
        backup_path = (
            _resolved_db_path(Path(str(backup_raw)))
            if backup_raw
            else None
        )
        preservation_status = str(raw.get("preservation_status") or "pending")
        backup_sha256 = raw.get("backup_sha256")
        if backup_sha256 is not None:
            backup_sha256 = str(backup_sha256)
    except (KeyError, OSError, TypeError, ValueError):
        return None
    if not incident_id or marker_db_path != resolved:
        return None
    if backup_path is not None and backup_path.parent != resolved.parent:
        return None
    return CorruptionIncident(
        incident_id=incident_id,
        db_path=resolved,
        dev=dev,
        ino=ino,
        reason=reason,
        detected_at=detected_at,
        backup_path=backup_path,
        preservation_status=preservation_status,
        backup_sha256=backup_sha256,
    )


def read_corruption_incident(
    db_path: Optional[Path] = None,
    *,
    board: Optional[str] = None,
) -> Optional[CorruptionIncident]:
    """Return the active incident marker for a board, if one is published."""
    path = db_path if db_path is not None else kanban_db_path(board=board)
    return _read_corruption_incident(Path(path))


def _publish_incident_backup(
    incident: CorruptionIncident,
) -> "tuple[str, Optional[str]]":
    """Publish the one canonical forensic image for an incident.

    The live DB and sidecars are first copied through a separate process. All
    visible backup files are then published with ``os.replace`` from private
    staged inodes, so a failed copy can leave no partial canonical artifact.
    """
    backup = incident.backup_path
    if backup is None:
        return CORRUPTION_PRESERVATION_FAILED, None
    source = incident.db_path
    staged = _stage_off_lock(source)
    if staged is None:
        return CORRUPTION_PRESERVATION_FAILED, None
    source_digest: Optional[str] = None
    sidecar_stages: list[tuple[Path, Path]] = []
    try:
        source_digest = _file_sha256(staged)
        if source_digest is None or not _atomic_copy2(staged, backup):
            return CORRUPTION_PRESERVATION_FAILED, source_digest
        for suffix in ("-wal", "-shm"):
            sidecar = source.with_name(source.name + suffix)
            if not sidecar.exists():
                continue
            staged_sidecar = _stage_off_lock(sidecar)
            if staged_sidecar is None:
                return CORRUPTION_PRESERVATION_FAILED, source_digest
            sidecar_stages.append((staged_sidecar, backup.with_name(backup.name + suffix)))
        for staged_sidecar, sidecar_backup in sidecar_stages:
            if not _atomic_copy2(staged_sidecar, sidecar_backup):
                return CORRUPTION_PRESERVATION_FAILED, source_digest
        return CORRUPTION_PRESERVATION_PUBLISHED, source_digest
    finally:
        try:
            staged.unlink()
        except OSError:
            pass
        for staged_sidecar, _destination in sidecar_stages:
            try:
                staged_sidecar.unlink()
            except OSError:
                pass


def _ensure_corruption_incident_locked(
    path: Path,
    reason: str,
    *,
    detected_at: Optional[float] = None,
) -> CorruptionIncident:
    """Create or reuse an incident while the cross-process init lock is held."""
    resolved = _resolved_db_path(path)
    generation = _db_file_generation(resolved)
    existing = _read_corruption_incident(resolved)
    if (
        existing is not None
        and existing.generation == generation
        and generation != (None, None)
    ):
        incident = existing
        backup_is_valid = (
            incident.backup_path is not None
            and incident.backup_path.exists()
            and (
                incident.backup_sha256 is None
                or _file_sha256(incident.backup_path) == incident.backup_sha256
            )
        )
        if incident.preservation_status == CORRUPTION_PRESERVATION_PUBLISHED:
            if backup_is_valid:
                return incident
        incident = replace(
            incident,
            reason=incident.reason or str(reason),
            preservation_status=CORRUPTION_PRESERVATION_PENDING,
        )
    else:
        incident_id = secrets.token_hex(16)
        incident = CorruptionIncident(
            incident_id=incident_id,
            db_path=resolved,
            dev=generation[0],
            ino=generation[1],
            reason=str(reason),
            detected_at=(
                _corruption_wall_clock()
                if detected_at is None
                else float(detected_at)
            ),
            backup_path=_incident_backup_path(resolved, incident_id),
            preservation_status=CORRUPTION_PRESERVATION_PENDING,
        )
    marker = corruption_incident_path(resolved)
    _atomic_write_json(marker, _incident_to_payload(incident))
    status, digest = _publish_incident_backup(incident)
    incident = replace(
        incident,
        preservation_status=status,
        backup_sha256=digest,
    )
    _atomic_write_json(marker, _incident_to_payload(incident))
    _log.warning(
        "kanban corruption incident %s for %s: preservation=%s backup=%s",
        incident.incident_id,
        resolved,
        incident.preservation_status,
        incident.backup_path,
    )
    return incident


def ensure_corruption_incident(
    db_path: Optional[Path] = None,
    reason: str = "corruption detected",
    *,
    board: Optional[str] = None,
    detected_at: Optional[float] = None,
) -> CorruptionIncident:
    """Publish or reuse the durable incident for a corrupt board DB."""
    path = Path(db_path) if db_path is not None else kanban_db_path(board=board)
    resolved = _resolved_db_path(path)
    with _cross_process_init_lock(resolved):
        return _ensure_corruption_incident_locked(
            resolved, reason, detected_at=detected_at,
        )


def _corrupt_error_for_incident(incident: CorruptionIncident) -> KanbanDbCorruptError:
    backup_path = (
        incident.backup_path
        if (
            incident.preservation_status == CORRUPTION_PRESERVATION_PUBLISHED
            and incident.backup_path is not None
            and incident.backup_path.exists()
        )
        else None
    )
    return KanbanDbCorruptError(
        incident.db_path,
        backup_path,
        incident.reason,
        incident_id=incident.incident_id,
        incident=incident,
    )


def _clear_corruption_incident_locked(
    path: Path,
    *,
    incident_id: Optional[str] = None,
) -> bool:
    resolved = _resolved_db_path(path)
    current = _read_corruption_incident(resolved)
    if current is None:
        return False
    if incident_id is not None and current.incident_id != incident_id:
        return False
    marker = corruption_incident_path(resolved)
    try:
        marker.unlink()
    except FileNotFoundError:
        return False
    _INITIALIZED_PATHS.discard(str(resolved))
    return True


def clear_corruption_incident(
    db_path: Optional[Path] = None,
    *,
    board: Optional[str] = None,
    incident_id: Optional[str] = None,
) -> bool:
    """Clear an incident after a verified healthy epoch is installed."""
    path = Path(db_path) if db_path is not None else kanban_db_path(board=board)
    resolved = _resolved_db_path(path)
    with _cross_process_init_lock(resolved):
        return _clear_corruption_incident_locked(
            resolved, incident_id=incident_id,
        )


def _guard_cached_db_header(path: Path) -> None:
    """Quarantine a newly-corrupt cached DB before SQLite opens it.

    ``_INITIALIZED_PATHS`` avoids an expensive integrity scan on every Kanban
    operation, but a long-lived gateway can keep that cache after an external
    writer has clobbered page one. Re-run only the 64-byte header check on the
    fast path so the exact TLS FD-recycle shape fails closed with a preserved
    forensic image instead of surfacing later as an unclassified raw SQLite
    error from WAL setup.
    """
    try:
        _validate_sqlite_header(path)
    except sqlite3.DatabaseError as exc:
        try:
            incident = ensure_corruption_incident(path, str(exc))
        except OSError as marker_exc:
            raise KanbanDbCorruptError(
                _resolved_db_path(path),
                None,
                f"{exc}; incident marker publication failed: {marker_exc}",
            ) from exc
        raise _corrupt_error_for_incident(incident) from exc


def _file_sha256(path: Path) -> Optional[str]:
    """Return a file digest without loading forensic images into memory."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _copy_off_lock(source: Path, destination: Path) -> bool:
    """Copy ``source`` to ``destination`` without ever holding an in-process fd
    to ``source``.

    BUILD-567: POSIX fcntl locks are per-process — closing ANY descriptor to an
    inode releases every lock the whole process holds on it (SQLite "How To
    Corrupt", §2.2). ``_backup_corrupt_db`` runs inside the long-lived gateway,
    which concurrently holds SQLite write/checkpoint locks on the same board DB
    from other (notifier) threads. Reading the live DB in-process
    (``path.open`` / ``shutil.copy2``) would drop those locks mid-checkpoint and
    let a second checkpointer backfill the notifier btrees concurrently — the
    exact stale-generation corruption of ``kanban_notify_subs`` /
    ``notify_deliveries`` seen on 2026-07-19. A *separate* process's
    open()/close() cannot touch this process's locks, so shell out to ``cp``.
    Same rationale as the never-closed fd in :func:`_read_db_header`.
    """
    if os.name == "nt":
        # Windows has no POSIX close-drops-locks hazard (see _read_db_header);
        # a plain in-process copy is safe there.
        try:
            shutil.copy2(source, destination)
            return True
        except OSError:
            return False
    try:
        result = subprocess.run(  # noqa: S603,S607 -- fixed argv, no shell
            ["cp", str(source), str(destination)],
            capture_output=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _stage_off_lock(source: Path) -> Optional[Path]:
    """Copy a live (possibly SQLite-locked) file to a private temp beside it via
    a subprocess, returning the temp path (caller unlinks) or ``None``.

    The temp is a fresh inode, so the in-process ``mkstemp`` open/close cannot
    drop any lock on ``source``. See :func:`_copy_off_lock`.
    """
    parent = source.parent
    try:
        fd, name = tempfile.mkstemp(
            dir=str(parent), prefix=f".{source.name}.staging.", suffix=".tmp"
        )
        os.close(fd)
    except OSError:
        return None
    staged = Path(name)
    if not _copy_off_lock(source, staged):
        try:
            staged.unlink()
        except OSError:
            pass
        return None
    return staged


def _atomic_copy2(source: Path, destination: Path) -> bool:
    """Copy ``source`` to ``destination`` without publishing partial bytes.

    ``source`` must be a file no other thread holds a SQLite lock on (a private
    staged copy or an existing backup) — it is read with an in-process fd. Never
    pass the live board DB here; use :func:`_stage_off_lock` first (BUILD-567).
    """
    temp_fd = -1
    staged: Optional[Path] = None
    try:
        temp_fd, temp_name = tempfile.mkstemp(
            dir=str(destination.parent),
            prefix=f".{destination.name}.",
            suffix=".tmp",
        )
        staged = Path(temp_name)
        os.close(temp_fd)
        temp_fd = -1  # closed; sentinel cleared so we don't double-close
        shutil.copy2(source, staged)
        os.replace(staged, destination)
    except OSError:
        if temp_fd != -1:
            try:
                os.close(temp_fd)
            except OSError:
                pass
        if staged is not None:
            try:
                staged.unlink()
            except OSError:
                pass
        return False
    return True


def _backup_corrupt_db(path: Path) -> Optional[Path]:
    """Compatibility wrapper for the durable incident publisher.

    Older internal callers keep this name, but the live guard uses the random
    incident marker and canonical backup rather than a byte fingerprint.
    """
    try:
        incident = ensure_corruption_incident(path, "corruption detected")
    except OSError:
        return None
    if (
        incident.preservation_status != CORRUPTION_PRESERVATION_PUBLISHED
        or incident.backup_path is None
        or not incident.backup_path.exists()
    ):
        return None
    return incident.backup_path


def _guard_existing_db_is_healthy(path: Path) -> None:
    """Run ``PRAGMA integrity_check`` on an existing non-empty DB file.

    Opens the probe in read/write mode so SQLite can recover or
    checkpoint a healthy WAL/hot-journal DB before we declare it
    corrupt. If the file is malformed, copy it (and any WAL/SHM
    sidecars) to the incident's canonical backup and raise
    :class:`KanbanDbCorruptError` so callers cannot silently recreate
    the schema on top of a damaged DB.

    Transient lock/busy errors (``sqlite3.OperationalError``) are NOT
    treated as corruption; they propagate raw so the caller sees a
    normal lock failure and no spurious ``.corrupt`` backup is made.

    No-op for missing files, zero-byte files (treated as fresh), and
    paths already proven healthy this process (cache hit).

    Path-trust note: ``path`` arrives via :func:`connect`, which itself
    resolves it from an explicit ``db_path`` argument, the
    :func:`kanban_db_path` env-var chain, or the kanban-home default —
    all sources Hermes treats as user-controlled-but-trusted on the
    user's own machine. We additionally resolve the path here and
    confine all filesystem writes to its parent directory so any
    accidental ``..`` segments are collapsed before any I/O happens.
    """
    _guard_existing_db_is_healthy_with_options(path)


def _is_corrupt_sqlite_error(exc: BaseException) -> bool:
    """Recognize only the SQLite messages that prove structural corruption."""
    if not isinstance(exc, sqlite3.DatabaseError):
        return False
    message = str(exc).lower()
    return (
        "file is not a database" in message
        or "database disk image is malformed" in message
    )


def _integrity_probe_reason(path: Path) -> Optional[str]:
    """Return a corruption reason, or ``None`` for a healthy existing DB."""
    resolved = _resolved_db_path(path)
    try:
        if not resolved.exists() or resolved.stat().st_size == 0:
            return None
    except OSError:
        return None
    try:
        _validate_sqlite_header(resolved)
    except sqlite3.DatabaseError as exc:
        if not _is_corrupt_sqlite_error(exc):
            raise
        return f"sqlite refused to open file: {exc}"
    probe = None
    try:
        probe = _sqlite_connect(resolved)
        row = probe.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.OperationalError as exc:
        # ``database is locked`` / ``disk I/O error`` remain ordinary transient
        # failures. SQLite reports the malformed-image class as OperationalError
        # too, so classify only the narrow structural messages above.
        if not _is_corrupt_sqlite_error(exc):
            raise
        return f"sqlite refused to open file: {exc}"
    except sqlite3.DatabaseError as exc:
        if not _is_corrupt_sqlite_error(exc):
            raise
        return f"sqlite refused to open file: {exc}"
    finally:
        if probe is not None:
            probe.close()
    if not row or (row[0] or "").lower() != "ok":
        return f"integrity_check returned {row[0] if row else '<no row>'!r}"
    return None


def _guard_existing_db_is_healthy_with_options(
    path: Path,
    *,
    force: bool = False,
    active_incident: Optional[CorruptionIncident] = None,
    lock_held: bool = False,
) -> None:
    """Integrity-guard one DB, optionally forcing a healing probe."""
    resolved = _resolved_db_path(path)
    try:
        if not resolved.exists() or resolved.stat().st_size == 0:
            return
    except OSError:
        return
    if not force and str(resolved) in _INITIALIZED_PATHS:
        return
    reason = _integrity_probe_reason(resolved)
    if reason is None:
        if active_incident is not None:
            if lock_held:
                _clear_corruption_incident_locked(
                    resolved, incident_id=active_incident.incident_id,
                )
            else:
                clear_corruption_incident(
                    resolved, incident_id=active_incident.incident_id,
                )
        return
    try:
        if lock_held:
            incident = _ensure_corruption_incident_locked(resolved, reason)
        else:
            incident = ensure_corruption_incident(resolved, reason)
    except OSError as marker_exc:
        raise KanbanDbCorruptError(
            resolved,
            None,
            f"{reason}; incident marker publication failed: {marker_exc}",
        ) from marker_exc
    raise _corrupt_error_for_incident(incident)


def probe_corruption_incident(
    db_path: Optional[Path] = None,
    *,
    board: Optional[str] = None,
) -> bool:
    """Force a full health probe for an active incident.

    ``connect()`` deliberately refuses an unchanged incident generation. The
    dispatcher calls this function when quarantine expires so an operator's
    in-place repair can be recognized without restarting the gateway. A
    changed inode is probed through the same path; a healthy result clears the
    marker, while a corrupt result preserves or advances the incident.
    """
    path = Path(db_path) if db_path is not None else kanban_db_path(board=board)
    resolved = _resolved_db_path(path)
    with _cross_process_init_lock(resolved):
        incident = _read_corruption_incident(resolved)
        if incident is None:
            return True
        current_generation = _db_file_generation(resolved)
        if current_generation == (None, None):
            return False
        try:
            _guard_existing_db_is_healthy_with_options(
                resolved,
                force=True,
                active_incident=incident,
                lock_held=True,
            )
        except KanbanDbCorruptError:
            return False
        return _read_corruption_incident(resolved) is None


def connect(
    db_path: Optional[Path] = None,
    *,
    board: Optional[str] = None,
) -> sqlite3.Connection:
    """Open (and initialize if needed) the kanban DB.

    WAL mode is enabled on every connection; it's a no-op after the first
    time but keeps the code robust if the DB file is ever re-created.

    The first connection to a given path auto-runs :func:`init_db` so
    fresh installs and test harnesses that construct `connect()`
    directly don't have to remember a separate init step. Subsequent
    connections skip the schema check via a module-level path cache.

    Path resolution:

    * ``db_path`` explicit → used as-is (legacy callers, tests).
    * ``board`` explicit → resolves to that board's DB.
    * Neither → :func:`kanban_db_path` resolves via
      ``HERMES_KANBAN_DB`` env → ``HERMES_KANBAN_BOARD`` env →
      ``<root>/kanban/current`` → ``default``.
    """
    if db_path is not None:
        path = db_path
    else:
        path = kanban_db_path(board=board)
    path.parent.mkdir(parents=True, exist_ok=True)

    # The marker is checked before SQLite opens the file. An unchanged active
    # generation is a board-wide quarantine: workers and gateway threads all
    # receive the same incident instead of opening the still-writable corrupt
    # image and producing a new incident backup. A changed generation
    # takes the slow path below, where a real integrity probe either heals the
    # marker or publishes a new incident.
    resolved_path = _resolved_db_path(path)
    active_incident = _read_corruption_incident(resolved_path)
    if active_incident is not None:
        current_generation = _db_file_generation(resolved_path)
        if (
            current_generation == (None, None)
            or current_generation == active_incident.generation
        ):
            if (
                active_incident.preservation_status
                != CORRUPTION_PRESERVATION_PUBLISHED
                or active_incident.backup_path is None
                or not active_incident.backup_path.exists()
            ):
                try:
                    active_incident = ensure_corruption_incident(
                        resolved_path, active_incident.reason,
                    )
                except OSError as marker_exc:
                    raise KanbanDbCorruptError(
                        resolved_path,
                        None,
                        f"{active_incident.reason}; incident marker publication "
                        f"failed: {marker_exc}",
                        incident_id=active_incident.incident_id,
                        incident=active_incident,
                    ) from marker_exc
            raise _corrupt_error_for_incident(active_incident)

    # Fast path: once THIS process has initialized this path, the expensive
    # first-open work (header validation, integrity probe, schema + additive
    # migrations) is already done and cached in _INITIALIZED_PATHS. Acquiring
    # the cross-process init lock on every connect is what let a single stalled
    # holder (e.g. an external `hermes kanban list` mid-integrity-probe) block
    # the long-lived gateway dispatcher's next-tick connect() forever — an
    # unbounded flock with no timeout, no LOCK_NB, no recovery (#36644). On the
    # steady-state path there is nothing for the cross-process lock to protect
    # (no schema/migration writes run), so skip it entirely and just open the
    # connection with WAL/pragmas under the cheap in-process _INIT_LOCK.
    resolved = str(resolved_path)
    if resolved in _INITIALIZED_PATHS and active_incident is None:
        # Preserve the cached fast path while detecting an external page-one
        # overwrite before SQLite's WAL setup turns it into an opaque error.
        _guard_cached_db_header(path)
        conn = _sqlite_connect(path)
        try:
            conn.row_factory = sqlite3.Row
            with _INIT_LOCK:
                from hermes_state import apply_wal_with_fallback
                apply_wal_with_fallback(conn, db_label=f"kanban.db ({path.name})")
                conn.execute("PRAGMA synchronous=FULL")
                conn.execute("PRAGMA wal_autocheckpoint=100")
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("PRAGMA secure_delete=ON")
                conn.execute("PRAGMA cell_size_check=ON")
        except Exception:
            conn.close()
            raise
        return conn

    with _cross_process_init_lock(path):
        # Re-read the marker after taking the cross-process lock: another
        # detector may have published it since the cheap pre-check above.
        active_incident = _read_corruption_incident(path)
        current_generation = _db_file_generation(path)
        if active_incident is not None:
            if (
                current_generation == (None, None)
                or current_generation == active_incident.generation
            ):
                if (
                    active_incident.preservation_status
                    != CORRUPTION_PRESERVATION_PUBLISHED
                    or active_incident.backup_path is None
                    or not active_incident.backup_path.exists()
                ):
                    try:
                        active_incident = _ensure_corruption_incident_locked(
                            path, active_incident.reason,
                        )
                    except OSError as marker_exc:
                        raise KanbanDbCorruptError(
                            resolved_path,
                            None,
                            f"{active_incident.reason}; incident marker "
                            f"publication failed: {marker_exc}",
                            incident_id=active_incident.incident_id,
                            incident=active_incident,
                        ) from marker_exc
                raise _corrupt_error_for_incident(active_incident)
            # An external atomic replacement or in-place repair invalidates the
            # old generation. Force the integrity probe before clearing it.
            _guard_existing_db_is_healthy_with_options(
                path,
                force=True,
                active_incident=active_incident,
                lock_held=True,
            )
        # Full integrity probe — catches corruption past the header (malformed
        # pages, broken internal metadata). Cached per-path after first success
        # via _INITIALIZED_PATHS so it only runs once per process per path.
        _guard_existing_db_is_healthy_with_options(
            path,
            force=False,
            lock_held=True,
        )
        resolved = str(resolved_path)
        conn = _sqlite_connect(path)
        try:
            conn.row_factory = sqlite3.Row
            with _INIT_LOCK:
                # WAL activation can take an exclusive lock while SQLite creates the
                # sidecar files for a fresh database. Keep it in the same process-local
                # critical section as schema initialization so concurrent gateway
                # startup threads do not race before _INITIALIZED_PATHS is populated.
                # WAL doesn't work on network filesystems (NFS/SMB/FUSE). Shared helper
                # falls back to DELETE with one WARNING so kanban stays usable there.
                # See hermes_state._WAL_INCOMPAT_MARKERS for detection logic.
                from hermes_state import apply_wal_with_fallback
                apply_wal_with_fallback(conn, db_label=f"kanban.db ({path.name})")
                # FULL (was NORMAL): fsync before each checkpoint to narrow the
                # crash window that can leave a b-tree page header torn.
                conn.execute("PRAGMA synchronous=FULL")
                conn.execute("PRAGMA wal_autocheckpoint=100")
                conn.execute("PRAGMA foreign_keys=ON")
                # Zero freed pages so a later torn write cannot expose stale
                # cell content; persisted in the DB header for new DBs.
                conn.execute("PRAGMA secure_delete=ON")
                # Surface corrupt cells as read errors instead of silent
                # wrong-data returns.
                conn.execute("PRAGMA cell_size_check=ON")
                needs_init = resolved not in _INITIALIZED_PATHS
                if needs_init:
                    # Idempotent: runs CREATE TABLE IF NOT EXISTS + the additive
                    # migrations. Cached so subsequent connect() calls in the same
                    # process are cheap. The lock prevents same-process dispatcher
                    # threads from racing through the additive ALTER TABLE pass with
                    # stale PRAGMA snapshots during gateway startup.
                    conn.executescript(SCHEMA_SQL)
                    _migrate_add_optional_columns(conn)
                    _INITIALIZED_PATHS.add(resolved)
        except Exception:
            conn.close()
            raise
    return conn


@contextlib.contextmanager
def connect_closing(
    db_path: Optional[Path] = None,
    *,
    board: Optional[str] = None,
):
    """Open a kanban DB connection and guarantee it is closed on exit.

    Use this instead of ``with kb.connect() as conn:`` — sqlite3's
    built-in connection context manager only commits/rollbacks the
    transaction; it does NOT close the file descriptor. In long-lived
    processes (gateway, dashboard) that route every kanban operation
    through ``connect()`` (e.g. ``run_slash`` dispatching ``/kanban …``
    commands, ``decompose_task_endpoint`` calling
    ``kanban_decompose.decompose_task``), the unclosed connections
    accumulate as open FDs to ``kanban.db`` and ``kanban.db-wal``. After
    enough operations the process hits the kernel FD limit and dies
    with ``[Errno 24] Too many open files``.

    See #33159 for the production incident.

    The ``connect()`` function itself remains unchanged so callers that
    intentionally manage the connection lifetime (tests, long-lived
    callers) continue to work.
    """
    conn = connect(db_path=db_path, board=board)
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass


def init_db(
    db_path: Optional[Path] = None,
    *,
    board: Optional[str] = None,
) -> Path:
    """Create the schema if it doesn't exist; return the path used.

    Kept as a public entry point so CLI ``hermes kanban init`` and the
    daemon have something explicit to call. Unlike :func:`connect`'s
    first-time auto-init (which caches by path), ``init_db`` always
    re-runs the migration pass. Callers that know the on-disk schema
    may have drifted — tests that write legacy event kinds directly,
    external tools that upgrade an old DB file — can call this to
    force re-migration.
    """
    if db_path is not None:
        path = db_path
    else:
        path = kanban_db_path(board=board)
    path.parent.mkdir(parents=True, exist_ok=True)
    resolved = str(path.resolve())
    # Clear the cache entry so the underlying connect() re-runs the
    # schema + migration pass unconditionally.
    with _INIT_LOCK:
        _INITIALIZED_PATHS.discard(resolved)
    with contextlib.closing(connect(path)):
        pass
    return path


def _migrate_add_optional_columns(conn: sqlite3.Connection) -> None:
    """Add columns that were introduced after v1 release to legacy DBs.

    Called by ``init_db`` so opening an old DB is always safe.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
    if "tenant" not in cols:
        _add_column_if_missing(conn, "tasks", "tenant", "tenant TEXT")
    if "result" not in cols:
        _add_column_if_missing(conn, "tasks", "result", "result TEXT")
    if "branch_name" not in cols:
        _add_column_if_missing(conn, "tasks", "branch_name", "branch_name TEXT")
    if "project_id" not in cols:
        _add_column_if_missing(conn, "tasks", "project_id", "project_id TEXT")
    if "idempotency_key" not in cols:
        _add_column_if_missing(
            conn, "tasks", "idempotency_key", "idempotency_key TEXT"
        )
    # ``idx_tasks_idempotency`` is created unconditionally below alongside
    # the other additive-column indexes — see the block after the
    # legacy-column migration. Creating it here too would be redundant.

    # Refresh after early additive migrations above. Some existing DBs were
    # partially migrated in older releases and can already contain the later
    # columns (for example ``consecutive_failures``) even when this function's
    # initial snapshot did not. Re-snapshot here so the legacy-column migration
    # below is truly idempotent and never re-adds columns that already exist.
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}

    # Legacy column migration: ``spawn_failures`` → ``consecutive_failures``
    # and ``last_spawn_error`` → ``last_failure_error``.
    #
    # Avoid ``ALTER TABLE ... RENAME COLUMN`` for two reasons:
    #   1. Primary: very old DBs may never have had ``spawn_failures`` at
    #      all, so RENAME raises OperationalError: no such column (the crash
    #      reported in issue #20842 after the #20410 update).
    #   2. Secondary: SQLite reparses the whole schema on any RENAME, which
    #      fails if related objects (views, triggers) reference the old name.
    #
    # ADD-first-then-copy is tolerant of both shapes and preserves
    # historical counter values when the legacy columns do exist.
    if "consecutive_failures" not in cols:
        added = _add_column_if_missing(
            conn,
            "tasks",
            "consecutive_failures",
            "consecutive_failures INTEGER NOT NULL DEFAULT 0",
        )
        if added and "spawn_failures" in cols:
            conn.execute(
                "UPDATE tasks SET consecutive_failures = COALESCE(spawn_failures, 0)"
            )
    if "worker_pid" not in cols:
        _add_column_if_missing(conn, "tasks", "worker_pid", "worker_pid INTEGER")
    if "worker_started_at" not in cols:
        _add_column_if_missing(
            conn, "tasks", "worker_started_at", "worker_started_at REAL"
        )
    if "worker_pgid" not in cols:
        _add_column_if_missing(conn, "tasks", "worker_pgid", "worker_pgid INTEGER")
    if "worker_sid" not in cols:
        _add_column_if_missing(conn, "tasks", "worker_sid", "worker_sid INTEGER")
    if "last_failure_error" not in cols:
        added = _add_column_if_missing(
            conn, "tasks", "last_failure_error", "last_failure_error TEXT"
        )
        if added and "last_spawn_error" in cols:
            conn.execute(
                "UPDATE tasks SET last_failure_error = last_spawn_error"
            )
    if "max_runtime_seconds" not in cols:
        _add_column_if_missing(
            conn, "tasks", "max_runtime_seconds", "max_runtime_seconds INTEGER"
        )
    if "last_heartbeat_at" not in cols:
        _add_column_if_missing(
            conn, "tasks", "last_heartbeat_at", "last_heartbeat_at INTEGER"
        )
    if "current_run_id" not in cols:
        _add_column_if_missing(
            conn, "tasks", "current_run_id", "current_run_id INTEGER"
        )
    if "workflow_template_id" not in cols:
        _add_column_if_missing(
            conn, "tasks", "workflow_template_id", "workflow_template_id TEXT"
        )
    if "current_step_key" not in cols:
        _add_column_if_missing(
            conn, "tasks", "current_step_key", "current_step_key TEXT"
        )
    if "skills" not in cols:
        # JSON array of skill names the dispatcher force-loads into the
        # worker via --skills. NULL is fine for existing rows.
        _add_column_if_missing(conn, "tasks", "skills", "skills TEXT")
    if "toolsets" not in cols:
        _add_column_if_missing(conn, "tasks", "toolsets", "toolsets TEXT")

    if "max_retries" not in cols:
        # Per-task override for the consecutive-failure circuit breaker.
        # NULL = fall through to the dispatcher-level ``kanban.failure_limit``
        # config, then ``DEFAULT_FAILURE_LIMIT``. Existing rows get NULL,
        # which is the correct default (they keep the global behaviour
        # they were getting before the column existed).
        _add_column_if_missing(conn, "tasks", "max_retries", "max_retries INTEGER")

    if "model_override" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN model_override TEXT")
    if "model_provider_override" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN model_provider_override TEXT")
    if "model_reasoning_effort" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN model_reasoning_effort TEXT")

    if "goal_mode" not in cols:
        # Ralph-style goal loop toggle for the dispatched worker. 0 (the
        # default) = classic single-shot worker, preserving the behaviour
        # existing rows had before the column existed.
        _add_column_if_missing(
            conn, "tasks", "goal_mode", "goal_mode INTEGER NOT NULL DEFAULT 0"
        )

    if "goal_max_turns" not in cols:
        # Per-task goal-loop turn budget. NULL = goals-engine default.
        _add_column_if_missing(
            conn, "tasks", "goal_max_turns", "goal_max_turns INTEGER"
        )

    if "session_id" not in cols:
        # Originating agent/chat session id, populated when the task is
        # created from within an agent loop that propagated
        # ``HERMES_SESSION_ID`` (e.g. ACP). NULL on legacy rows and on any
        # creation path that doesn't set the env var (CLI, dashboard).
        _add_column_if_missing(
            conn, "tasks", "session_id", "session_id TEXT"
        )
    if "workflow_key" not in cols:
        # Lightweight grouping key for tasks that belong to the same
        # orchestrator-created workflow. Enables the ``hermes kanban
        # workflow`` command and downstream aggregation without requiring
        # a separate workflow table. NULL on legacy rows and for tasks
        # created outside an explicit workflow.
        _add_column_if_missing(
            conn, "tasks", "workflow_key", "workflow_key TEXT"
        )

    if "block_kind" not in cols:
        # Typed block reason (VALID_BLOCK_KINDS), the kernel-owned
        # dependency_pending projection, or NULL for legacy/un-typed blocks.
        # Existing blocked rows get NULL, which is treated as a generic human
        # blocker — same behaviour they had before the column.
        _add_column_if_missing(conn, "tasks", "block_kind", "block_kind TEXT")

    if "block_recurrences" not in cols:
        # Unblock-loop counter. Existing rows start at 0, so the loop breaker
        # only begins counting from the first re-block after this migration.
        _add_column_if_missing(
            conn,
            "tasks",
            "block_recurrences",
            "block_recurrences INTEGER NOT NULL DEFAULT 0",
        )
    if "policy_quarantined" not in cols:
        _add_column_if_missing(
            conn, "tasks", "policy_quarantined", "policy_quarantined INTEGER NOT NULL DEFAULT 0"
        )
    if "policy_invalidated" not in cols:
        _add_column_if_missing(
            conn, "tasks", "policy_invalidated", "policy_invalidated INTEGER NOT NULL DEFAULT 0"
        )
    if "policy_quarantine_reason" not in cols:
        _add_column_if_missing(conn, "tasks", "policy_quarantine_reason", "policy_quarantine_reason TEXT")
    if "publication_expected_sha" not in cols:
        _add_column_if_missing(
            conn, "tasks", "publication_expected_sha", "publication_expected_sha TEXT"
        )
    if "publication_remote" not in cols:
        _add_column_if_missing(
            conn, "tasks", "publication_remote", "publication_remote TEXT"
        )
    if "publication_ref" not in cols:
        _add_column_if_missing(
            conn, "tasks", "publication_ref", "publication_ref TEXT"
        )
    if "workspace_managed" not in cols:
        _add_column_if_missing(
            conn,
            "tasks",
            "workspace_managed",
            "workspace_managed INTEGER NOT NULL DEFAULT 0",
        )
    if "workspace_repo_root" not in cols:
        _add_column_if_missing(
            conn, "tasks", "workspace_repo_root", "workspace_repo_root TEXT"
        )
    if "workspace_repo_common_dir" not in cols:
        _add_column_if_missing(
            conn,
            "tasks",
            "workspace_repo_common_dir",
            "workspace_repo_common_dir TEXT",
        )
    if "workspace_cleanup_lease" not in cols:
        _add_column_if_missing(
            conn,
            "tasks",
            "workspace_cleanup_lease",
            "workspace_cleanup_lease TEXT",
        )
    if "workspace_cleanup_lease_expires" not in cols:
        _add_column_if_missing(
            conn,
            "tasks",
            "workspace_cleanup_lease_expires",
            "workspace_cleanup_lease_expires INTEGER",
        )

    discovery_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'discovery_capabilities'"
    ).fetchone()
    if discovery_table is not None:
        discovery_cols = {row["name"] for row in conn.execute("PRAGMA table_info(discovery_capabilities)")}
        if "expires_at" not in discovery_cols:
            _add_column_if_missing(
                conn, "discovery_capabilities", "expires_at", "expires_at INTEGER NOT NULL DEFAULT 0"
            )

    gate_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'architecture_gates'"
    ).fetchone()
    if gate_table is not None:
        gate_cols = {row["name"] for row in conn.execute("PRAGMA table_info(architecture_gates)")}
        for name, definition in (
            ("creator_actor_type", "creator_actor_type TEXT"),
            ("creator_profile", "creator_profile TEXT"),
            ("approval_actor_id", "approval_actor_id TEXT"),
            ("approval_actor_type", "approval_actor_type TEXT"),
            ("approval_surface", "approval_surface TEXT"),
            ("approved_digest", "approved_digest TEXT"),
            ("approved_at", "approved_at INTEGER"),
            ("approval_review_task_id", "approval_review_task_id TEXT"),
            (
                "approval_review_completion_event_id",
                "approval_review_completion_event_id INTEGER",
            ),
            ("approval_artifact_generation", "approval_artifact_generation INTEGER"),
            ("approval_artifact_sha256", "approval_artifact_sha256 TEXT"),
            ("authorization_event_id", "authorization_event_id INTEGER"),
        ):
            if name not in gate_cols:
                _add_column_if_missing(conn, "architecture_gates", name, definition)

    # Indexes over additive ``tasks`` columns must be created after the
    # columns exist. Keeping them in SCHEMA_SQL breaks legacy boards: SQLite
    # parses each statement in ``executescript`` against the live schema, so a
    # ``CREATE INDEX`` over a missing column aborts initialization before the
    # additive ``ALTER TABLE`` migrations below can run. Re-running them here
    # is cheap thanks to ``IF NOT EXISTS`` and stays correct on fresh DBs
    # (where the columns already exist from SCHEMA_SQL).
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_tenant ON tasks(tenant)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_idempotency ON tasks(idempotency_key)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_session_id ON tasks(session_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_workflow_key ON tasks(workflow_key)"
    )

    # task_events gained a run_id column; back-fill it as NULL for
    # historical events (they predate runs and can't be attributed).
    ev_cols = {row["name"] for row in conn.execute("PRAGMA table_info(task_events)")}
    if "run_id" not in ev_cols:
        _add_column_if_missing(conn, "task_events", "run_id", "run_id INTEGER")

    # Same ordering rule as the additive ``tasks`` indexes above: create the
    # index after the additive column migration so legacy ``task_events``
    # tables don't fail during SCHEMA_SQL execution before ``run_id`` exists.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_run "
        "ON task_events(run_id, id)"
    )

    notify_table_exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='kanban_notify_subs'"
    ).fetchone() is not None
    if notify_table_exists:
        notify_cols = {
            row["name"] for row in conn.execute("PRAGMA table_info(kanban_notify_subs)")
        }
        if "notifier_profile" not in notify_cols:
            _add_column_if_missing(
                conn, "kanban_notify_subs", "notifier_profile", "notifier_profile TEXT"
            )
        if "kinds_json" not in notify_cols:
            # BUILD-508: additive, nullable. NULL = all kinds (back-compat).
            _add_column_if_missing(
                conn, "kanban_notify_subs", "kinds_json", "kinds_json TEXT"
            )

    workflow_compilation_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'workflow_graph_compilations'"
    ).fetchone()
    if workflow_compilation_table is not None:
        compilation_cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(workflow_graph_compilations)")
        }
        if "request_digest" not in compilation_cols:
            _add_column_if_missing(
                conn,
                "workflow_graph_compilations",
                "request_digest",
                "request_digest TEXT",
            )

    # One-shot backfill: any task that is 'running' before runs existed
    # had its claim_lock / claim_expires / worker_pid on the task row.
    # Synthesize a matching task_runs row so subsequent end-run / heartbeat
    # calls have something to write to. Wrapped in write_txn to serialize
    # against any concurrent dispatcher, and the per-row UPDATE uses
    # ``current_run_id IS NULL`` as a CAS guard so a racing claim can't
    # produce an orphaned row if it interleaves with the backfill pass.
    runs_exist = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='task_runs'"
    ).fetchone() is not None
    if runs_exist:
        run_cols = {
            row["name"] for row in conn.execute("PRAGMA table_info(task_runs)")
        }
        if "last_transport_activity_at" not in run_cols:
            _add_column_if_missing(
                conn,
                "task_runs",
                "last_transport_activity_at",
                "last_transport_activity_at INTEGER",
            )
        if "last_semantic_progress_at" not in run_cols:
            _add_column_if_missing(
                conn,
                "task_runs",
                "last_semantic_progress_at",
                "last_semantic_progress_at INTEGER",
            )
        if "last_durable_progress_at" not in run_cols:
            _add_column_if_missing(
                conn,
                "task_runs",
                "last_durable_progress_at",
                "last_durable_progress_at INTEGER",
            )
        if "run_spec_json" not in run_cols:
            _add_column_if_missing(
                conn, "task_runs", "run_spec_json", "run_spec_json TEXT",
            )
        if "worker_started_at" not in run_cols:
            _add_column_if_missing(
                conn, "task_runs", "worker_started_at", "worker_started_at REAL"
            )
        if "worker_pgid" not in run_cols:
            _add_column_if_missing(
                conn, "task_runs", "worker_pgid", "worker_pgid INTEGER"
            )
        if "worker_sid" not in run_cols:
            _add_column_if_missing(
                conn, "task_runs", "worker_sid", "worker_sid INTEGER"
            )
        with write_txn(conn):
            inflight = conn.execute(
                "SELECT id, assignee, claim_lock, claim_expires, worker_pid, "
                "       worker_started_at, worker_pgid, worker_sid, "
                "       max_runtime_seconds, last_heartbeat_at, started_at "
                "FROM tasks "
                "WHERE status = 'running' AND current_run_id IS NULL"
            ).fetchall()
            for row in inflight:
                started = row["started_at"] or int(time.time())
                cur = conn.execute(
                    """
                    INSERT INTO task_runs (
                        task_id, profile, status,
                        claim_lock, claim_expires, worker_pid,
                        worker_started_at, worker_pgid, worker_sid,
                        max_runtime_seconds, last_heartbeat_at,
                        started_at
                    ) VALUES (?, ?, 'running', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["id"], row["assignee"], row["claim_lock"],
                        row["claim_expires"], row["worker_pid"],
                        row["worker_started_at"], row["worker_pgid"],
                        row["worker_sid"],
                        row["max_runtime_seconds"], row["last_heartbeat_at"],
                        started,
                    ),
                )
                # CAS: only install the pointer if nothing else claimed
                # the task between our SELECT and here (shouldn't happen
                # under the write_txn, but belt-and-suspenders). If the
                # CAS fails we've got an orphan run_row — mark it
                # reclaimed so it doesn't look in-flight.
                upd = conn.execute(
                    "UPDATE tasks SET current_run_id = ? "
                    "WHERE id = ? AND current_run_id IS NULL",
                    (cur.lastrowid, row["id"]),
                )
                if upd.rowcount != 1:
                    conn.execute(
                        "UPDATE task_runs SET status = 'reclaimed', "
                        "    outcome = 'reclaimed', ended_at = ?, "
                        "    claim_lock = NULL, claim_expires = NULL, "
                        "    worker_pid = NULL, worker_started_at = NULL, "
                        "    worker_pgid = NULL, worker_sid = NULL "
                        "WHERE id = ?",
                        (int(time.time()), cur.lastrowid),
                    )

    # One-shot event-kind rename pass. The old names ("ready", "priority",
    # "spawn_auto_blocked") still worked but were awkward on the wire;
    # rename them in-place so existing DBs migrate cleanly. Fires once
    # per DB because after the UPDATE no rows match the old kinds.
    _EVENT_RENAMES = (
        # (old, new)
        ("ready",              "promoted"),
        ("priority",           "reprioritized"),
        ("spawn_auto_blocked", "gave_up"),
    )
    for old, new in _EVENT_RENAMES:
        conn.execute(
            "UPDATE task_events SET kind = ? WHERE kind = ?",
            (new, old),
        )

    _rebuild_drifted_tables(conn)

    _backfill_workflow_step_notify_subs(conn)


def _backfill_workflow_step_notify_subs(conn: sqlite3.Connection) -> None:
    """Give every unfinished step of a compiled workflow the terminal task's
    notify subscription, narrowed to FAILURE_KINDS.

    Workflows compiled before BUILD-503 (2026-07-16) subscribed only their
    terminal task, so a nonterminal step that blocked never notified anyone
    (2026-07-18 gsthst-q2 incident: t_a43ae5e2 sat `blocked` for 2.5h in
    silence). ``compile_workflow_graph`` now subscribes every step at
    creation; this pass heals workflows that already existed. Idempotent via
    the (task_id, platform, chat_id, thread_id) primary key.

    Cursor policy: a step currently `blocked` starts at 0 so its pending
    failure event is delivered on the next notifier tick; any other step
    subscribes go-forward only (cursor = its latest event id) so historical,
    already-resolved failures don't replay as noise.
    """
    try:
        rows = conn.execute(
            "SELECT workflow_key, task_ids, terminal_step_key"
            " FROM workflow_graph_compilations"
        ).fetchall()
    except sqlite3.Error:
        return
    if not rows:
        return
    failure_kinds_json = json.dumps(sorted(FAILURE_KINDS))
    now = int(time.time())
    for row in rows:
        try:
            task_ids = json.loads(row["task_ids"])
        except Exception:
            continue
        if not isinstance(task_ids, dict):
            continue
        terminal_id = task_ids.get(row["terminal_step_key"])
        if not terminal_id:
            continue
        terminal_subs = conn.execute(
            "SELECT platform, chat_id, thread_id, user_id, notifier_profile"
            " FROM kanban_notify_subs WHERE task_id = ?",
            (terminal_id,),
        ).fetchall()
        if not terminal_subs:
            continue
        for step_task_id in task_ids.values():
            if step_task_id == terminal_id:
                continue
            task_row = conn.execute(
                "SELECT status FROM tasks WHERE id = ?", (step_task_id,)
            ).fetchone()
            if task_row is None or task_row["status"] in ("done", "archived"):
                continue
            if task_row["status"] == "blocked":
                cursor = 0
            else:
                cur_row = conn.execute(
                    "SELECT COALESCE(MAX(id), 0) AS max_id FROM task_events"
                    " WHERE task_id = ?",
                    (step_task_id,),
                ).fetchone()
                cursor = int(cur_row["max_id"]) if cur_row else 0
            for sub in terminal_subs:
                conn.execute(
                    """INSERT OR IGNORE INTO kanban_notify_subs
                        (task_id, platform, chat_id, thread_id, user_id,
                         notifier_profile, created_at, last_event_id, kinds_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        step_task_id, sub["platform"], sub["chat_id"],
                        sub["thread_id"] or "", sub["user_id"],
                        sub["notifier_profile"], now, cursor,
                        failure_kinds_json,
                    ),
                )


# Legacy DBs defined these tables with a ``TEXT PRIMARY KEY`` id (or, for
# ``kanban_notify_subs``, a nullable ``TEXT last_event_id``). The current
# schema uses ``INTEGER PRIMARY KEY AUTOINCREMENT`` / ``INTEGER NOT NULL
# DEFAULT 0``. ``CREATE TABLE IF NOT EXISTS`` skips existing tables
# regardless of schema and ``_add_column_if_missing`` only adds columns, so
# neither can fix a drifted column type — the table must be rebuilt. See
# #35096.
#
# Each entry pairs the canonical CREATE TABLE with the CREATE INDEX
# statements that DROP TABLE would otherwise take down with it (including
# ``idx_events_run``, added by the additive pass above). To guard against
# this list drifting from SCHEMA_SQL, ``test_rebuilt_schema_matches_fresh``
# asserts a rebuilt legacy DB is byte-identical to a fresh one.
_REBUILD_SPECS = {
    "task_events": (
        "CREATE TABLE task_events ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " task_id TEXT NOT NULL, run_id INTEGER, kind TEXT NOT NULL,"
        " payload TEXT, created_at INTEGER NOT NULL)",
        (
            "CREATE INDEX idx_events_task ON task_events(task_id, created_at)",
            "CREATE INDEX idx_events_run ON task_events(run_id, id)",
        ),
    ),
    "task_comments": (
        "CREATE TABLE task_comments ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " task_id TEXT NOT NULL, author TEXT NOT NULL, body TEXT NOT NULL,"
        " created_at INTEGER NOT NULL)",
        ("CREATE INDEX idx_comments_task ON task_comments(task_id, created_at)",),
    ),
    "task_runs": (
        "CREATE TABLE task_runs ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " task_id TEXT NOT NULL, profile TEXT, step_key TEXT,"
        " status TEXT NOT NULL, claim_lock TEXT, claim_expires INTEGER,"
        " worker_pid INTEGER, worker_started_at REAL, worker_pgid INTEGER,"
        " worker_sid INTEGER, max_runtime_seconds INTEGER,"
        " last_heartbeat_at INTEGER, last_transport_activity_at INTEGER,"
        " last_semantic_progress_at INTEGER, last_durable_progress_at INTEGER,"
        " run_spec_json TEXT,"
        " started_at INTEGER NOT NULL,"
        " ended_at INTEGER, outcome TEXT, summary TEXT, metadata TEXT,"
        " error TEXT)",
        (
            "CREATE INDEX idx_runs_task ON task_runs(task_id, started_at)",
            "CREATE INDEX idx_runs_status ON task_runs(status)",
        ),
    ),
    "kanban_notify_subs": (
        "CREATE TABLE kanban_notify_subs ("
        " task_id TEXT NOT NULL, platform TEXT NOT NULL, chat_id TEXT NOT NULL,"
        " thread_id TEXT NOT NULL DEFAULT '', user_id TEXT,"
        " notifier_profile TEXT, created_at INTEGER NOT NULL,"
        " last_event_id INTEGER NOT NULL DEFAULT 0, kinds_json TEXT,"
        " PRIMARY KEY (task_id, platform, chat_id, thread_id))",
        ("CREATE INDEX idx_notify_task ON kanban_notify_subs(task_id)",),
    ),
}


def _table_has_drifted(conn: sqlite3.Connection, table: str) -> bool:
    """True when ``table`` still carries the legacy (pre-AUTOINCREMENT) shape."""
    info = conn.execute(f"PRAGMA table_info({table})").fetchall()
    if not info:
        return False  # table absent — nothing to rebuild
    if table == "kanban_notify_subs":
        lei = next((c for c in info if c["name"] == "last_event_id"), None)
        return lei is not None and (lei["type"] or "").upper() != "INTEGER"
    # task_events / task_comments / task_runs: id must be INTEGER and a PK.
    id_col = next((c for c in info if c["name"] == "id"), None)
    if id_col is None:
        return False
    return not ((id_col["type"] or "").upper() == "INTEGER" and id_col["pk"])


def _rebuild_drifted_tables(conn: sqlite3.Connection) -> None:
    """Rebuild any kanban table whose column types drifted from SCHEMA_SQL.

    Old boards crash the gateway notifier (``int(None)`` on a NULL id in
    ``unseen_events_for_sub``) and never match the ``id > cursor`` filter, so
    every kanban notification is silently lost (#35096). Each affected table is
    rebuilt with the standard SQLite pattern — CREATE new → INSERT shared
    columns → DROP old → RENAME — recreating its indexes too (DROP TABLE takes
    them down). The legacy TEXT ids are dropped (they aren't valid integers);
    AUTOINCREMENT assigns fresh ones and ``last_event_id`` cursors reset to 0,
    so the first post-migration tick replays a task's event history once —
    the safe failure mode for a feature that was already fully broken.

    The whole pass runs in one transaction so an interruption can't leave a
    table half-renamed, and under ``connect()``'s init locks so nothing races
    it. Idempotent: a correctly-typed DB skips every table and returns without
    opening a transaction.
    """
    drifted = [t for t in _REBUILD_SPECS if _table_has_drifted(conn, t)]
    if not drifted:
        return

    conn.execute("BEGIN IMMEDIATE")
    try:
        for table in drifted:
            create_sql, index_sqls = _REBUILD_SPECS[table]
            old_cols = [c["name"] for c in conn.execute(f"PRAGMA table_info({table})")]
            _log.info("kanban migration: rebuilding %s to match current schema", table)
            conn.execute(f"ALTER TABLE {table} RENAME TO {table}_legacy")
            conn.execute(create_sql)
            new_cols = {c["name"] for c in conn.execute(f"PRAGMA table_info({table})")}
            if table == "kanban_notify_subs":
                # Cast the legacy TEXT cursor to INTEGER; NULL / non-numeric → 0.
                shared = [c for c in old_cols if c in new_cols and c != "last_event_id"]
                cols_csv = ", ".join(shared)
                conn.execute(
                    f"INSERT INTO {table} ({cols_csv}, last_event_id) "
                    f"SELECT {cols_csv}, COALESCE(CAST(last_event_id AS INTEGER), 0) "
                    f"FROM {table}_legacy"
                )
            else:
                # Drop the legacy TEXT id; AUTOINCREMENT reassigns it.
                shared = [c for c in old_cols if c in new_cols and c != "id"]
                cols_csv = ", ".join(shared)
                conn.execute(
                    f"INSERT INTO {table} ({cols_csv}) "
                    f"SELECT {cols_csv} FROM {table}_legacy"
                )
            conn.execute(f"DROP TABLE {table}_legacy")
            for index_sql in index_sqls:
                conn.execute(index_sql)
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise


def _check_file_length_invariant(conn: sqlite3.Connection) -> None:
    """Read the SQLite header page_count and compare against actual file size.

    Raises sqlite3.DatabaseError if the file is shorter than the header claims
    (torn-extend corruption).
    """
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
        if row is None:
            return
        path_str = row[2]  # column 2 is the file path; empty for in-memory DBs
        if not path_str:
            return  # in-memory or unnamed DB; skip
        path = path_str
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        file_size = os.path.getsize(path)
        # Never plain-open the live DB here. On POSIX, close() cancels EVERY
        # fcntl lock this process holds on that inode -- including the locks
        # other SQLite connections in this process are actively relying on.
        # This check runs at the post-commit boundary of every write_txn, so a
        # plain open/close here revoked the gateway's kanban locks on every
        # write, letting concurrent writers/checkpointers proceed without
        # exclusion (board corruption + SQLITE_IOERR on a sibling connect()).
        # Same hazard as the fast-path header guard fixed in 97a17b6c3; reuse
        # the cached never-closed descriptor instead.
        header_bytes = _read_db_header(Path(path), 32)[28:32]
        if len(header_bytes) < 4:
            return  # can't read header; skip
        header_page_count = int.from_bytes(header_bytes, "big")
        if header_page_count == 0:
            return  # new/empty DB; skip
        actual_pages = file_size // page_size
        if actual_pages < header_page_count:
            raise sqlite3.DatabaseError(
                f"torn-extend detected: page count mismatch on {path}: "
                f"header claims {header_page_count} pages, "
                f"file has {actual_pages} pages "
                f"(missing {header_page_count - actual_pages} pages, "
                f"file_size={file_size}, page_size={page_size})"
            )
    except sqlite3.DatabaseError:
        raise
    except Exception:
        pass  # I/O errors during check are non-fatal; let normal ops continue


# SQLite's own busy_timeout uses a near-deterministic backoff, so concurrent
# writers re-collide in lockstep under a stampede. A jittered retry on the
# transaction boundary breaks that convoy. Mirrors state.db's _execute_write:
# a fixed 20-150ms jitter band (a 20ms floor prevents a near-zero retry from
# busy-spinning back into the collision). Only BEGIN IMMEDIATE and COMMIT are
# retried -- both are idempotent re-issues that touch no transaction body, so a
# CAS inside write_txn is never replayed. kanban keeps fewer retries than
# state.db (5 vs 15) because its 120s busy_timeout already absorbs most waits;
# the retry is the backstop for the tail SQLite returns BUSY on immediately.
_BUSY_MAX_RETRIES = 5
_BUSY_RETRY_MIN_S = 0.020  # 20ms
_BUSY_RETRY_MAX_S = 0.150  # 150ms


def _is_busy_error(exc: BaseException) -> bool:
    return isinstance(exc, sqlite3.OperationalError) and (
        "database is locked" in str(exc).lower()
        or "database is busy" in str(exc).lower()
    )


def _execute_boundary_with_retry(conn: sqlite3.Connection, sql: str) -> None:
    for attempt in range(_BUSY_MAX_RETRIES + 1):
        try:
            conn.execute(sql)
            return
        except sqlite3.OperationalError as exc:
            if not _is_busy_error(exc) or attempt == _BUSY_MAX_RETRIES:
                raise
            time.sleep(random.uniform(_BUSY_RETRY_MIN_S, _BUSY_RETRY_MAX_S))


@contextlib.contextmanager
def write_txn(conn: sqlite3.Connection):
    """Context manager for an IMMEDIATE write transaction.

    Use for any multi-statement write (creating a task + link, claiming a
    task + recording an event, etc.).  A claim CAS inside this context is
    atomic -- at most one concurrent writer can succeed.

    The explicit ROLLBACK on exception is wrapped in try/except so that
    a SQLite auto-rollback (which leaves no active transaction) does not
    shadow the original exception with a spurious rollback error.
    """
    _execute_boundary_with_retry(conn, "BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            # SQLite has already auto-rolled-back the transaction (typical
            # under EIO, lock contention, or corruption). Nothing to undo;
            # do not let this secondary failure shadow the real one.
            pass
        raise
    else:
        try:
            _execute_boundary_with_retry(conn, "COMMIT")
        except Exception:
            # COMMIT exhausted retries with the txn still open; roll back so the
            # connection isn't poisoned for the next BEGIN IMMEDIATE.
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
        # Post-commit file-length check: header page_count must match actual file pages.
        # A discrepancy means a torn-extend — raise now rather than silently corrupt.
        _check_file_length_invariant(conn)


# ---------------------------------------------------------------------------
# ID generation
# ---------------------------------------------------------------------------

def _new_task_id() -> str:
    """Generate a short, URL-safe task id.

    4 hex bytes = ~4.3B possibilities. At 10k tasks the collision
    probability is ~1.2e-5; at 100k it's ~1.2e-3. Previously we used 2
    hex bytes (65k possibilities) which hit the birthday paradox hard:
    ~5% collision probability at 1k tasks, ~50% at 10k. Callers that
    care about idempotency should pass ``idempotency_key`` to
    :func:`create_task` rather than rely on id uniqueness.
    """
    return "t_" + secrets.token_hex(4)


def _claimer_id() -> str:
    """Return a ``host:pid`` string that identifies this claimer."""
    import socket
    try:
        host = socket.gethostname() or "unknown"
    except Exception:
        host = "unknown"
    return f"{host}:{os.getpid()}"


# ---------------------------------------------------------------------------
# Task creation / mutation
# ---------------------------------------------------------------------------

def _canonical_assignee(assignee: Optional[str]) -> Optional[str]:
    """Lowercase-assignee normalization for Kanban rows (dashboard/CLI parity)."""
    if assignee is None:
        return None
    from hermes_cli.profiles import normalize_profile_name

    return normalize_profile_name(assignee)


@dataclass(frozen=True)
class ForcedSkillValidationIssue:
    """A task skill that would fail fatal CLI preload for an assignee profile."""

    skill: str
    code: str
    detail: str


def _normalize_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _load_profile_skills_config(profile_home: Path) -> dict[str, Any]:
    cfg_path = profile_home / "config.yaml"
    if not cfg_path.exists():
        return {}
    try:
        from agent.skill_utils import yaml_load

        parsed = yaml_load(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(parsed, dict):
        return {}
    skills_cfg = parsed.get("skills")
    return skills_cfg if isinstance(skills_cfg, dict) else {}


def _profile_disabled_skill_names(skills_cfg: dict[str, Any]) -> set[str]:
    platform = os.getenv("HERMES_PLATFORM") or os.getenv("HERMES_SESSION_PLATFORM")
    if platform:
        platform_disabled = skills_cfg.get("platform_disabled")
        if isinstance(platform_disabled, dict) and platform in platform_disabled:
            return set(_normalize_string_list(platform_disabled.get(platform)))
    return set(_normalize_string_list(skills_cfg.get("disabled")))


def _profile_external_skill_dirs(profile_home: Path, skills_cfg: dict[str, Any]) -> list[Path]:
    raw_dirs = skills_cfg.get("external_dirs")
    dirs: list[Path] = []
    seen: set[Path] = set()
    local_skills = (profile_home / "skills").resolve()
    for entry in _normalize_string_list(raw_dirs):
        expanded = os.path.expanduser(os.path.expandvars(entry))
        path = Path(expanded)
        if not path.is_absolute():
            path = profile_home / path
        try:
            path = path.resolve()
        except OSError:
            continue
        if path == local_skills or path in seen or not path.is_dir():
            continue
        seen.add(path)
        dirs.append(path)
    return dirs


def _candidate_skill_files(profile_home: Path, skills_cfg: dict[str, Any], name: str) -> list[Path]:
    """Return local/external SKILL.md candidates using skill_view's main strategies.

    This is intentionally lightweight and profile-home explicit. Importing
    tools.skills_tool in the dispatcher would bind its module-level SKILLS_DIR
    to the dispatcher's profile, not the worker's profile. Re-implement the
    filesystem lookup here so validation answers the question the child CLI will
    answer after ``hermes -p <assignee>`` rewrites HERMES_HOME.
    """
    from agent.skill_utils import iter_skill_index_files

    roots = [profile_home / "skills"] + _profile_external_skill_dirs(profile_home, skills_cfg)
    candidates: list[Path] = []
    seen: set[Path] = set()

    local_category_name: Optional[str] = None
    if ":" in name:
        namespace, bare = name.split(":", 1)
        if namespace and bare:
            local_category_name = f"{namespace}/{bare}"

    def _record(path: Path) -> None:
        try:
            key = path.resolve()
        except OSError:
            key = path
        if key in seen:
            return
        seen.add(key)
        candidates.append(path)

    for root in roots:
        if not root.exists():
            continue
        direct = root / name
        if direct.is_dir() and (direct / "SKILL.md").is_file():
            _record(direct / "SKILL.md")
        elif direct.with_suffix(".md").is_file():
            _record(direct.with_suffix(".md"))

        if local_category_name:
            categorized = root / local_category_name
            if categorized.is_dir() and (categorized / "SKILL.md").is_file():
                _record(categorized / "SKILL.md")
            elif categorized.with_suffix(".md").is_file():
                _record(categorized.with_suffix(".md"))

        for skill_md in iter_skill_index_files(root, "SKILL.md"):
            if skill_md.parent.name == name:
                _record(skill_md)

        # Legacy flat <name>.md files. Path separators and plugin-qualified
        # names are not valid flat basenames, so only attempt this for bare names.
        if "/" not in name and ":" not in name:
            try:
                for found_md in root.rglob(f"{name}.md"):
                    if found_md.name != "SKILL.md":
                        _record(found_md)
            except OSError:
                pass

    return candidates


def _validate_forced_skills_for_profile(
    assignee: Optional[str],
    skills: Optional[Iterable[str]],
) -> list[ForcedSkillValidationIssue]:
    """Classify per-task skills that cannot preload for *assignee*.

    Returns an empty list when validation passes or when the assignee is not a
    real Hermes profile. The latter is deliberate: dispatch already buckets
    non-profile lanes as ``skipped_nonspawnable`` and create_task historically
    allowed tasks for terminal-pulled lanes.
    """
    if not assignee or not skills:
        return []
    try:
        from hermes_cli.profiles import profile_exists, resolve_profile_env

        if not profile_exists(assignee):
            return []
        profile_home = Path(resolve_profile_env(assignee))
    except Exception:
        return []

    skills_cfg = _load_profile_skills_config(profile_home)
    disabled = _profile_disabled_skill_names(skills_cfg)
    issues: list[ForcedSkillValidationIssue] = []

    try:
        from agent.skill_utils import parse_frontmatter, skill_matches_platform
    except Exception:
        # If skill metadata helpers are unavailable, fail open: the child CLI
        # will still enforce preload. This avoids blocking dispatch because an
        # optional validation dependency failed inside the dispatcher.
        return []

    for raw_skill in skills:
        skill = str(raw_skill or "").strip()
        if not skill:
            continue
        candidates = _candidate_skill_files(profile_home, skills_cfg, skill)
        if not candidates:
            issues.append(
                ForcedSkillValidationIssue(
                    skill=skill,
                    code="skill-not-installed-for-profile",
                    detail=(
                        f"skill {skill!r} is not installed in profile {assignee!r} "
                        "skills or external_dirs"
                    ),
                )
            )
            continue
        if len(candidates) > 1:
            matches = ", ".join(str(path) for path in candidates[:5])
            issues.append(
                ForcedSkillValidationIssue(
                    skill=skill,
                    code="skill-ambiguous-for-profile",
                    detail=(
                        f"skill {skill!r} is ambiguous for profile {assignee!r}; "
                        f"matches: {matches}"
                    ),
                )
            )
            continue

        skill_md = candidates[0]
        try:
            content = skill_md.read_text(encoding="utf-8")
        except Exception as exc:
            issues.append(
                ForcedSkillValidationIssue(
                    skill=skill,
                    code="skill-unreadable-for-profile",
                    detail=f"skill {skill!r} cannot be read for profile {assignee!r}: {exc}",
                )
            )
            continue
        frontmatter, _body = parse_frontmatter(content)
        resolved_name = str(frontmatter.get("name") or skill_md.parent.name).strip()
        if not skill_matches_platform(frontmatter):
            issues.append(
                ForcedSkillValidationIssue(
                    skill=skill,
                    code="skill-unsupported-on-platform",
                    detail=(
                        f"skill {resolved_name or skill!r} is not supported on this platform "
                        f"for profile {assignee!r}"
                    ),
                )
            )
            continue
        if resolved_name in disabled:
            issues.append(
                ForcedSkillValidationIssue(
                    skill=skill,
                    code="skill-disabled-for-profile",
                    detail=f"skill {resolved_name!r} is disabled for profile {assignee!r}",
                )
            )

    return issues


def _format_forced_skill_validation_issues(
    assignee: Optional[str],
    issues: list[ForcedSkillValidationIssue],
) -> str:
    details = "; ".join(f"{issue.code}: {issue.detail}" for issue in issues)
    return (
        f"forced skill validation failed for assignee profile {assignee!r}: {details}. "
        "Remove or replace the task's forced skill(s), install/support the skill "
        "for that profile, or intentionally enable it before unblocking."
    )


def _forced_skill_validation_error(
    assignee: Optional[str],
    skills: Optional[Iterable[str]],
) -> Optional[str]:
    issues = _validate_forced_skills_for_profile(assignee, skills)
    if not issues:
        return None
    return _format_forced_skill_validation_issues(assignee, issues)


def _block_forced_skill_validation_failure(
    conn: sqlite3.Connection,
    task_id: str,
    reason: str,
) -> bool:
    if not block_task(conn, task_id, reason=reason):
        return False
    with write_txn(conn):
        conn.execute(
            "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
            (reason[:500], task_id),
        )
    return True


# ---------------------------------------------------------------------------
# Architecture-First authorization projection
# ---------------------------------------------------------------------------

def _architecture_gate_from_row(row: Optional[sqlite3.Row]) -> Optional[ArchitectureGate]:
    return ArchitectureGate.from_row(row) if row is not None else None


def get_architecture_gate(conn: sqlite3.Connection, gate_id: str) -> Optional[ArchitectureGate]:
    return _architecture_gate_from_row(
        conn.execute("SELECT * FROM architecture_gates WHERE gate_id = ?", (gate_id,)).fetchone()
    )


def get_architecture_gate_for_task(
    conn: sqlite3.Connection, task_id: str,
) -> Optional[ArchitectureGate]:
    """Resolve the nearest architecture gate from a task's parent ancestry."""
    seen: set[str] = set()
    stack = [task_id]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        gate = _architecture_gate_from_row(conn.execute(
            "SELECT * FROM architecture_gates WHERE architect_task_id = ? "
            "ORDER BY updated_at DESC LIMIT 1", (current,)
        ).fetchone())
        if gate is not None:
            return gate
        stack.extend(parent_ids(conn, current))
    return None


def get_delivery_architecture_gate(
    conn: sqlite3.Connection, task_id: str,
) -> Optional[ArchitectureGate]:
    """Resolve an enforcing gate that can affect this *current* worker turn.

    Parent traversal covers graph-issued workers.  Before a graph exists, an
    orchestrator can open an architect gate in the same turn; in that case the
    active task and new architect card share the durable session/workflow
    binding.  This lookup deliberately uses only persisted server-side fields,
    never model supplied scope values.
    """
    direct = get_architecture_gate_for_task(conn, task_id)
    if direct is not None and direct.enforcement_mode in ARCHITECTURE_GATE_ENFORCING_MODES:
        return direct
    task = get_task(conn, task_id)
    if task is None:
        return None
    if task.workflow_key:
        # Workflow identity is stronger than the long-lived chat session. Do
        # not OR the session into this predicate: a terminal gate for workflow
        # A must not poison unrelated workflow B in the same conversation.
        scope_sql = "workflow_key = ?"
        params: list[Any] = [task.workflow_key]
        state_sql = "state != 'human_approved'"
    elif task.session_id:
        # One-off tasks have no persisted request-scope id yet. Session is the
        # legacy fallback only for active pre-approval gates; terminal rejected
        # or invalidated one-offs cannot shadow the whole session forever.
        scope_sql = "session_id = ?"
        params = [task.session_id]
        state_sql = (
            "state IN ('open', 'validated_awaiting_approval', "
            "'policy_accepted')"
        )
    else:
        return None
    row = conn.execute(
        "SELECT * FROM architecture_gates WHERE enforcement_mode IN (?, ?) "
        f"AND {state_sql} AND {scope_sql} "
        "ORDER BY updated_at DESC LIMIT 1",
        [*ARCHITECTURE_GATE_ENFORCING_MODES, *params],
    ).fetchone()
    return _architecture_gate_from_row(row)


def _active_scope_gate(
    conn: sqlite3.Connection, context: MutationContext, *, include_terminal: bool = False,
) -> Optional[ArchitectureGate]:
    if context.gate_id:
        gate = get_architecture_gate(conn, context.gate_id)
        if gate is None or gate.board_key != context.board_key or gate.creator_principal != context.principal:
            raise ArchitectureGateError("architecture_gate_scope_mismatch")
        return gate
    predicates = ["board_key = ?", "creator_principal = ?"]
    if not include_terminal:
        predicates.append(
            "state IN ('open', 'validated_awaiting_approval', 'policy_accepted', 'human_approved')"
        )
    params: list[Any] = [context.board_key, context.principal]
    scopes: list[str] = []
    if context.request_scope_id:
        scopes.append("request_scope_id = ?")
        params.append(context.request_scope_id)
    if context.workflow_key:
        scopes.append("workflow_key = ?")
        params.append(context.workflow_key)
    if not scopes:
        return None
    row = conn.execute(
        "SELECT * FROM architecture_gates WHERE " + " AND ".join(predicates)
        + " AND (" + " OR ".join(scopes) + ") ORDER BY updated_at DESC LIMIT 1",
        params,
    ).fetchone()
    return _architecture_gate_from_row(row)


def _gate_requires_enforcement(gate: Optional[ArchitectureGate]) -> bool:
    return bool(
        gate
        and gate.enforcement_mode in ARCHITECTURE_GATE_ENFORCING_MODES
        and gate.state not in ARCHITECTURE_GATE_APPROVED_STATES
    )


def _new_gate_id() -> str:
    return "g_" + secrets.token_hex(8)


def _append_gate_audit(
    conn: sqlite3.Connection,
    gate: ArchitectureGate,
    kind: str,
    reason: Optional[str] = None,
    *,
    created_at: Optional[int] = None,
) -> int:
    payload: dict[str, Any] = {
        "gate_id": gate.gate_id,
        "state": gate.state,
        "mode": gate.enforcement_mode,
    }
    if reason:
        payload["reason"] = reason
    return _append_event(
        conn,
        gate.architect_task_id,
        kind,
        payload,
        created_at=created_at,
    )


def _open_architecture_gate(
    conn: sqlite3.Connection, task_id: str, context: MutationContext,
) -> ArchitectureGate:
    mode = context.mode.strip().lower()
    if mode not in ARCHITECTURE_GATE_MODES:
        raise ValueError(f"architecture gate mode must be one of {sorted(ARCHITECTURE_GATE_MODES)}")
    if mode in ARCHITECTURE_GATE_ENFORCING_MODES:
        clauses: list[str] = []
        params: list[Any] = []
        if context.session_id:
            clauses.append("session_id = ?")
            params.append(context.session_id)
        if context.workflow_key:
            clauses.append("workflow_key = ?")
            params.append(context.workflow_key)
        if clauses:
            running = conn.execute(
                "SELECT id FROM tasks WHERE status = 'running' AND ("
                + " OR ".join(clauses)
                + ") ORDER BY created_at LIMIT 1",
                params,
            ).fetchone()
            if running is not None:
                # A running attempt already carries an immutable claim-time
                # delivery disposition. Opening a new enforcing gate in its
                # scope would retroactively change that contract and create a
                # lookup-failure race. Require the attempt to reach a terminal
                # state, then open the gate before a fresh claim.
                raise ArchitectureGateError(
                    "architecture_gate_running_ungated_run"
                )
    now = int(time.time())
    conn.execute(
        """INSERT INTO architecture_gates (
            gate_id, board_key, creator_principal, creator_actor_type, creator_profile,
            request_scope_id, session_id, workflow_key, architect_task_id, state,
            policy_version, canonicalization_version, enforcement_mode, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?)""",
        (
            _new_gate_id(), context.board_key, context.principal,
            context.actor_type, context.profile, context.request_scope_id,
            context.session_id, context.workflow_key,
            task_id, ARCHITECTURE_GATE_POLICY_VERSION,
            ARCHITECTURE_GATE_CANONICALIZATION_VERSION, mode, now, now,
        ),
    )
    gate = get_architecture_gate_for_task(conn, task_id)
    assert gate is not None
    _append_gate_audit(conn, gate, "architecture_gate_opened")
    return gate


def _validate_json_value(value: Any) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("non-finite number in architecture handoff")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item)
        return
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("architecture handoff keys must be strings")
        for item in value.values():
            _validate_json_value(item)
        return
    raise ValueError("architecture handoff contains unstable non-JSON value")


def canonicalize_architecture_handoff(metadata: dict[str, Any]) -> str:
    """Validate authority fields and return version-v1 canonical JSON."""
    if not isinstance(metadata, dict):
        raise ValueError("architecture handoff metadata must be an object")
    allowed = {
        "role", "design_depth", "chosen_approach", "alternatives_rejected", "slices",
        "acceptance_criteria", "verification_plan", "human_approval_required", "rollout", "rollback",
    }
    operational = {"artifacts", "model_used", "worker_session_id"}
    unknown = set(metadata) - allowed - operational
    if unknown:
        raise ValueError("unknown top-level authority fields: " + ", ".join(sorted(unknown)))
    required = allowed
    missing = required - set(metadata)
    if missing:
        raise ValueError("missing architecture handoff fields: " + ", ".join(sorted(missing)))
    if metadata["role"] != "architect":
        raise ValueError("architecture handoff role must be architect")
    if metadata["design_depth"] not in {"none", "micro", "formal"}:
        raise ValueError("invalid architecture design_depth")
    if not isinstance(metadata["chosen_approach"], str) or not metadata["chosen_approach"].strip():
        raise ValueError("architecture chosen_approach is required")
    for field_name in ("alternatives_rejected", "slices", "acceptance_criteria", "verification_plan"):
        if not isinstance(metadata[field_name], list):
            raise ValueError(f"architecture {field_name} must be an array")
    if not isinstance(metadata["human_approval_required"], bool):
        raise ValueError("architecture human_approval_required must be boolean")
    if not isinstance(metadata["rollout"], dict) or not metadata["rollout"]:
        raise ValueError("architecture rollout must be a non-empty object")
    if not isinstance(metadata["rollback"], dict) or not metadata["rollback"]:
        raise ValueError("architecture rollback must be a non-empty object")
    if metadata["design_depth"] == "formal":
        if not metadata["slices"] or not metadata["acceptance_criteria"] or not metadata["verification_plan"]:
            raise ValueError("formal architecture handoff requires slices, acceptance criteria, and verification")
        if not any(isinstance(item, dict) and item.get("verification") for item in metadata["slices"]):
            raise ValueError("formal architecture handoff requires a slice verification")
    handoff = {key: metadata[key] for key in allowed}
    _validate_json_value(handoff)
    return json.dumps(handoff, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def architecture_handoff_digest(
    *, policy_version: str, canonicalization_version: str, trusted_scope: dict[str, Any],
    architect_task_id: str, accepted_run_id: int, canonical_handoff_json: str,
) -> str:
    domain = {
        "policy_version": policy_version,
        "canonicalization_version": canonicalization_version,
        "trusted_scope": trusted_scope,
        "architect_task_id": architect_task_id,
        "accepted_run_id": int(accepted_run_id),
        "canonical_handoff_json": canonical_handoff_json,
    }
    return hashlib.sha256(
        json.dumps(domain, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    ).hexdigest()


def _accept_architecture_handoff_in_txn(
    conn: sqlite3.Connection,
    gate: ArchitectureGate,
    *,
    run_id: int,
    metadata: dict[str, Any],
) -> ArchitectureGate:
    """Validate and accept one architect run inside its owning transaction."""
    if gate.state != "open":
        raise ArchitectureGateError("architecture_gate_not_open")
    canonical = canonicalize_architecture_handoff(metadata)
    digest = architecture_handoff_digest(
        policy_version=gate.policy_version,
        canonicalization_version=gate.canonicalization_version,
        trusted_scope={
            "board_key": gate.board_key,
            "creator_principal": gate.creator_principal,
            "request_scope_id": gate.request_scope_id,
            "session_id": gate.session_id,
            "workflow_key": gate.workflow_key,
        },
        architect_task_id=gate.architect_task_id,
        accepted_run_id=int(run_id),
        canonical_handoff_json=canonical,
    )
    target_state = (
        "validated_awaiting_approval"
        if metadata["human_approval_required"]
        else "policy_accepted"
    )
    cur = conn.execute(
        """UPDATE architecture_gates SET accepted_run_id = ?, state = ?, accepted_snapshot = ?,
           design_digest = ?, row_version = row_version + 1, updated_at = ?
           WHERE gate_id = ? AND state = 'open' AND row_version = ?""",
        (
            int(run_id), target_state, canonical, digest, int(time.time()),
            gate.gate_id, gate.row_version,
        ),
    )
    if cur.rowcount != 1:
        raise ArchitectureGateError("architecture_gate_cas_conflict")
    accepted = get_architecture_gate(conn, gate.gate_id)
    assert accepted is not None
    accepted_event_id = _append_gate_audit(
        conn, accepted, "handoff_validation_passed"
    )
    if accepted.state == "policy_accepted":
        conn.execute(
            "UPDATE architecture_gates SET authorization_event_id = ? "
            "WHERE gate_id = ? AND authorization_event_id IS NULL",
            (accepted_event_id, accepted.gate_id),
        )
        accepted = get_architecture_gate(conn, gate.gate_id)
        assert accepted is not None
    return accepted


def accept_architecture_handoff(conn: sqlite3.Connection, gate_id: str) -> ArchitectureGate:
    """Accept a completed architect run by immutable snapshot and CAS."""
    with write_txn(conn):
        gate = get_architecture_gate(conn, gate_id)
        if gate is None:
            raise ValueError("unknown architecture gate")
        if gate.state in {"policy_accepted", "validated_awaiting_approval"}:
            return gate
        if gate.state != "open":
            raise ArchitectureGateError("architecture_gate_not_open")
        task = get_task(conn, gate.architect_task_id)
        if task is None or task.assignee != "architect" or task.status != "done":
            raise ValueError("architect task must be completed by architect")
        run = conn.execute(
            "SELECT id, outcome, metadata FROM task_runs WHERE task_id = ? "
            "ORDER BY id DESC LIMIT 1", (gate.architect_task_id,),
        ).fetchone()
        if run is None or run["outcome"] != "completed" or not run["metadata"]:
            raise ValueError("architect completed run metadata is required")
        try:
            metadata = json.loads(run["metadata"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("architect completed run metadata is malformed") from exc
        return _accept_architecture_handoff_in_txn(
            conn,
            gate,
            run_id=int(run["id"]),
            metadata=metadata,
        )


def _architecture_descendant_task_ids(
    conn: sqlite3.Connection,
    architect_task_id: str,
) -> list[str]:
    """Return the gate's graph descendants in deterministic breadth order."""
    seen: set[str] = {architect_task_id}
    queue = [architect_task_id]
    ordered: list[str] = []
    while queue:
        parent = queue.pop(0)
        children = sorted(child_ids(conn, parent))
        for child in children:
            if child in seen:
                continue
            seen.add(child)
            ordered.append(child)
            queue.append(child)
    return ordered


def _review_attestation_for_task(
    conn: sqlite3.Connection,
    review_task_id: str,
    *,
    include_stale: bool = False,
) -> Optional[dict[str, Any]]:
    """Resolve a successful reviewer attestation against current bytes."""
    task = get_task(conn, review_task_id)
    if task is None or (task.status != "done" and not include_stale):
        return None
    binding = get_current_review_artifact(conn, review_task_id)
    if binding is None:
        return None
    rows = conn.execute(
        "SELECT id, payload, run_id FROM task_events "
        "WHERE task_id = ? AND kind = 'completed' ORDER BY id DESC",
        (review_task_id,),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        attestation = payload.get("review_artifact_attestation") if isinstance(payload, dict) else None
        if not isinstance(attestation, dict):
            continue
        required_attestation_fields = {
            "review_task_id",
            "review_completion_event_id",
            "artifact_generation",
            "artifact_attachment_id",
            "artifact_sha256",
        }
        if not required_attestation_fields.issubset(attestation):
            raise ArchitectureGateError("approval_evidence_changed")
        try:
            candidate = {
                "review_task_id": str(attestation["review_task_id"]),
                "review_completion_event_id": int(
                    attestation["review_completion_event_id"]
                ),
                "artifact_generation": int(attestation["artifact_generation"]),
                "artifact_attachment_id": int(attestation["artifact_attachment_id"]),
                "artifact_sha256": str(attestation["artifact_sha256"]),
            }
        except (TypeError, ValueError, OverflowError) as exc:
            raise ArchitectureGateError("approval_evidence_changed") from exc
        current = (
            candidate["review_task_id"] == review_task_id
            and candidate["review_completion_event_id"] == int(row["id"])
            and candidate["artifact_generation"] == binding.generation
            and candidate["artifact_attachment_id"] == binding.attachment_id
            and candidate["artifact_sha256"] == binding.sha256
        )
        if current:
            _verify_review_artifact_binding(conn, binding)
            return candidate
        if include_stale:
            return candidate
    return None


def _architecture_review_approval_subject_in_txn(
    conn: sqlite3.Connection,
    gate: ArchitectureGate,
) -> dict[str, Any]:
    """Build the authenticated approval subject without exposing artifact paths."""
    candidates: list[dict[str, Any]] = []
    stale_attestation = False
    for task_id in [gate.architect_task_id, *_architecture_descendant_task_ids(
        conn, gate.architect_task_id,
    )]:
        current = _review_attestation_for_task(conn, task_id)
        if current is not None:
            candidates.append(current)
        elif _review_attestation_for_task(conn, task_id, include_stale=True) is not None:
            stale_attestation = True
    if len(candidates) > 1:
        raise ArchitectureGateError("approval_review_ambiguous")
    if not candidates:
        if stale_attestation:
            raise ArchitectureGateError("approval_evidence_changed")
        return {
            "gate_id": gate.gate_id,
            "design_digest": gate.design_digest,
            "review_task_id": None,
            "review_completion_event_id": None,
            "artifact_generation": None,
            "artifact_sha256": None,
            "digest": gate.design_digest,
        }
    attestation = candidates[0]
    domain = {
        "version": 1,
        "gate_id": gate.gate_id,
        "design_digest": gate.design_digest,
        "review_task_id": attestation["review_task_id"],
        "review_completion_event_id": attestation["review_completion_event_id"],
        "artifact_generation": attestation["artifact_generation"],
        "artifact_sha256": attestation["artifact_sha256"],
    }
    digest = hashlib.sha256(
        json.dumps(
            domain,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    return {**domain, "digest": digest}


def architecture_review_approval_subject(
    conn: sqlite3.Connection,
    gate_id: str,
) -> dict[str, Any]:
    """Return the current authenticated approval subject for a gate.

    The returned digest is a subject digest, not the artifact/file digest.
    Callers must display and submit the stable review id, completion event,
    generation, and SHA alongside it.
    """
    gate = get_architecture_gate(conn, gate_id)
    if gate is None:
        raise ValueError("unknown architecture gate")
    return _architecture_review_approval_subject_in_txn(conn, gate)


def approve_architecture_gate(
    conn: sqlite3.Connection,
    gate_id: str,
    context: MutationContext,
    digest: str,
    *,
    review_task_id: Optional[str] = None,
    review_completion_event_id: Optional[int] = None,
    artifact_generation: Optional[int] = None,
    artifact_sha256: Optional[str] = None,
    now: Optional[int] = None,
) -> ArchitectureGate:
    """Record an authenticated exact-digest human approval.

    This is deliberately a DB domain action rather than a dispatchable task:
    scheduler status can never grant authority. Exact repeat submissions from
    the same authenticated actor/surface are idempotent; all other replays deny.
    """
    if context.actor_type != "human":
        raise ArchitectureGateError("approval_requires_human")
    if context.surface not in AUTHENTICATED_APPROVAL_SURFACES:
        raise ArchitectureGateError("approval_surface_not_authenticated")
    if review_completion_event_id is not None and isinstance(
        review_completion_event_id, bool
    ):
        raise ArchitectureGateError("approval_evidence_changed")
    if artifact_generation is not None and isinstance(artifact_generation, bool):
        raise ArchitectureGateError("approval_evidence_changed")
    try:
        submitted_completion_event_id = (
            int(review_completion_event_id)
            if review_completion_event_id is not None else None
        )
        submitted_artifact_generation = (
            int(artifact_generation)
            if artifact_generation is not None else None
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise ArchitectureGateError("approval_evidence_changed") from exc
    submitted_artifact_sha256 = (
        str(artifact_sha256).lower() if artifact_sha256 is not None else None
    )
    if now is None:
        approval_now = int(time.time())
    elif isinstance(now, bool) or not isinstance(now, int):
        raise ArchitectureGateError("approval_now_must_be_integer")
    else:
        approval_now = int(now)
    with write_txn(conn):
        gate = get_architecture_gate(conn, gate_id)
        if gate is None or gate.board_key != context.board_key:
            raise ArchitectureGateError("architecture_gate_scope_mismatch")
        if gate.state == "human_approved":
            if (
                gate.approval_actor_id == context.principal
                and gate.approval_surface == context.surface
                and gate.approved_digest == digest
                and gate.approval_review_task_id == (review_task_id or None)
                and gate.approval_review_completion_event_id == (
                    submitted_completion_event_id
                )
                and gate.approval_artifact_generation == (
                    submitted_artifact_generation
                )
                and gate.approval_artifact_sha256 == submitted_artifact_sha256
            ):
                return gate
            raise ArchitectureGateError("approval_replay_mismatch")
        if gate.state == "invalidated":
            raise ArchitectureGateError("approval_invalidated")
        if gate.state != "validated_awaiting_approval":
            raise ArchitectureGateError("approval_wrong_state")
        subject = _architecture_review_approval_subject_in_txn(conn, gate)
        subject_review_id = subject["review_task_id"]
        if subject_review_id is not None:
            if (
                str(review_task_id or "").strip() != subject_review_id
                or submitted_completion_event_id is None
                or submitted_completion_event_id != subject["review_completion_event_id"]
                or submitted_artifact_generation is None
                or submitted_artifact_generation != subject["artifact_generation"]
                or submitted_artifact_sha256 != subject["artifact_sha256"]
                or digest != subject["digest"]
            ):
                raise ArchitectureGateError("approval_evidence_changed")
        elif any(
            value is not None
            for value in (
                review_task_id,
                review_completion_event_id,
                artifact_generation,
                artifact_sha256,
            )
        ):
            raise ArchitectureGateError("approval_evidence_changed")
        expected_digest = str(subject["digest"] or "")
        if not digest or digest != expected_digest:
            raise ArchitectureGateError("approval_digest_mismatch")
        cur = conn.execute(
            """UPDATE architecture_gates
               SET state = 'human_approved', approval_actor_id = ?, approval_actor_type = ?,
                   approval_surface = ?, approved_digest = ?, approved_at = ?,
                   approval_review_task_id = ?,
                   approval_review_completion_event_id = ?,
                   approval_artifact_generation = ?,
                   approval_artifact_sha256 = ?,
                   row_version = row_version + 1, updated_at = ?
             WHERE gate_id = ? AND state = 'validated_awaiting_approval' AND row_version = ?""",
            (
                context.principal, context.actor_type, context.surface, digest,
                approval_now,
                subject_review_id,
                subject["review_completion_event_id"],
                subject["artifact_generation"],
                subject["artifact_sha256"],
                approval_now, gate_id, gate.row_version,
            ),
        )
        if cur.rowcount != 1:
            raise ArchitectureGateError("architecture_gate_cas_conflict")
        approved = get_architecture_gate(conn, gate_id)
        assert approved is not None
        approval_event_id = _append_gate_audit(
            conn,
            approved,
            "approval_approved",
            created_at=approval_now,
        )
        conn.execute(
            "UPDATE architecture_gates SET authorization_event_id = ? "
            "WHERE gate_id = ? AND authorization_event_id IS NULL",
            (approval_event_id, approved.gate_id),
        )
        approved = get_architecture_gate(conn, gate_id)
        assert approved is not None
        return approved


def reject_architecture_gate(
    conn: sqlite3.Connection, gate_id: str, context: MutationContext, digest: str,
) -> ArchitectureGate:
    """Record a human rejection without treating a UI projection as authority."""
    if context.actor_type != "human":
        raise ArchitectureGateError("approval_requires_human")
    if context.surface not in AUTHENTICATED_APPROVAL_SURFACES:
        raise ArchitectureGateError("approval_surface_not_authenticated")
    with write_txn(conn):
        gate = get_architecture_gate(conn, gate_id)
        if gate is None or gate.board_key != context.board_key:
            raise ArchitectureGateError("architecture_gate_scope_mismatch")
        if gate.state != "validated_awaiting_approval":
            raise ArchitectureGateError("approval_wrong_state")
        if digest != gate.design_digest:
            raise ArchitectureGateError("approval_digest_mismatch")
        cur = conn.execute(
            "UPDATE architecture_gates SET state = 'rejected', row_version = row_version + 1, updated_at = ? "
            "WHERE gate_id = ? AND state = 'validated_awaiting_approval' AND row_version = ?",
            (int(time.time()), gate_id, gate.row_version),
        )
        if cur.rowcount != 1:
            raise ArchitectureGateError("architecture_gate_cas_conflict")
        rejected = get_architecture_gate(conn, gate_id)
        assert rejected is not None
        _append_gate_audit(conn, rejected, "approval_rejected")
        return rejected


def issue_architecture_graph(
    conn: sqlite3.Connection,
    gate_id: str,
    context: MutationContext,
    tasks: list[dict[str, Any]],
    *,
    idempotency_key: str,
) -> list[str]:
    """Issue the one canonical implementation graph for a human-approved gate.

    The runtime-owned context is the authorization boundary. All graph rows are
    inserted in one transaction so a rejected duplicate cannot leave partial
    tasks or dependency edges behind.
    """
    if (
        context.actor_type != "orchestrator_agent"
        or context.profile != "orchestrator"
        or context.phase != "graph_issuance"
    ):
        raise ArchitectureGateError("architecture_graph_issuance_requires_orchestrator")
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise ValueError("architecture graph idempotency_key is required")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("architecture graph tasks must be a non-empty list")

    normalized: list[tuple[str, Optional[str], Optional[str], list[int]]] = []
    for index, raw in enumerate(tasks):
        if not isinstance(raw, dict):
            raise ValueError(f"architecture graph tasks[{index}] must be an object")
        title = raw.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ValueError(f"architecture graph tasks[{index}].title is required")
        assignee = _canonical_assignee(raw.get("assignee"))
        body = raw.get("body")
        if body is not None and not isinstance(body, str):
            raise ValueError(f"architecture graph tasks[{index}].body must be a string")
        parents = raw.get("parents") or []
        if not isinstance(parents, list) or any(
            not isinstance(parent, int) or parent < 0 or parent >= len(tasks) or parent == index
            for parent in parents
        ):
            raise ValueError(f"architecture graph tasks[{index}].parents is invalid")
        if len(set(parents)) != len(parents):
            raise ValueError(f"architecture graph tasks[{index}].parents has duplicates")
        normalized.append((title.strip(), assignee, body, parents))

    in_degree = [0] * len(normalized)
    descendants: list[list[int]] = [[] for _ in normalized]
    for index, (_, _, _, parents) in enumerate(normalized):
        for parent in parents:
            in_degree[index] += 1
            descendants[parent].append(index)
    ready = [index for index, degree in enumerate(in_degree) if degree == 0]
    visited = 0
    while ready:
        current = ready.pop()
        visited += 1
        for child in descendants[current]:
            in_degree[child] -= 1
            if in_degree[child] == 0:
                ready.append(child)
    if visited != len(normalized):
        raise ValueError("architecture graph contains a cycle")

    with write_txn(conn):
        gate = get_architecture_gate(conn, gate_id)
        if (
            gate is None
            or gate.board_key != context.board_key
            or gate.creator_principal != context.principal
        ):
            raise ArchitectureGateError("architecture_gate_scope_mismatch")
        if gate.state != "human_approved":
            raise ArchitectureGateError("architecture_graph_requires_human_approval")

        existing = conn.execute(
            "SELECT idempotency_key, task_ids FROM architecture_graph_issuances WHERE gate_id = ?",
            (gate_id,),
        ).fetchone()
        if existing is not None:
            if existing["idempotency_key"] == idempotency_key:
                try:
                    return list(json.loads(existing["task_ids"]))
                except (TypeError, json.JSONDecodeError) as exc:
                    raise ArchitectureGateError("architecture_graph_issuance_corrupt") from exc
            raise ArchitectureGateError("architecture_graph_issued")

        architect = get_task(conn, gate.architect_task_id)
        if architect is None:
            raise ArchitectureGateError("architecture_graph_architect_task_missing")
        now = int(time.time())
        task_ids = [_new_task_id() for _ in normalized]
        for index, (title, assignee, body, parents) in enumerate(normalized):
            task_status = "ready" if not parents else "todo"
            conn.execute(
                """INSERT INTO tasks (
                    id, title, body, assignee, status, created_by, created_at,
                    workspace_kind, workspace_path, tenant, session_id, workflow_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    task_ids[index], title, body, assignee, task_status,
                    "architecture-graph", now, architect.workspace_kind,
                    architect.workspace_path, architect.tenant, architect.session_id,
                    gate.workflow_key,
                ),
            )
            _append_event(
                conn, task_ids[index], "created",
                {"by": "architecture-graph", "gate_id": gate_id, "status": task_status},
            )
        for index, (_, _, _, parents) in enumerate(normalized):
            parent_ids_for_task = [task_ids[parent] for parent in parents]
            if not parent_ids_for_task:
                parent_ids_for_task = [gate.architect_task_id]
            for parent_id in parent_ids_for_task:
                conn.execute(
                    "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)",
                    (parent_id, task_ids[index]),
                )
                _append_event(
                    conn, task_ids[index], "linked",
                    {"parent": parent_id, "child": task_ids[index], "gate_id": gate_id},
                )
        conn.execute(
            """INSERT INTO architecture_graph_issuances
               (gate_id, idempotency_key, task_ids, issued_by, issued_at)
               VALUES (?, ?, ?, ?, ?)""",
            (gate_id, idempotency_key, json.dumps(task_ids), context.principal, now),
        )
        _append_gate_audit(conn, gate, "architecture_graph_issued")
        return task_ids


def _compiled_workflow_from_row(row: sqlite3.Row) -> CompiledWorkflowGraph:
    try:
        task_ids = json.loads(row["task_ids"])
        if not isinstance(task_ids, dict):
            raise TypeError("task_ids must be an object")
        terminal_task_id = task_ids[row["terminal_step_key"]]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise WorkflowGraphError("workflow graph compilation is corrupt") from exc
    return CompiledWorkflowGraph(
        workflow_key=str(row["workflow_key"]),
        task_ids={str(key): str(value) for key, value in task_ids.items()},
        terminal_task_id=str(terminal_task_id),
    )


def get_compiled_workflow_retry(
    conn: sqlite3.Connection,
    *,
    workflow_key: str,
    idempotency_key: str,
    request_digest: str,
) -> Optional[CompiledWorkflowGraph]:
    """Return an exact prior model-request compilation before volatile work.

    Route resolution, session ids, and delivery targets are execution-time
    snapshots. They must not turn a semantic retry into an identity conflict.
    """
    row = conn.execute(
        "SELECT * FROM workflow_graph_compilations WHERE workflow_key = ?",
        (str(workflow_key or "").strip(),),
    ).fetchone()
    if row is None:
        return None
    keys = set(row.keys())
    stored_request_digest = (
        row["request_digest"] if "request_digest" in keys else None
    ) or row["spec_digest"]
    if (
        row["idempotency_key"] != str(idempotency_key or "").strip()
        or stored_request_digest != str(request_digest or "").strip()
    ):
        raise WorkflowGraphError("workflow graph identity conflict")
    return _compiled_workflow_from_row(row)


def compile_workflow_graph(
    conn: sqlite3.Connection,
    *,
    workflow_key: str,
    idempotency_key: str,
    created_by: str,
    steps: list[dict[str, Any]],
    notification: Optional[dict[str, Any] | list[dict[str, Any]]] = None,
    tenant: Optional[str] = None,
    session_id: Optional[str] = None,
    workspace_kind: str = "scratch",
    workspace_path: Optional[str] = None,
    priority: int = 0,
    request_digest: Optional[str] = None,
    request_scope_id: Optional[str] = None,
    deny_active_architecture_session: bool = False,
) -> CompiledWorkflowGraph:
    """Compile one validated workflow graph into the Kanban kernel atomically.

    ``steps`` reference parents by stable step key and declare exactly one
    ``terminal`` step. Every step must reach that terminal. A notification, if
    supplied, is written only for the terminal task in the same transaction.
    Exact retries return the original ids; identity reuse with a different
    graph is rejected without modifying the board.
    """

    workflow_key = str(workflow_key or "").strip()
    idempotency_key = str(idempotency_key or "").strip()
    created_by = str(created_by or "").strip()
    if not workflow_key:
        raise WorkflowGraphError("workflow_key is required")
    if not idempotency_key:
        raise WorkflowGraphError("idempotency_key is required")
    if not created_by:
        raise WorkflowGraphError("created_by is required")
    if workspace_kind not in VALID_WORKSPACE_KINDS:
        raise WorkflowGraphError(
            f"workspace_kind must be one of {sorted(VALID_WORKSPACE_KINDS)}"
        )
    if not isinstance(steps, list) or not steps:
        raise WorkflowGraphError("workflow steps must be a non-empty list")

    allowed_step_fields = {
        "key", "title", "body", "assignee", "parents", "role", "terminal",
        "initial_status", "result", "skills", "toolsets", "max_runtime_seconds", "priority",
        "model_override", "model_provider_override", "model_reasoning_effort",
    }
    terminal_roles = {"finalizer", "synthesizer", "reporter"}
    normalized_steps: list[dict[str, Any]] = []
    step_keys: set[str] = set()
    for index, raw in enumerate(steps):
        if not isinstance(raw, dict):
            raise WorkflowGraphError(f"steps[{index}] must be an object")
        unknown_fields = set(raw) - allowed_step_fields
        if unknown_fields:
            raise WorkflowGraphError(
                f"steps[{index}] has unsupported fields: {sorted(unknown_fields)}"
            )
        step_key = str(raw.get("key") or "").strip()
        title = str(raw.get("title") or "").strip()
        assignee = _canonical_assignee(raw.get("assignee"))
        body = raw.get("body")
        parents = raw.get("parents") or []
        role = str(raw.get("role") or "worker").strip().lower()
        terminal = raw.get("terminal", False)
        initial_status = raw.get("initial_status")
        result = raw.get("result")
        raw_skills = raw.get("skills")
        raw_toolsets = raw.get("toolsets")
        max_runtime_seconds = raw.get("max_runtime_seconds")
        step_priority = raw.get("priority", priority)
        model_override = str(raw.get("model_override") or "").strip() or None
        model_provider_override = (
            str(raw.get("model_provider_override") or "").strip() or None
        )
        model_provider_override, model_override = _sanitize_denied_routing_override(
            model_provider_override, model_override, context="compile_workflow"
        )
        model_reasoning_effort = (
            str(raw.get("model_reasoning_effort") or "").strip().lower() or None
        )
        if not step_key:
            raise WorkflowGraphError(f"steps[{index}].key is required")
        if step_key in step_keys:
            raise WorkflowGraphError(f"duplicate workflow step key: {step_key}")
        if not title:
            raise WorkflowGraphError(f"steps[{index}].title is required")
        if not assignee:
            raise WorkflowGraphError(f"steps[{index}].assignee is required")
        if body is not None and not isinstance(body, str):
            raise WorkflowGraphError(f"steps[{index}].body must be a string")
        if not role:
            raise WorkflowGraphError(f"steps[{index}].role is required")
        if not isinstance(parents, list) or any(
            not isinstance(parent, str) or not parent.strip() for parent in parents
        ):
            raise WorkflowGraphError(f"steps[{index}].parents must be step keys")
        normalized_parents = [parent.strip() for parent in parents]
        if len(set(normalized_parents)) != len(normalized_parents):
            raise WorkflowGraphError(f"steps[{index}].parents has duplicates")
        if not isinstance(terminal, bool):
            raise WorkflowGraphError(f"steps[{index}].terminal must be boolean")
        if initial_status not in {None, "ready", "todo", "done"}:
            raise WorkflowGraphError(
                f"steps[{index}].initial_status must be ready, todo, or done"
            )
        if result is not None and not isinstance(result, str):
            raise WorkflowGraphError(f"steps[{index}].result must be a string")
        if raw_skills is not None and not isinstance(raw_skills, list):
            raise WorkflowGraphError(f"steps[{index}].skills must be a list")
        skills: list[str] = []
        for skill in raw_skills or []:
            name = str(skill or "").strip()
            if not name:
                continue
            if "," in name:
                raise WorkflowGraphError(
                    f"steps[{index}].skill names cannot contain commas"
                )
            if name not in skills:
                skills.append(name)
        skill_validation_error = _forced_skill_validation_error(assignee, skills)
        if skill_validation_error:
            raise WorkflowGraphError(skill_validation_error)
        if raw_toolsets is not None and not isinstance(raw_toolsets, list):
            raise WorkflowGraphError(f"steps[{index}].toolsets must be a list")
        toolsets: Optional[list[str]] = None
        if raw_toolsets is not None:
            toolsets = []
            unknown_toolsets: list[str] = []
            for raw_toolset in raw_toolsets:
                name = str(raw_toolset or "").strip().casefold()
                if not name:
                    continue
                if name not in KNOWN_TOOLSET_NAMES:
                    unknown_toolsets.append(name)
                elif name not in toolsets:
                    toolsets.append(name)
            if unknown_toolsets:
                raise WorkflowGraphError(
                    f"steps[{index}] has unknown toolsets: {sorted(unknown_toolsets)}"
                )
            if not toolsets:
                raise WorkflowGraphError(
                    f"steps[{index}].toolsets must contain at least one toolset"
                )
        if max_runtime_seconds is not None:
            try:
                max_runtime_seconds = int(max_runtime_seconds)
            except (TypeError, ValueError) as exc:
                raise WorkflowGraphError(
                    f"steps[{index}].max_runtime_seconds must be an integer"
                ) from exc
            if max_runtime_seconds <= 0:
                raise WorkflowGraphError(
                    f"steps[{index}].max_runtime_seconds must be positive"
                )
        try:
            step_priority = int(step_priority)
        except (TypeError, ValueError) as exc:
            raise WorkflowGraphError(
                f"steps[{index}].priority must be an integer"
            ) from exc
        supported_efforts = {"none", *VALID_REASONING_EFFORTS}
        if (
            model_reasoning_effort is not None
            and model_reasoning_effort not in supported_efforts
        ):
            raise WorkflowGraphError(
                f"steps[{index}].model_reasoning_effort must be one of "
                + ", ".join(sorted(supported_efforts))
            )
        step_keys.add(step_key)
        normalized_steps.append(
            {
                "key": step_key,
                "title": title,
                "assignee": assignee,
                "body": body,
                "parents": normalized_parents,
                "role": role,
                "terminal": terminal,
                "initial_status": initial_status,
                "result": result,
                "skills": skills,
                "toolsets": toolsets,
                "max_runtime_seconds": max_runtime_seconds,
                "priority": step_priority,
                "model_override": model_override,
                "model_provider_override": model_provider_override,
                "model_reasoning_effort": model_reasoning_effort,
            }
        )

    by_key = {step["key"]: step for step in normalized_steps}
    for step in normalized_steps:
        unknown = [parent for parent in step["parents"] if parent not in by_key]
        if unknown:
            raise WorkflowGraphError(
                f"workflow step {step['key']} has unknown parent(s): {', '.join(unknown)}"
            )
        if step["key"] in step["parents"]:
            raise WorkflowGraphError(f"workflow step {step['key']} cannot depend on itself")
        if step["initial_status"] == "done" and step["parents"]:
            raise WorkflowGraphError(
                f"precompleted workflow step {step['key']} cannot have parents"
            )
        if step["initial_status"] == "ready" and step["parents"]:
            raise WorkflowGraphError(
                f"ready workflow step {step['key']} cannot have unfinished parents"
            )

    terminal_keys = [step["key"] for step in normalized_steps if step["terminal"]]
    if len(terminal_keys) != 1:
        raise WorkflowGraphError("workflow graph must declare exactly one terminal step")
    terminal_key = terminal_keys[0]
    if by_key[terminal_key]["role"] not in terminal_roles:
        raise WorkflowGraphError(
            "workflow terminal role must be finalizer, synthesizer, or reporter"
        )
    if by_key[terminal_key]["initial_status"] == "done":
        raise WorkflowGraphError("workflow terminal cannot start completed")

    children: dict[str, list[str]] = {key: [] for key in by_key}
    in_degree = {key: len(step["parents"]) for key, step in by_key.items()}
    for step in normalized_steps:
        for parent in step["parents"]:
            children[parent].append(step["key"])
    ready = [key for key, degree in in_degree.items() if degree == 0]
    visited = 0
    while ready:
        current = ready.pop()
        visited += 1
        for child in children[current]:
            in_degree[child] -= 1
            if in_degree[child] == 0:
                ready.append(child)
    if visited != len(normalized_steps):
        raise WorkflowGraphError("workflow graph contains a cycle")

    reaches_terminal = {terminal_key}
    frontier = [terminal_key]
    while frontier:
        current = frontier.pop()
        for parent in by_key[current]["parents"]:
            if parent not in reaches_terminal:
                reaches_terminal.add(parent)
                frontier.append(parent)
    unreachable = [step["key"] for step in normalized_steps if step["key"] not in reaches_terminal]
    if unreachable:
        raise WorkflowGraphError(
            "every workflow step must reach the terminal; unreachable: "
            + ", ".join(unreachable)
        )

    normalized_notifications: list[dict[str, Optional[str]]] = []
    if notification is not None:
        raw_notifications = notification if isinstance(notification, list) else [notification]
        if not all(isinstance(item, dict) for item in raw_notifications):
            raise WorkflowGraphError("notification must be an object or list of objects")
        for item in raw_notifications:
            platform = str(item.get("platform") or "").strip()
            chat_id = str(item.get("chat_id") or "").strip()
            if not platform or not chat_id:
                raise WorkflowGraphError("notification platform and chat_id are required")
            normalized = {
                "platform": platform,
                "chat_id": chat_id,
                "thread_id": str(item.get("thread_id") or ""),
                "user_id": (
                    str(item["user_id"])
                    if item.get("user_id") is not None
                    else None
                ),
                "notifier_profile": (
                    str(item["notifier_profile"])
                    if item.get("notifier_profile") is not None
                    else None
                ),
            }
            if normalized not in normalized_notifications:
                normalized_notifications.append(normalized)
        normalized_notifications.sort(
            key=lambda item: (
                str(item["platform"]), str(item["chat_id"]),
                str(item["thread_id"]), str(item["user_id"]),
            )
        )

    canonical_steps = [
        {**step, "parents": sorted(step["parents"])}
        for step in sorted(normalized_steps, key=lambda item: item["key"])
    ]
    canonical_spec = {
        "version": 1,
        "workflow_key": workflow_key,
        "created_by": created_by,
        "steps": canonical_steps,
        "notification": normalized_notifications,
        "tenant": tenant,
        "session_id": session_id,
        "workspace_kind": workspace_kind,
        "workspace_path": workspace_path,
        "priority": int(priority),
    }
    spec_digest = hashlib.sha256(
        json.dumps(
            canonical_spec,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    request_digest = str(request_digest or "").strip() or spec_digest

    # Invariant 1 (BUILD-496), compiler surface: the compiler applies one
    # workspace_kind/workspace_path to every step. Reject a worktree
    # compilation with no materializable anchor BEFORE opening the write
    # transaction, so the whole all-or-none compilation fails closed instead
    # of persisting a graph whose every step deterministically fails at
    # dispatch. Mirrors create_task's board-default fallback (current board
    # when no explicit board is threaded through the compiler today).
    if workspace_kind == "worktree" and not workspace_path:
        board_default = (
            read_board_metadata(get_current_board()).get("default_workdir") or ""
        ).strip()
        if not board_default:
            raise WorkspaceContractError(
                "worktree_no_anchor",
                "workflow workspace_kind='worktree' needs an explicit "
                "workspace_path or a board default_workdir; none is set, so "
                "every compiled step would fail at dispatch.",
            )

    with write_txn(conn):
        existing = conn.execute(
            "SELECT * FROM workflow_graph_compilations WHERE workflow_key = ?",
            (workflow_key,),
        ).fetchone()
        if existing is not None:
            existing_keys = set(existing.keys())
            stored_request_digest = (
                existing["request_digest"]
                if "request_digest" in existing_keys else None
            ) or existing["spec_digest"]
            expected_digest = request_digest if request_digest else spec_digest
            if (
                existing["idempotency_key"] != idempotency_key
                or stored_request_digest != expected_digest
            ):
                raise WorkflowGraphError("workflow graph identity conflict")
            return _compiled_workflow_from_row(existing)

        if deny_active_architecture_session and session_id:
            active_gate = conn.execute(
                """SELECT gate_id FROM architecture_gates
                   WHERE (session_id = ? OR request_scope_id = ?)
                     AND state IN ('open', 'validated_awaiting_approval',
                                   'policy_accepted', 'human_approved')
                   LIMIT 1""",
                (session_id, request_scope_id),
            ).fetchone()
            if active_gate is not None:
                raise WorkflowGraphError("architecture_graph_issuance_required")

        partial = conn.execute(
            "SELECT 1 FROM tasks WHERE workflow_key = ? LIMIT 1",
            (workflow_key,),
        ).fetchone()
        if partial is not None:
            raise WorkflowGraphError("workflow graph identity conflict")

        now = int(time.time())
        task_ids = {step["key"]: _new_task_id() for step in normalized_steps}
        for step in normalized_steps:
            if step["initial_status"] is not None:
                task_status = step["initial_status"]
            else:
                task_status = (
                    "ready"
                    if all(by_key[parent]["initial_status"] == "done" for parent in step["parents"])
                    else "todo"
                )
            conn.execute(
                """INSERT INTO tasks (
                    id, title, body, assignee, status, priority, created_by,
                    created_at, workspace_kind, workspace_path, tenant,
                    session_id, workflow_key, current_step_key, result,
                    completed_at, skills, toolsets, model_override,
                    model_provider_override, model_reasoning_effort,
                    max_runtime_seconds
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    task_ids[step["key"]],
                    step["title"],
                    step["body"],
                    step["assignee"],
                    task_status,
                    step["priority"],
                    created_by,
                    now,
                    workspace_kind,
                    workspace_path,
                    tenant,
                    session_id,
                    workflow_key,
                    step["key"],
                    step["result"],
                    now if task_status == "done" else None,
                    json.dumps(step["skills"]) if step["skills"] else None,
                    (
                        json.dumps(step["toolsets"])
                        if step["toolsets"] is not None
                        else None
                    ),
                    step["model_override"],
                    step["model_provider_override"],
                    step["model_reasoning_effort"],
                    step["max_runtime_seconds"],
                ),
            )
            _append_event(
                conn,
                task_ids[step["key"]],
                "created",
                {
                    "by": "workflow-compiler",
                    "workflow_key": workflow_key,
                    "step_key": step["key"],
                    "role": step["role"],
                    "parents": list(step["parents"]),
                    "terminal": step["terminal"],
                    "status": task_status,
                    "model_override": step["model_override"],
                    "model_provider_override": step["model_provider_override"],
                    "model_reasoning_effort": step["model_reasoning_effort"],
                },
            )
            if task_status == "done":
                _append_event(
                    conn,
                    task_ids[step["key"]],
                    "completed",
                    {
                        "by": "workflow-compiler",
                        "workflow_key": workflow_key,
                        "step_key": step["key"],
                    },
                )
        for step in normalized_steps:
            for parent in step["parents"]:
                conn.execute(
                    "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)",
                    (task_ids[parent], task_ids[step["key"]]),
                )
                _append_event(
                    conn,
                    task_ids[step["key"]],
                    "linked",
                    {
                        "parent": task_ids[parent],
                        "child": task_ids[step["key"]],
                        "workflow_key": workflow_key,
                    },
                )

        terminal_task_id = task_ids[terminal_key]
        # BUILD-503 / ADR invariant 10-11: subscribe the origin to EVERY step
        # task, not just the terminal one. A nonterminal step that blocks,
        # gives up, or fails to spawn used to strand the workflow silently
        # because only the terminal task carried a subscription and its events
        # never fired (2026-07-16 incident). Each step gets its own
        # subscription row so the existing per-task cursor/claim machinery
        # (claim_unseen_events_for_sub) delivers and dedupes it exactly-once —
        # the only storage model consistent with that per-task cursor. Skip
        # pre-`done` steps: they already succeeded and re-subscribing would
        # replay their `completed` event as noise. INSERT OR IGNORE keeps the
        # terminal row idempotent.
        # BUILD-508: non-terminal step subs are narrowed to FAILURE_KINDS —
        # the terminal task's own `completed` event is the real "workflow
        # finished" signal, so a step merely completing on schedule no longer
        # pings the origin (this is the upgrade path BUILD-503 named in its
        # ponytail comment). The terminal task keeps kinds_json=NULL (all
        # kinds), unchanged from pre-BUILD-508 behavior.
        # A step is persisted `done` only when it was explicitly seeded that
        # way (the ready/todo fallback above never yields `done`).
        subscribe_task_ids = [
            task_ids[step["key"]]
            for step in normalized_steps
            if step["initial_status"] != "done"
        ]
        failure_kinds_json = json.dumps(sorted(FAILURE_KINDS))
        for normalized_notification in normalized_notifications:
            for sub_task_id in subscribe_task_ids:
                kinds_json = (
                    None if sub_task_id == terminal_task_id else failure_kinds_json
                )
                conn.execute(
                    """INSERT OR IGNORE INTO kanban_notify_subs
                        (task_id, platform, chat_id, thread_id, user_id,
                         notifier_profile, created_at, kinds_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        sub_task_id,
                        normalized_notification["platform"],
                        normalized_notification["chat_id"],
                        normalized_notification["thread_id"],
                        normalized_notification["user_id"],
                        normalized_notification["notifier_profile"],
                        now,
                        kinds_json,
                    ),
                )
        conn.execute(
            """INSERT INTO workflow_graph_compilations
                (workflow_key, idempotency_key, spec_digest, request_digest,
                 task_ids, terminal_step_key, created_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                workflow_key,
                idempotency_key,
                spec_digest,
                request_digest,
                json.dumps(task_ids, sort_keys=True),
                terminal_key,
                created_by,
                now,
            ),
        )
        return CompiledWorkflowGraph(
            workflow_key=workflow_key,
            task_ids=task_ids,
            terminal_task_id=terminal_task_id,
        )


def issue_discovery_capability(
    conn: sqlite3.Connection,
    gate_id: str,
    issuer: MutationContext,
    *,
    principal: str,
    session_id: str,
    request_scope_id: str,
    profile: str,
) -> DiscoveryCapability:
    """Issue a one-purpose current-turn capability from an authenticated UI."""
    if issuer.actor_type != "human" or issuer.surface not in AUTHENTICATED_APPROVAL_SURFACES:
        raise ArchitectureGateError("discovery_capability_requires_human")
    if profile not in READ_ONLY_DISCOVERY_PROFILES or not all((principal, session_id, request_scope_id)):
        raise ArchitectureGateError("discovery_capability_invalid_binding")
    with write_txn(conn):
        gate = get_architecture_gate(conn, gate_id)
        if gate is None or gate.board_key != issuer.board_key or gate.state not in ARCHITECTURE_GATE_ACTIVE_STATES:
            raise ArchitectureGateError("discovery_capability_gate_unavailable")
        token = secrets.token_urlsafe(24)
        now = int(time.time())
        expires_at = now + DISCOVERY_CAPABILITY_TTL_SECONDS
        conn.execute(
            """INSERT INTO discovery_capabilities
               (token, gate_id, board_key, principal, session_id, request_scope_id, profile, issued_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (token, gate_id, gate.board_key, principal, session_id, request_scope_id, profile, now, expires_at),
        )
        _append_gate_audit(conn, gate, "discovery_capability_issued")
        return DiscoveryCapability(token, gate_id, gate.board_key, principal, session_id, request_scope_id, profile, now, expires_at, None)


def _consume_discovery_capability(
    conn: sqlite3.Connection, gate: ArchitectureGate, context: MutationContext, assignee: Optional[str],
) -> None:
    if context.phase != "discovery":
        raise ArchitectureGateError(ARCHITECTURE_GATE_REASON_OPEN)
    if not context.discovery_capability:
        raise ArchitectureGateError("discovery_capability_missing")
    row = conn.execute(
        "SELECT * FROM discovery_capabilities WHERE token = ?", (context.discovery_capability,)
    ).fetchone()
    if row is None:
        raise ArchitectureGateError("discovery_capability_forged")
    if row["used_at"] is not None:
        raise ArchitectureGateError("discovery_capability_used")
    if int(row["expires_at"] or 0) <= int(time.time()):
        raise ArchitectureGateError("discovery_capability_expired")
    if (
        row["gate_id"] != gate.gate_id or row["board_key"] != context.board_key
        or row["principal"] != context.principal or row["session_id"] != context.session_id
        or row["request_scope_id"] != context.request_scope_id
        or row["profile"] != context.profile or assignee != context.profile
        or context.profile not in READ_ONLY_DISCOVERY_PROFILES
    ):
        raise ArchitectureGateError("discovery_capability_binding_mismatch")
    cur = conn.execute(
        "UPDATE discovery_capabilities SET used_at = ? WHERE token = ? AND used_at IS NULL",
        (int(time.time()), context.discovery_capability),
    )
    if cur.rowcount != 1:
        raise ArchitectureGateError("discovery_capability_used")
    _append_gate_audit(conn, gate, "discovery_capability_used")


def classify_policy_quarantine(
    conn: sqlite3.Connection, gate_id: str,
) -> list[PolicyQuarantineClassification]:
    """Read-only containment report for descendants created before authorization.

    ``task_events.id`` supplies transaction ordering that wall-clock seconds
    cannot.  Descendants created after the gate's accepted/approved audit
    receipt remain valid, including a canonical graph issued in that window.
    Unknown raw rows are left for the claim backstop instead of guessing that
    post-approval work was premature.
    """
    gate = get_architecture_gate(conn, gate_id)
    if gate is None:
        raise ValueError("unknown architecture gate")
    issued_ids: set[str] = set()
    issuance = conn.execute(
        "SELECT task_ids FROM architecture_graph_issuances WHERE gate_id = ?", (gate_id,)
    ).fetchone()
    if issuance is not None:
        try:
            issued_ids = set(json.loads(issuance["task_ids"]))
        except (TypeError, json.JSONDecodeError) as exc:
            raise ArchitectureGateError("architecture_graph_issuance_corrupt") from exc
    seen: set[str] = {gate.architect_task_id}
    stack = [gate.architect_task_id]
    classified: list[PolicyQuarantineClassification] = []
    while stack:
        parent = stack.pop()
        for child in child_ids(conn, parent):
            if child in seen:
                continue
            seen.add(child)
            stack.append(child)
            if child in issued_ids:
                continue
            created = conn.execute(
                "SELECT MIN(id) AS event_id FROM task_events "
                "WHERE task_id = ? AND kind = 'created'", (child,),
            ).fetchone()
            created_event_id = int(created["event_id"]) if created and created["event_id"] is not None else None
            if gate.authorization_event_id is None or (
                created_event_id is not None and created_event_id < gate.authorization_event_id
            ):
                classified.append(PolicyQuarantineClassification(child))
    return classified


def apply_policy_quarantine(
    conn: sqlite3.Connection,
    gate_id: str,
    *,
    context: MutationContext,
    signal_fn=None,
) -> set[str]:
    """Human-authorized containment with worker termination outside the txn."""
    if context.actor_type != "human" or context.surface not in AUTHENTICATED_APPROVAL_SURFACES:
        raise ArchitectureGateError("containment_requires_authenticated_human")
    terminations: list[
        tuple[
            str,
            Optional[int],
            Optional[str],
            Optional[int],
            Optional[float],
            Optional[int],
            Optional[int],
        ]
    ] = []
    with write_txn(conn):
        gate = get_architecture_gate(conn, gate_id)
        if gate is None:
            raise ValueError("unknown architecture gate")
        if gate.board_key != context.board_key:
            raise ArchitectureGateError("architecture_gate_scope_mismatch")
        task_ids = {item.task_id for item in classify_policy_quarantine(conn, gate_id)}
        now = int(time.time())
        for task_id in task_ids:
            row = conn.execute(
                "SELECT status, current_run_id, worker_pid, claim_lock, "
                "worker_started_at, worker_pgid, worker_sid "
                "FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                continue
            running_run_id = row["current_run_id"] if row["status"] == "running" else None
            if running_run_id is not None:
                terminations.append(
                    (
                        task_id,
                        row["worker_pid"],
                        row["claim_lock"],
                        running_run_id,
                        row["worker_started_at"],
                        row["worker_pgid"],
                        row["worker_sid"],
                    )
                )
            conn.execute(
                """UPDATE tasks
                   SET policy_quarantined = 1, policy_invalidated = CASE WHEN status = 'done' THEN 1 ELSE policy_invalidated END,
                       policy_quarantine_reason = ?, claim_lock = NULL, claim_expires = NULL,
                       worker_pid = NULL, worker_started_at = NULL,
                       worker_pgid = NULL, worker_sid = NULL,
                       current_run_id = NULL,
                       status = CASE WHEN status = 'done' THEN status ELSE 'blocked' END
                 WHERE id = ? AND policy_quarantined = 0""",
                ("architecture_gate_premature_card", task_id),
            )
            if running_run_id is not None:
                conn.execute(
                    """UPDATE task_runs
                       SET status = 'policy_quarantined', outcome = 'policy_quarantined',
                           summary = COALESCE(summary, 'policy quarantine'), ended_at = ?,
                           claim_lock = NULL, claim_expires = NULL,
                           worker_pid = NULL, worker_started_at = NULL,
                           worker_pgid = NULL, worker_sid = NULL
                     WHERE id = ? AND ended_at IS NULL""",
                    (now, int(running_run_id)),
                )
            _append_event(
                conn, task_id, "quarantined",
                {"reason": "architecture_gate_premature_card", "gate_id": gate_id},
                run_id=int(running_run_id) if running_run_id is not None else None,
            )
            if row["status"] in {"running", "done"}:
                _append_event(conn, task_id, "human_review_required", {"gate_id": gate_id})
        _append_gate_audit(conn, gate, "containment_quarantined")

    # Never signal a worker while the database transaction holds the board
    # lock.  Capture identity inside the transaction, then terminate after its
    # durable lease has been removed, preserving the reclaimed-worker pattern.
    termination_results: dict[str, dict[str, Any]] = {}
    for task_id, pid, lock, run_id, started_at, pgid, sid in terminations:
        termination_results[task_id] = _terminate_worker_for_task(
            pid,
            lock,
            task_id=task_id,
            run_id=run_id,
            worker_started_at=started_at,
            worker_pgid=pgid,
            worker_sid=sid,
            signal_fn=signal_fn,
        )
    if termination_results:
        with write_txn(conn):
            for task_id, result in termination_results.items():
                _append_event(conn, task_id, "worker_termination", result)
    return task_ids


def _invalidate_architecture_gate_in_txn(
    conn: sqlite3.Connection,
    gate_id: str,
    *,
    reason: str,
    now: Optional[int] = None,
) -> ArchitectureGate:
    """CAS invalidation for an owning mutation already holding ``write_txn``."""
    gate = get_architecture_gate(conn, gate_id)
    if gate is None:
        raise ValueError("unknown architecture gate")
    if gate.state == "invalidated":
        return gate
    invalidation_now = int(time.time()) if now is None else int(now)
    cur = conn.execute(
        """UPDATE architecture_gates SET state = 'invalidated', row_version = row_version + 1,
           updated_at = ? WHERE gate_id = ? AND row_version = ?""",
        (invalidation_now, gate_id, gate.row_version),
    )
    if cur.rowcount != 1:
        raise ArchitectureGateError("architecture_gate_cas_conflict")
    invalidated = get_architecture_gate(conn, gate_id)
    assert invalidated is not None
    _append_gate_audit(
        conn,
        invalidated,
        "approval_invalidated",
        reason,
        created_at=invalidation_now,
    )
    return invalidated


def _invalidate_review_artifact_authorizations_in_txn(
    conn: sqlite3.Connection,
    review_task_id: str,
    *,
    reason: str,
    now: Optional[int] = None,
) -> int:
    """Invalidate approvals whose authenticated subject names this review generation."""
    rows = conn.execute(
        "SELECT gate_id FROM architecture_gates "
        "WHERE state = 'human_approved' AND approval_review_task_id = ?",
        (review_task_id,),
    ).fetchall()
    invalidated = 0
    for row in rows:
        _invalidate_architecture_gate_in_txn(
            conn, row["gate_id"], reason=reason, now=now,
        )
        invalidated += 1
    return invalidated


def _invalidate_architect_gate_for_mutation(
    conn: sqlite3.Connection, task_id: str, *, reason: str,
) -> Optional[ArchitectureGate]:
    """Invalidate a previously accepted architect gate in its owning write."""
    gate = get_architecture_gate_for_task(conn, task_id)
    if gate is None or gate.architect_task_id != task_id:
        return None
    if gate.state not in ARCHITECTURE_GATE_APPROVED_STATES | {"validated_awaiting_approval"}:
        return None
    return _invalidate_architecture_gate_in_txn(conn, gate.gate_id, reason=reason)


def invalidate_architecture_gate(conn: sqlite3.Connection, gate_id: str, *, reason: str) -> ArchitectureGate:
    with write_txn(conn):
        return _invalidate_architecture_gate_in_txn(conn, gate_id, reason=reason)


def reopen_architecture_gate(
    conn: sqlite3.Connection, gate_id: str, context: MutationContext,
) -> ArchitectureGate:
    """CAS-reopen an invalidated handoff for its original architecture owner."""
    with write_txn(conn):
        gate = get_architecture_gate(conn, gate_id)
        if gate is None:
            raise ValueError("unknown architecture gate")
        if (
            context.phase != "architecture"
            or context.board_key != gate.board_key
            or context.principal != gate.creator_principal
            or (
                gate.creator_actor_type is not None
                and context.actor_type != gate.creator_actor_type
            )
            or (
                gate.creator_profile is not None
                and context.profile != gate.creator_profile
            )
            or context.session_id != gate.session_id
            or context.workflow_key != gate.workflow_key
            or context.request_scope_id != gate.request_scope_id
        ):
            raise ArchitectureGateError("architecture_gate_reopen_requires_owner")
        if gate.state == "open":
            return gate
        if gate.state != "invalidated":
            raise ArchitectureGateError("architecture_gate_reopen_requires_invalidation")
        cur = conn.execute(
            """UPDATE architecture_gates
               SET state = 'open', accepted_run_id = NULL, accepted_snapshot = NULL,
                   design_digest = NULL, approval_actor_id = NULL, approval_actor_type = NULL,
                   approval_surface = NULL, approved_digest = NULL, approved_at = NULL,
                   approval_review_task_id = NULL,
                   approval_review_completion_event_id = NULL,
                   approval_artifact_generation = NULL,
                   approval_artifact_sha256 = NULL,
                   authorization_event_id = NULL, row_version = row_version + 1, updated_at = ?
             WHERE gate_id = ? AND state = 'invalidated' AND row_version = ?""",
            (int(time.time()), gate_id, gate.row_version),
        )
        if cur.rowcount != 1:
            raise ArchitectureGateError("architecture_gate_cas_conflict")
        conn.execute(
            """UPDATE tasks SET status = 'ready', current_run_id = NULL,
                       completed_at = NULL, worker_pid = NULL,
                       worker_started_at = NULL, worker_pgid = NULL,
                       worker_sid = NULL
                 WHERE id = ? AND status = 'done'""",
            (gate.architect_task_id,),
        )
        reopened = get_architecture_gate(conn, gate_id)
        assert reopened is not None
        _append_gate_audit(conn, reopened, "architecture_gate_reopened", "owner_retry")
        return reopened


def _authorize_mutation(
    conn: sqlite3.Connection,
    context: Optional[MutationContext],
    *,
    task_id: Optional[str] = None,
    assignee: Optional[str] = None,
) -> Optional[ArchitectureGate]:
    if context is None or context.mode.strip().lower() == "off":
        return None
    gate = (
        get_architecture_gate_for_task(conn, task_id)
        if task_id
        else _active_scope_gate(conn, context, include_terminal=True)
    )
    if gate is None:
        return None
    if context.mode.strip().lower() == "shadow" and gate.state not in ARCHITECTURE_GATE_APPROVED_STATES:
        _append_gate_audit(conn, gate, "create_allowed", ARCHITECTURE_GATE_REASON_OPEN)
        return gate
    if gate.enforcement_mode in ARCHITECTURE_GATE_ENFORCING_MODES and gate.state == "human_approved":
        issued = conn.execute(
            "SELECT 1 FROM architecture_graph_issuances WHERE gate_id = ?", (gate.gate_id,)
        ).fetchone()
        if issued is not None:
            raise ArchitectureGateError("architecture_graph_issued")
        if context.phase != "graph_issuance":
            raise ArchitectureGateError("architecture_graph_issuance_required")
    if _gate_requires_enforcement(gate):
        if context.phase == "discovery":
            _consume_discovery_capability(conn, gate, context, assignee)
        elif context.phase != "architecture":
            raise ArchitectureGateError(ARCHITECTURE_GATE_REASON_OPEN)
    return gate


def _prepare_task_create(
    *,
    title: str,
    body: Optional[str] = None,
    assignee: Optional[str] = None,
    created_by: Optional[str] = None,
    workspace_kind: Optional[str] = "scratch",
    workspace_path: Optional[str] = None,
    branch_name: Optional[str] = None,
    tenant: Optional[str] = None,
    priority: Optional[int] = 0,
    parents: Iterable[str] = (),
    triage: bool = False,
    max_runtime_seconds: Optional[int] = None,
    skills: Optional[Iterable[str]] = None,
    toolsets: Optional[Iterable[str]] = None,
    model_override: Optional[str] = None,
    model_provider_override: Optional[str] = None,
    model_reasoning_effort: Optional[str] = None,
    max_retries: Optional[int] = None,
    goal_mode: bool = False,
    goal_max_turns: Optional[int] = None,
    initial_status: str = "running",
    session_id: Optional[str] = None,
    workflow_key: Optional[str] = None,
    workflow_template_id: Optional[str] = None,
    current_step_key: Optional[str] = None,
    publication_expected_sha: Optional[str] = None,
    publication_remote: Optional[str] = None,
    publication_ref: Optional[str] = None,
    board: Optional[str] = None,
    project_id: Optional[str] = None,
) -> _PreparedTaskCreate:
    """Validate and normalize task creation without touching the board DB."""
    assignee = _canonical_assignee(assignee)
    model_override = str(model_override).strip() or None if model_override is not None else None
    model_provider_override = (
        str(model_provider_override).strip() or None
        if model_provider_override is not None else None
    )
    model_provider_override, model_override = _sanitize_denied_routing_override(
        model_provider_override, model_override, context="create_task"
    )
    if model_reasoning_effort is not None:
        model_reasoning_effort = str(model_reasoning_effort).strip().lower() or None
        supported_efforts = {"none", *VALID_REASONING_EFFORTS}
        if model_reasoning_effort and model_reasoning_effort not in supported_efforts:
            raise ValueError(
                "model_reasoning_effort must be one of "
                + ", ".join(sorted(supported_efforts))
            )
    if not title or not title.strip():
        raise ValueError("title is required")
    if initial_status not in VALID_INITIAL_STATUSES:
        raise ValueError(
            f"initial_status must be one of {sorted(VALID_INITIAL_STATUSES)}"
        )
    if workspace_kind is None:
        workspace_kind = "scratch"
    if workspace_kind not in VALID_WORKSPACE_KINDS:
        raise ValueError(
            f"workspace_kind must be one of {sorted(VALID_WORKSPACE_KINDS)}, "
            f"got {workspace_kind!r}"
        )
    branch_name = str(branch_name).strip() or None if branch_name is not None else None

    project_obj = None
    project_repo: Optional[str] = None
    project_id = str(project_id).strip() or None if project_id is not None else None
    if project_id:
        try:
            from hermes_cli import projects_db as _pdb

            with _pdb.connect_closing() as _pconn:
                project_obj = _pdb.get_project(_pconn, project_id)
        except Exception:
            project_obj = None
        if project_obj is None:
            raise WorkspaceContractError(
                "unknown_project",
                f"project {project_id!r} does not resolve in this profile's "
                "project registry; create or bind it (`hermes project ...`) "
                "before linking, or omit the project reference.",
            )
        project_id = project_obj.id
        if workspace_kind == "scratch" and project_obj.primary_path:
            workspace_kind = "worktree"
        if (
            workspace_kind == "worktree"
            and workspace_path is None
            and project_obj.primary_path
        ):
            project_repo = str(project_obj.primary_path)

    if branch_name and workspace_kind != "worktree":
        raise ValueError("branch_name is only valid for worktree workspaces")

    publication_values = (
        publication_expected_sha,
        publication_remote,
        publication_ref,
    )
    if any(value is not None for value in publication_values):
        if any(value is None or not str(value).strip() for value in publication_values):
            raise ValueError(
                "publication_expected_sha, publication_remote, and publication_ref "
                "must be provided together"
            )
        publication_expected_sha = str(publication_expected_sha).strip().lower()
        if not re.fullmatch(r"[0-9a-f]{7,64}", publication_expected_sha):
            raise ValueError(
                "publication_expected_sha must be a hexadecimal commit SHA "
                "(7 to 64 characters)"
            )
        publication_remote = str(publication_remote).strip()
        if (
            not publication_remote
            or publication_remote.startswith("-")
            or any(ch.isspace() for ch in publication_remote)
        ):
            raise ValueError("publication_remote must be a non-empty token without whitespace")
        publication_ref = str(publication_ref).strip()
        if not publication_ref:
            raise ValueError("publication_ref is required")
        if not publication_ref.startswith("refs/"):
            publication_ref = f"refs/heads/{publication_ref.lstrip('/')}"
        if any(ch.isspace() for ch in publication_ref) or publication_ref == "refs/heads/":
            raise ValueError("publication_ref must be a non-empty git ref without whitespace")
        if workspace_kind == "scratch":
            # A publication card reuses the coder's checkout. Keep it out of
            # scratch cleanup even when the source worker came from scratch.
            workspace_kind = "dir"
        if not workspace_path or not str(workspace_path).strip():
            raise ValueError("publication workspace_path is required")
        workspace_path = str(Path(str(workspace_path).strip()).expanduser())
        if not Path(workspace_path).is_absolute():
            raise ValueError("publication workspace_path must be absolute")
    else:
        publication_expected_sha = None
        publication_remote = None
        publication_ref = None

    parents = tuple(p for p in parents if p)

    skills_list: Optional[list[str]] = None
    if skills is not None:
        cleaned: list[str] = []
        seen: set[str] = set()
        toolset_typos: list[str] = []
        for raw_skill in skills:
            if not raw_skill:
                continue
            name = str(raw_skill).strip()
            if not name:
                continue
            if "," in name:
                raise ValueError(
                    f"skill name cannot contain comma: {name!r} "
                    "(pass a list of separate names instead of a comma-joined string)"
                )
            if name.casefold() in KNOWN_TOOLSET_NAMES:
                toolset_typos.append(name)
                continue
            if name not in seen:
                seen.add(name)
                cleaned.append(name)
        if toolset_typos:
            quoted = ", ".join(repr(name) for name in toolset_typos)
            noun = "is a toolset name" if len(toolset_typos) == 1 else "are toolset names"
            raise ValueError(
                f"{quoted} {noun}, not skill name(s). "
                "Put toolsets in the assignee profile's `toolsets:` config "
                "instead of per-task skills. Skills are named skill bundles "
                "(e.g. `blogwatcher`, `github-code-review`); toolsets are runtime "
                "capabilities (e.g. `web`, `browser`, `terminal`)."
            )
        skills_list = cleaned

    toolsets_list: Optional[list[str]] = None
    if toolsets is not None:
        cleaned_toolsets: list[str] = []
        seen_toolsets: set[str] = set()
        unknown_toolsets: list[str] = []
        for raw_toolset in toolsets:
            name = str(raw_toolset or "").strip()
            if not name:
                continue
            normalized = name.casefold()
            if normalized not in KNOWN_TOOLSET_NAMES:
                unknown_toolsets.append(name)
            elif normalized not in seen_toolsets:
                seen_toolsets.add(normalized)
                cleaned_toolsets.append(normalized)
        if unknown_toolsets:
            raise ValueError(
                "unknown task toolset(s): " + ", ".join(sorted(unknown_toolsets))
            )
        if not cleaned_toolsets:
            raise ValueError("task toolsets must contain at least one toolset")
        toolsets_list = cleaned_toolsets

    skill_validation_error = _forced_skill_validation_error(assignee, skills_list)
    if skill_validation_error:
        raise ValueError(skill_validation_error)

    if (
        workspace_path is None
        and project_repo is None
        and workspace_kind in {"dir", "worktree"}
    ):
        board_slug = board if board else get_current_board()
        board_meta = read_board_metadata(board_slug)
        board_default = board_meta.get("default_workdir")
        if board_default:
            workspace_path = str(board_default)

    if workspace_kind == "worktree" and not workspace_path and project_repo is None:
        anchor_board = board if board else get_current_board()
        raise WorkspaceContractError(
            "worktree_no_anchor",
            "workspace_kind='worktree' needs a repository anchor: pass an "
            "explicit absolute workspace_path, a resolvable project, or set "
            f"board {anchor_board!r} default_workdir.",
        )

    worktree_anchor_path = workspace_path or project_repo
    if workspace_kind == "worktree" and worktree_anchor_path:
        worktree_anchor = Path(str(worktree_anchor_path)).expanduser()
        if not worktree_anchor.is_absolute():
            raise WorkspaceContractError(
                "worktree_bad_anchor",
                f"worktree anchor {worktree_anchor_path!r} is not absolute; use an "
                "absolute path to a git repo",
            )
        if _worktree_anchor_repo_root(worktree_anchor) is None:
            raise WorkspaceContractError(
                "worktree_bad_anchor",
                f"worktree anchor {worktree_anchor_path!r} is not inside a git repo; "
                "use an absolute path inside the repository that should contain "
                "the lazy worktree",
            )

    return _PreparedTaskCreate(
        title=title.strip(),
        body=body,
        assignee=assignee,
        created_by=created_by,
        workspace_kind=workspace_kind,
        workspace_path=workspace_path,
        branch_name=branch_name,
        tenant=tenant,
        priority=int(priority) if priority is not None else 0,
        parents=parents,
        triage=bool(triage),
        initial_status=initial_status,
        max_runtime_seconds=(
            int(max_runtime_seconds) if max_runtime_seconds is not None else None
        ),
        skills_list=skills_list,
        toolsets_list=toolsets_list,
        model_override=model_override,
        model_provider_override=model_provider_override,
        model_reasoning_effort=model_reasoning_effort,
        max_retries=int(max_retries) if max_retries is not None else None,
        goal_mode=bool(goal_mode),
        goal_max_turns=int(goal_max_turns) if goal_max_turns is not None else None,
        session_id=session_id,
        workflow_key=workflow_key or None,
        workflow_template_id=workflow_template_id or None,
        current_step_key=current_step_key or None,
        project_id=project_id,
        publication_expected_sha=publication_expected_sha,
        publication_remote=publication_remote,
        publication_ref=publication_ref,
        project_obj=project_obj,
        project_repo=project_repo,
    )


def _insert_task_in_txn(
    conn: sqlite3.Connection,
    prepared: _PreparedTaskCreate,
    *,
    idempotency_key: Optional[str] = None,
    mutation_context: Optional[MutationContext] = None,
    task_id: Optional[str] = None,
) -> str:
    """Insert one validated task while the caller owns ``write_txn``."""
    task_id = task_id or _new_task_id()
    parents = prepared.parents
    if mutation_context is not None:
        if mutation_context.phase == "architecture":
            existing_gate = _active_scope_gate(conn, mutation_context)
            if existing_gate is not None:
                return existing_gate.architect_task_id
        else:
            _authorize_mutation(
                conn,
                mutation_context,
                assignee=prepared.assignee,
            )
    elif parents:
        for parent_id in parents:
            parent_gate = get_architecture_gate_for_task(conn, parent_id)
            if _gate_requires_enforcement(parent_gate):
                raise ArchitectureGateError(ARCHITECTURE_GATE_REASON_OPEN)
            if (
                parent_gate is not None
                and parent_gate.enforcement_mode in ARCHITECTURE_GATE_ENFORCING_MODES
                and conn.execute(
                    "SELECT 1 FROM architecture_graph_issuances WHERE gate_id = ?",
                    (parent_gate.gate_id,),
                ).fetchone() is not None
            ):
                raise ArchitectureGateError("architecture_graph_issued")

    if prepared.initial_status == "blocked":
        task_status = "blocked"
        if parents:
            missing = _find_missing_parents(conn, parents)
            if missing:
                raise ValueError(f"unknown parent task(s): {', '.join(missing)}")
    elif prepared.triage:
        task_status = "triage"
    else:
        task_status = "ready"
        if parents:
            missing = _find_missing_parents(conn, parents)
            if missing:
                raise ValueError(f"unknown parent task(s): {', '.join(missing)}")
            rows = conn.execute(
                "SELECT status, policy_quarantined, policy_invalidated FROM tasks "
                "WHERE id IN (" + ",".join("?" * len(parents)) + ")",
                parents,
            ).fetchall()
            if any(not _parent_is_satisfied(row) for row in rows):
                task_status = "todo"
    if prepared.triage and parents:
        missing = _find_missing_parents(conn, parents)
        if missing:
            raise ValueError(f"unknown parent task(s): {', '.join(missing)}")

    workspace_path = prepared.workspace_path
    branch_name = prepared.branch_name
    if prepared.project_obj is not None and prepared.workspace_kind == "worktree":
        if prepared.project_repo and not workspace_path:
            workspace_path = os.path.join(prepared.project_repo, ".worktrees", task_id)
        if not branch_name:
            try:
                from hermes_cli import projects_db as _pdb

                branch_name = _pdb.branch_name_for(
                    prepared.project_obj, task_id, title=prepared.title,
                )
            except Exception:
                branch_name = None

    conn.execute(
        """
        INSERT INTO tasks (
            id, title, body, assignee, status, priority,
            created_by, created_at, workspace_kind, workspace_path,
            branch_name, project_id, tenant, idempotency_key,
            max_runtime_seconds,
            skills, toolsets, model_override, model_provider_override,
            model_reasoning_effort, max_retries, goal_mode, goal_max_turns,
            session_id, workflow_key, workflow_template_id, current_step_key,
            publication_expected_sha, publication_remote, publication_ref
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        """,
        (
            task_id,
            prepared.title,
            prepared.body,
            prepared.assignee,
            task_status,
            prepared.priority,
            prepared.created_by,
            int(time.time()),
            prepared.workspace_kind,
            workspace_path,
            branch_name,
            prepared.project_id,
            prepared.tenant,
            idempotency_key,
            prepared.max_runtime_seconds,
            json.dumps(prepared.skills_list) if prepared.skills_list is not None else None,
            json.dumps(prepared.toolsets_list) if prepared.toolsets_list is not None else None,
            prepared.model_override,
            prepared.model_provider_override,
            prepared.model_reasoning_effort,
            prepared.max_retries,
            1 if prepared.goal_mode else 0,
            prepared.goal_max_turns,
            prepared.session_id,
            prepared.workflow_key,
            prepared.workflow_template_id,
            prepared.current_step_key,
            prepared.publication_expected_sha,
            prepared.publication_remote,
            prepared.publication_ref,
        ),
    )
    for parent_id in parents:
        _link_tasks_in_txn(
            conn,
            parent_id,
            task_id,
            mutation_context=mutation_context,
            emit_event=False,
        )
    _append_event(
        conn,
        task_id,
        "created",
        {
            "assignee": prepared.assignee,
            "status": task_status,
            "parents": list(parents),
            "tenant": prepared.tenant,
            "branch_name": branch_name,
            "skills": list(prepared.skills_list) if prepared.skills_list else None,
            "toolsets": (
                list(prepared.toolsets_list)
                if prepared.toolsets_list is not None else None
            ),
            "model_override": prepared.model_override,
            "model_provider_override": prepared.model_provider_override,
            "model_reasoning_effort": prepared.model_reasoning_effort,
            "goal_mode": prepared.goal_mode or None,
            "goal_max_turns": prepared.goal_max_turns,
        },
    )
    if mutation_context is not None and mutation_context.phase == "architecture":
        _open_architecture_gate(conn, task_id, mutation_context)
    return task_id


def create_task(
    conn: sqlite3.Connection,
    *,
    title: str,
    body: Optional[str] = None,
    assignee: Optional[str] = None,
    created_by: Optional[str] = None,
    workspace_kind: str = "scratch",
    workspace_path: Optional[str] = None,
    branch_name: Optional[str] = None,
    tenant: Optional[str] = None,
    priority: int = 0,
    parents: Iterable[str] = (),
    triage: bool = False,
    idempotency_key: Optional[str] = None,
    max_runtime_seconds: Optional[int] = None,
    skills: Optional[Iterable[str]] = None,
    toolsets: Optional[Iterable[str]] = None,
    model_override: Optional[str] = None,
    model_provider_override: Optional[str] = None,
    model_reasoning_effort: Optional[str] = None,
    max_retries: Optional[int] = None,
    goal_mode: bool = False,
    goal_max_turns: Optional[int] = None,
    initial_status: str = "running",
    session_id: Optional[str] = None,
    workflow_key: Optional[str] = None,
    workflow_template_id: Optional[str] = None,
    current_step_key: Optional[str] = None,
    publication_expected_sha: Optional[str] = None,
    publication_remote: Optional[str] = None,
    publication_ref: Optional[str] = None,
    board: Optional[str] = None,
    project_id: Optional[str] = None,
    mutation_context: Optional[MutationContext] = None,
) -> str:
    """Create a task through the normal validation and one write transaction."""
    if idempotency_key:
        row = conn.execute(
            "SELECT id FROM tasks WHERE idempotency_key = ? AND status != 'archived' "
            "ORDER BY created_at DESC LIMIT 1",
            (idempotency_key,),
        ).fetchone()
        if row:
            return row["id"]
    prepared = _prepare_task_create(
        title=title,
        body=body,
        assignee=assignee,
        created_by=created_by,
        workspace_kind=workspace_kind,
        workspace_path=workspace_path,
        branch_name=branch_name,
        tenant=tenant,
        priority=priority,
        parents=parents,
        triage=triage,
        max_runtime_seconds=max_runtime_seconds,
        skills=skills,
        toolsets=toolsets,
        model_override=model_override,
        model_provider_override=model_provider_override,
        model_reasoning_effort=model_reasoning_effort,
        max_retries=max_retries,
        goal_mode=goal_mode,
        goal_max_turns=goal_max_turns,
        initial_status=initial_status,
        session_id=session_id,
        workflow_key=workflow_key,
        workflow_template_id=workflow_template_id,
        current_step_key=current_step_key,
        publication_expected_sha=publication_expected_sha,
        publication_remote=publication_remote,
        publication_ref=publication_ref,
        board=board,
        project_id=project_id,
    )
    for attempt in range(2):
        try:
            with write_txn(conn):
                return _insert_task_in_txn(
                    conn,
                    prepared,
                    idempotency_key=idempotency_key,
                    mutation_context=mutation_context,
                )
        except sqlite3.IntegrityError:
            if attempt == 1:
                raise
    raise RuntimeError("unreachable")


def _task_has_active_run_identity(row: sqlite3.Row, run: Optional[sqlite3.Row]) -> bool:
    """Return True when a task or its pointed run still owns a live attempt."""
    task_keys = (
        "current_run_id", "claim_lock", "claim_expires", "worker_pid",
        "worker_started_at", "worker_pgid", "worker_sid",
    )
    if any(row[key] is not None for key in task_keys if key in row.keys()):
        return True
    if run is not None:
        run_keys = (
            "claim_lock", "claim_expires", "worker_pid", "worker_started_at",
            "worker_pgid", "worker_sid",
        )
        if any(run[key] is not None for key in run_keys if key in run.keys()):
            return True
        if run["ended_at"] is None:
            return True
    return False


def _rework_event_payload(
    *,
    review_task_id: str,
    fix_task_id: Optional[str],
    request_key: str,
    actor: str,
    finding: str,
    disposition: Literal["created", "adopted", "escalated"],
    summary: Optional[str],
    metadata: Optional[dict],
    human_gate_task_id: Optional[str] = None,
    artifact_binding: Optional[ReviewArtifactBinding] = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "review_task_id": review_task_id,
        "fix_task_id": fix_task_id,
        "request_key": request_key,
        "actor": actor,
        "finding": finding,
        "fix_disposition": disposition,
        "fix_action": disposition,
        "disposition": disposition,
        "summary": summary,
        "metadata": metadata,
        "human_gate_task_id": human_gate_task_id,
    }
    if artifact_binding is not None:
        payload["artifact_binding"] = {
            "generation": artifact_binding.generation,
            "attachment_id": artifact_binding.attachment_id,
            "sha256": artifact_binding.sha256,
            "source_task_id": artifact_binding.source_task_id,
            "source_run_id": artifact_binding.source_run_id,
            "source_rework_event_id": artifact_binding.source_rework_event_id,
        }
    return payload


def _normalize_rework_metadata(metadata: Optional[dict]) -> Optional[dict]:
    """Validate and canonicalize the optional reviewer blocker snapshot."""
    if metadata is None:
        return None
    if not isinstance(metadata, dict):
        raise ValueError("metadata must be a dict or None")

    rework = metadata.get("rework")
    if rework is None:
        return dict(metadata)
    if not isinstance(rework, dict):
        raise ValueError("metadata.rework must be an object")
    if "open_blockers" not in rework:
        raise ValueError(
            "metadata.rework.open_blockers must be a complete blocker snapshot"
        )
    blockers = rework["open_blockers"]
    if not isinstance(blockers, list):
        raise ValueError("metadata.rework.open_blockers must be an array")

    normalized_blockers: list[dict[str, str]] = []
    seen_keys: set[str] = set()
    for index, blocker in enumerate(blockers):
        if not isinstance(blocker, dict):
            raise ValueError(
                f"metadata.rework.open_blockers[{index}] must be an object"
            )
        key = blocker.get("key")
        summary = blocker.get("summary")
        if not isinstance(key, str) or not key.strip():
            raise ValueError(
                f"metadata.rework.open_blockers[{index}].key is required"
            )
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError(
                f"metadata.rework.open_blockers[{index}].summary is required"
            )
        key = key.strip()
        if key in seen_keys:
            raise ValueError(
                f"metadata.rework.open_blockers contains duplicate key {key!r}"
            )
        seen_keys.add(key)
        normalized_blockers.append(
            {"key": key, "summary": summary.strip()}
        )

    normalized_rework = dict(rework)
    normalized_rework["open_blockers"] = sorted(
        normalized_blockers, key=lambda item: item["key"]
    )
    normalized = dict(metadata)
    normalized["rework"] = normalized_rework
    return normalized


def _rework_blocker_snapshot(metadata: Any) -> Optional[list[dict[str, str]]]:
    """Read a valid blocker snapshot, treating legacy/missing data as unknown."""
    if not isinstance(metadata, dict):
        return None
    rework = metadata.get("rework")
    if not isinstance(rework, dict):
        return None
    blockers = rework.get("open_blockers")
    if not isinstance(blockers, list):
        return None
    snapshot: list[dict[str, str]] = []
    seen_keys: set[str] = set()
    for blocker in blockers:
        if not isinstance(blocker, dict):
            return None
        key = blocker.get("key")
        summary = blocker.get("summary")
        if not isinstance(key, str) or not key.strip():
            return None
        if not isinstance(summary, str) or not summary.strip():
            return None
        key = key.strip()
        if key in seen_keys:
            return None
        seen_keys.add(key)
        snapshot.append({"key": key, "summary": summary.strip()})
    return sorted(snapshot, key=lambda item: item["key"])


def _rework_history_rows(
    conn: sqlite3.Connection,
    review_task_id: str,
) -> list[dict[str, Any]]:
    """Return the committed, unique rework requests for one review series."""
    rows = conn.execute(
        "SELECT id, payload FROM task_events "
        "WHERE task_id = ? AND kind = 'rework_requested' ORDER BY id ASC",
        (review_task_id,),
    ).fetchall()
    history: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        request_key = str(payload.get("request_key") or "").strip()
        if not request_key or request_key in seen_keys:
            continue
        seen_keys.add(request_key)
        history.append({"id": int(row["id"]), "payload": payload})
    return history


def _rework_progress_state(
    history: list[dict[str, Any]],
    current_metadata: Optional[dict],
) -> tuple[int, int]:
    """Return ``(unique_round_count_after_current, nonprogress_streak)``."""
    previous_snapshot: Optional[list[dict[str, str]]] = None
    nonprogress_streak = 0
    for item in history:
        snapshot = _rework_blocker_snapshot(
            item.get("payload", {}).get("metadata")
        )
        if snapshot is None:
            previous_snapshot = None
            nonprogress_streak = 0
        elif previous_snapshot is None:
            previous_snapshot = snapshot
            nonprogress_streak = 0
        elif {b["key"] for b in snapshot} < {
            b["key"] for b in previous_snapshot
        }:
            previous_snapshot = snapshot
            nonprogress_streak = 0
        else:
            previous_snapshot = snapshot
            nonprogress_streak += 1

    current_snapshot = _rework_blocker_snapshot(current_metadata)
    if current_snapshot is None:
        nonprogress_streak = 0
    elif previous_snapshot is None:
        nonprogress_streak = 0
    elif {b["key"] for b in current_snapshot} < {
        b["key"] for b in previous_snapshot
    }:
        nonprogress_streak = 0
    else:
        nonprogress_streak += 1
    return len(history) + 1, nonprogress_streak


def _rework_blocker_digest(
    history: list[dict[str, Any]],
    current_payload: dict[str, Any],
    *,
    round_count: int,
) -> str:
    """Build a bounded, deterministic digest from every rework request."""
    entries: list[tuple[str, str]] = []
    positions: dict[str, int] = {}
    for item in (*history, {"payload": current_payload}):
        payload = item.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        finding = str(payload.get("finding") or "").strip()
        if finding:
            entry_key = f"finding:{finding}"
            if entry_key not in positions:
                positions[entry_key] = len(entries)
                entries.append(("finding", finding))
        blockers = _rework_blocker_snapshot(payload.get("metadata"))
        if blockers is None:
            continue
        for blocker in blockers:
            key = blocker["key"]
            entry_key = f"blocker:{key}"
            value = f"{key}: {blocker['summary']}"
            if entry_key in positions:
                entries[positions[entry_key]] = ("blocker", value)
            else:
                positions[entry_key] = len(entries)
                entries.append(("blocker", value))

    lines = [
        f"Autonomous rework loop escalated after {round_count} unique rounds.",
        "Accumulated findings and blockers:",
    ]
    if not entries:
        lines.append("- No structured blocker snapshot was supplied.")
    else:
        lines.extend(f"- {value}" for _kind, value in entries)
    digest = "\n".join(lines)
    if len(digest) <= REWORK_BLOCKER_DIGEST_MAX_CHARS:
        return digest
    truncated = digest[: REWORK_BLOCKER_DIGEST_MAX_CHARS - 1].rstrip()
    return truncated + "…"


def _validate_rework_human_gate(
    conn: sqlite3.Connection,
    review_task_id: str,
    human_gate_task_id: Optional[str],
) -> tuple[bool, str]:
    """Fail closed unless the named gate is the sole releasable child."""
    gate_id = str(human_gate_task_id or "").strip()
    if not gate_id:
        return False, "no human gate was declared"
    gate = conn.execute(
        "SELECT * FROM tasks WHERE id = ?", (gate_id,)
    ).fetchone()
    if gate is None:
        return False, f"unknown human gate task: {gate_id}"
    if gate["status"] in {"done", "archived"}:
        return False, f"human gate {gate_id} is terminal"
    if gate["policy_quarantined"] or gate["policy_invalidated"]:
        return False, f"human gate {gate_id} is quarantined or invalidated"
    if not conn.execute(
        "SELECT 1 FROM task_links WHERE parent_id = ? AND child_id = ?",
        (review_task_id, gate_id),
    ).fetchone():
        return False, f"human gate {gate_id} is not a direct child of the review"
    if gate["status"] != "todo" or gate["current_run_id"] is not None:
        return False, f"human gate {gate_id} is not dependency-gated and idle"

    if gate["block_kind"] not in (None, "dependency"):
        return False, f"human gate {gate_id} is not a plain dependency wait"
    if any(
        gate[key] is not None
        for key in (
            "claim_lock", "claim_expires", "worker_pid", "worker_started_at",
            "worker_pgid", "worker_sid",
        )
    ):
        return False, f"human gate {gate_id} still carries worker ownership"

    other_children = conn.execute(
        "SELECT t.id, t.status FROM tasks t "
        "JOIN task_links l ON l.child_id = t.id "
        "WHERE l.parent_id = ? AND t.id != ?",
        (review_task_id, gate_id),
    ).fetchall()
    unsafe = [
        row["id"] for row in other_children
        if row["status"] not in {"done", "archived"}
    ]
    if unsafe:
        return False, (
            "review has another nonterminal direct child: "
            + ", ".join(sorted(unsafe))
        )
    return True, ""


def _promote_rework_gate_in_txn(
    conn: sqlite3.Connection,
    gate_task_id: str,
) -> bool:
    """Promote a validated gate when all of its parents are satisfied."""
    gate = conn.execute(
        "SELECT * FROM tasks WHERE id = ?", (gate_task_id,)
    ).fetchone()
    if gate is None or gate["status"] != "todo":
        return False
    parents = conn.execute(
        "SELECT p.status, p.policy_quarantined, p.policy_invalidated "
        "FROM tasks p JOIN task_links l ON l.parent_id = p.id "
        "WHERE l.child_id = ?",
        (gate_task_id,),
    ).fetchall()
    if not all(_parent_is_satisfied(parent) for parent in parents):
        return False
    cur = conn.execute(
        "UPDATE tasks SET status = 'ready', block_kind = NULL, "
        "claim_lock = NULL, claim_expires = NULL, worker_pid = NULL, "
        "worker_started_at = NULL, worker_pgid = NULL, worker_sid = NULL "
        "WHERE id = ? AND status = 'todo'",
        (gate_task_id,),
    )
    if cur.rowcount == 1:
        _append_event(conn, gate_task_id, "promoted", None)
        return True
    return False


@dataclass(frozen=True)
class _DependencyTransitionResult:
    """Shared result for a card-parent handoff transition."""

    requester_task_id: str
    dependency_task_id: Optional[str]
    dependency_action: Literal["created", "adopted", "replayed", "escalated"]
    requester_status: str
    request_event_id: int
    escalated: bool = False
    escalation_target_task_id: Optional[str] = None
    escalation_reason: Optional[str] = None
    replayed_same_run: bool = False


def _transition_to_dependency(
    conn: sqlite3.Connection,
    requester_task_id: str,
    *,
    request_key: str,
    request_event_kind: str,
    dependency_event_kind: str,
    dependency_id_payload_key: str,
    materialize_dependency: Callable[
        [sqlite3.Connection], tuple[str, Literal["created", "adopted"]]
    ],
    event_payload: Callable[
        [sqlite3.Connection, str, Literal["created", "adopted"]], dict[str, Any]
    ],
    terminal_error: str,
    unknown_error: str,
    quarantine_error: str,
    run_outcome: str,
    run_status: str,
    run_summary: Optional[str] = None,
    run_metadata: Optional[dict] = None,
    active_run_error: str = (
        "requester task has an active worker; fence and terminate it before transition"
    ),
    expected_run_id: Optional[int] = None,
    require_no_active_run: bool = False,
    pre_materialization: Optional[
        Callable[
            [sqlite3.Connection, sqlite3.Row, Optional[sqlite3.Row]],
            Optional[_DependencyTransitionResult],
        ]
    ] = None,
    post_request: Optional[
        Callable[
            [sqlite3.Connection, str, Literal["created", "adopted"], int, Optional[int]],
            None,
        ]
    ] = None,
    mutation_context: Optional[MutationContext] = None,
) -> _DependencyTransitionResult:
    """Atomically bind a dependency card as parent and park its requester.

    Rework and publication handoff have the same graph mutation: validate the
    requester/run fence, create or adopt a parent, link parent→requester,
    close the requester's run, re-arm it behind the terminal-parent predicate,
    and append one mirrored event pair. Keeping this composition point below
    both public transitions prevents either caller from nesting ``write_txn``
    through a public writer.
    """
    if expected_run_id is not None and require_no_active_run:
        raise ValueError("expected_run_id and require_no_active_run are exclusive")

    with write_txn(conn):
        requester_row = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (requester_task_id,)
        ).fetchone()
        if requester_row is None:
            raise ValueError(unknown_error)
        prior = conn.execute(
            f"""SELECT id, run_id, payload FROM task_events
                 WHERE task_id = ? AND kind = ?
                   AND json_extract(payload, '$.request_key') = ?
                 ORDER BY id DESC LIMIT 1""",
            (requester_task_id, request_event_kind, request_key),
        ).fetchone()
        if prior is not None:
            try:
                prior_payload = json.loads(prior["payload"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                prior_payload = {}
            dependency_id = str(
                prior_payload.get(dependency_id_payload_key) or ""
            ).strip()
            if not dependency_id and (
                prior_payload.get("fix_action") == "escalated"
                or prior_payload.get("disposition") == "escalated"
            ):
                current = get_task(conn, requester_task_id)
                current_status = current.status if current else "triage"
                return _DependencyTransitionResult(
                    requester_task_id=requester_task_id,
                    dependency_task_id=None,
                    dependency_action="escalated",
                    requester_status=current_status,
                    request_event_id=int(prior["id"]),
                    escalated=True,
                    escalation_target_task_id=(
                        str(prior_payload.get("human_gate_task_id") or "").strip()
                        or None
                    ),
                    escalation_reason=(
                        str(prior_payload.get("escalation_reason") or "").strip()
                        or None
                    ),
                    replayed_same_run=(
                        expected_run_id is not None
                        and prior["run_id"] is not None
                        and int(prior["run_id"]) == int(expected_run_id)
                    ),
                )
            if not dependency_id:
                raise ValueError(
                    f"{request_event_kind} event has no {dependency_id_payload_key}"
                )
            current = get_task(conn, requester_task_id)
            current_status = current.status if current else "todo"
            return _DependencyTransitionResult(
                requester_task_id=requester_task_id,
                dependency_task_id=dependency_id,
                dependency_action="replayed",
                requester_status=current_status,
                request_event_id=int(prior["id"]),
                replayed_same_run=(
                    expected_run_id is not None
                    and prior["run_id"] is not None
                    and int(prior["run_id"]) == int(expected_run_id)
                ),
            )

        if requester_row["status"] in {"done", "archived"}:
            raise ValueError(terminal_error)
        if requester_row["policy_quarantined"] or requester_row["policy_invalidated"]:
            raise ValueError(quarantine_error)

        active_run = None
        if requester_row["current_run_id"] is not None:
            active_run = conn.execute(
                "SELECT * FROM task_runs WHERE id = ?",
                (int(requester_row["current_run_id"]),),
            ).fetchone()

        if expected_run_id is not None:
            if requester_row["current_run_id"] != int(expected_run_id):
                raise ValueError("stale expected_run_id")
            if active_run is None or active_run["ended_at"] is not None:
                raise ValueError("expected_run_id is not an active run")
        if expected_run_id is None and _task_has_active_run_identity(
            requester_row, active_run,
        ):
            raise ValueError(active_run_error)
        elif require_no_active_run and requester_row["current_run_id"] is not None:
            raise ValueError("requester task still has an active run")

        if pre_materialization is not None:
            escalated = pre_materialization(conn, requester_row, active_run)
            if escalated is not None:
                return escalated

        dependency_task_id, disposition = materialize_dependency(conn)
        _link_tasks_in_txn(
            conn,
            dependency_task_id,
            requester_task_id,
            mutation_context=mutation_context,
        )

        run_id = _end_run(
            conn,
            requester_task_id,
            outcome=run_outcome,
            status=run_status,
            summary=run_summary,
            metadata=run_metadata,
        )
        requester_parents = conn.execute(
            """SELECT p.status, p.policy_quarantined, p.policy_invalidated
                 FROM tasks p JOIN task_links l ON l.parent_id = p.id
                WHERE l.child_id = ?""",
            (requester_task_id,),
        ).fetchall()
        requester_status = "todo" if any(
            not _parent_is_satisfied(parent) for parent in requester_parents
        ) else "ready"
        conn.execute(
            """UPDATE tasks
                  SET status = ?,
                      block_kind = NULL,
                      current_run_id = NULL,
                      claim_lock = NULL,
                      claim_expires = NULL,
                      worker_pid = NULL,
                      worker_started_at = NULL,
                      worker_pgid = NULL,
                      worker_sid = NULL
                WHERE id = ?
                  AND status NOT IN ('done', 'archived')""",
            (requester_status, requester_task_id),
        )
        requester = get_task(conn, requester_task_id)
        if requester is None or requester.status not in {"todo", "ready"}:
            raise ValueError("requester task could not be re-armed")
        requester_status = requester.status
        payload = event_payload(conn, dependency_task_id, disposition)
        if not payload.get("request_key"):
            raise ValueError("dependency transition event payload needs request_key")
        request_event_id = _append_event(
            conn,
            requester_task_id,
            request_event_kind,
            payload,
            run_id=run_id,
        )
        _append_event(conn, dependency_task_id, dependency_event_kind, payload)
        if post_request is not None:
            post_request(
                conn,
                dependency_task_id,
                disposition,
                request_event_id,
                run_id,
            )
        return _DependencyTransitionResult(
            requester_task_id=requester_task_id,
            dependency_task_id=dependency_task_id,
            dependency_action=disposition,
            requester_status=requester_status,
            request_event_id=request_event_id,
        )


def request_rework(
    conn: sqlite3.Connection,
    review_task_id: str,
    *,
    finding: str,
    fix: NewFixTask | ExistingFixTask,
    request_key: str,
    actor: str,
    summary: Optional[str] = None,
    metadata: Optional[dict] = None,
    human_gate_task_id: Optional[str] = None,
    expected_run_id: Optional[int] = None,
    require_no_active_run: bool = False,
    mutation_context: Optional[MutationContext] = None,
) -> ReworkResult:
    """Atomically materialize/adopt a fix and re-arm its review card.

    This function is intentionally the only composition point for the
    review→fix loop.  The idempotency lookup, task insert, graph edge, run
    closure, projection update, and audit events all happen under one
    ``write_txn`` so a retry can observe either the old graph or the complete
    transition, never a half-created remediation card.
    """
    review_task_id = str(review_task_id or "").strip()
    finding = str(finding or "").strip()
    request_key = str(request_key or "").strip()
    actor = str(actor or "").strip()
    if not review_task_id:
        raise ValueError("review_task_id is required")
    if not finding:
        raise ValueError("finding is required")
    if not request_key:
        raise ValueError("request_key is required")
    if not actor:
        raise ValueError("actor is required")
    metadata = _normalize_rework_metadata(metadata)
    if human_gate_task_id is not None and not isinstance(human_gate_task_id, str):
        raise ValueError("human_gate_task_id must be a string or None")
    human_gate_task_id = str(human_gate_task_id or "").strip() or None
    if not isinstance(fix, (NewFixTask, ExistingFixTask)):
        raise TypeError("fix must be NewFixTask or ExistingFixTask")

    def materialize(conn_in: sqlite3.Connection):
        if isinstance(fix, ExistingFixTask):
            fix_task_id = str(fix.task_id or "").strip()
            if not fix_task_id:
                raise ValueError("fix_task_id is required")
            fix_row = conn_in.execute(
                "SELECT * FROM tasks WHERE id = ?", (fix_task_id,),
            ).fetchone()
            if fix_row is None:
                raise ValueError(f"unknown fix task: {fix_task_id}")
            if fix_task_id == review_task_id:
                raise ValueError("a review task cannot be its own fix")
            if fix_row["policy_quarantined"] or fix_row["policy_invalidated"]:
                raise ValueError("cannot adopt a quarantined or invalidated fix task")
            return fix_task_id, "adopted"

        if not fix.title or not str(fix.title).strip():
            raise ValueError("fix.title is required")
        if not fix.assignee or not str(fix.assignee).strip():
            raise ValueError("fix.assignee is required")
        prepared = _prepare_task_create(
            title=fix.title,
            body=fix.body,
            assignee=fix.assignee,
            created_by=actor,
            workspace_kind=fix.workspace_kind,
            workspace_path=fix.workspace_path,
            project_id=fix.project_id,
            branch_name=fix.branch_name,
            priority=fix.priority,
            max_runtime_seconds=fix.max_runtime_seconds,
            skills=fix.skills,
            toolsets=fix.toolsets,
        )
        fix_task_id = _insert_task_in_txn(
            conn_in,
            prepared,
            mutation_context=mutation_context,
        )
        return fix_task_id, "created"

    def payload_factory(
        _conn_in: sqlite3.Connection,
        fix_task_id: str,
        disposition: Literal["created", "adopted"],
    ) -> dict[str, Any]:
        artifact_binding = _ensure_review_artifact_binding_in_txn(
            _conn_in,
            review_task_id,
            now=int(time.time()),
            require_if_referenced=True,
        )
        return _rework_event_payload(
            review_task_id=review_task_id,
            fix_task_id=fix_task_id,
            request_key=request_key,
            actor=actor,
            finding=finding,
            disposition=disposition,
            summary=summary,
            metadata=metadata,
            human_gate_task_id=human_gate_task_id,
            artifact_binding=artifact_binding,
        )

    def pre_materialization(
        conn_in: sqlite3.Connection,
        requester_row: sqlite3.Row,
        active_run: Optional[sqlite3.Row],
    ) -> Optional[_DependencyTransitionResult]:
        if requester_row["status"] == "triage":
            raise ValueError("review task is awaiting human triage")
        artifact_binding = _ensure_review_artifact_binding_in_txn(
            conn_in,
            review_task_id,
            now=int(time.time()),
            require_if_referenced=True,
        )
        if artifact_binding is not None:
            _verify_review_artifact_binding(conn_in, artifact_binding)
        history = _rework_history_rows(conn_in, review_task_id)
        round_count, nonprogress_streak = _rework_progress_state(
            history, metadata,
        )
        if (
            round_count < REWORK_ABSOLUTE_LIMIT
            and nonprogress_streak < REWORK_NONPROGRESS_LIMIT
        ):
            return None

        escalation_reason = (
            "absolute_limit"
            if round_count >= REWORK_ABSOLUTE_LIMIT
            else "nonprogress_limit"
        )
        current_payload = _rework_event_payload(
            review_task_id=review_task_id,
            fix_task_id=None,
            request_key=request_key,
            actor=actor,
            finding=finding,
            disposition="escalated",
            summary=summary,
            metadata=metadata,
            human_gate_task_id=human_gate_task_id,
            artifact_binding=artifact_binding,
        )
        digest = _rework_blocker_digest(
            history,
            current_payload,
            round_count=round_count,
        )
        gate_valid, gate_reason = _validate_rework_human_gate(
            conn_in, review_task_id, human_gate_task_id,
        )
        target_gate_id = human_gate_task_id if gate_valid else None
        escalation_metadata = dict(metadata or {})
        escalation_metadata["rework_escalation"] = {
            "round_count": round_count,
            "nonprogress_streak": nonprogress_streak,
            "reason": escalation_reason,
            "human_gate_task_id": target_gate_id,
            "blocker_digest": digest,
            "gate_validation": gate_reason or "validated",
        }
        run_id = _end_run(
            conn_in,
            review_task_id,
            outcome="rework_escalated",
            status="rework_escalated",
            summary=digest,
            metadata=escalation_metadata,
        )
        if run_id is None:
            run_id = _synthesize_ended_run(
                conn_in,
                review_task_id,
                outcome="rework_escalated",
                summary=digest,
                metadata=escalation_metadata,
            )

        now = int(time.time())
        review_status = "done" if gate_valid else "triage"
        review_result = (
            REWORK_ESCALATION_RESULT
            if gate_valid
            else f"Rework loop requires human triage: {digest}"
        )
        if gate_valid:
            cur = conn_in.execute(
                "UPDATE tasks SET status = 'done', result = ?, "
                "completed_at = ?, block_kind = NULL, block_recurrences = 0, "
                "current_run_id = NULL, claim_lock = NULL, claim_expires = NULL, "
                "worker_pid = NULL, worker_started_at = NULL, "
                "worker_pgid = NULL, worker_sid = NULL "
                "WHERE id = ? AND status NOT IN ('done', 'archived')",
                (review_result, now, review_task_id),
            )
        else:
            cur = conn_in.execute(
                "UPDATE tasks SET status = 'triage', result = ?, "
                "block_kind = 'needs_input', current_run_id = NULL, "
                "claim_lock = NULL, claim_expires = NULL, worker_pid = NULL, "
                "worker_started_at = NULL, worker_pgid = NULL, worker_sid = NULL "
                "WHERE id = ? AND status NOT IN ('done', 'archived')",
                (review_result, review_task_id),
            )
        if cur.rowcount != 1:
            raise ValueError("review task could not be escalated")

        current_payload.update(
            {
                "escalated": True,
                "round_count": round_count,
                "nonprogress_streak": nonprogress_streak,
                "escalation_reason": escalation_reason,
                "human_gate_task_id": target_gate_id,
                "blocker_digest": digest,
                "gate_validation": gate_reason or "validated",
            }
        )
        request_event_id = _append_event(
            conn_in,
            review_task_id,
            "rework_requested",
            current_payload,
            run_id=run_id,
        )

        if gate_valid:
            _append_event(
                conn_in,
                review_task_id,
                "completed",
                {
                    "result_len": len(review_result),
                    "summary": REWORK_ESCALATION_RESULT,
                    "outcome": "rework_escalated",
                    "human_gate_task_id": target_gate_id,
                },
                run_id=run_id,
            )
            comment = (
                f"{digest}\n\n"
                f"Human gate: {target_gate_id}\n"
                f"Autonomous review result: {REWORK_ESCALATION_RESULT}"
            )
            _add_comment_in_txn(
                conn_in,
                target_gate_id,
                author="kernel",
                body=comment,
                created_at=now,
            )
            _append_event(
                conn_in,
                target_gate_id,
                REWORK_ESCALATION_EVENT_KIND,
                {
                    "review_task_id": review_task_id,
                    "human_gate_task_id": target_gate_id,
                    "round_count": round_count,
                    "nonprogress_streak": nonprogress_streak,
                    "escalation_reason": escalation_reason,
                    "blocker_digest": digest,
                    "review_result": REWORK_ESCALATION_RESULT,
                },
                run_id=run_id,
            )
            _promote_rework_gate_in_txn(conn_in, target_gate_id)
        else:
            _add_comment_in_txn(
                conn_in,
                review_task_id,
                author="kernel",
                body=(
                    f"{digest}\n\n"
                    f"Automation stopped: {gate_reason}."
                ),
                created_at=now,
            )
            _append_event(
                conn_in,
                review_task_id,
                REWORK_ESCALATION_EVENT_KIND,
                {
                    "review_task_id": review_task_id,
                    "human_gate_task_id": None,
                    "round_count": round_count,
                    "nonprogress_streak": nonprogress_streak,
                    "escalation_reason": escalation_reason,
                    "blocker_digest": digest,
                    "gate_validation": gate_reason,
                    "review_result": review_result,
                },
                run_id=run_id,
            )
        return _DependencyTransitionResult(
            requester_task_id=review_task_id,
            dependency_task_id=None,
            dependency_action="escalated",
            requester_status=review_status,
            request_event_id=request_event_id,
            escalated=True,
            escalation_target_task_id=target_gate_id,
            escalation_reason=escalation_reason,
        )

    def post_request(
        conn_in: sqlite3.Connection,
        fix_task_id: str,
        disposition: Literal["created", "adopted"],
        request_event_id: int,
        _review_run_id: Optional[int],
    ) -> None:
        # An already-completed adopted fix has no future completion callback,
        # so it may bind only when its attachment output is unambiguous. A
        # still-open adopted fix follows the normal fix-completion path.
        if disposition != "adopted":
            return
        fix_row = conn_in.execute(
            "SELECT status FROM tasks WHERE id = ?", (fix_task_id,)
        ).fetchone()
        if fix_row is None or fix_row["status"] not in {"done", "archived"}:
            return
        binding = _ensure_review_artifact_binding_in_txn(
            conn_in,
            review_task_id,
            now=int(time.time()),
            require_if_referenced=False,
        )
        if binding is None:
            return
        candidates = list_attachments(conn_in, fix_task_id)
        if len(candidates) != 1:
            raise ReviewArtifactError(
                f"adopted completed fix {fix_task_id} has {len(candidates)} "
                "attachment candidates; explicit artifact selection is required"
            )
        completion_run = conn_in.execute(
            "SELECT id FROM task_runs WHERE task_id = ? AND outcome = 'completed' "
            "ORDER BY id DESC LIMIT 1",
            (fix_task_id,),
        ).fetchone()
        bind_review_artifact_in_txn(
            conn_in,
            review_task_id,
            candidates[0].id,
            fix_task_id,
            int(completion_run["id"]) if completion_run is not None else None,
            request_event_id,
            binding.generation,
            int(time.time()),
        )

    transition = _transition_to_dependency(
        conn,
        review_task_id,
        request_key=request_key,
        request_event_kind="rework_requested",
        dependency_event_kind="rework_for",
        dependency_id_payload_key="fix_task_id",
        materialize_dependency=materialize,
        event_payload=payload_factory,
        terminal_error="cannot request rework for a terminal review task",
        unknown_error=f"unknown review task: {review_task_id}",
        quarantine_error=(
            "cannot request rework for a quarantined or invalidated review task"
        ),
        run_outcome="rework_requested",
        run_status="rework_requested",
        run_summary=summary or finding,
        run_metadata=metadata,
        active_run_error=(
            "review task has an active worker; fence and terminate it before rework"
        ),
        expected_run_id=expected_run_id,
        require_no_active_run=require_no_active_run,
        pre_materialization=pre_materialization,
        post_request=post_request,
        mutation_context=mutation_context,
    )
    return ReworkResult(
        review_task_id=transition.requester_task_id,
        fix_task_id=transition.dependency_task_id,
        fix_action=transition.dependency_action,
        review_status=transition.requester_status,
        request_event_id=transition.request_event_id,
        escalated=transition.escalated,
        escalation_target_task_id=transition.escalation_target_task_id,
        escalation_reason=transition.escalation_reason,
        replayed_same_run=transition.replayed_same_run,
    )


def request_publication_handoff(
    conn: sqlite3.Connection,
    requester_task_id: str,
    *,
    publication: Optional[NewPublicationTask | ExistingPublicationTask] = None,
    publication_task_id: Optional[str] = None,
    expected_sha: Optional[str] = None,
    workspace_path: Optional[str] = None,
    remote: str = "origin",
    remote_ref: Optional[str] = None,
    request_key: str,
    actor: str,
    summary: Optional[str] = None,
    metadata: Optional[dict] = None,
    expected_run_id: Optional[int] = None,
    require_no_active_run: bool = False,
    mutation_context: Optional[MutationContext] = None,
) -> PublicationHandoffResult:
    """Atomically hand a committed-but-unpublished task to the releaser.

    The publication card is deliberately a normal dependency parent. Its
    three publication fields are immutable creation-time evidence consumed by
    :func:`complete_task`; worker-provided completion metadata cannot satisfy
    the publication gate.
    """
    requester_task_id = str(requester_task_id or "").strip()
    request_key = str(request_key or "").strip()
    actor = str(actor or "").strip()
    if not requester_task_id:
        raise ValueError("requester_task_id is required")
    if not request_key:
        raise ValueError("request_key is required")
    if not actor:
        raise ValueError("actor is required")
    if metadata is not None and not isinstance(metadata, dict):
        raise ValueError("metadata must be a dict or None")
    if (publication is not None or publication_task_id is not None) and (
        publication_task_id is not None
        or expected_sha is not None
        or workspace_path is not None
        or remote != "origin"
        or remote_ref is not None
    ):
        raise ValueError(
            "publication cannot be combined with publication card fields"
        )
    if publication is None:
        if publication_task_id is not None:
            publication = ExistingPublicationTask(task_id=publication_task_id)
        else:
            publication = NewPublicationTask(
                expected_sha=str(expected_sha or ""),
                workspace_path=str(workspace_path or ""),
                remote_ref=str(remote_ref or ""),
                remote=remote or "origin",
            )
    if not isinstance(publication, (NewPublicationTask, ExistingPublicationTask)):
        raise TypeError(
            "publication must be NewPublicationTask or ExistingPublicationTask"
        )

    def materialize(conn_in: sqlite3.Connection):
        if isinstance(publication, ExistingPublicationTask):
            publication_id = str(publication.task_id or "").strip()
            if not publication_id:
                raise ValueError("publication_task_id is required")
            row = conn_in.execute(
                "SELECT * FROM tasks WHERE id = ?", (publication_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown publication task: {publication_id}")
            if publication_id == requester_task_id:
                raise ValueError("a requester task cannot be its own publication parent")
            if any(
                row[name] is None
                for name in (
                    "publication_expected_sha",
                    "publication_remote",
                    "publication_ref",
                )
            ):
                raise ValueError("cannot adopt a task without a publication contract")
            if _canonical_assignee(row["assignee"]) != "releaser":
                raise ValueError("publication task must be assigned to releaser")
            if row["policy_quarantined"] or row["policy_invalidated"]:
                raise ValueError("cannot adopt a quarantined or invalidated publication task")
            return publication_id, "adopted"

        if _canonical_assignee(publication.assignee) != "releaser":
            raise ValueError("publication cards must be assigned to releaser")
        expected = str(publication.expected_sha or "").strip()
        ref = str(publication.remote_ref or "").strip()
        path = str(publication.workspace_path or "").strip()
        if not expected:
            raise ValueError("publication.expected_sha is required")
        if not path:
            raise ValueError("publication.workspace_path is required")
        if not ref:
            raise ValueError("publication.remote_ref is required")
        title = str(publication.title or "").strip() or (
            f"Publish {expected[:12]} to {ref}"
        )
        body = publication.body
        if body is None:
            body = (
                "Publish the recorded commit and verify the remote readback.\n"
                f"Expected SHA: {expected}\n"
                f"Workspace: {path}\n"
                f"Remote ref: {publication.remote or 'origin'} {ref}"
            )
        prepared = _prepare_task_create(
            title=title,
            body=body,
            assignee="releaser",
            created_by=actor,
            workspace_kind="dir",
            workspace_path=path,
            publication_expected_sha=expected,
            publication_remote=publication.remote or "origin",
            publication_ref=ref,
        )
        publication_id = _insert_task_in_txn(
            conn_in,
            prepared,
            idempotency_key=request_key,
            mutation_context=mutation_context,
        )
        return publication_id, "created"

    def payload_factory(
        conn_in: sqlite3.Connection,
        publication_id: str,
        disposition: Literal["created", "adopted"],
    ) -> dict[str, Any]:
        row = conn_in.execute(
            """SELECT publication_expected_sha, publication_remote,
                      publication_ref, workspace_path
                 FROM tasks WHERE id = ?""",
            (publication_id,),
        ).fetchone()
        if row is None:
            raise ValueError("publication card disappeared during handoff")
        return {
            "requester_task_id": requester_task_id,
            "publication_task_id": publication_id,
            "publisher_task_id": publication_id,
            "request_key": request_key,
            "idempotency_key": request_key,
            "actor": actor,
            "publication_action": disposition,
            "expected_sha": row["publication_expected_sha"],
            "workspace_path": row["workspace_path"],
            "remote": row["publication_remote"],
            "remote_ref": row["publication_ref"],
            "summary": summary,
            "metadata": metadata,
        }

    transition = _transition_to_dependency(
        conn,
        requester_task_id,
        request_key=request_key,
        request_event_kind="publication_handoff_requested",
        dependency_event_kind="publication_handoff_for",
        dependency_id_payload_key="publication_task_id",
        materialize_dependency=materialize,
        event_payload=payload_factory,
        terminal_error="cannot request publication for a terminal requester task",
        unknown_error=f"unknown requester task: {requester_task_id}",
        quarantine_error=(
            "cannot request publication for a quarantined or invalidated requester task"
        ),
        run_outcome="publication_handoff_requested",
        run_status="publication_handoff_requested",
        run_summary=summary,
        run_metadata=metadata,
        expected_run_id=expected_run_id,
        require_no_active_run=require_no_active_run,
        mutation_context=mutation_context,
    )
    return PublicationHandoffResult(
        requester_task_id=transition.requester_task_id,
        publication_task_id=transition.dependency_task_id,
        publication_action=transition.dependency_action,
        requester_status=transition.requester_status,
        request_event_id=transition.request_event_id,
        replayed_same_run=transition.replayed_same_run,
    )


# Short internal/API alias for callers that use the card's role as the noun.
request_publication = request_publication_handoff


def _find_missing_parents(conn: sqlite3.Connection, parents: Iterable[str]) -> list[str]:
    parents = list(parents)
    if not parents:
        return []
    placeholders = ",".join("?" * len(parents))
    rows = conn.execute(
        f"SELECT id FROM tasks WHERE id IN ({placeholders})",
        parents,
    ).fetchall()
    present = {r["id"] for r in rows}
    return [p for p in parents if p not in present]


def get_task(conn: sqlite3.Connection, task_id: str) -> Optional[Task]:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return Task.from_row(row) if row else None


# Canonical sort-order mappings for ``hermes kanban list --sort``.
# Each value is a raw SQL fragment appended after ``ORDER BY``.
VALID_SORT_ORDERS: dict[str, str] = {
    "created": "created_at ASC, id ASC",
    "created-desc": "created_at DESC, id DESC",
    "priority": "priority DESC, created_at ASC",
    "priority-desc": "priority ASC, created_at ASC",
    "status": "status ASC, created_at ASC",
    "assignee": "assignee ASC, created_at ASC",
    "title": "title ASC, id ASC",
    "updated": "started_at DESC NULLS LAST, created_at DESC",
}


def list_tasks(
    conn: sqlite3.Connection,
    *,
    assignee: Optional[str] = None,
    status: Optional[str] = None,
    tenant: Optional[str] = None,
    session_id: Optional[str] = None,
    include_archived: bool = False,
    limit: Optional[int] = None,
    order_by: Optional[str] = None,
    workflow_template_id: Optional[str] = None,
    current_step_key: Optional[str] = None,
    workflow_key: Optional[str] = None,
) -> list[Task]:
    query = "SELECT * FROM tasks WHERE 1=1"
    params: list[Any] = []
    if assignee is not None:
        query += " AND assignee = ?"
        params.append(_canonical_assignee(assignee))
    if status is not None:
        if status not in VALID_STATUSES:
            raise ValueError(f"status must be one of {sorted(VALID_STATUSES)}")
        query += " AND status = ?"
        params.append(status)
    if tenant is not None:
        query += " AND tenant = ?"
        params.append(tenant)
    if session_id is not None:
        query += " AND session_id = ?"
        params.append(session_id)
    if workflow_template_id is not None:
        query += " AND workflow_template_id = ?"
        params.append(workflow_template_id)
    if current_step_key is not None:
        query += " AND current_step_key = ?"
        params.append(current_step_key)
    if workflow_key is not None:
        query += " AND workflow_key = ?"
        params.append(workflow_key)
    if not include_archived and status != "archived":
        query += " AND status != 'archived'"
    if order_by is not None:
        order_by = order_by.strip().lower()
        if order_by not in VALID_SORT_ORDERS:
            raise ValueError(
                f"order_by must be one of {sorted(VALID_SORT_ORDERS.keys())}"
            )
        query += f" ORDER BY {VALID_SORT_ORDERS[order_by]}"
    else:
        query += " ORDER BY priority DESC, created_at ASC"
    if limit:
        query += f" LIMIT {int(limit)}"
    rows = conn.execute(query, params).fetchall()
    return [Task.from_row(r) for r in rows]


def list_tasks_by_workflow_key(
    conn: sqlite3.Connection,
    workflow_key: str,
    *,
    include_archived: bool = False,
) -> list[Task]:
    """List all tasks sharing a ``workflow_key``, ordered by creation time.

    Convenience wrapper that feeds through :func:`list_tasks` so all existing
    filters compose naturally.  Tasks without a workflow key are excluded
    (passing an empty string raises ``ValueError``).
    """
    if not workflow_key or not workflow_key.strip():
        raise ValueError("workflow_key is required and cannot be empty")
    return list_tasks(
        conn,
        workflow_key=workflow_key.strip(),
        include_archived=include_archived,
        order_by="created",
    )


def assign_task(conn: sqlite3.Connection, task_id: str, profile: Optional[str]) -> bool:
    """Assign or reassign a task.  Returns True on success.

    Refuses to reassign a task that's currently running (claim_lock set).
    Reassign after the current run completes if needed.
    """
    profile = _canonical_assignee(profile)
    with write_txn(conn):
        row = conn.execute(
            "SELECT status, claim_lock, assignee FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if not row:
            return False
        if row["claim_lock"] is not None and row["status"] == "running":
            raise RuntimeError(
                f"cannot reassign {task_id}: currently running (claimed). "
                "Wait for completion or reclaim the stale lock first."
            )
        if row["assignee"] != profile:
            # The retry guard is scoped to the task/profile combination. A
            # human reassigning the task is an explicit recovery action, so the
            # new profile should not inherit the previous profile's streak.
            conn.execute(
                "UPDATE tasks SET assignee = ?, consecutive_failures = 0, "
                "last_failure_error = NULL WHERE id = ?",
                (profile, task_id),
            )
        else:
            conn.execute("UPDATE tasks SET assignee = ? WHERE id = ?", (profile, task_id))
        if row["assignee"] != profile:
            _invalidate_architect_gate_for_mutation(
                conn, task_id, reason="architect_scope_changed",
            )
        _append_event(conn, task_id, "assigned", {"assignee": profile})
        return True


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------

def _link_tasks_in_txn(
    conn: sqlite3.Connection,
    parent_id: str,
    child_id: str,
    *,
    mutation_context: Optional[MutationContext] = None,
    emit_event: bool = True,
) -> bool:
    """Insert one dependency edge while the caller owns ``write_txn``."""
    if parent_id == child_id:
        raise ValueError("a task cannot depend on itself")
    gate = get_architecture_gate_for_task(conn, child_id) or get_architecture_gate_for_task(conn, parent_id)
    if mutation_context is not None:
        if gate is not None and _gate_requires_enforcement(gate) and mutation_context.phase != "architecture":
            raise ArchitectureGateError(ARCHITECTURE_GATE_REASON_OPEN)
        if gate is not None and mutation_context.mode.strip().lower() == "shadow":
            _append_gate_audit(conn, gate, "create_allowed", ARCHITECTURE_GATE_REASON_OPEN)
    elif gate is not None and gate.enforcement_mode in ARCHITECTURE_GATE_ENFORCING_MODES:
        if _gate_requires_enforcement(gate):
            raise ArchitectureGateError(ARCHITECTURE_GATE_REASON_OPEN)
        if conn.execute(
            "SELECT 1 FROM architecture_graph_issuances WHERE gate_id = ?", (gate.gate_id,)
        ).fetchone() is not None:
            raise ArchitectureGateError("architecture_graph_issued")
    missing = _find_missing_parents(conn, [parent_id, child_id])
    if missing:
        raise ValueError(f"unknown task(s): {', '.join(missing)}")
    if _would_cycle(conn, parent_id, child_id):
        raise ValueError(f"linking {parent_id} -> {child_id} would create a cycle")
    cur = conn.execute(
        "INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
        (parent_id, child_id),
    )
    parent = conn.execute(
        "SELECT status, policy_quarantined, policy_invalidated FROM tasks WHERE id = ?",
        (parent_id,),
    ).fetchone()
    if parent is not None and not _parent_is_satisfied(parent):
        conn.execute(
            "UPDATE tasks SET status = 'todo' WHERE id = ? AND status = 'ready'",
            (child_id,),
        )
    if emit_event:
        _append_event(
            conn, child_id, "linked",
            {"parent": parent_id, "child": child_id},
        )
    return cur.rowcount > 0


def link_tasks(
    conn: sqlite3.Connection,
    parent_id: str,
    child_id: str,
    *,
    mutation_context: Optional[MutationContext] = None,
) -> None:
    with write_txn(conn):
        _link_tasks_in_txn(
            conn,
            parent_id,
            child_id,
            mutation_context=mutation_context,
        )


def _would_cycle(conn: sqlite3.Connection, parent_id: str, child_id: str) -> bool:
    """Return True if adding parent->child creates a cycle.

    A cycle exists iff ``parent_id`` is already a descendant of
    ``child_id`` via existing parent->child links.  We walk downward
    from ``child_id`` and check whether we reach ``parent_id``.
    """
    seen = set()
    stack = [child_id]
    while stack:
        node = stack.pop()
        if node == parent_id:
            return True
        if node in seen:
            continue
        seen.add(node)
        rows = conn.execute(
            "SELECT child_id FROM task_links WHERE parent_id = ?", (node,)
        ).fetchall()
        stack.extend(r["child_id"] for r in rows)
    return False


def unlink_tasks(conn: sqlite3.Connection, parent_id: str, child_id: str) -> bool:
    with write_txn(conn):
        cur = conn.execute(
            "DELETE FROM task_links WHERE parent_id = ? AND child_id = ?",
            (parent_id, child_id),
        )
        if cur.rowcount:
            _append_event(
                conn, child_id, "unlinked",
                {"parent": parent_id, "child": child_id},
            )
        removed = cur.rowcount > 0
    if removed:
        # Dependency edge removed — re-evaluate promotion eligibility for the
        # child immediately.  Matches the contract of complete_task and
        # unblock_task; without this the child stays stuck in todo until the
        # next dispatcher tick or a manual `hermes kanban recompute` (issue #22459).
        recompute_ready(conn)
    return removed


def parent_ids(conn: sqlite3.Connection, task_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT parent_id FROM task_links WHERE child_id = ? ORDER BY parent_id",
        (task_id,),
    ).fetchall()
    return [r["parent_id"] for r in rows]


def child_ids(conn: sqlite3.Connection, task_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT child_id FROM task_links WHERE parent_id = ? ORDER BY child_id",
        (task_id,),
    ).fetchall()
    return [r["child_id"] for r in rows]


def parent_results(conn: sqlite3.Connection, task_id: str) -> list[tuple[str, Optional[str]]]:
    """Return ``(parent_id, result)`` for every done parent of ``task_id``."""
    rows = conn.execute(
        """
        SELECT t.id AS id, t.result AS result
        FROM tasks t
        JOIN task_links l ON l.parent_id = t.id
        WHERE l.child_id = ? AND t.status = 'done'
        ORDER BY t.completed_at ASC
        """,
        (task_id,),
    ).fetchall()
    return [(r["id"], r["result"]) for r in rows]


# ---------------------------------------------------------------------------
# Comments & events
# ---------------------------------------------------------------------------

def _add_comment_in_txn(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    author: str,
    body: str,
    created_at: Optional[int] = None,
) -> int:
    """Insert one comment while the caller owns the write transaction."""
    if not body or not body.strip():
        raise ValueError("comment body is required")
    if not author or not author.strip():
        raise ValueError("comment author is required")
    if not conn.execute(
        "SELECT 1 FROM tasks WHERE id = ?", (task_id,)
    ).fetchone():
        raise ValueError(f"unknown task {task_id}")
    now = int(time.time()) if created_at is None else int(created_at)
    cur = conn.execute(
        "INSERT INTO task_comments (task_id, author, body, created_at) "
        "VALUES (?, ?, ?, ?)",
        (task_id, author.strip(), body.strip(), now),
    )
    _append_event(
        conn,
        task_id,
        "commented",
        {"author": author.strip(), "len": len(body.strip())},
    )
    return int(cur.lastrowid or 0)


def add_comment(
    conn: sqlite3.Connection, task_id: str, author: str, body: str
) -> int:
    if not body or not body.strip():
        raise ValueError("comment body is required")
    if not author or not author.strip():
        raise ValueError("comment author is required")
    with write_txn(conn):
        return _add_comment_in_txn(
            conn, task_id, author=author, body=body,
        )


def list_comments(conn: sqlite3.Connection, task_id: str) -> list[Comment]:
    rows = conn.execute(
        "SELECT * FROM task_comments WHERE task_id = ? ORDER BY created_at ASC",
        (task_id,),
    ).fetchall()
    return [
        Comment(
            id=r["id"],
            task_id=r["task_id"],
            author=r["author"],
            body=r["body"],
            created_at=r["created_at"],
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------

# The attachment size cap is the module-level ``KANBAN_ATTACHMENT_MAX_BYTES``
# (defined near the top of this file) — one constant shared by the dashboard
# HTTP endpoint, the agent toolset, and the CLI so the limit cannot drift
# between surfaces.


class AttachmentTooLarge(ValueError):
    """Raised when an attachment exceeds the configured size cap.

    Subclasses :class:`ValueError` so generic ``except ValueError`` handlers
    (e.g. the dashboard's 400 fallback) still catch it, while callers that
    want a distinct user-facing message (the tool/CLI 413-equivalent) can
    catch it specifically.
    """


class ReviewArtifactError(ValueError):
    """Raised when a review artifact cannot be selected or authenticated."""


def _safe_attachment_name(raw: str) -> str:
    """Reduce a client-supplied filename to a safe basename.

    Strips any directory components (both separators) so a malicious
    ``../../etc/passwd`` or ``C:\\x`` collapses to its leaf. Drops control
    chars and leading dots so we never write a dotfile or a name with
    embedded NULs/newlines. Rejects empty / dotfile-only names. The result
    is only ever joined under the per-task attachments dir, never used
    verbatim as a path from the client.

    Raises :class:`ValueError` on an unusable name; HTTP callers map that
    to a 400.
    """
    name = (raw or "").replace("\\", "/").split("/")[-1].strip()
    name = "".join(ch for ch in name if ch.isprintable() and ch not in "\x00").strip()
    name = name.lstrip(".").strip()
    if not name:
        raise ValueError("invalid attachment filename")
    return name[:200]


def _collision_free_path(dest_dir: Path, safe_name: str) -> Path:
    """Return a path under ``dest_dir`` that doesn't clobber an existing file.

    ``foo.pdf`` → ``foo.pdf``, then ``foo (1).pdf``, ``foo (2).pdf``, …
    ``safe_name`` must already be sanitised via :func:`_safe_attachment_name`.
    """
    stem, dot, ext = safe_name.partition(".")
    candidate = safe_name
    n = 1
    while (dest_dir / candidate).exists():
        candidate = f"{stem} ({n}){dot}{ext}"
        n += 1
    return dest_dir / candidate


def store_attachment_bytes(
    conn: sqlite3.Connection,
    task_id: str,
    filename: str,
    data: bytes,
    *,
    content_type: Optional[str] = None,
    uploaded_by: Optional[str] = None,
    board: Optional[str] = None,
    max_bytes: Optional[int] = None,
) -> int:
    """Validate, size-check, persist a blob, and record its metadata row.

    This is the single write path shared by the dashboard endpoint, the
    agent toolset (``kanban_attach`` / ``kanban_attach_url``), and the CLI
    (``hermes kanban attach``) so name-sanitisation, the size cap, and the
    collision-resolution all behave identically everywhere.

    Steps: enforce ``max_bytes``, sanitise ``filename`` to a safe basename,
    write the bytes under :func:`task_attachments_dir` with a
    collision-free name, then insert the ``task_attachments`` row via
    :func:`add_attachment`. Returns the new attachment id.

    Raises :class:`AttachmentTooLarge` when ``data`` exceeds ``max_bytes``,
    or :class:`ValueError` for a bad filename / unknown task. On any failure
    after the blob is written (e.g. the task disappeared) the orphaned blob
    is removed before re-raising.
    """
    if max_bytes is None:
        max_bytes = KANBAN_ATTACHMENT_MAX_BYTES
    if len(data) > max_bytes:
        raise AttachmentTooLarge(
            f"attachment exceeds {max_bytes // (1024 * 1024)} MB limit"
        )
    safe_name = _safe_attachment_name(filename)
    dest_dir = task_attachments_dir(task_id, board=board)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = _collision_free_path(dest_dir, safe_name)
    dest_path.write_bytes(data)
    try:
        return add_attachment(
            conn,
            task_id,
            filename=dest_path.name,
            stored_path=str(dest_path.resolve()),
            content_type=content_type,
            size=len(data),
            uploaded_by=uploaded_by,
        )
    except Exception:
        # Don't leave an orphan blob if the metadata insert fails (most
        # commonly: the task id doesn't exist).
        try:
            dest_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def add_attachment(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    filename: str,
    stored_path: str,
    content_type: Optional[str] = None,
    size: int = 0,
    uploaded_by: Optional[str] = None,
) -> int:
    """Record a file attachment for a task. Returns the new attachment id.

    The caller is responsible for writing the blob to ``stored_path``
    first (under :func:`task_attachments_dir`); this only persists the
    metadata row and appends an ``attached`` event.
    """
    if not filename or not filename.strip():
        raise ValueError("attachment filename is required")
    if not stored_path or not stored_path.strip():
        raise ValueError("attachment stored_path is required")
    now = int(time.time())
    with write_txn(conn):
        if not conn.execute(
            "SELECT 1 FROM tasks WHERE id = ?", (task_id,)
        ).fetchone():
            raise ValueError(f"unknown task {task_id}")
        cur = conn.execute(
            "INSERT INTO task_attachments "
            "(task_id, filename, stored_path, content_type, size, uploaded_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                filename.strip(),
                stored_path,
                content_type,
                int(size),
                uploaded_by,
                now,
            ),
        )
        _append_event(
            conn,
            task_id,
            "attached",
            {"filename": filename.strip(), "size": int(size), "by": uploaded_by},
        )
        return int(cur.lastrowid or 0)


def list_attachments(conn: sqlite3.Connection, task_id: str) -> list[Attachment]:
    rows = conn.execute(
        "SELECT * FROM task_attachments WHERE task_id = ? ORDER BY created_at ASC, id ASC",
        (task_id,),
    ).fetchall()
    return [
        Attachment(
            id=r["id"],
            task_id=r["task_id"],
            filename=r["filename"],
            stored_path=r["stored_path"],
            content_type=r["content_type"],
            size=r["size"] or 0,
            uploaded_by=r["uploaded_by"],
            created_at=r["created_at"],
        )
        for r in rows
    ]


def get_attachment(conn: sqlite3.Connection, attachment_id: int) -> Optional[Attachment]:
    r = conn.execute(
        "SELECT * FROM task_attachments WHERE id = ?", (attachment_id,)
    ).fetchone()
    if r is None:
        return None
    return Attachment(
        id=r["id"],
        task_id=r["task_id"],
        filename=r["filename"],
        stored_path=r["stored_path"],
        content_type=r["content_type"],
        size=r["size"] or 0,
        uploaded_by=r["uploaded_by"],
        created_at=r["created_at"],
    )


def delete_attachment(conn: sqlite3.Connection, attachment_id: int) -> Optional[Attachment]:
    """Delete an attachment row and its on-disk blob. Returns the removed row.

    Returns ``None`` when no row matched. The blob is removed best-effort
    (a missing file is not an error); the metadata row is the source of
    truth for whether an attachment "exists".
    """
    with write_txn(conn):
        att = get_attachment(conn, attachment_id)
        if att is None:
            return None
        referenced = conn.execute(
            "SELECT review_task_id, generation FROM review_artifact_bindings "
            "WHERE attachment_id = ? ORDER BY review_task_id, generation",
            (attachment_id,),
        ).fetchall()
        if referenced:
            refs = ", ".join(
                f"{row['review_task_id']}@{row['generation']}"
                for row in referenced
            )
            raise ReviewArtifactError(
                f"attachment {attachment_id} is referenced by review artifact "
                f"binding(s): {refs}"
            )
        conn.execute("DELETE FROM task_attachments WHERE id = ?", (attachment_id,))
        _append_event(
            conn, att.task_id, "attachment_removed", {"filename": att.filename}
        )
    try:
        p = Path(att.stored_path)
        if p.is_file():
            p.unlink()
    except OSError:
        pass
    return att


def _review_artifact_binding_from_row(
    row: Optional[sqlite3.Row],
) -> Optional[ReviewArtifactBinding]:
    if row is None:
        return None
    keys = set(row.keys())
    return ReviewArtifactBinding(
        review_task_id=str(row["review_task_id"]),
        generation=int(row["generation"]),
        attachment_id=int(row["attachment_id"]),
        sha256=str(row["sha256"]),
        source_task_id=str(row["source_task_id"]),
        source_run_id=(
            int(row["source_run_id"])
            if row["source_run_id"] is not None else None
        ),
        source_rework_event_id=int(row["source_rework_event_id"]),
        created_at=int(row["created_at"]),
        filename=(row["filename"] if "filename" in keys else None),
        stored_path=(row["stored_path"] if "stored_path" in keys else None),
        attachment_task_id=(
            row["attachment_task_id"] if "attachment_task_id" in keys else None
        ),
        size=(int(row["size"]) if "size" in keys and row["size"] is not None else None),
    )


def get_current_review_artifact(
    conn: sqlite3.Connection,
    review_task_id: str,
) -> Optional[ReviewArtifactBinding]:
    """Return the highest explicit artifact generation for a review.

    This is intentionally a read of structured state only. Callers that are
    about to trust the bytes must use the integrity validator below; a row can
    remain present after an out-of-band filesystem mutation or a deleted
    attachment attempt.
    """
    row = conn.execute(
        """SELECT b.*, a.task_id AS attachment_task_id, a.filename,
                         a.stored_path, a.size
              FROM review_artifact_bindings b
              LEFT JOIN task_attachments a ON a.id = b.attachment_id
             WHERE b.review_task_id = ?
             ORDER BY b.generation DESC
             LIMIT 1""",
        (str(review_task_id),),
    ).fetchone()
    return _review_artifact_binding_from_row(row)


def _trusted_attachments_root(conn: sqlite3.Connection) -> Path:
    """Attachments root for the board that owns ``conn`` -- never task-derived.

    The containment boundary for an artifact must come from a trusted source,
    not from the attachment's own (untrusted) ``task_id`` or ``stored_path``.
    We derive the owning board from the connection's database file
    (``PRAGMA database_list``) and map it to that board's attachments root,
    honoring the ``HERMES_KANBAN_ATTACHMENTS_ROOT`` override. A non-canonical
    DB path with no override fails closed. See BUILD-711.
    """
    override = os.environ.get("HERMES_KANBAN_ATTACHMENTS_ROOT", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    row = conn.execute("PRAGMA database_list").fetchone()
    db_file = row[2] if row is not None else None
    if db_file:
        db_path = Path(db_file).resolve()
        # Map the connection's ACTUAL db file to its board's attachments root
        # using canonical layout constants -- never kanban_db_path(), which
        # honors HERMES_KANBAN_DB and would collapse every overridden db to the
        # "default" comparison. Default board: <home>/kanban.db. Named board:
        # <home>/kanban/boards/<slug>/kanban.db (exact structure required).
        if db_path == (kanban_home() / "kanban.db").resolve():
            return attachments_root(board=DEFAULT_BOARD).resolve()
        # The directory name must already BE the canonical slug -- otherwise
        # _normalize_board_slug would remap a non-canonical dir (e.g. 'PROJ',
        # 'proj ', or a named dir literally called 'default') onto a different
        # board's attachments root. Require an exact, canonical, non-default
        # slug or fail closed. (Sol review, BUILD-711.)
        slug = db_path.parent.name
        try:
            canonical_slug = _normalize_board_slug(slug)
        except ValueError:
            canonical_slug = None
        if (
            db_path.name == "kanban.db"
            and db_path.parent.parent == boards_root().resolve()
            and slug == canonical_slug
            and slug != DEFAULT_BOARD
        ):
            return attachments_root(board=slug).resolve()
    raise ReviewArtifactError(
        "review artifact owning board root is unresolvable for this connection"
    )


def _hash_review_attachment(
    conn: sqlite3.Connection,
    attachment: Attachment,
) -> str:
    """Rehash one attachment while proving path ownership and read stability."""
    if not attachment.stored_path:
        raise ReviewArtifactError("review artifact has no stored path")
    path = Path(attachment.stored_path)
    try:
        resolved = path.resolve(strict=True)
        root = _trusted_attachments_root(conn)
    except (OSError, RuntimeError) as exc:
        raise ReviewArtifactError(
            f"review artifact path cannot be resolved: {attachment.stored_path}"
        ) from exc
    # Boundary comes from the trusted board root; the id is validated as a
    # single safe component; the file must sit exactly at <root>/<task_id>/<name>.
    # This closes both the traversal escape (untrusted task_id moving the root)
    # and the false reject of a legitimate non-current-board attachment that the
    # old current-board-derived boundary produced. BUILD-711.
    try:
        owner_task_id = _validate_task_id_component(attachment.task_id)
    except ValueError as exc:
        raise ReviewArtifactError(
            f"review artifact has an unsafe owning task id: {attachment.task_id!r}"
        ) from exc
    try:
        relative = resolved.relative_to(root)
    except ValueError:
        raise ReviewArtifactError(
            "review artifact path escaped its owning attachment directory"
        )
    if len(relative.parts) != 2 or relative.parts[0] != owner_task_id:
        raise ReviewArtifactError(
            "review artifact path escaped its owning attachment directory"
        )

    try:
        before = resolved.stat()
    except OSError as exc:
        raise ReviewArtifactError(
            f"review artifact is unavailable: {resolved}"
        ) from exc
    if not resolved.is_file():
        raise ReviewArtifactError(f"review artifact is not a regular file: {resolved}")
    if before.st_size > KANBAN_ATTACHMENT_MAX_BYTES:
        raise ReviewArtifactError("review artifact exceeds the attachment size limit")

    digest = hashlib.sha256()
    total = 0
    try:
        with resolved.open("rb") as source:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > KANBAN_ATTACHMENT_MAX_BYTES:
                    raise ReviewArtifactError(
                        "review artifact exceeds the attachment size limit"
                    )
                digest.update(chunk)
            after = os.fstat(source.fileno())
        current = resolved.stat()
    except ReviewArtifactError:
        raise
    except OSError as exc:
        raise ReviewArtifactError(
            f"review artifact could not be read: {resolved}"
        ) from exc

    if (
        total != before.st_size
        or before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or current.st_dev != before.st_dev
        or current.st_ino != before.st_ino
        or current.st_size != before.st_size
    ):
        raise ReviewArtifactError(
            f"review artifact changed during read: {resolved}"
        )
    if int(attachment.size) != total:
        raise ReviewArtifactError(
            "review artifact integrity mismatch: metadata size does not match "
            "the stored bytes"
        )
    return digest.hexdigest()


def _validate_review_attachment(
    conn: sqlite3.Connection,
    attachment_id: int,
    *,
    owner_task_id: Optional[str] = None,
) -> tuple[Attachment, str]:
    try:
        normalized_id = int(attachment_id)
    except (TypeError, ValueError) as exc:
        raise ReviewArtifactError("attachment_id must be an integer") from exc
    if normalized_id <= 0:
        raise ReviewArtifactError("attachment_id must be positive")
    attachment = get_attachment(conn, normalized_id)
    if attachment is None:
        raise ReviewArtifactError(f"unknown attachment {normalized_id}")
    if owner_task_id is not None and attachment.task_id != str(owner_task_id):
        raise ReviewArtifactError(
            f"attachment {normalized_id} does not belong to task {owner_task_id}"
        )
    return attachment, _hash_review_attachment(conn, attachment)


def _body_contains_exact_stored_path(body: Optional[str], stored_path: str) -> bool:
    """Match a stored path as a delimited body token, never by glob/newest."""
    if not body or not stored_path:
        return False
    start = 0
    delimiters = set(" \t\r\n`'\"()[]{}<>:,;!")
    while True:
        index = body.find(stored_path, start)
        if index < 0:
            return False
        before_ok = index == 0 or body[index - 1] in delimiters
        end = index + len(stored_path)
        after_ok = end == len(body) or body[end] in delimiters
        if before_ok and after_ok:
            return True
        start = index + 1


def _legacy_review_artifact_matches(
    conn: sqlite3.Connection,
    review_task_id: str,
) -> list[Attachment]:
    task_row = conn.execute(
        "SELECT body FROM tasks WHERE id = ?", (review_task_id,)
    ).fetchone()
    body = task_row["body"] if task_row is not None else None
    if not body:
        return []
    rows = conn.execute(
        "SELECT * FROM task_attachments ORDER BY id ASC"
    ).fetchall()
    matches: list[Attachment] = []
    for row in rows:
        if _body_contains_exact_stored_path(body, str(row["stored_path"] or "")):
            matches.append(
                Attachment(
                    id=int(row["id"]),
                    task_id=str(row["task_id"]),
                    filename=str(row["filename"]),
                    stored_path=str(row["stored_path"]),
                    content_type=row["content_type"],
                    size=int(row["size"] or 0),
                    uploaded_by=row["uploaded_by"],
                    created_at=int(row["created_at"]),
                )
            )
    return matches


def _body_references_attachment_location(
    conn: sqlite3.Connection,
    review_task_id: str,
) -> bool:
    """Detect an artifact-looking absolute path without treating code paths as artifacts."""
    task_row = conn.execute(
        "SELECT body FROM tasks WHERE id = ?", (review_task_id,)
    ).fetchone()
    body = str(task_row["body"] or "") if task_row is not None else ""
    if not body:
        return False
    # Detection path -- must return a bool, never raise. task_attachments_dir
    # now rejects an unsafe id (BUILD-711); a malformed review id here simply
    # can't reference a valid attachment root, so fall back to the generic
    # marker rather than propagating the ValueError.
    try:
        roots = {str(task_attachments_dir(review_task_id).resolve(strict=False))}
    except ValueError:
        roots = set()
    return any(root in body for root in roots) or "/attachments/" in body


def _seed_review_artifact_binding_in_txn(
    conn: sqlite3.Connection,
    review_task_id: str,
    attachment: Attachment,
    *,
    now: int,
) -> ReviewArtifactBinding:
    """Seed generation 1 from an exact legacy body/path match."""
    if not conn.execute(
        "SELECT 1 FROM tasks WHERE id = ?", (review_task_id,)
    ).fetchone():
        raise ReviewArtifactError(f"unknown review task {review_task_id}")
    current = get_current_review_artifact(conn, review_task_id)
    if current is not None:
        return current
    _attachment, digest = _validate_review_attachment(conn, attachment.id)
    conn.execute(
        """INSERT INTO review_artifact_bindings
           (review_task_id, generation, attachment_id, sha256, source_task_id,
            source_run_id, source_rework_event_id, created_at)
           VALUES (?, 1, ?, ?, ?, NULL, 0, ?)""",
        (
            review_task_id, attachment.id, digest, attachment.task_id, int(now),
        ),
    )
    _append_event(
        conn,
        review_task_id,
        "review_artifact_bound",
        {
            "generation": 1,
            "attachment_id": attachment.id,
            "sha256": digest,
            "source_task_id": attachment.task_id,
            "source_rework_event_id": 0,
            "backfill": True,
        },
        created_at=int(now),
    )
    seeded = get_current_review_artifact(conn, review_task_id)
    if seeded is None:
        raise ReviewArtifactError("review artifact seed was not persisted")
    return seeded


def _ensure_review_artifact_binding_in_txn(
    conn: sqlite3.Connection,
    review_task_id: str,
    *,
    now: int,
    require_if_referenced: bool = False,
) -> Optional[ReviewArtifactBinding]:
    """Return the current binding, or deterministically seed legacy gen 1."""
    current = get_current_review_artifact(conn, review_task_id)
    if current is not None:
        return current
    matches = _legacy_review_artifact_matches(conn, review_task_id)
    if len(matches) == 1:
        return _seed_review_artifact_binding_in_txn(
            conn, review_task_id, matches[0], now=now,
        )
    if len(matches) > 1:
        raise ReviewArtifactError(
            f"artifact_selection_required: review {review_task_id} has "
            f"{len(matches)} exact attachment matches"
        )
    if require_if_referenced and _body_references_attachment_location(
        conn, review_task_id,
    ):
        raise ReviewArtifactError(
            f"artifact_selection_required: review {review_task_id} has no "
            "attachment row matching its pinned path"
        )
    return None


def _latest_rework_event_for_review(
    conn: sqlite3.Connection,
    review_task_id: str,
) -> tuple[int, dict[str, Any]]:
    row = conn.execute(
        "SELECT id, payload FROM task_events "
        "WHERE task_id = ? AND kind = 'rework_requested' "
        "ORDER BY id DESC LIMIT 1",
        (review_task_id,),
    ).fetchone()
    if row is None:
        raise ReviewArtifactError(
            f"review {review_task_id} has no rework_requested lineage event"
        )
    try:
        payload = json.loads(row["payload"] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ReviewArtifactError(
            f"review {review_task_id} has malformed rework lineage"
        ) from exc
    if not isinstance(payload, dict):
        raise ReviewArtifactError(
            f"review {review_task_id} has malformed rework lineage"
        )
    return int(row["id"]), payload


def bind_review_artifact_in_txn(
    conn: sqlite3.Connection,
    review_task_id: str,
    attachment_id: int,
    source_fix_task_id: str,
    source_run_id: Optional[int],
    source_rework_event_id: int,
    expected_generation: int,
    now: int,
) -> ReviewArtifactBinding:
    """Append the next authoritative artifact generation inside a write txn.

    The caller owns the surrounding :func:`write_txn`.  Validation happens
    before the insert: the rework event must still be the latest request for
    this review, the selected attachment must belong to the completing fix,
    and the bytes must hash to the stored digest.  The source-event UNIQUE
    constraint makes a replay return the original generation without writing a
    second row or event.
    """
    review_task_id = str(review_task_id or "").strip()
    source_fix_task_id = str(source_fix_task_id or "").strip()
    if not review_task_id:
        raise ReviewArtifactError("review_task_id is required")
    if not source_fix_task_id:
        raise ReviewArtifactError("source_fix_task_id is required")
    if type(expected_generation) is not int or expected_generation < 0:
        raise ReviewArtifactError("expected_generation must be a non-negative integer")
    if type(source_rework_event_id) is not int or source_rework_event_id <= 0:
        raise ReviewArtifactError("source_rework_event_id must be a positive integer")
    if source_run_id is not None and (
        type(source_run_id) is not int or source_run_id <= 0
    ):
        raise ReviewArtifactError("source_run_id must be a positive integer or None")
    try:
        normalized_now = int(now)
    except (TypeError, ValueError) as exc:
        raise ReviewArtifactError("now must be an integer") from exc

    existing_row = conn.execute(
        """SELECT b.*, a.task_id AS attachment_task_id, a.filename,
                         a.stored_path, a.size
              FROM review_artifact_bindings b
              LEFT JOIN task_attachments a ON a.id = b.attachment_id
             WHERE b.review_task_id = ? AND b.source_rework_event_id = ?""",
        (review_task_id, source_rework_event_id),
    ).fetchone()
    attachment, digest = _validate_review_attachment(
        conn, attachment_id, owner_task_id=source_fix_task_id,
    )
    if source_run_id is not None:
        run_row = conn.execute(
            "SELECT task_id FROM task_runs WHERE id = ?", (source_run_id,)
        ).fetchone()
        if run_row is None or run_row["task_id"] != source_fix_task_id:
            raise ReviewArtifactError(
                f"source_run_id {source_run_id} does not belong to fix task "
                f"{source_fix_task_id}"
            )

    if existing_row is not None:
        existing = _review_artifact_binding_from_row(existing_row)
        assert existing is not None
        if (
            existing.attachment_id != attachment.id
            or existing.source_task_id != source_fix_task_id
            or existing.sha256 != digest
        ):
            raise ReviewArtifactError(
                "review artifact replay conflicts with its original binding"
            )
        return existing

    latest_event_id, latest_payload = _latest_rework_event_for_review(
        conn, review_task_id,
    )
    if latest_event_id != source_rework_event_id:
        raise ReviewArtifactError(
            "review artifact source rework event is no longer current"
        )
    if str(latest_payload.get("fix_task_id") or "").strip() != source_fix_task_id:
        raise ReviewArtifactError(
            "review artifact source rework event names a different fix task"
        )

    review_row = conn.execute(
        "SELECT id FROM tasks WHERE id = ?", (review_task_id,)
    ).fetchone()
    if review_row is None:
        raise ReviewArtifactError(f"unknown review task {review_task_id}")
    current_row = conn.execute(
        "SELECT MAX(generation) AS generation FROM review_artifact_bindings "
        "WHERE review_task_id = ?",
        (review_task_id,),
    ).fetchone()
    current_generation = int(current_row["generation"] or 0)
    if current_generation != expected_generation:
        raise ReviewArtifactError(
            "review_artifact_generation_conflict: expected "
            f"{expected_generation}, current {current_generation}"
        )
    next_generation = current_generation + 1
    conn.execute(
        """INSERT INTO review_artifact_bindings
           (review_task_id, generation, attachment_id, sha256, source_task_id,
            source_run_id, source_rework_event_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            review_task_id, next_generation, attachment.id, digest,
            source_fix_task_id, source_run_id, source_rework_event_id,
            normalized_now,
        ),
    )
    _append_event(
        conn,
        review_task_id,
        "review_artifact_rebound",
        {
            "review_task_id": review_task_id,
            "generation": next_generation,
            "attachment_id": attachment.id,
            "sha256": digest,
            "source_task_id": source_fix_task_id,
            "source_run_id": source_run_id,
            "source_rework_event_id": source_rework_event_id,
        },
        run_id=source_run_id,
        created_at=normalized_now,
    )
    _invalidate_review_artifact_authorizations_in_txn(
        conn,
        review_task_id,
        reason="review_artifact_rebound",
        now=normalized_now,
    )
    bound = get_current_review_artifact(conn, review_task_id)
    if bound is None or bound.generation != next_generation:
        raise ReviewArtifactError("review artifact binding was not persisted")
    return bound


def _verify_review_artifact_binding(
    conn: sqlite3.Connection,
    binding: ReviewArtifactBinding,
) -> Attachment:
    """Authenticate the current binding against its live attachment bytes."""
    attachment, digest = _validate_review_attachment(
        conn,
        binding.attachment_id,
        owner_task_id=binding.source_task_id,
    )
    if digest != binding.sha256:
        raise ReviewArtifactError(
            f"review artifact digest mismatch for generation {binding.generation}: "
            f"expected {binding.sha256}, got {digest}"
        )
    return attachment


def list_events(conn: sqlite3.Connection, task_id: str) -> list[Event]:
    rows = conn.execute(
        "SELECT * FROM task_events WHERE task_id = ? ORDER BY created_at ASC, id ASC",
        (task_id,),
    ).fetchall()
    out = []
    for r in rows:
        try:
            payload = json.loads(r["payload"]) if r["payload"] else None
        except Exception:
            payload = None
        out.append(
            Event(
                id=r["id"],
                task_id=r["task_id"],
                kind=r["kind"],
                payload=payload,
                created_at=r["created_at"],
                run_id=(int(r["run_id"]) if "run_id" in r.keys() and r["run_id"] is not None else None),
            )
        )
    return out


def _append_event(
    conn: sqlite3.Connection,
    task_id: str,
    kind: str,
    payload: Optional[dict] = None,
    *,
    run_id: Optional[int] = None,
    created_at: Optional[int] = None,
) -> int:
    """Record an event row.  Called from within an already-open txn.

    ``run_id`` is optional: pass the current run id so UIs can group
    events by attempt. For events that aren't scoped to a single run
    (task created/edited/archived, dependency promotion) leave it None
    and the row carries NULL.
    """
    now = int(time.time()) if created_at is None else int(created_at)
    pl = json.dumps(payload, ensure_ascii=False) if payload else None
    cur = conn.execute(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (task_id, run_id, kind, pl, now),
    )
    event_id = cur.lastrowid
    assert event_id is not None
    return int(event_id)


def _end_run(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    outcome: str,
    summary: Optional[str] = None,
    error: Optional[str] = None,
    metadata: Optional[dict] = None,
    status: Optional[str] = None,
) -> Optional[int]:
    """Close the currently-active run for ``task_id`` and clear the pointer.

    ``outcome`` is the semantic result (completed / blocked / crashed /
    timed_out / spawn_failed / gave_up / reclaimed). ``status`` is the
    run-row status (usually just ``outcome``, but callers can pass it
    explicitly). Returns the closed run_id or ``None`` if no active run
    existed (e.g. a CLI user calling ``hermes kanban complete`` on a
    task that was never claimed).
    """
    now = int(time.time())
    row = conn.execute(
        "SELECT current_run_id FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    if not row or not row["current_run_id"]:
        return None
    run_id = int(row["current_run_id"])
    conn.execute(
        """
        UPDATE task_runs
           SET status        = ?,
               outcome       = ?,
               summary       = ?,
               error         = ?,
               metadata      = ?,
               ended_at      = ?,
               claim_lock    = NULL,
               claim_expires = NULL,
               worker_pid    = NULL,
               worker_started_at = NULL,
               worker_pgid   = NULL,
               worker_sid    = NULL
         WHERE id = ?
           AND ended_at IS NULL
        """,
        (
            status or outcome,
            outcome,
            summary,
            error,
            json.dumps(metadata, ensure_ascii=False) if metadata else None,
            now,
            run_id,
        ),
    )
    conn.execute(
        "UPDATE tasks SET current_run_id = NULL, claim_lock = NULL, "
        "    claim_expires = NULL, worker_pid = NULL, "
        "    worker_started_at = NULL, worker_pgid = NULL, worker_sid = NULL "
        "WHERE id = ?",
        (task_id,),
    )
    return run_id


def _current_run_id(conn: sqlite3.Connection, task_id: str) -> Optional[int]:
    row = conn.execute(
        "SELECT current_run_id FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    return int(row["current_run_id"]) if row and row["current_run_id"] else None


def _delivery_policy_snapshot(
    gate: Optional[ArchitectureGate],
) -> dict[str, Any]:
    """Return the trusted claim-time delivery authorization for a run."""
    enforcing = (
        gate is not None
        and gate.enforcement_mode in ARCHITECTURE_GATE_ENFORCING_MODES
    )
    if not enforcing:
        return {
            "version": 1,
            "disposition": "none",
            "gate_id": None,
            "architect_task_id": None,
            "state": None,
            "row_version": None,
            "accepted_run_id": None,
            "design_digest": None,
        }
    assert gate is not None
    disposition = (
        "enforcing_approved"
        if gate.state in {"policy_accepted", "human_approved"}
        else "enforcing_unresolved"
    )
    return {
        "version": 1,
        "disposition": disposition,
        "gate_id": gate.gate_id,
        "architect_task_id": gate.architect_task_id,
        "state": gate.state,
        "row_version": gate.row_version,
        "accepted_run_id": gate.accepted_run_id,
        "design_digest": gate.design_digest,
    }


def validate_delivery_policy_snapshot(value: Any) -> dict[str, Any]:
    """Validate and normalize a trusted run delivery attestation."""
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError("invalid delivery policy version")
    disposition = value.get("disposition")
    gate_id = value.get("gate_id")
    architect_task_id = value.get("architect_task_id")
    state = value.get("state")
    row_version = value.get("row_version")
    accepted_run_id = value.get("accepted_run_id")
    design_digest = value.get("design_digest")
    if disposition == "none":
        if any(
            item is not None
            for item in (
                gate_id,
                architect_task_id,
                state,
                row_version,
                accepted_run_id,
                design_digest,
            )
        ):
            raise ValueError("invalid ungated delivery policy")
    elif disposition in {"enforcing_unresolved", "enforcing_approved"}:
        if (
            not isinstance(gate_id, str)
            or not gate_id.strip()
            or gate_id != gate_id.strip()
            or not isinstance(architect_task_id, str)
            or not architect_task_id.strip()
            or architect_task_id != architect_task_id.strip()
            or not isinstance(state, str)
            or type(row_version) is not int
            or row_version < 0
        ):
            raise ValueError("invalid enforcing delivery policy")
        approved_states = {"policy_accepted", "human_approved"}
        unresolved_states = {
            "open",
            "validated_awaiting_approval",
            "invalidated",
            "rejected",
        }
        if disposition == "enforcing_approved":
            if (
                state not in approved_states
                or type(accepted_run_id) is not int
                or accepted_run_id <= 0
                or not isinstance(design_digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", design_digest)
            ):
                raise ValueError("invalid approved delivery policy")
        elif (
            state not in unresolved_states
            or accepted_run_id is not None
            or design_digest is not None
        ):
            raise ValueError("invalid unresolved delivery policy")
    else:
        raise ValueError("invalid delivery policy disposition")
    return {
        "version": 1,
        "disposition": disposition,
        "gate_id": gate_id,
        "architect_task_id": architect_task_id,
        "state": state,
        "row_version": row_version,
        "accepted_run_id": accepted_run_id,
        "design_digest": design_digest,
    }


def _build_run_spec(
    task_row: Optional[sqlite3.Row],
    *,
    architecture_gate: Optional[ArchitectureGate] = None,
) -> dict:
    """Build the immutable, secret-free launch contract for one run."""
    toolsets: Optional[list[str]] = None
    if task_row is not None and "toolsets" in task_row.keys() and task_row["toolsets"] is not None:
        try:
            parsed_toolsets = json.loads(task_row["toolsets"])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("task toolsets are not valid JSON") from exc
        if not isinstance(parsed_toolsets, list) or not parsed_toolsets or any(
            not isinstance(value, str)
            or value.casefold() not in KNOWN_TOOLSET_NAMES
            for value in parsed_toolsets
        ):
            raise ValueError("task toolsets contain unsupported values")
        toolsets = sorted({value.casefold() for value in parsed_toolsets})
    if toolsets is None:
        assignee = str(task_row["assignee"] or "") if task_row else ""
        profile_home: Optional[str] = None
        if assignee:
            try:
                from hermes_cli.profiles import profile_exists, resolve_profile_env

                if profile_exists(assignee):
                    profile_home = resolve_profile_env(assignee)
            except Exception:
                profile_home = None
        # Synthetic/non-profile lanes are never dispatcher-spawned. Resolve
        # against the active home for their audit-only/manual claims so the v2
        # schema remains closed without pretending a mutable null is safe.
        profile_home = profile_home or os.environ.get("HERMES_HOME")
        toolsets = _resolve_worker_cli_toolsets(profile_home)
    if not toolsets:
        raise ValueError("could not resolve effective profile toolsets for run")
    if any(
        not isinstance(value, str)
        or not value.strip()
        or "," in value
        for value in toolsets
    ):
        raise ValueError("effective profile toolsets contain invalid values")
    toolsets = sorted({value.strip().casefold() for value in toolsets})
    if not toolsets:
        raise ValueError("effective profile toolsets are empty")
    return {
        "version": 2,
        "profile": task_row["assignee"] if task_row else None,
        "requested_route": {
            "provider": task_row["model_provider_override"] if task_row else None,
            "model": task_row["model_override"] if task_row else None,
            "reasoning_effort": task_row["model_reasoning_effort"] if task_row else None,
        },
        "toolsets": toolsets,
        "delivery_policy": _delivery_policy_snapshot(architecture_gate),
    }


def get_run_spec(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    task_id: Optional[str] = None,
    require_current: bool = False,
) -> Optional[dict]:
    """Return a run's requested route, optionally guarded by active ownership."""
    clauses = ["r.id = ?"]
    params: list[Any] = [int(run_id)]
    join = ""
    if task_id is not None:
        clauses.append("r.task_id = ?")
        params.append(task_id)
    if require_current:
        join = " JOIN tasks t ON t.id = r.task_id AND t.current_run_id = r.id"
    row = conn.execute(
        "SELECT r.run_spec_json FROM task_runs r" + join
        + " WHERE " + " AND ".join(clauses),
        params,
    ).fetchone()
    if row is None or not row["run_spec_json"]:
        return None
    try:
        value = json.loads(row["run_spec_json"])
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _synthesize_ended_run(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    outcome: str,
    summary: Optional[str] = None,
    error: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> int:
    """Insert a zero-duration, already-closed run row.

    Used when a terminal transition happens on a task that was never
    claimed (CLI user calling ``hermes kanban complete <ready-task>
    --summary X``, or dashboard "mark done" on a ready task). Without
    this, the handoff fields (summary / metadata / error) would be
    silently dropped: ``_end_run`` is a no-op because there's no
    current run.

    The synthetic run has ``started_at == ended_at == now`` so it
    shows up in attempt history as "instant" and doesn't skew elapsed
    stats. Caller is responsible for leaving ``current_run_id`` NULL
    (or for clearing it elsewhere in the same txn) since this
    function does NOT touch the tasks row.
    """
    now = int(time.time())
    trow = conn.execute(
        "SELECT assignee, current_step_key FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    profile = trow["assignee"] if trow else None
    step_key = trow["current_step_key"] if trow else None
    cur = conn.execute(
        """
        INSERT INTO task_runs (
            task_id, profile, step_key,
            status, outcome,
            summary, error, metadata,
            started_at, ended_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_id, profile, step_key,
            outcome, outcome,
            summary, error,
            json.dumps(metadata, ensure_ascii=False) if metadata else None,
            now, now,
        ),
    )
    return int(cur.lastrowid or 0)


# ---------------------------------------------------------------------------
# Dependency resolution (todo -> ready)
# ---------------------------------------------------------------------------

def _parent_is_satisfied(parent: Any) -> bool:
    """Return the one canonical definition of a satisfied parent.

    Terminal status alone is not enough: policy quarantine and invalidation
    deliberately keep a parent from authorizing downstream work.  Accept
    both sqlite rows and ``Task`` instances so every lifecycle surface uses
    the same invariant without opening another read transaction.
    """
    if isinstance(parent, Task):
        status = parent.status
        quarantined = parent.policy_quarantined
        invalidated = parent.policy_invalidated
    else:
        try:
            status = parent["status"]
        except (KeyError, TypeError, IndexError):
            status = getattr(parent, "status", None)
        try:
            quarantined = bool(parent["policy_quarantined"])
        except (KeyError, TypeError, IndexError):
            quarantined = bool(getattr(parent, "policy_quarantined", False))
        try:
            invalidated = bool(parent["policy_invalidated"])
        except (KeyError, TypeError, IndexError):
            invalidated = bool(getattr(parent, "policy_invalidated", False))
    return (
        status in ("done", "archived")
        and not quarantined
        and not invalidated
    )


def _resolve_dependency_materialization_sla_seconds(
    value: Optional[int] = None,
) -> int:
    """Resolve the wait SLA without moving already-persisted deadlines."""
    if value is not None:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = 0
        if parsed > 0:
            return parsed
        return DEFAULT_DEPENDENCY_MATERIALIZATION_SLA_SECONDS
    try:
        from hermes_cli.config import load_config

        configured = (load_config().get("kanban") or {}).get(
            "dependency_materialization_sla_seconds",
        )
        parsed = int(configured)
        if parsed > 0:
            return parsed
    except Exception:
        # Configuration is an operator convenience, not a reason to make a
        # worker unable to persist a lifecycle transition.  The broad catch
        # mirrors other best-effort config readers in this module.
        pass
    return DEFAULT_DEPENDENCY_MATERIALIZATION_SLA_SECONDS

def _has_sticky_block(conn: sqlite3.Connection, task_id: str) -> bool:
    """Return True when ``task_id`` is sticky-blocked by an explicit
    worker/operator ``kanban_block`` call or by the failure circuit breaker.

    A ``blocked`` status can come from two very different sources:

    * **Worker- or operator-initiated** — a worker called
      ``kanban_block(reason="review-required: ...")`` (or somebody ran
      ``hermes kanban block <id>``).  This is a deliberate handoff that
      should stay blocked until an operator unblocks it.  The block tool
      emits a ``"blocked"`` event row in ``task_events``.

    * **Circuit-breaker** — ``_record_task_failure`` tripped after
      repeated crashes / spawn failures / timeouts.  This emits
      ``"gave_up"``, not ``"blocked"``.  It must also be sticky:
      auto-promoting it on the next dispatcher tick re-enters the same
      broken worker path forever, especially for parentless root tasks.

    The cheapest signal is the most recent block-state event for the task.
    If the most recent one is ``"blocked"``, ``"operator_block_fenced"``, or
    ``"gave_up"``, the task is sticky and ``recompute_ready`` must *not*
    auto-promote it.  The operator fence is durable before process
    termination so a surviving worker can never be joined by a replacement.

    Returns ``False`` when there is no such event at all (e.g. direct DB
    manipulation of old rows), preserving the legacy auto-recover path
    only for rows with no durable block/failure event.
    """
    row = conn.execute(
        "SELECT kind FROM task_events "
        "WHERE task_id = ? AND kind IN "
        "('blocked', 'operator_block_fenced', 'unblocked', 'gave_up') "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return bool(row) and row["kind"] in {
        "blocked", "operator_block_fenced", "gave_up",
    }


def _awaiting_manual_promotion(conn: sqlite3.Connection, task_id: str) -> bool:
    """Return True when ``task_id`` is a decomposed entry child that is
    still awaiting a manual promotion.

    When a triage task is fanned out with ``auto_promote_children`` false,
    ``decompose_triage_task`` emits a ``"promotion_gated"`` event on each
    parent-free entry child and leaves it in ``todo``. Those entries must
    NOT be auto-promoted by ``recompute_ready`` on any tick — only an
    explicit operator ``promote_task`` (which emits ``"promoted_manual"``)
    releases them. This mirrors ``_has_sticky_block``'s most-recent-event
    signal so the gate is intrinsic to ``recompute_ready`` and holds
    regardless of which call site fired it.

    Returns False when there is no gate event (the overwhelming common
    case), preserving the legacy auto-promote path unchanged.
    """
    row = conn.execute(
        "SELECT kind FROM task_events "
        "WHERE task_id = ? AND kind IN ('promotion_gated', 'promoted_manual') "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return bool(row) and row["kind"] == "promotion_gated"


def recompute_ready(
    conn: sqlite3.Connection, failure_limit: int = None,
) -> int:
    """Promote ``todo`` tasks to ``ready`` when all parents are ``done`` or ``archived``.

    Returns the number of tasks promoted.  Safe to call inside or outside
    an existing transaction; it opens its own IMMEDIATE txn.

    ``blocked`` tasks are also considered for promotion (so a task
    blocked purely by a parent dependency unblocks itself when the
    parent completes), *except* in two cases:

    1. The most recent block event was a worker-initiated
       ``kanban_block`` — those stay blocked until an explicit
       ``kanban_unblock`` (#28712).

    2. The task's ``consecutive_failures`` has reached the effective
       failure limit.  This prevents infinite retry loops when a task
       repeatedly exhausts its iteration budget: without this guard the
       counter would reset on every recovery cycle and the circuit
       breaker could never trip (#35072).

    The effective failure limit resolves in the same order as the
    circuit breaker in ``_record_task_failure`` so the two never
    disagree about when a task is permanently blocked:

      1. per-task ``max_retries`` if set
      2. caller-supplied ``failure_limit`` (the dispatcher passes the
         ``kanban.failure_limit`` config value through ``dispatch_once``)
      3. ``DEFAULT_FAILURE_LIMIT``
    """
    if failure_limit is None:
        failure_limit = DEFAULT_FAILURE_LIMIT
    promoted = 0
    with write_txn(conn):
        todo_rows = conn.execute(
            "SELECT t.id, t.status, t.current_run_id, t.worker_pid, "
            "       t.worker_started_at, t.worker_pgid, t.worker_sid, "
            "       r.worker_pid AS run_worker_pid, "
            "       r.worker_started_at AS run_started_at, "
            "       r.worker_pgid AS run_pgid, r.worker_sid AS run_sid, "
            "       t.consecutive_failures, t.max_retries, t.policy_quarantined, "
            "       t.policy_invalidated, t.block_kind "
            "FROM tasks t LEFT JOIN task_runs r ON r.id = t.current_run_id "
            "WHERE t.status IN ('todo', 'blocked')"
        ).fetchall()
        for row in todo_rows:
            task_id = row["id"]
            cur_status = row["status"]
            if any(
                row[key] is not None
                for key in (
                    "worker_pid", "worker_started_at", "worker_pgid", "worker_sid",
                    "run_worker_pid", "run_started_at", "run_pgid", "run_sid",
                )
            ):
                # A malformed/manual row may still carry a live attempt's
                # persisted identity in either table. Never promote it into a
                # second dispatchable attempt; the operator/reclaim path must
                # resolve the ownership first. A run pointer with no identity
                # is legacy/manual state and claim_task may safely close it.
                continue
            if row["policy_quarantined"]:
                continue
            if cur_status == "blocked" and _has_sticky_block(conn, task_id):
                # Worker / operator asked for human review — do not
                # silently auto-recover.  ``unblock_task`` is the only
                # legitimate exit (it emits ``"unblocked"`` which flips
                # this predicate back).
                continue
            if cur_status == "todo" and _awaiting_manual_promotion(conn, task_id):
                # Decomposed entry child under manual-promote mode — only
                # an explicit promote_task releases it; never auto-promote.
                continue
            parents = conn.execute(
                "SELECT t.status, t.policy_quarantined, t.policy_invalidated FROM tasks t "
                "JOIN task_links l ON l.parent_id = t.id "
                "WHERE l.child_id = ?",
                (task_id,),
            ).fetchall()
            if cur_status == "todo" and row["block_kind"] == "dependency_pending":
                # BUILD-613: a dependency declaration with no materialized
                # parent is deliberately non-dispatchable.  The reconciler is
                # the only path that may turn this kernel-owned pending state
                # back into a normal dependency wait or a ready task.
                continue
            if all(_parent_is_satisfied(p) for p in parents):
                if cur_status == "blocked":
                    # Don't auto-recover tasks that have hit the
                    # circuit-breaker failure limit.  Without this
                    # guard, a task that repeatedly exhausts its
                    # iteration budget would cycle forever:
                    # block → auto-recover → respawn → budget
                    # exhausted → block → …  The counter must also
                    # be preserved so the breaker can accumulate
                    # across recovery cycles.
                    failures = int(row["consecutive_failures"] or 0)
                    task_limit = row["max_retries"]
                    effective_limit = (
                        int(task_limit) if task_limit is not None
                        else int(failure_limit)
                    )
                    if failures >= effective_limit:
                        continue
                    conn.execute(
                        "UPDATE tasks SET status = 'ready', block_kind = NULL, claim_lock = NULL, "
                        "claim_expires = NULL, worker_pid = NULL, "
                        "worker_started_at = NULL, worker_pgid = NULL, worker_sid = NULL "
                        "WHERE id = ? AND status = 'blocked'",
                        (task_id,),
                    )
                else:
                    conn.execute(
                        "UPDATE tasks SET status = 'ready', block_kind = NULL, claim_lock = NULL, "
                        "claim_expires = NULL, worker_pid = NULL, "
                        "worker_started_at = NULL, worker_pgid = NULL, worker_sid = NULL "
                        "WHERE id = ? AND status = 'todo'",
                        (task_id,),
                    )
                _append_event(conn, task_id, "promoted", None)
                promoted += 1
    return promoted


# ---------------------------------------------------------------------------
# Claim / complete / block
# ---------------------------------------------------------------------------

def claim_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    ttl_seconds: Optional[int] = None,
    claimer: Optional[str] = None,
) -> Optional[Task]:
    """Atomically transition ``ready -> running``.

    Returns the claimed ``Task`` on success, ``None`` if the task was
    already claimed (or is not in ``ready`` status).
    """
    now = int(time.time())
    lock = claimer or _claimer_id()
    expires = now + _resolve_claim_ttl_seconds(ttl_seconds)
    with write_txn(conn):
        quarantined = conn.execute(
            "SELECT policy_quarantined, block_kind FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if quarantined and quarantined["policy_quarantined"]:
            _append_event(conn, task_id, "claim_blocked", {"reason": "policy_quarantined"})
            return None
        if quarantined and quarantined["block_kind"] == "dependency_pending":
            conn.execute(
                "UPDATE tasks SET status = 'todo' WHERE id = ? AND status = 'ready'",
                (task_id,),
            )
            _append_event(
                conn, task_id, "claim_blocked",
                {"reason": "dependency_materialization_pending"},
            )
            return None
        # Enforcement and the immutable delivery snapshot must use the same
        # canonical resolver. A scope gate can exist before a ready task is
        # claimed even when it is not yet linked by ancestry.
        gate = get_delivery_architecture_gate(conn, task_id)
        if gate is not None and gate.enforcement_mode in ARCHITECTURE_GATE_ENFORCING_MODES:
            issued = conn.execute(
                "SELECT task_ids FROM architecture_graph_issuances WHERE gate_id = ?", (gate.gate_id,)
            ).fetchone()
            if issued is not None and task_id != gate.architect_task_id:
                try:
                    issued_ids = set(json.loads(issued["task_ids"]))
                except (TypeError, json.JSONDecodeError):
                    issued_ids = set()
                if task_id not in issued_ids:
                    conn.execute(
                        "UPDATE tasks SET status = 'todo' WHERE id = ? AND status = 'ready'", (task_id,)
                    )
                    _append_event(
                        conn, task_id, "claim_blocked",
                        {"reason": "architecture_graph_issued", "gate_id": gate.gate_id},
                    )
                    return None
        if _gate_requires_enforcement(gate):
            assert gate is not None
            if gate.architect_task_id != task_id:
                conn.execute(
                    "UPDATE tasks SET status = 'todo' WHERE id = ? AND status = 'ready'",
                    (task_id,),
                )
                _append_event(
                    conn,
                    task_id,
                    "claim_blocked",
                    {"reason": ARCHITECTURE_GATE_REASON_OPEN, "gate_id": gate.gate_id},
                )
                return None
        # Structural invariant: never transition ready -> running while any
        # parent is not yet 'done'. This is the single enforcement point
        # regardless of which writer (create_task, link_tasks, unblock_task,
        # release_stale_claims, manual SQL) set status='ready'. If a racy
        # writer promoted a task with undone parents, demote it back to
        # 'todo' here — recompute_ready will re-promote when the parents
        # actually finish. See RCA at
        # kanban/boards/cookai/workspaces/t_a6acd07d/root-cause.md.
        parent_rows = conn.execute(
            "SELECT p.status, p.policy_quarantined, p.policy_invalidated "
            "FROM task_links l JOIN tasks p ON p.id = l.parent_id "
            "WHERE l.child_id = ?",
            (task_id,),
        ).fetchall()
        undone = any(not _parent_is_satisfied(parent) for parent in parent_rows)
        if undone:
            conn.execute(
                "UPDATE tasks SET status = 'todo' "
                "WHERE id = ? AND status = 'ready'",
                (task_id,),
            )
            _append_event(
                conn, task_id, "claim_rejected",
                {"reason": "parents_not_done"},
            )
            return None
        # Defensive: if a prior run somehow leaked (invariant violation from
        # an unknown code path), close it as 'reclaimed' so we don't strand
        # it when the CAS resets the pointer below. No-op when the invariant
        # holds (the common case).
        stale = conn.execute(
            "SELECT t.current_run_id, t.worker_pid AS task_worker_pid, "
            "       t.worker_started_at AS task_started_at, "
            "       t.worker_pgid AS task_pgid, t.worker_sid AS task_sid, "
            "       r.worker_pid AS run_worker_pid, "
            "       r.worker_started_at AS run_started_at, "
            "       r.worker_pgid AS run_pgid, r.worker_sid AS run_sid "
            "FROM tasks t LEFT JOIN task_runs r ON r.id = t.current_run_id "
            "WHERE t.id = ? AND t.status = 'ready'",
            (task_id,),
        ).fetchone()
        if stale and any(
            stale[key] is not None
            for key in (
                "task_worker_pid", "task_started_at", "task_pgid", "task_sid",
                "run_worker_pid", "run_started_at", "run_pgid", "run_sid",
            )
        ):
            # A ready row with any persisted worker identity is malformed or
            # legacy/manual state. Do not close its run or clear its identity
            # merely to make it claimable; reclaim/inspect it first.
            _append_event(
                conn, task_id, "claim_rejected",
                {"reason": "worker_identity_present"},
            )
            return None
        if stale and stale["current_run_id"]:
            conn.execute(
                """
                UPDATE task_runs
                   SET status = 'reclaimed', outcome = 'reclaimed',
                       summary = COALESCE(summary, 'invariant recovery on re-claim'),
                       ended_at = ?,
                       claim_lock = NULL, claim_expires = NULL,
                       worker_pid = NULL, worker_started_at = NULL,
                       worker_pgid = NULL, worker_sid = NULL
                 WHERE id = ? AND ended_at IS NULL
                """,
                (now, int(stale["current_run_id"])),
            )
        # ── Workspace collision guard ────────────────────────────────
        # Reject the claim when another *running* task already owns the
        # same non-scratch workspace.  Two workers editing the same dir:
        # or worktree path simultaneously corrupt each other's work.
        # Scratch workspaces are exempt — each task gets its own tmp dir
        # and cannot collide.
        ws_row = conn.execute(
            "SELECT workspace_path, workspace_kind FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if ws_row and ws_row["workspace_path"] and ws_row["workspace_kind"] \
           and ws_row["workspace_kind"] != "scratch":
            collision = conn.execute(
                "SELECT id FROM tasks "
                "WHERE status = 'running' "
                "  AND workspace_kind = ? "
                "  AND workspace_path = ? "
                "  AND id != ? "
                "LIMIT 1",
                (ws_row["workspace_kind"], ws_row["workspace_path"], task_id),
            ).fetchone()
            if collision:
                _append_event(
                    conn, task_id, "claim_rejected",
                    {
                        "reason": "workspace_collision",
                        "conflict_with": collision["id"],
                        "workspace_kind": ws_row["workspace_kind"],
                        "workspace_path": ws_row["workspace_path"],
                    },
                )
                return None
        cur = conn.execute(
            """
            UPDATE tasks
               SET status        = 'running',
                   claim_lock    = ?,
                   claim_expires = ?,
                   worker_pid    = NULL,
                   worker_started_at = NULL,
                   worker_pgid   = NULL,
                   worker_sid    = NULL,
                   started_at    = COALESCE(started_at, ?)
             WHERE id = ?
               AND status = 'ready'
               AND claim_lock IS NULL
            """,
            (lock, expires, now, task_id),
        )
        if cur.rowcount != 1:
            return None
        # Look up the current task row so we can populate the run with
        # its assignee / step / runtime cap.
        trow = conn.execute(
            "SELECT assignee, max_runtime_seconds, current_step_key, "
            "model_override, model_provider_override, model_reasoning_effort, toolsets "
            "FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        run_spec_json = json.dumps(
            _build_run_spec(trow, architecture_gate=gate),
            ensure_ascii=False,
            sort_keys=True,
        )
        run_cur = conn.execute(
            """
            INSERT INTO task_runs (
                task_id, profile, step_key, status,
                claim_lock, claim_expires, max_runtime_seconds,
                last_semantic_progress_at, run_spec_json, started_at
            ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                trow["assignee"] if trow else None,
                trow["current_step_key"] if trow else None,
                lock,
                expires,
                trow["max_runtime_seconds"] if trow else None,
                now,
                run_spec_json,
                now,
            ),
        )
        run_id = run_cur.lastrowid
        conn.execute(
            "UPDATE tasks SET current_run_id = ? WHERE id = ?",
            (run_id, task_id),
        )
        _append_event(
            conn, task_id, "claimed",
            {"lock": lock, "expires": expires, "run_id": run_id},
            run_id=run_id,
        )
        claimed = get_task(conn, task_id)
    _fire_kanban_lifecycle_hook(
        "kanban_task_claimed",
        task_id,
        board=get_current_board(),
        assignee=claimed.assignee if claimed else None,
        run_id=run_id,
    )
    return claimed


def claim_review_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    ttl_seconds: Optional[int] = None,
    claimer: Optional[str] = None,
) -> Optional[Task]:
    """Atomically transition ``review -> running``.

    Returns the claimed ``Task`` on success, ``None`` if the task was
    already claimed (or is not in ``review`` status).

    Unlike ``claim_task`` (which handles ``ready -> running``), this
    does NOT check parent dependencies — the task already passed that
    gate on its original ``todo -> ready -> running`` transition.

    Creates a new run entry so the review agent's lifecycle is tracked
    independently from the original worker run.
    """
    now = int(time.time())
    lock = claimer or _claimer_id()
    expires = now + _resolve_claim_ttl_seconds(ttl_seconds)
    with write_txn(conn):
        if not conn.execute(
            "SELECT 1 FROM tasks WHERE id = ?", (task_id,)
        ).fetchone():
            return None
        try:
            review_binding = _ensure_review_artifact_binding_in_txn(
                conn,
                task_id,
                now=now,
                require_if_referenced=True,
            )
            if review_binding is not None:
                _verify_review_artifact_binding(conn, review_binding)
        except ReviewArtifactError as exc:
            cur = conn.execute(
                "UPDATE tasks SET status = 'blocked', block_kind = 'needs_input', "
                "claim_lock = NULL, claim_expires = NULL, current_run_id = NULL "
                "WHERE id = ? AND status = 'review'",
                (task_id,),
            )
            if cur.rowcount == 1:
                _append_event(
                    conn,
                    task_id,
                    "artifact_selection_required",
                    {"reason": str(exc)},
                )
            _append_event(
                conn,
                task_id,
                "claim_blocked",
                {"reason": "review_artifact_unavailable", "detail": str(exc)},
            )
            return None
        gate = get_delivery_architecture_gate(conn, task_id)
        if gate is not None and gate.enforcement_mode in ARCHITECTURE_GATE_ENFORCING_MODES:
            issued = conn.execute(
                "SELECT task_ids FROM architecture_graph_issuances WHERE gate_id = ?",
                (gate.gate_id,),
            ).fetchone()
            if issued is not None and task_id != gate.architect_task_id:
                try:
                    issued_ids = set(json.loads(issued["task_ids"]))
                except (TypeError, json.JSONDecodeError):
                    issued_ids = set()
                if task_id not in issued_ids:
                    _append_event(
                        conn,
                        task_id,
                        "claim_blocked",
                        {
                            "reason": "architecture_graph_issued",
                            "gate_id": gate.gate_id,
                        },
                    )
                    return None
            if _gate_requires_enforcement(gate) and gate.architect_task_id != task_id:
                _append_event(
                    conn,
                    task_id,
                    "claim_blocked",
                    {
                        "reason": ARCHITECTURE_GATE_REASON_OPEN,
                        "gate_id": gate.gate_id,
                    },
                )
                return None
        identity = conn.execute(
            "SELECT worker_pid, worker_started_at, worker_pgid, worker_sid "
            "FROM tasks WHERE id = ? AND status = 'review'",
            (task_id,),
        ).fetchone()
        if identity and any(identity[key] is not None for key in identity.keys()):
            _append_event(
                conn, task_id, "claim_rejected",
                {"reason": "worker_identity_present"},
            )
            return None
        cur = conn.execute(
            """
            UPDATE tasks
               SET status        = 'running',
                   claim_lock    = ?,
                   claim_expires = ?,
                   worker_pid    = NULL,
                   worker_started_at = NULL,
                   worker_pgid   = NULL,
                   worker_sid    = NULL,
                   started_at    = COALESCE(started_at, ?)
             WHERE id = ?
               AND status = 'review'
               AND claim_lock IS NULL
            """,
            (lock, expires, now, task_id),
        )
        if cur.rowcount != 1:
            return None
        trow = conn.execute(
            "SELECT assignee, max_runtime_seconds, current_step_key, "
            "model_override, model_provider_override, model_reasoning_effort, toolsets "
            "FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        run_spec_json = json.dumps(
            _build_run_spec(trow, architecture_gate=gate),
            ensure_ascii=False,
            sort_keys=True,
        )
        run_cur = conn.execute(
            """
            INSERT INTO task_runs (
                task_id, profile, step_key, status,
                claim_lock, claim_expires, max_runtime_seconds,
                last_semantic_progress_at, run_spec_json, started_at
            ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                trow["assignee"] if trow else None,
                trow["current_step_key"] if trow else None,
                lock,
                expires,
                trow["max_runtime_seconds"] if trow else None,
                now,
                run_spec_json,
                now,
            ),
        )
        run_id = run_cur.lastrowid
        conn.execute(
            "UPDATE tasks SET current_run_id = ? WHERE id = ?",
            (run_id, task_id),
        )
        _append_event(
            conn, task_id, "claimed",
            {"lock": lock, "expires": expires, "run_id": run_id,
             "source_status": "review"},
            run_id=run_id,
        )
        return get_task(conn, task_id)


def heartbeat_claim(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    ttl_seconds: Optional[int] = None,
    claimer: Optional[str] = None,
) -> bool:
    """Extend a running claim.  Returns True if we still own it.

    Workers that know they'll exceed 15 minutes should call this every
    few minutes to keep ownership.
    """
    expires = int(time.time()) + _resolve_claim_ttl_seconds(ttl_seconds)
    lock = claimer or _claimer_id()
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET claim_expires = ? "
            "WHERE id = ? AND status = 'running' AND claim_lock = ?",
            (expires, task_id, lock),
        )
        if cur.rowcount == 1:
            run_id = _current_run_id(conn, task_id)
            if run_id is not None:
                conn.execute(
                    "UPDATE task_runs SET claim_expires = ? WHERE id = ?",
                    (expires, run_id),
                )
            return True
        return False


def release_stale_claims(
    conn: sqlite3.Connection,
    *,
    signal_fn=None,
) -> int:
    """Reset any ``running`` task whose claim has expired.

    A stale-by-TTL claim whose host-local worker PID is still alive is
    *extended* (with a ``claim_extended`` event) instead of being
    reclaimed. Reclaiming a live worker mid-flight produces the spawn-
    then-immediately-reclaim loop seen on slow models that spend longer
    than ``DEFAULT_CLAIM_TTL_SECONDS`` inside a single tool-free LLM
    call (#23025): no tool calls means no ``kanban_heartbeat``, even
    though the subprocess is healthy.

    Backstop (#29747 gap 3): if the worker's PID is still alive but its
    semantic-progress clock is stale by more than
    ``DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS`` (1h), the worker has
    been making no observable progress and we reclaim anyway — even if
    ``_pid_alive`` is still true. This catches the wedged-in-a-logic-loop
    case where the process is technically running but accomplishing
    nothing. Process and transport activity remain observable but cannot hide
    a semantically stalled run. ``enforce_max_runtime`` and
    ``detect_crashed_workers`` remain the upper bounds for genuinely wedged or
    dead workers.

    Returns the number of stale claims actually reclaimed (live-pid
    extensions don't count). Safe to call often.
    """
    now = int(time.time())
    reclaimed = 0
    host_prefix = f"{_claimer_id().split(':', 1)[0]}:"
    stale = conn.execute(
        "SELECT t.id, t.current_run_id, t.claim_lock, t.worker_pid, "
        "       t.worker_started_at, t.worker_pgid, t.worker_sid, t.claim_expires, "
        "       t.last_heartbeat_at, r.last_semantic_progress_at, "
        "       COALESCE(r.last_semantic_progress_at, "
        "                t.last_heartbeat_at) AS progress_at "
        "FROM tasks t "
        "LEFT JOIN task_runs r ON r.id = t.current_run_id "
        "WHERE t.status = 'running' AND t.claim_expires IS NOT NULL "
        "  AND t.claim_expires < ?",
        (now,),
    ).fetchall()
    for row in stale:
        lock = row["claim_lock"] or ""
        host_local = lock.startswith(host_prefix)
        if host_local and row["worker_pid"] is None:
            _log_orphan_worker_canary(
                task_id=row["id"],
                run_id=row["current_run_id"],
                claim_lock=row["claim_lock"],
            )
        hb = row["progress_at"]
        # Semantic-progress staleness is the backstop for live wrappers.
        # Legacy runs have no typed activity clock and fall back to the old
        # task heartbeat until they are retried under the new protocol.
        heartbeat_stale = (
            hb is not None
            and (now - int(hb)) > DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS
        )
        live_pid = bool(
            host_local and row["worker_pid"] and _pid_alive(row["worker_pid"])
        )
        extension_identity: Optional[bool] = True
        if live_pid:
            extension_identity = _attest_reclaim_process_identity(
                int(row["worker_pid"]),
                str(row["claim_lock"]),
                worker_started_at=row["worker_started_at"],
                worker_pgid=row["worker_pgid"],
                worker_sid=row["worker_sid"],
                task_id=row["id"],
                run_id=row["current_run_id"],
            )
        if (
            live_pid
            and extension_identity is not False
            and not heartbeat_stale
        ):
            new_expires = now + _resolve_claim_ttl_seconds()
            with write_txn(conn):
                cur = conn.execute(
                    "UPDATE tasks SET claim_expires = ? "
                    "WHERE id = ? AND status = 'running' "
                    "  AND claim_lock IS ? "
                    "  AND claim_expires IS NOT NULL "
                    "  AND claim_expires < ?",
                    (new_expires, row["id"], row["claim_lock"], now),
                )
                if cur.rowcount != 1:
                    continue
                run_id = _current_run_id(conn, row["id"])
                if run_id is not None:
                    conn.execute(
                        "UPDATE task_runs SET claim_expires = ? WHERE id = ?",
                        (new_expires, run_id),
                    )
                _append_event(
                    conn, row["id"], "claim_extended",
                    {
                        "reason": (
                            "pid_alive"
                            if extension_identity is True
                            else "pid_identity_unverifiable"
                        ),
                        "worker_pid": int(row["worker_pid"]),
                        "claim_lock": row["claim_lock"],
                        "claim_expires_was": int(row["claim_expires"]),
                        "claim_expires_now": new_expires,
                        "last_heartbeat_at": (
                            int(row["last_heartbeat_at"])
                            if row["last_heartbeat_at"] is not None
                            else None
                        ),
                    },
                    run_id=run_id,
                )
            continue

        termination = _terminate_worker_for_task(
            row["worker_pid"],
            row["claim_lock"],
            task_id=row["id"],
            run_id=row["current_run_id"],
            worker_started_at=row["worker_started_at"],
            worker_pgid=row["worker_pgid"],
            worker_sid=row["worker_sid"],
            signal_fn=signal_fn,
        )
        # Never release a claim while our own worker is still alive: that would
        # spawn a duplicate beside it. Hold the claim and retry next tick.
        if _worker_survived_termination(termination):
            _defer_reclaim_for_live_worker(
                conn, row["id"], row["claim_lock"], now, termination,
                reason="ttl_expired_worker_alive",
            )
            continue
        with write_txn(conn):
            cur = conn.execute(
                "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL, "
                "worker_started_at = NULL, worker_pgid = NULL, worker_sid = NULL "
                "WHERE id = ? AND status = 'running' AND policy_quarantined = 0 AND claim_lock IS ? "
                "AND claim_expires IS NOT NULL AND claim_expires < ?",
                (row["id"], row["claim_lock"], now),
            )
            if cur.rowcount != 1:
                continue
            _invalidate_architect_gate_for_mutation(
                conn, row["id"], reason="architect_stale_reclaimed",
            )
            run_id = _end_run(
                conn, row["id"],
                outcome="reclaimed", status="reclaimed",
                error=f"stale_lock={row['claim_lock']}",
                metadata=termination,
            )
            payload = {
                "stale_lock": row["claim_lock"],
                "worker_pid": (
                    int(row["worker_pid"])
                    if row["worker_pid"] is not None else None
                ),
                "claim_expires": int(row["claim_expires"]),
                "last_heartbeat_at": (
                    int(row["last_heartbeat_at"])
                    if row["last_heartbeat_at"] is not None else None
                ),
                "now": now,
                "host_local": host_local,
                "heartbeat_stale": bool(heartbeat_stale),
            }
            payload.update(termination)
            _append_event(
                conn, row["id"], "reclaimed",
                payload,
                run_id=run_id,
            )
            reclaimed += 1
    return reclaimed


def reclaim_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    reason: Optional[str] = None,
    signal_fn=None,
) -> bool:
    """Operator-driven reclaim: release the claim and reset to ``ready``.

    Unlike :func:`release_stale_claims` which only acts on tasks whose
    ``claim_expires`` has passed, this function reclaims immediately
    regardless of TTL. Intended for the dashboard/CLI recovery flow
    when an operator wants to abort a running worker without waiting
    for the TTL to expire (e.g. after seeing a hallucination warning).

    Returns True if a reclaim happened, False if the task isn't in a
    reclaimable state (not running, or doesn't exist).
    """
    row = conn.execute(
        "SELECT status, current_run_id, claim_lock, worker_pid, "
        "worker_started_at, worker_pgid, worker_sid FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if not row:
        return False
    if row["status"] != "running" and row["claim_lock"] is None:
        # Nothing to reclaim — already ready / blocked / done.
        return False
    prev_lock = row["claim_lock"]
    host_prefix = f"{_claimer_id().split(':', 1)[0]}:"
    if (
        row["worker_pid"] is None
        and isinstance(prev_lock, str)
        and prev_lock.startswith(host_prefix)
    ):
        _log_orphan_worker_canary(
            task_id=task_id,
            run_id=row["current_run_id"],
            claim_lock=prev_lock,
        )
    termination = _terminate_worker_for_task(
        row["worker_pid"],
        prev_lock,
        task_id=task_id,
        run_id=row["current_run_id"],
        worker_started_at=row["worker_started_at"],
        worker_pgid=row["worker_pgid"],
        worker_sid=row["worker_sid"],
        signal_fn=signal_fn,
    )
    if _worker_survived_termination(termination):
        _log.warning(
            "manual reclaim deferred for task=%s: worker identity could not "
            "be safely terminated",
            task_id,
        )
        _defer_reclaim_for_live_worker(
            conn,
            task_id,
            prev_lock,
            int(time.time()),
            termination,
            reason="manual_reclaim_identity_unverifiable",
        )
        return False
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
            "claim_expires = NULL, worker_pid = NULL, "
            "worker_started_at = NULL, worker_pgid = NULL, worker_sid = NULL "
            "WHERE id = ? AND status IN ('running', 'ready', 'blocked') "
            "AND claim_lock IS ?",
            (task_id, prev_lock),
        )
        if cur.rowcount != 1:
            return False
        _invalidate_architect_gate_for_mutation(
            conn, task_id, reason="architect_manual_reclaim",
        )
        run_id = _end_run(
            conn, task_id,
            outcome="reclaimed", status="reclaimed",
            error=(
                f"manual_reclaim: {reason}" if reason
                else f"manual_reclaim lock={prev_lock}"
            ),
            metadata=termination,
        )
        payload = {
            "manual": True,
            "reason": reason,
            "prev_lock": prev_lock,
        }
        payload.update(termination)
        _append_event(
            conn, task_id, "reclaimed",
            payload,
            run_id=run_id,
        )
    # Operator intervention — they've looked at the task, so the
    # consecutive-failures counter is now stale. Give the next retry
    # a fresh budget. (_clear_failure_counter opens its own write_txn,
    # so it runs after the enclosing one commits.)
    _clear_failure_counter(conn, task_id)
    return True


def reassign_task(
    conn: sqlite3.Connection,
    task_id: str,
    profile: Optional[str],
    *,
    reclaim_first: bool = False,
    reason: Optional[str] = None,
) -> bool:
    """Reassign a task, optionally reclaiming a stuck running worker first.

    This is the recovery path for "this profile's model is broken, try
    a different one". If ``reclaim_first`` is True, any active claim is
    released (via :func:`reclaim_task`) before the reassign happens;
    otherwise the function refuses to reassign a currently-running task
    and returns False (caller can retry with ``reclaim_first=True``).

    Returns True if the reassign landed. ``profile`` may be ``None`` to
    unassign entirely.
    """
    if reclaim_first:
        # Safe to call even if nothing to reclaim.
        reclaim_task(conn, task_id, reason=reason or "reassign")
    # assign_task handles its own txn + the still-running guard.
    try:
        return assign_task(conn, task_id, profile)
    except RuntimeError:
        # Task is still running and reclaim_first was False; caller
        # needs to decide whether to retry with reclaim.
        return False


def _verify_created_cards(
    conn: sqlite3.Connection,
    completing_task_id: str,
    claimed_ids: Iterable[str],
) -> tuple[list[str], list[str]]:
    """Partition ``claimed_ids`` into (verified, phantom).

    A card is "verified" iff a row exists in ``tasks`` AND at least one
    of the following holds:

    * ``created_by`` matches the completing task's ``assignee`` profile
      (the common case: worker A spawns a card via ``kanban_create``,
      which stamps ``created_by=A``).
    * ``created_by`` matches the completing task's id (edge case where
      a worker passed its own task id as the ``created_by`` value).
    * The card is linked as a ``task_links.child`` of the completing
      task — i.e. the worker explicitly called ``kanban_create`` with
      ``parents=[<current_task>]``. This accepts cards created through
      the dashboard/CLI by a different principal but then attached to
      the completing task by the worker.

    ``phantom`` returns ids that either don't exist at all, or exist
    but don't satisfy any of the three trust conditions. The caller
    decides what to do with each bucket; this helper never mutates.
    """
    claimed = [str(x).strip() for x in (claimed_ids or []) if str(x).strip()]
    if not claimed:
        return [], []
    # Dedupe while preserving order.
    seen: set[str] = set()
    ordered: list[str] = []
    for cid in claimed:
        if cid not in seen:
            seen.add(cid)
            ordered.append(cid)

    row = conn.execute(
        "SELECT assignee FROM tasks WHERE id = ?", (completing_task_id,),
    ).fetchone()
    if row is None:
        # Completing task not found — nothing resolves.
        return [], ordered
    completing_assignee = row["assignee"]

    # Batch-fetch existence + created_by in one query.
    placeholders = ",".join(["?"] * len(ordered))
    rows = conn.execute(
        f"SELECT id, created_by FROM tasks WHERE id IN ({placeholders})",
        tuple(ordered),
    ).fetchall()
    found = {r["id"]: r["created_by"] for r in rows}

    # Pull the set of cards linked as children of the completing task.
    # Cheap: one query, indexed on parent_id.
    linked_children: set[str] = set(child_ids(conn, completing_task_id))

    verified: list[str] = []
    phantom: list[str] = []
    for cid in ordered:
        created_by = found.get(cid)
        if created_by is None:
            phantom.append(cid)
            continue
        # Accept if any of the three trust conditions holds.
        if completing_assignee and created_by == completing_assignee:
            verified.append(cid)
        elif created_by == completing_task_id:
            verified.append(cid)
        elif cid in linked_children:
            verified.append(cid)
        else:
            phantom.append(cid)
    return verified, phantom


# Task-id pattern used both by ``kanban_create`` (``t_<12 hex>``) and
# ``_new_task_id`` below. Kept permissive on length for forward compat:
# accept 8+ hex chars after the ``t_`` prefix.
_TASK_ID_PROSE_RE = re.compile(r"\bt_[a-f0-9]{8,}\b")


def _scan_prose_for_phantom_ids(
    conn: sqlite3.Connection,
    text: str,
) -> list[str]:
    """Regex-scan free-form text for ``t_<hex>`` references; return the
    ones that don't exist in ``tasks``.

    Used as a non-blocking advisory check on completion summaries. An
    empty return means "no suspicious references found" — either the
    text had no IDs at all, or every ID it mentioned resolves to a real
    task. Duplicates are deduped.
    """
    if not text:
        return []
    matches = _TASK_ID_PROSE_RE.findall(text)
    if not matches:
        return []
    # Dedupe preserving order.
    seen: set[str] = set()
    unique: list[str] = []
    for m in matches:
        if m not in seen:
            seen.add(m)
            unique.append(m)
    placeholders = ",".join(["?"] * len(unique))
    rows = conn.execute(
        f"SELECT id FROM tasks WHERE id IN ({placeholders})",
        tuple(unique),
    ).fetchall()
    existing = {r["id"] for r in rows}
    return [m for m in unique if m not in existing]


class HallucinatedCardsError(ValueError):
    """Raised by ``complete_task`` when ``created_cards`` contains ids
    that don't exist or weren't created by the completing worker.

    The phantom list is attached as ``.phantom`` for callers that want
    structured access. Kept as ``ValueError`` subclass so existing
    tool-error handlers treat it as a recoverable user error.
    """

    def __init__(self, phantom: list[str], completing_task_id: str):
        self.phantom = list(phantom)
        self.completing_task_id = completing_task_id
        super().__init__(
            f"completion blocked: claimed created_cards that do not exist "
            f"or were not created by this worker: {', '.join(phantom)}"
        )


class ArtifactPreservationError(RuntimeError):
    """Raised when a declared scratch deliverable cannot be preserved."""


PUBLICATION_READBACK_TIMEOUT_SECONDS = 30


def _read_publication_contract(
    conn: sqlite3.Connection,
    task_id: str,
) -> Optional[_PublicationContract]:
    """Read the publication contract without opening a write transaction."""
    row = conn.execute(
        "SELECT publication_expected_sha, publication_remote, publication_ref, "
        "workspace_path FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    return _PublicationContract(
        expected_sha=row["publication_expected_sha"],
        remote=row["publication_remote"],
        ref=row["publication_ref"],
        workspace_path=row["workspace_path"],
    )


def _publication_contract_payload(
    contract: _PublicationContract,
) -> dict[str, Any]:
    """Return a stable, JSON-serializable view of a contract snapshot."""
    return {
        "expected_sha": contract.expected_sha,
        "remote": contract.remote,
        "ref": contract.ref,
        "workspace_path": contract.workspace_path,
    }


def _read_publication_remote_ref(contract: _PublicationContract) -> dict[str, Any]:
    """Read the recorded remote ref from the publication card's checkout.

    This is intentionally a read-only ``git ls-remote``. A push report or a
    worker-supplied metadata flag never reaches this function; only the remote
    object database's ref value can satisfy the completion gate.
    """
    expected = str(contract.expected_sha or "").strip().lower()
    remote = str(contract.remote or "").strip()
    ref = str(contract.ref or "").strip()
    workspace = str(contract.workspace_path or "").strip()
    details: dict[str, Any] = {
        "expected_sha": expected or None,
        "remote": remote or None,
        "remote_ref": ref or None,
        "workspace_path": workspace or None,
        "observed_sha": None,
        "verified": False,
    }
    if not expected or not remote or not ref:
        details["reason"] = "publication contract is incomplete"
        return details
    if not workspace:
        details["reason"] = "publication workspace is missing"
        return details
    workspace_path = Path(workspace).expanduser()
    if not workspace_path.is_dir():
        details["reason"] = "publication workspace does not exist"
        return details
    try:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(workspace_path),
                "ls-remote",
                "--exit-code",
                "--refs",
                remote,
                ref,
            ],
            capture_output=True,
            text=True,
            timeout=PUBLICATION_READBACK_TIMEOUT_SECONDS,
            check=False,
            stdin=subprocess.DEVNULL,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        details["reason"] = f"git readback unavailable: {type(exc).__name__}"
        return details
    if completed.returncode != 0:
        details["reason"] = "git ls-remote did not find the target ref"
        return details
    for line in (completed.stdout or "").splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[1] == ref:
            details["observed_sha"] = fields[0].lower()
            break
    if details["observed_sha"] is None:
        details["reason"] = "git readback returned no exact target ref"
        return details
    details["verified"] = details["observed_sha"] == expected
    if not details["verified"]:
        details["reason"] = "remote ref SHA does not match expected SHA"
    return details


def _normalize_review_outputs(
    review_outputs: Optional[Iterable[dict[str, Any]]],
) -> dict[str, int]:
    """Validate the completion's exact attachment-selection manifest."""
    if review_outputs is None:
        return {}
    if isinstance(review_outputs, (str, bytes, dict)):
        raise ReviewArtifactError("review_outputs must be an array of objects")
    try:
        items = list(review_outputs)
    except TypeError as exc:
        raise ReviewArtifactError("review_outputs must be an array of objects") from exc
    selected: dict[str, int] = {}
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ReviewArtifactError(f"review_outputs[{index}] must be an object")
        raw_review_id = item.get("review_task_id")
        review_id = raw_review_id.strip() if isinstance(raw_review_id, str) else ""
        if not review_id:
            raise ReviewArtifactError(
                f"review_outputs[{index}].review_task_id is required"
            )
        if "attachment_id" not in item or item.get("attachment_id") is None:
            raise ReviewArtifactError(
                f"review_outputs[{index}].attachment_id is required"
            )
        if type(item["attachment_id"]) is not int:
            raise ReviewArtifactError(
                f"review_outputs[{index}].attachment_id must be an integer"
            )
        attachment_id = item["attachment_id"]
        if attachment_id <= 0:
            raise ReviewArtifactError(
                f"review_outputs[{index}].attachment_id must be positive"
            )
        if review_id in selected:
            raise ReviewArtifactError(
                f"review_outputs selects review {review_id} more than once"
            )
        selected[review_id] = attachment_id
    return selected


def _current_rework_reviews_for_fix(
    conn: sqlite3.Connection,
    fix_task_id: str,
) -> list[tuple[str, int, dict[str, Any]]]:
    """Return reviews whose latest rework request still names this fix."""
    rows = conn.execute(
        "SELECT id, task_id, payload FROM task_events "
        "WHERE kind = 'rework_requested' ORDER BY id DESC"
    ).fetchall()
    seen_reviews: set[str] = set()
    matches: list[tuple[str, int, dict[str, Any]]] = []
    for row in rows:
        review_id = str(row["task_id"])
        if review_id in seen_reviews:
            continue
        seen_reviews.add(review_id)
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        if str(payload.get("fix_task_id") or "").strip() != fix_task_id:
            continue
        if payload.get("escalated") or payload.get("fix_action") == "escalated":
            continue
        matches.append((review_id, int(row["id"]), payload))
    matches.sort(key=lambda item: (item[0], item[1]))
    return matches


def complete_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    result: Optional[str] = None,
    summary: Optional[str] = None,
    metadata: Optional[dict] = None,
    created_cards: Optional[Iterable[str]] = None,
    review_outputs: Optional[Iterable[dict[str, Any]]] = None,
    expected_run_id: Optional[int] = None,
) -> bool:
    """Transition ``running|ready -> done`` and record ``result``.

    Accepts a task that is merely ``ready`` too, so a manual CLI
    completion (``hermes kanban complete <id>``) works without requiring
    a claim/start/complete sequence.

    ``summary`` and ``metadata`` are stored on the closing run (if any)
    and surfaced to downstream children via :func:`build_worker_context`.
    When ``summary`` is omitted we fall back to ``result`` so single-run
    callers do not have to pass both. ``metadata`` is a free-form dict
    (e.g. ``{"changed_files": [...], "tests_run": [...]}``) — workers
    are encouraged to use it for structured handoff facts.

    ``created_cards`` is an optional list of task ids the completing
    worker claims to have created. Each id is verified against
    ``tasks.created_by``. If any id is phantom (does not exist or was
    not created by this worker's assignee profile), completion is blocked
    with a ``HallucinatedCardsError`` and a
    ``completion_blocked_hallucination`` event is emitted so the rejected
    attempt is auditable. When all ids verify, they are recorded on the
    ``completed`` event payload.

    ``review_outputs`` selects exactly one attachment for each current
    artifact-bound review rework. The selection and byte-level binding happen
    in this same transaction before the fix completion wakes its reviewer.

    After a successful completion, ``summary`` and ``result`` are scanned
    for prose references like ``t_deadbeefcafe`` that do not resolve.
    Any suspected phantom references are recorded as a
    ``suspected_hallucinated_references`` event. This pass is advisory
    and never blocks.
    """
    now = int(time.time())
    normalized_review_outputs = _normalize_review_outputs(review_outputs)
    delivery_withheld = False
    delivery_gate_id: Optional[str] = None
    delivery_digest: Optional[str] = None
    publication_verification: Optional[dict[str, Any]] = None

    # Gate: verify created_cards BEFORE the main write txn. A rejected
    # completion still needs an auditable event, so we emit it in a
    # tiny dedicated txn, then raise. The caller is responsible for
    # surfacing HallucinatedCardsError to the worker; this function
    # never mutates task state on a phantom-card rejection.
    if created_cards:
        verified_cards, phantom_cards = _verify_created_cards(
            conn, task_id, created_cards
        )
        if phantom_cards:
            with write_txn(conn):
                _append_event(
                    conn, task_id, "completion_blocked_hallucination",
                    {
                        "phantom_cards": phantom_cards,
                        "verified_cards": verified_cards,
                        "summary_preview": (
                            (summary or result or "").strip().splitlines()[0][:200]
                            if (summary or result)
                            else None
                        ),
                    },
                )
            raise HallucinatedCardsError(phantom_cards, task_id)
    else:
        verified_cards = []

    metadata = _merge_completion_prose_artifacts(
        conn, task_id, metadata, summary=summary, result=result,
    )

    # Publication readback is deliberately outside the write transaction.
    # The contract is immutable creation-time evidence, so the short write
    # transaction below only needs to confirm that the fields it re-reads are
    # byte-identical to this snapshot before applying the status CAS.
    publication_contract = _read_publication_contract(conn, task_id)
    if publication_contract is not None and publication_contract.has_publication_fields:
        publication_verification = _read_publication_remote_ref(publication_contract)

    with write_txn(conn):
        quarantined = conn.execute(
            "SELECT policy_quarantined FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if quarantined and quarantined["policy_quarantined"]:
            _append_event(conn, task_id, "completion_blocked", {"reason": "policy_quarantined"})
            return False
        critical_blockers = open_critical_continuation_blockers(conn, task_id)
        if critical_blockers:
            _append_event(
                conn,
                task_id,
                "completion_blocked",
                {
                    "reason": "open_critical_continuation_blockers",
                    "blockers": [
                        {
                            "id": item.id,
                            "severity": item.severity,
                            "title": item.title,
                        }
                        for item in critical_blockers
                    ],
                },
                run_id=_current_run_id(conn, task_id),
            )
            return False
        gate = get_delivery_architecture_gate(conn, task_id)
        if expected_run_id is not None and (
            gate is None or gate.architect_task_id != task_id
        ):
            run_spec = get_run_spec(
                conn,
                int(expected_run_id),
                task_id=task_id,
                require_current=True,
            )
            try:
                claimed_delivery = validate_delivery_policy_snapshot(
                    run_spec.get("delivery_policy")
                    if isinstance(run_spec, dict)
                    else None
                )
            except ValueError:
                _append_event(
                    conn,
                    task_id,
                    "completion_blocked",
                    {"reason": "invalid_delivery_attestation"},
                )
                return False
            current_delivery = _delivery_policy_snapshot(gate)
            if claimed_delivery != current_delivery:
                _append_event(
                    conn,
                    task_id,
                    "completion_blocked",
                    {
                        "reason": "delivery_authority_epoch_mismatch",
                        "claimed_gate_id": claimed_delivery.get("gate_id"),
                        "current_gate_id": current_delivery.get("gate_id"),
                    },
                )
                return False
        if gate is not None and gate.enforcement_mode == "enforce" and gate.architect_task_id != task_id:
            task = get_task(conn, task_id)
            if task is not None and task.current_run_id is not None and expected_run_id is None:
                _append_event(conn, task_id, "completion_blocked", {"reason": "expected_run_required", "gate_id": gate.gate_id})
                return False
            if _gate_requires_enforcement(gate):
                _append_event(conn, task_id, "completion_blocked", {"reason": ARCHITECTURE_GATE_REASON_OPEN, "gate_id": gate.gate_id})
                return False
        # Keep this gate in the DB completion kernel so CLI, model-tool, and
        # any future writer cannot bypass it with a success flag. Re-read the
        # contract while this transaction owns the write lock and reject a
        # stale remote verification before the status CAS.
        current_publication_contract = _read_publication_contract(conn, task_id)
        if current_publication_contract is None or publication_contract is None:
            return False

        if current_publication_contract != publication_contract:
            _append_event(
                conn,
                task_id,
                "completion_blocked",
                {
                    "reason": "publication_contract_changed_during_readback",
                    "verified_contract": _publication_contract_payload(
                        publication_contract
                    ),
                    "current_contract": _publication_contract_payload(
                        current_publication_contract
                    ),
                    "publication_readback": publication_verification,
                },
                run_id=_current_run_id(conn, task_id),
            )
            return False

        if current_publication_contract.has_publication_fields:
            if publication_verification is None:
                return False
            if not publication_verification.get("verified"):
                blocked_payload = {
                    **publication_verification,
                    "reason": "publication_ref_not_verified",
                    "readback_reason": publication_verification.get("reason"),
                    "expected_sha": current_publication_contract.expected_sha,
                    "remote": current_publication_contract.remote,
                    "remote_ref": current_publication_contract.ref,
                }
                _append_event(
                    conn,
                    task_id,
                    "completion_blocked",
                    blocked_payload,
                    run_id=_current_run_id(conn, task_id),
                )
                return False

        reviewer_binding = get_current_review_artifact(conn, task_id)
        if reviewer_binding is not None:
            _verify_review_artifact_binding(conn, reviewer_binding)

        # Artifact-bound rework is resolved before the task status CAS.  A
        # rejected selection therefore rolls back the binding, the run close,
        # and every event written by this completion attempt together.
        source_run_id = _current_run_id(conn, task_id)
        task_state = conn.execute(
            "SELECT status, current_run_id FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if task_state is None or task_state["status"] not in {
            "running", "ready", "blocked",
        }:
            return False
        if expected_run_id is not None and (
            task_state["current_run_id"] != int(expected_run_id)
        ):
            return False

        current_rework_reviews = _current_rework_reviews_for_fix(conn, task_id)
        current_review_ids = {item[0] for item in current_rework_reviews}
        all_rework_rows = conn.execute(
            "SELECT task_id, payload FROM task_events "
            "WHERE kind = 'rework_requested'"
        ).fetchall()
        historical_review_ids: set[str] = set()
        for row in all_rework_rows:
            try:
                payload = json.loads(row["payload"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict) and str(
                payload.get("fix_task_id") or ""
            ).strip() == task_id:
                historical_review_ids.add(str(row["task_id"]))

        bound_review_outputs: list[dict[str, Any]] = []
        for review_id in sorted(historical_review_ids):
            binding = _ensure_review_artifact_binding_in_txn(
                conn,
                review_id,
                now=now,
                require_if_referenced=True,
            )
            if binding is None:
                continue
            if review_id not in current_review_ids:
                raise ReviewArtifactError(
                    f"review artifact source rework event for {review_id} "
                    "is no longer current"
                )
            _verify_review_artifact_binding(conn, binding)
            selected_attachment_id = normalized_review_outputs.get(review_id)
            if selected_attachment_id is None:
                raise ReviewArtifactError(
                    f"artifact_selection_required: review {review_id} requires "
                    "exactly one selected completion attachment"
                )
            current_request = next(
                item for item in current_rework_reviews if item[0] == review_id
            )
            rebound = bind_review_artifact_in_txn(
                conn,
                review_id,
                selected_attachment_id,
                task_id,
                source_run_id,
                current_request[1],
                binding.generation,
                now,
            )
            bound_review_outputs.append(
                {
                    "review_task_id": review_id,
                    "generation": rebound.generation,
                    "attachment_id": rebound.attachment_id,
                    "sha256": rebound.sha256,
                    "source_rework_event_id": current_request[1],
                }
            )

        unknown_output_reviews = set(normalized_review_outputs) - current_review_ids
        if unknown_output_reviews:
            raise ReviewArtifactError(
                "review_outputs names a review that is not the current rework "
                "target: " + ", ".join(sorted(unknown_output_reviews))
            )
        if expected_run_id is None:
            cur = conn.execute(
                """
                UPDATE tasks
                   SET status       = 'done',
                       result       = ?,
                       completed_at = ?,
                       claim_lock   = NULL,
                       claim_expires= NULL,
                       worker_pid   = NULL,
                       worker_started_at = NULL,
                       worker_pgid = NULL,
                       worker_sid = NULL,
                       block_kind   = NULL,
                       block_recurrences = 0
                 WHERE id = ?
                   AND status IN ('running', 'ready', 'blocked')
                """,
                (result, now, task_id),
            )
        else:
            cur = conn.execute(
                """
                UPDATE tasks
                   SET status       = 'done',
                       result       = ?,
                       completed_at = ?,
                       claim_lock   = NULL,
                       claim_expires= NULL,
                       worker_pid   = NULL,
                       worker_started_at = NULL,
                       worker_pgid = NULL,
                       worker_sid = NULL,
                       block_kind   = NULL,
                       block_recurrences = 0
                 WHERE id = ?
                   AND status IN ('running', 'ready', 'blocked')
                   AND current_run_id = ?
                """,
                (result, now, task_id, int(expected_run_id)),
            )
        if cur.rowcount != 1:
            return False
        if isinstance(metadata, dict):
            _persist_scratch_completion_artifacts(conn, task_id, metadata)
            for stored_path in metadata.pop("_staged_artifacts", []):
                path = Path(stored_path)
                _insert_completion_attachment(
                    conn,
                    task_id,
                    filename=path.name,
                    stored_path=str(path),
                    size=path.stat().st_size,
                    created_at=now,
                )
        run_id = _end_run(
            conn, task_id,
            outcome="completed", status="done",
            summary=summary if summary is not None else result,
            metadata=metadata,
        )
        # If complete_task was called on a never-claimed task (ready or
        # blocked → done with no run in flight), synthesize a
        # zero-duration run so the handoff fields are persisted in
        # attempt history instead of silently lost.
        if run_id is None and (summary or metadata or result):
            run_id = _synthesize_ended_run(
                conn, task_id,
                outcome="completed",
                summary=summary if summary is not None else result,
                metadata=metadata,
            )
        # Architecture completion and handoff acceptance are one atomic
        # transition. A malformed handoff rolls the completion back, leaving
        # the architect run alive so it can correct and retry. This removes
        # the former production dead-end where only tests called the separate
        # acceptance function after a successful completion.
        accepted_gate: Optional[ArchitectureGate] = None
        if gate is not None and gate.architect_task_id == task_id:
            if run_id is None or not isinstance(metadata, dict):
                raise ValueError(
                    "architect completion requires formal handoff metadata"
                )
            accepted_gate = _accept_architecture_handoff_in_txn(
                conn,
                gate,
                run_id=run_id,
                metadata=metadata,
            )
            if accepted_gate.state == "validated_awaiting_approval":
                delivery_withheld = True
                delivery_gate_id = accepted_gate.gate_id
                delivery_digest = accepted_gate.design_digest
        # Carry the handoff summary in the event payload so gateway
        # notifiers and dashboard WS consumers can render it without a
        # second SQL round-trip. First line only, 400 char cap — the
        # full summary stays on the run row.
        ev_summary = (summary if summary is not None else result) or ""
        ev_summary = ev_summary.strip().splitlines()[0][:400] if ev_summary else ""
        if delivery_withheld:
            completed_payload: dict = {
                "result_len": 0,
                "summary": (
                    "Architecture handoff validated; awaiting exact-digest "
                    "human approval."
                ),
                "delivery_withheld": True,
                "gate_id": delivery_gate_id,
                "design_digest": delivery_digest,
            }
        else:
            completed_payload = {
                "result_len": len(result) if result else 0,
                "summary": ev_summary or None,
            }
        if publication_verification is not None:
            completed_payload["publication_readback"] = publication_verification
        if verified_cards:
            completed_payload["verified_cards"] = verified_cards
        if bound_review_outputs:
            completed_payload["review_artifact_bindings"] = bound_review_outputs
        # Carry artifact paths in the event payload so the gateway
        # notifier can upload them as native attachments alongside the
        # completion message. Workers pass these via
        # ``kanban_complete(artifacts=[...])`` which stashes the list in
        # ``metadata["artifacts"]`` — we promote it onto the event so
        # consumers don't have to fetch the run row to find it.
        if isinstance(metadata, dict) and not delivery_withheld:
            md_model_used = metadata.get("model_used")
            if isinstance(md_model_used, dict):
                cleaned_model_used = {
                    str(k): str(v)
                    for k, v in md_model_used.items()
                    if k in {"provider", "model", "reasoning_effort"} and v
                }
                if cleaned_model_used:
                    completed_payload["model_used"] = cleaned_model_used
            md_artifacts = metadata.get("artifacts")
            if isinstance(md_artifacts, (list, tuple)):
                cleaned_artifacts = [
                    str(p).strip() for p in md_artifacts if isinstance(p, str) and str(p).strip()
                ]
                if cleaned_artifacts:
                    completed_payload["artifacts"] = cleaned_artifacts
        completed_event_id = _append_event(
            conn, task_id, "completed",
            completed_payload,
            run_id=run_id,
        )
        if reviewer_binding is not None:
            attestation = {
                "review_task_id": task_id,
                "review_completion_event_id": completed_event_id,
                "artifact_generation": reviewer_binding.generation,
                "artifact_attachment_id": reviewer_binding.attachment_id,
                "artifact_sha256": reviewer_binding.sha256,
            }
            completed_payload["review_artifact_attestation"] = attestation
            conn.execute(
                "UPDATE task_events SET payload = ? WHERE id = ?",
                (json.dumps(completed_payload, ensure_ascii=False), completed_event_id),
            )
        # A successful completion starts a fresh breaker window. The event
        # log is the audit trail and is never deleted; the breaker query
        # instead ignores failure signatures recorded at or before the
        # latest ``completed`` event across the saga (see
        # _recent_failure_signatures).
    # Prose-scan the summary + result for t_<hex> references that do
    # not resolve. Advisory — does not block the completion. Runs in
    # its own txn so the completion itself is already durable by the
    # time we emit the warning.
    scan_text = " ".join(filter(None, [summary, result]))
    if scan_text:
        phantom_refs = _scan_prose_for_phantom_ids(conn, scan_text)
        # Drop any phantom refs that were already flagged as verified
        # above (shouldn't happen — verified means they exist — but
        # belt-and-suspenders).
        phantom_refs = [p for p in phantom_refs if p not in set(verified_cards)]
        if phantom_refs:
            with write_txn(conn):
                _append_event(
                    conn, task_id, "suspected_hallucinated_references",
                    {
                        "phantom_refs": phantom_refs,
                        "source": "completion_summary",
                    },
                    run_id=run_id,
                )
    # Successful completion — wipe the consecutive-failures counter.
    # Failure history stays on the event log for audit; the counter
    # just tracks "is there a current pathology the breaker should
    # care about", and a success resets that question.
    _clear_failure_counter(conn, task_id)
    # Recompute ready status for dependents (separate txn so children see done).
    recompute_ready(conn)
    # Clean exact-owned child/tmux/worktree resources before ordinary scratch
    # cleanup. Identity mismatch is persisted and never treated as authority.
    if run_id is not None:
        cleanup_owned_run_resources(conn, task_id, run_id)
    # Clean up the scratch workspace. Legacy assignee-derived tmux cleanup was
    # removed by BUILD-487 because a guessed session name is not ownership.
    _cleanup_workspace(conn, task_id)
    # Reap a clean Hermes-owned linked worktree, or leave an auditable deferred
    # event when another task/run or Git's dirty-state guard still owns it.
    cleanup_terminal_task_worktrees(conn)
    _done_task = get_task(conn, task_id)
    _fire_kanban_lifecycle_hook(
        "kanban_task_completed",
        task_id,
        board=get_current_board(),
        assignee=_done_task.assignee if _done_task else None,
        run_id=run_id,
        summary=(
            "Architecture handoff validated; awaiting exact-digest human approval."
            if delivery_withheld
            else (summary if summary is not None else result)
        ),
    )
    return True


# ---------------------------------------------------------------------------
# Workspace / tmux cleanup
# ---------------------------------------------------------------------------


WORKSPACE_CLEANUP_LEASE_SECONDS = 60
_TERMINAL_WORKSPACE_STATUSES = ("done", "archived")


def _workspace_cleanup_now(
    now: Optional[int],
    clock: Optional[Callable[[], float]],
) -> int:
    if now is not None:
        return int(now)
    return int(clock() if clock is not None else time.time())


def _registered_worktree_path(repo_root: Path, path: Path) -> bool:
    """Return whether Git currently registers this exact worktree path."""

    try:
        listed = subprocess.run(
            ["git", "-C", str(repo_root), "worktree", "list", "--porcelain", "-z"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except OSError:
        return False
    if listed.returncode != 0:
        return False
    expected = str(path)
    for record in (listed.stdout or "").split("\x00\x00"):
        for field in record.split("\x00"):
            if field == f"worktree {expected}":
                return True
    return False


def _workspace_cleanup_lease(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    now: int,
    lease_seconds: int,
) -> tuple[str, sqlite3.Row] | None:
    """Acquire one task-level cleanup lease with a compare-and-swap."""

    token = f"{_claimer_id()}:workspace:{secrets.token_hex(12)}"
    expires = now + max(1, int(lease_seconds))
    with write_txn(conn):
        row = conn.execute(
            "SELECT * FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if row is None or row["workspace_kind"] != "worktree":
            return None
        if row["status"] not in _TERMINAL_WORKSPACE_STATUSES:
            return None
        if not bool(row["workspace_managed"]):
            return None
        cur = conn.execute(
            "UPDATE tasks SET workspace_cleanup_lease = ?, "
            "workspace_cleanup_lease_expires = ? WHERE id = ? "
            "AND status IN ('done', 'archived') AND workspace_managed = 1 "
            "AND (workspace_cleanup_lease IS NULL "
            "OR workspace_cleanup_lease_expires IS NULL "
            "OR workspace_cleanup_lease_expires <= ?)",
            (token, expires, task_id, now),
        )
        if cur.rowcount != 1:
            return None
        leased = conn.execute(
            "SELECT * FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        return token, leased


def _finish_workspace_cleanup_lease(
    conn: sqlite3.Connection,
    task_id: str,
    token: str,
    *,
    kind: str,
    payload: dict[str, Any],
) -> None:
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET workspace_cleanup_lease = NULL, "
            "workspace_cleanup_lease_expires = NULL "
            "WHERE id = ? AND workspace_cleanup_lease = ?",
            (task_id, token),
        )
        if cur.rowcount == 1:
            _append_event(conn, task_id, kind, payload)


def _workspace_cleanup_defer_reason(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
) -> str | None:
    path = row["workspace_path"]
    if not path:
        return "workspace_path_missing"
    try:
        target_path = Path(str(path)).expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        return "workspace_path_unresolvable"
    active_references = conn.execute(
        "SELECT id, workspace_path FROM tasks WHERE workspace_kind = 'worktree' "
        "AND workspace_path IS NOT NULL "
        "AND status NOT IN ('done', 'archived') AND id != ?",
        (row["id"],),
    ).fetchall()
    for active_reference in active_references:
        reference_path = Path(str(active_reference["workspace_path"])).expanduser()
        try:
            same_path = reference_path.resolve(strict=False) == target_path
        except (OSError, RuntimeError):
            return "active_workspace_reference_unresolvable"
        if same_path:
            return "active_task_references_workspace"
    if row["current_run_id"] is not None:
        return "active_run"
    active_run = conn.execute(
        "SELECT 1 FROM task_runs WHERE task_id = ? AND ended_at IS NULL LIMIT 1",
        (row["id"],),
    ).fetchone()
    if active_run is not None:
        return "active_run"
    return None


def _revalidate_managed_worktree(row: sqlite3.Row) -> tuple[Path, Path, str | None]:
    """Revalidate the registered path and Git identity before removal."""

    raw_path = str(row["workspace_path"] or "")
    raw_repo_root = str(row["workspace_repo_root"] or "")
    raw_common_dir = str(row["workspace_repo_common_dir"] or "")
    if not raw_path or not raw_repo_root or not raw_common_dir:
        return Path(raw_path), Path(raw_repo_root), "workspace_identity_incomplete"
    path = Path(raw_path).expanduser()
    repo_root = Path(raw_repo_root).expanduser()
    common_dir = Path(raw_common_dir).expanduser()
    if (
        not path.is_absolute()
        or not repo_root.is_absolute()
        or not common_dir.is_absolute()
        or path.resolve(strict=False) != path
        or repo_root.resolve(strict=False) != repo_root
        or common_dir.resolve(strict=False) != common_dir
    ):
        return path, repo_root, "workspace_identity_changed"
    if not path.exists():
        if _registered_worktree_path(repo_root, path):
            return path, repo_root, "workspace_path_missing_but_registered"
        return path, repo_root, "workspace_already_absent"
    if not path.is_dir() or not _is_linked_worktree_checkout(path):
        return path, repo_root, "workspace_is_not_linked_worktree"
    if _git_common_dir(path) != common_dir or _git_common_dir(repo_root) != common_dir:
        return path, repo_root, "workspace_git_common_directory_mismatch"
    if not _registered_worktree_path(repo_root, path):
        return path, repo_root, "workspace_not_registered"
    return path, repo_root, None


def cleanup_terminal_task_worktrees(
    conn: sqlite3.Connection,
    task_id: Optional[str] = None,
    *,
    now: Optional[int] = None,
    clock: Optional[Callable[[], float]] = None,
    lease_seconds: int = WORKSPACE_CLEANUP_LEASE_SECONDS,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Safely reap Hermes-owned linked worktrees after terminal transitions."""

    effective_now = _workspace_cleanup_now(now, clock)
    try:
        bounded_limit = max(1, min(int(limit), 1000))
    except (TypeError, ValueError):
        bounded_limit = 100
    if task_id is None:
        rows = conn.execute(
            "SELECT id FROM tasks WHERE status IN ('done', 'archived') "
            "AND workspace_kind = 'worktree' AND workspace_managed = 1 "
            "ORDER BY id LIMIT ?",
            (bounded_limit,),
        ).fetchall()
        task_ids = [str(row["id"]) for row in rows]
    else:
        reference = conn.execute(
            "SELECT workspace_path FROM tasks WHERE id = ?",
            (str(task_id),),
        ).fetchone()
        if reference is not None and reference["workspace_path"]:
            rows = conn.execute(
                "SELECT id FROM tasks WHERE status IN ('done', 'archived') "
                "AND workspace_kind = 'worktree' AND workspace_managed = 1 "
                "AND workspace_path = ? ORDER BY id LIMIT ?",
                (reference["workspace_path"], bounded_limit),
            ).fetchall()
            task_ids = [str(row["id"]) for row in rows]
        else:
            task_ids = [str(task_id)]

    results: list[dict[str, Any]] = []
    for current_task_id in task_ids:
        leased = _workspace_cleanup_lease(
            conn,
            current_task_id,
            now=effective_now,
            lease_seconds=lease_seconds,
        )
        if leased is None:
            continue
        token, row = leased
        path = Path(str(row["workspace_path"] or ""))
        defer_reason = _workspace_cleanup_defer_reason(conn, row)
        if defer_reason is not None:
            payload = {
                "path": str(path),
                "reason": defer_reason,
            }
            _finish_workspace_cleanup_lease(
                conn,
                current_task_id,
                token,
                kind="workspace_cleanup_deferred",
                payload=payload,
            )
            results.append({"task_id": current_task_id, "status": "deferred", **payload})
            continue

        path, repo_root, identity_error = _revalidate_managed_worktree(row)
        if identity_error == "workspace_already_absent":
            payload = {"path": str(path), "reason": identity_error}
            _finish_workspace_cleanup_lease(
                conn,
                current_task_id,
                token,
                kind="workspace_cleaned",
                payload=payload,
            )
            results.append({"task_id": current_task_id, "status": "absent", **payload})
            continue
        if identity_error is not None:
            payload = {"path": str(path), "reason": identity_error}
            _finish_workspace_cleanup_lease(
                conn,
                current_task_id,
                token,
                kind="workspace_cleanup_deferred",
                payload=payload,
            )
            results.append({"task_id": current_task_id, "status": "deferred", **payload})
            continue

        try:
            removed = subprocess.run(
                ["git", "-C", str(repo_root), "worktree", "remove", str(path)],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except OSError as exc:
            removed = None
            detail = f"git_worktree_remove_failed:{type(exc).__name__}"
        else:
            detail = (
                "removed"
                if removed.returncode == 0
                else "git_worktree_remove_refused"
            )
        if removed is not None and removed.returncode == 0:
            payload = {"path": str(path), "repo_root": str(repo_root)}
            event_kind = "workspace_cleaned"
            status = "cleaned"
        else:
            stderr = (
                ((removed.stderr or removed.stdout or "").strip() if removed else "")
                [:400]
            )
            payload = {
                "path": str(path),
                "repo_root": str(repo_root),
                "reason": detail,
                "detail": stderr or None,
            }
            event_kind = "workspace_cleanup_deferred"
            status = "deferred"
        _finish_workspace_cleanup_lease(
            conn,
            current_task_id,
            token,
            kind=event_kind,
            payload=payload,
        )
        results.append({"task_id": current_task_id, "status": status, **payload})
    return results


def _merge_completion_prose_artifacts(
    conn: sqlite3.Connection,
    task_id: str,
    metadata: Optional[dict],
    *,
    summary: Optional[str],
    result: Optional[str],
) -> Optional[dict]:
    """Promote existing scratch files named in legacy completion prose.

    ``artifacts=[...]`` is preferred. Older workers only wrote an absolute
    deliverable path in ``summary``/``result``; discover it while scratch still
    exists so cleanup cannot erase the file the user was promised.
    """
    row = conn.execute(
        "SELECT workspace_kind, workspace_path FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if not row or row["workspace_kind"] != "scratch" or not row["workspace_path"]:
        return metadata
    workspace = Path(row["workspace_path"]).expanduser()
    if not _is_managed_scratch_path(workspace):
        return metadata
    text = "\n".join(part for part in (summary, result) if part)
    if not text:
        return metadata
    prefix = re.escape(str(workspace))
    discovered: list[str] = []
    for match in re.finditer(prefix + r"(?:[/\\][^\s`\"'<>]+)", text):
        raw = match.group(0).rstrip(".,;:!?)]}")
        candidate = Path(raw)
        if candidate.is_file():
            discovered.append(str(candidate))
    if not discovered:
        return metadata
    updated = dict(metadata) if isinstance(metadata, dict) else {}
    existing = updated.get("artifacts")
    merged = list(existing) if isinstance(existing, (list, tuple)) else []
    seen = {str(path) for path in merged}
    for path in discovered:
        if path not in seen:
            merged.append(path)
            seen.add(path)
    updated["artifacts"] = merged
    return updated


def _persist_scratch_completion_artifacts(
    conn: sqlite3.Connection,
    task_id: str,
    metadata: dict,
) -> None:
    """Copy scratch-workspace completion artifacts before cleanup removes them."""
    raw_artifacts = metadata.get("artifacts")
    if not isinstance(raw_artifacts, (list, tuple)):
        return

    row = conn.execute(
        "SELECT workspace_kind, workspace_path FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if not row or row["workspace_kind"] != "scratch" or not row["workspace_path"]:
        return

    workspace = Path(row["workspace_path"]).expanduser()
    is_managed, board = _managed_scratch_path_info(workspace)
    if not is_managed:
        return

    try:
        workspace_root = workspace.resolve()
    except OSError:
        return

    attachment_dir = task_attachments_dir(task_id, board=board)
    persisted: list[str] = []
    used_destinations: set[Path] = set()
    changed = False

    def _discard_copies() -> None:
        for copied in used_destinations:
            try:
                copied.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            attachment_dir.rmdir()
        except OSError:
            pass

    for item in raw_artifacts:
        artifact = str(item).strip() if isinstance(item, str) else ""
        if not artifact:
            continue
        src = Path(artifact).expanduser()
        try:
            resolved_src = src.resolve()
        except OSError:
            persisted.append(artifact)
            continue

        if not resolved_src.is_relative_to(workspace_root):
            persisted.append(artifact)
            continue

        if not src.is_file():
            _discard_copies()
            raise ArtifactPreservationError(
                f"declared scratch artifact is unavailable or not a regular file: {artifact}"
            )

        size = resolved_src.stat().st_size
        if size > KANBAN_ATTACHMENT_MAX_BYTES:
            _discard_copies()
            raise ArtifactPreservationError(
                f"declared scratch artifact exceeds the "
                f"{KANBAN_ATTACHMENT_MAX_BYTES}-byte limit: {artifact}"
            )

        dest: Optional[Path] = None
        try:
            attachment_dir.mkdir(parents=True, exist_ok=True)
            dest = _unique_attachment_path(attachment_dir, resolved_src.name, used_destinations)
            with resolved_src.open("rb") as source_file, dest.open("xb") as destination_file:
                copied = 0
                while chunk := source_file.read(1024 * 1024):
                    copied += len(chunk)
                    if copied > KANBAN_ATTACHMENT_MAX_BYTES:
                        raise ArtifactPreservationError(
                            f"declared scratch artifact grew beyond the size limit: {artifact}"
                        )
                    destination_file.write(chunk)
        except Exception as exc:
            if dest is not None:
                try:
                    dest.unlink(missing_ok=True)
                except OSError:
                    pass
            _discard_copies()
            if isinstance(exc, ArtifactPreservationError):
                raise
            raise ArtifactPreservationError(
                f"could not preserve declared scratch artifact {artifact}: {exc}"
            ) from exc

        used_destinations.add(dest)
        persisted.append(str(dest.resolve()))
        changed = True

    if changed:
        metadata["artifacts"] = persisted
        metadata["_staged_artifacts"] = [
            path for path in persisted if path.startswith(str(attachment_dir.resolve()))
        ]


def _insert_completion_attachment(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    filename: str,
    stored_path: str,
    size: int,
    created_at: int,
) -> None:
    """Record a worker-produced artifact in the existing attachment table."""
    conn.execute(
        "INSERT INTO task_attachments "
        "(task_id, filename, stored_path, content_type, size, uploaded_by, created_at) "
        "VALUES (?, ?, ?, NULL, ?, 'kanban_complete', ?)",
        (task_id, filename, stored_path, size, created_at),
    )
    _append_event(
        conn,
        task_id,
        "attached",
        {"filename": filename, "size": size, "by": "kanban_complete"},
    )


def _unique_attachment_path(directory: Path, filename: str, used: set[Path]) -> Path:
    """Return a non-conflicting path under ``directory`` for ``filename``."""
    safe_name = Path(filename).name or "artifact"
    candidate = directory / safe_name
    if candidate not in used and not candidate.exists():
        return candidate

    stem = Path(safe_name).stem or "artifact"
    suffix = Path(safe_name).suffix
    idx = 1
    while True:
        candidate = directory / f"{stem}_{idx}{suffix}"
        if candidate not in used and not candidate.exists():
            return candidate
        idx += 1


def _managed_scratch_path_info(p: Path) -> tuple[bool, Optional[str]]:
    """Return whether *p* is managed scratch storage and the matching board."""
    try:
        p_abs = p.resolve(strict=False)
    except OSError:
        return False, None
    roots: list[tuple[Path, Optional[str]]] = []
    override = os.environ.get("HERMES_KANBAN_WORKSPACES_ROOT", "").strip()
    if override:
        try:
            roots.append((Path(override).expanduser().resolve(strict=False), None))
        except OSError:
            pass
    try:
        home = kanban_home()
    except OSError:
        home = None
    if home is not None:
        try:
            roots.append(((home / "kanban" / "workspaces").resolve(strict=False), DEFAULT_BOARD))
        except OSError:
            pass
        try:
            boards_parent = (home / "kanban" / "boards").resolve(strict=False)
        except OSError:
            boards_parent = None
        if boards_parent is not None:
            try:
                entries = list(boards_parent.iterdir())
            except OSError:
                entries = []
            for entry in entries:
                try:
                    if not entry.is_dir():
                        continue
                except OSError:
                    continue
                try:
                    roots.append(((entry / "workspaces").resolve(strict=False), entry.name))
                except OSError:
                    continue
    for root, board in roots:
        if p_abs == root:
            continue
        try:
            if p_abs.is_relative_to(root):
                return True, board
        except ValueError:
            continue
    return False, None


def _is_managed_scratch_path(p: Path) -> bool:
    """Return True iff *p* is a strict descendant of a kanban-managed scratch root.

    A managed root is exclusively a ``workspaces/`` directory — never the
    broader kanban home, a board root, or sibling subtrees like ``logs/`` or
    ``boards/<slug>/`` itself. Allowed roots:

    * ``HERMES_KANBAN_WORKSPACES_ROOT`` when set (worker-side override
      injected by the dispatcher).
    * ``<kanban_home>/kanban/workspaces`` — legacy default-board scratch root.
    * ``<kanban_home>/kanban/boards/<slug>/workspaces`` for each board slug
      that currently exists on disk.

    The check requires strict descendancy: a path equal to one of these
    roots is NOT managed (deleting the workspaces root would wipe every
    task's scratch dir at once), and a path that resolves to ``<kanban_home>
    /kanban`` itself, ``<kanban_home>/kanban/logs``, or
    ``<kanban_home>/kanban/boards/<slug>`` is rejected because those
    subtrees hold Hermes' own DB, metadata, and logs, not task workspaces.

    Used by :func:`_cleanup_workspace` to refuse to ``shutil.rmtree`` paths
    outside Hermes-managed storage. A board ``default_workdir`` pointing at a
    real source tree can otherwise pair with ``workspace_kind='scratch'`` and
    cause task completion to delete user data (#28818).
    """
    is_managed, _board = _managed_scratch_path_info(p)
    return is_managed


def _cleanup_workspace(conn: sqlite3.Connection, task_id: str) -> None:
    """Remove a task's scratch workspace dir.

    Called from :func:`complete_task` after the DB transaction commits.
    Best-effort — any error is swallowed so cleanup never blocks task completion.
    Only ``scratch`` workspaces are removed; ``worktree`` and ``dir`` workspaces
    are intentionally preserved.
    """
    try:
        row = conn.execute(
            "SELECT workspace_kind, workspace_path FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if not row:
            return
        kind: Optional[str] = row["workspace_kind"]
        path: Optional[str] = row["workspace_path"]
        if kind != "scratch" or not path:
            # This task's own workspace isn't a removable scratch dir, but its
            # completion may still unblock a deferred parent scratch cleanup
            # (e.g. a 'dir' child whose scratch parent was waiting on it). #33774
            _try_cleanup_parent_workspaces(conn, task_id)
            return
        # Check if this task has children that still need the workspace.
        # If any child is not yet done/archived, defer cleanup so the
        # child can read handoff artifacts from the scratch dir (#33774).
        _active_children = conn.execute(
            "SELECT 1 FROM task_links l "
            "JOIN tasks t ON t.id = l.child_id "
            "WHERE l.parent_id = ? AND t.status NOT IN ('done', 'archived', 'failed', 'cancelled') "
            "LIMIT 1",
            (task_id,),
        ).fetchone()
        if _active_children:
            _log.debug(
                "Deferring scratch workspace cleanup for task %s: "
                "active children still need workspace at %s",
                task_id, path,
            )
            return
        import shutil
        wp = Path(path)
        if wp.is_dir():
            # Containment guard (#28818): a board's ``default_workdir`` can
            # pair ``workspace_kind='scratch'`` with a user-supplied path
            # pointing at a real source tree. Without this check, task
            # completion would unconditionally ``shutil.rmtree`` that path
            # and silently delete the user's source data.
            if _is_managed_scratch_path(wp):
                shutil.rmtree(wp, ignore_errors=True)
                _log.debug("Removed scratch workspace: %s", wp)
            else:
                _log.warning(
                    "Refusing to remove out-of-scratch workspace for task %s: %s "
                    "(workspace_kind='scratch' but path is outside any "
                    "kanban-managed workspaces root)",
                    task_id, wp,
                )
        # After cleaning up this task's workspace, check if any parent
        # tasks now have all children done — their deferred cleanup can
        # proceed (#33774).
        _try_cleanup_parent_workspaces(conn, task_id)
    except Exception:
        pass  # best-effort — never block completion


def _try_cleanup_parent_workspaces(conn: sqlite3.Connection, task_id: str) -> None:
    """Clean up parent scratch workspaces now that *task_id* completed.

    When a parent task's cleanup was deferred because it had active children,
    this function is called after each child completes.  If all children of a
    parent are now done/archived/failed/cancelled, the parent's scratch
    workspace is removed (#33774).
    """
    try:
        parents = conn.execute(
            "SELECT parent_id FROM task_links WHERE child_id = ?",
            (task_id,),
        ).fetchall()
        for (parent_id,) in parents:
            row = conn.execute(
                "SELECT workspace_kind, workspace_path FROM tasks WHERE id = ?",
                (parent_id,),
            ).fetchone()
            if not row or row["workspace_kind"] != "scratch" or not row["workspace_path"]:
                continue
            # Check if ALL children of this parent are terminal
            active = conn.execute(
                "SELECT 1 FROM task_links l "
                "JOIN tasks t ON t.id = l.child_id "
                "WHERE l.parent_id = ? AND t.status NOT IN ('done', 'archived', 'failed', 'cancelled') "
                "LIMIT 1",
                (parent_id,),
            ).fetchone()
            if active:
                continue  # still has active children
            # All children done — safe to clean up parent workspace
            import shutil
            wp = Path(row["workspace_path"])
            if wp.is_dir() and _is_managed_scratch_path(wp):
                shutil.rmtree(wp, ignore_errors=True)
                _log.debug("Deferred cleanup: removed parent %s scratch workspace: %s", parent_id, wp)
    except Exception:
        pass  # best-effort


# ---------------------------------------------------------------------------
# First-use tip for scratch workspaces
# ---------------------------------------------------------------------------
#
# Scratch workspaces are intentionally ephemeral — ``_cleanup_workspace``
# removes them as soon as ``complete_task`` runs.  New users often don't
# realize that and lose worker output (community report, May 2026).  The
# behavior is right; the lack of warning is the bug.
#
# On the FIRST scratch workspace materialization across the whole install
# we:
#   1. Log a warning line on the dispatcher logger.
#   2. Append a ``tip_scratch_workspace`` event on the task so it's visible
#      via ``hermes kanban show <id>`` and the dashboard.
#   3. Touch a sentinel file under ``kanban_home() / '.scratch_tip_shown'``
#      so we don't repeat the tip — once you know, you know.
#
# Scope is per-install, not per-board: a user creating a second board
# already learned the lesson on board #1.

_SCRATCH_TIP_SENTINEL_NAME = ".scratch_tip_shown"

_SCRATCH_TIP_MESSAGE = (
    "scratch workspaces are ephemeral — they're deleted when the task "
    "completes. Use --workspace worktree: (git worktree) or "
    "--workspace dir:/abs/path (existing dir) to preserve worker output."
)


def _scratch_tip_sentinel_path() -> Path:
    """Path to the per-install scratch-workspace-tip sentinel file."""
    return kanban_home() / _SCRATCH_TIP_SENTINEL_NAME


def _scratch_tip_shown() -> bool:
    """True iff the scratch-workspace tip has already been emitted on this
    install. Best-effort — any error means we re-emit, which is the safer
    failure mode for a help message."""
    try:
        return _scratch_tip_sentinel_path().exists()
    except OSError:
        return False


def _mark_scratch_tip_shown() -> None:
    """Touch the sentinel so future scratch workspaces stay silent.

    Best-effort: a failure here just means the tip might appear once more,
    which is preferable to crashing dispatch over a help message.
    """
    try:
        path = _scratch_tip_sentinel_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
    except OSError:
        pass


def _maybe_emit_scratch_tip(
    conn: sqlite3.Connection,
    task_id: str,
    workspace_kind: Optional[str],
) -> None:
    """Emit the first-use scratch-workspace tip exactly once per install.

    Called from the dispatcher right after a scratch workspace is
    materialized. No-op for ``worktree`` / ``dir`` workspaces (they're
    preserved by design) and no-op after the sentinel exists.
    """
    if (workspace_kind or "scratch") != "scratch":
        return
    if _scratch_tip_shown():
        return
    try:
        _log.warning("kanban: %s (task %s)", _SCRATCH_TIP_MESSAGE, task_id)
        with write_txn(conn):
            _append_event(
                conn, task_id, "tip_scratch_workspace",
                {"message": _SCRATCH_TIP_MESSAGE},
            )
    except Exception:
        # Best-effort — never block the spawn loop over a help message.
        pass
    finally:
        _mark_scratch_tip_shown()


def edit_completed_task_result(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    result: str,
    summary: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> bool:
    """Backfill the user-visible result for an already completed task."""
    handoff_summary = summary if summary is not None else result
    with write_txn(conn):
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if not row or row["status"] != "done":
            return False
        conn.execute(
            "UPDATE tasks SET result = ? WHERE id = ?",
            (result, task_id),
        )
        run = conn.execute(
            """
            SELECT id FROM task_runs
             WHERE task_id = ?
               AND outcome = 'completed'
             ORDER BY COALESCE(ended_at, started_at, 0) DESC, id DESC
             LIMIT 1
            """,
            (task_id,),
        ).fetchone()
        run_id = int(run["id"]) if run else None
        if run_id is None:
            run_id = _synthesize_ended_run(
                conn, task_id,
                outcome="completed",
                summary=handoff_summary,
                metadata=metadata,
            )
        else:
            conn.execute(
                "UPDATE task_runs SET summary = ? WHERE id = ?",
                (handoff_summary, run_id),
            )
            if metadata is not None:
                conn.execute(
                    "UPDATE task_runs SET metadata = ? WHERE id = ?",
                    (json.dumps(metadata, ensure_ascii=False), run_id),
                )
        invalidated_gate = _invalidate_architect_gate_for_mutation(
            conn, task_id, reason="accepted_architect_handoff_edited",
        )
        if invalidated_gate is not None:
            # This owning mutation already holds the transaction; reopen with
            # the same CAS contract without nesting ``write_txn``.
            cur = conn.execute(
                """UPDATE architecture_gates SET state = 'open', accepted_run_id = NULL,
                   accepted_snapshot = NULL, design_digest = NULL, approval_actor_id = NULL,
                   approval_actor_type = NULL, approval_surface = NULL, approved_digest = NULL,
                   approved_at = NULL, authorization_event_id = NULL, row_version = row_version + 1,
                   updated_at = ? WHERE gate_id = ? AND state = 'invalidated' AND row_version = ?""",
                (int(time.time()), invalidated_gate.gate_id, invalidated_gate.row_version),
            )
            if cur.rowcount != 1:
                raise ArchitectureGateError("architecture_gate_cas_conflict")
            conn.execute(
                "UPDATE tasks SET status = 'ready', current_run_id = NULL, "
                "completed_at = NULL, worker_pid = NULL, "
                "worker_started_at = NULL, worker_pgid = NULL, "
                "worker_sid = NULL "
                "WHERE id = ? AND status = 'done'", (task_id,),
            )
            reopened_gate = get_architecture_gate(conn, invalidated_gate.gate_id)
            assert reopened_gate is not None
            _append_gate_audit(conn, reopened_gate, "architecture_gate_reopened", "accepted_handoff_edited")
        ev_summary = (
            handoff_summary.strip().splitlines()[0][:400]
            if handoff_summary else ""
        )
        _append_event(
            conn, task_id, "edited",
            {
                "fields": (
                    ["result", "summary"]
                    + (["metadata"] if metadata is not None else [])
                ),
                "result_len": len(result) if result else 0,
                "summary": ev_summary or None,
            },
            run_id=run_id,
        )
    return True


def block_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    reason: Optional[str] = None,
    summary: Optional[str] = None,
    metadata: Optional[dict] = None,
    kind: Optional[str] = None,
    expected_run_id: Optional[int] = None,
    require_no_active_run: bool = False,
    materialization_sla_seconds: Optional[int] = None,
) -> bool:
    """Transition a task to blocked, dependency-wait, or triage.

    ``reason`` is the concise board-visible blocker. ``summary`` and
    ``metadata`` are persisted on the run row like ``kanban_complete`` so a
    retrying worker or downstream reviewer sees what happened before the block.
    ``kind`` (one of :data:`VALID_BLOCK_KINDS`, or ``None`` for legacy blocks)
    drives v0.18 block routing/recurrence handling.
    """
    if kind is not None and kind not in VALID_BLOCK_KINDS:
        raise ValueError(
            f"block kind must be one of {sorted(VALID_BLOCK_KINDS)} or None"
        )
    if expected_run_id is not None and require_no_active_run:
        raise ValueError("expected_run_id and require_no_active_run are exclusive")
    effective_summary = summary or reason
    with write_txn(conn):
        cur_row = conn.execute(
            "SELECT status, block_kind, block_recurrences FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if cur_row is None:
            return False
        prev_kind = cur_row["block_kind"] if "block_kind" in cur_row.keys() else None
        prev_recurrences = (
            int(cur_row["block_recurrences"])
            if "block_recurrences" in cur_row.keys()
            and cur_row["block_recurrences"] is not None
            else 0
        )
        dependency_info = None
        dependency_pending = False
        if kind == "dependency":
            dependency_info = _dependency_wait_info(
                conn, task_id, effective_summary,
            )
            if not dependency_info["unresolved_parent_ids"]:
                # BUILD-613's anti-respawn invariant remains: the card is not
                # eligible for ``recompute_ready`` while no parent exists.
                # Keep the caller's dependency provenance, but project it to
                # the kernel-owned pending block kind until a parent is bound.
                dependency_pending = True

        if kind == "dependency":
            persisted_kind = "dependency_pending" if dependency_pending else "dependency"
            cur = conn.execute(
                """
                UPDATE tasks
                   SET status        = 'todo',
                       claim_lock    = NULL,
                       claim_expires = NULL,
                       worker_pid    = NULL,
                       worker_started_at = NULL,
                       worker_pgid = NULL,
                       worker_sid = NULL,
                       block_kind    = ?
                 WHERE id = ?
                   AND status IN ('running', 'ready', 'review')
                """ + (
                    " AND current_run_id IS NULL" if require_no_active_run
                    else ("" if expected_run_id is None else " AND current_run_id = ?")
                ),
                (persisted_kind, task_id) if expected_run_id is None
                else (persisted_kind, task_id, int(expected_run_id)),
            )
            if cur.rowcount != 1:
                return False
            run_id = _end_run(
                conn, task_id,
                outcome="blocked", status="blocked",
                summary=effective_summary,
                metadata=metadata,
            )
            if run_id is None and effective_summary:
                run_id = _synthesize_ended_run(
                    conn, task_id,
                    outcome="blocked",
                    summary=effective_summary,
                    metadata=metadata,
                )
            if dependency_pending:
                materialize_by = int(time.time()) + _resolve_dependency_materialization_sla_seconds(
                    materialization_sla_seconds,
                )
                _append_event(
                    conn,
                    task_id,
                    "dependency_pending",
                    {
                        "reason": reason,
                        "kind": "dependency_pending",
                        "baseline_parent_ids": dependency_info["parent_ids"],
                        "materialize_by": materialize_by,
                        "source_run_id": run_id,
                        "source_event_kind": "kanban_block",
                        "normalized_signature": dependency_info["signature"],
                    },
                    run_id=run_id,
                )
            else:
                _append_event(
                    conn,
                    task_id,
                    "dependency_wait",
                    {
                        "reason": reason,
                        "kind": kind,
                        "signature": _record_dependency_wait(
                            conn, task_id, effective_summary,
                            dependency_info=dependency_info, run_id=run_id,
                        )["signature"],
                        "unresolved_parent_ids": dependency_info["unresolved_parent_ids"],
                    },
                    run_id=run_id,
                )
            _blocked_task = get_task(conn, task_id)
        else:
            # Truly-blocked kinds. Increment the unblock-loop counter when this
            # is a re-block for the SAME reason after a prior unblock.
            same_cause = prev_kind == kind
            recurrences = prev_recurrences + 1 if same_cause else 1

            if recurrences >= BLOCK_RECURRENCE_LIMIT:
                target_status = "triage"
                event_kind = "block_loop_detected"
            else:
                target_status = "blocked"
                event_kind = "blocked"

            if expected_run_id is None:
                cur = conn.execute(
                    """
                    UPDATE tasks
                       SET status        = ?,
                           claim_lock    = NULL,
                           claim_expires = NULL,
                           worker_pid    = NULL,
                           worker_started_at = NULL,
                           worker_pgid = NULL,
                           worker_sid = NULL,
                           block_kind    = ?,
                           block_recurrences = ?
                     WHERE id = ?
                       AND status IN ('running', 'ready', 'review')
                    """ + (
                        " AND current_run_id IS NULL" if require_no_active_run else ""
                    ),
                    (target_status, kind, recurrences, task_id),
                )
            else:
                cur = conn.execute(
                    """
                    UPDATE tasks
                       SET status        = ?,
                           claim_lock    = NULL,
                           claim_expires = NULL,
                           worker_pid    = NULL,
                           worker_started_at = NULL,
                           worker_pgid = NULL,
                           worker_sid = NULL,
                           block_kind    = ?,
                           block_recurrences = ?
                     WHERE id = ?
                       AND status IN ('running', 'ready', 'review')
                       AND current_run_id = ?
                    """,
                    (target_status, kind, recurrences, task_id, int(expected_run_id)),
                )
            if cur.rowcount != 1:
                return False
            run_id = _end_run(
                conn, task_id,
                outcome="blocked", status="blocked",
                summary=effective_summary,
                metadata=metadata,
            )
            if run_id is None and effective_summary:
                run_id = _synthesize_ended_run(
                    conn, task_id,
                    outcome="blocked",
                    summary=effective_summary,
                    metadata=metadata,
                )
            payload = {"reason": reason, "kind": kind, "recurrences": recurrences}
            if event_kind == "block_loop_detected":
                payload["limit"] = BLOCK_RECURRENCE_LIMIT
            _append_event(conn, task_id, event_kind, payload, run_id=run_id)
            # BUILD-261: record the normalized failure signature for this
            # block so the release/remediation circuit breaker
            # (check_failure_signature_breaker) can compare it against this
            # task's (or its remediation children's) next attempt before a
            # future respawn. A worker's block reason is exactly the shape of
            # a CI failure excerpt (e.g. "##[error]smoke check failed: ...").
            # Deliberately NOT hooked into the crash/timeout/spawn-failure
            # funnel (_record_task_failure) — that path already has its own
            # well-tested, independently-configurable failure_limit breaker,
            # and layering a second, lower-default-threshold breaker on the
            # identical event stream would silently override an operator's
            # more lenient failure_limit for any task whose infra errors
            # happen to repeat verbatim (very common). This path — a task
            # explicitly blocked with a human-readable reason — is where the
            # BUILD-261 incident's failure text actually lives.
            _record_failure_signature(
                conn, task_id, effective_summary, run_id=run_id,
            )
            _blocked_task = get_task(conn, task_id)
    if kind != "dependency":
        _fire_kanban_lifecycle_hook(
            "kanban_task_blocked",
            task_id,
            board=get_current_board(),
            assignee=_blocked_task.assignee if _blocked_task else None,
            run_id=run_id,
            reason=reason,
        )
    return True


def operator_block_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    reason: Optional[str] = None,
    summary: Optional[str] = None,
    metadata: Optional[dict] = None,
    kind: Optional[str] = None,
    signal_fn=None,
    materialization_sla_seconds: Optional[int] = None,
) -> OperatorBlockResult:
    """Fence an active run, stop its worker, then finalize the same run.

    The first transaction makes the task non-routable without releasing its
    claim or PID.  Worker termination happens after that transaction commits.
    The second transaction clears the lease and closes only the run that was
    fenced, and only after the worker is confirmed dead.
    """
    if kind is not None and kind not in VALID_BLOCK_KINDS:
        raise ValueError(
            f"block kind must be one of {sorted(VALID_BLOCK_KINDS)} or None"
        )
    effective_summary = summary or reason
    fenced: Optional[sqlite3.Row] = None
    with write_txn(conn):
        row = conn.execute(
            """SELECT status, current_run_id, worker_pid, claim_lock,
                      worker_started_at, worker_pgid, worker_sid,
                      block_kind, block_recurrences
                 FROM tasks WHERE id = ?""",
            (task_id,),
        ).fetchone()
        if row is None:
            return OperatorBlockResult(False, False)
        if row["status"] == "running" and row["current_run_id"] is not None:
            cur = conn.execute(
                """UPDATE tasks SET status = 'blocked'
                     WHERE id = ? AND status = 'running'
                       AND current_run_id = ?""",
                (task_id, int(row["current_run_id"])),
            )
            if cur.rowcount != 1:
                return OperatorBlockResult(False, False)
            fenced = row
            _append_event(
                conn,
                task_id,
                "operator_block_fenced",
                {"reason": reason, "kind": kind},
                run_id=int(row["current_run_id"]),
            )
        elif row["status"] == "blocked" and row["current_run_id"] is not None:
            pending_fence = conn.execute(
                """SELECT 1 FROM task_events
                     WHERE task_id = ? AND run_id = ?
                       AND kind = 'operator_block_fenced'
                     LIMIT 1""",
                (task_id, int(row["current_run_id"])),
            ).fetchone()
            if pending_fence is not None:
                fenced = row

    if fenced is None:
        accepted = block_task(
            conn,
            task_id,
            reason=reason,
            summary=summary,
            metadata=metadata,
            kind=kind,
            require_no_active_run=True,
            materialization_sla_seconds=materialization_sla_seconds,
        )
        return OperatorBlockResult(accepted, accepted)

    run_id = int(fenced["current_run_id"])
    pid = fenced["worker_pid"]
    claim_lock = fenced["claim_lock"]
    termination = _terminate_worker_for_task(
        pid,
        claim_lock,
        task_id=task_id,
        run_id=run_id,
        worker_started_at=fenced["worker_started_at"],
        worker_pgid=fenced["worker_pgid"],
        worker_sid=fenced["worker_sid"],
        signal_fn=signal_fn,
    )
    if not termination.get("terminated"):
        with write_txn(conn):
            current = conn.execute(
                "SELECT current_run_id FROM tasks WHERE id = ? AND status = 'blocked'",
                (task_id,),
            ).fetchone()
            if current is not None and current["current_run_id"] == run_id:
                _append_event(
                    conn,
                    task_id,
                    "worker_termination",
                    termination,
                    run_id=run_id,
                )
        return OperatorBlockResult(True, False, termination)

    now = int(time.time())
    with write_txn(conn):
        prev_kind = fenced["block_kind"]
        prev_recurrences = int(fenced["block_recurrences"] or 0)
        dependency_info = None
        dependency_pending = False
        if kind == "dependency":
            dependency_info = _dependency_wait_info(
                conn, task_id, effective_summary,
            )
            if not dependency_info["unresolved_parent_ids"]:
                # Keep BUILD-613's anti-respawn invariant without turning a
                # missing materialization into a human page.  The reconciler
                # owns the later transition once the fix card is linked.
                dependency_pending = True

        if kind == "dependency":
            recurrences = prev_recurrences
            target_status = "todo"
            event_kind = "dependency_pending" if dependency_pending else "dependency_wait"
            persisted_kind = "dependency_pending" if dependency_pending else "dependency"
        else:
            recurrences = prev_recurrences + 1 if prev_kind == kind else 1
            target_status = (
                "triage" if recurrences >= BLOCK_RECURRENCE_LIMIT else "blocked"
            )
            event_kind = (
                "block_loop_detected"
                if target_status == "triage"
                else "blocked"
            )
            persisted_kind = kind
        cur = conn.execute(
            """UPDATE tasks
                  SET status = ?, claim_lock = NULL, claim_expires = NULL,
                      worker_pid = NULL, worker_started_at = NULL,
                      worker_pgid = NULL, worker_sid = NULL,
                      current_run_id = NULL,
                      block_kind = ?, block_recurrences = ?
                WHERE id = ? AND status = 'blocked'
                  AND current_run_id = ? AND claim_lock IS ? AND worker_pid IS ?""",
            (
                target_status,
                persisted_kind,
                recurrences,
                task_id,
                run_id,
                claim_lock,
                pid,
            ),
        )
        if cur.rowcount != 1:
            return OperatorBlockResult(False, False, termination)
        conn.execute(
            """UPDATE task_runs
                  SET status = 'blocked', outcome = 'blocked', summary = ?,
                      metadata = ?, ended_at = ?, claim_lock = NULL,
                      claim_expires = NULL, worker_pid = NULL,
                      worker_started_at = NULL, worker_pgid = NULL,
                      worker_sid = NULL
                WHERE id = ? AND task_id = ? AND ended_at IS NULL""",
            (
                effective_summary,
                json.dumps(metadata, ensure_ascii=False) if metadata else None,
                now,
                run_id,
                task_id,
            ),
        )
        _append_event(
            conn, task_id, "worker_termination", termination, run_id=run_id,
        )
        payload = {"reason": reason, "kind": kind}
        if kind != "dependency":
            payload["recurrences"] = recurrences
        elif dependency_pending:
            payload.update(
                {
                    "kind": "dependency_pending",
                    "baseline_parent_ids": dependency_info["parent_ids"],
                    "materialize_by": now + _resolve_dependency_materialization_sla_seconds(
                        materialization_sla_seconds,
                    ),
                    "source_run_id": run_id,
                    "source_event_kind": "operator_block",
                    "normalized_signature": dependency_info["signature"],
                }
            )
        else:
            payload["signature"] = _record_dependency_wait(
                conn, task_id, effective_summary,
                dependency_info=dependency_info, run_id=run_id,
            )["signature"]
            payload["unresolved_parent_ids"] = dependency_info[
                "unresolved_parent_ids"
            ]
        if event_kind == "block_loop_detected":
            payload["limit"] = BLOCK_RECURRENCE_LIMIT
        _append_event(conn, task_id, event_kind, payload, run_id=run_id)
        if kind != "dependency":
            _record_failure_signature(
                conn, task_id, effective_summary, run_id=run_id,
            )
        blocked_task = get_task(conn, task_id)
    if kind != "dependency":
        _fire_kanban_lifecycle_hook(
            "kanban_task_blocked",
            task_id,
            board=get_current_board(),
            assignee=blocked_task.assignee if blocked_task else None,
            run_id=run_id,
            reason=reason,
        )
    return OperatorBlockResult(True, True, termination)


# Self-classifying run outcomes that requeue a task WITHOUT counting a failure.
# Each returns early in ``check_respawn_guard`` (before the auth-blocker regex)
# and is spaced by its own cooldown, so a no-failure-counter requeue can never
# get trapped forever by the quota/auth ``last_failure_error`` text it stamps.
DELIVERY_AUTHORIZATION_UNAVAILABLE = "delivery_authorization_unavailable"
PROVIDER_AVAILABILITY_UNAVAILABLE = "provider_availability_unavailable"
# A fallback-exhausted quiet worker whose terminal reason is a provider QUOTA
# wall (rate_limit / billing / upstream_rate_limit). Same no-failure requeue as
# PROVIDER_AVAILABILITY_UNAVAILABLE, but spaced by the long rate-limit cooldown
# (~300s) instead of the short 30s delivery cooldown — a quota window recovers
# on a timer, so probing it every 30s would thrash a worker slot for nothing
# (BUILD-734: the quota subset the 2026-07-22 self-defer deliberately excluded).
QUOTA_UNAVAILABLE = "quota_unavailable"
_NO_FAILURE_DEFER_OUTCOMES = frozenset(
    {
        DELIVERY_AUTHORIZATION_UNAVAILABLE,
        PROVIDER_AVAILABILITY_UNAVAILABLE,
        QUOTA_UNAVAILABLE,
    }
)


def defer_task_for_delivery_authorization_retry(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    expected_run_id: int,
    error: str = "delivery gate lookup unavailable",
    outcome: str = DELIVERY_AUTHORIZATION_UNAVAILABLE,
) -> bool:
    """End one attempt and requeue it without counting a task failure.

    This is a kernel-owned recovery transition for a transient authorization
    resolver outage (delivery gate lookup or, via ``outcome``, a Claude Max
    attestation / provider-availability blip). It deliberately differs from
    ``kanban_block`` (which requires an operator to unblock) and from
    ``_record_task_failure`` (which can trip the task circuit breaker). The
    respawn guard spaces the next attempt using a short, configurable cooldown.
    """
    if outcome not in _NO_FAILURE_DEFER_OUTCOMES:
        raise ValueError(f"unsupported no-failure defer outcome: {outcome!r}")
    with write_txn(conn):
        cur = conn.execute(
            """UPDATE tasks
               SET status = 'ready', claim_lock = NULL, claim_expires = NULL,
                   worker_pid = NULL, worker_started_at = NULL,
                   worker_pgid = NULL, worker_sid = NULL,
                   last_failure_error = ?
               WHERE id = ? AND status = 'running' AND current_run_id = ?""",
            (error[:500], task_id, int(expected_run_id)),
        )
        if cur.rowcount != 1:
            return False
        run_id = _end_run(
            conn,
            task_id,
            outcome=outcome,
            status=outcome,
            error=error[:500],
            metadata={"retryable": True},
        )
        _append_event(
            conn,
            task_id,
            outcome,
            {"error": error[:500], "retryable": True},
            run_id=run_id,
        )
    return True



def promote_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    actor: str,
    reason: Optional[str] = None,
    force: bool = False,
    dry_run: bool = False,
) -> tuple[bool, Optional[str]]:
    """Manually promote a `todo` or `blocked` task to `ready`.

    Mirrors the automatic promotion done by ``recompute_ready`` but
    drives it from a deliberate operator action with an audit-trail
    entry. Refuses to promote if any parent dep is not in a terminal
    state (`done`/`archived`) unless ``force=True``. Does NOT change
    assignee or claim state. Returns ``(True, None)`` on success and
    ``(False, reason)`` if refused. ``dry_run=True`` validates the
    promotion would succeed without mutating state.
    """
    row = conn.execute(
        "SELECT status, policy_quarantined, block_kind, current_run_id, worker_pid, "
               "       worker_started_at, worker_pgid, worker_sid "
        "FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if row is None:
        return False, f"task {task_id} not found"
    if row["policy_quarantined"]:
        return False, "task is policy quarantined and requires human disposition"
    if row["block_kind"] == "dependency_pending":
        return False, "dependency materialization is pending; wait for the reconciler"

    cur_status = row["status"]
    if cur_status not in ("todo", "blocked"):
        return False, (
            f"task {task_id} is {cur_status!r}; promote only applies to "
            f"'todo' or 'blocked'"
        )
    if any(
        row[key] is not None
        for key in ("worker_pid", "worker_started_at", "worker_pgid", "worker_sid")
    ):
        return False, (
            "worker termination/identity is not confirmed; reclaim it before promotion"
        )
    if cur_status == "blocked" and row["current_run_id"] is not None:
        operator_fence = conn.execute(
            """SELECT 1 FROM task_events
                 WHERE task_id = ? AND run_id = ?
                   AND kind = 'operator_block_fenced'
                 LIMIT 1""",
            (task_id, int(row["current_run_id"])),
        ).fetchone()
        if operator_fence is not None:
            return False, (
                "worker termination is not confirmed; retry the operator block"
            )
        return False, "task still has an active run; reclaim it before promotion"

    if not force:
        parents = conn.execute(
            "SELECT t.id, t.status, t.policy_quarantined, t.policy_invalidated FROM tasks t "
            "JOIN task_links l ON l.parent_id = t.id "
            "WHERE l.child_id = ?",
            (task_id,),
        ).fetchall()
        unsatisfied = [
            p["id"] for p in parents
            if not _parent_is_satisfied(p)
        ]
        if unsatisfied:
            return False, (
                f"unsatisfied parent dependencies: "
                f"{', '.join(unsatisfied)} (use --force to override)"
            )

    if dry_run:
        return True, None

    with write_txn(conn):
        upd = conn.execute(
            "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
            "claim_expires = NULL, worker_pid = NULL, "
            "worker_started_at = NULL, worker_pgid = NULL, worker_sid = NULL "
            "WHERE id = ? AND status IN ('todo', 'blocked')",
            (task_id,),
        )
        if upd.rowcount != 1:
            return False, f"task {task_id} status changed during promotion"
        _append_event(
            conn,
            task_id,
            "promoted_manual",
            {"actor": actor, "reason": reason, "forced": force},
        )

    return True, None


def unblock_task(conn: sqlite3.Connection, task_id: str) -> bool:
    """Transition ``blocked``/``scheduled`` -> ready or todo.

    Defensively closes any stale ``current_run_id`` pointer before flipping
    status. In the common path (``block_task`` closed the run already) this
    is a no-op. If a future or external write left the pointer dangling,
    the leaked run is closed as ``reclaimed`` inside the same txn so the
    runs invariant (``current_run_id IS NULL`` ⇔ run row in terminal
    state) holds for the rest of this function's lifetime.
    """
    task = get_task(conn, task_id)
    if task and task.policy_quarantined:
        return False
    if task and task.status in ("blocked", "scheduled"):
        skill_validation_error = _forced_skill_validation_error(
            task.assignee,
            task.skills,
        )
        if skill_validation_error:
            raise ValueError(skill_validation_error)

    now = int(time.time())
    with write_txn(conn):
        operator_fence = conn.execute(
            """SELECT 1
                 FROM tasks t
                 JOIN task_events e
                   ON e.task_id = t.id AND e.run_id = t.current_run_id
                WHERE t.id = ? AND t.status = 'blocked'
                  AND t.current_run_id IS NOT NULL
                  AND e.kind = 'operator_block_fenced'
                LIMIT 1""",
            (task_id,),
        ).fetchone()
        if operator_fence is not None:
            # The worker survived (or could not be safely targeted).  Keep the
            # exact run fenced until an operator retry confirms it is dead;
            # exposing ready here could dispatch a duplicate worker.
            return False
        stale = conn.execute(
            "SELECT t.current_run_id, t.worker_pid, t.worker_started_at, "
            "       t.worker_pgid, t.worker_sid, r.worker_pid AS run_worker_pid, "
            "       r.worker_started_at AS run_started_at, r.worker_pgid AS run_pgid, "
            "       r.worker_sid AS run_sid "
            "FROM tasks t LEFT JOIN task_runs r ON r.id = t.current_run_id "
            "WHERE t.id = ? AND t.status IN ('blocked', 'scheduled')",
            (task_id,),
        ).fetchone()
        if stale and stale["current_run_id"]:
            if any(
                stale[key] is not None
                for key in (
                    "worker_pid", "worker_started_at", "worker_pgid", "worker_sid",
                    "run_worker_pid", "run_started_at", "run_pgid", "run_sid",
                )
            ):
                # The attempt has a persisted worker.  Closing its run here
                # would release the task beside a still-running process.
                return False
            conn.execute(
                """
                UPDATE task_runs
                   SET status = 'reclaimed', outcome = 'reclaimed',
                       summary = COALESCE(summary, 'invariant recovery on unblock'),
                       ended_at = ?,
                       claim_lock = NULL, claim_expires = NULL,
                       worker_pid = NULL, worker_started_at = NULL,
                       worker_pgid = NULL, worker_sid = NULL
                 WHERE id = ? AND ended_at IS NULL
                """,
                (now, int(stale["current_run_id"])),
            )
        # Re-gate on parent completion before flipping 'blocked' back to
        # 'ready'. Unconditionally setting status='ready' here bypasses the
        # parent-completion invariant (the dispatcher trusts that column);
        # if parents are still in progress the task must wait in 'todo'
        # until recompute_ready picks it up. RCA: Bug 2 at
        # kanban/boards/cookai/workspaces/t_a6acd07d/root-cause.md.
        parent_rows = conn.execute(
            "SELECT p.status, p.policy_quarantined, p.policy_invalidated "
            "FROM task_links l JOIN tasks p ON p.id = l.parent_id "
            "WHERE l.child_id = ?",
            (task_id,),
        ).fetchall()
        undone_parents = any(not _parent_is_satisfied(parent) for parent in parent_rows)
        new_status = "todo" if undone_parents else "ready"
        # NOTE: deliberately does NOT touch ``block_recurrences`` or
        # ``block_kind``. Resetting the recurrence counter on unblock is exactly
        # the amnesia that let a cron unblock → worker re-block loop run
        # unbounded (Dale's report). The counter survives the unblock so that a
        # subsequent same-cause ``block_task`` can detect the loop and route to
        # triage at ``BLOCK_RECURRENCE_LIMIT``. It is reset to 0 only on a
        # successful completion (see ``complete_task``). ``consecutive_failures``
        # (the *dispatcher* spawn/crash/timeout counter — a different signal) is
        # still reset here, which is correct: a deliberate unblock is a fresh
        # start for the dispatcher's retry budget.
        cur = conn.execute(
            "UPDATE tasks SET status = ?, current_run_id = NULL, "
            "claim_lock = NULL, claim_expires = NULL, worker_pid = NULL, "
            "worker_started_at = NULL, "
            "worker_pgid = NULL, worker_sid = NULL, "
            "consecutive_failures = 0, last_failure_error = NULL "
            "WHERE id = ? AND status IN ('blocked', 'scheduled')",
            (new_status, task_id),
        )
        if cur.rowcount != 1:
            return False
        _invalidate_architect_gate_for_mutation(
            conn, task_id, reason="architect_reopened",
        )
        _append_event(
            conn, task_id, "unblocked",
            {"status": new_status} if new_status != "ready" else None,
        )
        return True


def specify_triage_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    title: Optional[str] = None,
    body: Optional[str] = None,
    assignee: Optional[str] = None,
    author: Optional[str] = None,
) -> bool:
    """Flesh out a triage task and promote it to ``todo``.

    Atomically updates ``title`` / ``body`` / ``assignee`` (when provided)
    and transitions ``status: triage -> todo`` in a single write txn. Returns
    False when the task is missing or not in the ``triage`` column — callers
    should surface that as "nothing to specify" rather than an error.

    ``todo`` (not ``ready``) is the correct landing column: ``recompute_ready``
    promotes parent-free / parent-done todos to ``ready`` on the next
    dispatcher tick, which keeps the normal parent-gating behaviour intact
    for specified tasks that happen to have open parents.

    ``author`` is recorded on an audit comment only when at least one of
    ``title`` / ``body`` / ``assignee`` actually changed — avoids noisy
    comment spam for status-only promotions.
    """
    if title is not None and not title.strip():
        raise ValueError("title cannot be blank")
    assignee = _canonical_assignee(assignee)
    with write_txn(conn):
        existing = conn.execute(
            "SELECT title, body, assignee FROM tasks WHERE id = ? AND status = 'triage'",
            (task_id,),
        ).fetchone()
        if existing is None:
            return False
        sets: list[str] = ["status = 'todo'"]
        params: list[Any] = []
        changed_fields: list[str] = []
        if title is not None and title.strip() != (existing["title"] or ""):
            sets.append("title = ?")
            params.append(title.strip())
            changed_fields.append("title")
        if body is not None and (body or "") != (existing["body"] or ""):
            sets.append("body = ?")
            params.append(body)
            changed_fields.append("body")
        if assignee is not None and assignee != (existing["assignee"] or None):
            sets.append("assignee = ?")
            params.append(assignee)
            changed_fields.append("assignee")
        params.append(task_id)
        cur = conn.execute(
            f"UPDATE tasks SET {', '.join(sets)} "
            f"WHERE id = ? AND status = 'triage'",
            tuple(params),
        )
        if cur.rowcount != 1:
            return False
        if changed_fields and author and author.strip():
            # Inline INSERT (rather than ``add_comment``) because we're
            # already inside this function's write_txn — nested BEGIN
            # IMMEDIATE would raise OperationalError. We also skip the
            # 'commented' event that ``add_comment`` emits, since the
            # 'specified' event below already records the change.
            conn.execute(
                "INSERT INTO task_comments (task_id, author, body, created_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    task_id,
                    author.strip(),
                    "Specified — updated "
                    + ", ".join(changed_fields)
                    + " and promoted to todo.",
                    int(time.time()),
                ),
            )
        _append_event(
            conn,
            task_id,
            "specified",
            {"changed_fields": changed_fields} if changed_fields else None,
        )
    # Outside the write_txn above, so we don't nest BEGIN IMMEDIATE — the
    # ready-promotion pass opens its own IMMEDIATE txn. This runs the same
    # logic the dispatcher would on its next tick, so a specified task
    # with no open parents flips straight to 'ready' here instead of
    # idling in 'todo' until the next sweep.
    recompute_ready(conn)
    return True


def decompose_triage_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    root_assignee: Optional[str],
    children: list[dict],
    author: Optional[str] = None,
    auto_promote: bool = True,
) -> Optional[list[str]]:
    """Fan a triage task out into child tasks and promote the root to ``todo``.

    The root task stays alive and becomes the parent of every child —
    when all children reach ``done``, the root promotes to ``ready`` and
    its assignee (typically the orchestrator profile) wakes back up to
    judge completion or spawn more work.

    ``children`` is a list of dicts, each shaped like::

        {
            "title": "...",
            "body": "...",                     # optional
            "assignee": "profile-name",        # optional, None -> default fallback
            "parents": [0, 2],                 # indices into this same children list
        }

    Returns the list of created child task ids (in input order) on
    success. Returns ``None`` when:
      - The root task does not exist
      - The root task is not in ``triage``
      - A cycle would result (caller built a bad graph)

    Validation of titles/assignees happens inside the same write_txn as
    the inserts so a malformed entry aborts the whole decomposition
    cleanly (no orphan children).
    """
    if not children:
        return None
    gate = get_architecture_gate_for_task(conn, task_id)
    if gate is not None and gate.enforcement_mode in ARCHITECTURE_GATE_ENFORCING_MODES:
        if gate.state == "human_approved":
            issued = conn.execute(
                "SELECT 1 FROM architecture_graph_issuances WHERE gate_id = ?", (gate.gate_id,)
            ).fetchone()
            if issued is not None:
                raise ArchitectureGateError("architecture_graph_issued")
            raise ArchitectureGateError("architecture_graph_issuance_required")
        raise ArchitectureGateError(ARCHITECTURE_GATE_REASON_OPEN)
    if root_assignee is not None:
        root_assignee = _canonical_assignee(root_assignee)

    # Pre-validate the children list shape outside the txn. Cheap checks
    # that don't need DB access. Bad input aborts before we touch the DB.
    for idx, child in enumerate(children):
        if not isinstance(child, dict):
            raise ValueError(f"child[{idx}] is not a dict")
        title = child.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ValueError(f"child[{idx}].title is required")
        parents_idx = child.get("parents") or []
        if not isinstance(parents_idx, list):
            raise ValueError(f"child[{idx}].parents must be a list")
        for p in parents_idx:
            if not isinstance(p, int) or p < 0 or p >= len(children):
                raise ValueError(
                    f"child[{idx}].parents[{p}] is not a valid index into children"
                )
            if p == idx:
                raise ValueError(f"child[{idx}] cannot list itself as a parent")

    # Detect cycles in the sibling parent graph (Kahn's topological sort).
    # link_tasks() calls _would_cycle() for every new edge; here we check
    # the entire sibling graph before touching the DB.  A cycle silently
    # deadlocks every involved child in 'todo' because recompute_ready()
    # can never promote them.
    _in_deg = [0] * len(children)
    _adj: list[list[int]] = [[] for _ in range(len(children))]
    for _i, _c in enumerate(children):
        for _p in (_c.get("parents") or []):
            _adj[_p].append(_i)
            _in_deg[_i] += 1
    _queue = [_i for _i in range(len(children)) if _in_deg[_i] == 0]
    _seen = 0
    while _queue:
        _node = _queue.pop()
        _seen += 1
        for _nb in _adj[_node]:
            _in_deg[_nb] -= 1
            if _in_deg[_nb] == 0:
                _queue.append(_nb)
    if _seen != len(children):
        raise ValueError("cyclic dependency detected in decomposed children list")

    # We do the full decomposition in a SINGLE write_txn so it's
    # atomic: either every child is created AND the root flips to
    # ``todo``, or nothing changes. We deliberately do NOT call any
    # kb helper that opens its own write_txn (create_task, link_tasks,
    # add_comment) from inside this block — see architecture.md
    # write_txn pitfalls. Instead we inline the INSERTs and
    # _append_event calls.
    now = int(time.time())
    child_ids: list[str] = []
    with write_txn(conn):
        root_row = conn.execute(
            "SELECT id, status, tenant, workspace_kind, workspace_path "
            "FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if root_row is None:
            return None
        if root_row["status"] != "triage":
            return None
        tenant = root_row["tenant"]
        # Children inherit the root's workspace by default so a fan-out
        # of a code-gen task lands in the parent's project dir/worktree
        # rather than throwaway scratch tmp dirs. A child dict can still
        # override with its own 'workspace_kind' / 'workspace_path'.
        root_ws_kind = root_row["workspace_kind"] or "scratch"
        root_ws_path = root_row["workspace_path"]

        # Create children. Status is 'todo' regardless of parents — we
        # link them under the root AFTER creation so the dispatcher
        # sees a coherent state, and recompute_ready() at the end
        # promotes parent-free children to 'ready'.
        for idx, child in enumerate(children):
            new_id = _new_task_id()
            title = child["title"].strip()
            body = child.get("body")
            assignee = _canonical_assignee(child.get("assignee"))
            # Per-child override wins; otherwise inherit the root's
            # workspace. A child that sets workspace_kind without a path
            # falls back to the root path only when kinds match (so a
            # child can't accidentally point a 'dir' at the root's
            # worktree path or vice versa).
            child_ws_kind = child.get("workspace_kind") or root_ws_kind
            if child.get("workspace_path"):
                child_ws_path = child.get("workspace_path")
            elif child_ws_kind == root_ws_kind:
                child_ws_path = root_ws_path
            else:
                child_ws_path = None
            conn.execute(
                "INSERT INTO tasks "
                "(id, title, body, assignee, status, workspace_kind, "
                " workspace_path, tenant, created_at, created_by) "
                "VALUES (?, ?, ?, ?, 'todo', ?, ?, ?, ?, ?)",
                (
                    new_id,
                    title,
                    body if isinstance(body, str) else None,
                    assignee,
                    child_ws_kind,
                    child_ws_path,
                    tenant,
                    now,
                    (author or "decomposer"),
                ),
            )
            _append_event(
                conn, new_id, "created",
                {"by": author or "decomposer", "from_decompose_of": task_id},
            )
            child_ids.append(new_id)

        # Link children to their sibling parents (within the decomposed graph).
        for idx, child in enumerate(children):
            for p_idx in child.get("parents") or []:
                parent_id = child_ids[p_idx]
                child_id = child_ids[idx]
                conn.execute(
                    "INSERT OR IGNORE INTO task_links (parent_id, child_id) "
                    "VALUES (?, ?)",
                    (parent_id, child_id),
                )
                _append_event(
                    conn, child_id, "linked",
                    {"parent": parent_id, "child": child_id},
                )

        # Link the ROOT task as a child of every leaf child — i.e. the
        # root waits for the whole graph. Simpler than computing leaves:
        # link root under every child. Cycle-free because the root is
        # only ever a child here, never a parent of children.
        for cid in child_ids:
            conn.execute(
                "INSERT OR IGNORE INTO task_links (parent_id, child_id) "
                "VALUES (?, ?)",
                (cid, task_id),
            )

        # Flip the root: triage -> todo, set assignee to the orchestrator.
        sets = ["status = 'todo'"]
        params: list[Any] = []
        if root_assignee is not None:
            sets.append("assignee = ?")
            params.append(root_assignee)
        params.append(task_id)
        conn.execute(
            f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?",
            tuple(params),
        )

        # Audit comment + event on the root so the timeline shows the fan-out.
        if author and author.strip():
            conn.execute(
                "INSERT INTO task_comments (task_id, author, body, created_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    task_id,
                    author.strip(),
                    "Decomposed into "
                    + ", ".join(child_ids)
                    + ". Root will wake when all children complete.",
                    now,
                ),
            )
        _append_event(
            conn, task_id, "decomposed",
            {
                "child_ids": child_ids,
                "root_assignee": root_assignee,
            },
        )

    # Outside the write_txn: promote parent-free children to 'ready'
    # so the dispatcher picks them up on its next tick. Same pattern
    # specify_triage_task uses.  When auto_promote is False children
    # stay in 'todo' until the user manually promotes them — useful
    # for manual-review-first workflows.
    if auto_promote:
        recompute_ready(conn)
    else:
        # Manual-promote mode: parent-free entry children would otherwise be
        # trivially promoted by the very next recompute_ready tick (all-parents-
        # done is vacuously true for a parent-free task). Emit a persistent
        # ``promotion_gated`` event on each so ``_awaiting_manual_promotion``
        # holds them in 'todo' across every dispatcher tick until an operator
        # ``promote_task`` (which emits ``"promoted_manual"``) releases them.
        # Children WITH sibling parents need no gate: they're already blocked
        # by their unfinished dependencies. _append_event needs an open txn.
        gated = [
            child_ids[idx]
            for idx in range(len(children))
            if not (children[idx].get("parents"))
        ]
        if gated:
            with write_txn(conn):
                for child_id in gated:
                    _append_event(
                        conn,
                        child_id,
                        "promotion_gated",
                        {"reason": "auto_promote_children=false", "root": task_id},
                    )
    return child_ids


def archive_task(conn: sqlite3.Connection, task_id: str) -> bool:
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET status = 'archived', "
            "    claim_lock = NULL, claim_expires = NULL, worker_pid = NULL, "
            "    worker_started_at = NULL, worker_pgid = NULL, worker_sid = NULL "
            "WHERE id = ? AND status != 'archived'",
            (task_id,),
        )
        if cur.rowcount != 1:
            return False
        # If archive happened while a run was still in flight (e.g. user
        # archived a running task from the dashboard), close that run with
        # outcome='reclaimed' so attempt history isn't orphaned.
        run_id = _end_run(
            conn, task_id,
            outcome="reclaimed", status="reclaimed",
            summary="task archived with run still active",
        )
        _append_event(conn, task_id, "archived", None, run_id=run_id)
    # ``archived`` parents no longer block children, same as ``done``.
    # Promote newly-unblocked dependents immediately instead of waiting
    # for a later dispatcher tick.
    recompute_ready(conn)
    cleanup_terminal_task_worktrees(conn)
    return True


def delete_archived_task(conn: sqlite3.Connection, task_id: str) -> bool:
    """Permanently remove an already-archived task and its related rows.

    Safety guard: only archived tasks can be deleted. Active / blocked / done
    tasks must be explicitly archived first so accidental data loss requires a
    second deliberate action.
    """
    with write_txn(conn):
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if not row or row["status"] != "archived":
            return False
        conn.execute(
            "DELETE FROM task_links WHERE parent_id = ? OR child_id = ?",
            (task_id, task_id),
        )
        conn.execute("DELETE FROM task_comments WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM task_events WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM task_runs WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM kanban_notify_subs WHERE task_id = ?", (task_id,))
        cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        return cur.rowcount == 1


def delete_task(conn: sqlite3.Connection, task_id: str) -> bool:
    """Hard-delete a task and cascade to all related rows.

    Because the schema does not use ``ON DELETE CASCADE`` foreign keys,
    we explicitly delete from child tables first, then the task row.
    This keeps the operation atomic (single ``write_txn``).

    Returns ``True`` if the task existed and was deleted, ``False``
    if the task was not found.
    """
    with write_txn(conn):
        cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        if cur.rowcount != 1:
            return False
        conn.execute("DELETE FROM task_links WHERE parent_id = ? OR child_id = ?", (task_id, task_id))
        conn.execute("DELETE FROM task_comments WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM task_events WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM task_runs WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM kanban_notify_subs WHERE task_id = ?", (task_id,))
    recompute_ready(conn)
    return True


# ---------------------------------------------------------------------------
# Workspace resolution
# ---------------------------------------------------------------------------

def _git_toplevel(path: Path) -> Optional[Path]:
    """Return the git toplevel containing ``path``, or ``None`` if not in a repo."""
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    out = (result.stdout or "").strip()
    if not out:
        return None
    try:
        return Path(out).expanduser().resolve()
    except Exception:
        return Path(out).expanduser()


def _git_branch_exists(repo_root: Path, branch_name: str) -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "show-ref", "--verify", f"refs/heads/{branch_name}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except Exception:
        return False
    return result.returncode == 0


def _git_common_dir(path: Path) -> Optional[Path]:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    out = (result.stdout or "").strip()
    if not out:
        return None
    return Path(out).expanduser().resolve(strict=False)


def _git_dir(path: Path) -> Optional[Path]:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--path-format=absolute", "--git-dir"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    out = (result.stdout or "").strip()
    if not out:
        return None
    return Path(out).expanduser().resolve(strict=False)


def _git_current_branch(path: Path) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "branch", "--show-current"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    branch = (result.stdout or "").strip()
    return branch or None


def _is_linked_worktree_checkout(path: Path) -> bool:
    git_dir = _git_dir(path)
    common_dir = _git_common_dir(path)
    if git_dir is None or common_dir is None:
        return False
    return git_dir != common_dir


def _nearest_existing_path(path: Path) -> Path:
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def _repo_root_for_worktree_target(path: Path) -> Optional[Path]:
    current = _nearest_existing_path(path).resolve(strict=False)
    while True:
        repo_root = _git_toplevel(current)
        if repo_root is not None:
            return repo_root
        if current == current.parent:
            return None
        current = current.parent


def _worktree_anchor_repo_root(path: Path) -> Optional[Path]:
    """Return the repository containing a worktree target, if any.

    The target itself may not exist yet: worktrees are materialized lazily.
    Keep the repo-root special case and containing-repo lookup in one helper so
    creation-time validation and dispatch use the same anchor contract.
    """
    requested = path.expanduser()
    requested_resolved = requested.resolve(strict=False)
    repo_root = _git_toplevel(requested)
    if repo_root is not None and requested_resolved == repo_root:
        return repo_root
    return _repo_root_for_worktree_target(requested.parent)


def _exclude_managed_worktree_container(repo_root: Path, target: Path) -> None:
    """Hide Hermes's in-repo worktree container from source status output."""
    try:
        relative = target.resolve(strict=False).relative_to(repo_root.resolve(strict=False))
    except ValueError:
        return
    if not relative.parts or relative.parts[0] != ".worktrees":
        return
    common_dir = _git_common_dir(repo_root)
    if common_dir is None:
        return
    exclude_path = common_dir / "info" / "exclude"
    try:
        existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
        if any(line.strip() == ".worktrees/" for line in existing.splitlines()):
            return
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        prefix = "" if not existing or existing.endswith("\n") else "\n"
        with exclude_path.open("a", encoding="utf-8") as handle:
            handle.write(prefix + ".worktrees/\n")
    except OSError:
        # Status cleanliness is helpful but not required for recoverability.
        pass


def _create_git_checkpoint(
    repo_root: Path,
    *,
    checkpoint_key: str,
) -> tuple[str, str]:
    """Snapshot tracked + non-ignored untracked WIP through an alternate index."""
    safe_key = re.sub(r"[^A-Za-z0-9._-]+", "-", checkpoint_key).strip("-.")
    if not safe_key:
        raise ValueError("checkpoint key has no safe git-ref characters")
    checkpoint_ref = f"refs/hermes/checkpoints/{safe_key}"
    temp_dir = Path(tempfile.mkdtemp(prefix="hermes-kanban-index-"))
    env = dict(os.environ)
    env["GIT_INDEX_FILE"] = str(temp_dir / "index")
    env["GIT_AUTHOR_NAME"] = "Hermes Kanban"
    env["GIT_AUTHOR_EMAIL"] = "hermes-kanban@localhost"
    env["GIT_COMMITTER_NAME"] = env["GIT_AUTHOR_NAME"]
    env["GIT_COMMITTER_EMAIL"] = env["GIT_AUTHOR_EMAIL"]

    def run(*args: str, input_text: Optional[str] = None) -> str:
        result = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            input=input_text,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            env=env,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
        return (result.stdout or "").strip()

    try:
        head_result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--verify", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        head = (head_result.stdout or "").strip() if head_result.returncode == 0 else None
        if head:
            run("read-tree", head)
        else:
            run("read-tree", "--empty")
        # The alternate index captures the source exactly while leaving the
        # user's real index, branch, and files untouched. Git's ignore rules
        # remain authoritative; the managed worktree container is never data.
        run("add", "-A", "--", ".")
        tree = run("write-tree")
        commit_args = ["commit-tree", tree]
        if head:
            commit_args.extend(["-p", head])
        commit_args.extend(["-m", f"Hermes recoverable checkpoint {safe_key}"])
        checkpoint_sha = run(*commit_args)
        run("update-ref", checkpoint_ref, checkpoint_sha)
        return checkpoint_ref, checkpoint_sha
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _existing_checkpoint(
    repo_root: Path,
    checkpoint_key: str,
) -> tuple[Optional[str], Optional[str]]:
    safe_key = re.sub(r"[^A-Za-z0-9._-]+", "-", checkpoint_key).strip("-.")
    if not safe_key:
        return None, None
    checkpoint_ref = f"refs/hermes/checkpoints/{safe_key}"
    result = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "--verify", checkpoint_ref],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    checkpoint_sha = (result.stdout or "").strip()
    if result.returncode != 0 or not checkpoint_sha:
        return None, None
    return checkpoint_ref, checkpoint_sha


def _ensure_git_worktree(
    repo_root: Path,
    target: Path,
    branch_name: str,
    *,
    checkpoint_key: str,
) -> tuple[Optional[str], Optional[str], bool]:
    """Materialize a linked worktree from a recoverable source checkpoint."""
    target = target.expanduser()
    branch_check = subprocess.run(
        ["git", "check-ref-format", "--branch", branch_name],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if branch_check.returncode != 0:
        raise ValueError(f"invalid worktree branch name: {branch_name!r}")
    repo_common = _git_common_dir(repo_root)
    if target.exists() and repo_common is not None:
        target_common = _git_common_dir(target)
        if target_common == repo_common:
            checkpoint_ref, checkpoint_sha = _existing_checkpoint(repo_root, checkpoint_key)
            return checkpoint_ref, checkpoint_sha, False
    _exclude_managed_worktree_container(repo_root, target)
    target.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_ref: Optional[str] = None
    checkpoint_sha: Optional[str] = None
    if _git_branch_exists(repo_root, branch_name):
        cmd = ["git", "-C", str(repo_root), "worktree", "add", str(target), branch_name]
        checkpoint_ref, checkpoint_sha = _existing_checkpoint(
            repo_root, checkpoint_key,
        )
    else:
        checkpoint_ref, checkpoint_sha = _create_git_checkpoint(
            repo_root,
            checkpoint_key=checkpoint_key,
        )
        cmd = [
            "git", "-C", str(repo_root), "worktree", "add", "-b", branch_name,
            str(target), checkpoint_sha,
        ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        stderr = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(
            f"git worktree add failed for {target} on branch {branch_name}: {stderr}"
        )
    return checkpoint_ref, checkpoint_sha, True


def _persist_worktree_materialization(
    conn: sqlite3.Connection,
    task: Task,
    path: Path,
    *,
    managed: bool,
    repo_root: Path,
    repo_common_dir: Path,
) -> None:
    """Persist the exact worktree ownership and repository identity."""

    task.workspace_path = str(path.resolve(strict=False))
    task.workspace_managed = bool(managed)
    task.workspace_repo_root = str(repo_root.resolve(strict=False))
    task.workspace_repo_common_dir = str(repo_common_dir.resolve(strict=False))
    with write_txn(conn):
        conn.execute(
            "UPDATE tasks SET workspace_path = ?, workspace_managed = ?, "
            "workspace_repo_root = ?, workspace_repo_common_dir = ? "
            "WHERE id = ?",
            (
                task.workspace_path,
                int(task.workspace_managed),
                task.workspace_repo_root,
                task.workspace_repo_common_dir,
                task.id,
            ),
        )


def _worktree_materialization_identity(
    path: Path,
    *,
    fallback_repo_root: Path | None = None,
) -> tuple[Path, Path]:
    common_dir = _git_common_dir(path)
    if common_dir is None:
        raise WorkspaceContractError(
            "worktree_bad_anchor",
            f"materialized worktree {path} has no validated Git common directory",
        )
    repo_root = fallback_repo_root.resolve(strict=False) if fallback_repo_root else None
    if repo_root is None or _git_common_dir(repo_root) != common_dir:
        repo_root = common_dir.parent.resolve(strict=False)
    if _git_common_dir(repo_root) != common_dir:
        raise WorkspaceContractError(
            "worktree_bad_anchor",
            f"materialized worktree {path} has an unverifiable repository root",
        )
    return repo_root, common_dir


def _resolve_worktree_workspace(
    task: Task,
    *,
    board: Optional[str] = None,
    conn: sqlite3.Connection | None = None,
) -> tuple[Path, str, Optional[str], Optional[str]]:
    """Resolve + materialize a linked git worktree for ``task``.

    When ``task.workspace_path`` is unset, the anchor is the board's
    ``default_workdir`` (a persistent project checkout). This keeps every
    worktree task under a meaningful, board-owned repo — ``<repo>/.worktrees/
    <task-id>`` — instead of silently landing under the dispatcher's current
    working directory (which is whatever directory the gateway happened to be
    launched from, e.g. the Hermes checkout). If no anchor is configured
    anywhere, we fail loudly rather than guess.
    """
    def _finish(
        path: Path,
        resolved_branch: str,
        checkpoint_ref: Optional[str],
        checkpoint_sha: Optional[str],
        *,
        managed: bool,
        repo_root: Path,
    ) -> tuple[Path, str, Optional[str], Optional[str]]:
        identity_root, common_dir = _worktree_materialization_identity(
            path,
            fallback_repo_root=repo_root,
        )
        # Reusing a worktree Hermes previously created must retain ownership,
        # while a task whose requested path changed must not carry ownership
        # over to an unrelated checkout.
        previous_path: Path | None = None
        previous_common_dir: Path | None = None
        if task.workspace_managed and task.workspace_path:
            try:
                previous_path = Path(task.workspace_path).expanduser().resolve(strict=False)
                previous_common_dir = (
                    Path(task.workspace_repo_common_dir).expanduser().resolve(strict=False)
                    if task.workspace_repo_common_dir
                    else None
                )
            except (OSError, RuntimeError, ValueError):
                previous_path = None
                previous_common_dir = None
        retained_ownership = (
            previous_path == path.resolve(strict=False)
            and previous_common_dir == common_dir
        )
        effective_managed = bool(managed or retained_ownership)
        if conn is not None:
            _persist_worktree_materialization(
                conn,
                task,
                path,
                managed=effective_managed,
                repo_root=identity_root,
                repo_common_dir=common_dir,
            )
        else:
            task.workspace_path = str(path.resolve(strict=False))
            task.workspace_managed = effective_managed
            task.workspace_repo_root = str(identity_root)
            task.workspace_repo_common_dir = str(common_dir)
        return path, resolved_branch, checkpoint_ref, checkpoint_sha

    branch_name = (task.branch_name or "").strip() or f"wt/{task.id}"
    if not task.workspace_path:
        # Anchor on the board's configured default_workdir, not Path.cwd().
        # The dispatcher's CWD is incidental (gateway launch dir) and using it
        # scatters worktrees under whatever repo the gateway started in.
        board_slug = board if board else get_current_board()
        board_default = (read_board_metadata(board_slug).get("default_workdir") or "").strip()
        if not board_default:
            # Deterministic contract violation (BUILD-496 invariant 7): a
            # legacy row that slipped past creation-time validation. Typed so
            # dispatch blocks it instead of counting a retryable spawn failure.
            raise WorkspaceContractError(
                "worktree_no_anchor",
                f"task {task.id} has workspace_kind=worktree but no workspace_path, "
                f"and board {board_slug!r} has no default_workdir set. Set a board "
                "default workdir (a git repo) or create the task with "
                "--workspace worktree:<absolute-repo-path>.",
            )
        anchor = Path(board_default).expanduser()
        if not anchor.is_absolute():
            raise WorkspaceContractError(
                "worktree_bad_anchor",
                f"board {board_slug!r} default_workdir {board_default!r} is not "
                "absolute; use an absolute path to a git repo",
            )
        repo_root = _git_toplevel(anchor)
        if repo_root is None:
            raise WorkspaceContractError(
                "worktree_bad_anchor",
                f"task {task.id} has workspace_kind=worktree but board "
                f"{board_slug!r} default_workdir {board_default!r} is not inside a git repo",
            )
        target = repo_root / ".worktrees" / task.id
        checkpoint_ref, checkpoint_sha, created = _ensure_git_worktree(
            repo_root, target, branch_name, checkpoint_key=task.id,
        )
        return _finish(
            target,
            branch_name,
            checkpoint_ref,
            checkpoint_sha,
            managed=created,
            repo_root=repo_root,
        )

    requested = Path(task.workspace_path).expanduser()
    if not requested.is_absolute():
        raise WorkspaceContractError(
            "worktree_bad_anchor",
            f"task {task.id} has non-absolute worktree path "
            f"{task.workspace_path!r}; use an absolute path",
        )
    requested_resolved = requested.resolve(strict=False)

    if requested.exists() and _is_linked_worktree_checkout(requested):
        actual_branch = _git_current_branch(requested)
        repo_root, _common_dir = _worktree_materialization_identity(requested_resolved)
        return _finish(
            requested_resolved,
            actual_branch or branch_name,
            None,
            None,
            managed=False,
            repo_root=repo_root,
        )

    repo_root = _worktree_anchor_repo_root(requested)
    if repo_root is not None and requested_resolved == repo_root:
        target = repo_root / ".worktrees" / task.id
        checkpoint_ref, checkpoint_sha, created = _ensure_git_worktree(
            repo_root, target, branch_name, checkpoint_key=task.id,
        )
        return _finish(
            target,
            branch_name,
            checkpoint_ref,
            checkpoint_sha,
            managed=created,
            repo_root=repo_root,
        )

    if repo_root is None:
        raise WorkspaceContractError(
            "worktree_bad_anchor",
            f"task {task.id} worktree path {task.workspace_path!r} is not inside a git repo "
            "and does not point at a git repo root",
        )
    checkpoint_ref, checkpoint_sha, created = _ensure_git_worktree(
        repo_root, requested, branch_name, checkpoint_key=task.id,
    )
    return _finish(
        requested,
        branch_name,
        checkpoint_ref,
        checkpoint_sha,
        managed=created,
        repo_root=repo_root,
    )


def resolve_workspace(
    task: Task,
    *,
    board: Optional[str] = None,
    conn: sqlite3.Connection | None = None,
) -> Path:
    """Resolve (and create if needed) the workspace for a task.

    - ``scratch``: a fresh dir under ``<board-root>/workspaces/<id>/``,
      where ``<board-root>`` is the active board's root. The path is the
      same for the dispatcher and every profile worker, so handoff is
      path-stable.
    - ``dir:<path>``: the path stored in ``workspace_path``.  Created
      if missing.  MUST be absolute — relative paths are rejected to
      prevent confused-deputy traversal where ``../../../tmp/attacker``
      resolves against the dispatcher's CWD instead of a meaningful
      root.  Users who want a kanban-root-relative workspace should
      compute the absolute path themselves.
    - ``worktree``: a real linked git worktree. If ``workspace_path`` names
      a repo root, Hermes treats it as an anchor and materializes a linked
      worktree at ``<repo>/.worktrees/<task-id>``. If ``workspace_path`` names
      a concrete target path, Hermes creates/reuses that linked worktree. With
      no ``workspace_path``, Hermes anchors on the board's ``default_workdir``
      and materializes ``<repo>/.worktrees/<task-id>`` per task; if no
      ``default_workdir`` is configured it raises rather than guessing from the
      dispatcher's CWD. When ``branch_name`` is empty, Hermes uses
      ``wt/<task-id>``.

    Persist the resolved path back to the task row via ``set_workspace_path``
    so subsequent runs reuse the same directory.
    """
    kind = task.workspace_kind or "scratch"
    if kind == "scratch":
        if task.workspace_path:
            # Legacy scratch tasks that were set to an explicit path get the
            # same absolute-path guard as dir: — consistent with the
            # threat model.
            p = Path(task.workspace_path).expanduser()
            if not p.is_absolute():
                raise ValueError(
                    f"task {task.id} has non-absolute workspace_path "
                    f"{task.workspace_path!r}; workspace paths must be absolute"
                )
        else:
            p = workspaces_root(board=board) / task.id
        p.mkdir(parents=True, exist_ok=True)
        return p
    if kind == "dir":
        if not task.workspace_path:
            raise ValueError(
                f"task {task.id} has workspace_kind=dir but no workspace_path"
            )
        p = Path(task.workspace_path).expanduser()
        if not p.is_absolute():
            raise ValueError(
                f"task {task.id} has non-absolute workspace_path "
                f"{task.workspace_path!r}; use an absolute path "
                f"(relative paths are ambiguous against the dispatcher's CWD)"
            )
        p.mkdir(parents=True, exist_ok=True)
        return p
    if kind == "worktree":
        p, _branch_name, _checkpoint_ref, _checkpoint_sha = (
            _resolve_worktree_workspace(task, board=board, conn=conn)
        )
        return p
    raise ValueError(f"unknown workspace_kind: {kind}")


def set_workspace_path(
    conn: sqlite3.Connection, task_id: str, path: Path | str
) -> None:
    with write_txn(conn):
        conn.execute(
            "UPDATE tasks SET workspace_path = ? WHERE id = ?",
            (str(path), task_id),
        )


def set_branch_name(
    conn: sqlite3.Connection, task_id: str, branch_name: str
) -> None:
    with write_txn(conn):
        conn.execute(
            "UPDATE tasks SET branch_name = ? WHERE id = ?",
            (str(branch_name), task_id),
        )


# ---------------------------------------------------------------------------
def schedule_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    reason: Optional[str] = None,
    expected_run_id: Optional[int] = None,
) -> bool:
    """Park a task in ``scheduled`` so it is waiting on time, not human input.

    ``scheduled`` tasks are intentionally not dispatchable; an external cron,
    human action, or automation can later call ``unblock_task`` to re-gate them
    to ``ready`` (or ``todo`` if parents are still incomplete).
    """
    with write_txn(conn):
        params: list[Any] = [task_id]
        sql = """
            UPDATE tasks
               SET status       = 'scheduled',
                   claim_lock   = NULL,
                   claim_expires= NULL,
                   worker_pid   = NULL,
                   worker_started_at = NULL,
                   worker_pgid = NULL,
                   worker_sid = NULL
             WHERE id = ?
               AND status IN ('todo', 'ready', 'running', 'blocked')
        """
        if expected_run_id is not None:
            sql += " AND current_run_id = ?"
            params.append(int(expected_run_id))
        cur = conn.execute(sql, params)
        if cur.rowcount != 1:
            return False
        run_id = _end_run(
            conn, task_id,
            outcome="scheduled", status="scheduled",
            summary=reason,
        )
        if run_id is None and reason:
            run_id = _synthesize_ended_run(
                conn, task_id,
                outcome="scheduled",
                summary=reason,
            )
        _append_event(conn, task_id, "scheduled", {"reason": reason}, run_id=run_id)
        return True


# Dispatcher (one-shot pass)
# ---------------------------------------------------------------------------

# After this many consecutive non-success attempts on a task/profile, the
# dispatcher stops retrying and parks the task in ``blocked`` with a reason so
# a human can investigate. Prevents retry storms when a worker repeatedly times
# out, crashes, or cannot spawn.
DEFAULT_FAILURE_LIMIT = 2
# Legacy alias — callers / tests still reference the old name.
DEFAULT_SPAWN_FAILURE_LIMIT = DEFAULT_FAILURE_LIMIT

# Max bytes to keep in a single worker log file. The dispatcher truncates
# and rotates on spawn if the file is larger than this at spawn time.
DEFAULT_LOG_ROTATE_BYTES = 2 * 1024 * 1024   # 2 MiB
DEFAULT_LOG_BACKUP_COUNT = 1

# Keep a little wall-clock budget for the worker to observe a terminal timeout
# and call kanban_block/kanban_complete before max_runtime_seconds kills it.
KANBAN_TERMINAL_TIMEOUT_GRACE_SECONDS = 30

# ---------------------------------------------------------------------------
# Respawn guard constants
# ---------------------------------------------------------------------------

# Patterns in last_failure_error that indicate a quota / auth blocker.
# These errors won't resolve by retrying immediately — auto-block instead.
_RESPAWN_BLOCKER_RE = re.compile(
    r"\b(quota|rate[\s_\-]?limit|429|403|auth\w*|"
    r"unauthorized|forbidden|billing|subscription|"
    r"access[\s_]denied|permission[\s_]denied|"
    r"invalid[\s_]api[\s_]key)\b",
    re.IGNORECASE,
)

# Within this window a completed run counts as "recent proof"; don't re-spawn.
_RESPAWN_GUARD_SUCCESS_WINDOW = 3600  # 1 hour

# Cooldown after a rate-limited (quota-wall) requeue before the dispatcher
# re-spawns the worker. Without this, a task released by the rate-limit path
# would be re-spawned on the very next tick and immediately bounce off the
# same quota wall, burning a worker slot every tick for hours. The cooldown
# spaces retries out so the board keeps cheaply probing whether quota is back
# without thrashing. Overridable via ``HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS``
# for operators who want a tighter/looser probe cadence.
DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS = 300  # 5 minutes
DEFAULT_DELIVERY_AUTHORIZATION_COOLDOWN_SECONDS = 30


def _resolve_delivery_authorization_cooldown_seconds() -> int:
    """Return the retry delay after a delivery-authority lookup outage."""
    raw = os.environ.get(
        "HERMES_KANBAN_DELIVERY_AUTHORIZATION_COOLDOWN_SECONDS", ""
    ).strip()
    if raw:
        try:
            parsed = int(raw)
        except ValueError:
            parsed = -1
        if parsed >= 0:
            return parsed
    return DEFAULT_DELIVERY_AUTHORIZATION_COOLDOWN_SECONDS


@dataclass
class SpawnReceipt:
    """A started worker that remains gated until its run is durably attached.

    ``release`` lets the child proceed only after the dispatcher has stored
    and read back the exact task/run/claim/PID tuple. ``abort`` must terminate
    the process group when attach or gate release fails.
    """

    pid: int
    release: Callable[[], None] = field(repr=False)
    abort: Callable[[], None] = field(repr=False)
    process_started_at: Optional[float] = None
    process_group_id: Optional[int] = None
    session_id: Optional[int] = None


def _publish_start_gate_token(gate_path: Path, gate_token: str) -> None:
    """Publish a complete gate token atomically without replacing a peer.

    The worker treats the final path as the commit record.  Building the token
    under a private sibling name and linking it into place only after ``fsync``
    prevents readers from observing the create-before-write window.
    """
    parent = gate_path.parent
    temp_name = f".{gate_path.name}.{secrets.token_hex(8)}.tmp"
    temp_path = parent / temp_name
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    fd = os.open(temp_path, flags, 0o600)
    try:
        payload = gate_token.encode("utf-8")
        offset = 0
        while offset < len(payload):
            written = os.write(fd, payload[offset:])
            if written <= 0:
                raise OSError("failed to write worker start gate token")
            offset += written
        os.fsync(fd)
        os.close(fd)
        fd = -1
        # A hard-link publication is atomic and, unlike replace(), refuses to
        # overwrite a path an unexpected peer managed to create first.
        os.link(temp_path, gate_path)
        try:
            dir_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except OSError:
            dir_fd = None
        if dir_fd is not None:
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


@dataclass
class DispatchResult:
    """Outcome of a single ``dispatch`` pass."""

    reclaimed: int = 0
    promoted: int = 0
    dependency_reconciled: DependencyReconcileResult = field(
        default_factory=DependencyReconcileResult,
    )
    dependency_links_restored: int = 0
    dependency_waits_materialized: int = 0
    dependency_waits_rearmed: int = 0
    dependency_legacy_recovered: int = 0
    dependency_waits_timed_out: int = 0
    review_artifacts_backfilled: int = 0
    review_artifact_selections_required: int = 0
    spawned: list[tuple[str, str, str]] = field(default_factory=list)
    """List of ``(task_id, assignee, workspace_path)`` triples."""
    skipped_unassigned: list[str] = field(default_factory=list)
    """Ready task ids skipped because they have no assignee at all.
    Operator-actionable — usually a misfiled task waiting for routing."""
    auto_assigned_default: list[str] = field(default_factory=list)
    """Task ids that were unassigned in the DB and had
    ``kanban.default_assignee`` applied this tick before spawning (#27145).
    Surfaces the auto-assignment to telemetry / CLI / dashboard so the
    operator can see when the dispatcher is acting on the fallback rule
    rather than on explicit per-task assignments."""
    skipped_nonspawnable: list[str] = field(default_factory=list)
    """Ready task ids skipped because their assignee names a control-plane
    lane (a Claude Code terminal like ``orion-cc``) rather than a Hermes
    profile. Expected steady-state on multi-lane setups; NOT an
    operator-actionable failure. Tracked separately so health telemetry
    can distinguish "real stuck" (nothing spawned but spawnable work
    available) from "correctly idle" (nothing spawnable in the queue)."""
    skipped_per_profile_capped: list[tuple[str, str, int]] = field(default_factory=list)
    """Tasks deferred this tick because their assignee is already at
    ``kanban.max_in_progress_per_profile`` (#21582). Each entry is
    ``(task_id, assignee, current_running_count)``. NOT an
    operator-actionable failure — the task will be picked up on a
    subsequent tick when the assignee has capacity. Separate bucket so
    telemetry / dashboards can show "this profile is busy" vs
    "task is genuinely stuck"."""
    crashed: list[str] = field(default_factory=list)
    """Task ids reclaimed because their worker PID disappeared."""
    auto_blocked: list[str] = field(default_factory=list)
    """Task ids auto-blocked by the spawn-failure circuit breaker."""
    timed_out: list[str] = field(default_factory=list)
    """Task ids whose workers exceeded ``max_runtime_seconds``."""
    stale: list[str] = field(default_factory=list)
    """Task ids reclaimed because no progress (heartbeat) was seen
    within ``dispatch_stale_timeout_seconds``."""
    respawn_guarded: list[tuple[str, str]] = field(default_factory=list)
    """Tasks skipped by the respawn guard, as ``(task_id, reason)`` pairs.
    Reasons: ``"blocker_auth"`` (quota/auth error — also auto-blocked) and
    ``"recent_success"`` (completed run within guard window)."""
    circuit_breaker_tripped: list[tuple[str, str]] = field(default_factory=list)
    """Tasks blocked (kind=``needs_input``) by the BUILD-261 release/
    remediation circuit breaker, as ``(task_id, signature)`` pairs. Unlike
    ``respawn_guarded`` (a one-tick defer) and ``auto_blocked`` (the crash/
    timeout counter), this fires when the task's last N recorded failure
    signatures — see :func:`check_failure_signature_breaker` — are all
    identical, meaning retries are not converging. In ``dry_run`` mode the
    task is reported here but NOT actually blocked."""
    workspace_collisions: list[tuple[str, str]] = field(default_factory=list)
    """Tasks skipped because another running task already owns the same
    non-scratch workspace, as ``(task_id, conflict_with_task_id)`` pairs.
    These tasks will be retried on the next tick once the occupying task
    finishes — no operator action needed."""
    rate_limited: list[str] = field(default_factory=list)
    """Task ids whose workers bailed on a provider rate-limit / quota wall
    (EX_TEMPFAIL sentinel exit) and were released back to ``ready`` WITHOUT
    counting a failure. These never trip the circuit breaker — a long quota
    window just makes the task bounce cheaply until the window clears."""
    skipped_locked: bool = False
    """True when this tick was skipped because another process already held
    the board's dispatch lock (issue #35240). A losing dispatcher does no
    DB writes this tick — the lock holder is making progress on the same
    board. This is the steady-state signal that a single-writer guard is
    actively preventing two dispatchers from racing on ``kanban.db``."""
    dirty_workspace: list[str] = field(default_factory=list)
    """Task ids skipped because their workspace was detected as dirty
    (uncommitted/untracked changes in a git repo). These tasks stay
    ``running`` but no worker is spawned — the sentinel can escalate
    or the human can clean the workspace and retry."""
    claim_race: list[str] = field(default_factory=list)
    """Task ids where an atomic claim (``claim_task`` / ``claim_review_task``)
    returned ``None`` — another claimant (a concurrent dispatcher, or a
    terminal pulling the same task directly) won the race between this tick
    reading the row as unclaimed and the ``UPDATE ... WHERE status='ready'``
    actually committing. Not an error: the task stays claimable and is
    retried next tick. Tracked (BUILD-263) so stuck-dispatcher diagnostics
    can distinguish "lost a claim race" from "nothing spawnable"."""
    spawn_errors: list[tuple[str, str]] = field(default_factory=list)
    """``(task_id, error)`` pairs for exceptions raised while resolving a
    task's workspace or invoking ``spawn_fn`` (BUILD-263). These are also
    recorded on the task row via ``_record_spawn_failure`` (which may or may
    not trip the auto-block circuit breaker depending on ``failure_limit``),
    but that path does not emit a log record — callers used to have **no**
    visibility into *why* a tick spawned nothing beyond reading task rows by
    hand. The raise site now also logs the exception (see
    ``_dispatch_once_locked``), so a broken venv/profile/PATH surfaces in
    the gateway/CLI logs immediately instead of being silently swallowed."""
    max_in_progress_deferred: int = 0
    """Count of ready tasks deferred this tick because the board was already
    at ``kanban.max_in_progress`` (BUILD-263). The pre-existing code path
    returned early here with no telemetry at all — a board sitting at its
    concurrency cap looked identical to a broken dispatcher in the "stuck"
    diagnostics. Zero when ``max_in_progress`` is unset or headroom exists."""


# Respawn-guard reasons that mean "a provider quota/auth wall, not a real
# refusal" — bucketed as a flat top-level ``quota`` cause by
# `summarize_dispatch_causes` rather than `respawn_guarded(<reason>)`, since
# operators reach for the same remediation (wait, or fix credentials)
# regardless of which of the two paths stamped it.
_QUOTA_RESPAWN_GUARD_REASONS = frozenset(
    {"blocker_auth", "rate_limit_cooldown", "quota_unavailable_cooldown"}
)
CAPACITY_ONLY_CAUSES = frozenset({"concurrency_cap", "concurrency_cap(per_profile)"})

# Routing steady-states that also must never page an operator. An assignee that
# maps to no spawnable profile -- a human / control-plane lane (``nonspawnable``)
# -- or a task with no assignee at all (``unassigned``) is not a dispatcher
# stall: no worker will EVER spawn these, and a running worker finishing won't
# change that (unlike a capacity deferral, which drains on its own). Both are
# documented as the expected steady state (see summarize_dispatch_causes); a
# zero-spawn streak whose causes are only these -- with or without capacity
# deferrals -- is benign and must be logged, never escalated to Telegram.
ROUTING_STEADY_STATE_CAUSES = frozenset({"nonspawnable", "unassigned"})
BENIGN_CAUSES = CAPACITY_ONLY_CAUSES | ROUTING_STEADY_STATE_CAUSES


@dataclass
class DispatchHealthLogCooldowns:
    """Rate-limit capacity and actionable dispatcher health logs separately."""

    cooldown_seconds: float = 300.0
    last_capacity_at: Optional[float] = None
    last_actionable_at: Optional[float] = None

    def should_emit(self, *, capacity_only: bool, now: float) -> bool:
        attr = "last_capacity_at" if capacity_only else "last_actionable_at"
        last_emitted = getattr(self, attr)
        if (
            last_emitted is not None
            and now - last_emitted < self.cooldown_seconds
        ):
            return False
        setattr(self, attr, now)
        return True


def dispatch_cause_counts(
    results: "Iterable[Optional[DispatchResult]]",
) -> "dict[str, int]":
    """Aggregate per-cause spawn-refusal counts across ``results``.

    ``None`` entries are skipped defensively when a board raises before
    producing a :class:`DispatchResult`.
    """
    counts: "dict[str, int]" = {}

    def _bump(key: str, n: int = 1) -> None:
        if n:
            counts[key] = counts.get(key, 0) + n

    for result in results:
        if result is None:
            continue
        for _tid, reason in result.respawn_guarded:
            if reason in _QUOTA_RESPAWN_GUARD_REASONS:
                _bump("quota")
            else:
                _bump(f"respawn_guarded({reason})")
        _bump("quota", len(result.rate_limited))
        _bump("unassigned", len(result.skipped_unassigned))
        _bump("nonspawnable", len(result.skipped_nonspawnable))
        _bump("concurrency_cap", getattr(result, "max_in_progress_deferred", 0) or 0)
        if result.skipped_per_profile_capped:
            _bump("concurrency_cap(per_profile)", len(result.skipped_per_profile_capped))
        _bump("claim_race", len(getattr(result, "claim_race", []) or []))
        _bump("workspace_collision", len(result.workspace_collisions))
        _bump("spawn_exception", len(getattr(result, "spawn_errors", []) or []))
        if result.skipped_locked:
            _bump("dispatch_lock_contended")

    return counts


def dispatch_causes_capacity_only(counts: "dict[str, int]") -> bool:
    """Whether ``counts`` contains only intentional concurrency deferrals."""
    return bool(counts) and CAPACITY_ONLY_CAUSES.issuperset(counts)


def dispatch_causes_benign_only(counts: "dict[str, int]") -> bool:
    """Whether every zero-spawn cause is benign -- a capacity deferral or a
    routing steady-state (nonspawnable/unassigned).

    A benign streak warrants a log line but never an operator page: capacity
    deferrals drain when a worker frees a slot, and routing steady-states are
    the expected condition for human / control-plane lanes. Only a cause
    OUTSIDE this set (spawn_exception, quota, workspace_collision, claim_race,
    dispatch_lock_contended, respawn_guarded, ...) indicates a real stall.
    """
    return bool(counts) and BENIGN_CAUSES.issuperset(counts)


def summarize_dispatch_causes(results: "Iterable[Optional[DispatchResult]]") -> str:
    """Format per-cause spawn-refusal counts, e.g.::

        "respawn_guarded(recent_success)=3, quota=1"

    Used by the gateway's/CLI's "dispatcher stuck" warning and Telegram
    escalation so an operator sees *why* zero workers spawned instead of
    just the bare tick count. ``None`` entries in ``results`` are skipped
    (defensive — callers that collect per-board results across a tick may
    have a board that raised before producing a ``DispatchResult``).

    Buckets, ordered by descending count (ties broken alphabetically):

    * ``respawn_guarded(<reason>)`` — non-quota respawn-guard reasons
      (``recent_success``, ``forced_skill_validation``).
    * ``quota`` — respawn-guard reasons ``blocker_auth`` /
      ``rate_limit_cooldown``, plus post-crash ``rate_limited`` requeues.
      Retrying immediately won't help; the provider needs to cool down.
    * ``concurrency_cap`` — deferred by the global ``max_in_progress`` cap.
    * ``concurrency_cap(per_profile)`` — deferred by
      ``max_in_progress_per_profile`` for a specific assignee.
    * ``unassigned`` / ``nonspawnable`` — routing issues, not dispatcher bugs
      (the latter is the expected steady-state for control-plane lanes).
    * ``claim_race`` — lost an atomic claim to a concurrent claimant.
    * ``workspace_collision`` — another running task already owns the
      non-scratch workspace.
    * ``spawn_exception`` — workspace resolution or ``spawn_fn`` raised.
      The exception text itself is logged at the raise site (never
      swallowed) — this bucket only counts occurrences.
    * ``dispatch_lock_contended`` — this tick's board was already being
      serviced by another dispatcher process (issue #35240).

    Returns ``""`` when every bucket is empty (nothing to report).
    """
    counts = dispatch_cause_counts(results)
    if not counts:
        return ""
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return ", ".join(f"{cause}={n}" for cause, n in ordered)


# Bounded registry of recently-reaped worker child exits, populated by the
# reap loop at the top of ``dispatch_once`` and consulted by
# ``detect_crashed_workers`` to classify a dead-pid task.
#
# Entry: ``pid -> (raw_wait_status, reaped_at_epoch)``. We keep raw status
# so both ``os.WIFEXITED`` / ``os.WEXITSTATUS`` and ``os.WIFSIGNALED`` can
# be consulted. Entries are trimmed by age (and total size cap as a
# belt-and-braces against unbounded growth on exotic platforms).
_RECENT_WORKER_EXIT_TTL_SECONDS = 600
_RECENT_WORKER_EXITS_MAX = 4096
_recent_worker_exits: "dict[int, tuple[int, float]]" = {}


def _record_worker_exit(pid: int, raw_status: int) -> None:
    """Record a reaped child's exit status for later classification.

    Called from the reap loop in ``dispatch_once``. Safe to call many
    times; duplicate pids overwrite (pids can cycle, latest wins).
    """
    if not pid or pid <= 0:
        return
    now = time.time()
    _recent_worker_exits[int(pid)] = (int(raw_status), now)
    # Age-based trim: drop entries older than the TTL.
    if len(_recent_worker_exits) > _RECENT_WORKER_EXITS_MAX // 2:
        cutoff = now - _RECENT_WORKER_EXIT_TTL_SECONDS
        for _pid in [p for p, (_s, t) in _recent_worker_exits.items() if t < cutoff]:
            _recent_worker_exits.pop(_pid, None)
    # Size cap as a final guard.
    if len(_recent_worker_exits) > _RECENT_WORKER_EXITS_MAX:
        # Drop oldest half.
        ordered = sorted(_recent_worker_exits.items(), key=lambda kv: kv[1][1])
        for _pid, _ in ordered[: len(ordered) // 2]:
            _recent_worker_exits.pop(_pid, None)


def _classify_worker_exit(pid: int) -> "tuple[str, Optional[int]]":
    """Classify a recently-reaped worker by pid.

    Returns ``(kind, code)`` where ``kind`` is one of:

    * ``"clean_exit"`` — ``WIFEXITED`` with ``WEXITSTATUS == 0``. When the
      task is still ``running`` in the DB, this is a protocol violation
      (worker exited without calling ``kanban_complete`` / ``kanban_block``)
      and should be auto-blocked immediately — retrying will just loop.
    * ``"rate_limited"`` — ``WIFEXITED`` with status
      ``KANBAN_RATE_LIMIT_EXIT_CODE``. The worker bailed because the
      provider rate-limited / exhausted quota, NOT because the task failed.
      ``detect_crashed_workers`` releases the task back to ``ready`` without
      counting a failure, so a long quota window can't trip the breaker.
    * ``"nonzero_exit"`` — ``WIFEXITED`` with non-zero status. Real error.
    * ``"signaled"`` — ``WIFSIGNALED`` (OOM killer, SIGKILL, etc). Real crash.
    * ``"unknown"`` — pid was not in the reap registry (either reaped by
      something else, or died between reap tick and liveness check). Fall
      back to existing crashed-counter behavior.

    ``code`` is the exit status (for ``clean_exit`` / ``rate_limited`` /
    ``nonzero_exit``) or the signal number (for ``signaled``), or ``None``
    for ``unknown``.
    """
    entry = _recent_worker_exits.get(int(pid))
    if entry is None:
        return ("unknown", None)
    raw, _ = entry
    try:
        if os.WIFEXITED(raw):
            code = os.WEXITSTATUS(raw)
            if code == 0:
                return ("clean_exit", 0)
            if code == KANBAN_RATE_LIMIT_EXIT_CODE:
                return ("rate_limited", code)
            return ("nonzero_exit", code)
        if os.WIFSIGNALED(raw):
            return ("signaled", os.WTERMSIG(raw))
    except Exception:
        pass
    return ("unknown", None)


def reap_worker_zombies() -> "list[int]":
    """Reap all zombie children of this process without blocking.

    Returns the list of reaped PIDs. Safe to call when there are no
    children (returns []). No-op on Windows.
    """
    reaped: "list[int]" = []
    if os.name != "nt":
        try:
            while True:
                try:
                    pid, status = os.waitpid(-1, os.WNOHANG)
                except ChildProcessError:
                    break
                if pid == 0:
                    break
                _record_worker_exit(pid, status)
                reaped.append(pid)
        except Exception:
            pass
    return reaped


def _pid_alive(pid: Optional[int]) -> bool:
    """Return True if ``pid`` is still running on this host.

    Cross-platform: uses ``OpenProcess`` + ``WaitForSingleObject`` on
    Windows (via ``gateway.status._pid_exists``) and ``os.kill(pid, 0)``
    on POSIX. Returns False for falsy PIDs or on any OS error.

    **DO NOT** use ``os.kill(pid, 0)`` directly on Windows — Python's
    Windows ``os.kill`` treats ``sig=0`` as ``CTRL_C_EVENT`` (bpo-14484)
    and will broadcast it to the target's console group, potentially
    killing unrelated processes.

    **Zombie handling:** the existence check succeeds against zombie
    processes (post-exit, pre-reap) because the process table entry
    still exists. A worker that exits without being reaped by its
    parent would stay "alive" to the dispatcher forever. Dispatcher
    workers are started via ``start_new_session=True`` + intentional
    Popen handle abandonment, so init reaps them quickly — but during
    the window between exit and reap, we'd otherwise see stale "alive"
    signals. On Linux we peek at ``/proc/<pid>/status`` and treat
    ``State: Z`` as dead. On macOS we ask ``ps`` for the BSD ``stat``
    field and treat values containing ``Z`` as dead.
    """
    if not pid or pid <= 0:
        return False
    from gateway.status import _pid_exists
    if not _pid_exists(int(pid)):
        return False
    # Still here → process exists. Check for zombie on platforms
    # where we have a cheap, deterministic process-state probe.
    if sys.platform == "linux":
        try:
            with open(f"/proc/{int(pid)}/status", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("State:"):
                        # "State:\tZ (zombie)" → dead
                        if "Z" in line.split(":", 1)[1]:
                            return False
                        break
        except (FileNotFoundError, PermissionError, OSError):
            # proc entry gone → already reaped; treat as dead.
            # PermissionError shouldn't happen for our own children but
            # be defensive.
            pass
    elif sys.platform == "darwin":
        try:
            proc = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(int(pid))],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=1,
                check=False,
            )
            if proc.returncode != 0:
                return False
            if "Z" in (proc.stdout or "").strip():
                return False
        except (OSError, subprocess.SubprocessError, TimeoutError):
            # If the secondary probe fails, keep the kill(0) answer.
            pass
    return True


def _scan_exact_kanban_workers(
    task_id: str,
    run_id: Optional[int],
    claim_lock: Optional[str],
) -> list[int]:
    """Observe workers whose inherited run identity exactly matches a claim.

    This is deliberately detection-only.  It closes the observability gap for
    legacy/malformed runs where ``worker_pid`` was never durably attached, but
    it does not make a process-kill decision from a process-table scan.  The
    attach-or-die spawn gate is the enforcement mechanism for new workers.
    """
    if run_id is None or not claim_lock:
        return []
    expected = {
        "HERMES_KANBAN_TASK": str(task_id),
        "HERMES_KANBAN_RUN_ID": str(int(run_id)),
        "HERMES_KANBAN_CLAIM_LOCK": str(claim_lock),
    }
    matches: list[int] = []
    try:
        import psutil  # type: ignore

        for process in psutil.process_iter(["pid"]):
            try:
                pid = int(process.info["pid"])
                if pid == os.getpid():
                    continue
                environ = process.environ()
            except (KeyError, TypeError, ValueError):
                continue
            except Exception:
                # AccessDenied/NoSuchProcess/ZombieProcess and partially
                # supported platforms are normal for a best-effort canary.
                continue
            if all(environ.get(key) == value for key, value in expected.items()):
                matches.append(pid)
    except Exception:
        return []
    return sorted(set(matches))


def _log_orphan_worker_canary(
    *,
    task_id: str,
    run_id: Optional[int],
    claim_lock: Optional[str],
) -> list[int]:
    """Emit a secret-free warning for exact workers missing DB PID ownership."""
    matches = _scan_exact_kanban_workers(task_id, run_id, claim_lock)
    if matches:
        lock_digest = hashlib.sha256(str(claim_lock).encode("utf-8")).hexdigest()[:12]
        _log.warning(
            "orphan_worker_canary action=observe_only task_id=%s run_id=%s "
            "claim_lock_sha256=%s worker_pids=%s",
            task_id,
            run_id,
            lock_digest,
            matches,
        )
    return matches


def _attest_reclaim_process_group(
    pgid: int,
    sid: int,
    claim_lock: str,
    *,
    task_id: Optional[str] = None,
    run_id: Optional[int] = None,
) -> Optional[bool]:
    """Attest every surviving member after a worker group leader exits."""
    if pgid <= 1 or sid <= 0 or not claim_lock:
        return False
    # Never inspect or signal the dispatcher's own process group.  A malformed
    # manual row must not turn a reclaim operation into a host-wide kill.
    try:
        if os.name != "nt" and int(pgid) == os.getpgrp():
            return False
    except (AttributeError, OSError):
        return None
    try:
        import psutil  # type: ignore

        expected = {"HERMES_KANBAN_CLAIM_LOCK": str(claim_lock)}
        if task_id is not None:
            expected["HERMES_KANBAN_TASK"] = str(task_id)
        if run_id is not None:
            expected["HERMES_KANBAN_RUN_ID"] = str(int(run_id))
        found = False
        for process in psutil.process_iter(["pid", "status"]):
            try:
                pid = int(process.info["pid"])
                if process.info.get("status") == psutil.STATUS_ZOMBIE:
                    continue
                if os.getpgid(pid) != int(pgid):
                    continue
                found = True
                if os.getsid(pid) != int(sid):
                    return False
                environ = process.environ()
                if any(environ.get(key) != value for key, value in expected.items()):
                    return False
            except (psutil.NoSuchProcess, psutil.ZombieProcess, ProcessLookupError):
                continue
            except (psutil.AccessDenied, PermissionError, OSError):
                return None
        return True if found else False
    except Exception:
        return None


def _attest_reclaim_process_identity(
    pid: int,
    claim_lock: str,
    *,
    worker_started_at: Optional[float] = None,
    worker_pgid: Optional[int] = None,
    worker_sid: Optional[int] = None,
    task_id: Optional[str] = None,
    run_id: Optional[int] = None,
) -> Optional[bool]:
    """Match a live PID to the exact claim without trusting PID reuse.

    ``True`` means the process environment and birth identity were observed
    twice and match. ``False`` means the PID is gone or belongs to a different
    process. ``None`` means the platform denied inspection; callers must hold
    the claim rather than guessing.
    """
    if worker_started_at is None:
        return None
    if os.name != "nt" and (worker_pgid is None or worker_sid is None):
        return None

    def _attest_group_after_leader_exit() -> Optional[bool]:
        if worker_pgid is None or worker_sid is None:
            return False
        return _attest_reclaim_process_group(
            int(worker_pgid),
            int(worker_sid),
            str(claim_lock),
            task_id=task_id,
            run_id=run_id,
        )

    try:
        import psutil  # type: ignore

        process = psutil.Process(int(pid))
        born_at = float(process.create_time())
        if abs(born_at - float(worker_started_at)) > 0.01:
            # The PID was reused.  Even if the old group still contains
            # attested descendants, killpg() would also signal the new owner
            # when it inherited the numeric PGID.  Hold the claim instead of
            # guessing; group reclaim remains allowed only when the leader PID
            # is actually gone or a zombie.
            group_identity = _attest_group_after_leader_exit()
            return False if group_identity is False else None
        environ = process.environ()
        if environ.get("HERMES_KANBAN_CLAIM_LOCK") != str(claim_lock):
            return False
        if task_id is not None and environ.get("HERMES_KANBAN_TASK") != str(task_id):
            return False
        if run_id is not None and environ.get("HERMES_KANBAN_RUN_ID") != str(int(run_id)):
            return False
        if worker_pgid is not None:
            if not hasattr(os, "getpgid") or os.getpgid(int(pid)) != int(worker_pgid):
                return False
        if worker_sid is not None:
            if not hasattr(os, "getsid") or os.getsid(int(pid)) != int(worker_sid):
                return False
        if abs(float(process.create_time()) - born_at) > 0.01:
            group_identity = _attest_group_after_leader_exit()
            return False if group_identity is False else None
        if not process.is_running():
            return _attest_group_after_leader_exit()
        return True
    except ImportError:
        return None
    except Exception as exc:
        try:
            import psutil  # type: ignore

            if isinstance(exc, psutil.NoSuchProcess):
                return _attest_group_after_leader_exit()
            if isinstance(exc, psutil.ZombieProcess):
                return _attest_group_after_leader_exit()
            if isinstance(exc, psutil.AccessDenied):
                return None
        except Exception:
            pass
        return None


def _process_group_alive(pgid: int) -> bool:
    """Return whether a POSIX process group has any non-zombie member."""
    try:
        import psutil  # type: ignore

        for process in psutil.process_iter(["pid", "status"]):
            try:
                pid = int(process.info["pid"])
                if os.getpgid(pid) != int(pgid):
                    continue
                if process.info.get("status") != psutil.STATUS_ZOMBIE:
                    return True
            except (ProcessLookupError, KeyError, TypeError, ValueError):
                continue
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
            except (psutil.AccessDenied, PermissionError, OSError):
                return True
        return False
    except Exception:
        try:
            os.killpg(int(pgid), 0)  # windows-footgun: ok — pgid probe, POSIX-only fallback
            return True
        except ProcessLookupError:
            return False
        except (PermissionError, OSError):
            return True


def _terminate_reclaimed_worker(
    pid: Optional[int],
    claim_lock: Optional[str],
    *,
    worker_started_at: Optional[float] = None,
    worker_pgid: Optional[int] = None,
    worker_sid: Optional[int] = None,
    task_id: Optional[str] = None,
    run_id: Optional[int] = None,
    signal_fn=None,
) -> dict[str, Any]:
    """Best-effort host-local worker termination for reclaim paths."""
    import signal

    info: dict[str, Any] = {
        "prev_pid": int(pid) if pid else None,
        "host_local": False,
        "termination_attempted": False,
        "terminated": False,
        "sigkill": False,
        "identity_verified": False,
        "identity_mismatch": False,
        "identity_unverifiable": False,
    }
    if not pid or pid <= 0 or not claim_lock:
        return info

    host_prefix = f"{_claimer_id().split(':', 1)[0]}:"
    if not str(claim_lock).startswith(host_prefix):
        return info
    info["host_local"] = True

    kill = signal_fn if signal_fn is not None else (
        os.kill if hasattr(os, "kill") else None
    )
    if kill is None:
        return info

    use_process_group = bool(
        os.name != "nt"
        and worker_pgid is not None
        and worker_sid is not None
        and int(worker_pgid) == int(pid)
        and int(worker_sid) == int(pid)
        and hasattr(os, "killpg")
    )
    if use_process_group and worker_pgid is not None and int(worker_pgid) <= 1:
        info["identity_unverifiable"] = True
        info["worker_still_alive"] = _pid_alive(pid)
        return info

    if use_process_group and int(pid) == os.getpid():
        # Test/manual rows sometimes use the dispatcher PID with an injected
        # signal transport.  The dispatcher's own group must not make that
        # synthetic worker appear alive after the hook says it exited.
        target_alive = lambda: _pid_alive(pid)
    else:
        target_alive = (
            (lambda: _pid_alive(pid) or _process_group_alive(int(worker_pgid)))
            if use_process_group
            else (lambda: _pid_alive(pid))
        )
    if os.name != "nt" and not use_process_group:
        info["identity_unverifiable"] = True
        info["worker_still_alive"] = target_alive()
        return info

    # Signal injection is only a transport hook; it never bypasses ownership
    # attestation. A reused PID must be rejected before either real or injected
    # signalling is invoked.
    identity = _attest_reclaim_process_identity(
        int(pid),
        str(claim_lock),
        worker_started_at=worker_started_at,
        worker_pgid=worker_pgid,
        worker_sid=worker_sid,
        task_id=task_id,
        run_id=run_id,
    )
    if identity is False:
        info["identity_mismatch"] = True
        info["terminated"] = True
        return info
    if identity is None:
        info["identity_unverifiable"] = True
        info["worker_still_alive"] = target_alive()
        return info
    info["identity_verified"] = True

    info["termination_attempted"] = True
    signal_target = (
        signal_fn
        if signal_fn is not None
        else (os.killpg if use_process_group else kill)  # windows-footgun: ok — pgroups are POSIX-only
    )
    target_id = int(worker_pgid) if use_process_group else int(pid)
    try:
        signal_target(target_id, signal.SIGTERM)
    except ProcessLookupError:
        # Process is already gone — that's a successful termination, not a
        # survival. Leaving terminated=False here would make the reclaim guard
        # misread a dead worker as still-alive and defer forever.
        info["terminated"] = True
        return info
    except OSError:
        return info

    for _ in range(10):
        if not target_alive():
            info["terminated"] = True
            return info
        time.sleep(0.5)

    if target_alive():
        # Re-attest immediately before escalation.  This closes the
        # PID/PGID-reuse window between SIGTERM and SIGKILL, including the
        # leader-dead/descendant-only case and injected signal_fn tests.
        identity = _attest_reclaim_process_identity(
            int(pid),
            str(claim_lock),
            worker_started_at=worker_started_at,
            worker_pgid=worker_pgid,
            worker_sid=worker_sid,
            task_id=task_id,
            run_id=run_id,
        )
        if identity is not True:
            info["identity_verified"] = False
            info["identity_mismatch"] = identity is False
            info["identity_unverifiable"] = identity is None
            info["terminated"] = identity is False
            info["worker_still_alive"] = target_alive()
            return info
        try:
            # signal.SIGKILL doesn't exist on Windows; fall back to SIGTERM
            # (which maps to TerminateProcess via the stdlib shim).
            _sigkill = getattr(signal, "SIGKILL", signal.SIGTERM)
            signal_target(target_id, _sigkill)
            info["sigkill"] = True
        except (ProcessLookupError, OSError):
            return info

    info["terminated"] = not target_alive()
    return info


def _terminate_worker_for_task(
    pid: Optional[int],
    claim_lock: Optional[str],
    *,
    task_id: Optional[str],
    run_id: Optional[int],
    worker_started_at: Optional[float],
    worker_pgid: Optional[int],
    worker_sid: Optional[int],
    signal_fn=None,
) -> dict[str, Any]:
    """Terminate a task-owned worker while preserving legacy hook callers.

    Rows without spawn-time identity are legacy/manual state.  They remain
    fail-closed in ``_terminate_reclaimed_worker``; omitting the new keyword
    arguments here also keeps older dashboard/test transports source
    compatible without weakening the production receipt contract.
    """
    kwargs: dict[str, Any] = {"signal_fn": signal_fn}
    if any(value is not None for value in (worker_started_at, worker_pgid, worker_sid)):
        kwargs.update(
            task_id=task_id,
            run_id=run_id,
            worker_started_at=worker_started_at,
            worker_pgid=worker_pgid,
            worker_sid=worker_sid,
        )
    return _terminate_reclaimed_worker(pid, claim_lock, **kwargs)


def _worker_survived_termination(termination: dict) -> bool:
    """True when we tried to kill our own host-local worker and it is still alive.

    Reclaiming in this state would release the claim and let the dispatcher
    spawn a second worker while the first is still running — the duplication
    loop. Only host-local workers we actually signalled count: a non-local
    claim lock or a no-op attempt (no ``os.kill`` available) must fall through
    to the normal release path, since we cannot manage that worker anyway.
    """
    if (
        termination.get("host_local")
        and termination.get("identity_unverifiable")
        and termination.get("worker_still_alive")
    ):
        return True
    return bool(
        termination.get("termination_attempted")
        and termination.get("host_local")
        and not termination.get("terminated")
    )


def _defer_reclaim_for_live_worker_in_txn(
    conn: sqlite3.Connection,
    task_id: str,
    claim_lock: Optional[str],
    now: int,
    termination: dict,
    *,
    reason: str,
) -> None:
    """Hold a claim whose worker survived termination instead of releasing it.

    Extends ``claim_expires`` by ``RECLAIM_DEFER_GRACE_SECONDS`` so the task
    stays ``running`` (no duplicate spawn) and records a ``reclaim_deferred``
    event so the hold is visible in ``hermes kanban tail``. The next dispatch
    tick retries the kill; this is self-correcting because not spawning a
    duplicate is what lets the throttled worker finally die.
    """
    grace = now + RECLAIM_DEFER_GRACE_SECONDS
    cur = conn.execute(
        "UPDATE tasks SET claim_expires = ? "
        "WHERE id = ? AND status = 'running' AND claim_lock IS ?",
        (grace, task_id, claim_lock),
    )
    if cur.rowcount != 1:
        return
    run_id = _current_run_id(conn, task_id)
    if run_id is not None:
        conn.execute(
            "UPDATE task_runs SET claim_expires = ? WHERE id = ?",
            (grace, run_id),
        )
    payload = {
        "reason": reason,
        "claim_lock": claim_lock,
        "claim_expires_now": grace,
    }
    payload.update(termination)
    _append_event(conn, task_id, "reclaim_deferred", payload, run_id=run_id)


def _defer_reclaim_for_live_worker(
    conn: sqlite3.Connection,
    task_id: str,
    claim_lock: Optional[str],
    now: int,
    termination: dict,
    *,
    reason: str,
) -> None:
    """Persist a live-worker defer using its own transaction when needed."""
    with write_txn(conn):
        _defer_reclaim_for_live_worker_in_txn(
            conn,
            task_id,
            claim_lock,
            now,
            termination,
            reason=reason,
        )


def heartbeat_worker(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    note: Optional[str] = None,
    expected_run_id: Optional[int] = None,
    activity_kind: str = "semantic",
) -> bool:
    """Record a typed activity event and touch the run's matching clock.

    ``process`` means only the wrapper is alive, ``transport`` means provider
    bytes/events arrived, ``semantic`` means useful workflow output occurred,
    and ``durable`` means recoverable state was persisted.  The legacy task
    heartbeat remains a compatibility projection of every kind.

    Returns True on success, False if the task is not in a state that
    should be heartbeating (not running, or claim expired).
    """
    now = int(time.time())
    with write_txn(conn):
        if expected_run_id is None:
            cur = conn.execute(
                "UPDATE tasks SET last_heartbeat_at = ? "
                "WHERE id = ? AND status = 'running'",
                (now, task_id),
            )
        else:
            cur = conn.execute(
                "UPDATE tasks SET last_heartbeat_at = ? "
                "WHERE id = ? AND status = 'running' AND current_run_id = ?",
                (now, task_id, int(expected_run_id)),
            )
        if cur.rowcount != 1:
            return False
        run_id = (
            int(expected_run_id)
            if expected_run_id is not None
            else _current_run_id(conn, task_id)
        )
        if run_id is not None:
            if activity_kind == "transport":
                conn.execute(
                    "UPDATE task_runs SET last_heartbeat_at = ?, "
                    "last_transport_activity_at = ? WHERE id = ?",
                    (now, now, run_id),
                )
            elif activity_kind == "semantic":
                conn.execute(
                    "UPDATE task_runs SET last_heartbeat_at = ?, "
                    "last_semantic_progress_at = ? WHERE id = ?",
                    (now, now, run_id),
                )
            elif activity_kind == "durable":
                conn.execute(
                    "UPDATE task_runs SET last_heartbeat_at = ?, "
                    "last_semantic_progress_at = ?, "
                    "last_durable_progress_at = ? WHERE id = ?",
                    (now, now, now, run_id),
                )
            else:
                conn.execute(
                    "UPDATE task_runs SET last_heartbeat_at = ? WHERE id = ?",
                    (now, run_id),
                )
        payload: dict[str, Any] = {"activity_kind": activity_kind}
        if note:
            payload["note"] = note
        _append_event(
            conn, task_id, "heartbeat", payload, run_id=run_id,
        )
    return True


_RUNTIME_OBSERVATION_FIELDS = frozenset({
    "version", "phase", "provider", "model", "reasoning_effort",
    "runtime", "api_mode", "reason", "from",
})


def record_runtime_observation(
    conn: sqlite3.Connection,
    task_id: str,
    run_id: int,
    observation: dict,
) -> bool:
    """Append the actual active route for the current contracted run."""
    if not isinstance(observation, dict):
        raise ValueError("runtime observation must be an object")
    unknown = set(observation) - _RUNTIME_OBSERVATION_FIELDS
    if unknown:
        raise ValueError(
            f"runtime observation has unsupported fields: {sorted(unknown)}"
        )
    if observation.get("version") != 1:
        raise ValueError("runtime observation version must be 1")
    if observation.get("phase") not in {"initial", "fallback", "primary", "switch"}:
        raise ValueError("runtime observation phase is invalid")
    for key in ("provider", "model", "runtime", "api_mode"):
        if not isinstance(observation.get(key), str):
            raise ValueError(f"runtime observation {key} must be a string")

    with write_txn(conn):
        row = conn.execute(
            "SELECT r.id FROM task_runs r "
            "JOIN tasks t ON t.id = r.task_id AND t.current_run_id = r.id "
            "WHERE r.id = ? AND r.task_id = ? AND r.status = 'running' "
            "AND r.run_spec_json IS NOT NULL",
            (int(run_id), task_id),
        ).fetchone()
        if row is None:
            return False
        _append_event(
            conn,
            task_id,
            "runtime_observed",
            observation,
            run_id=int(run_id),
        )
    return True


def latest_runtime_observation(
    conn: sqlite3.Connection,
    run_id: int,
) -> Optional[dict]:
    row = conn.execute(
        "SELECT payload FROM task_events WHERE run_id = ? "
        "AND kind = 'runtime_observed' ORDER BY id DESC LIMIT 1",
        (int(run_id),),
    ).fetchone()
    if row is None or not row["payload"]:
        return None
    try:
        value = json.loads(row["payload"])
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


# ---------------------------------------------------------------------------
# Durable continuation contracts (BUILD-487)
# ---------------------------------------------------------------------------

def _continuation_config() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        kanban_cfg = load_config().get("kanban") or {}
        continuation = kanban_cfg.get("continuation") or {}
        return dict(continuation) if isinstance(continuation, dict) else {}
    except Exception:
        return {}


def continuation_runtime_enabled(config: Optional[dict[str, Any]] = None) -> bool:
    cfg = config if isinstance(config, dict) else _continuation_config()
    return bool(cfg.get("enabled", False))


def _continuation_manifest_from_row(row: sqlite3.Row) -> ContinuationManifest:
    from hermes_cli.kanban_continuation import (
        ContinuationContractError,
        content_digest,
        normalize_manifest,
        validate_compiled_context,
    )

    try:
        manifest = normalize_manifest(json.loads(row["manifest_json"]))
        compiled = validate_compiled_context(
            manifest, json.loads(row["compiled_context_json"])
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        if isinstance(exc, ContinuationContractError):
            raise
        raise ContinuationContractError(
            "continuation_record_corrupt", "continuation record is not valid JSON"
        ) from exc
    if row["manifest_digest"] != content_digest(manifest):
        raise ContinuationContractError("manifest_digest_mismatch")
    if row["context_digest"] != compiled["context_digest"]:
        raise ContinuationContractError("compiled_context_digest_mismatch")
    return ContinuationManifest(
        run_id=int(row["run_id"]),
        task_id=row["task_id"],
        version=int(row["version"]),
        manifest_digest=row["manifest_digest"],
        manifest=manifest,
        context_digest=row["context_digest"],
        compiled_context=compiled,
        created_at=int(row["created_at"]),
    )


def get_continuation_manifest(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    task_id: Optional[str] = None,
    require_current: bool = False,
) -> Optional[ContinuationManifest]:
    clauses = ["m.run_id = ?"]
    params: list[Any] = [int(run_id)]
    join = ""
    if task_id is not None:
        clauses.append("m.task_id = ?")
        params.append(task_id)
    if require_current:
        join = " JOIN tasks t ON t.id = m.task_id AND t.current_run_id = m.run_id"
    row = conn.execute(
        "SELECT m.* FROM continuation_manifests m" + join
        + " WHERE " + " AND ".join(clauses),
        params,
    ).fetchone()
    return _continuation_manifest_from_row(row) if row is not None else None


def _continuation_limits(config: dict[str, Any]) -> tuple[int, int]:
    from hermes_cli.kanban_continuation import (
        DEFAULT_MAX_CORE_BYTES,
        DEFAULT_MAX_TOTAL_BYTES,
    )

    def _positive(value: Any, default: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        return parsed if parsed > 0 else default

    return (
        _positive(config.get("max_core_bytes"), DEFAULT_MAX_CORE_BYTES),
        _positive(config.get("max_total_bytes"), DEFAULT_MAX_TOTAL_BYTES),
    )


def _continuation_provider_policy(config: dict[str, Any]) -> dict[str, Any]:
    from hermes_cli.kanban_continuation import normalize_provider_policy

    raw = config.get("provider_policy") or {}
    if not isinstance(raw, dict):
        raw = {}
    return normalize_provider_policy(
        {
            "allow": raw.get("allow", raw.get("allowed_providers", [])),
            "deny": raw.get("deny", raw.get("denied_providers", [])),
        }
    )


def _sanitize_denied_routing_override(
    provider_override: Optional[str],
    model_override: Optional[str],
    *,
    context: str,
) -> "tuple[Optional[str], Optional[str]]":
    """Drop a routing override the continuation provider_policy denies.

    A card minted with a provider the policy forbids is deterministically
    unspawnable: ``assert_provider_allowed()`` fails at ``pre_spawn_primary``,
    the failure is charged to the task's budget, and the card human-blocks
    hours later having done no work. Catch it at CREATION instead.

    Fail-safe, not fail-closed: the override is dropped (provider AND the
    coupled model together) rather than raised, so the autonomous loop keeps
    the work item and routes it through the assignee profile's default
    (allowed) provider. Raising here would risk the orchestrator discarding
    the create call and losing the task entirely. The requested route is
    logged so the drop is never silent. Deterministic, so idempotent
    decompose retries converge on the same sanitized card.

    Failure mode is fail-SAFE, not fail-open (Sol review): the override is
    KEPT only when ``provider_allowed`` positively confirms it. If the policy
    cannot be evaluated (import/read error), the override is DROPPED, not
    preserved -- persisting an unverified override would let it survive
    creation and then hard-block at ``pre_spawn_primary`` on a later
    successful policy read, recreating the very block this guard prevents.
    A card with no override always routes via the assignee default, which is
    an allowed provider by construction. (Residual: an empty policy from a
    disabled/unconfigured continuation legitimately keeps the override -- the
    pre_spawn guard uses the same empty policy and agrees; the authority
    remains ``assert_provider_allowed`` at spawn.)
    """
    if not provider_override:
        return provider_override, model_override
    try:
        from hermes_cli.kanban_continuation import provider_allowed

        allowed = provider_allowed(
            provider_override,
            _continuation_provider_policy(_continuation_config()),
        )
    except Exception:
        # Cannot confirm the override is allowed -> treat as not-allowed and
        # drop it (fail-safe). See docstring.
        allowed = False
    if allowed:
        return provider_override, model_override
    _log.warning(
        "kanban %s: dropping continuation-policy-denied or unverifiable "
        "routing override (provider=%r model=%r) -- routing via assignee "
        "default provider",
        context,
        provider_override,
        model_override,
    )
    return None, None


def _continuation_references(
    conn: sqlite3.Connection,
    task: Task,
) -> list[dict[str, Any]]:
    from hermes_cli.kanban_continuation import extract_jira_keys, text_digest

    board = get_current_board()
    refs: list[dict[str, Any]] = [
        {
            "kind": "kanban",
            "uri": f"kanban://{board}/tasks/{task.id}",
            "required": True,
            "label": "authoritative task, graph, runs, comments, and events",
        }
    ]
    if task.body:
        refs.append(
            {
                "kind": "kanban",
                "uri": f"kanban://{board}/tasks/{task.id}/body",
                "digest": text_digest(task.body),
                "required": True,
                "label": "full opening specification",
            }
        )
    for key in extract_jira_keys(task.title, task.body, task.branch_name):
        refs.append(
            {
                "kind": "jira",
                "uri": f"jira://ahlnos/{key}",
                "required": True,
                "label": "objective, acceptance, dependencies, and final status",
            }
        )
    for parent_id in parent_ids(conn, task.id):
        refs.append(
            {
                "kind": "kanban",
                "uri": f"kanban://{board}/tasks/{parent_id}",
                "required": False,
                "label": "upstream handoff evidence",
            }
        )
    for attachment in list_attachments(conn, task.id):
        refs.append(
            {
                "kind": "artifact",
                "uri": f"file:{attachment.stored_path}",
                "required": False,
                "label": attachment.filename,
            }
        )
    return refs


def _render_continuation_blockers(
    conn: sqlite3.Connection,
    task_id: str,
) -> str:
    blockers = list_continuation_blockers(conn, task_id)
    if not blockers:
        return ""
    lines = ["", "## Durable review blocker ledger"]
    for blocker in blockers[-100:]:
        evidence = (
            blocker.resolution_evidence_ref
            if blocker.status == "resolved"
            else blocker.evidence_ref
        )
        lines.append(
            f"- `B{blocker.id}` [{blocker.severity}/{blocker.status}] "
            f"{blocker.title}"
            + (f" — evidence: `{evidence}`" if evidence else "")
        )
    return "\n".join(lines) + "\n"


def prepare_run_continuation(
    conn: sqlite3.Connection,
    task_id: str,
    run_id: int,
    *,
    config: Optional[dict[str, Any]] = None,
) -> ContinuationManifest:
    """Create one immutable context bundle after workspace resolution.

    The operation is idempotent for a run. Git is sampled before the SQLite
    write lock; worker bootstrap rechecks it after the start gate opens, so a
    drift in that interval fails before the agent can mutate the workspace.
    """
    from hermes_cli.kanban_continuation import (
        compile_context,
        content_digest,
        decisions_from_comments,
        extract_acceptance_criteria,
        git_repository_snapshot,
        normalize_manifest,
    )

    existing = get_continuation_manifest(conn, run_id, task_id=task_id)
    if existing is not None:
        return existing
    cfg = dict(config) if isinstance(config, dict) else _continuation_config()
    if not continuation_runtime_enabled(cfg):
        raise ValueError("continuation runtime is not enabled")
    task = get_task(conn, task_id)
    if task is None or task.current_run_id != int(run_id):
        raise ValueError("continuation run is no longer current")
    repository = git_repository_snapshot(task.workspace_path)
    now = int(time.time())
    max_core, max_total = _continuation_limits(cfg)

    with write_txn(conn):
        current = get_task(conn, task_id)
        if current is None or current.current_run_id != int(run_id):
            raise ValueError("continuation run is no longer current")
        run_spec = get_run_spec(
            conn, run_id, task_id=task_id, require_current=True,
        )
        if run_spec is None:
            raise ValueError("continuation run has no immutable RunSpec")
        working_set = build_worker_context(
            conn,
            task_id,
            _use_continuation=False,
            _now_override=now,
        ) + _render_continuation_blockers(conn, task_id)
        manifest_value = {
            "version": 1,
            "task_id": task_id,
            "run_id": int(run_id),
            "objective": current.title,
            "acceptance_criteria": extract_acceptance_criteria(current.body),
            "decisions": decisions_from_comments(list_comments(conn, task_id)),
            "references": _continuation_references(conn, current),
            "provider_policy": _continuation_provider_policy(cfg),
            "repository": repository,
            "created_at": now,
        }
        # Persist and digest the canonical validated shape.  Raw reference
        # objects may omit optional null fields; hashing that pre-normalized
        # representation would make an immediate readback appear corrupt.
        manifest_value = normalize_manifest(manifest_value)
        compiled = compile_context(
            manifest_value,
            working_set,
            max_core_bytes=max_core,
            max_total_bytes=max_total,
        )
        manifest_digest = content_digest(manifest_value)
        # Idempotent on run_id: a bootstrap retried after a partial earlier
        # attempt (worker respawn on the same run, crash between manifest
        # write and the rest of bootstrap) must refresh the manifest, not
        # die on the UNIQUE constraint — that IntegrityError blocked
        # workflows on two boards (2026-07-18/19: t_24314039, t_91fe35b0).
        conn.execute(
            "INSERT INTO continuation_manifests "
            "(run_id, task_id, version, manifest_digest, manifest_json, "
            " context_digest, compiled_context_json, created_at) "
            "VALUES (?, ?, 1, ?, ?, ?, ?, ?) "
            "ON CONFLICT(run_id) DO UPDATE SET "
            " task_id=excluded.task_id, version=excluded.version, "
            " manifest_digest=excluded.manifest_digest, "
            " manifest_json=excluded.manifest_json, "
            " context_digest=excluded.context_digest, "
            " compiled_context_json=excluded.compiled_context_json, "
            " created_at=excluded.created_at",
            (
                int(run_id), task_id, manifest_digest,
                json.dumps(manifest_value, ensure_ascii=False, sort_keys=True),
                compiled["context_digest"],
                json.dumps(compiled, ensure_ascii=False, sort_keys=True),
                now,
            ),
        )
        _append_event(
            conn,
            task_id,
            "continuation_prepared",
            {
                "version": 1,
                "manifest_digest": manifest_digest,
                "context_digest": compiled["context_digest"],
                "context_bytes": compiled["bytes"],
                "provider_policy": manifest_value["provider_policy"],
            },
            run_id=int(run_id),
        )
    prepared = get_continuation_manifest(conn, run_id, task_id=task_id)
    if prepared is None:
        raise RuntimeError("continuation manifest insert readback failed")
    return prepared


def record_continuation_bootstrap_failure(
    conn: sqlite3.Connection,
    task_id: str,
    run_id: int,
    *,
    code: str,
    message: str,
    phase: str,
) -> bool:
    """Persist one queryable failure decision for a cold-start operator."""
    with write_txn(conn):
        current = conn.execute(
            "SELECT 1 FROM tasks WHERE id = ? AND current_run_id = ?",
            (task_id, int(run_id)),
        ).fetchone()
        if current is None:
            return False
        _append_event(
            conn,
            task_id,
            "continuation_bootstrap_failed",
            {
                "code": str(code)[:128],
                "message": str(message)[:2000],
                "phase": str(phase)[:64],
            },
            run_id=int(run_id),
        )
    return True


def continuation_status(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    run_id: Optional[int] = None,
) -> dict[str, Any]:
    task = get_task(conn, task_id)
    active_run = task.current_run_id if task is not None else None
    selected_run = int(run_id) if run_id is not None else active_run
    if selected_run is None:
        latest_manifest = conn.execute(
            "SELECT run_id FROM continuation_manifests WHERE task_id = ? "
            "ORDER BY run_id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if latest_manifest is not None:
            selected_run = int(latest_manifest["run_id"])
    manifest = (
        get_continuation_manifest(conn, selected_run, task_id=task_id)
        if selected_run is not None
        else None
    )
    open_blockers = list_continuation_blockers(conn, task_id, status="open")
    last_failure = conn.execute(
        "SELECT payload, created_at FROM task_events WHERE task_id = ? "
        "AND kind = 'continuation_bootstrap_failed' ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    failure = None
    if last_failure is not None:
        try:
            failure = json.loads(last_failure["payload"] or "null")
        except (TypeError, ValueError):
            failure = {"code": "corrupt_failure_event"}
        if isinstance(failure, dict):
            failure["created_at"] = int(last_failure["created_at"])
    return {
        "enabled_for_run": manifest is not None,
        "active_run": selected_run is not None and selected_run == active_run,
        "run_id": selected_run,
        "manifest_digest": manifest.manifest_digest if manifest else None,
        "context_digest": manifest.context_digest if manifest else None,
        "context_bytes": (
            manifest.compiled_context.get("bytes") if manifest else None
        ),
        "open_critical_blockers": [
            {"id": item.id, "severity": item.severity, "title": item.title}
            for item in open_blockers
            if item.severity in CONTINUATION_CRITICAL_SEVERITIES
        ],
        "open_advisory_blockers": [
            {"id": item.id, "severity": item.severity, "title": item.title}
            for item in open_blockers
            if item.severity not in CONTINUATION_CRITICAL_SEVERITIES
        ],
        "last_bootstrap_failure": failure,
    }


def _continuation_blocker_from_row(row: sqlite3.Row) -> ContinuationBlocker:
    return ContinuationBlocker(
        id=int(row["id"]), task_id=row["task_id"], severity=row["severity"],
        title=row["title"], details=row["details"],
        evidence_ref=row["evidence_ref"], fingerprint=row["fingerprint"],
        status=row["status"],
        discovered_run_id=(int(row["discovered_run_id"]) if row["discovered_run_id"] is not None else None),
        discovered_by=row["discovered_by"], discovered_at=int(row["discovered_at"]),
        resolved_run_id=(int(row["resolved_run_id"]) if row["resolved_run_id"] is not None else None),
        resolved_by=row["resolved_by"],
        resolution_evidence_ref=row["resolution_evidence_ref"],
        resolved_at=(int(row["resolved_at"]) if row["resolved_at"] is not None else None),
    )


def list_continuation_blockers(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    status: Optional[str] = None,
) -> list[ContinuationBlocker]:
    if status is not None and status not in {"open", "resolved"}:
        raise ValueError("blocker status must be open or resolved")
    query = "SELECT * FROM continuation_blockers WHERE task_id = ?"
    params: list[Any] = [task_id]
    if status is not None:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY id ASC"
    return [
        _continuation_blocker_from_row(row)
        for row in conn.execute(query, params).fetchall()
    ]


def record_continuation_blocker(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    severity: str,
    title: str,
    discovered_by: str,
    details: Optional[str] = None,
    evidence_ref: Optional[str] = None,
    discovered_run_id: Optional[int] = None,
) -> ContinuationBlocker:
    severity = str(severity or "").strip().upper()
    if severity not in CONTINUATION_BLOCKER_SEVERITIES:
        raise ValueError(
            f"severity must be one of {sorted(CONTINUATION_BLOCKER_SEVERITIES)}"
        )
    title = " ".join(str(title or "").split())
    if not title or len(title.encode("utf-8")) > 2000:
        raise ValueError("blocker title is required and must be <= 2000 bytes")
    actor = str(discovered_by or "").strip()
    if not actor:
        raise ValueError("discovered_by is required")
    details_clean = str(details).strip()[:16000] if details else None
    evidence_clean = str(evidence_ref).strip()[:4096] if evidence_ref else None
    fingerprint = hashlib.sha256(
        json.dumps(
            {"severity": severity, "title": title.casefold()},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    now = int(time.time())
    with write_txn(conn):
        if conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone() is None:
            raise ValueError(f"unknown task {task_id}")
        existing = conn.execute(
            "SELECT * FROM continuation_blockers WHERE task_id = ? "
            "AND fingerprint = ? AND status = 'open' ORDER BY id DESC LIMIT 1",
            (task_id, fingerprint),
        ).fetchone()
        if existing is not None:
            return _continuation_blocker_from_row(existing)
        if discovered_run_id is not None and conn.execute(
            "SELECT 1 FROM task_runs WHERE id = ? AND task_id = ?",
            (int(discovered_run_id), task_id),
        ).fetchone() is None:
            raise ValueError("discovered run does not belong to task")
        cur = conn.execute(
            "INSERT INTO continuation_blockers "
            "(task_id, severity, title, details, evidence_ref, fingerprint, "
            " status, discovered_run_id, discovered_by, discovered_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)",
            (
                task_id, severity, title, details_clean, evidence_clean,
                fingerprint, discovered_run_id, actor, now,
            ),
        )
        blocker_id = int(cur.lastrowid or 0)
        _append_event(
            conn,
            task_id,
            "continuation_blocker_opened",
            {
                "blocker_id": blocker_id,
                "severity": severity,
                "title": title,
                "evidence_ref": evidence_clean,
            },
            run_id=discovered_run_id,
        )
        row = conn.execute(
            "SELECT * FROM continuation_blockers WHERE id = ?", (blocker_id,)
        ).fetchone()
        assert row is not None
        return _continuation_blocker_from_row(row)


def resolve_continuation_blocker(
    conn: sqlite3.Connection,
    task_id: str,
    blocker_id: int,
    *,
    resolved_by: str,
    resolution_evidence_ref: str,
    resolved_run_id: Optional[int] = None,
) -> ContinuationBlocker:
    actor = str(resolved_by or "").strip()
    evidence = str(resolution_evidence_ref or "").strip()
    if not actor:
        raise ValueError("resolved_by is required")
    if not evidence or len(evidence.encode("utf-8")) > 4096:
        raise ValueError("resolution_evidence_ref is required and must be <= 4096 bytes")
    now = int(time.time())
    with write_txn(conn):
        if resolved_run_id is not None and conn.execute(
            "SELECT 1 FROM task_runs WHERE id = ? AND task_id = ?",
            (int(resolved_run_id), task_id),
        ).fetchone() is None:
            raise ValueError("resolved run does not belong to task")
        cur = conn.execute(
            "UPDATE continuation_blockers SET status = 'resolved', "
            "resolved_run_id = ?, resolved_by = ?, resolution_evidence_ref = ?, "
            "resolved_at = ? WHERE id = ? AND task_id = ? AND status = 'open'",
            (
                resolved_run_id, actor, evidence, now,
                int(blocker_id), task_id,
            ),
        )
        if cur.rowcount != 1:
            raise ValueError("unknown or already-resolved blocker")
        _append_event(
            conn,
            task_id,
            "continuation_blocker_resolved",
            {
                "blocker_id": int(blocker_id),
                "resolution_evidence_ref": evidence,
            },
            run_id=resolved_run_id,
        )
        row = conn.execute(
            "SELECT * FROM continuation_blockers WHERE id = ?", (int(blocker_id),)
        ).fetchone()
        assert row is not None
        return _continuation_blocker_from_row(row)


def open_critical_continuation_blockers(
    conn: sqlite3.Connection,
    task_id: str,
) -> list[ContinuationBlocker]:
    placeholders = ",".join("?" for _ in CONTINUATION_CRITICAL_SEVERITIES)
    rows = conn.execute(
        "SELECT * FROM continuation_blockers WHERE task_id = ? AND status = 'open' "
        f"AND severity IN ({placeholders}) ORDER BY id ASC",
        [task_id, *sorted(CONTINUATION_CRITICAL_SEVERITIES)],
    ).fetchall()
    return [_continuation_blocker_from_row(row) for row in rows]


class OpenCriticalBlockersError(ValueError):
    def __init__(self, blockers: list[ContinuationBlocker]):
        self.blockers = blockers
        super().__init__(
            "completion blocked by open critical findings: "
            + ", ".join(f"B{item.id}/{item.severity}" for item in blockers)
        )


def checkpoint_execution_epoch(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    reason: str,
    summary: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
    expected_run_id: Optional[int] = None,
) -> str:
    """Close only the bounded epoch and return the task to ready.

    Productive epochs are unlimited. Three byte-identical checkpoint digests
    with no intervening progress trip a non-convergence block; the limit is on
    repeated no-progress state, not on total implementation/review cycles.
    """
    reason_clean = str(reason or "").strip()
    if not reason_clean:
        raise ValueError("checkpoint reason is required")
    task = get_task(conn, task_id)
    if task is None or task.status != "running" or task.current_run_id is None:
        return "not_running"
    run_id = int(task.current_run_id)
    if expected_run_id is not None and run_id != int(expected_run_id):
        return "ownership_mismatch"
    if get_continuation_manifest(conn, run_id, task_id=task_id) is None:
        return "legacy_run"
    from hermes_cli.kanban_continuation import git_repository_snapshot

    # Compare semantic state, not volatile timeout/process telemetry.  A run
    # may checkpoint for a differently worded reason or with a different PID
    # and still have made no progress.  Conversely a commit, dirty-tree
    # change, test result, artifact, or explicit progress evidence advances
    # the digest and permits another bounded epoch.
    raw_metadata = metadata or {}
    semantic_metadata_keys = {
        "artifacts",
        "changed_files",
        "commit",
        "head",
        "progress_evidence",
        "tests_run",
        "workspace_status",
    }
    semantic_metadata = {
        key: raw_metadata[key]
        for key in sorted(semantic_metadata_keys)
        if key in raw_metadata
    }
    repository = git_repository_snapshot(task.workspace_path)
    progress_value = {
        "summary": str(summary or "").strip(),
        "semantic_metadata": semantic_metadata,
        "repository": repository,
    }
    progress_digest = hashlib.sha256(
        json.dumps(
            progress_value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    prior = conn.execute(
        "SELECT metadata FROM task_runs WHERE task_id = ? "
        "AND outcome = 'checkpointed' ORDER BY id DESC LIMIT ?",
        (task_id, CONTINUATION_NONPROGRESS_LIMIT - 1),
    ).fetchall()
    repeated = 1
    for row in prior:
        try:
            prior_metadata = json.loads(row["metadata"] or "{}")
        except (TypeError, ValueError):
            break
        if prior_metadata.get("progress_digest") != progress_digest:
            break
        repeated += 1

    checkpoint_metadata = dict(metadata or {})
    checkpoint_metadata.update(
        {
            "checkpoint_reason": reason_clean,
            "progress_digest": progress_digest,
            "identical_checkpoint_count": repeated,
            "repository_checkpoint": repository,
        }
    )
    with write_txn(conn):
        current = conn.execute(
            "SELECT current_run_id, status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if current is None or current["status"] != "running" or int(
            current["current_run_id"] or 0
        ) != run_id:
            return "ownership_mismatch"
        ended_run = _end_run(
            conn,
            task_id,
            outcome="checkpointed",
            status="checkpointed",
            summary=summary or reason_clean,
            metadata=checkpoint_metadata,
        )
        if ended_run != run_id:
            raise RuntimeError("checkpoint closed the wrong run")
        if repeated >= CONTINUATION_NONPROGRESS_LIMIT:
            conn.execute(
                "UPDATE tasks SET status = 'blocked', block_kind = 'needs_input' "
                "WHERE id = ? AND current_run_id IS NULL",
                (task_id,),
            )
            _append_event(
                conn,
                task_id,
                "continuation_nonconvergent",
                {
                    "progress_digest": progress_digest,
                    "identical_checkpoint_count": repeated,
                    "limit": CONTINUATION_NONPROGRESS_LIMIT,
                },
                run_id=run_id,
            )
            outcome = "blocked_nonconvergent"
        else:
            conn.execute(
                "UPDATE tasks SET status = 'ready', last_heartbeat_at = NULL "
                "WHERE id = ? AND current_run_id IS NULL",
                (task_id,),
            )
            _append_event(
                conn,
                task_id,
                "epoch_checkpointed",
                {
                    "reason": reason_clean,
                    "progress_digest": progress_digest,
                    "identical_checkpoint_count": repeated,
                },
                run_id=run_id,
            )
            outcome = "ready"
    # A new epoch may be claimed on the very next dispatcher tick. Release
    # only resources with exact registered identity before that can happen.
    cleanup_owned_run_resources(conn, task_id, run_id)
    return outcome


def _owned_resource_from_row(row: sqlite3.Row) -> OwnedRunResource:
    try:
        identity = json.loads(row["identity_json"])
    except (TypeError, ValueError):
        identity = {}
    return OwnedRunResource(
        id=int(row["id"]), task_id=row["task_id"], run_id=int(row["run_id"]),
        claim_lock=row["claim_lock"], kind=row["kind"], identity=identity,
        identity_digest=row["identity_digest"], cleanup_policy=row["cleanup_policy"],
        state=row["state"], created_at=int(row["created_at"]),
        cleaned_at=(int(row["cleaned_at"]) if row["cleaned_at"] is not None else None),
        cleanup_error=row["cleanup_error"],
    )


def register_owned_run_resource(
    conn: sqlite3.Connection,
    task_id: str,
    run_id: int,
    claim_lock: str,
    *,
    kind: str,
    identity: dict[str, Any],
    cleanup_policy: str = "on_terminal",
) -> OwnedRunResource:
    from hermes_cli.kanban_continuation import canonical_json, content_digest

    if kind not in CONTINUATION_RESOURCE_KINDS:
        raise ValueError(f"unknown resource kind {kind!r}")
    if cleanup_policy not in CONTINUATION_RESOURCE_CLEANUP_POLICIES:
        raise ValueError(f"unknown cleanup policy {cleanup_policy!r}")
    if not isinstance(identity, dict) or not identity:
        raise ValueError("resource identity must be a non-empty object")
    canonical_identity = canonical_json(identity)
    identity_digest = content_digest(identity)
    now = int(time.time())
    with write_txn(conn):
        owner = conn.execute(
            "SELECT 1 FROM task_runs WHERE id = ? AND task_id = ? AND claim_lock = ?",
            (int(run_id), task_id, claim_lock),
        ).fetchone()
        if owner is None:
            raise ValueError("resource owner does not match task/run/claim")
        conn.execute(
            "INSERT OR IGNORE INTO continuation_owned_resources "
            "(task_id, run_id, claim_lock, kind, identity_json, identity_digest, "
            " cleanup_policy, state, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)",
            (
                task_id, int(run_id), claim_lock, kind, canonical_identity,
                identity_digest, cleanup_policy, now,
            ),
        )
        row = conn.execute(
            "SELECT * FROM continuation_owned_resources WHERE run_id = ? "
            "AND kind = ? AND identity_digest = ?",
            (int(run_id), kind, identity_digest),
        ).fetchone()
        assert row is not None
        resource = _owned_resource_from_row(row)
        _append_event(
            conn,
            task_id,
            "continuation_resource_registered",
            {"resource_id": resource.id, "kind": kind, "cleanup_policy": cleanup_policy},
            run_id=int(run_id),
        )
        return resource


def list_owned_run_resources(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    state: Optional[str] = None,
) -> list[OwnedRunResource]:
    query = "SELECT * FROM continuation_owned_resources WHERE run_id = ?"
    params: list[Any] = [int(run_id)]
    if state is not None:
        query += " AND state = ?"
        params.append(state)
    query += " ORDER BY id ASC"
    return [
        _owned_resource_from_row(row)
        for row in conn.execute(query, params).fetchall()
    ]


def _cleanup_exact_tmux(identity: dict[str, Any]) -> tuple[bool, str]:
    name = str(identity.get("session_name") or "").strip()
    session_id = str(identity.get("session_id") or "").strip()
    created = str(identity.get("session_created") or "").strip()
    if not name or not session_id or not created:
        return False, "tmux_identity_incomplete"
    probe = subprocess.run(
        ["tmux", "display-message", "-p", "-t", name, "#{session_id}\t#{session_created}"],
        capture_output=True, text=True, timeout=5,
    )
    if probe.returncode != 0:
        return True, "already_absent"
    if probe.stdout.strip() != f"{session_id}\t{created}":
        return False, "tmux_identity_mismatch"
    killed = subprocess.run(
        ["tmux", "kill-session", "-t", name],
        capture_output=True, text=True, timeout=5,
    )
    return (killed.returncode == 0, "cleaned" if killed.returncode == 0 else "tmux_kill_failed")


def _cleanup_exact_worktree(identity: dict[str, Any]) -> tuple[bool, str]:
    path = Path(str(identity.get("path") or ""))
    repo_root = Path(str(identity.get("repo_root") or ""))
    if not path.is_absolute() or not repo_root.is_absolute():
        return False, "worktree_identity_incomplete"
    listed = subprocess.run(
        ["git", "-C", str(repo_root), "worktree", "list", "--porcelain"],
        capture_output=True, text=True, timeout=10,
    )
    if listed.returncode != 0:
        return False, "worktree_list_failed"
    if f"worktree {path}\n" not in listed.stdout:
        return True, "already_absent"
    removed = subprocess.run(
        ["git", "-C", str(repo_root), "worktree", "remove", str(path)],
        capture_output=True, text=True, timeout=30,
    )
    return (removed.returncode == 0, "cleaned" if removed.returncode == 0 else "worktree_remove_refused")


def _cleanup_exact_child_process(identity: dict[str, Any]) -> tuple[bool, str]:
    try:
        import psutil  # type: ignore
    except Exception as exc:
        return False, f"process_cleanup_dependency_unavailable:{type(exc).__name__}"

    try:
        pid = int(identity["pid"])
        started_at = float(identity["process_started_at"])
        process = psutil.Process(pid)
        if abs(float(process.create_time()) - started_at) > 0.01:
            return False, "process_birth_identity_mismatch"
        if pid == os.getpid():
            return False, "refusing_to_kill_current_process"
        process.terminate()
        try:
            process.wait(timeout=2)
        except psutil.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
        return True, "cleaned"
    except psutil.NoSuchProcess:
        return True, "already_absent"
    except Exception as exc:
        return False, f"process_cleanup_failed:{type(exc).__name__}"


def cleanup_owned_run_resources(
    conn: sqlite3.Connection,
    task_id: str,
    run_id: int,
) -> list[dict[str, Any]]:
    """Best-effort cleanup, but never fail open on identity mismatch."""
    results: list[dict[str, Any]] = []
    resources = list_owned_run_resources(conn, run_id, state="active")
    for resource in resources:
        if resource.task_id != task_id or resource.cleanup_policy != "on_terminal":
            continue
        try:
            if resource.kind == "tmux_session":
                ok, detail = _cleanup_exact_tmux(resource.identity)
            elif resource.kind == "worktree":
                ok, detail = _cleanup_exact_worktree(resource.identity)
            else:
                ok, detail = _cleanup_exact_child_process(resource.identity)
        except Exception as exc:
            ok, detail = False, f"cleanup_exception:{type(exc).__name__}"
        state = "cleaned" if ok else "identity_mismatch" if "mismatch" in detail else "cleanup_failed"
        with write_txn(conn):
            conn.execute(
                "UPDATE continuation_owned_resources SET state = ?, cleaned_at = ?, "
                "cleanup_error = ? WHERE id = ? AND state = 'active'",
                (state, int(time.time()), None if ok else detail, resource.id),
            )
            _append_event(
                conn,
                task_id,
                "continuation_resource_cleanup",
                {
                    "resource_id": resource.id,
                    "kind": resource.kind,
                    "status": state,
                    "detail": detail,
                },
                run_id=int(run_id),
            )
        results.append(
            {"resource_id": resource.id, "kind": resource.kind, "status": state, "detail": detail}
        )
    return results


def cleanup_terminal_run_resources(
    conn: sqlite3.Connection,
    *,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Sweep exact-owned resources for runs ended through any lifecycle path.

    Completion and checkpoint paths clean immediately. This dispatcher sweep
    covers block, reclaim, crash, and legacy terminal transitions without
    duplicating destructive cleanup calls throughout the state machine.
    """
    try:
        bounded_limit = max(1, min(int(limit), 1000))
    except (TypeError, ValueError):
        bounded_limit = 100
    rows = conn.execute(
        "SELECT DISTINCT o.task_id, o.run_id "
        "FROM continuation_owned_resources o "
        "JOIN task_runs r ON r.id = o.run_id AND r.task_id = o.task_id "
        "WHERE o.state = 'active' AND o.cleanup_policy = 'on_terminal' "
        "AND r.ended_at IS NOT NULL ORDER BY o.run_id ASC LIMIT ?",
        (bounded_limit,),
    ).fetchall()
    cleaned: list[dict[str, Any]] = []
    for row in rows:
        cleaned.extend(
            cleanup_owned_run_resources(
                conn, row["task_id"], int(row["run_id"]),
            )
        )
    return cleaned


def enforce_max_runtime(
    conn: sqlite3.Connection,
    *,
    signal_fn=None,
) -> list[str]:
    """Terminate workers whose per-task ``max_runtime_seconds`` has elapsed.

    Sends SIGTERM, waits a short grace window, then SIGKILL. Emits a
    ``timed_out`` event and drops the task back to ``ready`` so the next
    dispatcher tick re-spawns it — unless the spawn-failure circuit
    breaker has already given up, in which case the task stays blocked
    where ``_record_spawn_failure`` parked it.

    Runs host-local: only tasks claimed by this host are candidates
    (same reasoning as ``detect_crashed_workers``). ``signal_fn`` is a
    test hook; defaults to ``os.kill`` on POSIX.
    """
    timed_out: list[str] = []
    now = int(time.time())
    host_prefix = f"{_claimer_id().split(':', 1)[0]}:"

    rows = conn.execute(
        "SELECT t.id, t.current_run_id, t.worker_pid, t.worker_started_at, t.worker_pgid, t.worker_sid, "
        "       COALESCE(r.started_at, t.started_at) AS active_started_at, "
        "       t.max_runtime_seconds, t.claim_lock "
        "FROM tasks t "
        "LEFT JOIN task_runs r ON r.id = t.current_run_id "
        "WHERE t.status = 'running' AND t.max_runtime_seconds IS NOT NULL "
        "  AND COALESCE(r.started_at, t.started_at) IS NOT NULL "
        "  AND t.worker_pid IS NOT NULL"
    ).fetchall()
    for row in rows:
        lock = row["claim_lock"] or ""
        if not lock.startswith(host_prefix):
            continue
        # Runtime is per attempt, not lifetime-of-task. ``tasks.started_at``
        # intentionally records the first time a task ever started, so retries
        # must be measured from the active task_runs row when present.
        elapsed = now - int(row["active_started_at"])
        if elapsed < int(row["max_runtime_seconds"]):
            continue

        pid = int(row["worker_pid"])
        tid = row["id"]
        termination = _terminate_worker_for_task(
            pid,
            row["claim_lock"],
            task_id=tid,
            run_id=row["current_run_id"],
            worker_started_at=row["worker_started_at"],
            worker_pgid=row["worker_pgid"],
            worker_sid=row["worker_sid"],
            signal_fn=signal_fn,
        )
        if _worker_survived_termination(termination):
            _defer_reclaim_for_live_worker(
                conn,
                tid,
                row["claim_lock"],
                now,
                termination,
                reason="max_runtime_worker_alive",
            )
            continue
        killed = bool(termination.get("sigkill"))

        continuation = (
            get_continuation_manifest(
                conn,
                int(row["current_run_id"]),
                task_id=tid,
                require_current=True,
            )
            if row["current_run_id"] is not None
            else None
        )
        if continuation is not None:
            checkpoint_outcome = checkpoint_execution_epoch(
                conn,
                tid,
                reason=(
                    f"Execution epoch reached max runtime "
                    f"({int(elapsed)}s/{int(row['max_runtime_seconds'])}s)"
                ),
                metadata={
                    "trigger": "max_runtime",
                    "pid": pid,
                    "elapsed_seconds": int(elapsed),
                    "limit_seconds": int(row["max_runtime_seconds"]),
                    "sigkill": killed,
                    **termination,
                },
                expected_run_id=int(row["current_run_id"]),
            )
            if checkpoint_outcome in {"ready", "blocked_nonconvergent"}:
                timed_out.append(tid)
            continue

        with write_txn(conn):
            cur = conn.execute(
                "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL, "
                "worker_started_at = NULL, worker_pgid = NULL, worker_sid = NULL, "
                "last_heartbeat_at = NULL "
                "WHERE id = ? AND status = 'running' "
                "  AND worker_pid = ? AND claim_lock IS ?",
                (tid, pid, row["claim_lock"]),
            )
            if cur.rowcount == 1:
                payload = {
                    "pid": pid,
                    "elapsed_seconds": int(elapsed),
                    "limit_seconds": int(row["max_runtime_seconds"]),
                    "sigkill": killed,
                }
                payload.update(termination)
                run_id = _end_run(
                    conn, tid,
                    outcome="timed_out", status="timed_out",
                    error=f"elapsed {int(elapsed)}s > limit {int(row['max_runtime_seconds'])}s",
                    metadata=payload,
                )
                _append_event(
                    conn, tid, "timed_out", payload, run_id=run_id,
                )
                timed_out.append(tid)
        # Increment the unified failure counter. Outside the write_txn
        # above because ``_record_task_failure`` opens its own. If the
        # breaker trips, this flips the task ``ready → blocked`` and
        # emits a ``gave_up`` event on top of the ``timed_out`` we
        # already emitted.
        if cur.rowcount == 1:
            _record_task_failure(
                conn, tid,
                error=f"elapsed {int(elapsed)}s > limit {int(row['max_runtime_seconds'])}s",
                outcome="timed_out",
                release_claim=False,
                end_run=False,
                event_payload_extra={"pid": pid, "sigkill": killed},
            )
    return timed_out


# Semantic-progress gap — if a running task has not produced meaningful work
# in this many seconds it is considered inactive regardless of
# the ``dispatch_stale_timeout_seconds`` threshold.  Hardcoded at 1 hour
# to match the original spec (">4h started + no commits in 1h").
_STALE_HEARTBEAT_GAP_SECONDS = 3600


def detect_stale_running(
    conn: sqlite3.Connection,
    *,
    stale_timeout_seconds: int = 0,
    signal_fn=None,
) -> list[str]:
    """Reclaim ``running`` tasks that show no semantic progress within the
    staleness window.

    A task is considered stale when BOTH of these hold:

    1. It has been running for longer than ``stale_timeout_seconds``
       (measured from the active run's ``started_at``, falling back to
       ``tasks.started_at`` on older runs).
    2. Its active run's ``last_semantic_progress_at`` is older than
       ``_STALE_HEARTBEAT_GAP_SECONDS`` (or NULL — never sent a heartbeat).

    On reclaim the task is reset to ``ready``, the run is closed with
    Legacy runs without typed clocks fall back to ``tasks.last_heartbeat_at``.
    On reclaim the task is reset to ``ready``, the run is closed with
    ``outcome='stale'``, and the host-local worker (if still running) is
    terminated.

    Only considers ``status='running'`` tasks. Blocked tasks are never
    candidates.  Returns the list of reclaimed task IDs.

    ``stale_timeout_seconds=0`` disables the check entirely (returns ``[]``
    immediately).  ``signal_fn`` is a test hook; defaults to ``os.kill``
    on POSIX.
    """
    if stale_timeout_seconds <= 0:
        return []


    now = int(time.time())
    host_prefix = f"{_claimer_id().split(':', 1)[0]}:"
    reclaimed: list[str] = []

    rows = conn.execute(
        "SELECT t.id, t.current_run_id, t.worker_pid, t.worker_started_at, t.worker_pgid, "
        "       t.worker_sid, t.last_heartbeat_at, t.claim_lock, "
        "       r.last_semantic_progress_at, "
        "       COALESCE(r.last_semantic_progress_at, "
        "                t.last_heartbeat_at) AS progress_at, "
        "       COALESCE(r.started_at, t.started_at) AS active_started_at "
        "FROM tasks t "
        "LEFT JOIN task_runs r ON r.id = t.current_run_id "
        "WHERE t.status = 'running'"
    ).fetchall()

    for row in rows:
        # Skip if no started_at (shouldn't happen for running, but be safe).
        if row["active_started_at"] is None:
            continue

        elapsed = now - int(row["active_started_at"])
        if elapsed < stale_timeout_seconds:
            continue  # not old enough to check

        last_hb = row["progress_at"]
        hb_age = (now - int(last_hb)) if last_hb is not None else None
        if hb_age is not None and hb_age < _STALE_HEARTBEAT_GAP_SECONDS:
            continue  # recent heartbeat → still alive

        pid = row["worker_pid"]
        tid = row["id"]
        lock = row["claim_lock"] or ""

        # Terminate the worker if it's still host-local.
        termination = _terminate_worker_for_task(
            pid,
            lock,
            task_id=tid,
            run_id=row["current_run_id"],
            worker_started_at=row["worker_started_at"],
            worker_pgid=row["worker_pgid"],
            worker_sid=row["worker_sid"],
            signal_fn=signal_fn,
        )

        # Never release a claim while our own worker is still alive: that would
        # spawn a duplicate beside it. Hold the claim and retry next tick.
        if _worker_survived_termination(termination):
            _defer_reclaim_for_live_worker(
                conn, tid, lock, now, termination,
                reason="heartbeat_stale_worker_alive",
            )
            continue

        with write_txn(conn):
            cur = conn.execute(
                "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL, "
                "worker_started_at = NULL, worker_pgid = NULL, worker_sid = NULL, "
                "last_heartbeat_at = NULL "
                "WHERE id = ? AND status = 'running' "
                "  AND claim_lock IS ?",
                (tid, row["claim_lock"]),
            )
            if cur.rowcount != 1:
                continue

            payload = {
                "elapsed_seconds": int(elapsed),
                "last_heartbeat_at": (
                    int(last_hb) if last_hb is not None else None
                ),
                "heartbeat_age_seconds": (
                    int(hb_age) if hb_age is not None else None
                ),
                "timeout_seconds": stale_timeout_seconds,
                "pid": int(pid) if pid else None,
            }
            payload.update(termination)

            run_id = _end_run(
                conn, tid,
                outcome="stale", status="stale",
                error=(
                    f"no heartbeat for {int(hb_age)}s "
                    if hb_age is not None
                    else "no heartbeat ever"
                ) + f" after {int(elapsed)}s running",
                metadata=payload,
            )
            _append_event(
                conn, tid, "stale", payload, run_id=run_id,
            )
            reclaimed.append(tid)

        # Intentionally NOT calling _record_task_failure here. Stale reclaim
        # is dispatcher-side detection of an absent heartbeat; the task is
        # going straight back to ``ready`` for re-dispatch. Counting it as
        # a worker failure would let two legitimately-long-running tasks
        # (>4h without explicit heartbeat) trip the circuit breaker and
        # auto-block, even though no worker actually failed. The 'stale'
        # event already lives in task_events for auditability; that's the
        # right surface for "this happened" without conflating with the
        # spawn_failed / timed_out / crashed counters.

    return reclaimed


# ── Release/remediation circuit breaker (BUILD-261) ────────────────
#
# Incident: on 2026-07-09 a releaser kanban card kept respawning (and, in
# the fan-out shape, spawning fresh remediation children) after every
# attempt "succeeded" (worker completed, PR merged) yet the post-merge
# "Master Release" workflow failed again with the byte-identical error
# signature each time — 8 PRs merged in one day without ever fixing the
# root cause. The existing ``consecutive_failures`` circuit breaker
# (``DEFAULT_FAILURE_LIMIT`` / ``_record_task_failure`` below) is blind to
# this shape: it resets to 0 on every successful completion, so a task
# that keeps "succeeding" but reproducing the same downstream failure
# never trips it. ``check_respawn_guard`` also doesn't help — it only
# rate-limits how often a respawn is *attempted*, it never inspects
# whether repeated attempts are actually converging.
#
# The functions in this section are a second, content-aware breaker:
# every task run that ends in failure gets a normalized "signature"
# recorded (see :func:`normalize_failure_signature`); before a respawn is
# allowed, :func:`check_failure_signature_breaker` compares the most
# recent ``threshold`` signatures (across the task and any linked
# remediation children) and — if they're all identical — halts the saga
# by blocking the task (kind=``needs_input``) instead of respawning it
# again. Blocking reuses ``block_task``'s existing ``blocked`` task_event,
# which the gateway's ``_kanban_notifier_watcher`` already delivers to any
# subscriber (Telegram included) — no new notify channel is added.

_FAILURE_SIGNATURE_EVENT_KIND = "failure_signature"

# Trip threshold for check_failure_signature_breaker: how many of the most
# recent recorded failure signatures must be identical before a respawn is
# refused and the task is blocked for human review. Configurable via
# ``HERMES_KANBAN_FAILURE_SIGNATURE_THRESHOLD`` (env) or the dispatcher's
# ``kanban.failure_signature_threshold`` config key (threaded through
# ``dispatch_once`` the same way ``kanban.failure_limit`` is).
DEFAULT_FAILURE_SIGNATURE_REPEAT_THRESHOLD = 2

# --- normalize_failure_signature helpers ---
# A GitHub Actions error annotation line, e.g.
#   ##[error]smoke check failed: checkout (/checkout) returned 500 ...
_FAILURE_SIG_ERROR_MARKER = "##[error]"
# ISO-8601 timestamps, with or without fractional seconds / trailing Z,
# using either a literal 'T' or a space between date and time.
_FAILURE_SIG_ISO_TS_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?"
)
# "run id 123", "run_id=123", "run-id: 123", "runId 123", "run-123456".
_FAILURE_SIG_RUN_ID_RE = re.compile(
    r"\brun[\s_-]?id\s*[:=]?\s*[0-9a-f-]+\b|\brun[\s_-]\d+\b",
    re.IGNORECASE,
)
# Git SHAs (short or full) — hex-only tokens 7-40 chars long.
_FAILURE_SIG_SHA_RE = re.compile(r"\b[0-9a-f]{7,40}\b", re.IGNORECASE)
# Any other long bare digit run (workflow run numbers, PIDs, epoch millis,
# etc.) that isn't meaningful to the failure's identity.
_FAILURE_SIG_LONG_NUM_RE = re.compile(r"\b\d{6,}\b")
_FAILURE_SIG_WS_RE = re.compile(r"\s+")


def normalize_failure_signature(text: Optional[str]) -> str:
    """Reduce a (possibly multi-line) failure log to a stable signature.

    Used by the BUILD-261 release/remediation circuit breaker to detect
    when repeated task attempts are failing for the identical underlying
    reason rather than making progress.

    Extraction (in order):
      1. The first line containing a ``##[error]`` GitHub Actions error
         annotation, truncated to start at that marker. Only the first
         such line is used — a trailing summary line like
         ``##[error]Process completed with exit code 1.`` on the next
         line is discarded so it can't dilute/change the signature.
      2. If no ``##[error]`` marker is found anywhere, the final
         non-blank line of the text (heuristic "final error line").

    Normalization (so re-runs of the identical failure produce the
    identical signature even though timestamps/ids differ between runs):
      * ISO-8601 timestamps -> ``<TS>``
      * ``run id`` / ``run_id=`` / ``run-<n>`` tokens -> ``run <ID>``
      * 7-40 char hex tokens (git SHAs) -> ``<SHA>``
      * bare runs of 6+ digits (workflow run numbers, PIDs, epoch millis)
        -> ``<N>``
      * whitespace collapsed to single spaces, trimmed, lowercased

    Returns ``""`` for falsy/blank input so callers can treat "no
    signature" without a None-check, and so two failures that both lack
    any error text never spuriously compare equal (callers must treat an
    empty signature as "no signal", not "matches").
    """
    if not text:
        return ""
    lines = text.splitlines()
    line = None
    for candidate in lines:
        if _FAILURE_SIG_ERROR_MARKER in candidate:
            idx = candidate.find(_FAILURE_SIG_ERROR_MARKER)
            line = candidate[idx:]
            break
    if line is None:
        for candidate in reversed(lines):
            if candidate.strip():
                line = candidate
                break
    if line is None:
        line = text
    sig = _FAILURE_SIG_ISO_TS_RE.sub("<TS>", line)
    sig = _FAILURE_SIG_RUN_ID_RE.sub("run <ID>", sig)
    sig = _FAILURE_SIG_SHA_RE.sub("<SHA>", sig)
    sig = _FAILURE_SIG_LONG_NUM_RE.sub("<N>", sig)
    sig = _FAILURE_SIG_WS_RE.sub(" ", sig).strip()
    return sig.lower()


def _record_failure_signature(
    conn: sqlite3.Connection,
    task_id: str,
    error_text: Optional[str],
    *,
    run_id: Optional[int] = None,
    context: Optional[dict] = None,
) -> str:
    """Persist the normalized failure signature for a run that just ended
    in failure, as a ``failure_signature`` task_event.

    Must be called from inside an already-open ``write_txn`` (mirrors
    ``_append_event``). Returns the computed signature; appends nothing
    and returns ``""`` when ``error_text`` normalizes to empty, so a
    failure with no usable error text never contributes a stray entry
    that could spuriously "match" another empty entry in the breaker's
    comparison.
    """
    sig = normalize_failure_signature(error_text)
    if not sig:
        return ""
    payload = {
        "signature": sig,
        "raw_excerpt": (error_text or "")[:300],
    }
    if context:
        payload.update(context)
    _append_event(
        conn, task_id, _FAILURE_SIGNATURE_EVENT_KIND,
        payload,
        run_id=run_id,
    )
    return sig


def _dependency_wait_info(
    conn: sqlite3.Connection,
    task_id: str,
    reason: Optional[str],
) -> dict:
    """Return the stable accounting identity for one dependency report.

    A dependency wait is specific to the unfinished parent set, not merely
    to the prose a worker happened to use.  Parent ids are sorted so the
    same graph state produces the same signature regardless of query order;
    the reason uses the existing failure-signature normalizer so timestamps,
    run ids, and other attempt-specific noise do not defeat the breaker.
    """
    rows = conn.execute(
        """SELECT p.id, p.status, p.policy_quarantined, p.policy_invalidated
             FROM tasks p
             JOIN task_links l ON l.parent_id = p.id
            WHERE l.child_id = ?""",
        (task_id,),
    ).fetchall()
    parent_ids = sorted(row["id"] for row in rows)
    unresolved_parent_ids = sorted(
        row["id"] for row in rows if not _parent_is_satisfied(row)
    )
    normalized_reason = normalize_failure_signature(reason)
    signature = (
        "dependency parents: "
        f"{', '.join(unresolved_parent_ids) if unresolved_parent_ids else '<none>'}; "
        f"reason: {normalized_reason or '<none>'}"
    )
    return {
        "signature": signature,
        "parent_ids": parent_ids,
        "unresolved_parent_ids": unresolved_parent_ids,
        "normalized_reason": normalized_reason,
    }


def _record_dependency_wait(
    conn: sqlite3.Connection,
    task_id: str,
    reason: Optional[str],
    *,
    dependency_info: Optional[dict] = None,
    run_id: Optional[int] = None,
) -> dict:
    """Record one genuine dependency wait through the signature breaker.

    Both worker and operator block paths call this helper.  A pending
    materialization uses the same signature construction for audit purposes,
    but does not call this helper and therefore does not enter the repeat
    window until its SLA expires.
    """
    info = dependency_info or _dependency_wait_info(conn, task_id, reason)
    if info["unresolved_parent_ids"]:
        info = dict(info)
        info["signature"] = _record_failure_signature(
            conn,
            task_id,
            info["signature"],
            run_id=run_id,
            context={
                "source": "dependency_wait",
                "unresolved_parent_ids": info["unresolved_parent_ids"],
                "dependency_reason": info["normalized_reason"],
            },
        )
    return info


def _mark_review_artifact_selection_required_in_txn(
    conn: sqlite3.Connection,
    review_task_id: str,
    *,
    reason: str,
    source_rework_event_id: Optional[int] = None,
    fix_task_id: Optional[str] = None,
) -> bool:
    """Hold a review so the dispatcher cannot send it back to stale bytes."""
    row = conn.execute(
        "SELECT status, current_run_id FROM tasks WHERE id = ?",
        (review_task_id,),
    ).fetchone()
    if row is None or row["current_run_id"] is not None:
        return False
    cur = conn.execute(
        "UPDATE tasks SET status = 'blocked', block_kind = 'needs_input', "
        "claim_lock = NULL, claim_expires = NULL, worker_pid = NULL, "
        "worker_started_at = NULL, worker_pgid = NULL, worker_sid = NULL "
        "WHERE id = ? AND status IN ('review', 'todo', 'ready') "
        "AND current_run_id IS NULL",
        (review_task_id,),
    )
    if cur.rowcount != 1:
        return False
    payload: dict[str, Any] = {
        "reason": reason,
        "failure_code": "artifact_selection_required",
    }
    if source_rework_event_id is not None:
        payload["source_rework_event_id"] = int(source_rework_event_id)
    if fix_task_id:
        payload["fix_task_id"] = fix_task_id
    _append_event(conn, review_task_id, "artifact_selection_required", payload)
    _append_event(
        conn,
        review_task_id,
        "blocked",
        {"kind": "needs_input", **payload},
    )
    return True


def _legacy_review_artifact_reconcile_in_txn(
    conn: sqlite3.Connection,
    *,
    now: int,
    limit: int,
    requested_ids: Optional[set[str]],
) -> tuple[int, int]:
    """Backfill exact legacy paths and advance unambiguous completed fixes."""
    if requested_ids is None:
        scope = ""
        params: list[Any] = []
    elif not requested_ids:
        return 0, 0
    else:
        placeholders = ",".join("?" for _ in requested_ids)
        scope = f" AND id IN ({placeholders})"
        params = sorted(requested_ids)

    rows = conn.execute(
        """SELECT id, status FROM tasks
             WHERE (
                 status IN ('review', 'todo', 'ready', 'running', 'done', 'blocked')
                 AND (
                     instr(COALESCE(body, ''), '/attachments/') > 0
                     OR EXISTS (
                         SELECT 1 FROM task_events e
                          WHERE e.task_id = tasks.id
                            AND e.kind = 'rework_requested'
                     )
                 )
             )""" + scope + " ORDER BY created_at, id LIMIT ?",
        (*params, int(limit)),
    ).fetchall()
    seeded = 0
    held = 0
    for row in rows:
        review_id = str(row["id"])
        current = get_current_review_artifact(conn, review_id)
        if current is None:
            matches = _legacy_review_artifact_matches(conn, review_id)
            if len(matches) == 1:
                try:
                    _seed_review_artifact_binding_in_txn(
                        conn, review_id, matches[0], now=now,
                    )
                    seeded += 1
                except ReviewArtifactError as exc:
                    if _mark_review_artifact_selection_required_in_txn(
                        conn, review_id, reason=str(exc),
                    ):
                        held += 1
            elif len(matches) > 1:
                if _mark_review_artifact_selection_required_in_txn(
                    conn,
                    review_id,
                    reason=(
                        f"{len(matches)} exact attachments match the pinned "
                        "review path"
                    ),
                ):
                    held += 1
            elif _body_references_attachment_location(conn, review_id):
                if _mark_review_artifact_selection_required_in_txn(
                    conn,
                    review_id,
                    reason="no attachment row matches the pinned review path",
                ):
                    held += 1

    # Completed historical fixes are eligible only when their latest rework
    # request names them and exactly one attachment row belongs to that fix.
    event_rows = conn.execute(
        "SELECT id, task_id, payload FROM task_events "
        "WHERE kind = 'rework_requested' ORDER BY id DESC LIMIT ?",
        (int(limit),),
    ).fetchall()
    seen_reviews: set[str] = set()
    for event in event_rows:
        review_id = str(event["task_id"])
        if review_id in seen_reviews:
            continue
        seen_reviews.add(review_id)
        try:
            payload = json.loads(event["payload"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        fix_id = str(payload.get("fix_task_id") or "").strip()
        if not fix_id:
            continue
        fix = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (fix_id,)
        ).fetchone()
        if fix is None or fix["status"] not in {"done", "archived"}:
            continue
        binding = get_current_review_artifact(conn, review_id)
        if binding is None:
            continue
        if conn.execute(
            "SELECT 1 FROM review_artifact_bindings "
            "WHERE review_task_id = ? AND source_rework_event_id = ?",
            (review_id, int(event["id"])),
        ).fetchone() is not None:
            continue
        candidates = list_attachments(conn, fix_id)
        if len(candidates) != 1:
            if _mark_review_artifact_selection_required_in_txn(
                conn,
                review_id,
                reason=(
                    f"completed fix {fix_id} has {len(candidates)} attachment "
                    "candidates; explicit artifact selection is required"
                ),
                source_rework_event_id=int(event["id"]),
                fix_task_id=fix_id,
            ):
                held += 1
            continue
        completion_event = conn.execute(
            "SELECT run_id FROM task_events "
            "WHERE task_id = ? AND kind = 'completed' "
            "ORDER BY id DESC LIMIT 1",
            (fix_id,),
        ).fetchone()
        source_run_id = (
            int(completion_event["run_id"])
            if completion_event is not None and completion_event["run_id"] is not None
            else None
        )
        try:
            _verify_review_artifact_binding(conn, binding)
            bind_review_artifact_in_txn(
                conn,
                review_id,
                candidates[0].id,
                fix_id,
                source_run_id,
                int(event["id"]),
                binding.generation,
                now,
            )
            seeded += 1
        except ReviewArtifactError as exc:
            if _mark_review_artifact_selection_required_in_txn(
                conn,
                review_id,
                reason=str(exc),
                source_rework_event_id=int(event["id"]),
                fix_task_id=fix_id,
            ):
                held += 1
    return seeded, held


def reconcile_dependency_waits(
    conn: sqlite3.Connection,
    *,
    now: Optional[int] = None,
    materialization_sla_seconds: int = DEFAULT_DEPENDENCY_MATERIALIZATION_SLA_SECONDS,
    limit: int = 200,
    task_ids: Optional[Iterable[str]] = None,
) -> DependencyReconcileResult:
    """Reconcile dependency materialization and rework projections.

    This is deliberately a bounded, single-writer sweep.  A rework request is
    the durable source of truth for the fix->review edge; the task projection
    may be repaired from that event, but arbitrary ``needs_input`` cards are
    never released merely because they happen to have a parent.
    """
    try:
        bounded_limit = max(1, min(int(limit), 2000))
    except (TypeError, ValueError):
        bounded_limit = 200
    current_time = int(time.time() if now is None else now)
    sla = _resolve_dependency_materialization_sla_seconds(
        materialization_sla_seconds,
    )
    requested_ids = {str(value) for value in task_ids} if task_ids is not None else None
    links_restored = 0
    waits_materialized = 0
    waits_rearmed = 0
    legacy_recovered = 0
    timed_out = 0
    artifact_backfilled = 0
    artifact_selection_required = 0

    def _payload(row: sqlite3.Row) -> dict:
        try:
            value = json.loads(row["payload"]) if row["payload"] else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            value = {}
        return value if isinstance(value, dict) else {}

    def _task_scope(column: str = "task_id") -> tuple[str, list[str]]:
        """Push an optional task-id selection below each sweep's LIMIT."""
        if requested_ids is None:
            return "", []
        if not requested_ids:
            return " AND 0", []
        placeholders = ",".join("?" for _ in requested_ids)
        return f" AND {column} IN ({placeholders})", sorted(requested_ids)

    def _parent_rows(task_id: str) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT p.id, p.status, p.policy_quarantined, p.policy_invalidated "
            "FROM tasks p JOIN task_links l ON l.parent_id = p.id "
            "WHERE l.child_id = ? ORDER BY p.id",
            (task_id,),
        ).fetchall()

    def _clear_to_ready(task_id: str) -> bool:
        cur = conn.execute(
            """UPDATE tasks
                  SET status = 'ready', block_kind = NULL,
                      current_run_id = NULL, claim_lock = NULL,
                      claim_expires = NULL, worker_pid = NULL,
                      worker_started_at = NULL, worker_pgid = NULL,
                      worker_sid = NULL
                WHERE id = ? AND status IN ('todo', 'blocked')
                  AND current_run_id IS NULL
                  AND claim_lock IS NULL
                  AND worker_pid IS NULL""",
            (task_id,),
        )
        return cur.rowcount == 1

    def _to_dependency(task_id: str) -> bool:
        cur = conn.execute(
            """UPDATE tasks
                  SET status = 'todo', block_kind = 'dependency',
                      current_run_id = NULL, claim_lock = NULL,
                      claim_expires = NULL, worker_pid = NULL,
                      worker_started_at = NULL, worker_pgid = NULL,
                      worker_sid = NULL
                WHERE id = ? AND status IN ('todo', 'blocked')
                  AND current_run_id IS NULL
                  AND claim_lock IS NULL
                  AND worker_pid IS NULL""",
            (task_id,),
        )
        return cur.rowcount == 1

    with write_txn(conn):
        (
            artifact_backfilled,
            artifact_selection_required,
        ) = _legacy_review_artifact_reconcile_in_txn(
            conn,
            now=current_time,
            limit=bounded_limit,
            requested_ids=requested_ids,
        )
        # Rework events are authoritative for the orientation and allow a
        # crashed reconciler or an older writer to repair the missing edge.
        event_scope, event_scope_params = _task_scope("task_id")
        event_query = (
            "SELECT id, task_id, payload FROM task_events "
            "WHERE kind = 'rework_requested'"
            + event_scope
            + " ORDER BY id DESC LIMIT ?"
        )
        event_rows = conn.execute(
            event_query, (*event_scope_params, bounded_limit),
        ).fetchall()
        for event in event_rows:
            review_id = event["task_id"]
            payload = _payload(event)
            fix_id = str(payload.get("fix_task_id") or "").strip()
            if not fix_id or fix_id == review_id:
                continue
            if conn.execute(
                "SELECT 1 FROM tasks WHERE id = ?", (review_id,)
            ).fetchone() is None or conn.execute(
                "SELECT 1 FROM tasks WHERE id = ?", (fix_id,)
            ).fetchone() is None:
                continue
            if conn.execute(
                "SELECT 1 FROM task_links WHERE parent_id = ? AND child_id = ?",
                (fix_id, review_id),
            ).fetchone() is not None:
                continue
            try:
                restored = _link_tasks_in_txn(
                    conn, fix_id, review_id, emit_event=False,
                )
            except (ArchitectureGateError, ValueError):
                # The original request already passed the same checks.  If a
                # policy was changed afterwards, leave the audit event intact
                # and let the operator resolve the now-invalid edge.
                continue
            if restored:
                links_restored += 1
                _append_event(
                    conn,
                    review_id,
                    "rework_link_restored",
                    {
                        "review_task_id": review_id,
                        "fix_task_id": fix_id,
                        "request_key": payload.get("request_key"),
                        "source_event_id": int(event["id"]),
                    },
                )

        pending_scope, pending_scope_params = _task_scope("id")
        pending_rows = conn.execute(
            "SELECT id FROM tasks WHERE status = 'todo' "
            "AND block_kind = 'dependency_pending'"
            + pending_scope
            + " ORDER BY created_at, id LIMIT ?",
            (*pending_scope_params, bounded_limit),
        ).fetchall()
        for task_row in pending_rows:
            task_id = task_row["id"]
            pending_event = conn.execute(
                "SELECT id, payload FROM task_events "
                "WHERE task_id = ? AND kind = 'dependency_pending' "
                "ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            if pending_event is None:
                baseline_ids: set[str] = set()
                materialize_by = current_time + sla
            else:
                pending_payload = _payload(pending_event)
                raw_baseline = pending_payload.get("baseline_parent_ids") or []
                baseline_ids = {
                    str(value) for value in raw_baseline
                    if isinstance(value, (str, int))
                }
                try:
                    materialize_by = int(pending_payload.get("materialize_by"))
                except (TypeError, ValueError):
                    materialize_by = current_time + sla
            parents = _parent_rows(task_id)
            parent_ids = {str(parent["id"]) for parent in parents}
            newly_linked = parent_ids - baseline_ids
            if newly_linked:
                if any(not _parent_is_satisfied(parent) for parent in parents):
                    if _to_dependency(task_id):
                        waits_materialized += 1
                        _append_event(
                            conn,
                            task_id,
                            "dependency_materialized",
                            {
                                "parent_ids": sorted(parent_ids),
                                "new_parent_ids": sorted(newly_linked),
                                "source_event_id": (
                                    int(pending_event["id"])
                                    if pending_event is not None else None
                                ),
                            },
                        )
                elif _clear_to_ready(task_id):
                    waits_rearmed += 1
                    _append_event(
                        conn,
                        task_id,
                        "dependency_rearmed",
                        {
                            "parent_ids": sorted(parent_ids),
                            "new_parent_ids": sorted(newly_linked),
                        },
                    )
                continue
            if current_time >= materialize_by:
                timeout_reason = (
                    "dependency materialization timeout: no fix card was linked "
                    f"by {materialize_by}"
                )
                cur = conn.execute(
                    """UPDATE tasks
                          SET status = 'blocked', block_kind = 'needs_input',
                              current_run_id = NULL, claim_lock = NULL,
                              claim_expires = NULL, worker_pid = NULL,
                              worker_started_at = NULL, worker_pgid = NULL,
                              worker_sid = NULL
                        WHERE id = ? AND status = 'todo'
                          AND block_kind = 'dependency_pending'""",
                    (task_id,),
                )
                if cur.rowcount == 1:
                    timed_out += 1
                    timeout_payload = {
                        "failure_code": "dependency_materialization_timeout",
                        "reason": timeout_reason,
                        "materialize_by": materialize_by,
                        "baseline_parent_ids": sorted(baseline_ids),
                    }
                    _append_event(
                        conn, task_id, "dependency_materialization_timeout",
                        timeout_payload,
                    )
                    _append_event(
                        conn,
                        task_id,
                        "blocked",
                        {"reason": timeout_reason, "kind": "needs_input", **timeout_payload},
                    )
                    _record_failure_signature(
                        conn,
                        task_id,
                        timeout_reason,
                        context={"failure_code": "dependency_materialization_timeout"},
                    )

        dependency_scope, dependency_scope_params = _task_scope("tasks.id")
        dependency_rows = conn.execute(
            "SELECT tasks.id, tasks.status, tasks.block_kind FROM tasks "
            "WHERE ((tasks.status = 'todo' AND tasks.block_kind = 'dependency') "
            "   OR (tasks.status = 'blocked' AND tasks.block_kind = 'needs_input' "
            "       AND EXISTS ("
            "           SELECT 1 FROM task_links candidate_link "
            "            WHERE candidate_link.child_id = tasks.id"
            "       )"
            "       AND EXISTS ("
            "           SELECT 1 FROM task_events provenance "
            "            WHERE provenance.task_id = tasks.id"
            "              AND ("
            "                  provenance.kind IN "
            "                      ('dependency_loop_detected', "
            "                       'dependency_materialization_timeout')"
            "                  OR (provenance.kind = 'blocked' AND ("
            "                      json_extract(provenance.payload, '$.kind') = 'dependency' "
            "                      OR json_extract(provenance.payload, '$.failure_code') "
            "                           = 'dependency_materialization_timeout' "
            "                      OR lower(COALESCE(json_extract(provenance.payload, "
            "                           '$.reason'), '')) LIKE '%dependency_unavailable%'"
            "                  ))"
            "              )"
            "       )"
            "   ))"
            + dependency_scope
            + " ORDER BY tasks.created_at, tasks.id LIMIT ?",
            (*dependency_scope_params, bounded_limit),
        ).fetchall()
        for task_row in dependency_rows:
            task_id = task_row["id"]
            parents = _parent_rows(task_id)
            if task_row["status"] == "todo" and task_row["block_kind"] == "dependency":
                if parents and all(_parent_is_satisfied(parent) for parent in parents):
                    if _clear_to_ready(task_id):
                        waits_rearmed += 1
                        _append_event(
                            conn,
                            task_id,
                            "dependency_rearmed",
                            {"parent_ids": sorted(parent["id"] for parent in parents)},
                        )
                continue

            # Only dependency-provenance hard blocks may recover here.  The
            # latest block-state event is authoritative, so an earlier
            # dependency report cannot release a later genuine human gate.
            state_event = None
            state_kinds = {
                "blocked", "block_loop_detected", "dependency_loop_detected",
                "dependency_materialization_timeout", "gave_up", "unblocked",
                "promoted", "promoted_manual", "rework_requested", "status",
                "completed", "archived",
            }
            for event in conn.execute(
                "SELECT kind, payload FROM task_events WHERE task_id = ? "
                "ORDER BY id DESC LIMIT 100",
                (task_id,),
            ).fetchall():
                if event["kind"] in state_kinds:
                    state_event = event
                    break
            if state_event is None:
                continue
            state_payload = _payload(state_event)
            dependency_origin = state_event["kind"] in {
                "dependency_loop_detected", "dependency_materialization_timeout",
            }
            if state_event["kind"] == "blocked":
                reason_text = str(state_payload.get("reason") or "").lower()
                dependency_origin = (
                    state_payload.get("kind") == "dependency"
                    or state_payload.get("failure_code") == "dependency_materialization_timeout"
                    or "dependency_unavailable" in reason_text
                )
            if not dependency_origin or not parents:
                continue
            unsatisfied = [parent for parent in parents if not _parent_is_satisfied(parent)]
            # Legacy hard blocks have no baseline from which to prove that a
            # parent was newly linked.  A fully satisfied existing parent set
            # is therefore not evidence of a fix and must remain blocked.
            if not unsatisfied:
                continue
            if _to_dependency(task_id):
                legacy_recovered += 1
                _append_event(
                    conn,
                    task_id,
                    "dependency_recovered",
                    {
                        "parent_ids": sorted(parent["id"] for parent in parents),
                        "unfinished_parent_ids": sorted(parent["id"] for parent in unsatisfied),
                        "status": "todo",
                        "source_event_kind": state_event["kind"],
                    },
                )

    return DependencyReconcileResult(
        links_restored=links_restored,
        waits_materialized=waits_materialized,
        waits_rearmed=waits_rearmed,
        legacy_recovered=legacy_recovered,
        timed_out=timed_out,
        artifact_backfilled=artifact_backfilled,
        artifact_selection_required=artifact_selection_required,
    )


def _dependency_hard_block_reason(info: dict, reported_reason: Optional[str]) -> str:
    """Explain the legacy BUILD-613 hard-block reason for old event data.

    New dependency declarations use ``dependency_pending`` instead.  Keeping
    this formatter preserves the provenance vocabulary used by legacy
    ``dependency_unavailable``/``dependency_loop_detected`` events.
    """
    if info["parent_ids"]:
        parent_context = (
            "no unfinished linked parent "
            f"(declared: {', '.join(info['parent_ids'])})"
        )
    else:
        parent_context = "no linked parent"
    detail = (reported_reason or "unspecified dependency wait").strip()
    return (
        "dependency_unavailable: artifact/capability unavailable; "
        f"{parent_context}; reported reason: {detail}"
    )


def _append_dependency_loop_event(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    signature: str,
    recurrences: int,
    limit: int,
    reason: Optional[str],
    run_id: Optional[int] = None,
) -> int:
    """Append the dependency-loop audit event without firing another hook."""
    return _append_event(
        conn,
        task_id,
        "dependency_loop_detected",
        {
            "signature": signature,
            "recurrences": recurrences,
            "limit": limit,
            "reason": reason,
        },
        run_id=run_id,
    )


def _resolve_failure_signature_repeat_threshold(
    threshold: Optional[int] = None,
) -> int:
    """Resolve the circuit breaker's identical-signature trip threshold.

    Resolution order: explicit ``threshold`` argument (dispatcher passes
    its resolved ``kanban.failure_signature_threshold`` config value) ->
    ``HERMES_KANBAN_FAILURE_SIGNATURE_THRESHOLD`` env var ->
    ``DEFAULT_FAILURE_SIGNATURE_REPEAT_THRESHOLD``. Values below 2 are
    rejected and fall through to the next source — a threshold of 1 would
    trip on the very first failure with no repetition at all, which is
    what the existing crash/timeout breaker (``failure_limit``) already
    covers; this breaker is specifically about *repeated identical*
    signatures, so it needs at least 2 samples to compare.
    """
    if threshold is not None:
        try:
            parsed = int(threshold)
        except (TypeError, ValueError):
            parsed = -1
        if parsed >= 2:
            return parsed
    raw = os.environ.get(
        "HERMES_KANBAN_FAILURE_SIGNATURE_THRESHOLD", ""
    ).strip()
    if raw:
        try:
            parsed = int(raw)
        except ValueError:
            parsed = -1
        if parsed >= 2:
            return parsed
    return DEFAULT_FAILURE_SIGNATURE_REPEAT_THRESHOLD


def _recent_failure_signatures(
    conn: sqlite3.Connection, task_id: str, limit: int,
) -> list[dict]:
    """Return up to ``limit`` most recent failure-signature records for
    ``task_id`` and any of its linked remediation children, newest first.

    Each record is ``{"task_id", "run_id", "signature", "created_at"}``.
    Including linked children (``task_links``) covers the "saga" shape
    from the BUILD-261 incident where a parent release/remediation card
    fans out a fresh child task per remediation attempt instead of
    looping the same ``task_id`` — each child's recorded failure counts
    toward the parent's breaker check.
    """
    ids = [task_id] + child_ids(conn, task_id)
    placeholders = ",".join("?" for _ in ids)
    # A ``completed`` event anywhere in the saga closes the breaker window:
    # signatures recorded at or before the latest success must not trip a
    # later, unrelated run. History stays in the event log for audit.
    reset_row = conn.execute(
        f"SELECT created_at, id FROM task_events "
        f"WHERE task_id IN ({placeholders}) AND kind = 'completed' "
        f"ORDER BY created_at DESC, id DESC LIMIT 1",
        (*ids,),
    ).fetchone()
    reset_clause = ""
    reset_params: tuple = ()
    if reset_row is not None:
        reset_clause = " AND (created_at > ? OR (created_at = ? AND id > ?))"
        reset_params = (
            reset_row["created_at"], reset_row["created_at"], reset_row["id"],
        )
    rows = conn.execute(
        f"SELECT task_id, run_id, payload, created_at FROM task_events "
        f"WHERE task_id IN ({placeholders}) AND kind = ?{reset_clause} "
        f"ORDER BY created_at DESC, id DESC LIMIT ?",
        (*ids, _FAILURE_SIGNATURE_EVENT_KIND, *reset_params, limit),
    ).fetchall()
    out = []
    for r in rows:
        try:
            payload = json.loads(r["payload"]) if r["payload"] else {}
        except (TypeError, ValueError):
            payload = {}
        record = {
            "task_id": r["task_id"],
            "run_id": r["run_id"],
            "signature": payload.get("signature", ""),
            "created_at": r["created_at"],
        }
        if payload.get("source") == "dependency_wait":
            record.update(
                {
                    "source": "dependency_wait",
                    "unresolved_parent_ids": payload.get(
                        "unresolved_parent_ids", []
                    ),
                    "dependency_reason": payload.get("dependency_reason", ""),
                }
            )
        out.append(record)
    return out


def check_failure_signature_breaker(
    conn: sqlite3.Connection, task_id: str, *, threshold: Optional[int] = None,
) -> Optional[dict]:
    """Return trip details if the last ``threshold`` failure signatures for
    ``task_id`` (and its remediation children) are identical, else
    ``None``.

    Called per ready task in ``dispatch_once`` before a respawn is
    allowed — distinct from (and checked before) ``check_respawn_guard``,
    which only defers a single tick. This breaker's trip is permanent
    (the task is blocked, not just skipped) because identical repeated
    failures mean the automated retries are not converging.

    Returns ``None`` when there are fewer than ``threshold`` recorded
    signatures, or when any window entry is empty/falsy, or when the
    window isn't all the same signature.
    """
    n = _resolve_failure_signature_repeat_threshold(threshold)
    records = _recent_failure_signatures(conn, task_id, n)
    if len(records) < n:
        return None
    window = records[:n]
    sigs = [r["signature"] for r in window]
    if not all(sigs) or len(set(sigs)) != 1:
        return None
    return {
        "signature": sigs[0],
        "threshold": n,
        "records": window,
    }


def _trip_failure_signature_breaker(
    conn: sqlite3.Connection, task_id: str, trip: dict,
) -> bool:
    """Halt the saga: block ``task_id`` (kind=``needs_input``) instead of
    letting the dispatcher respawn it again, and leave a comment with
    both signatures + run refs for the human who has to unblock it.

    Blocking goes through the normal ``block_task`` path, so it emits the
    same ``blocked`` task_event the gateway's ``_kanban_notifier_watcher``
    already polls and delivers to subscribers on whatever platform they're
    on (Telegram included) — reusing the existing alert path rather than
    inventing a new one, per the BUILD-261 spec.
    """
    refs = ", ".join(
        r["task_id"] + (f"#run{r['run_id']}" if r["run_id"] else "")
        for r in trip["records"]
    )
    dependency_trip = bool(trip["records"]) and all(
        record.get("source") == "dependency_wait"
        for record in trip["records"]
    )
    dependency_reason = next(
        (
            record.get("dependency_reason")
            for record in trip["records"]
            if record.get("dependency_reason")
        ),
        "",
    )
    reason = (
        f"circuit breaker: {trip['threshold']} consecutive identical "
        f"failure signatures — halting respawn (runs: {refs})"
    )
    comment = (
        "Release/remediation circuit breaker tripped (BUILD-261).\n\n"
        f"The last {trip['threshold']} recorded failures reduce to the "
        f"identical signature:\n\n    {trip['signature']}\n\n"
        f"Runs: {refs}\n\n"
        "Respawning again would very likely reproduce the same failure "
        "instead of converging — this is the non-convergent-saga pattern "
        "from the 2026-07-09 incident (repeated PRs merged without fixing "
        "the underlying failure). Blocked for human review instead of "
        "retrying automatically."
    )
    blocked = block_task(
        conn, task_id, reason=reason, summary=reason, kind="needs_input",
    )
    if blocked:
        if dependency_trip:
            with write_txn(conn):
                _append_dependency_loop_event(
                    conn,
                    task_id,
                    signature=trip["signature"],
                    recurrences=len(trip["records"]),
                    limit=trip["threshold"],
                    reason=dependency_reason or reason,
                )
        try:
            add_comment(conn, task_id, "circuit-breaker", comment)
        except ValueError:
            pass
    return blocked


def _error_fingerprint(error_text: str) -> str:
    """Normalize an error message for grouping identical failures.

    Strips host-specific details (PIDs, timestamps) so that errors
    with the same root cause produce the same fingerprint.
    """
    fp = re.sub(r'\bpid \d+\b', 'pid N', error_text[:80])
    fp = re.sub(r'\b\d{10,}\b', '<TS>', fp)
    return fp.lower().strip()


# ── Safe workspace diagnostics for crash/timeout forensics ─────────
_WORKSPACE_DIAG_MAX_BYTES = 4096
_WORKSPACE_DIAG_MAX_LINES = 50
_WORKSPACE_DIAG_REDACT_PATTERNS = [
    re.compile(r"(?:api_?key|token|secret|password|auth)\s*[:=]\s*\S+", re.I),
    re.compile(r"[A-Za-z0-9+/]{40,}={0,2}"),
    re.compile(r"sk-[A-Za-z0-9]{32,}"),
    re.compile(r"ghp_[A-Za-z0-9]{36,}"),
]


def _capture_workspace_diag(workspace_path: str) -> Optional[dict]:
    """Capture capped, redacted git status for a crashed worker's workspace.

    Returns ``None`` if the path does not exist or is not a git repo.
    Never returns full diffs — only status/diffstat lines.
    """
    if not workspace_path or not os.path.isdir(workspace_path):
        return None
    try:
        out = subprocess.run(
            ["git", "-C", workspace_path, "status", "--short", "--branch"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode != 0:
            return None
        raw = out.stdout.strip()
        if not raw:
            return {"git_repo": True, "dirty": False}
        lines = raw.split("\n")
        # Filter out branch-header lines (start with "## ") — they are
        # metadata, not file-level dirty signals.  If only branch headers
        # remain the workspace is clean.
        file_lines = [l for l in lines if not l.startswith("## ")]
        if not file_lines:
            branch = ""
            first_line = lines[0] if lines else ""
            if first_line.startswith("## "):
                branch = first_line[3:].split("...")[0].strip()
            return {"git_repo": True, "branch": branch or None, "dirty": False}
        capped_lines = file_lines[:_WORKSPACE_DIAG_MAX_LINES]
        capped = "\n".join(capped_lines)
        if len(capped) > _WORKSPACE_DIAG_MAX_BYTES:
            capped = capped[:_WORKSPACE_DIAG_MAX_BYTES] + "\n… [truncated]"
        for pat in _WORKSPACE_DIAG_REDACT_PATTERNS:
            capped = pat.sub("[REDACTED]", capped)
        branch = ""
        first_line = capped_lines[0] if capped_lines else ""
        if first_line.startswith("## "):
            branch = first_line[3:].split("...")[0].strip()
        return {
            "git_repo": True,
            "branch": branch or None,
            "dirty": True,
            "git_status_raw": capped,
        }
    except Exception:
        return None


def _is_workspace_dirty(workspace_path: str) -> bool:
    """Pure check: return True if *workspace_path* is a git repo with uncommitted/untracked changes.

    Thin wrapper around :func:`_capture_workspace_diag`. Never mutates state.
    Returns ``False`` when the path is not a git repo or does not exist.
    """
    diag = _capture_workspace_diag(workspace_path)
    return diag is not None and diag.get("dirty") is True


# Empirically ~96% of "clean exit without a terminal tool call" tasks complete
# on a later run (a goal-mode finalize nudge, or the model simply emitting the
# tool call next time), so a protocol violation is NOT deterministic — give it a
# bounded retry before the breaker trips instead of blocking on the first hit.
#
# The budget is a violation-only STREAK, not a share of the unified
# ``consecutive_failures`` counter: it counts consecutive clean-exit protocol
# violations (derived from run history by ``_protocol_violation_streak``), so
# earlier timeouts / nonzero exits neither consume nor extend it, and a
# below-budget violation does not tick the unified counter either. A per-task
# ``max_retries`` overrides this bound — the same "task override wins"
# precedence ``_record_task_failure`` documents for every other failure kind.
_PROTOCOL_VIOLATION_FAILURE_LIMIT = 3

# How far back to walk a task's closed runs when counting the violation
# streak. The streak trips at a handful of violations, so anything beyond a
# few dozen rows (violations interleaved with neutral rate-limited requeues)
# can only mean "way past the bound" anyway.
_PROTOCOL_VIOLATION_SCAN_LIMIT = 50


def _protocol_violation_streak(conn: sqlite3.Connection, task_id: str) -> int:
    """Count the task's trailing run of clean-exit protocol violations.

    Walks the task's closed runs newest-first — including the violation run
    ``detect_crashed_workers`` just closed — and counts how many in a row were
    clean-exit protocol violations:

    * ``rate_limited`` runs and the self-classifying no-failure defers
      (``_NO_FAILURE_DEFER_OUTCOMES``: delivery-gate / provider-availability /
      quota) are neutral and skipped: they say nothing about the task, exactly
      as they are neutral for the unified ``consecutive_failures`` counter. An
      intervening quota defer must not reset a genuine protocol-violation streak.
    * Any other closed run (completed, plain crash, timeout, spawn failure,
      reclaim, …) breaks the streak, so the bounded retry budget counts ONLY
      protocol violations — mixed failure kinds can neither consume nor
      extend it.

    Violation runs are recognized by the ``protocol_violation`` marker that
    ``detect_crashed_workers`` stamps into the run metadata; the violation
    error text is matched as a fallback for runs recorded before the marker
    existed.
    """
    streak = 0
    rows = conn.execute(
        "SELECT outcome, error, metadata FROM task_runs "
        "WHERE task_id = ? AND ended_at IS NOT NULL "
        "ORDER BY id DESC LIMIT ?",
        (task_id, _PROTOCOL_VIOLATION_SCAN_LIMIT),
    ).fetchall()
    for row in rows:
        outcome = row["outcome"] or ""
        if outcome == "rate_limited" or outcome in _NO_FAILURE_DEFER_OUTCOMES:
            continue
        if outcome == "crashed":
            is_violation = False
            raw_meta = row["metadata"]
            if raw_meta:
                try:
                    is_violation = bool(
                        json.loads(raw_meta).get("protocol_violation")
                    )
                except (ValueError, TypeError):
                    is_violation = False
            if not is_violation:
                is_violation = "protocol violation" in (row["error"] or "")
            if is_violation:
                streak += 1
                continue
        break
    return streak


def detect_crashed_workers(conn: sqlite3.Connection) -> list[str]:
    """Reclaim ``running`` tasks whose worker PID is no longer alive.

    Appends a ``crashed`` event and drops the task back to ``ready``.
    Different from ``release_stale_claims``: this checks liveness
    immediately rather than waiting for the claim TTL.

    Only considers tasks claimed by *this host* — PIDs from other hosts
    are meaningless here. The host-local check is enough because
    ``_default_spawn`` always runs the worker on the same host as the
    dispatcher (the whole design is single-host).

    When the reap registry shows the worker exited cleanly (rc=0) but
    the task was still ``running`` in the DB, treat it as a protocol
    violation (worker answered conversationally without calling
    ``kanban_complete`` / ``kanban_block``) and trip the circuit breaker
    on the first occurrence — retrying a worker whose CLI keeps
    returning 0 without a terminal transition just loops forever.

    When the reap registry shows the worker exited with the rate-limit
    sentinel (``KANBAN_RATE_LIMIT_EXIT_CODE``), the worker bailed on a
    provider quota wall, NOT a task failure. Such tasks are released back
    to ``ready`` WITHOUT counting a failure (so a long quota window can't
    trip the breaker) and stamped with a quota-blocker error so
    ``check_respawn_guard`` defers their respawn until the window clears.
    The ids are returned via the ``_last_rate_limited`` function attribute
    (the public return stays the crashed-only ``list[str]``).
    """
    crashed: list[str] = []
    rate_limited: list[str] = []
    # Per-crash details collected inside the main txn, used after it
    # closes to run ``_record_task_failure`` (which needs its own
    # write_txn so can't nest). ``protocol_violation`` flags the
    # clean-exit-but-still-running case, which is accounted against its
    # own bounded violation streak instead of the unified failure
    # counter (see the post-txn loop below).
    crash_details: list[tuple[str, int, str, bool, str]] = []
    # (task_id, pid, claimer, protocol_violation, error_text)
    with write_txn(conn):
        rows = conn.execute(
            "SELECT id, current_run_id, worker_pid, worker_started_at, worker_pgid, worker_sid, "
            "claim_lock, started_at, workspace_path "
            "FROM tasks "
            "WHERE status = 'running' AND worker_pid IS NOT NULL"
        ).fetchall()
        host_prefix = f"{_claimer_id().split(':', 1)[0]}:"
        for row in rows:
            # Only check liveness for claims owned by this host.
            lock = row["claim_lock"] or ""
            if not lock.startswith(host_prefix):
                continue
            # Skip liveness check inside the launch-window grace period
            # so a freshly-spawned worker isn't reclaimed before its PID
            # is visible on /proc.
            started_at = row["started_at"] if "started_at" in row.keys() else None
            if started_at is not None:
                grace = _resolve_crash_grace_seconds()
                if time.time() - started_at < grace:
                    continue
            if _pid_alive(row["worker_pid"]):
                continue

            # The session leader may have crashed while provider/CLI children
            # inherited its exact claim and kept running. Reap the attested
            # group before releasing the claim; otherwise the next tick can
            # spawn a duplicate beside those orphans.
            if (
                os.name != "nt"
                and row["worker_pgid"] is not None
                and _process_group_alive(int(row["worker_pgid"]))
            ):
                termination = _terminate_worker_for_task(
                    row["worker_pid"],
                    row["claim_lock"],
                    task_id=row["id"],
                    run_id=row["current_run_id"],
                    worker_started_at=row["worker_started_at"],
                    worker_pgid=row["worker_pgid"],
                    worker_sid=row["worker_sid"],
                )
                if _worker_survived_termination(termination):
                    _defer_reclaim_for_live_worker_in_txn(
                        conn,
                        row["id"],
                        row["claim_lock"],
                        int(time.time()),
                        termination,
                        reason="crashed_leader_group_alive",
                    )
                    continue

            pid = int(row["worker_pid"])
            kind, code = _classify_worker_exit(pid)
            rate_limited_exit = False
            if kind == "clean_exit":
                # Worker subprocess returned 0 but its task is still
                # ``running`` in the DB — it exited without calling
                # ``kanban_complete`` / ``kanban_block``. Overwhelmingly the
                # work itself succeeded and only the paperwork was skipped, so
                # a retry usually completes; the corrective sentence below is
                # surfaced to the retry worker via the prior-attempt error in
                # ``build_worker_context`` (guidance approach from #61817).
                protocol_violation = True
                error_text = (
                    "worker exited cleanly (rc=0) without calling "
                    "kanban_complete or kanban_block — protocol violation. "
                    "If the prior run already did the work, verify it and "
                    "report the result via kanban_complete; a run that ends "
                    "without a terminal kanban call counts as failed no "
                    "matter what it did."
                )
                event_kind = "protocol_violation"
                event_payload = {
                    "pid": pid,
                    "claimer": row["claim_lock"],
                    "exit_code": code,
                    # Durable marker for _protocol_violation_streak: _end_run
                    # copies this payload into the run metadata, which is how
                    # the violation-only retry budget is derived later.
                    "protocol_violation": True,
                }
            elif kind == "rate_limited":
                # Worker bailed because the provider rate-limited / exhausted
                # quota (EX_TEMPFAIL sentinel). This is NOT a task failure —
                # the task is fine, the account just hit a wall. Release it
                # back to ``ready`` so the respawn guard defers it until the
                # quota window clears, and crucially do NOT count a failure
                # (skip ``_record_task_failure``) so a long quota window can't
                # trip the circuit breaker and permanently block the card.
                protocol_violation = False
                rate_limited_exit = True
                error_text = (
                    f"pid {pid} exited rate-limited (quota wall) — "
                    f"requeued without counting a failure"
                )
                event_kind = "rate_limited"
                event_payload = {
                    "pid": pid,
                    "claimer": row["claim_lock"],
                    "exit_code": code,
                }
            else:
                protocol_violation = False
                if kind == "nonzero_exit":
                    error_text = f"pid {pid} exited with code {code}"
                elif kind == "signaled":
                    error_text = f"pid {pid} killed by signal {code}"
                else:
                    error_text = f"pid {pid} not alive"
                event_kind = "crashed"
                event_payload = {"pid": pid, "claimer": row["claim_lock"]}
                if code is not None and kind != "unknown":
                    event_payload["exit_kind"] = kind
                    event_payload["exit_code"] = code

            # Capture safe workspace diagnostics for crash/timeout
            # forensics.  Only runs when the task has a workspace_path
            # and the directory exists on this host.
            _ws_path = row["workspace_path"]
            if _ws_path:
                _ws_diag = _capture_workspace_diag(_ws_path)
                if _ws_diag:
                    event_payload["workspace_diag"] = _ws_diag

            cur = conn.execute(
                "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL, "
                "worker_started_at = NULL, worker_pgid = NULL, worker_sid = NULL "
                "WHERE id = ? AND status = 'running' "
                "  AND worker_pid = ? AND claim_lock IS ?",
                (row["id"], pid, row["claim_lock"]),
            )
            if cur.rowcount == 1:
                # Rate-limited requeues are a clean release, not a crash —
                # record the run outcome as ``rate_limited`` so the board
                # history doesn't show a phantom crash for a quota wall.
                _run_outcome = "rate_limited" if rate_limited_exit else "crashed"
                run_id = _end_run(
                    conn, row["id"],
                    outcome=_run_outcome, status=_run_outcome,
                    error=error_text,
                    metadata=dict(event_payload),
                )
                _append_event(
                    conn, row["id"], event_kind,
                    event_payload,
                    run_id=run_id,
                )
                if rate_limited_exit:
                    # Stamp the failure-error column so ``check_respawn_guard``
                    # recognizes this as a quota blocker and defers the
                    # respawn until the window clears — WITHOUT touching
                    # ``consecutive_failures`` (that's the whole point: no
                    # breaker trip on a throttle).
                    conn.execute(
                        "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
                        (error_text[:500], row["id"]),
                    )
                    rate_limited.append(row["id"])
                else:
                    if protocol_violation:
                        # Stamp the failure error now: a below-budget
                        # violation never reaches ``_record_task_failure``
                        # (which stamps this column for every other failure
                        # kind), yet the board UI and the retry worker's
                        # context still need the violation message + the
                        # corrective guidance it carries.
                        conn.execute(
                            "UPDATE tasks SET last_failure_error = ? "
                            "WHERE id = ?",
                            (error_text[:500], row["id"]),
                        )
                    crashed.append(row["id"])
                    crash_details.append(
                        (row["id"], pid, row["claim_lock"],
                         protocol_violation, error_text)
                    )
    # Outside the main txn: account each crashed task and maybe trip the
    # breaker (the task transitions ready → blocked with a ``gave_up`` event
    # on top of the event we already emitted).
    #
    # Protocol-violation crashes (clean exit, no terminal tool call) get a
    # BOUNDED retry, not an immediate trip: empirically ~96% of these tasks
    # complete on a later run (a goal-mode finalize nudge, or the model simply
    # emitting kanban_complete/kanban_block next time), so blocking on the first
    # occurrence just churned them through the respawn cycle. The retry budget
    # is a violation-only streak (``_protocol_violation_streak``): earlier
    # timeouts / nonzero exits neither consume nor extend it, and a
    # below-budget violation does not tick the unified
    # ``consecutive_failures`` counter, so the two budgets stay independent.
    # A per-task ``max_retries`` overrides the violation bound with the same
    # top precedence it has for every other failure kind. Systemic same-error
    # crashes still trip immediately.
    auto_blocked: list[str] = []
    if crash_details:
        # Fingerprint errors to detect systemic failures.
        _fp_counts: dict[str, int] = {}
        for _, _, _, _, err_text in crash_details:
            fp = _error_fingerprint(err_text)
            _fp_counts[fp] = _fp_counts.get(fp, 0) + 1
        for tid, pid, claimer, protocol_violation, error_text in crash_details:
            if protocol_violation:
                streak = _protocol_violation_streak(conn, tid)
                trow = conn.execute(
                    "SELECT max_retries FROM tasks WHERE id = ?", (tid,),
                ).fetchone()
                if trow is None:
                    continue  # task deleted mid-loop
                task_override = (
                    trow["max_retries"] if "max_retries" in trow.keys() else None
                )
                violation_limit = (
                    int(task_override)
                    if task_override is not None
                    else _PROTOCOL_VIOLATION_FAILURE_LIMIT
                )
                if streak < violation_limit:
                    # Below budget: the task is already back at ``ready``
                    # (respawn allowed) with ``last_failure_error`` stamped.
                    # Deliberately no ``_record_task_failure`` call — a
                    # below-budget violation must not consume the unified
                    # failure budget, just as other failure kinds don't
                    # consume this one.
                    continue
                # Streak reached the bound: trip the breaker. ``force_trip``
                # skips the threshold resolution inside
                # ``_record_task_failure`` because the decision — including
                # the per-task ``max_retries`` override — was already made
                # against the violation streak above.
                tripped = _record_task_failure(
                    conn, tid,
                    error=error_text,
                    outcome="crashed",
                    failure_limit=violation_limit,
                    force_trip=True,
                    release_claim=False,
                    end_run=False,
                    event_payload_extra={
                        "pid": pid,
                        "claimer": claimer,
                        "protocol_violations": streak,
                        "protocol_violation_limit": violation_limit,
                    },
                )
                if tripped:
                    auto_blocked.append(tid)
                continue
            fp = _error_fingerprint(error_text)
            is_systemic = _fp_counts.get(fp, 0) >= 3
            tripped = _record_task_failure(
                conn, tid,
                error=error_text,
                outcome="crashed",
                failure_limit=1 if is_systemic else None,
                release_claim=False,
                end_run=False,
                event_payload_extra={"pid": pid, "claimer": claimer},
            )
            if tripped:
                auto_blocked.append(tid)
    # Stash auto-blocked ids on the function for the dispatch loop to pick up.
    # Keeps the public return type (``list[str]``) stable for direct callers
    # and tests that destructure the result; ``dispatch_once`` reads this
    # side-channel attribute to populate ``DispatchResult.auto_blocked``.
    detect_crashed_workers._last_auto_blocked = auto_blocked  # type: ignore[attr-defined]
    # Same side-channel for rate-limited requeues — these did NOT count a
    # failure and are NOT crashes, so they stay out of the ``crashed`` return.
    detect_crashed_workers._last_rate_limited = rate_limited  # type: ignore[attr-defined]
    return crashed


def _record_task_failure(
    conn: sqlite3.Connection,
    task_id: str,
    error: str,
    *,
    outcome: str,
    failure_limit: int = None,
    force_trip: bool = False,
    release_claim: bool = False,
    end_run: bool = False,
    event_payload_extra: Optional[dict] = None,
    expected_run_id: Optional[int] = None,
    expected_claim_lock: Optional[str] = None,
) -> Optional[bool]:
    """Record a non-success outcome (spawn_failed / crashed / timed_out)
    and maybe trip the circuit breaker.

    Unified replacement for the old spawn-only ``_record_spawn_failure``.
    Every path that ends a task with a non-success outcome funnels
    through here so the ``consecutive_failures`` counter and the
    auto-block threshold stay consistent.

    Returns True when the task was auto-blocked (counter reached
    ``failure_limit``), False when it was just updated in place, and None
    when an expected active run/claim no longer owns the task.

    Modes:

    * ``release_claim=True, end_run=True`` — spawn-failure path.
      Caller has a running task with an open run; this transitions
      it back to ``ready`` (or ``blocked`` when the breaker trips),
      releases the claim, and closes the run with ``outcome=<outcome>``.

    * ``release_claim=False, end_run=False`` — timeout/crash path.
      Caller has ALREADY flipped the task to ``ready`` and closed the
      run with the appropriate outcome. This just increments the
      counter; if the breaker trips, the task is re-transitioned
      ``ready → blocked`` and a ``gave_up`` event is emitted.

    ``event_payload_extra`` merges into the ``gave_up`` event payload
    when the breaker trips, so callers can include outcome-specific
    context (e.g. pid on crash, elapsed on timeout).

    Resolution order for the effective threshold:
      1. per-task ``max_retries`` if set (nothing else overrides)
      2. caller-supplied ``failure_limit`` (gateway passes the config
         value from ``kanban.failure_limit``; tests pass fixed values)
      3. ``DEFAULT_FAILURE_LIMIT``

    ``force_trip=True`` trips the breaker unconditionally, skipping the
    counter-vs-threshold comparison (the resolution order above is then
    only reported in the ``gave_up`` payload, not re-evaluated). Callers
    use it when they have already applied their own bounded-retry policy
    — e.g. the clean-exit protocol-violation streak in
    ``detect_crashed_workers``, which resolves the per-task
    ``max_retries`` override against the violation streak itself. The
    failure is still counted into ``consecutive_failures``.
    """
    if failure_limit is None:
        failure_limit = DEFAULT_FAILURE_LIMIT
    expected_ownership = (
        expected_run_id is not None or expected_claim_lock is not None
    )
    blocked = False
    with write_txn(conn):
        row = conn.execute(
            "SELECT consecutive_failures, status, max_retries, "
            "current_run_id, claim_lock "
            "FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if row is None:
            return False
        if expected_ownership and (
            expected_run_id is None
            or expected_claim_lock is None
            or row["status"] != "running"
            or row["current_run_id"] != int(expected_run_id)
            or row["claim_lock"] != expected_claim_lock
        ):
            return None
        failures = int(row["consecutive_failures"]) + 1
        # Per-task override wins over both caller-supplied and default
        # thresholds. None (the common case) falls through.
        task_override = (
            row["max_retries"] if "max_retries" in row.keys() else None
        )
        if task_override is not None:
            effective_limit = int(task_override)
            limit_source = "task"
        else:
            effective_limit = int(failure_limit)
            limit_source = "dispatcher"

        if force_trip or failures >= effective_limit:
            # Trip the breaker.
            if release_claim:
                # Spawn path: still running, also clear claim state.
                update = conn.execute(
                    "UPDATE tasks SET status = 'blocked', claim_lock = NULL, "
                    "claim_expires = NULL, worker_pid = NULL, "
                    "worker_started_at = NULL, worker_pgid = NULL, worker_sid = NULL, "
                    "consecutive_failures = ?, last_failure_error = ? "
                    "WHERE id = ? AND "
                    + (
                        "status = 'running'"
                        if expected_ownership
                        else "status IN ('running', 'ready')"
                    )
                    + (
                        " AND current_run_id = ? AND claim_lock = ?"
                        if expected_ownership else ""
                    ),
                    (
                        failures, error[:500], task_id,
                        *((int(expected_run_id), expected_claim_lock)
                          if expected_ownership else ()),
                    ),
                )
            else:
                # Timeout/crash path: task is already at ``ready``
                # with claim cleared; just flip to blocked + update
                # counter fields.
                update = conn.execute(
                    "UPDATE tasks SET status = 'blocked', "
                    "consecutive_failures = ?, last_failure_error = ? "
                    "WHERE id = ? AND status IN ('ready', 'running')"
                    + (
                        " AND current_run_id = ? AND claim_lock = ?"
                        if expected_ownership else ""
                    ),
                    (
                        failures, error[:500], task_id,
                        *((int(expected_run_id), expected_claim_lock)
                          if expected_ownership else ()),
                    ),
                )
            if expected_ownership and update.rowcount != 1:
                return None
            run_id = None
            if end_run:
                # Only the spawn path has an open run to close.
                run_id = _end_run(
                    conn, task_id,
                    outcome="gave_up", status="gave_up",
                    error=error[:500],
                    metadata={
                        "failures": failures,
                        "trigger_outcome": outcome,
                        "effective_limit": effective_limit,
                        "limit_source": limit_source,
                    },
                )
            payload = {
                "failures": failures,
                "effective_limit": effective_limit,
                "limit_source": limit_source,
                "error": error[:500],
                "trigger_outcome": outcome,
            }
            if event_payload_extra:
                payload.update(event_payload_extra)
            _append_event(
                conn, task_id, "gave_up", payload, run_id=run_id,
            )
            blocked = True
        else:
            # Below threshold.
            if release_claim:
                # Spawn path: transition running → ready + clear claim.
                update = conn.execute(
                    "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
                    "claim_expires = NULL, worker_pid = NULL, "
                    "worker_started_at = NULL, worker_pgid = NULL, worker_sid = NULL, "
                    "consecutive_failures = ?, last_failure_error = ? "
                    "WHERE id = ? AND status = 'running'"
                    + (
                        " AND current_run_id = ? AND claim_lock = ?"
                        if expected_ownership else ""
                    ),
                    (
                        failures, error[:500], task_id,
                        *((int(expected_run_id), expected_claim_lock)
                          if expected_ownership else ()),
                    ),
                )
            else:
                # Timeout/crash path: task is already at ``ready`` via
                # its own UPDATE. Just bookkeep the counter + last error.
                update = conn.execute(
                    "UPDATE tasks SET consecutive_failures = ?, "
                    "last_failure_error = ? WHERE id = ?",
                    (failures, error[:500], task_id),
                )
            if expected_ownership and update.rowcount != 1:
                return None
            if end_run:
                # Spawn path: close the open run with outcome.
                run_id = _end_run(
                    conn, task_id,
                    outcome=outcome, status=outcome,
                    error=error[:500],
                    metadata={"failures": failures},
                )
                _append_event(
                    conn, task_id, outcome,
                    {"error": error[:500], "failures": failures},
                    run_id=run_id,
                )
            # Timeout/crash path's caller already emitted its own event.
    return blocked


# Backward-compat alias. Old name is referenced from tests and possibly
# third-party callers. New code should call ``_record_task_failure``.
def _record_spawn_failure(
    conn: sqlite3.Connection,
    task_id: str,
    error: str,
    *,
    failure_limit: int = None,
    expected_run_id: Optional[int] = None,
    expected_claim_lock: Optional[str] = None,
) -> Optional[bool]:
    return _record_task_failure(
        conn, task_id, error,
        outcome="spawn_failed",
        failure_limit=failure_limit,
        release_claim=True,
        end_run=True,
        expected_run_id=expected_run_id,
        expected_claim_lock=expected_claim_lock,
    )


def _block_workspace_contract_violation(
    conn: sqlite3.Connection,
    result: "DispatchResult",
    task_id: str,
    exc: WorkspaceContractError,
) -> None:
    """Route a deterministic worktree-contract violation straight to blocked.

    BUILD-496 invariant 7: a structurally-impossible worktree anchor cannot
    be fixed by respawning, so retrying (the default ``_record_spawn_failure``
    path) only burns ``failure_limit`` attempts producing the identical error
    before it finally gives up. Block now instead. ``block_task`` releases the
    claim, ends the run, emits the ``blocked`` task_event the gateway notifier
    already delivers (TERMINAL_KINDS), and records the normalized failure
    signature — reusing the same terminal alert path as the BUILD-261
    signature breaker rather than inventing a new one. Note the signature
    breaker at ``check_failure_signature_breaker`` deliberately excludes the
    spawn funnel; we bypass it on purpose by blocking on first occurrence.
    The reason carries the typed ``code`` and only structural detail (task id,
    board, path) — never credentials or unbounded stderr.
    """
    _log.warning(
        "kanban dispatch: deterministic workspace contract violation "
        "for %s [%s]: %s", task_id, exc.code, exc,
    )
    reason = f"workspace_contract:{exc.code}: {exc}"
    result.spawn_errors.append((task_id, reason))
    if block_task(conn, task_id, reason=reason, summary=reason, kind="needs_input"):
        result.auto_blocked.append(task_id)


def _set_worker_pid(
    conn: sqlite3.Connection,
    task_id: str,
    pid: int,
    *,
    run_id: Optional[int] = None,
    claim_lock: Optional[str] = None,
    worker_started_at: Optional[float] = None,
    worker_pgid: Optional[int] = None,
    worker_sid: Optional[int] = None,
) -> None:
    """Atomically attach ``pid`` to the exact active task run.

    The historical implementation performed two unguarded updates and then
    reported success even if the task had been reclaimed between spawn and
    persistence. This compare-and-swap refuses stale ownership, updates the
    task and run together, and reads the tuple back before emitting the
    ``spawned`` event. Optional identifiers preserve the helper's public test
    surface while still deriving and enforcing the active tuple.
    """
    worker_pid = int(pid)
    if worker_pid <= 0:
        raise RuntimeError(f"worker PID must be positive, got {worker_pid}")

    with write_txn(conn):
        active = conn.execute(
            "SELECT status, current_run_id, claim_lock FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if active is None or active["status"] != "running":
            raise RuntimeError(f"task {task_id} is no longer running")
        active_run_id = (
            int(active["current_run_id"])
            if active["current_run_id"] is not None
            else None
        )
        active_lock = active["claim_lock"]
        expected_run_id = int(run_id) if run_id is not None else active_run_id
        expected_lock = claim_lock if claim_lock is not None else active_lock
        if active_run_id is None or expected_run_id != active_run_id:
            raise RuntimeError(f"task {task_id} active run changed before PID attach")
        if not expected_lock or expected_lock != active_lock:
            raise RuntimeError(f"task {task_id} claim changed before PID attach")

        task_update = conn.execute(
            """
            UPDATE tasks
               SET worker_pid = ?, worker_started_at = ?,
                   worker_pgid = ?, worker_sid = ?
             WHERE id = ?
               AND status = 'running'
               AND current_run_id = ?
               AND claim_lock = ?
               AND (worker_pid IS NULL OR worker_pid = ?)
            """,
            (
                worker_pid,
                worker_started_at,
                worker_pgid,
                worker_sid,
                task_id,
                expected_run_id,
                expected_lock,
                worker_pid,
            ),
        )
        run_update = conn.execute(
            """
            UPDATE task_runs
               SET worker_pid = ?, worker_started_at = ?,
                   worker_pgid = ?, worker_sid = ?
             WHERE id = ?
               AND task_id = ?
               AND status = 'running'
               AND ended_at IS NULL
               AND claim_lock = ?
               AND (worker_pid IS NULL OR worker_pid = ?)
            """,
            (
                worker_pid,
                worker_started_at,
                worker_pgid,
                worker_sid,
                expected_run_id,
                task_id,
                expected_lock,
                worker_pid,
            ),
        )
        if task_update.rowcount != 1 or run_update.rowcount != 1:
            raise RuntimeError(f"task {task_id} lost ownership before PID attach")

        attached = conn.execute(
            """
            SELECT t.worker_pid AS task_pid, r.worker_pid AS run_pid,
                   t.worker_started_at AS task_started_at,
                   r.worker_started_at AS run_started_at,
                   t.worker_pgid AS task_pgid, r.worker_pgid AS run_pgid,
                   t.worker_sid AS task_sid, r.worker_sid AS run_sid
              FROM tasks t
              JOIN task_runs r ON r.id = t.current_run_id
             WHERE t.id = ? AND t.status = 'running'
               AND t.current_run_id = ? AND t.claim_lock = ?
               AND r.task_id = t.id AND r.status = 'running'
               AND r.ended_at IS NULL AND r.claim_lock = ?
            """,
            (task_id, expected_run_id, expected_lock, expected_lock),
        ).fetchone()
        if (
            attached is None
            or attached["task_pid"] != worker_pid
            or attached["run_pid"] != worker_pid
            or attached["task_started_at"] != worker_started_at
            or attached["run_started_at"] != worker_started_at
            or attached["task_pgid"] != worker_pgid
            or attached["run_pgid"] != worker_pgid
            or attached["task_sid"] != worker_sid
            or attached["run_sid"] != worker_sid
        ):
            raise RuntimeError(f"task {task_id} PID attach readback failed")
        _append_event(
            conn,
            task_id,
            "spawned",
            {
                "pid": worker_pid,
                "process_started_at": worker_started_at,
                "process_group_id": worker_pgid,
                "session_id": worker_sid,
                "run_id": expected_run_id,
            },
            run_id=expected_run_id,
        )


def _coerce_spawn_receipt(value: Any) -> SpawnReceipt:
    """Require explicit process ownership and gate controls from spawners."""
    if not isinstance(value, SpawnReceipt):
        raise RuntimeError("spawn function must return an owned SpawnReceipt")
    receipt = value
    if receipt.pid <= 0:
        raise RuntimeError("spawn function returned a non-positive worker PID")
    if receipt.process_started_at is None or float(receipt.process_started_at) <= 0:
        raise RuntimeError("spawn receipt must include worker process birth identity")
    if os.name != "nt" and (
        receipt.process_group_id != receipt.pid or receipt.session_id != receipt.pid
    ):
        raise RuntimeError(
            "spawn receipt must identify its dedicated worker process group/session"
        )
    if not callable(receipt.release) or not callable(receipt.abort):
        raise RuntimeError("spawn receipt must provide release and abort controls")
    return receipt


def _spawn_and_attach_worker(
    conn: sqlite3.Connection,
    task: Task,
    workspace: str,
    spawn_fn: Callable[..., Any],
    *,
    board: Optional[str],
) -> int:
    """Start a worker, durably attach its exact run, then open its gate."""
    import inspect

    receipt: Optional[SpawnReceipt] = None
    try:
        try:
            sig: Optional[inspect.Signature] = inspect.signature(spawn_fn)
        except (TypeError, ValueError):
            # Signature introspection genuinely failed (e.g. an
            # unintrospectable callable) -- fall back to the legacy
            # two-argument compatibility path below. This is the ONLY
            # TypeError/ValueError this function catches: it never wraps
            # the call to spawn_fn itself, so an exception raised from
            # inside spawn_fn's own body (e.g. ContinuationContractError,
            # a ValueError subclass) always propagates unchanged instead
            # of being mistaken for an arity mismatch and silently retried.
            sig = None
        accepts_board = sig is not None and "board" in sig.parameters
        if accepts_board:
            assert sig is not None
            # Decide (and validate) the invocation form BEFORE executing
            # spawn_fn. bind() only inspects the signature -- it never
            # runs any of spawn_fn's own code -- so any TypeError it
            # raises here is a genuine, pre-invocation arity problem, not
            # something from inside the function body, and is allowed to
            # propagate uncaught.
            sig.bind(task, workspace, board=board)
            raw_receipt = spawn_fn(task, workspace, board=board)
        else:
            # Legacy two-argument spawn_fn: a PRESELECTED compatibility
            # path, never a fallback retry after a failed call. A
            # two-argument spawn_fn has no way to honour ``board`` and
            # always resolves the kanban DB implicitly via board=None, so
            # it is only safe to invoke when that implicit resolution
            # agrees with the database this dispatch actually claimed the
            # task from. Refuse BEFORE invocation otherwise -- omission
            # must never silently redirect database resolution.
            if kanban_db_path(board=board) != kanban_db_path(board=None):
                raise RuntimeError(
                    "spawn_fn must accept board for a board-scoped "
                    f"dispatch (board={board!r}): a legacy two-argument "
                    "spawn_fn would resolve the kanban database as "
                    f"{kanban_db_path(board=None)} instead of the "
                    f"dispatched board's {kanban_db_path(board=board)}"
                )
            if sig is not None:
                sig.bind(task, workspace)
            raw_receipt = spawn_fn(task, workspace)
        receipt = _coerce_spawn_receipt(raw_receipt)
        if task.current_run_id is None or not task.claim_lock:
            raise RuntimeError(f"task {task.id} has no active run ownership")
        _set_worker_pid(
            conn,
            task.id,
            receipt.pid,
            run_id=task.current_run_id,
            claim_lock=task.claim_lock,
            worker_started_at=receipt.process_started_at,
            worker_pgid=receipt.process_group_id,
            worker_sid=receipt.session_id,
        )
        receipt.release()
        return receipt.pid
    except Exception:
        if receipt is not None:
            try:
                receipt.abort()
            except Exception:
                _log.warning(
                    "failed to abort unattached worker pid=%s task=%s",
                    receipt.pid,
                    task.id,
                    exc_info=True,
                )
        raise


def _clear_failure_counter(conn: sqlite3.Connection, task_id: str) -> None:
    """Reset the unified consecutive-failures counter.

    Called from ``complete_task`` on successful completion — a fresh
    success means the task + profile combination is working and any
    past failures are history. NOT called on spawn success anymore:
    a successful spawn proves the worker could start but says nothing
    about whether the run will succeed, so we need to let timeouts and
    crashes accumulate across spawn boundaries.
    """
    with write_txn(conn):
        conn.execute(
            "UPDATE tasks SET consecutive_failures = 0, "
            "last_failure_error = NULL WHERE id = ?",
            (task_id,),
        )


# Legacy alias for test-code and anything else that still imports it.
_clear_spawn_failures = _clear_failure_counter


def check_respawn_guard(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """Return a guard reason if ``task_id`` should NOT be re-spawned, else None.

    Called per ready task in ``dispatch_once`` before any claim attempt.
    Returning a reason defers the spawn this tick; the task stays in
    ``ready`` and gets another chance on the next dispatcher tick.

    Checks in priority order:

    ``"rate_limit_cooldown"``
        The task's most recent run ended with the ``rate_limited`` outcome
        (a worker bailed on a provider quota wall via the EX_TEMPFAIL
        sentinel) within ``_resolve_rate_limit_cooldown_seconds()``. The
        quota almost certainly hasn't reset yet, so defer the respawn until
        the cooldown elapses — then allow a cheap probe. This is checked
        BEFORE ``blocker_auth`` because the rate-limit requeue stamps a
        quota-flavored ``last_failure_error`` that would otherwise match the
        auth-blocker regex and park the task forever (the rate-limit path
        never increments ``consecutive_failures``, so the breaker can't free
        it). Once the cooldown elapses the task falls through and respawns.

    ``"delivery_authorization_cooldown"``
        The previous worker requeued itself because the canonical delivery
        gate resolver was transiently unavailable. Retry after a short delay
        without incrementing the task failure circuit breaker.

    ``"blocker_auth"``
        The task's last failure error matches a quota / authentication
        pattern. Retrying immediately is unlikely to help (rate limits
        reset on a timer; auth needs human action), so we defer to the
        next tick. The existing ``consecutive_failures`` counter still
        trips the auto-block circuit breaker after ``failure_limit``
        consecutive failures, so a persistent auth error eventually
        blocks via the normal path — but a transient 429 gets a few
        ticks of recovery first.

    ``"recent_success"``
        A completed run exists within ``_RESPAWN_GUARD_SUCCESS_WINDOW``
        seconds.  Useful work already succeeded for this task; wait for
        human review rather than immediately re-spawning. Bypassed when an
        explicit re-queue event (status change, promote, unblock, reclaim)
        arrives AFTER that completion — that's a deliberate re-run request.

    Stale / dead claim locks are NOT a guard reason — they are handled
    by ``release_stale_claims`` and ``detect_crashed_workers`` which
    reset the task to ``ready`` only after verifying the lock is
    genuinely dead (no live PID on this host).
    """
    row = conn.execute(
        "SELECT last_failure_error FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return None

    now = int(time.time())

    # 1. Rate-limit cooldown. The most recent run ended ``rate_limited``
    #    (quota wall) — defer while inside the cooldown window, then allow a
    #    cheap probe. Must run BEFORE the blocker_auth regex check, because a
    #    rate-limit requeue stamps a quota-flavored last_failure_error that
    #    the regex would otherwise match → defer forever (no failure counter
    #    increment on this path means the breaker can never free it).
    #
    #    We look at the LATEST run only (ORDER BY ended_at DESC LIMIT 1): if a
    #    newer crash/completion superseded the rate-limit run, this guard
    #    no longer applies and the normal paths take over.
    rl_cooldown = _resolve_rate_limit_cooldown_seconds()
    latest_run = conn.execute(
        "SELECT outcome, ended_at FROM task_runs "
        "WHERE task_id = ? AND ended_at IS NOT NULL "
        # ended_at is second-resolution, so two runs closing in the same second
        # can tie; break the tie by run id (monotonic) so the genuinely newest
        # run wins and a quota defer is never masked by an older sibling.
        "ORDER BY ended_at DESC, id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if (
        latest_run is not None
        and latest_run["outcome"] in _NO_FAILURE_DEFER_OUTCOMES
    ):
        # QUOTA_UNAVAILABLE is a provider quota wall: space it by the long
        # rate-limit cooldown (~300s), not the 30s delivery cooldown, so a
        # long quota window is probed cheaply instead of thrashed (BUILD-734).
        if latest_run["outcome"] == QUOTA_UNAVAILABLE:
            cooldown = _resolve_rate_limit_cooldown_seconds()
            guard_reason = "quota_unavailable_cooldown"
        else:
            cooldown = _resolve_delivery_authorization_cooldown_seconds()
            guard_reason = "delivery_authorization_cooldown"
        ended_at = latest_run["ended_at"]
        if cooldown > 0 and ended_at is not None and now - int(ended_at) < cooldown:
            return guard_reason
        # These outcomes are self-classifying. Once the cooldown expires they
        # must not fall into the generic auth-error regex and defer forever
        # (the requeue never increments the failure counter, so the breaker
        # could not free them).
        return None
    if (
        latest_run is not None
        and latest_run["outcome"] == "rate_limited"
    ):
        if rl_cooldown <= 0:
            # Cooldown disabled — respawn immediately, and skip the
            # blocker_auth regex so the stamped rate-limit text doesn't
            # re-trap the task.
            return None
        ended_at = latest_run["ended_at"]
        if ended_at is not None and (now - int(ended_at)) < rl_cooldown:
            return "rate_limit_cooldown"
        # Cooldown elapsed — allow the respawn. Return early so the
        # blocker_auth check below doesn't catch the rate-limit text we
        # stamped on the task; this path intentionally retries forever
        # (cheaply, spaced by the cooldown) until quota returns or a real
        # crash/completion supersedes it.
        return None

    # 2. Quota / auth blocker: retrying immediately will not help.
    err = row["last_failure_error"]
    if err and _RESPAWN_BLOCKER_RE.search(err):
        return "blocker_auth"

    # 3. Completed run within guard window — proof of recent success.
    #    Exception: an explicit re-queue AFTER that success (an operator
    #    dragging done→ready, a dependency re-promotion, an unblock, a
    #    reclaim) is a deliberate "run it again" — honor it instead of
    #    deferring. Without this, a manual done→ready just sits there,
    #    silently held by the guard, until the window elapses.
    cutoff = now - _RESPAWN_GUARD_SUCCESS_WINDOW
    recent_completed = conn.execute(
        "SELECT ended_at FROM task_runs "
        "WHERE task_id = ? AND outcome = 'completed' AND ended_at >= ? "
        "ORDER BY ended_at DESC LIMIT 1",
        (task_id, cutoff),
    ).fetchone()
    if recent_completed:
        completed_at = int(recent_completed["ended_at"] or 0)
        requeued_after = conn.execute(
            "SELECT 1 FROM task_events "
            "WHERE task_id = ? AND created_at >= ? "
            "AND kind IN ('status', 'promoted', 'unblocked', 'reclaimed') "
            "LIMIT 1",
            (task_id, completed_at),
        ).fetchone()
        if not requeued_after:
            return "recent_success"

    # PR URLs in comments are intentionally diagnostic only. Publication lanes
    # must reconcile duplicate/open PRs idempotently; the dispatcher must not
    # infer scheduler state from prose and strand ready tasks behind stale PRs.
    return None


def has_spawnable_ready(conn: sqlite3.Connection) -> bool:
    """Return True iff there is at least one ready+assigned+unclaimed task
    whose assignee maps to a real Hermes profile.

    Used by the gateway- and CLI-embedded dispatchers' health telemetry to
    decide whether ``0 spawned`` is a "stuck" condition (real spawnable
    work waiting) or a "correctly idle" condition (only control-plane
    lanes like ``orion-cc`` / ``orion-research`` waiting on terminals
    that pull tasks via ``claim_task`` directly).

    Falls back to "any ready+assigned" if ``profile_exists`` is not
    importable (e.g. partial install) — preserves the old behavior so
    the warning still fires in degraded environments.
    """
    rows = conn.execute(
        "SELECT DISTINCT assignee FROM tasks "
        "WHERE status = 'ready' AND assignee IS NOT NULL "
        "    AND claim_lock IS NULL"
    ).fetchall()
    if not rows:
        return False
    try:
        from hermes_cli.profiles import profile_exists  # local import: avoids cycle
    except Exception:
        # Can't introspect — assume spawnable, preserve legacy behavior.
        return True
    for row in rows:
        if profile_exists(row["assignee"]):
            return True
    return False


def has_spawnable_review(conn: sqlite3.Connection) -> bool:
    """Return True iff there is at least one review+assigned+unclaimed task
    whose assignee maps to a real Hermes profile.

    Mirror of :func:`has_spawnable_ready` for the review column —
    used by the health telemetry to decide whether the dispatcher
    should have spawned a review agent.
    """
    rows = conn.execute(
        "SELECT DISTINCT assignee FROM tasks "
        "WHERE status = 'review' AND assignee IS NOT NULL "
        "    AND claim_lock IS NULL"
    ).fetchall()
    if not rows:
        return False
    try:
        from hermes_cli.profiles import profile_exists  # local import: avoids cycle
    except Exception:
        return True
    for row in rows:
        if profile_exists(row["assignee"]):
            return True
    return False


def _prepare_continuation_or_block(
    conn: sqlite3.Connection,
    task: Task,
    *,
    config: dict[str, Any],
) -> Optional[str]:
    """Prepare the gated bootstrap or stop a deterministic respawn loop."""
    if not continuation_runtime_enabled(config):
        return None
    if task.current_run_id is None:
        return "continuation_missing_run"
    try:
        prepare_run_continuation(
            conn, task.id, task.current_run_id, config=config,
        )
        return None
    except Exception as exc:
        code = str(getattr(exc, "code", type(exc).__name__))[:128]
        message = str(exc)[:2000]
        _log.warning(
            "kanban continuation bootstrap preparation failed for %s run %s: %s",
            task.id,
            task.current_run_id,
            message,
            exc_info=True,
        )
        try:
            record_continuation_bootstrap_failure(
                conn,
                task.id,
                task.current_run_id,
                code=code,
                message=message,
                phase="prepare",
            )
            block_task(
                conn,
                task.id,
                reason=f"Continuation bootstrap failed ({code}): {message}",
                summary="Fail-closed before worker start; inspect continuation status/events.",
                metadata={"failure_code": code, "phase": "prepare"},
                kind="capability",
                expected_run_id=task.current_run_id,
            )
        except Exception:
            _log.error(
                "failed to persist continuation bootstrap block for task %s",
                task.id,
                exc_info=True,
            )
        return f"continuation:{code}: {message}"


def dispatch_once(
    conn: sqlite3.Connection,
    *,
    spawn_fn=None,
    ttl_seconds: Optional[int] = None,
    dry_run: bool = False,
    max_spawn: Optional[int] = None,
    max_in_progress: Optional[int] = None,
    failure_limit: int = DEFAULT_SPAWN_FAILURE_LIMIT,
    stale_timeout_seconds: int = 0,
    board: Optional[str] = None,
    default_assignee: Optional[str] = None,
    max_in_progress_per_profile: Optional[int] = None,
    signature_repeat_threshold: Optional[int] = None,
    dependency_materialization_sla_seconds: Optional[int] = None,
) -> DispatchResult:
    """Run one dispatcher tick under the board's single-writer lock.

    Thin wrapper around :func:`_dispatch_once_locked`. It acquires a
    non-blocking, board-scoped dispatch lock (issue #35240) so that two
    dispatchers pointed at the same ``kanban.db`` — e.g. the service-
    managed gateway and a shell-spawned orphan that escaped the service
    cgroup — can never run a reclaim/spawn/write tick concurrently and
    race on WAL frames. The losing dispatcher returns an empty
    ``DispatchResult`` with ``skipped_locked=True`` and does no DB writes;
    the holder is already making progress on the same board.

    The lock is keyed off the board's resolved DB path, so unrelated
    boards tick in parallel. See :func:`_dispatch_tick_lock` for the
    cross-process / cross-platform mechanics.
    """
    try:
        db_path = kanban_db_path(board=board)
    except Exception:
        # Path resolution should never fail, but if it somehow does we
        # must not lose the tick — fall through to an unguarded dispatch
        # rather than dropping work.
        return _dispatch_once_locked(
            conn,
            spawn_fn=spawn_fn,
            ttl_seconds=ttl_seconds,
            dry_run=dry_run,
            max_spawn=max_spawn,
            max_in_progress=max_in_progress,
            failure_limit=failure_limit,
            stale_timeout_seconds=stale_timeout_seconds,
            board=board,
            default_assignee=default_assignee,
            max_in_progress_per_profile=max_in_progress_per_profile,
            signature_repeat_threshold=signature_repeat_threshold,
            dependency_materialization_sla_seconds=dependency_materialization_sla_seconds,
        )
    with _dispatch_tick_lock(db_path) as held:
        if not held:
            return DispatchResult(skipped_locked=True)
        return _dispatch_once_locked(
            conn,
            spawn_fn=spawn_fn,
            ttl_seconds=ttl_seconds,
            dry_run=dry_run,
            max_spawn=max_spawn,
            max_in_progress=max_in_progress,
            failure_limit=failure_limit,
            stale_timeout_seconds=stale_timeout_seconds,
            board=board,
            default_assignee=default_assignee,
            max_in_progress_per_profile=max_in_progress_per_profile,
            signature_repeat_threshold=signature_repeat_threshold,
            dependency_materialization_sla_seconds=dependency_materialization_sla_seconds,
        )


def _dispatch_once_locked(
    conn: sqlite3.Connection,
    *,
    spawn_fn=None,
    ttl_seconds: Optional[int] = None,
    dry_run: bool = False,
    max_spawn: Optional[int] = None,
    max_in_progress: Optional[int] = None,
    failure_limit: int = DEFAULT_SPAWN_FAILURE_LIMIT,
    stale_timeout_seconds: int = 0,
    board: Optional[str] = None,
    default_assignee: Optional[str] = None,
    max_in_progress_per_profile: Optional[int] = None,
    signature_repeat_threshold: Optional[int] = None,
    dependency_materialization_sla_seconds: Optional[int] = None,
) -> DispatchResult:
    """Run one dispatcher tick.

    Steps:
      1. Reclaim stale running tasks (TTL expired).
      2. Reclaim stale running tasks (no recent heartbeat).
      3. Reclaim crashed running tasks (host-local PID no longer alive).
      3. Promote todo -> ready where all parents are done.
      4. For each ready task with an assignee, atomically claim and call
         ``spawn_fn(task, workspace_path, board) -> SpawnReceipt``. The
         receipt must own both gate release and process-group abort controls;
         bare integer PIDs are rejected because their ownership is unattested.
         The exact PID is recorded on both task and run before a gated worker
         is released, so subsequent ticks can detect crashes before the TTL
         expires.

    Spawn failures are counted per-task. After ``failure_limit`` consecutive
    failures the task is auto-blocked with the last error as its reason —
    prevents the dispatcher from thrashing forever on an unfixable task.

    ``max_spawn`` is a **live concurrency cap**, not a per-tick spawn budget:
    it counts tasks already in ``status='running'`` plus this tick's spawns
    against the limit. So ``max_spawn=4`` means "at most 4 workers running
    at any time across the whole board" — matching the gateway's stated
    intent ("limit concurrent kanban tasks"). With a per-tick interpretation
    a 60-second tick interval could grow concurrency by N every minute on a
    busy board and accumulate without bound.

    ``spawn_fn`` defaults to ``_default_spawn``. Tests pass a stub.
    ``board`` pins workspace/log/db resolution for this tick to a specific
    board. When omitted, the current-board resolution chain is used.
    """
    # Reap zombie children from previously spawned workers. See
    # reap_worker_zombies() for the full rationale.
    reap_worker_zombies()
    # Exact-identity cleanup for runs ended by block/reclaim/crash paths. This
    # happens before any ready task can be claimed for its next epoch.
    cleanup_terminal_run_resources(conn)
    # Crash recovery for managed terminal worktrees. The lease and identity
    # checks make this sweep safe to run on every dispatcher tick.
    cleanup_terminal_task_worktrees(conn)
    # Snapshot once per tick. New behavior is opt-in and the exact policy is
    # sealed into each run's immutable continuation manifest.
    _continuation_cfg = _continuation_config()

    result = DispatchResult()
    result.reclaimed = release_stale_claims(conn)
    result.stale = detect_stale_running(
        conn, stale_timeout_seconds=stale_timeout_seconds,
    )
    result.crashed = detect_crashed_workers(conn)
    # detect_crashed_workers stashes protocol-violation auto-blocks on
    # itself so the public list-return stays stable. Pull them into the
    # DispatchResult here so telemetry / tests see the trip.
    _crash_auto_blocked = getattr(
        detect_crashed_workers, "_last_auto_blocked", []
    )
    if _crash_auto_blocked:
        result.auto_blocked.extend(_crash_auto_blocked)
    # Rate-limited requeues (quota wall, no failure counted) — surface for
    # telemetry / tests. These tasks went back to ``ready`` and the respawn
    # guard will defer them until the quota window clears.
    _crash_rate_limited = getattr(
        detect_crashed_workers, "_last_rate_limited", []
    )
    if _crash_rate_limited:
        result.rate_limited.extend(_crash_rate_limited)
    result.timed_out = enforce_max_runtime(conn)
    result.dependency_reconciled = reconcile_dependency_waits(
        conn,
        materialization_sla_seconds=(
            _resolve_dependency_materialization_sla_seconds(
                dependency_materialization_sla_seconds,
            )
        ),
    )
    result.dependency_links_restored = result.dependency_reconciled.links_restored
    result.dependency_waits_materialized = result.dependency_reconciled.waits_materialized
    result.dependency_waits_rearmed = result.dependency_reconciled.waits_rearmed
    result.dependency_legacy_recovered = result.dependency_reconciled.legacy_recovered
    result.dependency_waits_timed_out = result.dependency_reconciled.timed_out
    result.review_artifacts_backfilled = (
        result.dependency_reconciled.artifact_backfilled
    )
    result.review_artifact_selections_required = (
        result.dependency_reconciled.artifact_selection_required
    )
    result.promoted = recompute_ready(conn, failure_limit=failure_limit)

    # Count tasks already running so max_spawn enforces concurrency rather
    # than a per-tick spawn budget. See the docstring above for the full
    # rationale; the short version is that a 60-second tick interval with a
    # per-tick budget of N would grow concurrency by N every tick on a busy
    # board, since "running" tasks aren't reclaimed by completion alone —
    # they sit in status='running' until the worker calls
    # kanban_complete/kanban_block (or the dispatcher TTL-reclaims them).
    running_count = 0
    if max_spawn is not None:
        running_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE status = 'running'"
            ).fetchone()[0]
        )

    ready_rows = conn.execute(
        "SELECT id, assignee FROM tasks "
        "WHERE status = 'ready' AND claim_lock IS NULL "
        "ORDER BY priority DESC, created_at ASC"
    ).fetchall()
    # Honour kanban.max_in_progress: if the board already has enough running
    # tasks, skip spawning this tick so slow workers (local LLMs,
    # resource-constrained hosts) can finish what they have before more tasks
    # pile up and time out.
    if max_in_progress is not None and ready_rows:
        in_progress = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status = 'running'"
        ).fetchone()[0]
        if in_progress >= max_in_progress:
            # BUILD-263: record how many ready tasks were deferred by the cap
            # so "dispatcher stuck" diagnostics can tell "at concurrency cap"
            # (expected, self-clearing) apart from "genuinely broken".
            result.max_in_progress_deferred = len(ready_rows)
            return result
        # Only spawn enough to reach the cap, respecting max_spawn too.
        remaining = max_in_progress - in_progress
        if max_spawn is None or max_spawn > remaining:
            max_spawn = remaining
    spawned = 0
    # Per-profile concurrency cap (#21582): when set, track how many
    # workers each assignee already has in flight, and refuse to spawn
    # when this would push that assignee past the cap. Prevents
    # fan-out workloads from melting a single profile's local model /
    # API quota / browser pool while leaving other profiles idle.
    # Tasks blocked this way go to skipped_per_profile_capped (not
    # skipped_unassigned — the operator-actionable signal is different:
    # "this profile is busy, try again later" not "this needs routing").
    _per_profile_cap = max_in_progress_per_profile if (
        isinstance(max_in_progress_per_profile, int)
        and max_in_progress_per_profile > 0
    ) else None
    _per_profile_running: dict[str, int] = {}
    if _per_profile_cap is not None:
        for prow in conn.execute(
            "SELECT assignee, COUNT(*) AS n FROM tasks "
            "WHERE status = 'running' AND assignee IS NOT NULL "
            "GROUP BY assignee"
        ):
            _per_profile_running[prow["assignee"]] = int(prow["n"])
    # Normalize default_assignee once: empty/whitespace string → None so the
    # rest of the loop can use ``if default_assignee:`` as a single check.
    # We also resolve profile_exists once here for the same reason.
    _default_assignee = (default_assignee or "").strip() or None
    _default_assignee_resolved = False
    if _default_assignee:
        try:
            from hermes_cli.profiles import profile_exists as _pe
            _default_assignee_resolved = bool(_pe(_default_assignee))
        except Exception:
            # Profiles module not importable (test stubs, exotic envs).
            # Trust the operator's config and try the assignment; the
            # downstream profile_exists check on the assigned row will
            # bucket it as nonspawnable if the profile genuinely isn't
            # there, with the existing diagnostic.
            _default_assignee_resolved = True
    for row in ready_rows:
        if max_spawn is not None and running_count + spawned >= max_spawn:
            break
        row_assignee = row["assignee"]
        if not row_assignee:
            # Honour kanban.default_assignee: when the dispatcher hits an
            # unassigned ready task and an operator-configured fallback
            # exists, persist the assignment and proceed. This removes the
            # dashboard footgun where a task created without an assignee
            # parks in 'ready' forever even though the operator's intent
            # ("default") was perfectly clear (#27145). Mutating the row
            # (not just the in-memory view) keeps diagnostics and the
            # board state consistent: the task is now legitimately owned
            # by ``kanban.default_assignee``, not "unassigned but secretly
            # routed".
            if _default_assignee and _default_assignee_resolved:
                # Dry-run: show what WOULD happen (auto-assign + spawn) without
                # mutating the DB. Real run: mutate the row + emit the
                # 'assigned' event so the board state matches what just happened.
                if not dry_run:
                    try:
                        with write_txn(conn):
                            conn.execute(
                                "UPDATE tasks SET assignee = ? WHERE id = ? "
                                "AND (assignee IS NULL OR assignee = '')",
                                (_default_assignee, row["id"]),
                            )
                            _append_event(
                                conn, row["id"], "assigned",
                                {
                                    "assignee": _default_assignee,
                                    "source": "kanban.default_assignee",
                                },
                            )
                    except Exception:
                        _log.debug(
                            "kanban dispatch: failed to apply default_assignee=%r "
                            "to task %s",
                            _default_assignee, row["id"], exc_info=True,
                        )
                        result.skipped_unassigned.append(row["id"])
                        continue
                row_assignee = _default_assignee
                result.auto_assigned_default.append(row["id"])
            else:
                result.skipped_unassigned.append(row["id"])
                continue
        # Skip ready tasks whose assignee is not a real Hermes profile.
        # `_default_spawn` invokes ``hermes -p <assignee>`` which fails
        # with "Profile 'X' does not exist" when the assignee names a
        # control-plane lane (e.g. an interactive Claude Code terminal
        # like ``orion-cc`` / ``orion-research``) rather than a Hermes
        # profile. Those task lanes are pulled by terminals via
        # ``claim_task`` directly and should NEVER auto-spawn — the
        # subprocess would crash on startup, get reaped as a zombie,
        # the task would loop back to ``ready`` on next tick, and we'd
        # burn CPU forever (#kanban-dispatcher-crash-loop 2026-05-05).
        try:
            from hermes_cli.profiles import profile_exists  # local import: avoids cycle
        except Exception:
            profile_exists = None  # type: ignore[assignment]
        if profile_exists is not None and not profile_exists(row_assignee):
            # Bucket separately from skipped_unassigned: the operator
            # cannot fix this by assigning a profile (the assignee IS the
            # intended owner — a terminal lane). Health telemetry uses
            # this distinction to suppress spurious "stuck" warnings on
            # multi-lane setups where the ready queue is steadily full
            # of human-pulled work.
            result.skipped_nonspawnable.append(row["id"])
            continue
        validation_task = get_task(conn, row["id"])
        skill_validation_error = _forced_skill_validation_error(
            validation_task.assignee if validation_task else row["assignee"],
            validation_task.skills if validation_task else None,
        )
        if skill_validation_error:
            if dry_run:
                result.respawn_guarded.append((row["id"], "forced_skill_validation"))
            elif _block_forced_skill_validation_failure(
                conn, row["id"], skill_validation_error,
            ):
                result.auto_blocked.append(row["id"])
            continue
        # Per-profile concurrency cap (#21582): even if there's global
        # headroom, refuse to spawn for an assignee that's already at
        # its in-flight cap. Prevents one profile's local model / API
        # quota / browser pool from being overwhelmed by a fan-out
        # while the global max_in_progress / max_spawn caps still allow
        # work on OTHER profiles.
        if _per_profile_cap is not None:
            current = _per_profile_running.get(row_assignee, 0)
            if current >= _per_profile_cap:
                result.skipped_per_profile_capped.append(
                    (row["id"], row_assignee, current)
                )
                continue
        # BUILD-261 release/remediation circuit breaker: checked BEFORE
        # check_respawn_guard (and unlike it, this trip is permanent, not
        # a one-tick defer). If the task's (or its remediation children's)
        # last N recorded failures reduce to the identical signature, the
        # automated retries are not converging — halt the saga by
        # blocking for human review instead of respawning again.
        breaker_trip = check_failure_signature_breaker(
            conn, row["id"], threshold=signature_repeat_threshold,
        )
        if breaker_trip is not None:
            if dry_run:
                result.circuit_breaker_tripped.append(
                    (row["id"], breaker_trip["signature"])
                )
            elif _trip_failure_signature_breaker(conn, row["id"], breaker_trip):
                result.circuit_breaker_tripped.append(
                    (row["id"], breaker_trip["signature"])
                )
            continue
        # Respawn guard: refuse to re-spawn when useful work is already
        # in-flight/recent, or when the last failure is a deterministic
        # blocker (quota / auth). The guard defers the spawn this tick so
        # the task gets a chance to clear (rate limits often reset in
        # seconds-to-minutes); the existing consecutive_failures counter
        # still trips the auto-block circuit breaker after failure_limit
        # consecutive failures, so a persistent auth error eventually
        # blocks via the normal path rather than on first occurrence.
        guard_reason = check_respawn_guard(conn, row["id"])
        if guard_reason is not None:
            result.respawn_guarded.append((row["id"], guard_reason))
            # Emit an event so operators can see why the task was
            # skipped when reading `hermes kanban tail` — without
            # this the task appears stuck in ready with no diagnosis.
            if not dry_run:
                with write_txn(conn):
                    _append_event(
                        conn, row["id"], "respawn_guarded",
                        {"reason": guard_reason},
                    )
            continue
        if dry_run:
            result.spawned.append((row["id"], row_assignee, ""))
            # Increment per-profile counter even in dry_run so the cap
            # check sees the would-be spawn on subsequent iterations.
            # Without this, dry_run reports every task as spawnable and
            # under-reports the capped subset (#21582).
            if _per_profile_cap is not None and row_assignee:
                _per_profile_running[row_assignee] = (
                    _per_profile_running.get(row_assignee, 0) + 1
                )
            continue
        # ── Workspace collision pre-check ────────────────────────
        # Before claiming, check whether another running task already
        # holds the same non-scratch workspace.  The in-transaction
        # guard inside claim_task() is the atomic backstop; this
        # pre-check lets the dispatch output report collisions
        # explicitly so operators can see that a task is deferred
        # rather than stuck.
        ws_info = conn.execute(
            "SELECT workspace_kind, workspace_path FROM tasks WHERE id = ?",
            (row["id"],),
        ).fetchone()
        if ws_info and ws_info["workspace_path"] and ws_info["workspace_kind"] \
           and ws_info["workspace_kind"] != "scratch":
            coll = conn.execute(
                "SELECT id FROM tasks "
                "WHERE status = 'running' "
                "  AND workspace_kind = ? "
                "  AND workspace_path = ? "
                "  AND id != ? "
                "LIMIT 1",
                (ws_info["workspace_kind"], ws_info["workspace_path"], row["id"]),
            ).fetchone()
            if coll:
                result.workspace_collisions.append((row["id"], coll["id"]))
                continue
        # ── Dirty-workspace pre-flight check ──────────────────────
        # Approach A: refuse to dispatch when the workspace is a git
        # repo with uncommitted/untracked changes.  This prevents the
        # crash-class from occurring (architect workers in dirty
        # shared workspaces).  Only reads git status — never writes.
        # Scratch workspaces (no git repo) pass through cleanly.
        # Worktree tasks use the board's default_workdir (the shared
        # repo root) as their workspace_path — flagging those as dirty
        # would block all worktree dispatches.  Skip worktrees.
        if ws_info and ws_info[0] != "worktree":
            _ws_path = (ws_info[1] if ws_info else None) or None
            if _ws_path and _is_workspace_dirty(_ws_path):
                ws_diag = _capture_workspace_diag(_ws_path)
                if not dry_run:
                    with write_txn(conn):
                        _append_event(
                            conn, row["id"], "dirty_workspace",
                            {"workspace_diag": ws_diag or {}},
                        )
                result.dirty_workspace.append(row["id"])
                continue
        claimed = claim_task(conn, row["id"], ttl_seconds=ttl_seconds)
        if claimed is None:
            # BUILD-263: lost the atomic claim to a concurrent claimant
            # (another dispatcher, or a terminal pulling the task directly).
            # Not an error — the task remains claimable and is retried next
            # tick — but stuck-dispatcher diagnostics need to see this
            # instead of it looking identical to "nothing spawnable".
            result.claim_race.append(row["id"])
            continue
        try:
            resolved_branch_name = None
            checkpoint_ref = None
            checkpoint_sha = None
            if claimed.workspace_kind == "worktree":
                (
                    workspace,
                    resolved_branch_name,
                    checkpoint_ref,
                    checkpoint_sha,
                ) = _resolve_worktree_workspace(claimed, board=board, conn=conn)
            else:
                workspace = resolve_workspace(claimed, board=board)
        except WorkspaceContractError as exc:
            # BUILD-496: deterministic contract violation (legacy worktree row
            # with no materializable anchor). Block once with a typed reason
            # instead of retrying a failure that can only reproduce itself.
            _block_workspace_contract_violation(conn, result, claimed.id, exc)
            continue
        except Exception as exc:
            # BUILD-263: this used to be recorded on the task row (via
            # _record_spawn_failure) with no corresponding log line — a
            # broken venv/profile/PATH silently produced "0 spawned" ticks
            # forever with no diagnosis. Log it (not swallowed) and surface
            # it on the DispatchResult for cause-breakdown telemetry.
            _log.warning(
                "kanban dispatch: workspace resolution failed for %s: %s",
                claimed.id, exc, exc_info=True,
            )
            auto = _record_spawn_failure(
                conn, claimed.id, f"workspace: {exc}",
                failure_limit=failure_limit,
                expected_run_id=claimed.current_run_id,
                expected_claim_lock=claimed.claim_lock,
            )
            if auto is None:
                result.claim_race.append(claimed.id)
            else:
                result.spawn_errors.append((claimed.id, f"workspace: {exc}"))
            if auto is True:
                result.auto_blocked.append(claimed.id)
            continue
        # Persist the resolved workspace path so the worker can cd there.
        set_workspace_path(conn, claimed.id, str(workspace))
        if claimed.workspace_kind == "worktree":
            set_branch_name(conn, claimed.id, resolved_branch_name or (claimed.branch_name or "").strip() or f"wt/{claimed.id}")
            if checkpoint_ref and checkpoint_sha:
                with write_txn(conn):
                    if conn.execute(
                        "SELECT 1 FROM task_events WHERE task_id = ? "
                        "AND kind = 'workspace_checkpointed' LIMIT 1",
                        (claimed.id,),
                    ).fetchone() is None:
                        _append_event(
                            conn,
                            claimed.id,
                            "workspace_checkpointed",
                            {"ref": checkpoint_ref, "sha": checkpoint_sha},
                            run_id=claimed.current_run_id,
                        )
        _maybe_emit_scratch_tip(conn, claimed.id, claimed.workspace_kind)
        continuation_error = _prepare_continuation_or_block(
            conn, claimed, config=_continuation_cfg,
        )
        if continuation_error is not None:
            result.spawn_errors.append((claimed.id, continuation_error))
            result.auto_blocked.append(claimed.id)
            continue
        _spawn = spawn_fn if spawn_fn is not None else _default_spawn
        try:
            _spawn_and_attach_worker(
                conn,
                claimed,
                str(workspace),
                _spawn,
                board=board,
            )
            # NOTE: we intentionally do NOT reset consecutive_failures
            # here. A successful spawn proves the worker can start but
            # doesn't prove the run will succeed. Under unified
            # failure counting, resetting on spawn would let a task
            # that keeps timing out after spawn loop forever. The
            # counter is cleared only on successful completion (see
            # complete_task).
            result.spawned.append((claimed.id, claimed.assignee or "", str(workspace)))
            spawned += 1
            # Track the new in-flight count for this profile so later
            # iterations in this same tick respect the per-profile cap
            # (#21582). Subsequent ticks re-query from the DB.
            if _per_profile_cap is not None and claimed.assignee:
                _per_profile_running[claimed.assignee] = (
                    _per_profile_running.get(claimed.assignee, 0) + 1
                )
        except Exception as exc:
            # BUILD-263: log the spawn exception — see the matching comment
            # on the workspace-resolution catch above for the rationale.
            _log.warning(
                "kanban dispatch: spawn_fn raised for %s (assignee=%s): %s",
                claimed.id, claimed.assignee, exc, exc_info=True,
            )
            auto = _record_spawn_failure(
                conn, claimed.id, str(exc),
                failure_limit=failure_limit,
                expected_run_id=claimed.current_run_id,
                expected_claim_lock=claimed.claim_lock,
            )
            if auto is None:
                result.claim_race.append(claimed.id)
            else:
                result.spawn_errors.append((claimed.id, str(exc)))
            if auto is True:
                result.auto_blocked.append(claimed.id)

    # ---- review column dispatch ----
    # Review tasks are tasks that a worker moved to 'review' after
    # creating a PR.  The dispatcher spawns a review agent (loading
    # sdlc-review skill) that verifies the PR and either merges (→ done)
    # or rejects (→ back to running for the worker to fix).
    #
    # Same concurrency model as ready dispatch: review spawns count
    # against max_spawn alongside ready tasks, so the total number of
    # running workers stays bounded.
    review_rows = conn.execute(
        "SELECT id, assignee FROM tasks "
        "WHERE status = 'review' AND claim_lock IS NULL "
        "ORDER BY priority DESC, created_at ASC"
    ).fetchall()
    for row in review_rows:
        if max_spawn is not None and running_count + spawned >= max_spawn:
            break
        if not row["assignee"]:
            result.skipped_unassigned.append(row["id"])
            continue
        try:
            from hermes_cli.profiles import profile_exists
        except Exception:
            profile_exists = None  # type: ignore[assignment]
        if profile_exists is not None and not profile_exists(row["assignee"]):
            result.skipped_nonspawnable.append(row["id"])
            continue
        skill_validation_error = _forced_skill_validation_error(
            row["assignee"],
            ["sdlc-review"],
        )
        if skill_validation_error:
            if dry_run:
                result.respawn_guarded.append((row["id"], "forced_skill_validation"))
            elif _block_forced_skill_validation_failure(
                conn, row["id"], skill_validation_error,
            ):
                result.auto_blocked.append(row["id"])
            continue
        if dry_run:
            result.spawned.append((row["id"], row["assignee"], ""))
            continue
        claimed = claim_review_task(conn, row["id"], ttl_seconds=ttl_seconds)
        if claimed is None:
            # BUILD-263: see the matching comment in the ready-task loop
            # above — lost claim race, not an error.
            result.claim_race.append(row["id"])
            continue
        try:
            resolved_branch_name = None
            checkpoint_ref = None
            checkpoint_sha = None
            if claimed.workspace_kind == "worktree":
                (
                    workspace,
                    resolved_branch_name,
                    checkpoint_ref,
                    checkpoint_sha,
                ) = _resolve_worktree_workspace(claimed, board=board, conn=conn)
            else:
                workspace = resolve_workspace(claimed, board=board)
        except WorkspaceContractError as exc:
            # BUILD-496: deterministic contract violation — block once with a
            # typed reason, no retry burn. See the ready-task loop above.
            _block_workspace_contract_violation(conn, result, claimed.id, exc)
            continue
        except Exception as exc:
            # BUILD-263: see the matching comment in the ready-task loop
            # above — log (not swallowed) + surface for cause telemetry.
            _log.warning(
                "kanban dispatch: review workspace resolution failed for %s: %s",
                claimed.id, exc, exc_info=True,
            )
            auto = _record_spawn_failure(
                conn, claimed.id, f"workspace: {exc}",
                failure_limit=failure_limit,
                expected_run_id=claimed.current_run_id,
                expected_claim_lock=claimed.claim_lock,
            )
            if auto is None:
                result.claim_race.append(claimed.id)
            else:
                result.spawn_errors.append((claimed.id, f"workspace: {exc}"))
            if auto is True:
                result.auto_blocked.append(claimed.id)
            continue
        # Persist the resolved workspace path so the worker can cd there.
        set_workspace_path(conn, claimed.id, str(workspace))
        if claimed.workspace_kind == "worktree":
            set_branch_name(conn, claimed.id, resolved_branch_name or (claimed.branch_name or "").strip() or f"wt/{claimed.id}")
            if checkpoint_ref and checkpoint_sha:
                with write_txn(conn):
                    if conn.execute(
                        "SELECT 1 FROM task_events WHERE task_id = ? "
                        "AND kind = 'workspace_checkpointed' LIMIT 1",
                        (claimed.id,),
                    ).fetchone() is None:
                        _append_event(
                            conn,
                            claimed.id,
                            "workspace_checkpointed",
                            {"ref": checkpoint_ref, "sha": checkpoint_sha},
                            run_id=claimed.current_run_id,
                        )
        _maybe_emit_scratch_tip(conn, claimed.id, claimed.workspace_kind)
        continuation_error = _prepare_continuation_or_block(
            conn, claimed, config=_continuation_cfg,
        )
        if continuation_error is not None:
            result.spawn_errors.append((claimed.id, continuation_error))
            result.auto_blocked.append(claimed.id)
            continue
        # Force-load the sdlc-review skill for review agents — it carries
        # the review logic (AC verification, merge, etc.). The mandatory
        # kanban lifecycle is already injected into every worker's system
        # prompt via KANBAN_GUIDANCE, so this is the only extra skill the
        # review agent needs.
        claimed.skills = ["sdlc-review"]
        _spawn = spawn_fn if spawn_fn is not None else _default_spawn
        try:
            _spawn_and_attach_worker(
                conn,
                claimed,
                str(workspace),
                _spawn,
                board=board,
            )
            result.spawned.append((claimed.id, claimed.assignee or "", str(workspace)))
            spawned += 1
        except Exception as exc:
            # BUILD-263: see the matching comment in the ready-task loop above.
            _log.warning(
                "kanban dispatch: review spawn_fn raised for %s (assignee=%s): %s",
                claimed.id, claimed.assignee, exc, exc_info=True,
            )
            auto = _record_spawn_failure(
                conn, claimed.id, str(exc),
                failure_limit=failure_limit,
                expected_run_id=claimed.current_run_id,
                expected_claim_lock=claimed.claim_lock,
            )
            if auto is None:
                result.claim_race.append(claimed.id)
            else:
                result.spawn_errors.append((claimed.id, str(exc)))
            if auto is True:
                result.auto_blocked.append(claimed.id)
    return result


def _positive_int(value: Any, default: int, *, minimum: int = 1) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= minimum else default


def worker_log_rotation_config(kanban_cfg: Optional[dict] = None) -> tuple[int, int]:
    """Return ``(rotate_bytes, backup_count)`` for worker log rotation.

    Defaults preserve the historical behavior: rotate at 2 MiB and keep one
    backup generation (``.log.1``). Operators with long-running workers can
    raise either value from ``config.yaml`` without changing dispatcher code.
    """
    if kanban_cfg is None:
        try:
            from hermes_cli.config import load_config

            kanban_cfg = (load_config().get("kanban") or {})
        except Exception:
            kanban_cfg = {}
    max_bytes = _positive_int(
        (kanban_cfg or {}).get("worker_log_rotate_bytes"),
        DEFAULT_LOG_ROTATE_BYTES,
        minimum=1,
    )
    backup_count = _positive_int(
        (kanban_cfg or {}).get("worker_log_backup_count"),
        DEFAULT_LOG_BACKUP_COUNT,
        minimum=0,
    )
    return max_bytes, backup_count


def _rotated_log_path(log_path: Path, generation: int) -> Path:
    return log_path.with_suffix(log_path.suffix + f".{generation}")


def _rotate_worker_log(
    log_path: Path,
    max_bytes: int,
    backup_count: int = DEFAULT_LOG_BACKUP_COUNT,
) -> None:
    """Rotate ``<log>`` when it exceeds ``max_bytes``.

    ``backup_count=1`` preserves the legacy single-generation behavior:
    ``<log>`` moves to ``<log>.1`` and any previous ``.1`` is replaced.
    Higher values shift older generations up to ``backup_count``.
    """
    try:
        if not log_path.exists():
            return
        if log_path.stat().st_size <= max_bytes:
            return
        backup_count = _positive_int(
            backup_count,
            DEFAULT_LOG_BACKUP_COUNT,
            minimum=0,
        )
        if backup_count == 0:
            log_path.unlink()
            return
        oldest = _rotated_log_path(log_path, backup_count)
        try:
            if oldest.exists():
                oldest.unlink()
        except OSError:
            pass
        for generation in range(backup_count - 1, 0, -1):
            src = _rotated_log_path(log_path, generation)
            if not src.exists():
                continue
            try:
                src.rename(_rotated_log_path(log_path, generation + 1))
            except OSError:
                pass
        log_path.rename(_rotated_log_path(log_path, 1))
    except OSError:
        pass


def _module_hermes_argv() -> list[str]:
    """Return the interpreter-bound Hermes CLI invocation."""
    # ``hermes_cli.main`` is the console-script target declared in
    # pyproject.toml, NOT a top-level ``hermes`` package — there is no
    # ``hermes`` package to import.
    return [sys.executable, "-m", "hermes_cli.main"]


def _absolute_hermes_path(path: str) -> str:
    """Return an absolute filesystem path for a resolved Hermes shim."""
    expanded = os.path.expanduser(path)
    return expanded if os.path.isabs(expanded) else os.path.abspath(expanded)


def _looks_like_path(value: str) -> bool:
    """Return true when a command override is an explicit path, not a name."""
    expanded = os.path.expanduser(value)
    return (
        expanded.startswith("~")
        or os.path.isabs(expanded)
        or bool(os.path.dirname(expanded))
        or "\\" in expanded
        or bool(re.match(r"^[A-Za-z]:", expanded))
    )


def _is_windows_batch_shim(path: str) -> bool:
    """Return true for Windows shell/batch shims that should not be argv[0]."""
    return path.lower().endswith((".cmd", ".bat"))


def _path_search_names(command: str) -> list[str]:
    """Return executable names to try for an unqualified command."""
    if not _IS_WINDOWS or os.path.splitext(command)[1]:
        return [command]
    raw = os.environ.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD"
    exts = [ext for ext in raw.split(";") if ext]
    return [command + ext for ext in exts]


def _safe_which_no_cwd(command: str) -> Optional[str]:
    """Resolve a bare command from PATH without implicit current-dir search.

    ``shutil.which`` follows platform search behavior. On Windows that can
    include the current directory before PATH for bare names, which is not a
    safe dispatcher primitive. This resolver only considers explicit PATH
    entries and skips empty / ``.`` entries.
    """
    path_env = os.environ.get("PATH", "")
    for raw_dir in path_env.split(os.pathsep):
        if not raw_dir or raw_dir == ".":
            continue
        directory = os.path.expanduser(raw_dir)
        for name in _path_search_names(command):
            candidate = os.path.join(directory, name)
            if not os.path.isfile(candidate):
                continue
            if _IS_WINDOWS or os.access(candidate, os.X_OK):
                return candidate
    return None


def _hermes_path_argv(path: str) -> list[str]:
    """Return argv for a resolved Hermes executable path.

    Windows batch shims (`.cmd` / `.bat`) are not safe as argv[0] for
    worker launches because the argument vector includes task-derived
    values. Prefer the interpreter-bound module form whenever the resolved
    executable is only a shell shim.
    """
    if _IS_WINDOWS and _is_windows_batch_shim(path):
        return _module_hermes_argv()
    return [_absolute_hermes_path(path)]


def _resolve_hermes_argv() -> list[str]:
    """Resolve the ``hermes`` invocation as argv parts for ``Popen``.

    Tries in order:

    1. ``$HERMES_BIN`` — explicit operator override. Path-like values are
       normalized to absolute paths; bare command names keep normal PATH
       semantics and never prefer a same-directory file before ``PATH``.
    2. ``shutil.which("hermes")`` — the console-script shim, normalized to
       an absolute path. On Windows, ``which`` can return a relative
       ``.\\hermes.CMD`` when the current directory is on ``PATH``; directly
       launching batch shims is also unsafe with task-derived argv. The
       dispatcher therefore falls back to the interpreter-bound module form
       for implicit ``.cmd`` / ``.bat`` shims.
    3. ``sys.executable -m hermes_cli.main`` — fallback for setups where
       Hermes is launched from a venv and the ``hermes`` shim is not on
       the dispatcher's ``$PATH`` (cron, systemd ``User=`` services,
       launchd jobs, detached processes, etc.). Goes through the running
       interpreter so the result is independent of ``$PATH``.

    Mirrors ``gateway.run._resolve_hermes_bin`` for the same reason. Kept
    local (not imported from gateway) because ``hermes_cli`` sits below
    ``gateway`` in the dependency order.
    """
    import shutil

    env_bin = os.environ.get("HERMES_BIN", "").strip()
    if env_bin:
        if _looks_like_path(env_bin):
            return _hermes_path_argv(env_bin)
        resolved_env_bin = _safe_which_no_cwd(env_bin)
        if resolved_env_bin:
            return _hermes_path_argv(resolved_env_bin)
        return _module_hermes_argv()

    hermes_bin = _safe_which_no_cwd("hermes") if _IS_WINDOWS else shutil.which("hermes")
    if hermes_bin:
        return _hermes_path_argv(hermes_bin)
    return _module_hermes_argv()


def _kanban_worker_skill_available(hermes_home: Optional[str]) -> bool:
    """True if ``kanban-worker`` can be safely preloaded for a worker home.

    The dispatcher injects ``--skills kanban-worker`` into every worker when
    safe. When the worker activates a profile (``hermes -p <name>``), its
    skills root becomes ``<profile_home>/skills`` and its profile config may
    explicitly disable skills. Preloading a missing, disabled, ambiguous, or
    platform-unsupported skill is fatal at CLI startup, aborting the worker
    before the agent loop runs. Gate the flag on profile-specific preload
    safety; the Kanban lifecycle contract is still injected via
    ``KANBAN_GUIDANCE``, so omitting the flag only drops the supplementary
    pattern library.
    """
    profile_home = Path(hermes_home) if hermes_home else (Path.home() / ".hermes")
    skills_cfg = _load_profile_skills_config(profile_home)
    candidates = _candidate_skill_files(profile_home, skills_cfg, "kanban-worker")
    if len(candidates) != 1:
        return False
    try:
        from agent.skill_utils import parse_frontmatter, skill_matches_platform

        content = candidates[0].read_text(encoding="utf-8")
        frontmatter, _body = parse_frontmatter(content)
    except Exception:
        return False
    if not skill_matches_platform(frontmatter):
        return False
    resolved_name = str(frontmatter.get("name") or candidates[0].parent.name).strip()
    return resolved_name not in _profile_disabled_skill_names(skills_cfg)


def _worker_terminal_timeout_env(
    max_runtime_seconds: Optional[int],
    current_timeout: Optional[str],
) -> Optional[str]:
    """Return a worker-scoped TERMINAL_TIMEOUT override, if needed.

    Kanban's ``max_runtime_seconds`` bounds the whole worker attempt. The
    terminal tool has its own default timeout via ``TERMINAL_TIMEOUT``; when
    the worker runtime is longer, raise only the child process default so a
    long command is not killed by the generic terminal default first.
    """
    if max_runtime_seconds is None:
        return None
    try:
        runtime = int(max_runtime_seconds)
    except (TypeError, ValueError):
        return None
    if runtime <= 0:
        return None

    desired = max(1, runtime - KANBAN_TERMINAL_TIMEOUT_GRACE_SECONDS)
    try:
        existing = int(str(current_timeout).strip()) if current_timeout else 0
    except (TypeError, ValueError):
        existing = 0
    if existing >= desired:
        return None
    return str(desired)


def _resolve_worker_cli_toolsets(hermes_home: Optional[str]) -> Optional[list[str]]:
    """Return the assigned profile's effective CLI toolsets for a worker.

    Dispatcher-spawned workers are launched from a long-lived gateway process,
    then the child re-enters the CLI with ``-p <assignee>``. Resolve the
    assignee profile's CLI tool surface at dispatch time and pass it as an
    explicit ``--toolsets`` pin so worker startup cannot fall back to a stale
    root/active-profile config or a profile whose top-level ``toolsets`` entry
    is only the kanban orchestrator surface. ``model_tools`` still appends the
    task-scoped kanban lifecycle tools when ``HERMES_KANBAN_TASK`` is set.
    """
    if not hermes_home:
        return None
    try:
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override
        from hermes_cli.config import load_config
        from hermes_cli.tools_config import _get_platform_tools

        token = set_hermes_home_override(hermes_home)
        try:
            cfg = load_config()
            toolsets = sorted(_get_platform_tools(cfg, "cli"))
        finally:
            reset_hermes_home_override(token)
        return toolsets or None
    except Exception as exc:
        _log.debug(
            "kanban worker: could not resolve CLI toolsets for HERMES_HOME=%r (%s)",
            hermes_home,
            exc,
        )
        return None


def _assert_worker_continuation_provider_policy(
    hermes_home: Optional[str],
    requested_route: dict[str, Optional[str]],
    provider_policy: dict[str, Any],
) -> None:
    """Reject known primary/fallback providers before creating the worker.

    Runtime preflight remains authoritative for `auto` resolution and route
    mutation, but every provider already named by the RunSpec/profile config is
    knowable here and must pass before Popen.
    """
    from hermes_cli.kanban_continuation import assert_provider_allowed

    config: dict[str, Any] = {}
    if hermes_home:
        from hermes_constants import (
            reset_hermes_home_override,
            set_hermes_home_override,
        )
        from hermes_cli.config import load_config

        token = set_hermes_home_override(hermes_home)
        try:
            loaded = load_config()
            if isinstance(loaded, dict):
                config = loaded
        finally:
            reset_hermes_home_override(token)

    configured_model = config.get("model") or {}
    if not isinstance(configured_model, dict):
        configured_model = {}
    primary = requested_route.get("provider") or configured_model.get("provider")
    assert_provider_allowed(primary, provider_policy, phase="pre_spawn_primary")

    from hermes_cli.fallback_config import get_fallback_chain

    for index, fallback in enumerate(get_fallback_chain(config)):
        assert_provider_allowed(
            fallback.get("provider"),
            provider_policy,
            phase=f"pre_spawn_fallback[{index}]",
        )


def _spawn_contract(
    task: Task,
    *,
    board: Optional[str],
) -> tuple[
    str,
    dict[str, Optional[str]],
    dict[str, Any],
    Optional[list[str]],
    Optional[str],
    Optional[dict[str, Any]],
]:
    """Load the active run's immutable launch contract.

    A task without a run spec is a legacy/manual spawn and keeps the old task
    fields. Once a run carries a contract, mutable task routing is ignored.
    Missing or malformed contracted runs fail closed before Popen. Run
    currency is checked separately by the gated PID-attachment CAS.
    """
    legacy_route = {
        "provider": task.model_provider_override,
        "model": task.model_override,
        "reasoning_effort": task.model_reasoning_effort,
    }
    if task.current_run_id is None:
        return (
            task.assignee or "",
            legacy_route,
            _delivery_policy_snapshot(None),
            task.toolsets,
            None,
            None,
        )

    with connect(board=board) as conn:
        row = conn.execute(
            "SELECT r.run_spec_json FROM task_runs r "
            "WHERE r.id = ? AND r.task_id = ?",
            (int(task.current_run_id), task.id),
        ).fetchone()
        continuation = get_continuation_manifest(
            conn,
            int(task.current_run_id),
            task_id=task.id,
            require_current=False,
        )
    if row is None:
        # A missing row only proves the (run_id, task_id) pair was absent
        # from the queried board database -- it does NOT establish that the
        # run is stale/superseded. Naming it "no longer current" masked the
        # real failure class (e.g. a spawn_fn body exception mis-caught as
        # an arity mismatch and retried against the wrong board's DB -- see
        # _spawn_and_attach_worker). Report the queried board + resolved
        # path so a wrong-database lookup is diagnosable at a glance.
        resolved_board = _normalize_board_slug(board) or get_current_board()
        resolved_path = kanban_db_path(board=board)
        raise RuntimeError(
            "spawn contract lookup failed: task/run pair "
            f"({task.id}, {task.current_run_id}) was not found in board "
            f"database board={resolved_board!r} path={str(resolved_path)!r}"
        )
    raw = row["run_spec_json"]
    if not raw:
        raise RuntimeError(
            f"task {task.id} run {task.current_run_id} has no run spec"
        )
    try:
        spec = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"task {task.id} run {task.current_run_id} has invalid run spec"
        ) from exc
    if not isinstance(spec, dict):
        raise RuntimeError(
            f"task {task.id} run {task.current_run_id} has unsupported run spec"
        )
    route = spec.get("requested_route")
    version = spec.get("version")
    if version not in {1, 2} or not isinstance(route, dict):
        raise RuntimeError(
            f"task {task.id} run {task.current_run_id} has unsupported run spec"
        )
    run_toolsets: Optional[list[str]] = None
    if version == 2:
        raw_toolsets = spec.get("toolsets")
        if not isinstance(raw_toolsets, list) or not raw_toolsets or any(
            not isinstance(value, str) or not value.strip() or "," in value
            for value in raw_toolsets
        ):
            raise RuntimeError(
                f"task {task.id} run {task.current_run_id} has invalid toolsets"
            )
        run_toolsets = sorted({value.strip().casefold() for value in raw_toolsets})
    try:
        delivery_policy = validate_delivery_policy_snapshot(
            spec.get("delivery_policy")
        )
    except ValueError as exc:
        raise RuntimeError(
            f"task {task.id} run {task.current_run_id} has invalid delivery policy"
        ) from exc
    return (
        str(spec.get("profile") or ""),
        {
            "provider": route.get("provider"),
            "model": route.get("model"),
            "reasoning_effort": route.get("reasoning_effort"),
        },
        delivery_policy,
        run_toolsets,
        continuation.manifest_digest if continuation is not None else None,
        continuation.manifest["provider_policy"] if continuation is not None else None,
    )


def _default_spawn(
    task: Task,
    workspace: str,
    *,
    board: Optional[str] = None,
) -> SpawnReceipt:
    """Fire-and-forget ``hermes -p <profile> chat -q ...`` subprocess.

    Returns a gated receipt. The child cannot begin command dispatch until
    the dispatcher durably attaches its PID to the exact run and releases the
    gate. The child's completion is still observed
    via the ``complete`` / ``block`` transitions the worker writes itself;
    the PID check is a safety net for crashes, OOM kills, and Ctrl+C.

    ``board`` pins the child's kanban context to that board: the child's
    ``HERMES_KANBAN_DB`` / ``HERMES_KANBAN_BOARD`` / workspaces_root env
    vars all resolve to the same board the dispatcher claimed the task
    from. Workers cannot accidentally see other boards.
    """
    import subprocess

    from hermes_cli.profiles import normalize_profile_name

    (
        requested_profile,
        requested_route,
        delivery_policy,
        run_toolsets,
        continuation_digest,
        continuation_provider_policy,
    ) = _spawn_contract(
        task, board=board,
    )
    profile_arg = normalize_profile_name(requested_profile)
    if not profile_arg:
        raise RuntimeError(f"task {task.id} run contract has no profile")

    # Resolve the machine-global worker credential contract before creating
    # any worker artifacts or releasing the start gate.  Custom spawn_fn
    # paths remain deliberately outside this default-spawn authority path.
    from hermes_cli.worker_credentials import (
        build_worker_environment,
        prepare_worker_credentials,
    )

    worker_credential_plan = prepare_worker_credentials(
        profile_arg,
        base_env=os.environ,
        run_id=task.current_run_id,
    )

    prompt = f"work kanban task {task.id}"
    env = build_worker_environment(os.environ, worker_credential_plan)
    # Workers are always headless.  A gateway launched from a TUI session or
    # a profile defaulting to TUI must not turn a quiet one-shot worker into an
    # interactive process that exits without running the task.
    env.pop("HERMES_TUI", None)

    # Inject HERMES_HOME so the worker reads the profile-scoped config.yaml
    # (fallback_providers, toolsets, agent settings, etc.) instead of the root
    # config.  Without this, `env = dict(os.environ)` copies only the parent's
    # env, and when the child process starts `hermes -p <name>` the
    # _apply_profile_override() runs *before* hermes_constants is imported.
    # If HERMES_HOME is absent from the child's env, get_hermes_home() falls
    # back to Path.home() / ".hermes" (the DEFAULT profile root), ignoring the
    # profile-specific config entirely.  Fixes profile-scoped fallback_providers
    # being invisible to kanban workers.
    from hermes_cli.profiles import resolve_profile_env
    try:
        env["HERMES_HOME"] = resolve_profile_env(profile_arg)
    except FileNotFoundError:
        # Profile dir doesn't exist — defer resolution to the CLI's
        # _apply_profile_override() via HERMES_PROFILE (set below).
        # This only happens in test fixtures where the isolated
        # HERMES_HOME never had profiles created.
        pass
    if continuation_provider_policy is not None:
        _assert_worker_continuation_provider_policy(
            env.get("HERMES_HOME"),
            requested_route,
            continuation_provider_policy,
        )
    # Do not leak the dispatcher's HOME into a worker for another profile.
    # The worker process itself should use the assignee profile's isolated
    # home; host-auth CLIs get the OS-account home only through explicit,
    # narrow shims downstream.
    try:
        from hermes_constants import get_host_user_home, get_subprocess_home_for_hermes_home

        profile_home = get_subprocess_home_for_hermes_home(env.get("HERMES_HOME"))
        if profile_home:
            env["HOME"] = profile_home
        host_home = get_host_user_home()
        if host_home:
            env["HERMES_HOST_HOME"] = host_home
    except Exception:
        pass
    if task.tenant:
        env["HERMES_TENANT"] = task.tenant
    env["HERMES_KANBAN_TASK"] = task.id
    # Propagate the dispatcher's durable task priority to the point-of-use
    # local-model capacity arbiter. Provider fallback is model-blind at claim
    # time, so admission happens immediately before a concrete local call.
    env["HERMES_KANBAN_PRIORITY"] = str(int(task.priority))
    env["HERMES_KANBAN_WORKSPACE"] = workspace
    # Pin TERMINAL_CWD to the task's workspace so the worker's file tools and
    # context-file loader anchor on the workspace, not whatever cwd the
    # dispatching gateway happened to export. The worker subprocess is already
    # launched with cwd=workspace, but TERMINAL_CWD takes precedence over the
    # process cwd in both file_tools._resolve_base_dir (#41312 — relative
    # write_file paths were landing in the gateway user's home) and
    # build_context_files_prompt (#34619 — workers loaded the dispatching
    # gateway's AGENTS.md instead of the task's). Setting it to the workspace
    # fixes both: the workspace is where the task's work actually happens.
    # Only pin a real, absolute directory — file_tools rejects relative /
    # sentinel TERMINAL_CWD values, so a non-dir workspace must NOT be set
    # here (leave the inherited value rather than write a meaningless one).
    if workspace and os.path.isabs(workspace) and os.path.isdir(workspace):
        env["TERMINAL_CWD"] = workspace
    if task.branch_name:
        env["HERMES_KANBAN_BRANCH"] = task.branch_name
    if task.current_run_id is not None:
        env["HERMES_KANBAN_RUN_ID"] = str(task.current_run_id)
    if continuation_digest is not None:
        env["HERMES_KANBAN_CONTINUATION_DIGEST"] = continuation_digest
    else:
        env.pop("HERMES_KANBAN_CONTINUATION_DIGEST", None)
    # The dispatcher already read and validated the immutable RunSpec before
    # spawning. Pass only its delivery attestation to the child so output
    # boundaries do not need SQLite merely to discover that this run is
    # authoritatively ungated.
    env["HERMES_KANBAN_DELIVERY_POLICY"] = json.dumps(
        delivery_policy,
        sort_keys=True,
        separators=(",", ":"),
    )
    if task.claim_lock:
        env["HERMES_KANBAN_CLAIM_LOCK"] = task.claim_lock
    # Goal-loop mode: the worker reads these and wraps its run in the
    # Ralph-style /goal judge loop (see cli.py quiet-mode path). Only set
    # when enabled so non-goal tasks keep a clean env.
    if task.goal_mode:
        env["HERMES_KANBAN_GOAL_MODE"] = "1"
        if task.goal_max_turns is not None:
            env["HERMES_KANBAN_GOAL_MAX_TURNS"] = str(int(task.goal_max_turns))
    terminal_timeout = _worker_terminal_timeout_env(
        task.max_runtime_seconds,
        env.get("TERMINAL_TIMEOUT"),
    )
    if terminal_timeout is not None:
        env["TERMINAL_TIMEOUT"] = terminal_timeout
    foreground_timeout = _worker_terminal_timeout_env(
        task.max_runtime_seconds,
        env.get("TERMINAL_MAX_FOREGROUND_TIMEOUT"),
    )
    if foreground_timeout is not None:
        env["TERMINAL_MAX_FOREGROUND_TIMEOUT"] = foreground_timeout
    # Pin the shared board + workspaces root the dispatcher resolved, so
    # that even when the worker activates a profile (`hermes -p <name>`
    # rewrites HERMES_HOME), its kanban paths still match the
    # dispatcher's. Belt-and-braces with the `get_default_hermes_root()`
    # resolution in `kanban_home()` — symmetric resolution is the norm,
    # but unusual symlink / Docker layouts are caught here too.
    env["HERMES_KANBAN_DB"] = str(kanban_db_path(board=board))
    env["HERMES_KANBAN_WORKSPACES_ROOT"] = str(workspaces_root(board=board))
    # Board slug — the final defense-in-depth pin. If the worker ever
    # resolves kanban paths without the DB / workspaces env vars, the
    # board slug still forces it to the right directory.
    resolved_board = _normalize_board_slug(board) or get_current_board()
    env["HERMES_KANBAN_BOARD"] = resolved_board
    # HERMES_PROFILE is the author the kanban_comment tool defaults to.
    # `hermes -p <assignee>` activates the profile, but the env var is
    # what the tool reads — set it explicitly here so comments are
    # attributed correctly regardless of how the child loads config.
    env["HERMES_PROFILE"] = profile_arg

    # A worker must NEVER boot the interactive TUI: an inherited HERMES_TUI=1
    # or a `display.interface: tui` in the profile's config would send the
    # quiet chat run into the Ink TUI, whose no-TTY bail-out exits 0 without
    # doing the task → "protocol violation" on every attempt. `--cli` is the
    # highest-precedence interface override; dropping the env var covers
    # older hermes builds on PATH that predate the flag's precedence.
    env.pop("HERMES_TUI", None)

    cmd = [
        *_resolve_hermes_argv(),
        "-p", profile_arg,
        "--cli",
        # Worker subprocesses switch to a profile-scoped HERMES_HOME above,
        # so they see that profile's shell-hook allowlist instead of the
        # dispatcher's root allowlist. Pass --accept-hooks explicitly so
        # profile-local worker sessions still register configured hooks.
        "--accept-hooks",
        "--cli",
    ]
    # Per-task force-loaded skills. Each name goes in its own
    # `--skills X` pair rather than a single comma-joined arg: the CLI
    # accepts both forms (action='append' + comma-split), but
    # per-name pairs are easier to read in `ps` output and avoid any
    # quoting ambiguity if a skill name ever contains unusual chars.
    if task.skills:
        for sk in task.skills:
            if sk:
                cmd.extend(["--skills", sk])
    requested_model = requested_route.get("model")
    requested_provider = requested_route.get("provider")
    requested_effort = requested_route.get("reasoning_effort")
    if requested_model:
        cmd.extend(["-m", requested_model])
        env["HERMES_MODEL"] = requested_model
    if requested_provider:
        cmd.extend(["--provider", requested_provider])
        env["HERMES_PROVIDER"] = requested_provider
        env["HERMES_MODEL_PROVIDER"] = requested_provider
    if requested_effort:
        cmd.extend(["--reasoning-effort", requested_effort])
        env["HERMES_REASONING_EFFORT"] = requested_effort
    worker_toolsets = (
        run_toolsets
        if run_toolsets is not None
        else _resolve_worker_cli_toolsets(env.get("HERMES_HOME"))
    )
    if worker_toolsets is not None:
        cmd.extend(["--toolsets", ",".join(worker_toolsets)])
    cmd.extend([
        "chat",
        "-q", prompt,
    ])
    if task.goal_mode:
        # Goal-mode workers must take the fully-quiet single-query path:
        # the kanban goal-loop hook (_run_kanban_goal_loop_q) only runs in
        # cli.py's quiet branch. Without -Q the worker gets exactly one
        # turn, prints text, exits rc=0, and the dispatcher records a
        # protocol violation (incident 2026-06-09 t_d9cbe312).
        cmd.append("-Q")
    # Redirect output to a per-task log under <board-root>/logs/.
    # Anchored at the board root (not the shared kanban root), so
    # `hermes kanban log` on a specific board reads its own file and
    # logs don't collide across boards that happen to share task ids.
    log_dir = worker_logs_dir(board=board)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{task.id}.log"
    rotate_bytes, backup_count = worker_log_rotation_config()
    _rotate_worker_log(log_path, rotate_bytes, backup_count)

    # Cross-platform two-phase start gate. The child polls for an unguessable
    # token and then attests the task/run/claim/PID tuple directly from the
    # pinned DB before it can parse or execute the chat command. If the parent
    # dies or attach fails, no token appears and the child exits on timeout.
    gate_dir = log_dir / ".start-gates"
    gate_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    gate_token = secrets.token_urlsafe(32)
    gate_name = hashlib.sha256(
        f"{task.id}:{task.current_run_id}:{gate_token}".encode("utf-8")
    ).hexdigest()
    gate_path = gate_dir / gate_name
    env["HERMES_KANBAN_START_GATE_PATH"] = str(gate_path)
    env["HERMES_KANBAN_START_GATE_TOKEN"] = gate_token
    env["HERMES_KANBAN_START_GATE_TIMEOUT_SECONDS"] = "30"

    # Use 'a' so a re-run on unblock appends rather than overwrites.
    log_f = open(log_path, "ab")
    try:
        proc = subprocess.Popen(  # noqa: S603 -- argv is a fixed list built above
            cmd,
            cwd=workspace if os.path.isdir(workspace) else None,
            stdin=subprocess.DEVNULL,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
            creationflags=subprocess.CREATE_NO_WINDOW if _IS_WINDOWS else 0,
        )
    except FileNotFoundError:
        log_f.close()
        raise RuntimeError(
            "`hermes` executable not found on PATH. "
            "Install Hermes Agent or activate its venv before running the kanban dispatcher."
        )
    except Exception:
        log_f.close()
        raise
    # Popen duplicates the descriptor into the child, so the parent can close
    # its copy immediately without interrupting worker logging.
    log_f.close()
    try:
        import psutil  # type: ignore

        process_started_at = float(psutil.Process(proc.pid).create_time())
        if _IS_WINDOWS:
            process_group_id = None
            process_session_id = None
        else:
            process_group_id = int(os.getpgid(proc.pid))
            process_session_id = int(os.getsid(proc.pid))
    except Exception:
        # A receipt without exact identity cannot be durably attached. The
        # caller owns ``abort`` and will terminate the still-gated subprocess.
        process_started_at = None
        process_group_id = None
        process_session_id = None

    def _release() -> None:
        _publish_start_gate_token(gate_path, gate_token)

    def _abort() -> None:
        try:
            gate_path.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            if proc.poll() is not None:
                return
            if not _IS_WINDOWS and os.getpgid(proc.pid) == proc.pid:
                import signal

                os.killpg(proc.pid, signal.SIGTERM)  # windows-footgun: ok — _IS_WINDOWS-gated
            else:
                proc.terminate()
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                if not _IS_WINDOWS and os.getpgid(proc.pid) == proc.pid:
                    import signal

                    os.killpg(proc.pid, signal.SIGKILL)  # windows-footgun: ok — _IS_WINDOWS-gated
                else:
                    proc.kill()
        except (ProcessLookupError, PermissionError, OSError):
            return

    return SpawnReceipt(
        pid=int(proc.pid),
        release=_release,
        abort=_abort,
        process_started_at=process_started_at,
        process_group_id=process_group_id,
        session_id=process_session_id,
    )


# ---------------------------------------------------------------------------
# Long-lived dispatcher daemon
# ---------------------------------------------------------------------------

def run_daemon(
    *,
    interval: float = 60.0,
    max_spawn: Optional[int] = None,
    failure_limit: int = DEFAULT_SPAWN_FAILURE_LIMIT,
    stop_event=None,
    on_tick=None,
) -> None:
    """Run the dispatcher in a loop until interrupted.

    Calls :func:`dispatch_once` every ``interval`` seconds. Exits cleanly
    on SIGINT / SIGTERM so ``hermes kanban daemon`` is systemd-friendly.
    ``stop_event`` (a :class:`threading.Event`) and ``on_tick`` (a
    callable receiving the :class:`DispatchResult`) are test hooks.
    """
    import signal
    import threading

    if stop_event is None:
        stop_event = threading.Event()

    def _handle(_signum, _frame):
        stop_event.set()

    # Install handlers only when running on the main thread — tests call
    # this inline from worker threads and signal() would raise there.
    if threading.current_thread() is threading.main_thread():
        for sig_name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, sig_name, None)
            if sig is not None:
                try:
                    signal.signal(sig, _handle)
                except (ValueError, OSError):
                    pass

    while not stop_event.is_set():
        try:
            with contextlib.closing(connect()) as conn:
                res = dispatch_once(
                    conn,
                    max_spawn=max_spawn,
                    failure_limit=failure_limit,
                )
            if on_tick is not None:
                try:
                    on_tick(res)
                except Exception:
                    pass
        except Exception:
            # Don't let any single tick kill the daemon.
            import traceback
            traceback.print_exc()
        stop_event.wait(timeout=interval)


# ---------------------------------------------------------------------------
# Worker context builder (what a spawned worker sees)
# ---------------------------------------------------------------------------

def build_worker_context(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    _use_continuation: bool = True,
    _now_override: Optional[int] = None,
) -> str:
    """Return the full text a worker should read to understand its task.

    Order:
      1. Task title (mandatory).
      2. Task body (optional opening post, capped at 8 KB).
      3. Compact identities of direct downstream child tasks, when any are
         already declared. Bodies and results are deliberately excluded.
      4. Prior attempts on THIS task (most recent ``_CTX_MAX_PRIOR_ATTEMPTS``
         shown; older attempts collapsed into a one-line summary).
         Each attempt's ``summary`` / ``error`` / ``metadata`` capped at
         ``_CTX_MAX_FIELD_BYTES`` each.
      5. Structured handoff results of every done parent task. Prefers
         ``run.summary`` / ``run.metadata`` when the parent was executed
         via a run; falls back to ``task.result`` for older data. Same
         per-field cap.
      6. Cross-task role history for the assignee (most recent 5
         completed runs on other tasks).
      7. Comment thread (most recent ``_CTX_MAX_COMMENTS`` shown, older
         collapsed).

    All caps exist so worker prompts stay bounded even on pathological
    boards (retry-heavy tasks, comment storms). The per-field char cap
    prevents a single 1 MB summary from dominating context.
    """
    task = get_task(conn, task_id)
    if not task:
        raise ValueError(f"unknown task {task_id}")

    # A continuation-enabled run consumes its immutable claim-time snapshot.
    # Live blockers still gate completion and surface in kanban_show status;
    # they are intentionally not injected mid-conversation because mutating a
    # cached prompt is one of Hermes's most expensive correctness failures.
    if _use_continuation and task.current_run_id is not None:
        continuation = get_continuation_manifest(
            conn, task.current_run_id, task_id=task_id, require_current=True,
        )
        if continuation is not None:
            return str(continuation.compiled_context["rendered"])

    # Single clock reading shared by every relative-age stamp below, so all
    # ages in one rendering are consistent ("3h ago" / "3h ago", not drifting
    # by the seconds it takes to build the block).
    _now = int(_now_override) if _now_override is not None else int(time.time())

    def _cap(s: Optional[str], limit: int = _CTX_MAX_FIELD_BYTES) -> str:
        """Truncate a string to `limit` chars with a visible ellipsis."""
        if not s:
            return ""
        s = s.strip()
        if len(s) <= limit:
            return s
        return s[:limit] + f"… [truncated, {len(s) - limit} chars omitted]"

    def _section_size(section_lines: list[str]) -> int:
        return len(("\n".join(section_lines) + "\n").encode("utf-8"))

    def _omitted_ids(ids: list[str]) -> str:
        """Bounded identifier sample that preserves both ends of a fan-in."""
        if len(ids) <= 100:
            return ", ".join(ids)
        return ", ".join([*ids[:20], "…", *ids[-20:]])

    def _hard_cap(text: str) -> str:
        raw = text.encode("utf-8")
        if len(raw) <= _CTX_MAX_TOTAL_BYTES:
            return text
        marker = b"\n\n_[additional context omitted by 48 KiB aggregate budget]_\n"
        prefix = raw[: _CTX_MAX_TOTAL_BYTES - len(marker)]
        return prefix.decode("utf-8", errors="ignore").rstrip() + marker.decode()

    lines: list[str] = []
    lines.append(f"# Kanban task {task.id}: {task.title}")
    lines.append("")
    lines.append(f"Assignee: {task.assignee or '(unassigned)'}")
    lines.append(f"Status:   {task.status}")
    if task.tenant:
        lines.append(f"Tenant:   {task.tenant}")
    lines.append(f"Workspace: {task.workspace_kind} @ {task.workspace_path or '(unresolved)'}")
    if task.is_publication:
        lines.append("Publication contract: remote readback is required before completion")
        lines.append(f"Expected SHA: {task.publication_expected_sha or '(missing)'}")
        lines.append(
            f"Remote ref: {task.publication_remote or '(missing)'} "
            f"{task.publication_ref or '(missing)'}"
        )
    if task.max_runtime_seconds is not None:
        terminal_timeout = _worker_terminal_timeout_env(
            task.max_runtime_seconds,
            os.environ.get("TERMINAL_TIMEOUT"),
        )
        effective_terminal_timeout = terminal_timeout or os.environ.get("TERMINAL_TIMEOUT")
        lines.append(f"Max runtime: {task.max_runtime_seconds}s")
        if effective_terminal_timeout:
            lines.append(f"Terminal timeout: {effective_terminal_timeout}s")
    if task.branch_name:
        lines.append(f"Branch:   {task.branch_name}")
    lines.append("")

    current_artifact = get_current_review_artifact(conn, task_id)
    if current_artifact is not None:
        artifact_lines = [
            "## Current review artifact — authoritative",
            f"Generation: {current_artifact.generation}",
            f"Attachment: {current_artifact.attachment_id}",
            f"SHA-256: {current_artifact.sha256}",
            f"Path: {current_artifact.stored_path or '(missing attachment row)'}",
            "Supersedes artifact paths preserved in the historical task body.",
        ]
        try:
            _verify_review_artifact_binding(conn, current_artifact)
        except ReviewArtifactError as exc:
            artifact_lines.append(f"Integrity check: FAILED — {exc}")
        lines.extend([*artifact_lines, ""])

    if task.body and task.body.strip():
        lines.append("## Body")
        lines.append(_cap(task.body, _CTX_MAX_BODY_BYTES))
        lines.append("")

    # Attachments — files uploaded to this task (PDFs, source docs,
    # images). Surface the absolute on-disk path so the worker, which has
    # full file-tool access, can read them directly (read_file, terminal
    # `pdftotext`, etc.). On the local terminal backend the path resolves
    # as-is; remote backends need the kanban attachments dir mounted.
    attachments = list_attachments(conn, task_id)
    if attachments:
        attachment_lines = ["## Attachments"]
        attachment_lines.append(
            "Files attached to this task. Read them with the file/terminal "
            "tools at the absolute paths below:"
        )
        omitted_attachments: list[str] = []
        for att in attachments:
            size_kb = max(1, (att.size + 1023) // 1024) if att.size else 0
            size_str = f", {size_kb} KB" if size_kb else ""
            ctype = f", {att.content_type}" if att.content_type else ""
            entry = _cap(
                f"- `{att.filename}`{ctype}{size_str} → `{att.stored_path}`",
                1024,
            )
            if _section_size([*attachment_lines, entry]) <= (
                _CTX_MAX_ATTACHMENTS_BYTES - 768
            ):
                attachment_lines.append(entry)
            else:
                omitted_attachments.append(att.filename)
        if omitted_attachments:
            attachment_lines.append(
                f"_({len(omitted_attachments)} attachment path(s) omitted by "
                f"section budget: {_cap(', '.join(omitted_attachments), 512)})_"
            )
        lines.extend([*attachment_lines, ""])

    # Planned downstream work — expose only compact identities for direct
    # children. Atomic workflow compilation creates the whole graph before the
    # first worker starts, so hiding child cards causes workers to recreate work
    # that is already waiting behind them. Dynamic/remediation children and
    # ordinary non-workflow children use the same direct-link contract: showing
    # their explicit workflow identity (or ``(none)``) makes the distinction
    # visible without leaking their bodies, results, comments, or run history.
    child_count_row = conn.execute(
        "SELECT COUNT(*) AS n FROM task_links WHERE parent_id = ?",
        (task_id,),
    ).fetchone()
    child_count = int(child_count_row["n"]) if child_count_row else 0
    if child_count:
        child_rows = conn.execute(
            """SELECT t.id, t.title, t.assignee, t.status,
                      t.current_step_key, t.workflow_key
                 FROM task_links l
                 JOIN tasks t ON t.id = l.child_id
                WHERE l.parent_id = ?
                ORDER BY
                      CASE WHEN ? IS NOT NULL AND t.workflow_key = ? THEN 0 ELSE 1 END,
                      COALESCE(t.workflow_key, ''),
                      CASE WHEN t.current_step_key IS NULL OR t.current_step_key = ''
                           THEN 1 ELSE 0 END,
                      COALESCE(t.current_step_key, ''),
                      t.id
                LIMIT ?""",
            (
                task_id,
                task.workflow_key,
                task.workflow_key,
                _CTX_MAX_DOWNSTREAM_TASKS,
            ),
        ).fetchall()
        downstream_section = [
            "## Planned downstream workflow steps",
            "_These direct child tasks are already declared. Reuse the declared "
            "task IDs and workflow steps; do not create duplicate delegated or "
            "Kanban cards for the same work._",
        ]
        omitted_children = child_count - len(child_rows)
        for row in child_rows:
            title = " ".join(str(row["title"] or "").split())
            assignee = " ".join(str(row["assignee"] or "(unassigned)").split())
            status = " ".join(str(row["status"] or "(unknown)").split())
            step_key = " ".join(str(row["current_step_key"] or "(none)").split())
            workflow_key = " ".join(str(row["workflow_key"] or "(none)").split())
            entry = (
                f"- `{row['id']}` — {_cap(title, 512)} | "
                f"assignee: {_cap(assignee, 128)} | status: {_cap(status, 64)} | "
                f"step: {_cap(step_key, 256)} | workflow: {_cap(workflow_key, 256)}"
            )
            if _section_size([*downstream_section, entry]) <= (
                _CTX_MAX_DOWNSTREAM_BYTES - 512
            ):
                downstream_section.append(entry)
            else:
                omitted_children += 1
        if omitted_children:
            downstream_section.append(
                f"_({omitted_children} additional direct child task"
                f"{'s' if omitted_children != 1 else ''} omitted by section budget)_"
            )
        lines.extend([*downstream_section, ""])

    # Prior attempts — show closed runs so a retrying worker sees the
    # history. Skip the currently-active run (that's this worker).
    # Cap at _CTX_MAX_PRIOR_ATTEMPTS most-recent closed runs; older
    # attempts get collapsed into a one-line marker so the worker knows
    # more exist without bloating the prompt.
    all_prior = [r for r in list_runs(conn, task_id) if r.ended_at is not None]
    # list_runs returns ascending by started_at; "most recent" = last N
    if len(all_prior) > _CTX_MAX_PRIOR_ATTEMPTS:
        omitted = len(all_prior) - _CTX_MAX_PRIOR_ATTEMPTS
        shown = all_prior[-_CTX_MAX_PRIOR_ATTEMPTS:]
        first_shown_idx = omitted + 1
    else:
        omitted = 0
        shown = all_prior
        first_shown_idx = 1
    if shown:
        attempt_groups: list[list[str]] = []
        for offset, run in enumerate(shown):
            idx = first_shown_idx + offset
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(run.started_at))
            age = _relative_age(run.started_at, _now)
            ts_disp = f"{ts}, {age}" if age else ts
            profile = run.profile or "(unknown)"
            outcome = run.outcome or run.status
            group = [f"### Attempt {idx} — {outcome} ({profile}, {ts_disp})"]
            if run.summary and run.summary.strip():
                group.append(_cap(run.summary))
            if run.error and run.error.strip():
                group.append(f"_error_: {_cap(run.error)}")
            if run.metadata:
                try:
                    meta_str = json.dumps(run.metadata, ensure_ascii=False, sort_keys=True)
                    group.append(f"_metadata_: `{_cap(meta_str)}`")
                except Exception:
                    pass
            group.append("")
            attempt_groups.append(group)

        selected_attempts: list[list[str]] = []
        attempt_header = ["## Prior attempts on this task"]
        # Prefer the newest recoverable state when aggregate history is large.
        for group in reversed(attempt_groups):
            candidate = [
                *attempt_header,
                *[line for selected in reversed(selected_attempts) for line in selected],
                *group,
            ]
            if _section_size(candidate) <= _CTX_MAX_ATTEMPTS_BYTES - 512:
                selected_attempts.append(group)
            else:
                omitted += 1
        if omitted:
            attempt_header.append(
                f"_({omitted} earlier attempt{'s' if omitted != 1 else ''} omitted; "
                f"showing most recent {len(selected_attempts)})_"
            )
        lines.extend(attempt_header)
        for group in reversed(selected_attempts):
            lines.extend(group)

    # Parents: prefer the most-recent 'completed' run's summary + metadata,
    # fall back to ``task.result`` when no run rows exist (legacy DBs,
    # or tasks completed before the runs table landed).
    parent_rows = conn.execute(
        "SELECT parent_id FROM task_links WHERE child_id = ? ORDER BY parent_id",
        (task_id,),
    ).fetchall()
    parent_ids = [r["parent_id"] for r in parent_rows]

    if parent_ids:
        parent_groups: list[tuple[str, list[str]]] = []
        for pid in parent_ids:
            pt = get_task(conn, pid)
            if not pt or pt.status != "done":
                continue
            runs = [r for r in list_runs(conn, pid) if r.outcome == "completed"]
            runs.sort(key=lambda r: r.started_at, reverse=True)
            run = runs[0] if runs else None

            # When did this parent's result get produced? Prefer the
            # completed run's end time; fall back to the task's completed_at.
            done_ts = None
            if run is not None and getattr(run, "ended_at", None):
                done_ts = run.ended_at
            elif pt.completed_at:
                done_ts = pt.completed_at
            age = _relative_age(done_ts, _now)
            group = [f"### {pid}" + (f" (completed {age})" if age else "")]

            body_lines: list[str] = []
            if run is not None and run.summary and run.summary.strip():
                body_lines.append(_cap(run.summary))
            elif pt.result:
                body_lines.append(_cap(pt.result))
            else:
                body_lines.append("(no result recorded)")

            if run is not None and run.metadata:
                try:
                    meta_str = json.dumps(run.metadata, ensure_ascii=False, sort_keys=True)
                    body_lines.append(f"_metadata_: `{_cap(meta_str)}`")
                except Exception:
                    pass
            group.extend(body_lines)
            group.append("")
            parent_groups.append((pid, group))

        if parent_groups:
            parent_section = [
                "## Parent task results",
                "_Handoffs from upstream tasks are point-in-time snapshots, "
                "not live state. Re-verify stale facts before treating them "
                "as current._",
            ]
            omitted_parent_ids: list[str] = []
            for pid, group in parent_groups:
                if _section_size([*parent_section, *group]) <= (
                    _CTX_MAX_PARENTS_BYTES - 1536
                ):
                    parent_section.extend(group)
                else:
                    omitted_parent_ids.append(pid)
            if omitted_parent_ids:
                parent_section.append(
                    f"_({len(omitted_parent_ids)} parent handoff(s) omitted by "
                    f"section budget; ids: {_omitted_ids(omitted_parent_ids)})_"
                )
                parent_section.append("")
            lines.extend(parent_section)

    # Cross-task role history: what else has THIS assignee completed
    # recently? Gives the worker implicit continuity — "I'm the reviewer
    # and my last three reviews focused on security" — without forcing
    # the user to wire anything into SOUL.md / MEMORY.md. Bounded to the
    # most recent 5 completed runs, excluding this task so the retry
    # section above isn't duplicated. Safe on assignee=None (skipped).
    if task.assignee:
        role_rows = conn.execute(
            "SELECT t.id, t.title, r.summary, r.ended_at "
            "FROM task_runs r JOIN tasks t ON r.task_id = t.id "
            "WHERE r.profile = ? AND r.task_id != ? "
            "  AND r.outcome = 'completed' "
            "ORDER BY r.ended_at DESC LIMIT 5",
            (task.assignee, task_id),
        ).fetchall()
        if role_rows:
            lines.append(f"## Recent work by @{task.assignee}")
            for row in role_rows:
                ts = time.strftime(
                    "%Y-%m-%d %H:%M", time.localtime(int(row["ended_at"]))
                )
                age = _relative_age(row["ended_at"], _now)
                ts_disp = f"{ts}, {age}" if age else ts
                s = (row["summary"] or "").strip().splitlines()
                first = s[0][:200] if s else "(no summary)"
                lines.append(f"- {row['id']} — {row['title']} ({ts_disp}): {first}")
            lines.append("")

    # Comments: cap at the most-recent _CTX_MAX_COMMENTS so
    # comment-storm tasks don't blow out the worker's prompt. Older
    # comments summarised in a one-line marker like prior attempts.
    all_comments = list_comments(conn, task_id)
    if len(all_comments) > _CTX_MAX_COMMENTS:
        omitted_c = len(all_comments) - _CTX_MAX_COMMENTS
        shown_c = all_comments[-_CTX_MAX_COMMENTS:]
    else:
        omitted_c = 0
        shown_c = all_comments
    if shown_c:
        comment_groups: list[list[str]] = []
        for c in shown_c:
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(c.created_at))
            age = _relative_age(c.created_at, _now)
            ts_disp = f"{ts}, {age}" if age else ts
            # Render author with explicit "comment from worker" framing so
            # operator-controlled HERMES_PROFILE values like "hermes-system"
            # or "operator" can't be misread by the next worker as a system
            # directive above the (attacker-influenceable) comment body.
            # Defense-in-depth — the LLM-controlled author-forgery surface
            # was already closed in #22435. See #22452.
            safe_author = (c.author or "").replace("`", "")
            comment_groups.append(
                [
                    f"comment from worker `{safe_author}` at {ts_disp}:",
                    _cap(c.body, _CTX_MAX_COMMENT_BYTES),
                    "",
                ]
            )

        selected_comments: list[list[str]] = []
        for group in reversed(comment_groups):
            candidate = [
                "## Comment thread",
                *[line for selected in reversed(selected_comments) for line in selected],
                *group,
            ]
            if _section_size(candidate) <= _CTX_MAX_COMMENTS_BYTES - 512:
                selected_comments.append(group)
            else:
                omitted_c += 1
        lines.append("## Comment thread")
        if omitted_c:
            lines.append(
                f"_({omitted_c} earlier comment{'s' if omitted_c != 1 else ''} "
                f"omitted; showing most recent {len(selected_comments)})_"
            )
        for group in reversed(selected_comments):
            lines.extend(group)

    return _hard_cap("\n".join(lines).rstrip() + "\n")


# ---------------------------------------------------------------------------
# Stats + SLA helpers
# ---------------------------------------------------------------------------

def board_stats(conn: sqlite3.Connection) -> dict:
    """Per-status + per-assignee counts, plus the oldest ``ready`` age in
    seconds (the clearest staleness signal for a router or HUD).
    """
    by_status: dict[str, int] = {}
    for row in conn.execute(
        "SELECT status, COUNT(*) AS n FROM tasks "
        "WHERE status != 'archived' GROUP BY status"
    ):
        by_status[row["status"]] = int(row["n"])

    by_assignee: dict[str, dict[str, int]] = {}
    for row in conn.execute(
        "SELECT assignee, status, COUNT(*) AS n FROM tasks "
        "WHERE status != 'archived' AND assignee IS NOT NULL "
        "GROUP BY assignee, status"
    ):
        by_assignee.setdefault(row["assignee"], {})[row["status"]] = int(row["n"])

    oldest_row = conn.execute(
        "SELECT MIN(created_at) AS ts FROM tasks WHERE status = 'ready'"
    ).fetchone()
    now = int(time.time())
    oldest_ready_age = (
        (now - int(oldest_row["ts"]))
        if oldest_row and oldest_row["ts"] is not None else None
    )

    return {
        "by_status": by_status,
        "by_assignee": by_assignee,
        "oldest_ready_age_seconds": oldest_ready_age,
        "now": now,
    }


def _to_epoch(val) -> Optional[int]:
    """Normalise a timestamp to unix epoch seconds.

    Accepts ints (pass-through), numeric strings, and ISO-8601 strings.
    Returns ``None`` for ``None`` / empty values.
    """
    if val is None:
        return None
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return int(val)
    s = str(val).strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        pass
    # ISO-8601 fallback (e.g. '2026-05-10T15:00:00Z')
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except (ValueError, OSError):
        return None


def task_age(task: Task) -> dict:
    """Return age metrics for a single task. All values are seconds or None."""
    now = int(time.time())
    _c = _to_epoch(task.created_at)
    _s = _to_epoch(task.started_at)
    _co = _to_epoch(task.completed_at)
    age_since_created = now - _c if _c is not None else None
    age_since_started = now - _s if _s is not None else None
    time_to_complete = (
        _co - (_s or _c) if _co is not None else None
    )
    return {
        "created_age_seconds": age_since_created,
        "started_age_seconds": age_since_started,
        "time_to_complete_seconds": time_to_complete,
    }


# ---------------------------------------------------------------------------
# Notification subscriptions (used by the gateway kanban-notifier)
# ---------------------------------------------------------------------------

def add_notify_sub(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    user_id: Optional[str] = None,
    notifier_profile: Optional[str] = None,
    kinds: Optional[Iterable[str]] = None,
) -> None:
    """Register a gateway source that wants terminal-state notifications
    for ``task_id``. Idempotent on (task, platform, chat, thread).

    ``kinds`` (BUILD-508), if given, must be a subset of TERMINAL_KINDS —
    the only kinds either consumer ever claims. Stored as a sorted JSON
    array; omitted/None (the default) means "all kinds", matching every
    subscription's behavior before this filter existed.
    """
    kinds_json: Optional[str] = None
    if kinds is not None:
        kind_set = {str(k) for k in kinds}
        invalid = kind_set - set(TERMINAL_KINDS)
        if invalid:
            raise ValueError(
                f"kinds must be a subset of TERMINAL_KINDS, got invalid: {sorted(invalid)}"
            )
        kinds_json = json.dumps(sorted(kind_set))
    now = int(time.time())
    with write_txn(conn):
        conn.execute(
            """
            INSERT OR IGNORE INTO kanban_notify_subs
                (task_id, platform, chat_id, thread_id, user_id, notifier_profile,
                 created_at, kinds_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id, platform, chat_id, thread_id or "", user_id,
                notifier_profile, now, kinds_json,
            ),
        )
        if notifier_profile:
            # Self-heal legacy rows that predate notifier ownership by
            # backfilling only when the existing value is unset.
            conn.execute(
                """
                UPDATE kanban_notify_subs
                   SET notifier_profile = ?
                 WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ?
                   AND (notifier_profile IS NULL OR notifier_profile = '')
                """,
                (notifier_profile, task_id, platform, chat_id, thread_id or ""),
            )
        if kinds_json is not None:
            # Same self-heal shape as notifier_profile above: only backfills
            # a row that predates this filter (kinds_json still NULL). An
            # already-filtered row's intent isn't silently overwritten by a
            # later re-subscribe.
            conn.execute(
                """
                UPDATE kanban_notify_subs
                   SET kinds_json = ?
                 WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ?
                   AND kinds_json IS NULL
                """,
                (kinds_json, task_id, platform, chat_id, thread_id or ""),
            )


def list_notify_subs(
    conn: sqlite3.Connection, task_id: Optional[str] = None,
) -> list[dict]:
    if task_id is not None:
        rows = conn.execute(
            "SELECT * FROM kanban_notify_subs WHERE task_id = ?", (task_id,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM kanban_notify_subs").fetchall()
    out = []
    for r in rows:
        sub = dict(r)
        # Malformed rows have been observed live (2026-07-18: five rows whose
        # task_id was a TABLE NAME and platform an integer row count wedged
        # the notifier tick for every board with "'int' object has no
        # attribute 'lower'"). One bad row must degrade to a warning, not
        # kill every consumer of this list — skip anything whose routing
        # fields aren't strings.
        if not isinstance(sub.get("task_id"), str) or not isinstance(
            sub.get("platform"), str
        ):
            _log.warning(
                "kanban: skipping malformed notify sub row (task_id=%r "
                "platform=%r) — delete it from kanban_notify_subs",
                sub.get("task_id"), sub.get("platform"),
            )
            continue
        out.append(sub)
    return out


def notify_sub_kinds(sub: dict) -> Optional["frozenset[str]"]:
    """Return a subscription row's event-kind filter, or ``None`` for "all
    kinds" (BUILD-508). ``None`` covers every pre-BUILD-508 row (NULL
    ``kinds_json``) as well as any unparseable value, which fails open to
    the pre-existing behavior rather than silently dropping events."""
    raw = sub.get("kinds_json")
    if not raw:
        return None
    try:
        kinds = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(kinds, list):
        return None
    return frozenset(str(k) for k in kinds)


def remove_notify_sub(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
) -> bool:
    with write_txn(conn):
        cur = conn.execute(
            "DELETE FROM kanban_notify_subs WHERE task_id = ? "
            "AND platform = ? AND chat_id = ? AND thread_id = ?",
            (task_id, platform, chat_id, thread_id or ""),
        )
    return cur.rowcount > 0


def unseen_events_for_sub(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    kinds: Optional[Iterable[str]] = None,
    through_event_id: Optional[int] = None,
) -> tuple[int, list[Event]]:
    """Return ``(new_cursor, events)`` for a given subscription.

    Only events with ``id > last_event_id`` are returned. The subscription's
    cursor is NOT advanced here; call :func:`advance_notify_cursor` after
    the gateway has successfully delivered the notifications.
    """
    row = conn.execute(
        "SELECT last_event_id FROM kanban_notify_subs "
        "WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ?",
        (task_id, platform, chat_id, thread_id or ""),
    ).fetchone()
    if row is None:
        return 0, []
    cursor = int(row["last_event_id"])
    kind_list = list(kinds) if kinds else None
    q = (
        "SELECT * FROM task_events WHERE task_id = ? AND id > ? "
        + ("AND id <= ? " if through_event_id is not None else "")
        + ("AND kind IN (" + ",".join("?" * len(kind_list)) + ") " if kind_list else "")
        + "ORDER BY id ASC"
    )
    params: list[Any] = [task_id, cursor]
    if through_event_id is not None:
        params.append(int(through_event_id))
    if kind_list:
        params.extend(kind_list)
    rows = conn.execute(q, params).fetchall()
    out: list[Event] = []
    max_id = cursor
    for r in rows:
        try:
            payload = json.loads(r["payload"]) if r["payload"] else None
        except Exception:
            payload = None
        out.append(Event(
            id=r["id"], task_id=r["task_id"], kind=r["kind"],
            payload=payload, created_at=r["created_at"],
            run_id=(int(r["run_id"]) if "run_id" in r.keys() and r["run_id"] is not None else None),
        ))
        max_id = max(max_id, int(r["id"]))
    return max_id, out


def claim_unseen_events_for_sub(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    kinds: Optional[Iterable[str]] = None,
    expected_old_cursor: Optional[int] = None,
    through_event_id: Optional[int] = None,
) -> tuple[int, int, list[Event]]:
    """Atomically claim unseen notification events for one subscription.

    Returns ``(old_cursor, new_cursor, events)``. When events are returned,
    ``kanban_notify_subs.last_event_id`` has already been advanced to
    ``new_cursor`` inside a ``BEGIN IMMEDIATE`` transaction. That makes the
    notifier's read/claim step single-owner across multiple gateway watcher
    processes pointed at the same board DB: concurrent watchers serialize on
    SQLite's writer lock, and only the first process sees and claims a given
    event range.

    Callers should send the claimed events, then either leave the cursor at
    ``new_cursor`` on success or call :func:`rewind_notify_cursor` if delivery
    failed before any terminal unsubscribe removed the row.
    """
    with write_txn(conn):
        row = conn.execute(
            "SELECT last_event_id FROM kanban_notify_subs "
            "WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ?",
            (task_id, platform, chat_id, thread_id or ""),
        ).fetchone()
        if row is None:
            return 0, 0, []
        old_cursor = int(row["last_event_id"])
        if expected_old_cursor is not None and old_cursor != int(expected_old_cursor):
            return old_cursor, old_cursor, []
        new_cursor, events = unseen_events_for_sub(
            conn,
            task_id=task_id,
            platform=platform,
            chat_id=chat_id,
            thread_id=thread_id,
            kinds=kinds,
            through_event_id=through_event_id,
        )
        if not events:
            return old_cursor, old_cursor, []
        conn.execute(
            "UPDATE kanban_notify_subs SET last_event_id = ? "
            "WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ? "
            "AND last_event_id = ?",
            (int(new_cursor), task_id, platform, chat_id, thread_id or "", int(old_cursor)),
        )
        return old_cursor, new_cursor, events


def advance_notify_cursor(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    new_cursor: int,
) -> None:
    with write_txn(conn):
        conn.execute(
            "UPDATE kanban_notify_subs SET last_event_id = ? "
            "WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ?",
            (int(new_cursor), task_id, platform, chat_id, thread_id or ""),
        )


def advance_notify_cursor_monotonic(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    new_cursor: int,
) -> bool:
    """Advance a subscription cursor without ever regressing newer progress."""
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE kanban_notify_subs SET last_event_id = ? "
            "WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ? "
            "AND last_event_id < ?",
            (
                int(new_cursor), task_id, platform, chat_id, thread_id or "",
                int(new_cursor),
            ),
        )
    return cur.rowcount > 0


def notify_delivery_key(
    *,
    resolved_db_path: str | os.PathLike[str],
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str],
    first_event_id: int,
    last_event_id: int,
) -> str:
    """Return stable idempotency material for one bounded delivery range."""
    canonical_path = str(Path(resolved_db_path).expanduser().resolve(strict=False))
    path_hash = hashlib.sha256(canonical_path.encode("utf-8")).hexdigest()
    return "/".join(
        (
            path_hash,
            str(task_id),
            str(platform).lower(),
            str(chat_id),
            str(thread_id or "-"),
            str(int(first_event_id)),
            str(int(last_event_id)),
        )
    )


def record_notify_delivery(
    conn: sqlite3.Connection,
    *,
    delivery_key: str,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    first_event_id: int,
    last_event_id: int,
    status: str,
) -> None:
    """Record the outcome of one bounded notification delivery range.

    Keyed on the stable ``notify_delivery_key`` so a watcher restart (which
    replays nothing past the cursor anyway) re-recording the same range is a
    no-op update rather than a duplicate row. This is an audit/telemetry
    ledger — the ``kanban_notify_subs`` cursor remains the exactly-once dedup
    authority; this table just makes "was it actually delivered?" verifiable,
    which a ``session_subscription_exists`` check cannot answer.
    """
    now = int(time.time())
    with write_txn(conn):
        conn.execute(
            """
            INSERT INTO notify_deliveries
                (delivery_key, task_id, platform, chat_id, thread_id,
                 first_event_id, last_event_id, status, attempts,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(delivery_key) DO UPDATE SET
                status = excluded.status,
                attempts = notify_deliveries.attempts + 1,
                updated_at = excluded.updated_at
            """,
            (
                delivery_key, task_id, platform, chat_id, thread_id or "",
                int(first_event_id), int(last_event_id), status, now, now,
            ),
        )


def list_notify_deliveries(
    conn: sqlite3.Connection, task_id: Optional[str] = None,
) -> list[dict]:
    """Return recorded deliveries (all, or for one task), newest first."""
    if task_id is not None:
        rows = conn.execute(
            "SELECT * FROM notify_deliveries WHERE task_id = ? "
            "ORDER BY updated_at DESC",
            (task_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM notify_deliveries ORDER BY updated_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def rewind_notify_cursor(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    claimed_cursor: int,
    old_cursor: int,
) -> bool:
    """Undo a notification claim when delivery fails.

    The CAS guard only rewinds if no later notifier advanced the row after our
    claim. This keeps retry behavior for transient send failures without
    clobbering newer progress.
    """
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE kanban_notify_subs SET last_event_id = ? "
            "WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ? "
            "AND last_event_id = ?",
            (
                int(old_cursor), task_id, platform, chat_id, thread_id or "",
                int(claimed_cursor),
            ),
        )
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Retention + garbage collection
# ---------------------------------------------------------------------------

def gc_events(
    conn: sqlite3.Connection, *, older_than_seconds: int = 30 * 24 * 3600,
) -> int:
    """Delete task_events rows older than ``older_than_seconds`` for tasks
    in a terminal state (``done`` or ``archived``). Returns the number of
    rows deleted. Running / ready / blocked tasks keep their full event
    history."""
    cutoff = int(time.time()) - int(older_than_seconds)
    with write_txn(conn):
        cur = conn.execute(
            "DELETE FROM task_events WHERE created_at < ? AND task_id IN "
            "(SELECT id FROM tasks WHERE status IN ('done', 'archived'))",
            (cutoff,),
        )
    return int(cur.rowcount or 0)


def gc_worker_logs(
    *, older_than_seconds: int = 30 * 24 * 3600,
    board: Optional[str] = None,
) -> int:
    """Delete worker log files older than ``older_than_seconds``. Returns
    the number of files removed. Kept separate from ``gc_events`` because
    log files live on disk, not in SQLite. Scoped to ``board`` (defaults
    to the active board) — per-board isolation means deleting logs from
    board A cannot touch board B's logs."""
    log_dir = worker_logs_dir(board=board)
    if not log_dir.exists():
        return 0
    cutoff = time.time() - older_than_seconds
    removed = 0
    for p in log_dir.iterdir():
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except OSError:
            continue
    return removed


# ---------------------------------------------------------------------------
# Worker log accessor
# ---------------------------------------------------------------------------

def worker_log_path(task_id: str, *, board: Optional[str] = None) -> Path:
    """Return the path to a worker's log file. The file may not exist
    (task never spawned, or log already GC'd).

    When ``board`` is None, resolves via the active board (env var →
    current-board file → default). The dispatcher always passes the
    board explicitly to avoid any resolution ambiguity when multiple
    boards exist."""
    return worker_logs_dir(board=board) / f"{task_id}.log"


def read_worker_log(
    task_id: str, *, tail_bytes: Optional[int] = None,
    board: Optional[str] = None,
) -> Optional[str]:
    """Read the worker log for ``task_id``. Returns None if the file
    doesn't exist. If ``tail_bytes`` is set, only the last N bytes are
    returned (useful for the dashboard drawer which shouldn't page megabytes)."""
    path = worker_log_path(task_id, board=board)
    if not path.exists():
        return None
    try:
        if tail_bytes is None:
            return path.read_text(encoding="utf-8", errors="replace")
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
                # Skip a partial line if we tailed mid-line. But if the
                # window has no newline at all (one giant log line),
                # readline() would eat everything — in that case don't
                # skip and return the raw tail.
                probe = f.tell()
                partial = f.readline()
                if not partial.endswith(b"\n") and f.tell() >= size:
                    f.seek(probe)
            data = f.read()
        return data.decode("utf-8", errors="replace")
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Assignee enumeration (known profiles + per-profile board stats)
# ---------------------------------------------------------------------------

def list_profiles_on_disk() -> list[str]:
    """Return the set of assignee/profile names discovered on disk.

    Includes:
    - named profiles under ``<default-root>/profiles/<name>/config.yaml``
    - the implicit ``default`` profile when the default Hermes root exists

    Reads profile paths directly so this module has no import dependency on
    ``hermes_cli.profiles`` (which pulls in a large chunk of the CLI startup
    path).
    """
    try:
        from hermes_constants import get_default_hermes_root
        default_root = get_default_hermes_root()
        profiles_dir = default_root / "profiles"
    except Exception:
        return []

    names: set[str] = set()
    if default_root.exists():
        names.add("default")

    if profiles_dir.is_dir():
        try:
            for entry in sorted(profiles_dir.iterdir()):
                if not entry.is_dir():
                    continue
                if (entry / "config.yaml").is_file():
                    names.add(entry.name)
        except OSError:
            pass

    return sorted(names)


def known_assignees(conn: sqlite3.Connection) -> list[dict]:
    """Return every assignee name known to the board or on disk.

    Each entry is ``{"name": str, "on_disk": bool, "counts": {status: n}}``.
    A name is included when it's a configured profile on disk OR when
    any non-archived task has it as the assignee. Used by:

    - ``hermes kanban assignees`` for the terminal.
    - The dashboard assignee dropdown (so a fresh profile appears in
      the picker even before it's been given any task).
    - Router-profile heuristics ("who's overloaded?") without scanning
      the whole board.
    """
    on_disk = set(list_profiles_on_disk())

    # Count tasks per (assignee, status), excluding archived.
    counts: dict[str, dict[str, int]] = {}
    for row in conn.execute(
        "SELECT assignee, status, COUNT(*) AS n FROM tasks "
        "WHERE status != 'archived' AND assignee IS NOT NULL "
        "GROUP BY assignee, status"
    ):
        counts.setdefault(row["assignee"], {})[row["status"]] = int(row["n"])

    names = sorted(on_disk | set(counts.keys()))
    return [
        {
            "name": name,
            "on_disk": name in on_disk,
            "counts": counts.get(name, {}),
        }
        for name in names
    ]


# ---------------------------------------------------------------------------
# Runs (attempt history on a task)
# ---------------------------------------------------------------------------

def list_runs(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    include_active: bool = True,
    state_type: Optional[str] = None,
    state_name: Optional[str] = None,
) -> list[Run]:
    """Return all runs for ``task_id`` in start order.

    ``include_active=True`` (default) includes the currently-running
    attempt if any. Set False to return only closed runs (useful for
    "how many prior attempts have there been?" checks).

    When ``state_type`` and ``state_name`` are set, restrict to rows
    where that column equals ``state_name`` (``state_type`` is
    ``status`` or ``outcome``). Both must be passed together.
    """
    if (state_type is None) ^ (state_name is None):
        raise ValueError("state_type and state_name must both be set or both omitted")
    if state_type is not None:
        if state_type not in ("status", "outcome"):
            raise ValueError("state_type must be 'status' or 'outcome'")
    q = "SELECT * FROM task_runs WHERE task_id = ?"
    params: list[Any] = [task_id]
    if not include_active:
        q += " AND ended_at IS NOT NULL"
    if state_type is not None:
        q += f" AND {state_type} = ?"
        params.append(state_name)
    q += " ORDER BY started_at ASC, id ASC"
    rows = conn.execute(q, params).fetchall()
    return [Run.from_row(r) for r in rows]


def get_run(conn: sqlite3.Connection, run_id: int) -> Optional[Run]:
    row = conn.execute(
        "SELECT * FROM task_runs WHERE id = ?", (int(run_id),),
    ).fetchone()
    return Run.from_row(row) if row else None


def latest_run(conn: sqlite3.Connection, task_id: str) -> Optional[Run]:
    """Return the most recent run regardless of outcome (active or closed)."""
    row = conn.execute(
        "SELECT * FROM task_runs WHERE task_id = ? "
        "ORDER BY started_at DESC, id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return Run.from_row(row) if row else None


def latest_summary(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """Return the latest non-null ``task_runs.summary`` for ``task_id``.

    The worker writes its handoff to ``task_runs.summary``
    via ``complete_task(summary=...)``; ``tasks.result`` is left empty
    unless the caller passes ``result=`` explicitly. Dashboards and CLI
    "show" views need this value to surface what a worker actually did
    — without it, ``tasks.result`` is NULL and the task looks like a
    no-op even when the run completed.

    Picks the most recent run by ``ended_at`` (falling back to ``id``
    for ties or unfinished rows). Returns None if no run has a summary.
    """
    row = conn.execute(
        "SELECT summary FROM task_runs "
        "WHERE task_id = ? AND summary IS NOT NULL AND summary != '' "
        "ORDER BY COALESCE(ended_at, started_at) DESC, id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return row["summary"] if row else None


def latest_summaries(
    conn: sqlite3.Connection, task_ids: Iterable[str]
) -> dict[str, str]:
    """Batch-fetch latest non-null summaries for a list of task ids.

    Used by the dashboard board endpoint to attach ``latest_summary`` to
    every card in a single SQL query, avoiding the N+1 pattern of
    calling :func:`latest_summary` per task. Returns a dict mapping
    ``task_id`` → summary string, omitting tasks with no summary.

    Approach: a window function picks the newest non-null-summary row
    per ``task_id``; works against SQLite ≥ 3.25 (default on every
    supported platform).
    """
    ids = list(task_ids)
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT task_id, summary FROM (
            SELECT task_id, summary,
                   ROW_NUMBER() OVER (
                       PARTITION BY task_id
                       ORDER BY COALESCE(ended_at, started_at) DESC, id DESC
                   ) AS rn
              FROM task_runs
             WHERE task_id IN ({placeholders})
               AND summary IS NOT NULL AND summary != ''
        ) WHERE rn = 1
        """,
        ids,
    ).fetchall()
    return {r["task_id"]: r["summary"] for r in rows}
