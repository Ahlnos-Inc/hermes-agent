"""BUILD-801: `main()` must propagate a handler's exit status to the process.

`main()` called `args.func(args)` and discarded the result, so every command
whose handler returns a non-zero code — the whole `kanban` tree, `project`,
`secrets` — exited 0 on failure. Scripts and the dispatcher read `$?`, so a
missing card, an unknown board and a refused gate approval all looked like
success.
"""
import sys

import pytest

from hermes_cli.main import main


@pytest.fixture
def isolated_board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    return tmp_path


def _run(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["hermes", *argv])
    return main()


def test_failing_kanban_handler_exits_non_zero(isolated_board, monkeypatch):
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, "kanban", "show", "t_definitely_missing")
    assert exc.value.code == 1


def test_unknown_board_exits_before_the_gate_dispatcher(isolated_board, monkeypatch):
    """The residual noted on the ticket: this path returns early, above the
    gate dispatcher, so it only reports through the return value."""
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, "kanban", "--board", "no_such_board_typo",
             "gate", "approve", "g_x", "deadbeef")
    assert exc.value.code == 2


def test_successful_handler_does_not_exit(isolated_board, monkeypatch):
    assert _run(monkeypatch, "kanban", "list") is None


def test_a_handler_returning_true_is_success_not_exit_1(isolated_board, monkeypatch):
    """`bool` is an `int` subclass. A handler returning True means it worked;
    treating that as a status would turn every such command into exit 1."""
    import hermes_cli.main as m

    monkeypatch.setattr(m, "cmd_doctor", lambda args: True)
    assert _run(monkeypatch, "doctor") is None

    monkeypatch.setattr(m, "cmd_doctor", lambda args: {"ok": True})
    assert _run(monkeypatch, "doctor") is None

    monkeypatch.setattr(m, "cmd_doctor", lambda args: 3)
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, "doctor")
    assert exc.value.code == 3
