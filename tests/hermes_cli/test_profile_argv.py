"""Parity tests for profile pre-argparse scanning and execution ingress."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from gateway.process_identity import GatewayRuntimeRole, classify_gateway_argv
from hermes_cli.profile_argv import parse_profile_argv


def test_profile_parser_normalizes_inline_and_spaced_selectors():
    for argv in (
        ("--profile=WoRK", "gateway", "run"),
        ("-p", "WoRK", "gateway", "run"),
    ):
        parsed = parse_profile_argv(argv)
        assert parsed.argv == ("gateway", "run")
        assert parsed.profile == "work"
        assert parsed.valid


def test_profile_parser_matches_real_value_and_optional_value_bounds():
    assert parse_profile_argv(
        ("--model", "--profile=child", "gateway", "run")
    ).profile is None
    reasoning = parse_profile_argv(
        ("--reasoning-effort", "--profile=child", "gateway", "run")
    )
    assert reasoning.argv == (
        "--reasoning-effort",
        "--profile=child",
        "gateway",
        "run",
    )
    assert reasoning.profile is None
    assert reasoning.valid
    parsed = parse_profile_argv(
        ("--continue", "--profile=child", "gateway", "run")
    )
    assert parsed.argv == ("--continue", "gateway", "run")
    assert parsed.profile == "child"
    assert parsed.valid


def test_profile_parser_stops_at_passthrough_bounds():
    for argv in (
        ("--", "--profile=child", "gateway", "run"),
        ("mcp", "add", "tool", "--args", "--profile=child"),
    ):
        parsed = parse_profile_argv(argv)
        assert parsed.argv == argv
        assert parsed.profile is None
        assert parsed.valid


def test_profile_parser_rejects_duplicate_malformed_and_reserved_selectors():
    duplicate = parse_profile_argv(
        ("--profile", "work", "--profile=other", "gateway", "run")
    )
    assert duplicate.argv == ("--profile=other", "gateway", "run")
    assert duplicate.profile == "work"
    assert not duplicate.valid

    for argv in (
        ("-p=x", "gateway", "run"),
        ("--profile", "bad.name", "gateway", "run"),
        ("--profile", "test", "gateway", "run"),
        ("--profile",),
    ):
        parsed = parse_profile_argv(argv)
        assert not parsed.valid
        assert parsed.profile is None
        assert classify_gateway_argv(("hermes", *argv))[0] is GatewayRuntimeRole.FOREIGN


def test_apply_profile_override_uses_the_same_normalized_selector(tmp_path, monkeypatch):
    profile_dir = tmp_path / ".hermes" / "profiles" / "work"
    profile_dir.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setattr(
        sys, "argv", ["hermes", "--profile=WoRK", "gateway", "run"]
    )

    from hermes_cli.main import _apply_profile_override

    _apply_profile_override()

    assert os.environ["HERMES_HOME"] == str(profile_dir)
    assert sys.argv == ["hermes", "gateway", "run"]
