"""Fork merge-survival guard: kanban quota-exit code contract.

The fork maps a quota-class turn failure under HERMES_KANBAN_TASK to
KANBAN_RATE_LIMIT_EXIT_CODE so the dispatcher's reap classifier releases the
card back to `ready` WITHOUT ticking the failure counter (a 5h window must not
trip the circuit breaker). The reason-tuple is guarded by
test_kanban_quota_exit_gate.py; this pins the exit-code VALUE the gate maps to,
which the dispatcher side depends on. See [audit: quota-origin cluster, item 2].
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from hermes_cli.kanban_db import KANBAN_RATE_LIMIT_EXIT_CODE


def test_kanban_rate_limit_exit_code_is_75():
    # cli.py emits this on quota failure; the reap classifier reads it back.
    # If a merge changes the value on one side only, the two silently disagree.
    assert KANBAN_RATE_LIMIT_EXIT_CODE == 75
