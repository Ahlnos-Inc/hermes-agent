"""Shared argparse builders for the gateway entry points.

The gateway runtime and its process-identity classifier must agree on the
accepted command-line grammar.  Keep these builders stdlib-only so identity
checks can use them without importing the gateway runtime.
"""

from __future__ import annotations

import argparse


class ArgumentParseFailure(Exception):
    """Raised by :class:`NonExitingArgumentParser` instead of writing output."""


class NonExitingArgumentParser(argparse.ArgumentParser):
    """An argparse parser suitable for quiet, fail-closed identity checks."""

    def _print_message(self, message: str, file=None) -> None:
        return None

    def print_help(self, file=None) -> None:
        return None

    def print_usage(self, file=None) -> None:
        return None

    def exit(self, status: int = 0, message: str | None = None) -> None:
        raise ArgumentParseFailure(message or f"argparse exited with status {status}")

    def error(self, message: str) -> None:
        raise ArgumentParseFailure(message)


def build_direct_gateway_parser(
    parser_class: type[argparse.ArgumentParser] = argparse.ArgumentParser,
) -> argparse.ArgumentParser:
    """Build the exact ``gateway.run`` parser."""
    parser = parser_class(description="Hermes Gateway - Multi-platform messaging")
    parser.add_argument("--config", "-c", help="Path to gateway config file")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    return parser


def build_legacy_gateway_parser(
    parser_class: type[argparse.ArgumentParser] = argparse.ArgumentParser,
) -> argparse.ArgumentParser:
    """Build the exact ``scripts/hermes-gateway`` parser."""
    parser = parser_class(
        description="Hermes Gateway - Messaging Platform Integration",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Run in foreground (for testing)
    ./scripts/hermes-gateway run

    # Install as systemd service
    ./scripts/hermes-gateway install

    # Manage the service
    ./scripts/hermes-gateway start
    ./scripts/hermes-gateway stop
    ./scripts/hermes-gateway restart
    ./scripts/hermes-gateway status

    # Uninstall
    ./scripts/hermes-gateway uninstall

Configuration:
    Set environment variables in .env file or system environment:
    - TELEGRAM_BOT_TOKEN
    - DISCORD_BOT_TOKEN
    - WHATSAPP_ENABLED

    Or create ~/.hermes/gateway.json for advanced configuration.
""",
    )
    parser.add_argument(
        "command",
        choices=["run", "install", "uninstall", "start", "stop", "restart", "status"],
        nargs="?",
        default="run",
        help="Command to execute (default: run)",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    return parser
