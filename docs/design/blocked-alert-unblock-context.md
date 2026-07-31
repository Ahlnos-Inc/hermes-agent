# Blocked-alert unblock context

**Problem.** A "🧑‍🔧 Human input needed" alert in the Kanban Telegram topic
carried only a free-text reason. To act, the operator had to open a terminal,
find the task, and hunt for the content under review.

**Contract (worker side).** `kanban_block(metadata=...)` recognizes three
keys on `needs_input` blocks; `block_task` copies them (sanitized, capped)
into the blocked event payload — `_block_context_from_metadata` in
`hermes_cli/kanban_db.py`:

- `ask` — one sentence: the exact decision/action needed (≤500 chars).
- `links` — http(s) URLs to the content or ticket under review (≤10).
- `artifacts` — absolute local file paths, uploaded into the alert as native
  Telegram media (same key as `kanban_complete`; ≤10).

Everything else in `metadata` stays run-row-only. Invalid entries are
dropped, never fatal. `operator_block_task` carries the same keys on both
its fenced and non-fenced paths.

**Rendering (notifier side).** `render_kanban_event` for `blocked` /
`block_loop_detected` now emits the task title plus context lines
(`_block_context_lines` in `gateway/kanban_notifications.py`):

```
⏸ [board] Kanban t_x blocked — IG posts week 31: needs approval
❓ Approve the 3 posts or reply with edits
🔗 https://drive.google.com/drive/folders/abc
🎫 BUILD-777
📁 /path/to/workspace
```

- Legacy blocks (no metadata): URLs are scraped from the reason text.
- Jira: bare keys only, extracted from title/body/branch via
  `extract_jira_keys`, skipped when already visible in the message — the
  Telegram adapter linkifies bare keys (`JIRA_BROWSE_URL`), so the renderer
  stays transport-neutral. `file://` links are never emitted (Telegram can't
  open them); local content travels as `artifacts` uploads instead.

**Delivery.** After a successful human-block text send, the notifier reuses
`_deliver_kanban_artifacts` (the completed-task upload path: image batching,
`filter_local_delivery_paths` safety filter, per-file error isolation) with
`task=None` so a prior run's `result` can't resend stale files. Media
failure never blocks the text alert; dedup ledger semantics unchanged.
