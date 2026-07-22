"""Read-only worker view of a run-bound Google Ads status receipt."""

from __future__ import annotations

import json
from pathlib import Path

from tools.registry import registry

TOOL_NAME = "google_ads_campaign_status_receipt"


def _delivery_context() -> tuple[str, int, str] | None:
    from hermes_cli.worker_credentials import trusted_worker_receipt_context

    return trusted_worker_receipt_context()


def _check_receipt_available() -> bool:
    context = _delivery_context()
    if context is None:
        return False
    try:
        from hermes_cli import kanban_db as kb
        from hermes_cli.capability_actions import read_run_receipt

        with kb.connect_closing(Path(context[2])) as conn:
            return read_run_receipt(conn, context[0], context[1]) is not None
    except Exception:
        return False


def _handle_receipt(_args: dict, **_kwargs) -> str:
    context = _delivery_context()
    if context is None:
        return json.dumps({"ok": False, "error": "receipt delivery is unavailable"})
    from hermes_cli import kanban_db as kb
    from hermes_cli.capability_actions import read_run_receipt

    with kb.connect_closing(Path(context[2])) as conn:
        delivery = read_run_receipt(conn, context[0], context[1])
    if delivery is None:
        return json.dumps({"ok": False, "error": "receipt delivery is unavailable"})
    return json.dumps(
        {
            "ok": True,
            "capability": "google_ads_campaign_status_read",
            "delivery_id": delivery.delivery_id,
            "receipt_id": delivery.receipt_id,
            "receipt_digest": delivery.receipt_digest,
            "run_id": delivery.run_id,
            "reused": delivery.reused,
            "receipt": delivery.receipt,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


registry.register(
    name=TOOL_NAME,
    toolset="kanban",
    schema={
        "name": TOOL_NAME,
        "description": (
            "Read the exact controller-produced, non-secret Google Ads campaign "
            "status receipt delivered to this Kanban run. This tool never exposes "
            "OAuth, developer-token, or Bitwarden source values."
        ),
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    handler=_handle_receipt,
    check_fn=_check_receipt_available,
    emoji="🧾",
)
