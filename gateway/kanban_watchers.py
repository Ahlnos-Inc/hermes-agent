"""Kanban board watcher methods for GatewayRunner.

Extracted verbatim from ``gateway/run.py`` (god-file decomposition Phase 3).
These are the background-loop methods that subscribe to kanban boards, deliver
notifications/artifacts, and drive the multi-agent dispatcher. They use only
``self`` state, so they live on a mixin that ``GatewayRunner`` inherits — the
``self._kanban_*`` call sites resolve identically via the MRO, making this a
behavior-neutral move that lifts ~1,000 LOC out of run.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Optional

from agent.i18n import t
from gateway.kanban_notifications import render_kanban_event
from hermes_cli.kanban_db import FAILURE_KINDS

# Match the logger run.py uses (logging.getLogger(__name__) where __name__ ==
# "gateway.run") so extracted log records keep their original logger name.
logger = logging.getLogger("gateway.run")


def _architecture_delivery_withheld(event: Any) -> bool:
    """Return whether an event may expose only its fixed gate receipt."""
    payload = getattr(event, "payload", None)
    return bool(isinstance(payload, dict) and payload.get("delivery_withheld"))


# Failure-kind events (BUILD-503): these are the ones that must reach the
# operator via the Telegram home fallback when the origin is unreachable.
# `completed` / `status` / `archived` / `unblocked` are not failures and are
# not worth waking the home channel for. Aliased from hermes_cli/kanban_db.py
# (BUILD-508) rather than redefined here — both the telegram send-failure
# fallback below and the tui orphan sweep (BUILD-506) use this name, and
# compile_workflow_graph's per-step kinds_json filter needs the identical set,
# so one definition avoids the three drifting apart (BUILD-443 pattern).
#
# sweep_orphaned_tui_sub() below claims with this exact set regardless of a
# swept subscription's own kinds_json (verified BUILD-508): every real
# subscription's filter is either NULL ("all kinds", a superset) or this same
# FAILURE_KINDS set (compile_workflow_graph's step-task subs), so intersecting
# with a per-sub filter here would never change what gets claimed. Treating a
# dead/orphaned session's failure events as always-escalate — irrespective of
# a hypothetical narrower per-sub filter — is also the correct safety-net
# behavior: a subscriber who scoped themselves out of failure pings while
# their desktop was alive shouldn't also lose the "your session died with a
# task stuck" escalation.

# BUILD-506: orphaned tui-subscription sweep age gate. A tui subscription's
# oldest unclaimed failure event must be at least this old, AND have no live
# session lease matching it (see _live_tui_session_ids), before the gateway
# will claim it and redeliver to the Telegram home channel. Env-overridable
# for ops tuning without a code change.
TUI_ORPHAN_AGE_ENV = "HERMES_KANBAN_TUI_ORPHAN_AGE_SECONDS"
DEFAULT_TUI_ORPHAN_AGE_SECONDS = 15 * 60


def tui_orphan_age_seconds() -> int:
    """Resolve the tui-orphan-sweep age gate (seconds), env-overridable."""
    raw = os.environ.get(TUI_ORPHAN_AGE_ENV, "").strip()
    if raw:
        try:
            val = int(raw)
        except ValueError:
            val = 0
        if val > 0:
            return val
    return DEFAULT_TUI_ORPHAN_AGE_SECONDS


def _live_tui_session_ids() -> "set[str]":
    """Cross-process liveness snapshot (BUILD-506).

    tui_gateway/server.py's own poller knows its live sessions from an
    in-process ``_sessions`` dict this (separate) gateway process can never
    see. The one liveness signal that DOES cross the process boundary is
    ``hermes_cli.active_sessions``' pid-checked lease file
    (``~/.hermes/runtime/active_sessions.json``): a tui session claims a
    lease keyed by its ``session_key`` (the same value stored as a tui
    subscription's ``chat_id``), and reading the registry prunes any lease
    whose pid is no longer alive.

    A match here is trusted immediately (see :func:`sweep_orphaned_tui_sub`).
    Absence is NOT proof of death: ``max_concurrent_sessions`` (the setting
    that gates whether leases are written at all) defaults to unset, so on
    most installs this set is empty even while a desktop session is very
    much alive. That is exactly why the age gate — not this set — is the
    actual safety backstop against stealing an active desktop's delivery.
    """
    try:
        from hermes_cli.active_sessions import active_session_registry_snapshot
        entries = active_session_registry_snapshot()
    except Exception:
        return set()
    return {
        str(entry.get("session_id"))
        for entry in entries
        if entry.get("session_id")
    }


def sweep_orphaned_tui_sub(
    kb,
    conn,
    sub: dict,
    *,
    live_session_ids: "set[str]",
    age_gate_seconds: int,
    now: Optional[float] = None,
) -> Optional[dict]:
    """Claim one orphaned tui subscription's unclaimed failure events (BUILD-506).

    A tui subscription is orphaned when no live session lease matches its
    ``chat_id`` (see :func:`_live_tui_session_ids`) AND its oldest unclaimed
    failure-kind event is older than ``age_gate_seconds``. Both conditions
    must hold — the live-lease check alone can't be trusted (see above), and
    the age gate alone would misfire on every normal desktop restart, which
    completes in seconds, and on a desktop that's merely mid-turn, whose own
    poller (``_poll_kanban_tui_subs``) claims within a ~2s cadence the
    instant it goes idle — both comfortably inside the default 15-minute
    gate.

    The claim reuses ``claim_unseen_events_for_sub``'s BEGIN IMMEDIATE + CAS
    (``expected_old_cursor``), the same exactly-once mechanism every other
    consumer of this cursor uses. THIS is what makes stealing an active
    desktop's delivery *impossible*, not just unlikely: if a live tui poller
    (or another gateway's sweep tick) claims the same range first, this call
    loses the CAS and returns ``None`` — never a duplicate delivery.

    Returns ``None`` when there is nothing to sweep (live owner, too fresh,
    no failure events, or lost the claim race). On success returns
    ``{"sub": sub, "task": task, "events": [...]}`` for the caller to render
    and deliver; the cursor has already moved past ``events`` at that point.
    """
    if (sub.get("platform") or "").lower() != "tui":
        return None
    chat_id = str(sub.get("chat_id") or "")
    if chat_id and chat_id in live_session_ids:
        return None  # active desktop owns this sub; never steal its events.
    old_cursor = int(sub.get("last_event_id") or 0)
    _peek_cursor, events = kb.unseen_events_for_sub(
        conn,
        task_id=sub["task_id"], platform=sub["platform"],
        chat_id=sub["chat_id"], thread_id=sub.get("thread_id") or "",
        kinds=FAILURE_KINDS,
    )
    if not events:
        return None
    now = time.time() if now is None else now
    if (now - events[0].created_at) < age_gate_seconds:
        return None  # too fresh — could be an in-progress desktop restart.
    _old, _new, claimed_events = kb.claim_unseen_events_for_sub(
        conn,
        task_id=sub["task_id"], platform=sub["platform"],
        chat_id=sub["chat_id"], thread_id=sub.get("thread_id") or "",
        kinds=FAILURE_KINDS, expected_old_cursor=old_cursor,
    )
    if not claimed_events:
        # Lost the CAS race: a live tui poller or a concurrent sweep already
        # claimed this range between our peek and our claim.
        return None
    return {
        "sub": sub,
        "task": kb.get_task(conn, sub["task_id"]),
        "events": claimed_events,
    }


# Catch-all sweep bounds: only look back this far for unsubscribed failure
# events (an upgrade onto a months-old board must not replay ancient
# history), and cap per tick so a pathological board can't flood the home
# channel in one burst. Retry backoff applies when the home channel is
# missing/unreachable so the 5s tick doesn't warn-spam.
ORPHAN_FAILURE_LOOKBACK_SECONDS = 72 * 3600
ORPHAN_FAILURE_BATCH_PER_TICK = 5
ORPHAN_FAILURE_RETRY_SECONDS = 900


def _collect_unsubscribed_failure_events(kb, conn) -> "list[dict]":
    """Recent failure-kind events on tasks with NO notify subscription.

    Dedup is one home delivery per (task, kind), recorded in the
    ``notify_deliveries`` ledger under a ``home-sweep/`` key — a task that
    crashes five times in a retry loop pings home once, and the ledger row
    survives restarts (unlike a subscription cursor, which unsubscribed
    tasks don't have).
    """
    kinds = sorted(FAILURE_KINDS)
    placeholders = ",".join("?" for _ in kinds)
    cutoff = int(time.time()) - ORPHAN_FAILURE_LOOKBACK_SECONDS
    rows = conn.execute(
        f"""SELECT e.id, e.task_id, e.kind, e.payload, e.created_at, e.run_id
            FROM task_events e
            JOIN tasks t ON t.id = e.task_id
            WHERE e.kind IN ({placeholders})
              AND e.created_at >= ?
              AND t.status NOT IN ('done', 'archived')
              AND NOT EXISTS (SELECT 1 FROM kanban_notify_subs s
                              WHERE s.task_id = e.task_id)
              AND NOT EXISTS (SELECT 1 FROM notify_deliveries nd
                              WHERE nd.delivery_key =
                                  'home-sweep/' || e.task_id || '/' || e.kind)
            ORDER BY e.id
            LIMIT ?""",
        (*kinds, cutoff, ORPHAN_FAILURE_BATCH_PER_TICK),
    ).fetchall()
    out: list[dict] = []
    seen_task_kinds: set[tuple] = set()
    for row in rows:
        tk = (row["task_id"], row["kind"])
        if tk in seen_task_kinds:
            continue
        seen_task_kinds.add(tk)
        payload = None
        if row["payload"]:
            try:
                payload = json.loads(row["payload"])
            except Exception:
                payload = None
        try:
            task = kb.get_task(conn, row["task_id"])
        except Exception:
            task = None
        out.append({
            "task_id": row["task_id"],
            "task": task,
            "event": kb.Event(
                id=int(row["id"]), task_id=row["task_id"], kind=row["kind"],
                payload=payload, created_at=int(row["created_at"] or 0),
                run_id=row["run_id"],
            ),
            "delivery_key": f"home-sweep/{row['task_id']}/{row['kind']}",
        })
    return out


# Block kinds that resolve WITHOUT a human: 'dependency' waits on parents,
# 'transient' is retried by the dispatcher. Everything else lands blocked for
# a person, so it belongs on the operator's Kanban console topic.
# gave_up: the dispatcher exhausted retries and parked the task blocked
# WITHOUT emitting a blocked event — it is the clearest "human needed"
# state of all (2026-07-19: two architect tasks sat silent this way).
HUMAN_BLOCK_EVENT_KINDS = ("blocked", "block_loop_detected", "gave_up")
HUMAN_BLOCK_AUTO_KINDS = ("transient", "dependency")


def _collect_human_blocked_events(kb, conn) -> "list[dict]":
    """Blocked events that need a human, for the Kanban console topic.

    Unlike the home sweep this fires regardless of subscriptions — the
    console topic is the operator's single queue of "waiting on me" items.
    Dedup is per event id (a re-block after an unblock is a NEW ask and
    alerts again), recorded in the ``notify_deliveries`` ledger under a
    ``human-block/`` key so it survives restarts. Same lookback/batch caps
    as the home sweep so an old board can't flood the topic.
    """
    kinds = HUMAN_BLOCK_EVENT_KINDS
    placeholders = ",".join("?" for _ in kinds)
    auto_placeholders = ",".join("?" for _ in HUMAN_BLOCK_AUTO_KINDS)
    cutoff = int(time.time()) - ORPHAN_FAILURE_LOOKBACK_SECONDS
    rows = conn.execute(
        f"""SELECT e.id, e.task_id, e.kind, e.payload, e.created_at, e.run_id
            FROM task_events e
            JOIN tasks t ON t.id = e.task_id
            WHERE e.kind IN ({placeholders})
              AND e.created_at >= ?
              AND t.status NOT IN ('done', 'archived')
              AND COALESCE(json_extract(e.payload, '$.kind'), '')
                  NOT IN ({auto_placeholders})
              AND NOT EXISTS (SELECT 1 FROM notify_deliveries nd
                              WHERE nd.delivery_key =
                                  'human-block/' || e.task_id || '/' || e.id)
            ORDER BY e.id
            LIMIT ?""",
        (*kinds, cutoff, *HUMAN_BLOCK_AUTO_KINDS, ORPHAN_FAILURE_BATCH_PER_TICK),
    ).fetchall()
    out: list[dict] = []
    for row in rows:
        payload = None
        if row["payload"]:
            try:
                payload = json.loads(row["payload"])
            except Exception:
                payload = None
        try:
            task = kb.get_task(conn, row["task_id"])
        except Exception:
            task = None
        out.append({
            "task_id": row["task_id"],
            "task": task,
            "event": kb.Event(
                id=int(row["id"]), task_id=row["task_id"], kind=row["kind"],
                payload=payload, created_at=int(row["created_at"] or 0),
                run_id=row["run_id"],
            ),
            "delivery_key": f"human-block/{row['task_id']}/{int(row['id'])}",
        })
    return out


def _snapshot_process_fds(db_path: Path, out_path: Path) -> "Optional[str]":
    """Dump this process's open-fd table next to a corruption backup.

    BUILD-531: the recurring board corruption is stray in-process writes
    through recycled file descriptors (PR #29 found TLS record bytes in
    page one; the three archived corruption images show three unrelated
    random-offset structural signatures, and every legitimate writer path
    audits clean). The missing evidence at each event is WHICH fd aliased
    the DB file — capture the whole table at detection time so the next
    occurrence identifies the offender instead of just the victim.

    Returns a one-line summary (or None on failure). Any fd whose inode
    matches the corrupt DB is flagged ``**DB-ALIAS**``.
    """
    try:
        db_stat = os.stat(db_path)
    except OSError:
        db_stat = None
    lines = [f"# fd map at corruption detection for {db_path}"]
    aliases = 0
    try:
        fd_names = sorted(int(n) for n in os.listdir("/dev/fd") if n.isdigit())
    except OSError as exc:
        return f"fd snapshot unavailable: {exc}"
    for fd in fd_names:
        try:
            st = os.fstat(fd)
        except OSError:
            continue
        try:
            target = os.readlink(f"/dev/fd/{fd}")
        except OSError:
            target = "?"
        flag = ""
        if (
            db_stat is not None
            and st.st_dev == db_stat.st_dev
            and st.st_ino == db_stat.st_ino
        ):
            flag = " **DB-ALIAS**"
            aliases += 1
        lines.append(
            f"fd={fd} mode={oct(st.st_mode)} ino={st.st_ino} "
            f"size={st.st_size} -> {target}{flag}"
        )
    try:
        out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as exc:
        return f"fd snapshot write failed: {exc}"
    return f"{len(lines) - 1} fds captured, {aliases} aliasing the DB, at {out_path}"


def _attempt_board_db_recovery(kb, slug: str) -> "tuple[bool, str]":
    """Try to rebuild a corrupt board DB in place via ``sqlite3 .recover``.

    The 2026-07-18 vault-v2 incident: index-level corruption paused dispatch
    for 16+ hours while the dispatcher quietly re-logged every 5 minutes —
    yet the very first manual ``.recover`` produced a clean DB. Corruption
    of this class is mechanically recoverable, so the dispatcher does it
    itself instead of waiting for a human to notice log spam.

    Safety: the corrupt original (and its -wal/-shm sidecars) are renamed to
    ``.corrupt-<ts>.bak`` before the recovered file is moved in, so nothing
    is ever destroyed; the swap uses ``os.replace`` (atomic on POSIX). The
    recovered DB must pass ``PRAGMA integrity_check`` before the swap.
    Returns (ok, detail).
    """
    sqlite3_cli = shutil.which("sqlite3")
    if not sqlite3_cli:
        return False, "sqlite3 CLI not on PATH"
    path = Path(kb.kanban_db_path(slug))
    if not path.exists():
        return False, f"{path} does not exist"
    ts = time.strftime("%Y%m%d-%H%M%S")
    recovered_tmp = path.with_name(path.name + f".recovered-{ts}.tmp")
    corrupt_bak = path.with_name(path.name + f".corrupt-{ts}.bak")

    def _dev_ino(p: Path) -> "tuple[int, int] | None":
        try:
            st = os.stat(p)
            return (st.st_dev, st.st_ino)
        except OSError:
            return None

    # BUILD-567 writer-safe swap. Hold ONE connection open from before the
    # .recover snapshot through the swap: ``PRAGMA data_version`` read on the
    # SAME connection changes iff another connection commits in between. It is
    # the right signal here because it is immune to checkpoint churn (a reader
    # moving WAL frames into the DB is not a new commit) and because
    # data_version is only meaningful within a single connection — comparing it
    # across the separate opens a stat-based token would need is useless. The
    # DB's dev/ino guards against an *external* replacement of the file (another
    # recovery) that this connection could not observe. A raw sqlite3 connection
    # is used (not kb.connect) precisely because the health guard would refuse
    # to open the corrupt file; the idle connection holds no lock during the
    # slow .recover.
    ino_before = _dev_ino(path)
    guard = None
    try:
        guard = sqlite3.connect(str(path), timeout=5.0)
        dv_before = guard.execute("PRAGMA data_version").fetchone()[0]
    except sqlite3.Error as exc:
        if guard is not None:
            guard.close()
        return False, f"could not open board to guard the swap: {exc}"
    try:
        # No immutable=1: it tells SQLite the file cannot change, so the
        # .recover pass skips locks AND the live WAL — the 2026-07-18 ROOT
        # board corruption was WAL-resident first and an immutable scan
        # missed it, and a moving file read without locks can enshrine a
        # torn snapshot as the "recovered" DB. A normal open takes shared
        # locks and includes WAL content; writers just block briefly.
        dump = subprocess.run(
            [sqlite3_cli, str(path), ".recover"],
            capture_output=True, timeout=300,
        )
        if dump.returncode != 0 or not dump.stdout.strip():
            return False, f".recover failed: {dump.stderr.decode(errors='replace')[:200]}"
        load = subprocess.run(
            [sqlite3_cli, str(recovered_tmp)],
            input=dump.stdout, capture_output=True, timeout=300,
        )
        if load.returncode != 0:
            return False, f"reload failed: {load.stderr.decode(errors='replace')[:200]}"
        check = subprocess.run(
            [sqlite3_cli, str(recovered_tmp), "PRAGMA integrity_check"],
            capture_output=True, timeout=120,
        )
        if check.stdout.decode(errors="replace").strip() != "ok":
            return False, (
                "recovered DB failed integrity_check: "
                + check.stdout.decode(errors="replace")[:200]
            )
        # Take the write lock (BEGIN IMMEDIATE only acquires the RESERVED lock —
        # it does not read user btrees, so it still succeeds on an index-corrupt
        # DB) and re-verify nothing committed since the .recover snapshot.
        # Holding the lock across the os.replace sequence also stops a fresh
        # connect() from landing mid-swap and binding a stale -wal to the
        # recovered inode.
        try:
            guard.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            return False, f"could not acquire write lock for swap: {exc}"
        if _dev_ino(path) != ino_before:
            return False, "board file was replaced during recovery; aborting swap (retry next tick)"
        dv_after = guard.execute("PRAGMA data_version").fetchone()[0]
        if dv_after != dv_before:
            return False, (
                "board changed during recovery; aborting swap "
                "(a writer committed since the .recover snapshot; retry next tick)"
            )
        os.replace(path, corrupt_bak)
        for suffix in ("-wal", "-shm"):
            sidecar = path.with_name(path.name + suffix)
            if sidecar.exists():
                os.replace(sidecar, corrupt_bak.with_name(corrupt_bak.name + suffix))
        os.replace(recovered_tmp, path)
        return True, f"corrupt original preserved at {corrupt_bak}"
    except subprocess.TimeoutExpired:
        return False, "recovery subprocess timed out"
    except OSError as exc:
        return False, f"recovery swap failed: {exc}"
    finally:
        # guard's fd references the pre-swap inode; closing it drops locks only
        # on the discarded corrupt_bak, never on the freshly-installed DB.
        try:
            guard.rollback()
        except sqlite3.Error:
            pass
        try:
            guard.close()
        except sqlite3.Error:
            pass
        try:
            if recovered_tmp.exists() and path.exists() and not recovered_tmp.samefile(path):
                recovered_tmp.unlink()
        except OSError:
            pass


def _resolve_auto_decompose_settings(
    load_config: Callable[[], Any],
) -> "tuple[bool, int]":
    """Resolve the live (enabled, per_tick) auto-decompose settings.

    Read fresh from config on every dispatcher tick (#49638) so that flipping
    ``kanban.auto_decompose: false`` to STOP runaway fan-out takes effect on the
    next tick instead of requiring a gateway restart. Auto-decompose is a
    safety toggle — a user who sees it create and launch tasks they didn't
    intend reaches for this flag to halt it, and a stale boot-captured value
    silently ignoring that change is the bug reported in #49638.

    Fails **safe**: if the config read raises, return ``(False, 3)`` — a
    transient read error must never re-enable a feature the user turned off,
    nor fall back to the burst-prone default-on behaviour. ``per_tick`` is
    clamped to ``>= 1``.
    """
    try:
        cfg = load_config()
    except Exception:
        return False, 3
    kcfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    enabled = bool(kcfg.get("auto_decompose", True))
    try:
        per_tick = int(kcfg.get("auto_decompose_per_tick", 3) or 3)
    except (TypeError, ValueError):
        per_tick = 3
    if per_tick < 1:
        per_tick = 1
    return enabled, per_tick


def _acquire_singleton_lock(lock_path) -> "tuple[Optional[object], str]":
    """Take an exclusive, non-blocking advisory lock for the sole dispatcher.

    Only one gateway process machine-wide may run the embedded kanban
    dispatcher: concurrent dispatchers double the reclaim frequency (each
    runs its own ``release_stale_claims`` → promote → dispatch loop), double
    claim-attempt events in the event log, and — with ``wal_autocheckpoint=0`` —
    concurrent manual WAL checkpoints can corrupt index pages. The
    ``dispatch_in_gateway`` config flag is the primary control; this lock is the
    backstop that survives config drift and same-profile restart races.

    Delegates to :func:`gateway.status._try_acquire_file_lock` (``fcntl`` on
    POSIX, ``msvcrt`` on Windows) so the guard is cross-platform.

    Returns ``(handle, "held")`` on success — the caller keeps the file handle
    for the process lifetime and **must** release it via
    :func:`_release_singleton_lock` when done. ``(None, "contended")`` when
    another process holds the lock (caller must NOT dispatch). ``(None,
    "unavailable")`` when locking cannot be performed (non-POSIX filesystem
    without flock, or the status.py helpers are unimportable) — caller falls
    back to config-only control.
    """
    try:
        from gateway.status import _try_acquire_file_lock  # deferred; same package
    except ImportError:
        return None, "unavailable"
    try:
        Path(lock_path).parent.mkdir(parents=True, exist_ok=True)
        handle = open(str(lock_path), "a+", encoding="utf-8")
    except OSError:
        return None, "unavailable"
    if not _try_acquire_file_lock(handle):
        handle.close()
        return None, "contended"
    # Stamp our pid (+ acquired_at) into the lock file for operator
    # diagnostics only (BUILD-263) — e.g. `hermes kanban dispatch` can tell
    # an operator "held by pid 1234" when it refuses. Mutual exclusion itself
    # is enforced entirely by the ``flock`` above; this content is never
    # consulted to decide held/contended, so a write failure here must not
    # fail the acquire.
    try:
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps({"pid": os.getpid(), "acquired_at": int(time.time())}))
        handle.flush()
    except OSError:
        pass
    return handle, "held"


def _release_singleton_lock(handle) -> None:
    """Release a dispatcher singleton lock acquired via :func:`_acquire_singleton_lock`."""
    if handle is None:
        return
    try:
        from gateway.status import _release_file_lock
        _release_file_lock(handle)
    except Exception:
        pass
    try:
        handle.close()
    except Exception:
        pass


def _read_singleton_lock_holder_pid(lock_path) -> Optional[int]:
    """Best-effort read of the pid stamped by the current lock holder.

    Diagnostics only (BUILD-263) — used to make a refusal message like
    "kanban dispatch: another dispatcher (pid 1234) holds the lock" instead
    of a bare "lock is held". Never raises; returns ``None`` on any failure
    (missing file, empty/legacy content, holder mid-write race, etc).
    """
    try:
        raw = Path(lock_path).read_text(encoding="utf-8").strip()
        if not raw:
            return None
        data = json.loads(raw)
        pid = data.get("pid")
        return int(pid) if pid else None
    except Exception:
        return None


def dispatcher_singleton_lock_path() -> Path:
    """Canonical path of the machine-wide dispatcher singleton lock.

    Shared by the gateway's embedded dispatcher loop
    (:meth:`GatewayKanbanWatchersMixin._kanban_dispatcher_watcher`) and the
    ``hermes kanban dispatch`` CLI entry (BUILD-263) so both compete for the
    exact same advisory lock — the CLI wiring is what closes the gap the
    2026-07-08 incident exposed (orphaned ``hermes kanban dispatch`` shell
    loops racing the gateway's internal scheduler with no mutual exclusion).
    """
    from hermes_cli import kanban_db as _kb
    return _kb.kanban_home() / "kanban" / ".dispatcher.lock"


def classify_stuck_streak(results) -> "tuple[bool, str]":
    """Return whether a zero-spawn streak is only concurrency deferrals."""
    from hermes_cli import kanban_db as _kb
    counts = _kb.dispatch_cause_counts(results)
    return (
        _kb.dispatch_causes_capacity_only(counts),
        _kb.summarize_dispatch_causes(results),
    )


class DispatcherStuckEscalationState:
    """Pure state machine deciding when to fire the dispatcher-stuck Telegram
    escalation (BUILD-263).

    Deliberately decoupled from asyncio and the notify transport so it's
    unit-testable with a fake clock and no gateway/adapters: feed it the
    current ``bad_ticks`` count and a timestamp via :meth:`should_alert`,
    and it tells you whether to fire. Call :meth:`mark_alerted` right after
    a successful send, and :meth:`mark_recovered` as soon as the dispatcher
    spawns a worker again (or the ready queue drains) — a fresh stuck period
    should not be silenced by the previous period's re-alert timer.

    Semantics:

    * No alert below ``escalate_after_ticks`` consecutive bad ticks.
    * First alert fires the moment the threshold is reached.
    * Subsequent alerts are rate-limited to at most one per
      ``realert_seconds`` while still stuck.
    * :meth:`mark_recovered` resets both the "already alerted" flag and the
      re-alert timer, so the NEXT stuck streak alerts immediately at
      threshold rather than waiting out the old cadence.
    """

    def __init__(
        self,
        *,
        escalate_after_ticks: int = 12,
        realert_seconds: int = 3600,
    ) -> None:
        self.escalate_after_ticks = max(1, int(escalate_after_ticks))
        self.realert_seconds = max(1, int(realert_seconds))
        self._alerted = False
        self._last_alert_at = 0.0

    def should_alert(self, bad_ticks: int, now: float) -> bool:
        if bad_ticks < self.escalate_after_ticks:
            return False
        if not self._alerted:
            return True
        return (now - self._last_alert_at) >= self.realert_seconds

    def mark_alerted(self, now: float) -> None:
        self._alerted = True
        self._last_alert_at = now

    def mark_recovered(self) -> None:
        self._alerted = False
        self._last_alert_at = 0.0


class GatewayKanbanWatchersMixin:
    """Kanban watcher / notifier / dispatcher loops for GatewayRunner."""

    async def _kanban_notifier_watcher(self, interval: float = 5.0) -> None:
        """Poll ``kanban_notify_subs`` and deliver terminal events to users.

        For each subscription row, fetches ``task_events`` newer than the
        stored cursor with kind in the terminal set (``completed``,
        ``blocked``, ``gave_up``, ``crashed``, ``timed_out``). Sends one
        message per new event to ``(platform, chat_id, thread_id)``,
        then advances the cursor. When a task reaches a terminal state
        (``completed`` / ``archived``), the subscription is removed.

        Runs in the gateway event loop; all SQLite work is pushed to a
        thread via ``asyncio.to_thread`` so the loop never blocks on the
        WAL lock. Failures in one tick don't stop subsequent ticks.

        **Multi-board:** iterates every board discovered on disk per
        tick. Subscriptions live inside each board's own DB and cannot
        cross boards, so delivery semantics are unchanged — this is
        purely a fan-out of the single-DB poll.
        """
        # Gate: only the dispatch-owning gateway opens kanban DBs for notifier polling.
        # Non-dispatch gateways have no subscriptions to deliver — all kanban state lives
        # in the dispatch owner's per-board DBs. This prevents N-gateway -shm contention.
        # TODO: gate per-board when per-board dispatcher_owner tracking lands.
        try:
            from hermes_cli.config import load_config as _load_config
        except Exception:
            logger.warning("kanban notifier: config loader unavailable; disabled")
            return
        env_override = os.environ.get("HERMES_KANBAN_DISPATCH_IN_GATEWAY", "").strip().lower()
        if env_override in {"0", "false", "no", "off"}:
            logger.info("kanban notifier: disabled via HERMES_KANBAN_DISPATCH_IN_GATEWAY env")
            return
        try:
            cfg = _load_config()
        except Exception as exc:
            logger.warning("kanban notifier: cannot load config (%s); disabled", exc)
            return
        kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
        if not kanban_cfg.get("dispatch_in_gateway", True):
            logger.info(
                "kanban notifier: disabled via config kanban.dispatch_in_gateway=false"
            )
            return
        from gateway.config import Platform as _Platform
        try:
            from hermes_cli import kanban_db as _kb
        except Exception:
            logger.warning("kanban notifier: kanban_db not importable; notifier disabled")
            return

        # Shared with tui_gateway/server.py's consumer — see
        # hermes_cli/kanban_db.py::TERMINAL_KINDS (BUILD-443) so the two
        # can't drift out of sync.
        TERMINAL_KINDS = _kb.TERMINAL_KINDS
        # Subscriptions are removed only when the task reaches a truly final
        # status (done / archived). We used to also unsub on any terminal
        # event kind (gave_up / crashed / timed_out / blocked), but that
        # silently dropped the user out of the loop whenever the dispatcher
        # respawned the task: a worker that crashes, gets reclaimed, runs
        # again, and crashes a second time would only notify on the first
        # crash because the subscription was deleted after the first event.
        # Same shape as the reblock-after-unblock cycle that PR #22941
        # fixed for `blocked`. Keeping the subscription alive until the
        # task is genuinely done lets the cursor (advanced atomically by
        # claim_unseen_events_for_sub) handle dedup, and any retry-loop
        # event reaches the user.
        # Per-subscription send-failure counter. Adapter.send raising
        # means the chat is dead (deleted, bot kicked, etc.) — after N
        # consecutive send failures the sub is dropped so we don't spin
        # against a dead chat every 5 seconds forever.
        MAX_SEND_FAILURES = 3
        sub_fail_counts: dict[tuple, int] = getattr(
            self, "_kanban_sub_fail_counts", {}
        )
        self._kanban_sub_fail_counts = sub_fail_counts
        notifier_profile = getattr(self, "_kanban_notifier_profile", None)
        if not notifier_profile:
            notifier_profile = self._active_profile_name()
            self._kanban_notifier_profile = notifier_profile

        def _parse_notification_sources(raw) -> set[str]:
            if raw in (None, "", False):
                return set()
            if raw is True:
                return {"*"}
            if isinstance(raw, str):
                return {p.strip() for p in raw.split(",") if p.strip()}
            if isinstance(raw, (list, tuple, set)):
                return {str(p).strip() for p in raw if str(p).strip()}
            return set()

        def _allowed_notification_sources() -> set[str]:
            cached = getattr(self, "_kanban_notification_sources", None)
            if cached is not None:
                return _parse_notification_sources(cached)
            try:
                from hermes_cli.config import load_config as _load_config

                cfg = _load_config()
                kanban_cfg = cfg.get("kanban") if isinstance(cfg, dict) else {}
                raw = (
                    kanban_cfg.get("notification_sources")
                    if isinstance(kanban_cfg, dict)
                    else None
                )
            except Exception:
                raw = None
            parsed = _parse_notification_sources(raw)
            setattr(self, "_kanban_notification_sources", parsed)
            return parsed

        def _notification_source_allowed(owner_profile: str | None) -> bool:
            if not owner_profile or owner_profile == notifier_profile:
                return True
            allowed = _allowed_notification_sources()
            return "*" in allowed or owner_profile in allowed

        # Initial delay so the gateway can finish wiring adapters.
        await asyncio.sleep(5)

        while self._running:
            try:
                def _collect():
                    deliveries: list[dict] = []
                    # BUILD-506: orphaned tui subs claimed this tick — a
                    # separate bucket because they're delivered via the
                    # Telegram home fallback, not a `platform`-keyed adapter.
                    tui_sweeps: list[dict] = []
                    # Failure events on unsubscribed tasks, routed to home.
                    orphan_failures: list[dict] = []
                    active_platforms = {
                        getattr(platform, "value", str(platform)).lower()
                        for platform in self.adapters.keys()
                    }
                    if not active_platforms:
                        logger.debug("kanban notifier: no connected adapters; skipping tick")
                        return deliveries, tui_sweeps, orphan_failures
                    live_tui_ids = _live_tui_session_ids()
                    tui_age_gate = tui_orphan_age_seconds()

                    # Enumerate every board on disk, but poll each resolved DB
                    # path once. Multiple slugs can point at the same DB when
                    # HERMES_KANBAN_DB pins the board path; without this guard
                    # one gateway could collect the same subscription/event
                    # more than once before advancing the cursor.
                    try:
                        boards = _kb.list_boards(include_archived=False)
                    except Exception:
                        boards = [_kb.read_board_metadata(_kb.DEFAULT_BOARD)]
                    seen_db_paths: set[str] = set()
                    for board_meta in boards:
                        slug = board_meta.get("slug") or _kb.DEFAULT_BOARD
                        db_path = board_meta.get("db_path")
                        try:
                            resolved_db_path = str(Path(db_path).expanduser().resolve()) if db_path else str(_kb.kanban_db_path(slug).resolve())
                        except Exception:
                            resolved_db_path = f"slug:{slug}"
                        if resolved_db_path in seen_db_paths:
                            logger.debug(
                                "kanban notifier: skipping duplicate board slug %s for DB %s",
                                slug, resolved_db_path,
                            )
                            continue
                        seen_db_paths.add(resolved_db_path)
                        try:
                            conn = _kb.connect(board=slug)
                        except Exception as exc:
                            logger.debug("kanban notifier: cannot open board %s: %s", slug, exc)
                            continue
                        try:
                            # `connect()` runs the schema + idempotent migration
                            # on first open per process, so an explicit
                            # `init_db()` here would be redundant. Worse:
                            # `init_db()` deliberately busts the per-process
                            # cache and re-runs the migration on a *second*
                            # connection, which races the first and used to
                            # log a benign but noisy `duplicate column name`
                            # traceback (and intermittent "database is locked"
                            # — issue #21378) on every gateway start against
                            # a legacy DB. `_add_column_if_missing` now
                            # tolerates that race, but we still skip the
                            # redundant call to avoid the wasted work.
                            subs = _kb.list_notify_subs(conn)
                            if not subs:
                                logger.debug("kanban notifier: board %s has no subscriptions", slug)
                            for sub in subs:
                                owner_profile = sub.get("notifier_profile") or None
                                if not _notification_source_allowed(owner_profile):
                                    logger.debug(
                                        "kanban notifier: subscription for %s owned by profile %s; current profile %s not allowed",
                                        sub.get("task_id"), owner_profile,
                                        notifier_profile,
                                    )
                                    continue
                                platform = (sub.get("platform") or "").lower()
                                if platform not in active_platforms:
                                    logger.debug(
                                        "kanban notifier: subscription for %s on %s skipped; adapter not connected",
                                        sub.get("task_id"), platform or "<missing>",
                                    )
                                    continue
                                old_cursor, cursor, events = _kb.claim_unseen_events_for_sub(
                                    conn,
                                    task_id=sub["task_id"],
                                    platform=sub["platform"],
                                    chat_id=sub["chat_id"],
                                    thread_id=sub.get("thread_id") or "",
                                    kinds=TERMINAL_KINDS,
                                )
                                if not events:
                                    continue
                                task = _kb.get_task(conn, sub["task_id"])
                                # Completion events carry only a compact
                                # payload summary for cheap dashboard/event-log
                                # reads. Chat notifications are the operator's
                                # primary feedback loop, so hydrate the
                                # associated run while the DB is already open
                                # and send the full worker handoff. Platform
                                # adapters own transport splitting (Telegram
                                # 4096 chars, etc.); the notifier should not
                                # silently chop words mid-summary.
                                event_runs = {}
                                for ev in events:
                                    if _architecture_delivery_withheld(ev):
                                        # The event intentionally carries only
                                        # a fixed approval receipt. Hydrating
                                        # its run would reattach the withheld
                                        # architecture handoff.
                                        continue
                                    run_id = getattr(ev, "run_id", None)
                                    if run_id is None:
                                        continue
                                    try:
                                        run = _kb.get_run(conn, int(run_id))
                                    except Exception:
                                        run = None
                                    if run is not None:
                                        event_runs[ev.id] = run
                                logger.debug(
                                    "kanban notifier: claimed %d event(s) for %s on board %s cursor %s→%s",
                                    len(events), sub["task_id"], slug, old_cursor, cursor,
                                )
                                deliveries.append({
                                    "sub": sub,
                                    "old_cursor": old_cursor,
                                    "cursor": cursor,
                                    "events": events,
                                    "event_runs": event_runs,
                                    "task": task,
                                    "board": slug,
                                    "db_path": resolved_db_path,
                                })

                            # BUILD-506: sweep orphaned tui subs on this same
                            # open connection. tui subs never appear above —
                            # "tui" is never a key in self.adapters — so they
                            # would otherwise sit unclaimed forever whenever
                            # the desktop process that owned them is gone.
                            for sub in subs:
                                swept = sweep_orphaned_tui_sub(
                                    _kb, conn, sub,
                                    live_session_ids=live_tui_ids,
                                    age_gate_seconds=tui_age_gate,
                                )
                                if swept is None:
                                    continue
                                swept["board"] = slug
                                swept["db_path"] = resolved_db_path
                                tui_sweeps.append(swept)

                            # Catch-all: failure events on tasks NOBODY
                            # subscribed to. Subscription wiring has failed
                            # silently before (pre-BUILD-503 workflows only
                            # subscribed their terminal task — the 2026-07-18
                            # gsthst-q2 incident where t_a43ae5e2 sat blocked
                            # for 2.5h unnoticed). This sweep is the invariant
                            # that no failure event goes unseen regardless of
                            # how the task was created; delivery is deduped
                            # per (task, kind) via the notify_deliveries
                            # ledger so a crash-retry loop pings home once,
                            # not once per recurrence.
                            try:
                                orphans = _collect_unsubscribed_failure_events(
                                    _kb, conn,
                                )
                                for o in orphans:
                                    o["board"] = slug
                                    o["db_path"] = resolved_db_path
                                orphan_failures.extend(orphans)
                            except Exception as exc:
                                logger.debug(
                                    "kanban notifier: orphan failure sweep "
                                    "failed for board %s: %s", slug, exc,
                                )
                            if human_block_target is not None:
                                try:
                                    blocked = _collect_human_blocked_events(
                                        _kb, conn,
                                    )
                                    for b in blocked:
                                        b["board"] = slug
                                    human_blocked.extend(blocked)
                                except Exception as exc:
                                    logger.debug(
                                        "kanban notifier: human-block sweep "
                                        "failed for board %s: %s", slug, exc,
                                    )
                        finally:
                            conn.close()
                    return deliveries, tui_sweeps, orphan_failures

                human_blocked: list = []
                raw_target = kanban_cfg.get("human_block_alerts")
                human_block_target = None
                if isinstance(raw_target, dict):
                    _hb_chat = str(raw_target.get("chat_id") or "").strip()
                    if _hb_chat:
                        human_block_target = {
                            "chat_id": _hb_chat,
                            "thread_id": str(
                                raw_target.get("thread_id") or ""
                            ).strip(),
                        }
                deliveries, tui_sweeps, orphan_failures = await asyncio.to_thread(_collect)
                for d in deliveries:
                    sub = d["sub"]
                    task = d["task"]
                    board_slug = d.get("board")
                    platform_str = (sub["platform"] or "").lower()
                    try:
                        plat = _Platform(platform_str)
                    except ValueError:
                        # Unknown platform string; skip and advance cursor so
                        # we don't replay forever.
                        await asyncio.to_thread(
                            self._kanban_advance, sub, d["cursor"], board_slug,
                        )
                        continue
                    sub_profile = sub.get("notifier_profile") or ""
                    # Route via the SAME chokepoint the authorization path uses
                    # (gateway/authz_mixin.py::_authorization_adapter): a stamped
                    # profile with its own adapter-registry entry must be served
                    # by THAT profile's same-platform adapter and must NOT silently
                    # fall back to the default profile's adapter — otherwise a
                    # secondary profile's task notification is delivered by the
                    # wrong bot (the cross-profile mis-delivery this whole change
                    # exists to fix). The helper returns None only when the profile
                    # (or default) genuinely has no adapter for the platform.
                    adapter = self._authorization_adapter(plat, sub_profile or None)
                    if adapter is None and (sub_profile or None) not in (None, notifier_profile):
                        # Cross-profile delivery authorized via
                        # notification_sources: the owner profile isn't
                        # registered with its own adapter in this gateway, so
                        # deliver through the current (notifier) profile's
                        # adapter. Upstream's _authorization_adapter fail-closes
                        # for a stamped-but-unregistered profile (correct for the
                        # inbound authz gate), but the notifier's outbound path
                        # must fall through to this gateway's bot — the behavior
                        # the cross-profile notification_sources feature relies on.
                        adapter = self._authorization_adapter(plat, notifier_profile)
                    if adapter is None:
                        logger.debug(
                            "kanban notifier: adapter %s disconnected before delivery for %s; rewinding claim",
                            platform_str, sub["task_id"],
                        )
                        await asyncio.to_thread(
                            self._kanban_rewind,
                            sub,
                            d["cursor"],
                            d.get("old_cursor", 0),
                            board_slug,
                        )
                        continue
                    event_runs = d.get("event_runs") or {}
                    # BUILD-508: per-subscription event-kind filter. None
                    # (every pre-BUILD-508 sub, and the workflow terminal
                    # task's own sub) means "all kinds" — unchanged behavior.
                    sub_kinds = _kb.notify_sub_kinds(sub)
                    for ev in d["events"]:
                        kind = ev.kind
                        if sub_kinds is not None and kind not in sub_kinds:
                            # Claimed-and-skipped: the cursor already moved
                            # past this event in the claim_unseen_events_for_sub
                            # call inside _collect() above, so dropping it here
                            # can never stall or replay it — exactly how
                            # archived/unblocked already fall through
                            # render_kanban_event returning None just below.
                            continue
                        run = event_runs.get(ev.id)
                        msg = render_kanban_event(
                            task_id=sub["task_id"], task=task, event=ev,
                            run=run, board_slug=board_slug,
                        )
                        if msg is None:
                            # archived / unblocked are claimed by TERMINAL_KINDS
                            # (so the cursor advances past them and they can't
                            # wedge a later completed/blocked event behind an
                            # unclaimed row) but are intentionally SILENT: an
                            # archive needs no user ping, and unblocked is an
                            # internal transition. They are also excluded from
                            # _WAKE_KINDS below, so they never wake the creator.
                            continue
                        metadata: dict[str, Any] = {}
                        if sub.get("thread_id"):
                            metadata["thread_id"] = sub["thread_id"]
                        sub_key = (
                            sub["task_id"], sub["platform"],
                            sub["chat_id"], sub.get("thread_id") or "",
                        )
                        try:
                            await adapter.send(
                                sub["chat_id"], msg, metadata=metadata,
                            )
                            logger.debug(
                                "kanban notifier: delivered %s event for %s to %s/%s on board %s",
                                kind, sub["task_id"], platform_str, sub["chat_id"], board_slug,
                            )
                            # After delivering the text notification, surface
                            # any artifact paths the worker referenced in
                            # ``kanban_complete(summary=..., artifacts=[...])``
                            # (or the legacy ``result`` field) as native
                            # uploads. ``extract_local_files`` finds bare
                            # absolute paths in the summary;
                            # ``send_document`` / ``send_image_file`` uploads
                            # them. Only fires on the ``completed`` event so
                            # we never spam attachments on retries.
                            if (
                                kind == "completed"
                                and not _architecture_delivery_withheld(ev)
                            ):
                                try:
                                    await self._deliver_kanban_artifacts(
                                        adapter=adapter,
                                        chat_id=sub["chat_id"],
                                        metadata=metadata,
                                        event_payload=getattr(ev, "payload", None),
                                        task=task,
                                    )
                                except Exception as art_exc:
                                    logger.debug(
                                        "kanban notifier: artifact delivery for %s failed: %s",
                                        sub["task_id"], art_exc,
                                    )
                            # Reset the failure counter on success.
                            sub_fail_counts.pop(sub_key, None)
                        except Exception as exc:
                            fails = sub_fail_counts.get(sub_key, 0) + 1
                            sub_fail_counts[sub_key] = fails
                            logger.warning(
                                "kanban notifier: send failed for %s on %s "
                                "(attempt %d/%d): %s",
                                sub["task_id"], platform_str, fails,
                                MAX_SEND_FAILURES, exc,
                            )
                            if fails >= MAX_SEND_FAILURES:
                                logger.warning(
                                    "kanban notifier: dropping subscription "
                                    "%s on %s after %d consecutive send failures",
                                    sub["task_id"], platform_str, fails,
                                )
                                # BUILD-503: origin chat is unreachable. Record
                                # the failed delivery and, for failure-kind
                                # events, route to the Telegram home channel so
                                # a stranded worker failure still reaches the
                                # operator before we drop the dead subscription.
                                await asyncio.to_thread(
                                    self._kanban_record_delivery,
                                    sub, d.get("db_path") or board_slug or "",
                                    d["events"][0].id, d["events"][-1].id,
                                    "failed", board_slug,
                                )
                                if kind in FAILURE_KINDS and msg:
                                    await self._kanban_notify_home_fallback(msg)
                                await asyncio.to_thread(self._kanban_unsub, sub, board_slug)
                                sub_fail_counts.pop(sub_key, None)
                            else:
                                await asyncio.to_thread(
                                    self._kanban_rewind,
                                    sub,
                                    d["cursor"],
                                    d.get("old_cursor", 0),
                                    board_slug,
                                )
                            # Rewind the pre-send claim on transient failure so
                            # a later tick can retry. After too many failures,
                            # dropping the subscription is the terminal action.
                            break
                    else:
                        # All events delivered; advance cursor. The cursor
                        # is the dedup mechanism — it prevents re-delivery
                        # of the same event on subsequent ticks.
                        await asyncio.to_thread(
                            self._kanban_advance, sub, d["cursor"], board_slug,
                        )
                        # BUILD-503: record the delivered range in the ledger
                        # so delivery is verifiable (subscription-exists != it
                        # was delivered). Idempotent on the delivery_key range.
                        await asyncio.to_thread(
                            self._kanban_record_delivery,
                            sub, d.get("db_path") or board_slug or "",
                            d["events"][0].id, d["events"][-1].id,
                            "delivered", board_slug,
                        )
                        # Unsubscribe only when the task has reached a truly
                        # final status (done / archived). For blocked /
                        # gave_up / crashed / timed_out the subscription is
                        # kept alive so the user gets notified again if the
                        # dispatcher respawns the task and it cycles into the
                        # same state. See the longer comment on TERMINAL_KINDS
                        # above for the failure mode this prevents.
                        task_terminal = task and task.status in {"done", "archived"}
                        _WAKE_KINDS = ("completed", "gave_up", "crashed", "timed_out", "blocked")
                        _wake_kinds = {ev.kind for ev in d["events"] if ev.kind in _WAKE_KINDS}
                        if _wake_kinds:
                            try:
                                _session_key = getattr(task, "session_id", None) or ""
                                if _session_key:
                                    _title = (task.title if task else sub["task_id"])[:120]
                                    _assignee = task.assignee if task else ""
                                    _parts = []
                                    if "completed" in _wake_kinds: _parts.append(t("gateway.kanban.wake.completed"))
                                    if "gave_up" in _wake_kinds: _parts.append(t("gateway.kanban.wake.gave_up"))
                                    if "crashed" in _wake_kinds: _parts.append(t("gateway.kanban.wake.crashed"))
                                    if "timed_out" in _wake_kinds: _parts.append(t("gateway.kanban.wake.timed_out"))
                                    if "blocked" in _wake_kinds: _parts.append(t("gateway.kanban.wake.blocked"))
                                    _status = t("gateway.kanban.wake.status_joiner").join(_parts) or t("gateway.kanban.wake.status_default")
                                    _synth = t(
                                        "gateway.kanban.wake.message",
                                        task_id=sub["task_id"],
                                        status=_status,
                                        title=_title,
                                        assignee=_assignee,
                                        board=board_slug,
                                    )
                                    from gateway.session import SessionSource
                                    from gateway.platforms.base import MessageEvent, MessageType
                                    # KNOWN LIMITATION (tracked follow-up): the
                                    # subscription row does not persist the
                                    # creator's chat_type, and it is not carried
                                    # on the session-context bridge, so we cannot
                                    # faithfully reconstruct the creator's real
                                    # session key here. build_session_key() keys
                                    # DMs (":dm:<chat_id>") on a wholly different
                                    # shape from group/thread, so any hardcoded
                                    # value mis-routes some creators. "group" is
                                    # the least-surprising default for the
                                    # dashboard/group flows this wake primarily
                                    # serves; DM-originated creators are handled
                                    # by the follow-up that stamps + persists
                                    # chat_type end-to-end. handle_message()
                                    # get_or_create_session's the target, so a
                                    # mismatch degrades to "wake lands in a fresh
                                    # group session" — never an exception.
                                    _source = SessionSource(
                                        platform=plat,
                                        chat_id=sub["chat_id"],
                                        chat_type="group",
                                        thread_id=sub.get("thread_id") or None,
                                        user_id=sub.get("user_id"),
                                        profile=sub_profile or None,
                                    )
                                    _synth_event = MessageEvent(
                                        text=_synth,
                                        message_type=MessageType.TEXT,
                                        source=_source,
                                        internal=True,
                                    )
                                    await adapter.handle_message(_synth_event)
                                    logger.info(
                                        "kanban notifier: woke agent for %s on %s/%s profile=%s events=%s",
                                        sub["task_id"], platform_str, sub["chat_id"], sub_profile or "default", _wake_kinds,
                                    )
                            except Exception as _wk_err:
                                # Best-effort: the notification itself already
                                # delivered and the cursor has advanced, so a
                                # broken wake path must not wedge the tick — but
                                # log at WARNING with a traceback rather than
                                # DEBUG so a persistently-failing wake is visible
                                # in normal logs instead of silently no-op'ing.
                                logger.warning(
                                    "kanban notifier: wakeup injection failed for %s: %s",
                                    sub["task_id"], _wk_err, exc_info=True,
                                )
                        if task_terminal:
                            await asyncio.to_thread(
                                self._kanban_unsub, sub, board_slug,
                            )

                # BUILD-506: deliver orphaned-tui-sub failure events claimed
                # this tick to the Telegram home channel. The cursor already
                # moved (sweep_orphaned_tui_sub claimed via the same CAS as
                # every other consumer) — never rewound here, matching the
                # existing MAX_SEND_FAILURES path above: once claimed, the
                # ledger row is the record of what happened, not a retry
                # queue. The subscription itself is never removed — the
                # desktop may come back, and its cursor will simply be past
                # what the sweep already delivered.
                for sweep in tui_sweeps:
                    sub = sweep["sub"]
                    task = sweep["task"]
                    events = sweep["events"]
                    board_slug = sweep.get("board")
                    all_delivered = True
                    for ev in events:
                        msg = render_kanban_event(
                            task_id=sub["task_id"], task=task, event=ev,
                            board_slug=board_slug,
                        )
                        if not msg:
                            continue
                        ok = await self._kanban_notify_home_fallback(msg)
                        all_delivered = all_delivered and ok
                    await asyncio.to_thread(
                        self._kanban_record_delivery,
                        sub, sweep.get("db_path") or board_slug or "",
                        events[0].id, events[-1].id,
                        "delivered" if all_delivered else "failed", board_slug,
                    )
                    logger.info(
                        "kanban notifier: swept orphaned tui sub %s (chat=%s) on "
                        "board %s — %d failure event(s) routed to Telegram home (ok=%s)",
                        sub["task_id"], sub.get("chat_id"), board_slug,
                        len(events), all_delivered,
                    )

                # Catch-all delivery of failure events on unsubscribed tasks
                # (collected above). Per-key monotonic backoff so a missing
                # home channel doesn't retry-warn every 5s tick; the ledger
                # row written on success is the permanent dedup.
                orphan_attempts: dict[str, float] = getattr(
                    self, "_kanban_orphan_attempts", {}
                )
                self._kanban_orphan_attempts = orphan_attempts
                for orphan in orphan_failures:
                    key = orphan["delivery_key"]
                    last = orphan_attempts.get(key, 0.0)
                    if time.monotonic() - last < ORPHAN_FAILURE_RETRY_SECONDS:
                        continue
                    orphan_attempts[key] = time.monotonic()
                    msg = render_kanban_event(
                        task_id=orphan["task_id"],
                        task=orphan.get("task"),
                        event=orphan["event"],
                        board_slug=orphan.get("board"),
                    )
                    if not msg:
                        continue
                    msg = (
                        "⚠️ Unrouted workflow failure — no chat is subscribed "
                        "to this task, delivering to home:\n" + msg
                    )
                    ok = await self._kanban_notify_home_fallback(msg)
                    if ok:
                        await asyncio.to_thread(
                            self._kanban_record_orphan_delivery, orphan,
                        )
                        orphan_attempts.pop(key, None)
                        logger.info(
                            "kanban notifier: routed unsubscribed %s event for "
                            "%s on board %s to Telegram home",
                            orphan["event"].kind, orphan["task_id"],
                            orphan.get("board"),
                        )

                # Human-gated blocks → the operator's Kanban console topic,
                # regardless of subscriptions. Same backoff/ledger discipline
                # as the home sweep; dedup is per event id so a re-block
                # after an unblock alerts again.
                hb_attempts: dict[str, float] = getattr(
                    self, "_kanban_human_block_attempts", {}
                )
                self._kanban_human_block_attempts = hb_attempts
                for item in human_blocked:
                    key = item["delivery_key"]
                    last = hb_attempts.get(key, 0.0)
                    if time.monotonic() - last < ORPHAN_FAILURE_RETRY_SECONDS:
                        continue
                    hb_attempts[key] = time.monotonic()
                    msg = render_kanban_event(
                        task_id=item["task_id"],
                        task=item.get("task"),
                        event=item["event"],
                        board_slug=item.get("board"),
                    )
                    if not msg:
                        continue
                    msg = (
                        "🧑‍🔧 Human input needed — reply here to unblock:\n"
                        + msg
                    )
                    ok = await self._kanban_notify_channel(
                        msg,
                        chat_id=human_block_target["chat_id"],
                        thread_id=human_block_target["thread_id"],
                    )
                    if ok:
                        await asyncio.to_thread(
                            self._kanban_record_human_block_delivery,
                            item, human_block_target,
                        )
                        hb_attempts.pop(key, None)
                        logger.info(
                            "kanban notifier: human-block alert for %s on "
                            "board %s delivered to Kanban console topic",
                            item["task_id"], item.get("board"),
                        )
            except Exception as exc:
                # exc_info: this tick has failed persistently before with only
                # str(exc) ("'int' object has no attribute 'lower'"), which is
                # undiagnosable without the frame — keep the traceback.
                logger.warning("kanban notifier tick failed: %s", exc, exc_info=True)
            # Sleep with cancellation checks.
            for _ in range(int(max(1, interval))):
                if not self._running:
                    return
                await asyncio.sleep(1)

    def _kanban_advance(
        self, sub: dict, cursor: int, board: Optional[str] = None,
    ) -> None:
        """Sync helper: advance a subscription's cursor. Runs in to_thread.

        ``board`` scopes the DB connection to the board that owns this
        subscription. Unsub cursors in one board can't touch another's.
        """
        from hermes_cli import kanban_db as _kb
        conn = _kb.connect(board=board)
        try:
            _kb.advance_notify_cursor(
                conn,
                task_id=sub["task_id"],
                platform=sub["platform"],
                chat_id=sub["chat_id"],
                thread_id=sub.get("thread_id") or "",
                new_cursor=cursor,
            )
        finally:
            conn.close()

    def _kanban_unsub(self, sub: dict, board: Optional[str] = None) -> None:
        from hermes_cli import kanban_db as _kb
        conn = _kb.connect(board=board)
        try:
            _kb.remove_notify_sub(
                conn,
                task_id=sub["task_id"],
                platform=sub["platform"],
                chat_id=sub["chat_id"],
                thread_id=sub.get("thread_id") or "",
            )
        finally:
            conn.close()

    def _kanban_rewind(
        self,
        sub: dict,
        claimed_cursor: int,
        old_cursor: int,
        board: Optional[str] = None,
    ) -> None:
        """Sync helper: undo a claimed notification cursor after send failure."""
        from hermes_cli import kanban_db as _kb
        conn = _kb.connect(board=board)
        try:
            _kb.rewind_notify_cursor(
                conn,
                task_id=sub["task_id"],
                platform=sub["platform"],
                chat_id=sub["chat_id"],
                thread_id=sub.get("thread_id") or "",
                claimed_cursor=claimed_cursor,
                old_cursor=old_cursor,
            )
        finally:
            conn.close()

    def _kanban_record_delivery(
        self,
        sub: dict,
        db_path: str,
        first_event_id: int,
        last_event_id: int,
        status: str,
        board: Optional[str] = None,
    ) -> None:
        """Sync helper (BUILD-503): append to the delivery ledger. Runs in
        to_thread. Best-effort — a ledger write failure must never wedge the
        notifier tick, so callers wrap this and the cursor advance stays the
        exactly-once authority regardless."""
        from hermes_cli import kanban_db as _kb
        conn = _kb.connect(board=board)
        try:
            delivery_key = _kb.notify_delivery_key(
                resolved_db_path=db_path,
                task_id=sub["task_id"],
                platform=sub["platform"],
                chat_id=sub["chat_id"],
                thread_id=sub.get("thread_id") or "",
                first_event_id=first_event_id,
                last_event_id=last_event_id,
            )
            _kb.record_notify_delivery(
                conn,
                delivery_key=delivery_key,
                task_id=sub["task_id"],
                platform=sub["platform"],
                chat_id=sub["chat_id"],
                thread_id=sub.get("thread_id") or "",
                first_event_id=first_event_id,
                last_event_id=last_event_id,
                status=status,
            )
        except Exception as exc:
            logger.debug(
                "kanban notifier: delivery ledger write failed for %s: %s",
                sub.get("task_id"), exc,
            )
        finally:
            conn.close()

    def _kanban_record_orphan_delivery(self, orphan: dict) -> None:
        """Sync helper: ledger row for a home-swept unsubscribed failure.

        The ``home-sweep/<task>/<kind>`` key is the dedup authority for the
        catch-all sweep (there is no subscription cursor for these tasks),
        so unlike ``_kanban_record_delivery`` this write is NOT best-effort
        garnish — a failure here just means the same event is re-delivered
        on a later tick, which the per-key in-memory backoff rate-limits.
        """
        from hermes_cli import kanban_db as _kb
        conn = _kb.connect(board=orphan.get("board"))
        try:
            _kb.record_notify_delivery(
                conn,
                delivery_key=orphan["delivery_key"],
                task_id=orphan["task_id"],
                platform="telegram",
                chat_id="home",
                thread_id="",
                first_event_id=orphan["event"].id,
                last_event_id=orphan["event"].id,
                status="delivered",
            )
        except Exception as exc:
            logger.warning(
                "kanban notifier: home-sweep ledger write failed for %s: %s",
                orphan.get("task_id"), exc,
            )
        finally:
            conn.close()

    async def _kanban_notify_channel(
        self, message: str, *, chat_id: str, thread_id: str = "",
    ) -> bool:
        """Deliver a notifier message to an explicit Telegram chat/topic.

        Returns True only on a confirmed send."""
        from gateway.config import Platform as _Platform
        adapter = self.adapters.get(_Platform.TELEGRAM)
        if adapter is None:
            return False
        metadata: dict[str, Any] = {}
        if thread_id:
            metadata["thread_id"] = thread_id
        try:
            result = await adapter.send(chat_id, message, metadata=metadata)
        except Exception as exc:
            logger.warning(
                "kanban notifier: Telegram send to %s/%s failed: %s",
                chat_id, thread_id or "-", exc,
            )
            return False
        if result is not None and getattr(result, "success", True) is False:
            return False
        return True

    async def _kanban_notify_home_fallback(self, message: str) -> bool:
        """Last-resort delivery of a failure notification to the Telegram home
        channel when the origin subscription is unreachable (BUILD-503).

        Reuses the same home-channel path as the dispatcher-stuck escalation
        (``config.get_home_channel`` / ``/sethome``) so a stranded worker
        failure still surfaces to the operator instead of vanishing when the
        originating chat is gone. Returns True only on a confirmed send."""
        from gateway.config import Platform as _Platform
        try:
            home = self.config.get_home_channel(_Platform.TELEGRAM)
        except Exception:
            home = None
        if home is None:
            logger.warning(
                "kanban notifier: origin unreachable and no Telegram home "
                "channel configured; failure notification dropped. Run "
                "/sethome in Telegram to enable the fallback."
            )
            return False
        ok = await self._kanban_notify_channel(
            message,
            chat_id=home.chat_id,
            thread_id=str(home.thread_id or ""),
        )
        if ok:
            logger.info(
                "kanban notifier: delivered failure notification to Telegram "
                "home (origin unreachable)",
            )
        return ok

    def _kanban_record_human_block_delivery(
        self, item: dict, target: dict,
    ) -> None:
        """Sync helper: durable ledger row for a delivered human-block alert."""
        from hermes_cli import kanban_db as _kb
        conn = _kb.connect(board=item.get("board"))
        try:
            _kb.record_notify_delivery(
                conn,
                delivery_key=item["delivery_key"],
                task_id=item["task_id"],
                platform="telegram",
                chat_id=target.get("chat_id") or "",
                thread_id=target.get("thread_id") or "",
                first_event_id=item["event"].id,
                last_event_id=item["event"].id,
                status="delivered",
            )
        except Exception as exc:
            logger.warning(
                "kanban notifier: human-block ledger write failed for %s: %s",
                item.get("task_id"), exc,
            )
        finally:
            conn.close()

    async def _deliver_kanban_artifacts(
        self,
        *,
        adapter,
        chat_id: str,
        metadata: dict,
        event_payload: Optional[dict],
        task,
    ) -> None:
        """Upload artifact files referenced by a completed kanban task.

        Workers passing ``kanban_complete(artifacts=[...])`` ship absolute
        file paths through the completion event so downstream humans get
        the deliverable as a native upload instead of a path printed in
        chat.

        Sources scanned, in priority order:
          1. ``event_payload['artifacts']`` (explicit list — preferred)
          2. ``event_payload['summary']`` (truncated first line)
          3. ``task.result`` (legacy fallback)

        Files are deduplicated, missing files are silently skipped (the
        path may have been mentioned for reference only), and delivery
        errors are logged but do not break the notifier loop.
        """
        from pathlib import Path as _Path

        candidates: list[str] = []
        seen: set[str] = set()

        def _add(path: str) -> None:
            if not path:
                return
            expanded = os.path.expanduser(path)
            if expanded in seen:
                return
            if not os.path.isfile(expanded):
                return
            seen.add(expanded)
            candidates.append(expanded)

        # 1. Explicit artifacts list in payload.
        if isinstance(event_payload, dict):
            raw = event_payload.get("artifacts")
            if isinstance(raw, (list, tuple)):
                for item in raw:
                    if isinstance(item, str):
                        _add(item)

            # 2. Paths embedded in the payload summary.
            summary = event_payload.get("summary")
            if isinstance(summary, str) and summary:
                paths, _ = adapter.extract_local_files(summary)
                for p in paths:
                    _add(p)

        # 3. Legacy: paths embedded in task.result.
        if task is not None and getattr(task, "result", None):
            result_text = str(task.result)
            paths, _ = adapter.extract_local_files(result_text)
            for p in paths:
                _add(p)

        if not candidates:
            return

        from gateway.platforms.base import BasePlatformAdapter
        candidates = BasePlatformAdapter.filter_local_delivery_paths(candidates)
        if not candidates:
            return

        _IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
        _VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".3gp"}

        from urllib.parse import quote as _quote

        # Partition images so they ride a single send_multiple_images call
        # on platforms that support batch image uploads (Signal/Slack RPCs).
        image_paths = [p for p in candidates if _Path(p).suffix.lower() in _IMAGE_EXTS]
        other_paths = [p for p in candidates if _Path(p).suffix.lower() not in _IMAGE_EXTS]

        if image_paths:
            try:
                batch = [(f"file://{_quote(p)}", "") for p in image_paths]
                await adapter.send_multiple_images(
                    chat_id=chat_id, images=batch, metadata=metadata,
                )
            except Exception as exc:
                logger.warning(
                    "kanban notifier: image batch upload failed: %s", exc,
                )

        for path in other_paths:
            ext = _Path(path).suffix.lower()
            try:
                if ext in _VIDEO_EXTS:
                    await adapter.send_video(
                        chat_id=chat_id, video_path=path, metadata=metadata,
                    )
                else:
                    await adapter.send_document(
                        chat_id=chat_id, file_path=path, metadata=metadata,
                    )
            except Exception as exc:
                logger.warning(
                    "kanban notifier: artifact upload (%s) failed: %s",
                    path, exc,
                )

    async def _kanban_dispatcher_stuck_alert(self, message: str) -> bool:
        """Send a dispatcher-stuck escalation via Telegram (BUILD-263).

        Reuses the existing home-channel notify path (``config.get_home_channel``
        — the same mechanism cron auto-delivery uses for ``deliver="telegram"``
        without an explicit chat id, set via ``/sethome``) rather than
        inventing a new admin-chat concept. Returns ``True`` only on a
        confirmed successful send so the caller's escalation state machine
        doesn't mark itself "alerted" on a silent no-op.
        """
        from gateway.config import Platform as _Platform

        try:
            home = self.config.get_home_channel(_Platform.TELEGRAM)
        except Exception:
            home = None
        if home is None:
            logger.warning(
                "kanban dispatcher stuck: cannot send Telegram escalation — "
                "no home channel configured. Run /sethome in Telegram, or "
                "set platforms.telegram.home_channel in config.yaml."
            )
            return False
        adapter = self.adapters.get(_Platform.TELEGRAM)
        if adapter is None:
            logger.warning(
                "kanban dispatcher stuck: cannot send Telegram escalation — "
                "Telegram adapter not connected."
            )
            return False
        metadata: dict[str, Any] = {}
        if home.thread_id:
            metadata["thread_id"] = home.thread_id
        try:
            result = await adapter.send(home.chat_id, message, metadata=metadata)
        except Exception as exc:
            logger.warning(
                "kanban dispatcher stuck: Telegram escalation send failed: %s", exc,
            )
            return False
        # adapter.send() catches provider errors and returns
        # SendResult(success=False) rather than raising (same caveat as the
        # restart-notification path in gateway/run.py) — check it explicitly.
        if result is not None and getattr(result, "success", True) is False:
            logger.warning(
                "kanban dispatcher stuck: Telegram escalation was not "
                "delivered: %s", getattr(result, "error", result),
            )
            return False
        logger.info(
            "kanban dispatcher stuck: sent Telegram escalation to %s", home.chat_id,
        )
        return True

    async def _kanban_dispatcher_watcher(self) -> None:
        """Embedded kanban dispatcher — one tick every `dispatch_interval_seconds`.

        Gated by `kanban.dispatch_in_gateway` in config.yaml (default True).
        When true, the gateway hosts the single dispatcher for this profile:
        no separate `hermes kanban daemon` process needed. When false, the
        loop exits immediately and an external daemon is expected.

        Each tick calls :func:`kanban_db.dispatch_once` inside
        ``asyncio.to_thread`` so the SQLite WAL lock never blocks the
        event loop. Failures in one tick don't stop subsequent ticks —
        same pattern as `_kanban_notifier_watcher`.

        Shutdown: the loop checks ``self._running`` between ticks; gateway
        stop() flips it to False and cancels pending tasks, and the
        in-flight ``to_thread`` returns on its own after the current
        ``dispatch_once`` call finishes (typically <1ms on an idle board).
        """
        # Read config once at boot. If the user flips the flag later, they
        # restart the gateway; same pattern as every other background
        # watcher here. Honours HERMES_KANBAN_DISPATCH_IN_GATEWAY env var
        # as an escape hatch (false-y value disables without editing YAML).
        try:
            from hermes_cli.config import load_config as _load_config
        except Exception:
            logger.warning("kanban dispatcher: config loader unavailable; disabled")
            return
        env_override = os.environ.get("HERMES_KANBAN_DISPATCH_IN_GATEWAY", "").strip().lower()
        if env_override in {"0", "false", "no", "off"}:
            logger.info("kanban dispatcher: disabled via HERMES_KANBAN_DISPATCH_IN_GATEWAY env")
            return

        try:
            cfg = _load_config()
        except Exception as exc:
            logger.warning("kanban dispatcher: cannot load config (%s); disabled", exc)
            return
        kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
        if not kanban_cfg.get("dispatch_in_gateway", True):
            logger.info(
                "kanban dispatcher: disabled via config kanban.dispatch_in_gateway=false"
            )
            return

        try:
            from hermes_cli import kanban_db as _kb
        except Exception:
            logger.warning("kanban dispatcher: kanban_db not importable; dispatcher disabled")
            return

        # Single-dispatcher backstop. dispatch_in_gateway defaults to true, so a
        # new profile gateway (or a same-profile restart race) can silently
        # start a second dispatcher; concurrent dispatchers double reclaim
        # frequency, double claim-attempt events, and — with
        # wal_autocheckpoint=0 — concurrent manual WAL checkpoints can corrupt
        # index pages. The lock lives at the machine-global kanban root
        # (shared across profiles by design), so it serialises ALL gateways.
        self._kanban_dispatcher_lock_handle = None
        _lock_path = dispatcher_singleton_lock_path()
        try:
            _lock_handle, _lock_state = _acquire_singleton_lock(_lock_path)
        except Exception as exc:
            logger.error(
                "kanban dispatcher: refusing to start embedded dispatcher — "
                "singleton lock acquisition failed at %s (%s); "
                "config kanban.dispatch_in_gateway=true requires that lock.",
                _lock_path,
                exc,
            )
            return
        if _lock_state == "contended":
            logger.info(
                "kanban dispatcher: another gateway already holds the dispatcher "
                "lock (%s); this gateway will NOT dispatch.", _lock_path,
            )
            return
        if _lock_state == "held":
            self._kanban_dispatcher_lock_handle = _lock_handle  # hold for process lifetime
            logger.info("kanban dispatcher: holding singleton dispatcher lock (%s)", _lock_path)
        else:
            logger.error(
                "kanban dispatcher: refusing to start embedded dispatcher — "
                "singleton lock unavailable at %s; config "
                "kanban.dispatch_in_gateway=true cannot be honored safely.",
                _lock_path,
            )
            return

        try:
            interval = float(kanban_cfg.get("dispatch_interval_seconds", 60) or 60)
        except (ValueError, TypeError):
            logger.warning(
                "kanban dispatcher: invalid dispatch_interval_seconds=%r, using default 60",
                kanban_cfg.get("dispatch_interval_seconds"),
            )
            interval = 60.0
        interval = max(interval, 1.0)  # sanity floor — tighter than this is a footgun

        # Read max_spawn config to limit concurrent kanban tasks
        max_spawn = kanban_cfg.get("max_spawn", None)
        if max_spawn is not None:
            logger.info(f"kanban dispatcher: max_spawn={max_spawn}")

        # Cap the number of simultaneously running tasks so slow workers
        # (local LLMs, resource-constrained hosts) don't pile up and time
        # out. When set, the dispatcher skips spawning when the board
        # already has this many tasks in 'running' status.
        raw_max_in_progress = kanban_cfg.get("max_in_progress", None)
        max_in_progress = None
        if raw_max_in_progress is not None:
            try:
                max_in_progress = int(raw_max_in_progress)
            except (TypeError, ValueError):
                logger.warning(
                    "kanban dispatcher: invalid kanban.max_in_progress=%r; ignoring",
                    raw_max_in_progress,
                )
                max_in_progress = None
            else:
                if max_in_progress < 1:
                    logger.warning(
                        "kanban dispatcher: kanban.max_in_progress=%r is below 1; ignoring",
                        raw_max_in_progress,
                    )
                    max_in_progress = None
                else:
                    logger.info(f"kanban dispatcher: max_in_progress={max_in_progress}")

        raw_failure_limit = kanban_cfg.get("failure_limit", _kb.DEFAULT_FAILURE_LIMIT)
        try:
            failure_limit = int(raw_failure_limit)
        except (TypeError, ValueError):
            logger.warning(
                "kanban dispatcher: invalid kanban.failure_limit=%r; using default %d",
                raw_failure_limit,
                _kb.DEFAULT_FAILURE_LIMIT,
            )
            failure_limit = _kb.DEFAULT_FAILURE_LIMIT
        if failure_limit < 1:
            logger.warning(
                "kanban dispatcher: kanban.failure_limit=%r is below 1; using default %d",
                raw_failure_limit,
                _kb.DEFAULT_FAILURE_LIMIT,
            )
            failure_limit = _kb.DEFAULT_FAILURE_LIMIT

        # Read kanban.failure_signature_threshold — BUILD-261 release/
        # remediation circuit breaker. How many consecutive IDENTICAL
        # failure signatures (see kanban_db.normalize_failure_signature)
        # must be seen before a respawn is refused and the task is
        # blocked for human review instead. Distinct from failure_limit
        # above: that counts ANY non-success outcome regardless of
        # content and resets on a successful completion; this counts
        # repeated identical failure *content* across attempts, so a
        # saga that keeps "succeeding" but reproducing the same
        # underlying failure still gets caught.
        raw_sig_threshold = kanban_cfg.get(
            "failure_signature_threshold",
            _kb.DEFAULT_FAILURE_SIGNATURE_REPEAT_THRESHOLD,
        )
        try:
            signature_repeat_threshold = int(raw_sig_threshold)
        except (TypeError, ValueError):
            logger.warning(
                "kanban dispatcher: invalid "
                "kanban.failure_signature_threshold=%r; using default %d",
                raw_sig_threshold,
                _kb.DEFAULT_FAILURE_SIGNATURE_REPEAT_THRESHOLD,
            )
            signature_repeat_threshold = (
                _kb.DEFAULT_FAILURE_SIGNATURE_REPEAT_THRESHOLD
            )
        if signature_repeat_threshold < 2:
            logger.warning(
                "kanban dispatcher: kanban.failure_signature_threshold=%r "
                "is below 2; using default %d",
                raw_sig_threshold,
                _kb.DEFAULT_FAILURE_SIGNATURE_REPEAT_THRESHOLD,
            )
            signature_repeat_threshold = (
                _kb.DEFAULT_FAILURE_SIGNATURE_REPEAT_THRESHOLD
            )

        # Read stale_timeout_seconds — 0 disables stale detection.
        raw_stale = kanban_cfg.get("dispatch_stale_timeout_seconds", 0)
        try:
            stale_timeout_seconds = int(raw_stale or 0)
        except (TypeError, ValueError):
            logger.warning(
                "kanban dispatcher: invalid kanban.dispatch_stale_timeout_seconds=%r; "
                "disabling stale detection",
                raw_stale,
            )
            stale_timeout_seconds = 0

        # Read kanban.default_assignee — fallback profile for tasks
        # created without an explicit assignee (e.g. via the dashboard).
        # When set, the dispatcher applies it to unassigned ready tasks
        # instead of skipping them indefinitely (#27145). Empty string
        # (the schema default) means "no fallback, keep skipping" —
        # backward-compatible with existing installs.
        default_assignee = (kanban_cfg.get("default_assignee") or "").strip() or None
        if default_assignee:
            logger.info(
                "kanban dispatcher: default_assignee=%r (unassigned ready tasks "
                "will route to this profile)",
                default_assignee,
            )

        # Read kanban.max_in_progress_per_profile — per-profile concurrency
        # cap (#21582). When set, no single profile gets more than N
        # workers running at once, even if the global max_in_progress
        # would allow it. Prevents one profile's local model / API quota
        # / browser pool from being overwhelmed by a fan-out.
        raw_per_profile = kanban_cfg.get("max_in_progress_per_profile", None)
        max_in_progress_per_profile = None
        if raw_per_profile is not None:
            try:
                max_in_progress_per_profile = int(raw_per_profile)
            except (TypeError, ValueError):
                logger.warning(
                    "kanban dispatcher: invalid kanban.max_in_progress_per_profile=%r; ignoring",
                    raw_per_profile,
                )
                max_in_progress_per_profile = None
            else:
                if max_in_progress_per_profile < 1:
                    logger.warning(
                        "kanban dispatcher: kanban.max_in_progress_per_profile=%r is below 1; ignoring",
                        raw_per_profile,
                    )
                    max_in_progress_per_profile = None
                else:
                    logger.info(
                        "kanban dispatcher: max_in_progress_per_profile=%d",
                        max_in_progress_per_profile,
                    )

        # Initial delay so the gateway finishes wiring adapters before the
        # dispatcher spawns workers (those workers may hit gateway notify
        # subscriptions etc.). Matches the notifier watcher's delay.
        await asyncio.sleep(5)

        # Health telemetry mirrored from `_cmd_daemon`: warn when ready
        # queue is non-empty but spawns are 0 for N consecutive ticks —
        # usually means broken PATH, missing venv, or credential loss.
        HEALTH_WINDOW = 6
        bad_ticks = 0
        health_log_cooldowns = _kb.DispatchHealthLogCooldowns()
        # Per-cause DispatchResults accumulated across the CURRENT bad-tick
        # streak (BUILD-263) — reset the moment the streak clears — so the
        # "stuck" warning/escalation can say *why* nothing spawned
        # (`_kb.summarize_dispatch_causes`) instead of just the tick count.
        stuck_tick_results: list = []
        # Escalation (BUILD-263): after the incident where a broken profile
        # produced silent "stuck" log lines for ~6 hours climbing past 250
        # ticks with nobody paged, escalate to a Telegram alert once the
        # streak is long enough that it's clearly not a transient blip.
        # Re-read once at boot (same cadence as the other dispatcher knobs
        # below, e.g. failure_limit / max_spawn) — this is an ops-tuning
        # value, not a runaway-fanout kill switch like auto_decompose, so it
        # doesn't need the live per-tick re-read.
        _escalate_after_ticks = _kb._positive_int(
            kanban_cfg.get("dispatch_stuck_escalate_after_ticks"), 12,
        )
        _escalate_realert_seconds = _kb._positive_int(
            kanban_cfg.get("dispatch_stuck_realert_seconds"), 3600,
        )
        stuck_escalation = DispatcherStuckEscalationState(
            escalate_after_ticks=_escalate_after_ticks,
            realert_seconds=_escalate_realert_seconds,
        )
        # Avoid hot-looping corrupt-looking board DBs, but do not suppress
        # same-fingerprint retries forever: transient WAL/open races can
        # surface as "database disk image is malformed" for one tick.
        CORRUPT_BOARD_RETRY_AFTER_SECONDS = 300
        disabled_corrupt_boards: dict[
            str, tuple[tuple[str, int | None, int | None], float]
        ] = {}
        # One recovery attempt and one operator alert per distinct corrupt
        # fingerprint; re-detections of the same fingerprint log at INFO so
        # a still-corrupt board doesn't ERROR-spam every quarantine expiry
        # (the 2026-07-18 vault-v2 incident logged the identical ERROR every
        # 5 minutes for 16 hours and never told the operator).
        corrupt_recovery_attempted: set = set()
        corrupt_alerted: set = set()
        corrupt_alerts: list[str] = []

        def _board_db_fingerprint(slug: str) -> tuple[str, int | None, int | None]:
            path = _kb.kanban_db_path(slug)
            try:
                resolved = str(path.expanduser().resolve())
            except Exception:
                resolved = str(path)
            try:
                stat = path.stat()
            except OSError:
                return (resolved, None, None)
            return (resolved, stat.st_mtime_ns, stat.st_size)

        def _is_corrupt_board_db_error(exc: Exception) -> bool:
            corrupt_guard_error = getattr(_kb, "KanbanDbCorruptError", None)
            if corrupt_guard_error is not None and isinstance(exc, corrupt_guard_error):
                return True
            if not isinstance(exc, sqlite3.DatabaseError):
                return False
            msg = str(exc).lower()
            return (
                "file is not a database" in msg
                or "database disk image is malformed" in msg
            )

        def _handle_corrupt_board(
            slug: str,
            fingerprint: "tuple[str, int | None, int | None]",
            exc: Exception,
        ) -> None:
            """Corrupt board DB: attempt one in-place recovery, alert the
            operator once, and quarantine without ERROR-spamming.

            Runs in the dispatcher tick thread. Alerts are queued on
            ``corrupt_alerts`` and flushed to the Telegram home channel by
            the async loop after the tick returns.
            """
            if fingerprint not in corrupt_recovery_attempted:
                corrupt_recovery_attempted.add(fingerprint)
                # BUILD-531 forensics: capture the fd table BEFORE recovery
                # swaps files around, while the stray-writing fd (if any)
                # may still alias the corrupt image.
                try:
                    db_path = Path(_kb.kanban_db_path(slug))
                    fd_note = _snapshot_process_fds(
                        db_path,
                        db_path.with_name(
                            db_path.name
                            + f".fdmap-{time.strftime('%Y%m%d-%H%M%S')}.txt"
                        ),
                    )
                    if fd_note:
                        logger.warning(
                            "kanban dispatcher: board %s corruption fd "
                            "snapshot: %s", slug, fd_note,
                        )
                except Exception:
                    logger.debug("fd snapshot failed", exc_info=True)
                ok, detail = _attempt_board_db_recovery(_kb, slug)
                if ok:
                    logger.warning(
                        "kanban dispatcher: board %s database was corrupt (%s); "
                        "auto-recovered in place. %s",
                        slug, exc, detail,
                    )
                    corrupt_alerts.append(
                        f"🛠 Kanban board `{slug}` database was corrupt and has "
                        f"been auto-recovered; dispatch resumes next tick. "
                        f"{detail}."
                    )
                    # Fingerprint changed on swap, so the normal
                    # changed-fingerprint path re-enables dispatch.
                    disabled_corrupt_boards.pop(slug, None)
                    return
                logger.error(
                    "kanban dispatcher: board %s database %s is not a valid SQLite "
                    "database (%s); auto-recovery failed (%s); pausing dispatch until "
                    "the file changes or the quarantine timer expires. Restore the "
                    "file, then run `hermes kanban init` if you need a fresh board.",
                    slug, fingerprint[0], exc, detail,
                )
            else:
                logger.info(
                    "kanban dispatcher: board %s still corrupt (fingerprint "
                    "unchanged); dispatch remains paused.", slug,
                )
            disabled_corrupt_boards[slug] = (fingerprint, time.monotonic())
            if fingerprint not in corrupt_alerted:
                corrupt_alerted.add(fingerprint)
                corrupt_alerts.append(
                    f"🚨 Kanban board `{slug}` database is corrupt "
                    f"({fingerprint[0]}) and auto-recovery failed — dispatch "
                    f"for this board is PAUSED. Tasks on it will not run until "
                    f"the file is restored (`hermes kanban init` for a fresh "
                    f"board)."
                )

        def _tick_once_for_board(slug: str) -> "Optional[object]":
            """Run one dispatch_once for a specific board.

            Runs in a worker thread via `asyncio.to_thread`. `board=slug`
            is passed through `dispatch_once` so `resolve_workspace` and
            `_default_spawn` see the right paths. The per-board DB is
            opened explicitly so concurrent boards never share a
            connection handle or accidentally claim across each other.
            """
            conn = None
            fingerprint = _board_db_fingerprint(slug)
            disabled_entry = disabled_corrupt_boards.get(slug)
            if disabled_entry is not None:
                disabled_fingerprint, disabled_at = disabled_entry
                age = time.monotonic() - disabled_at
                if (
                    disabled_fingerprint == fingerprint
                    and age < CORRUPT_BOARD_RETRY_AFTER_SECONDS
                ):
                    return None
                if disabled_fingerprint == fingerprint:
                    logger.info(
                        "kanban dispatcher: board %s database fingerprint unchanged "
                        "after %.0fs quarantine; retrying dispatch",
                        slug,
                        age,
                    )
                else:
                    logger.info(
                        "kanban dispatcher: board %s database changed; retrying dispatch",
                        slug,
                    )
                disabled_corrupt_boards.pop(slug, None)
            try:
                conn = _kb.connect(board=slug)
                # `connect()` runs the schema + idempotent migration on
                # first open per process; the previous explicit
                # `init_db()` call here busted the per-process cache and
                # re-ran the migration on a second connection, racing
                # the first. See the matching comment in
                # `_kanban_notifier_watcher` and issue #21378.
                return _kb.dispatch_once(
                    conn,
                    board=slug,
                    max_spawn=max_spawn,
                    max_in_progress=max_in_progress,
                    failure_limit=failure_limit,
                    stale_timeout_seconds=stale_timeout_seconds,
                    default_assignee=default_assignee,
                    max_in_progress_per_profile=max_in_progress_per_profile,
                    signature_repeat_threshold=signature_repeat_threshold,
                )
            except Exception as exc:
                if _is_corrupt_board_db_error(exc):
                    _handle_corrupt_board(slug, fingerprint, exc)
                    return None
                logger.exception("kanban dispatcher: tick failed on board %s", slug)
                return None
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass

        def _tick_once() -> "list[tuple[str, Optional[object]]]":
            """Run one dispatch_once per board. Returns (slug, result) pairs.

            Enumerating boards on every tick keeps the dispatcher honest
            when users create a new board mid-run: no restart required,
            the next tick picks it up automatically.
            """
            try:
                boards = _kb.list_boards(include_archived=False)
            except Exception:
                boards = [_kb.read_board_metadata(_kb.DEFAULT_BOARD)]
            out: list[tuple[str, "Optional[object]"]] = []
            for b in boards:
                slug = b.get("slug") or _kb.DEFAULT_BOARD
                out.append((slug, _tick_once_for_board(slug)))
            return out

        def _ready_nonempty() -> bool:
            """Cheap probe: is there at least one ready+assigned+unclaimed
            task on ANY board whose assignee maps to a real Hermes profile
            (i.e. one the dispatcher would actually spawn for)?

            Tasks assigned to control-plane lanes (e.g. ``orion-cc``,
            ``orion-research``) are pulled by terminals via
            ``claim_task`` directly and never spawnable, so a queue full
            of those is "correctly idle", not "stuck". Filtering them out
            here keeps the stuck-warn fire only on real failures (broken
            PATH, missing venv, credential loss for a real Hermes profile).
            """
            try:
                boards = _kb.list_boards(include_archived=False)
            except Exception:
                boards = [_kb.read_board_metadata(_kb.DEFAULT_BOARD)]
            for b in boards:
                slug = b.get("slug") or _kb.DEFAULT_BOARD
                conn = None
                try:
                    conn = _kb.connect(board=slug)
                    if _kb.has_spawnable_ready(conn):
                        return True
                    if _kb.has_spawnable_review(conn):
                        return True
                except Exception:
                    continue
                finally:
                    if conn is not None:
                        try:
                            conn.close()
                        except Exception:
                            pass
            return False

        # Auto-decompose: turn fresh triage tasks into ready workgraphs
        # before the dispatcher fans out workers. Gated by
        # ``kanban.auto_decompose`` (default True). Capped by
        # ``kanban.auto_decompose_per_tick`` (default 3) so a bulk-load
        # of triage tasks doesn't burst-spend the aux LLM in one tick;
        # remainder defers to subsequent ticks.
        #
        # The flag is re-read from config EVERY tick (#49638) rather than
        # captured once at boot. Auto-decompose is a safety toggle: a user who
        # sees it fan out and run tasks they didn't intend reaches for
        # ``kanban.auto_decompose: false`` to STOP it — and that must take
        # effect on the next tick, not require a gateway restart. (Reported:
        # auto-decompose created and launched destructive tasks while the user
        # was still typing the task description, and the flag "couldn't be
        # disabled" because the gateway had captured its boot-time value.)
        def _read_auto_decompose_settings() -> tuple[bool, int]:
            """Re-resolve (enabled, per_tick) from current config each tick."""
            return _resolve_auto_decompose_settings(_load_config)

        def _auto_decompose_tick(auto_decompose_per_tick: int) -> int:
            """Run the auto-decomposer for up to N triage tasks across all
            boards. Returns the number of triage tasks that were
            successfully decomposed or specified this tick.
            """
            try:
                from hermes_cli import kanban_decompose as _decomp
            except Exception as exc:  # pragma: no cover
                logger.warning(
                    "kanban auto-decompose: import failed (%s); skipping", exc,
                )
                return 0
            try:
                boards = _kb.list_boards(include_archived=False)
            except Exception:
                boards = [_kb.read_board_metadata(_kb.DEFAULT_BOARD)]
            attempted = 0
            successes = 0
            for b in boards:
                slug = b.get("slug") or _kb.DEFAULT_BOARD
                if attempted >= auto_decompose_per_tick:
                    break
                # Pin this board for the duration of the call — same
                # pattern as the dashboard specify endpoint. The
                # decomposer module connects with no board kwarg and
                # relies on the env var.
                prev_env = os.environ.get("HERMES_KANBAN_BOARD")
                try:
                    os.environ["HERMES_KANBAN_BOARD"] = slug
                    try:
                        triage_ids = _decomp.list_triage_ids()
                    except Exception as exc:
                        logger.debug(
                            "kanban auto-decompose: list_triage_ids failed on board %s (%s)",
                            slug, exc,
                        )
                        triage_ids = []
                    for tid in triage_ids:
                        if attempted >= auto_decompose_per_tick:
                            break
                        attempted += 1
                        try:
                            outcome = _decomp.decompose_task(
                                tid, author="auto-decomposer",
                            )
                        except Exception:
                            logger.exception(
                                "kanban auto-decompose: decompose_task crashed on %s",
                                tid,
                            )
                            continue
                        if outcome.ok:
                            successes += 1
                            if outcome.fanout and outcome.child_ids:
                                logger.info(
                                    "kanban auto-decompose [%s]: %s → %d children",
                                    slug, tid, len(outcome.child_ids),
                                )
                            else:
                                logger.info(
                                    "kanban auto-decompose [%s]: %s → single task (no fanout)",
                                    slug, tid,
                                )
                        else:
                            # Common no-op reasons (no aux client configured) shouldn't
                            # spam logs every tick. Log at debug.
                            logger.debug(
                                "kanban auto-decompose [%s]: %s skipped: %s",
                                slug, tid, outcome.reason,
                            )
                finally:
                    if prev_env is None:
                        os.environ.pop("HERMES_KANBAN_BOARD", None)
                    else:
                        os.environ["HERMES_KANBAN_BOARD"] = prev_env
            return successes

        logger.info(
            "kanban dispatcher: embedded in gateway (interval=%.1fs)", interval
        )
        while self._running:
            try:
                # Reap zombie children before per-board work so a board DB
                # failure cannot block cleanup of unrelated workers.
                pids = await asyncio.to_thread(_kb.reap_worker_zombies)
                if pids:
                    logger.info(
                        "kanban dispatcher: reaped %d zombie worker(s), pids=%s",
                        len(pids),
                        pids,
                    )
            except Exception:
                logger.exception("kanban dispatcher: zombie reaper failed")

            try:
                # Re-read the auto-decompose toggle live each tick so a user
                # flipping kanban.auto_decompose=false to STOP runaway fan-out
                # takes effect on the next tick, not on gateway restart (#49638).
                _ad_enabled, _ad_per_tick = _read_auto_decompose_settings()
                if _ad_enabled:
                    await asyncio.to_thread(_auto_decompose_tick, _ad_per_tick)
                results = await asyncio.to_thread(_tick_once)
                # Flush corrupt-board operator alerts queued by the tick
                # thread. Send failures are dropped (the alert re-queues
                # only on a new fingerprint) — alerting must never wedge
                # dispatch.
                while corrupt_alerts:
                    alert = corrupt_alerts.pop(0)
                    try:
                        await self._kanban_notify_home_fallback(alert)
                    except Exception as alert_exc:
                        logger.warning(
                            "kanban dispatcher: corrupt-board alert send "
                            "failed: %s", alert_exc,
                        )
                any_spawned = False
                for slug, res in (results or []):
                    if res is not None and getattr(res, "spawned", None):
                        any_spawned = True
                        # Quiet by default — only log when something actually
                        # happened, so an idle gateway stays silent.
                        logger.info(
                            "kanban dispatcher [%s]: spawned=%d reclaimed=%d "
                            "crashed=%d timed_out=%d promoted=%d auto_blocked=%d",
                            slug,
                            len(res.spawned),
                            res.reclaimed,
                            len(res.crashed) if hasattr(res.crashed, "__len__") else 0,
                            len(res.timed_out) if hasattr(res.timed_out, "__len__") else 0,
                            res.promoted,
                            len(res.auto_blocked) if hasattr(res.auto_blocked, "__len__") else 0,
                        )
                # Health telemetry (aggregate across boards)
                ready_pending = await asyncio.to_thread(_ready_nonempty)
                if ready_pending and not any_spawned:
                    bad_ticks += 1
                    stuck_tick_results.extend(
                        res for _slug, res in (results or []) if res is not None
                    )
                    # Bound growth for an extreme streak (the 2026-07-08
                    # incident ran 250+ ticks unnoticed) — the aggregated
                    # cause breakdown only needs a representative recent
                    # window, not the full unbounded history.
                    _STUCK_RESULTS_MAX = 500
                    if len(stuck_tick_results) > _STUCK_RESULTS_MAX:
                        del stuck_tick_results[: len(stuck_tick_results) - _STUCK_RESULTS_MAX]
                else:
                    if bad_ticks > 0:
                        # Recovered: a worker spawned (or the ready queue
                        # drained) after a stuck streak. Clear the
                        # accumulated cause breakdown AND the escalation
                        # timer (BUILD-263) — the NEXT stuck streak must
                        # alert at threshold again, not stay silenced by
                        # this streak's re-alert cadence.
                        stuck_tick_results.clear()
                        stuck_escalation.mark_recovered()
                    bad_ticks = 0
                if bad_ticks >= HEALTH_WINDOW:
                    now = int(time.time())
                    capacity_only, causes = classify_stuck_streak(stuck_tick_results)
                    causes_suffix = f" causes: {causes}" if causes else ""
                    if capacity_only:
                        # Cause counts accumulate across the streak window, so
                        # a per-task "N deferred" figure would inflate with
                        # streak length — the causes breakdown carries the
                        # cumulative counts, same convention as the WARN path.
                        if health_log_cooldowns.should_emit(
                            capacity_only=True, now=now,
                        ):
                            logger.info(
                                "kanban dispatcher at capacity: ready tasks "
                                "deferred by concurrency caps for %d consecutive "
                                "ticks (causes: %s) — healthy; drains when a "
                                "running worker finishes.",
                                bad_ticks, causes,
                            )
                    else:
                        if health_log_cooldowns.should_emit(
                            capacity_only=False, now=now,
                        ):
                            logger.warning(
                                "kanban dispatcher stuck: ready queue non-empty for "
                                "%d consecutive ticks but 0 workers spawned. Check "
                                "profile health (venv, PATH, credentials) and "
                                "`hermes kanban list --status ready`.%s",
                                bad_ticks, causes_suffix,
                            )
                        # Escalation (BUILD-263): once the streak is long enough
                        # that it's clearly not a transient blip, page via
                        # Telegram — logs alone went unread for ~6 hours in the
                        # 2026-07-08 incident. Re-alerts at most hourly while
                        # still stuck; cleared above the moment a worker spawns.
                        if stuck_escalation.should_alert(bad_ticks, now):
                            alert_msg = (
                                "⚠️ kanban dispatcher stuck: ready queue non-empty "
                                f"for {bad_ticks} consecutive ticks but 0 workers "
                                f"spawned.{causes_suffix} Check profile health "
                                "(venv, PATH, credentials) and "
                                "`hermes kanban list --status ready`."
                            )
                            try:
                                sent = await self._kanban_dispatcher_stuck_alert(alert_msg)
                            except Exception:
                                logger.exception(
                                    "kanban dispatcher: stuck-escalation alert send failed"
                                )
                                sent = False
                            if sent:
                                stuck_escalation.mark_alerted(now)
            except asyncio.CancelledError:
                logger.debug("kanban dispatcher: cancelled")
                _release_singleton_lock(self._kanban_dispatcher_lock_handle)
                self._kanban_dispatcher_lock_handle = None
                raise
            except Exception:
                logger.exception("kanban dispatcher: unexpected watcher error")

            # Sleep in 1s slices so shutdown is snappy — otherwise a stop()
            # waits up to `interval` seconds for the current sleep to finish.
            slept = 0.0
            while slept < interval and self._running:
                await asyncio.sleep(min(1.0, interval - slept))
                slept += 1.0

        _release_singleton_lock(self._kanban_dispatcher_lock_handle)
        self._kanban_dispatcher_lock_handle = None
