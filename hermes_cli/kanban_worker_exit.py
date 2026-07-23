"""Durable per-pid worker exit sidecar (BUILD-735).

A dispatcher worker is spawned ``start_new_session=True`` with its ``Popen``
handle abandoned (``kanban_db._default_spawn``), so **init** reaps it — the
dispatcher's ``reap_worker_zombies`` (``waitpid(-1)``) never sees the exit
status, ``_classify_worker_exit`` falls back to ``("unknown", None)``, and
``detect_crashed_workers`` miscounts the exit as a crash (``cf++`` → a card
self-arrests at ``cf=2`` even when nothing was wrong). That is the root of the
2026-07-22 architect whack-a-mole.

The worker-side no-failure self-defer (a2a8f6305 / BUILD-734) only covers the
reasons a worker can *pre-classify* before exiting cleanly. This module closes
the residual: the worker records its **own** terminal disposition to
``<sidecar_dir>/<pid>`` right before it dies, so the dispatcher can classify a
session-detached exit concretely instead of guessing. The record is a plain
file → it survives a dispatcher restart between the worker's exit and the next
reconcile tick (the in-memory ``_recent_worker_exits`` registry does not).

Capture surface:

* clean return / ``sys.exit(n)`` / ``raise SystemExit(n)`` → ``exit:<code>``
* uncaught exception (interpreter exits 1)                  → ``exit:1``
* catchable signal (SIGTERM / SIGINT / SIGHUP)              → ``signal:<n>``
* ``SIGKILL`` / OOM / power loss (uncatchable)              → no file → the
  dispatcher's existing ``unknown`` → crash-counter fallback (correct: the
  exit is genuinely lost).
"""

from __future__ import annotations

import atexit
import contextlib
import os
import signal
import sys
from pathlib import Path

# Set by ``_default_spawn`` for every dispatcher worker. Absent for every other
# ``hermes`` invocation, which is how the recorder no-ops outside a worker.
SIDECAR_DIR_ENV = "HERMES_KANBAN_EXIT_SIDECAR_DIR"

# Catchable termination signals worth recording. SIGHUP is POSIX-only; the whole
# block is skipped on Windows (no session-detached worker model there).
_CAUGHT_SIGNALS = ("SIGTERM", "SIGINT", "SIGHUP")


def _sidecar_path(sidecar_dir: str) -> Path:
    return Path(sidecar_dir) / str(os.getpid())


def _atomic_write(sidecar_dir: str, payload: str) -> None:
    """Write ``payload`` to ``<sidecar_dir>/<pid>`` atomically. Best-effort."""
    try:
        path = _sidecar_path(sidecar_dir)
        # Unique-ish temp within the same dir so os.replace() is atomic. The pid
        # is stable within this process, so a single temp name is fine.
        tmp = path.with_name(f"{path.name}.tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        # A sidecar we cannot write just degrades to the unknown → crash
        # fallback; never let exit bookkeeping crash the worker's own exit.
        pass


@contextlib.contextmanager
def worker_exit_recorder():
    """Record this worker's terminal disposition on the way out.

    No-op unless ``SIDECAR_DIR_ENV`` is set (i.e. this is a dispatcher worker).
    Installs catchable-signal handlers and an ``atexit`` fallback, and captures
    the ``SystemExit`` / exception path around the wrapped body. Idempotent:
    the first disposition observed wins, so a signal handler that re-raises does
    not get overwritten by the subsequent ``atexit`` ``exit:0``.
    """
    sidecar_dir = (os.environ.get(SIDECAR_DIR_ENV) or "").strip()
    if not sidecar_dir:
        yield
        return

    state = {"recorded": False}

    def record(payload: str) -> None:
        if state["recorded"]:
            return
        state["recorded"] = True
        _atomic_write(sidecar_dir, payload)

    # Normal interpreter shutdown (clean return, or sys.exit(0) whose code we
    # also see below) leaves no exception for the body to catch — atexit is the
    # only hook that fires. Latched, so an already-recorded disposition wins.
    atexit.register(lambda: record("exit:0"))

    if os.name != "nt":
        prev_handlers: "dict[int, object]" = {}

        def handle_signal(signum, _frame):
            record(f"signal:{signum}")
            # Restore the prior disposition and re-raise so the real exit status
            # reflects the signal (128 + signum), not a swallowed handler.
            try:
                signal.signal(signum, prev_handlers.get(signum, signal.SIG_DFL))
            except (ValueError, OSError):
                pass
            os.kill(os.getpid(), signum)

        for name in _CAUGHT_SIGNALS:
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                prev_handlers[int(sig)] = signal.getsignal(sig)
                signal.signal(sig, handle_signal)
            except (ValueError, OSError):
                # Not the main thread, or the platform rejects it — degrade.
                pass

    try:
        yield
    except SystemExit as exc:
        code = exc.code
        if code is None:
            resolved = 0
        elif isinstance(code, int):
            resolved = code
        else:
            # A string/object exit message → interpreter exits 1.
            resolved = 1
        record(f"exit:{resolved}")
        raise
    except BaseException:
        # Uncaught exception → the interpreter exits 1.
        record("exit:1")
        raise


def parse_sidecar(payload: str) -> "tuple[str, int] | None":
    """Parse a sidecar file's contents into ``(kind, value)``.

    ``kind`` is ``"exit"`` (value = exit code) or ``"signal"`` (value = signal
    number). Returns ``None`` for anything malformed so the caller falls back to
    the unknown channel.
    """
    text = (payload or "").strip()
    if ":" not in text:
        return None
    kind, _, raw = text.partition(":")
    if kind not in ("exit", "signal"):
        return None
    try:
        return (kind, int(raw))
    except ValueError:
        return None


if __name__ == "__main__":  # pragma: no cover - manual smoke check
    # ponytail: tiny runnable self-check — parse round-trips + latch semantics.
    assert parse_sidecar("exit:0") == ("exit", 0)
    assert parse_sidecar("exit:75") == ("exit", 75)
    assert parse_sidecar("signal:15") == ("signal", 15)
    assert parse_sidecar("garbage") is None
    assert parse_sidecar("exit:notanint") is None
    print("ok")
