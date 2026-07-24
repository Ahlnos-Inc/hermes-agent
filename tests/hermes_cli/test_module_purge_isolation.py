"""A test's sys.modules purge must not leak into the next test (BUILD-747).

Order matters here: the first test purges via the shared fixture, the second
asserts that the module identity a module-level import captured is still the one
the code under test imports. Before the fixture existed, the second assertion
failed and any monkeypatch on ``kb`` silently missed the module the CLI calls —
which is how the daemon test ran a real ``run_daemon()`` and hung the suite.
"""
from __future__ import annotations

import sys

from hermes_cli import kanban_db as kb


def test_purge_gives_a_fresh_tree(purged_hermes_modules):
    from hermes_cli import kanban_db as fresh

    assert fresh is not kb


def test_module_identity_survives_previous_purge():
    from hermes_cli import kanban as kb_cli

    assert sys.modules["hermes_cli.kanban_db"] is kb
    assert kb_cli.kb is kb
