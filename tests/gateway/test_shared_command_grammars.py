"""Differential coverage for gateway identity and execution parsers."""

from __future__ import annotations

from pathlib import Path

import pytest

from gateway.command_line import (
    ArgumentParseFailure,
    NonExitingArgumentParser,
    build_direct_gateway_parser,
    build_legacy_gateway_parser,
)
from gateway.process_identity import (
    GatewayRuntimeRole,
    _build_cli_identity_parser,
    _parse_cli_command,
    classify_gateway_argv,
)
from hermes_cli.profile_argv import ProfileArgvParse
from hermes_cli._parser import build_top_level_parser
from hermes_cli.profile_argv import parse_profile_argv
from hermes_cli.subcommands.gateway import build_gateway_parser


def _parse(parser, argv: tuple[str, ...]):
    try:
        return parser.parse_args(argv)
    except ArgumentParseFailure:
        return None


def _identity(argv: tuple[str, ...], **kwargs):
    return classify_gateway_argv(argv, **kwargs)[0]


def _cli_parser():
    parser, subparsers, _chat = build_top_level_parser(
        parser_class=NonExitingArgumentParser
    )
    inert = lambda _args: None
    build_gateway_parser(
        subparsers,
        cmd_gateway=inert,
        cmd_proxy=inert,
        cmd_gateway_enroll=inert,
    )
    return parser


@pytest.mark.parametrize(
    "tail",
    [
        (),
        ("-cfoo",),
        ("-c=foo",),
        ("-vv",),
        ("--config", "foo", "--verbose"),
    ],
)
def test_direct_identity_matches_shared_runner_parser(tail):
    parser = build_direct_gateway_parser(NonExitingArgumentParser)
    assert _parse(parser, tail) is not None
    assert _identity(("python", "-m", "gateway.run", *tail)) is GatewayRuntimeRole.RUNTIME


@pytest.mark.parametrize("tail", [("junk",), ("--config",), ("--unknown",)])
def test_direct_identity_rejects_shared_runner_parser_failures(tail):
    parser = build_direct_gateway_parser(NonExitingArgumentParser)
    assert _parse(parser, tail) is None
    assert _identity(("python", "-m", "gateway.run", *tail)) is GatewayRuntimeRole.FOREIGN


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (("hermes-gateway",), "run"),
        (("hermes-gateway", "-vv", "run"), "run"),
        (("hermes-gateway", "start", "--verbose"), "start"),
        (("hermes-gateway", "restart", "--verbose"), "restart"),
    ],
)
def test_legacy_identity_matches_shared_wrapper_parser(argv, expected):
    parser = build_legacy_gateway_parser(NonExitingArgumentParser)
    parsed = _parse(parser, argv[1:])
    assert parsed is not None
    assert parsed.command == expected
    role = _identity(argv)
    assert role is (
        GatewayRuntimeRole.RUNTIME if expected == "run" else GatewayRuntimeRole.MANAGER
    )


def test_legacy_identity_rejects_extra_tail_that_wrapper_rejects():
    parser = build_legacy_gateway_parser(NonExitingArgumentParser)
    argv = ("hermes-gateway", "restart", "junk")
    assert _parse(parser, argv[1:]) is None
    assert _identity(argv) is GatewayRuntimeRole.FOREIGN


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (("hermes", "gateway", "--accept-hooks"), GatewayRuntimeRole.RUNTIME),
        (("hermes", "gateway", "--accept-hooks", "run"), GatewayRuntimeRole.RUNTIME),
        (("hermes", "gateway", "run", "-vv"), GatewayRuntimeRole.RUNTIME),
        (("hermes", "gateway", "start", "--system", "--all"), GatewayRuntimeRole.MANAGER),
        (("hermes", "gateway", "setup"), GatewayRuntimeRole.MANAGER),
        (("hermes", "--profile", "WoRK", "gateway", "run"), GatewayRuntimeRole.RUNTIME),
        (("hermes", "gateway", "run", "--profile", "work"), GatewayRuntimeRole.RUNTIME),
    ],
)
def test_cli_identity_matches_canonical_gateway_parser(argv, expected):
    parsed = parse_profile_argv(argv[1:])
    assert parsed.valid
    parsed = _parse(_cli_parser(), parsed.argv)
    assert parsed is not None
    assert parsed.command == "gateway"
    assert _identity(argv) is expected


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (("python", "-m", "hermes_cli.main", "gateway", "run"), GatewayRuntimeRole.RUNTIME),
        (("python", "hermes_cli/main.py", "gateway", "run"), GatewayRuntimeRole.RUNTIME),
        (("python", "-m", "hermes_cli/main.py", "gateway", "run"), GatewayRuntimeRole.FOREIGN),
    ],
)
def test_cli_identity_entrypoint_spellings_match_canonical_grammar(argv, expected):
    assert _identity(argv) is expected


def test_cli_identity_fails_closed_on_invalid_profile_scan(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.profile_argv.parse_profile_argv",
        lambda _args: ProfileArgvParse(("gateway", "run"), None, False),
    )

    def unexpected_parser_build():
        raise AssertionError("invalid profile argv must not reach argparse")

    monkeypatch.setattr(
        "gateway.process_identity._build_cli_identity_parser",
        unexpected_parser_build,
    )
    assert _parse_cli_command(("gateway", "run")) is None


def test_cached_cli_identity_parser_has_no_sequential_parse_state_leakage():
    parser = _build_cli_identity_parser()
    assert parser is _build_cli_identity_parser()
    assert _parse_cli_command(("--profile", "work", "gateway", "run")) == (
        "run",
        "work",
    )
    assert _parse_cli_command(("gateway", "start")) == ("start", None)
    assert _parse_cli_command(("gateway", "run")) == ("run", None)


@pytest.mark.parametrize(
    "argv",
    [
        ("hermes", "gateway", "start", "junk"),
        ("hermes", "gateway", "run", "junk"),
        ("hermes", "chat", "gateway", "run"),
        ("hermes", "unknown", "gateway", "run"),
        ("hermes", "--oneshot", "x", "gateway"),
        ("hermes", "--version", "gateway"),
        ("hermes", "-p=x", "gateway", "run"),
        ("hermes", "--profile", "work", "--profile", "other", "gateway", "run"),
        ("hermes", "--profile", "gateway", "run", "--", "--profile", "other"),
        ("hermes", "--profile", "bad.name", "gateway", "run"),
    ],
)
def test_cli_identity_is_foreign_when_canonical_parser_rejects_or_mode_wins(argv):
    parsed = parse_profile_argv(argv[1:])
    canonical = _parse(_cli_parser(), parsed.argv)
    if argv[1:3] in (("--oneshot", "x"), ("--version", "gateway")):
        assert canonical is not None
    else:
        assert canonical is None
    assert _identity(argv) is GatewayRuntimeRole.FOREIGN


def test_named_profile_home_must_match_dispatch_identity(tmp_path: Path):
    root = tmp_path / "root"
    matching = root / "profiles" / "work"
    other = root / "profiles" / "other"
    argv = ("hermes", "--profile=WoRK", "gateway", "run")

    role, profile, home = classify_gateway_argv(
        argv, default_home=root
    )
    assert (role, profile, home) == (
        GatewayRuntimeRole.RUNTIME,
        "work",
        matching.resolve(),
    )

    role, profile, home = classify_gateway_argv(
        argv, environment={"HERMES_HOME": str(matching)}
    )
    assert (role, profile, home) == (
        GatewayRuntimeRole.RUNTIME,
        "work",
        matching.resolve(),
    )

    for captured_home in (root, other):
        role, profile, home = classify_gateway_argv(
            argv,
            environment={"HERMES_HOME": str(captured_home)},
            default_home=root,
        )
        assert (role, profile, home) == (GatewayRuntimeRole.FOREIGN, None, None)


def test_leading_and_environment_homes_must_resolve_identically(tmp_path: Path):
    role, profile, home = classify_gateway_argv(
        (
            f"HERMES_HOME={tmp_path / 'a'}",
            "hermes",
            "gateway",
        ),
        environment={"HERMES_HOME": str(tmp_path / "b")},
    )
    assert (role, profile, home) == (GatewayRuntimeRole.FOREIGN, None, None)


def test_windows_unquoted_entrypoint_without_assignment_stays_supported():
    role, _profile, _home = classify_gateway_argv(
        r"C:\Program Files\Hermes\Hermes.EXE gateway run"
    )
    assert role is GatewayRuntimeRole.RUNTIME
