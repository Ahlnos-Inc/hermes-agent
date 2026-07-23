# ADR BUILD-700 v9 — Verifier Isolated Execution Capability Routing (Slices 2–4)

- **Status:** DESIGN ONLY. No source changed. No human approval claimed. Not a decision record of an approved change.
- **Supersedes:** v8 — attachment 91 on `t_b3d8d85b`, sha256 `3977a7cb10db7523e653d9acfdc30d416fff25d1ede868059a8c49af5c2f3e3b` (48648 bytes).
- **Source baseline:** HEAD `9665015f2bb77a88cae398900f1be53c648c29c5`, re-derived from live source (paths/lines below are at this HEAD).
- **Jira:** BUILD-700. **Kanban:** architect card `t_233d9277`; independent Sol re-review card `t_cd03338f`; human approval card `t_0577d6d7`.
- **Resolves reviewer blockers:** B72, B73, B74, B75, B76, B77 (all P1, open on `t_cd03338f`).
- **Environmental note (t_390a9f80):** this run authored the ADR through the `WorkspaceFileBroker` file surface only; the workspace terminal is currently rejected by the nested-worktree hardlink sandbox failure tracked in `t_390a9f80` (a stale `.worktrees/t_67f31d19/node_modules/@esbuild/darwin-arm64/bin/esbuild` hard link). No live proof was run this cycle; Axis N therefore remains OFF/unpublished per its live-proof gate (unchanged from v8). The ADR digest is the immutable kanban attachment; the reviewer computes it from the stored bytes.

---

## 0. Invariants preserved from v8 (non-negotiable, re-asserted)

These are unchanged and every slice below keeps them true:

1. **Fresh-namespace, no-veth Axis E.** Execution runs in **fresh PID + mount + network (+IPC/UTS/cgroup) namespaces** via a `crun` OCI container inside the Colima/Lima VM. The net namespace holds **only `lo`**; for Axis E `lo` is **left DOWN** — no `veth`, no NAT, no host networking. Mandatory namespace readback (ns inodes differ from host; `lo` DOWN; empty routes; external + gateway connect denied) → **fail closed to reviewer parity** on any failure. PID-namespace containment is never claimed to imply network isolation.
2. **Reviewer byte-for-byte parity is the fail-closed floor.** Any missing/False/malformed/stale authority, any non-literal-`True` master flag, bare-Darwin (no VM), or any readback failure ⇒ the verifier is **exactly** the current reviewer: tool set `{terminal, read_file}`, `deny_credential_reads=True`, no `process`/background, no `write_file`, Darwin Seatbelt `(deny network*)`. Enforced by a golden Seatbelt fixture + hash.
3. **Credential denial holds at two layers.** `WorkspaceFileBroker(deny_credential_reads=True)` for the read surface (`agent/claude_workspace_files.py:71-74`) **and** a kernel/mount boundary in the exec root so the raw shell cannot `cat` a mounted credential path (v8 B71). Credential aliases are rejected at materialization.
4. **Default OFF.** `agent.verifier_isolated_capability` defaults absent/`False`. Absence, unknown, or any non-literal-`True` value ⇒ reviewer parity.
5. **Slice-2 exact-SHA materialization is a prerequisite** to Slices 3–4. Axis E executes the **exact reviewed commit** materialized by `git archive <sha>` into a verifier-owned disposable exec root. Slices 3–4 must not be implemented before Slice-2 lands the exact-SHA materialization contract.
6. **No source in this ADR.** This is a design artifact; implementation is gated behind the human approval card `t_0577d6d7` and the independent Sol re-review of this exact digest.

---

## 1. Context and what changed since v8

v8 resolved B67–B71 (kernel network boundary, structured authority intent, gate-caller intent, exec-root broker intent, terminal-level credential confinement). Independent GPT-5.6 Sol re-review (t_cd03338f, attempt 10) raised six new P1s. Every one is a **grounding/executability** defect: v8's authority/broker/registration/flag designs do not fit the *live* call graph at HEAD `9665015f2`. v9 re-derives each from source and makes it executable.

Reviewer blockers, mapped to task requirements:

| Blocker | Title | Requirement |
|---|---|---|
| B72 | Human approval cannot select the axis authority it signs | (1) mechanically human-selectable Option A/B via separate immutable digests |
| B73 | Authenticated human gate surface remains an open security decision | (2) one concrete non-model authenticated human approval adapter |
| B74 | VerifierExecBroker construction and error cleanup do not fit the live session graph | (3) construct broker beside `file_broker`, pass to options+resources, close/pop on every terminal path |
| B75 | Exec-root registration has an unowned mkdir crash window | (4) explicit durable state/API + crash test |
| B76 | Master-flag threading and rollback cannot fail closed as written | (5) exact AIAgent constructor/bootstrap value + active-session rollback/drain |
| B77 | Axis N lacks executable loopback setup and container-scoped process authority | (6) remove Axis N or fully specify lo bring-up, proof binding, container provenance, teardown |

---

## 2. B72 — Human-selectable authority via separately published immutable option digests

### 2.1 The live-source defect

`canonicalize_architecture_handoff(metadata)` (`hermes_cli/kanban_db.py:4034-4072`) accepts a **closed** field set:

```
{role, design_depth, chosen_approach, alternatives_rejected, slices,
 acceptance_criteria, verification_plan, human_approval_required, rollout, rollback}
```

and raises `ValueError("unknown top-level authority fields …")` for anything else (4043-4045). The `design_digest` is `architecture_handoff_digest(...)` over that canonical JSON (4075-4089). Therefore v8's plan to embed a `verifier_execution_authority` object with `axes:{axis_e,axis_n}` in the handoff and "hash it into `design_digest`" **cannot be represented** — the canonicalizer rejects the field. And `approve_architecture_gate(gate_id, context, digest)` (4180-4235) only accepts `digest == gate.design_digest` (4212) — the human signs a digest **fixed at accept time**, so they cannot author an A/B choice. B72 is correct.

### 2.2 Design — two separately published immutable option digests + a human-authored selection

The axis choice is removed from the pre-human handoff entirely and represented as **immutable option artifacts** the human selects between at approval time.

**(a) Option artifacts (published by the architect run, immutable).** The architect publishes, as kanban attachments, exactly two canonical-JSON option authorities:

- `verifier-capability-option-A.json` — Axis E only.
- `verifier-capability-option-B.json` — Axis E + Axis N loopback (see §7).

Each is canonical (`json.dumps(..., sort_keys=True, separators=(",",":"), ensure_ascii=False, allow_nan=False)`) and its **sha256 is its `option_digest`**. Schema (version-pinned):

```json
{ "authority_version": "v1",
  "adr": {"artifact": "ADR-BUILD-700-verifier-capability-routing-v9.md", "sha256": "<adr digest>"},
  "option": "A" | "B",
  "axes": {"axis_e": true, "axis_n": false|true},
  "axis_n_live_proof": null | {"artifact": "<proof>", "sha256": "<proof digest>"} }
```

The **base** ADR text names both option digests, so the base `design_digest` (via the free-text `chosen_approach`, which is inside the canonical handoff) transitively binds the exact option set. The option artifacts are *not* smuggled into the handoff field set — they are independent immutable objects, which is precisely the reviewer's preferred "separately published immutable option digests (simplest)."

**(b) Publishing the options onto the gate (orchestrator boundary).** After `accept_architecture_handoff` validates the design_digest, the orchestrator (the same trusted `graph_issuance`/gate boundary that already exists) calls a new domain action:

```python
publish_gate_options(conn, gate_id, context: MutationContext, option_digests: list[str], *, adr_sha256: str)
```

Guard (identical trust model to `issue_architecture_graph`, 4281-4286): `context.actor_type == "orchestrator_agent" and context.profile == "orchestrator" and context.phase == "graph_issuance"`. It requires `gate.state == "validated_awaiting_approval"`, validates each digest is `re.fullmatch(r"[0-9a-f]{64}")`, and CAS-writes a **new** immutable column `published_option_digests` (JSON array) plus `published_adr_sha256` onto `architecture_gates`. Publication is one-shot (reject a second publish with a different set). Option B's digest is included **only if** its `axis_n_live_proof` is a registered proof artifact whose sha matches (see §7.4); otherwise only Option A is published and B is simply unselectable.

**(c) Human selection at approval (extend `approve_architecture_gate`).** Signature gains one required argument:

```python
approve_architecture_gate(conn, gate_id, context: MutationContext, digest: str, *, selected_option_digest: str)
```

New checks, added to the existing body (4196-4224):

- `digest == gate.design_digest` (unchanged — the human still signs the immutable design).
- `selected_option_digest in json.loads(gate.published_option_digests)` else `ArchitectureGateError("approval_option_not_published")` — the human's choice must be one of the published immutable options (tamper denial).
- On success, CAS-write `approved_option_digest = selected_option_digest` alongside the existing approved fields (4215-4222).
- **Replay/tamper:** extend the idempotent-replay guard (4200-4207) to also require `gate.approved_option_digest == selected_option_digest`; a repeat with a *different* selection raises `approval_replay_mismatch`. A tampered digest not in the published set denies before any write.

The choice is thus **human-authored** (the human passes `selected_option_digest`), immutable (content-addressed), replay-safe, and bound to the exact ADR. No model or prior architect run can pre-decide it.

**(d) Sealing the choice for the worker.** The verifier authority the worker reads is the sealed run-spec snapshot (§6), which carries `approved_option_digest`; the worker re-fetches the option artifact by digest and decodes `axes`. Forged/absent/mismatched option digest ⇒ reviewer parity.

### 2.3 Tests (B72)
- Approve with Option A digest → authority decodes `axes.axis_e=true, axis_n=false`; approve with Option B digest → `axis_n=true` (both choices exercised).
- Approve with a digest **not** in `published_option_digests` → `approval_option_not_published`, no state change.
- Idempotent re-approve with the same option → returns gate; re-approve with a different published option → `approval_replay_mismatch`.
- Tamper: mutate one byte of an option artifact → its recomputed digest ∉ published set → selection denied.
- Publish refuses Option B when no matching live-proof artifact is registered (§7.4).

---

## 3. B73 — One concrete, non-model authenticated human approval boundary

### 3.1 The live-source defect

`approve_architecture_gate` / `reject_architecture_gate` require `context.actor_type == "human"` **and** `context.surface in AUTHENTICATED_APPROVAL_SURFACES` (4192-4195, 4242-4245). But `AUTHENTICATED_APPROVAL_SURFACES = frozenset({"cli","dashboard","api","acp","gateway"})` (`kanban_db.py:196`) is a bare **string allowlist**, and there is **no production caller** that constructs a `MutationContext` for approval (B69 acknowledged: `hermes kanban` has no gate verb). A model-driven worker shell (or a PTY sharing the operator's OS identity) that runs `hermes kanban gate approve` could present `surface="cli"` and synthesize `actor_type="human"`, reaching approval. B73 is correct: "select one real direct-human boundary and prove model/worker invocations cannot reach it."

### 3.2 Design — a single out-of-band one-time-code operator boundary

**Pick exactly one boundary and make it the sole minter of an approval-capable context: the `operator_console` OOB-code redemption path.** No CLI/TTY ambiguity is left open.

**(a) Remove `"cli"` from the approval-capable surface set.** Split the constant: keep `AUTHENTICATED_APPROVAL_SURFACES` for read/observability, but gate approval/rejection specifically on a new, disjoint `HUMAN_APPROVAL_SURFACES = frozenset({"operator_console"})`. `approve_architecture_gate`/`reject_architecture_gate` require `context.surface in HUMAN_APPROVAL_SURFACES`. `operator_console` is **not** in any set an agent tool or worker route can construct.

**(b) The only minter of `operator_console`.** A dedicated verb `hermes kanban gate approve` (and `reject`) that:
1. **Refuses to run inside any agent/worker context.** Hard fail if any of `HERMES_KANBAN_TASK`, `HERMES_KANBAN_RUN_ID`, `HERMES_KANBAN_CLAIM_LOCK`, `HERMES_DELEGATION_ID`, `HERMES_SUBAGENT` is set (workers always carry these; see `kanban_runtime_contract._kanban_identity_declared`, lines 31-37). This blocks worker shells and delegated children even under the operator's OS identity.
2. **Requires a per-gate one-time approval code (OTP)** generated server-side at `publish_gate_options` and delivered **only out-of-band** — to the operator's verified gateway DM (configured messaging identity) **or** written to a `0600` file under `HERMES_HOME` that is **outside every `WorkspaceFileBroker` root and the exec-root mount jail** (so no worker/verifier can read it; enforced by §5's kernel/mount boundary and the broker's relative-only, jail-rooted `os.open` at `claude_workspace_files.py:27-29,55-69`). The OTP is single-use, short-TTL, and bound to `(gate_id, design_digest, selected_option_digest)`.
3. Only after the OTP is presented and matched does the verb construct `MutationContext(actor_type="human", surface="operator_console", principal=<os-user>, …)` and call `approve_architecture_gate(...)`.

**(c) Why a model/worker cannot reach it.** Defense in depth:
- The worker tool surface (`tools/kanban_tools.py`) exposes **no** gate verb — the kanban tools are `show/complete/block/heartbeat/comment/create/link/attach*` only (module header lists them). There is nothing to call.
- `delegate_task` leaf children are stripped of `memory/clarify/send_message/execute_code` and cannot spawn; they cannot invoke the verb.
- Even a worker that shells `hermes kanban gate approve` fails guard (1) on the always-present kanban env, and fails guard (2) because the OTP was delivered on a channel the agent provably cannot observe (gateway DM to the human, or a root/0600 file outside the mount jail). Sharing the operator's OS identity is insufficient — the discriminator is **possession of the OOB secret**, not the OS uid.

This is one concrete adapter (OOB one-time code → `operator_console`), not an unresolved CLI/TTY choice.

### 3.3 Tests (B73)
- Worker simulation: env `HERMES_KANBAN_TASK`/`CLAIM_LOCK` set + correct-looking args ⇒ verb refuses; gate stays `validated_awaiting_approval`.
- No/expired/wrong OTP ⇒ refusal; no `MutationContext(surface="operator_console")` is ever constructed.
- Correct OTP + clean (non-worker) env ⇒ approval succeeds; `approve_architecture_gate` receives `surface="operator_console"`.
- Assert no code path constructs `actor_type="human", surface="operator_console"` except the OTP-redemption boundary (grep-proof replaced by a behavioral test: a fake `MutationContext(surface="cli", actor_type="human")` is rejected by `approve_architecture_gate` with `approval_surface_not_authenticated`).
- Mount-jail test: a verifier/worker `read_file` of the OTP file path is denied (outside broker root / credential-denied).

---

## 4. B74 — VerifierExecBroker that fits the live session graph

### 4.1 The live-source defect (exact call graph)

In `agent/external_runtime.py`:
- `file_broker = WorkspaceFileBroker(workspace, deny_credential_reads=…)` is constructed **once, before the session** (416-420).
- `_options(resume)` (422-448) is the **per-turn** `options_factory`: `ClaudeAgentSdkSession` stores it (451-453) and calls it every turn (`options = self._options_factory(self.session_id)`, `claude_sdk_session.py:271`). Anything constructed *inside* `build_claude_agent_options` is therefore **rebuilt each turn** and can never already be in `resources` — exactly the v8 duplication bug.
- `resources=[file_broker]` (456) — `ClaudeAgentSdkSession.close()` iterates and closes them (`claude_sdk_session.py:251-257`).
- The non-auth `except Exception` (485-494) returns a `RuntimeFailure` **without** closing the session/resources; only auth failures call `_clear_auth_state()` (479-483, 491-492), which pops and closes (465-472). So a terminal non-auth failure **leaks** the broker.

### 4.2 Design — one broker, constructed beside `file_broker`, in both options and resources, torn down on every terminal path

**(a) Construct once, beside `file_broker` (after 420):**

```python
verifier_exec_broker = None
if (getattr(agent, "_verifier_isolated_capability", False) is True
        and getattr(agent, "_verifier_execution_authority", None)):
    verifier_exec_broker = VerifierExecBroker(
        workspace=workspace,
        authority=agent._verifier_execution_authority,   # sealed, validated (§6)
        task_id=kanban_task_id, run_id=<run_id>, claim_lock=<claim_lock>,
    )
```

`VerifierExecBroker` lives in `agent/` beside `WorkspaceFileBroker`. It owns exec-root **reservation**, exact-SHA materialization, container-init launch, tool-call scoping, and teardown. It is **lazy**: the disposable exec root is reserved+created on the **first Axis-E turn**, then reused for the session's life (never per-turn).

**(b) Pass the same object into `_options` and into `resources`:**
- `_options` closes over `verifier_exec_broker` and forwards it: `build_claude_agent_options(..., verifier_exec_broker=verifier_exec_broker)`. Because it is captured (not constructed inside), every turn sees the **same** instance — no duplication. When present + armed, `build_claude_agent_options` sets the exec-root cwd and the Axis-appropriate tool set; when `None`, options are byte-identical to today (parity).
- `resources=[b for b in (file_broker, verifier_exec_broker) if b is not None]` (extends 456). `ClaudeAgentSdkSession.close()` then tears the broker down on normal session end.

**(c) `begin_turn`/`close` contract.** The broker implements `begin_turn()` (called per turn at `claude_sdk_session.py:266-269`) as a no-op/turn-budget reset, and an **idempotent** `close()` (mirroring `WorkspaceFileBroker.close`, 42-45) that: kills the container (Axis N), removes the exec root by exact identity, and marks its owned resources cleaned in the DB.

**(d) Close/pop on EVERY terminal failure path.** Generalize the existing auth-only teardown into a single `_teardown_session(key)` that pops `sessions[key]` + `session_attestations[key]` and calls `failed_session.close()` (i.e. what `_clear_auth_state` already does at 469-472, minus the auth-attestation reset). Then:
- `_clear_auth_state()` calls `_teardown_session(key)` (unchanged behavior for auth).
- The `except Exception` block (485-494): after classification, if `classified.reason` is **terminal/non-retryable** (auth, auth_permanent, and the non-retryable RuntimeFailure reasons), call `_teardown_session(key)`; for a genuinely transient reason, keep the session (the exec root is bound to the exact reviewed SHA and safe to reuse) — the next turn's `begin_turn` resets per-turn state.
- On a returned `projection.failure` that is terminal (475-483 path), also `_teardown_session(key)`.
- **Backstop:** even if a process dies before teardown, the exec root and container are **owned run resources** (§5), so the dispatcher's `cleanup_terminal_run_resources` sweep (`kanban_db.py:13208-13238`) reclaims them once the run's `ended_at` is set. Two independent teardown guarantees.

### 4.3 Tests (B74)
- Turn 1 and turn 2 of one session observe the **same** `VerifierExecBroker` id (no per-turn reconstruction); exec root created once.
- Injected terminal non-auth exception ⇒ `sessions` no longer holds the key and `broker.close()` ran (container killed, resource marked cleaned).
- Injected transient exception ⇒ session retained, broker not closed, `begin_turn` reset observed.
- `close()` called twice ⇒ idempotent, no raise.
- Kill-before-teardown ⇒ dispatcher sweep cleans the exec root (backstop).

---

## 5. B75 — Crash-safe exec-root registration (reserved → identified)

### 5.1 The live-source defect

`register_owned_run_resource(...)` (`kanban_db.py:13022-13074`) is a single `INSERT OR IGNORE` keyed by `identity_digest = content_digest(identity)` (13040-13063) — **content-addressed, no update CAS**. `CONTINUATION_RESOURCE_KINDS = {"tmux_session","worktree","child_process"}` (136) has no exec-root kind, and `cleanup_owned_run_resources` dispatches `tmux_session`/`worktree`/**else→`_cleanup_exact_child_process`** (13175-13180) — a new kind would be wrongly treated as a process. v8's "mkdir then update dev/inode" cannot work: there is no update path, and a crash between `mkdir` and identity write leaves a real directory the sweeper cannot prove-or-delete. B75 is correct.

### 5.2 Design — deterministic-path reservation + CAS identification + dedicated cleanup

**(a) New kind + dedicated cleanup branch.** Add `"verifier_exec_root"` to `CONTINUATION_RESOURCE_KINDS` (136) and an explicit branch in `cleanup_owned_run_resources` (before the `else`) → `_cleanup_exact_verifier_exec_root(identity)`. Never let it fall through to `_cleanup_exact_child_process`.

**(b) Two-phase protocol (closes the mkdir window).** The exec-root **path is deterministically derived from a broker-generated reservation token recorded in the DB *before* any `mkdir`**, so a directory that exists on disk is always locatable from the DB row alone — even with no dev/inode yet.

- **Phase 1 — reserve (DB write before mkdir).** New API `reserve_owned_run_resource(conn, task_id, run_id, claim_lock, *, kind="verifier_exec_root", reservation_token, base_dir)` inserts a row with `state="reserved"`, `identity={"version":1,"base_dir":<abs>,"reservation_token":<hex>,"planned_path":<base_dir>/<run_id>/<reservation_token>}`. `identity_digest` over the reservation token ⇒ a unique addressable row. `base_dir` is a verifier-exec root under `HERMES_HOME`, **outside** every workspace/broker root.
- **Phase 2 — mkdir.** `os.mkdir(planned_path)` (parent pre-created; token is 128-bit random ⇒ no collision). Nothing else can ever create this exact leaf.
- **Phase 3 — identify (CAS update).** `os.stat(planned_path)` → `(st_dev, st_ino)`. New API `identify_owned_run_resource(conn, resource_id, claim_lock, *, dev, inode)` runs a **CAS**: `UPDATE continuation_owned_resources SET state='active', identity_json=<full incl dev/inode/path>, identity_digest=<recomputed> WHERE id=? AND state='reserved' AND run_id=? AND claim_lock=?` (rowcount must be 1). This is the missing update-CAS.

**(c) New `reserved` state everywhere it matters.**
- Cleanup handles both identities in `_cleanup_exact_verifier_exec_root`:
  - `reserved`: locate `planned_path` from the reserved identity; if it exists **and** its parent is exactly `base_dir/<run_id>` **and** its leaf equals `reservation_token`, `shutil.rmtree`; if absent → `already_absent`. The deterministic token-derived path is itself the proof of ownership for the reserved state (no dev/inode needed).
  - `active`: `os.stat(path)`; require `st_dev==identity.dev and st_ino==identity.inode` and path-under-base before `rmtree`; mismatch → `identity_mismatch` (fail closed, never delete an unproven path).
- `list_owned_run_resources`/sweeps consider `reserved` non-terminal; `cleanup_owned_run_resources` must clean `state IN ('active','reserved')` for `verifier_exec_root` (extend the `state='active'` filter at 13170/13187 for this kind).
- **DB completion gate:** the terminal/completion resource check treats a `verifier_exec_root` that is not `cleaned` (including `reserved`) as blocking, same as v7.

**(d) Crash-window analysis.**
- Crash after reserve, before mkdir → `reserved` row, no dir → sweep marks `already_absent`/cleaned. Safe.
- Crash after mkdir, before identify → `reserved` row + dir at the deterministic token path → sweep deletes exactly that path (proven by token) → cleaned. **No orphan.**
- Crash after identify (`active`) → full dev/inode identity → normal proven cleanup.

### 5.3 Tests (B75)
- **Crash at the exact instruction boundary between `mkdir` and `identify`** (simulate by reserving+mkdir, then invoking the sweep without identify): assert the dir is deleted and the row `cleaned`.
- Crash between reserve and mkdir → `already_absent`/cleaned.
- `identify` CAS returns rowcount 1 on `reserved`, 0 on already-`active` (no double-identify).
- Active-state dev/inode mismatch (path recreated by something else) → `identity_mismatch`, directory **not** deleted.
- `_cleanup_exact_verifier_exec_root` refuses any path not under `base_dir` (path-escape guard).

---

## 6. B76 — Master-flag threading and fail-closed rollback

### 6.1 The live-source defect

The master flag `agent.verifier_isolated_capability` is not threaded. `_init_agent` (`hermes_cli/cli_agent_setup_mixin.py:322`) calls `preflight_kanban_cli_route(...)` (343-348) purely for validation and **discards** the return; the `AIAgent(...)` constructor (495-538+) has no verifier params. The run-spec (§`_build_run_spec`, `kanban_db.py:7180-7237`) exposes only `delivery_policy`, whose `enforcing_approved` disposition treats **`policy_accepted` == `human_approved`** (`_delivery_policy_snapshot`, 7090-7093) — unsafe to reuse for a security-sensitive capability. And "flag OFF restores parity" is false for a live session, because authority is snapshotted once. B76 is correct.

### 6.2 Design — exact bootstrap value + strict snapshot + defined rollback

**(a) Strict, separate `verifier_execution` snapshot (not `delivery_policy`).** Add `_verifier_execution_snapshot(gate)` and `validate_verifier_execution_snapshot(value)` beside the delivery-policy pair (7070-7177), and a `"verifier_execution"` key in `_build_run_spec`'s return (7227-7237). It yields an *armed* disposition **only** when `gate.state == "human_approved"` (strict — never `policy_accepted`) and carries `{version:1, disposition:"human_approved_execution", gate_id, design_digest, approved_option_digest, accepted_run_id, row_version}`; otherwise `{version:1, disposition:"none", …}`. Validation regex-checks digests and rejects any armed snapshot missing `approved_option_digest` or with a non-`human_approved` state.

**(b) Exact bootstrap value in `_init_agent`.** Capture the preflight return (stop discarding it) — extend `preflight_kanban_cli_route` to return a `KanbanRoutePreflight` including the sealed `verifier_execution` snapshot from `load_active_run_spec()` (`kanban_runtime_contract.py:40-49`). Then compute, once, before the `AIAgent(...)` call:

```python
verifier_isolated_capability = (config.get("agent", {}).get("verifier_isolated_capability") is True)  # literal True only
verifier_execution_authority = resolve_verifier_execution_authority(preflight.verifier_execution)      # dict or None
```

`resolve_verifier_execution_authority` returns a validated authority dict **only if**: the master flag is literally `True`; the snapshot disposition is `human_approved_execution`; the snapshot is **not stale** — `gate.row_version` and `design_digest` still match the live gate at claim time (re-read), and `approved_option_digest ∈ gate.published_option_digests`; and the option artifact decodes. Any failure ⇒ `None` (parity). Pass both as **new `AIAgent(...)` kwargs** (added to the call at 495):

```python
verifier_isolated_capability=verifier_isolated_capability,
verifier_execution_authority=verifier_execution_authority,
```

`AIAgent.__init__` stores `self._verifier_isolated_capability` (default `False`) and `self._verifier_execution_authority` (default `None`) — the exact attributes `external_runtime` reads in §4.2(a). This is the single, exact constructor/bootstrap seam; the value is computed once and **sealed for the session's life** (never mutated mid-conversation — preserves prompt caching).

**(c) Rollback semantics (explicit, fail-closed).** Because authority is snapshot-sealed at bootstrap:
- **Config flag OFF ⇒ next-claim-only.** A live session keeps its sealed authority; the change takes effect on the next run claim, which re-bootstraps in parity. Stated plainly (no false "instant OFF for live sessions").
- **Force-disarm active runs ⇒ drain protocol.** `hermes kanban gate invalidate` (orchestrator/operator boundary) sets the gate `invalidated` and triggers a **drain sweep**: for every active run whose sealed `verifier_execution.gate_id` matches, send SIGTERM→grace→SIGKILL via the existing `enforce_max_runtime` machinery (`kanban_db.py:13241+`), which sets `ended_at`; the resource sweep (`cleanup_terminal_run_resources`) then tears down the broker/exec-root/container. Re-claims re-bootstrap in parity (gate no longer `human_approved`). This is the *only* way to disarm a running session, and it is fail-closed (terminates rather than mutating live context).

### 6.3 Tests (B76)
- Flag not literally `True` (e.g. `"true"`, `1`, truthy object) ⇒ `verifier_isolated_capability=False` ⇒ parity.
- Sealed snapshot with `state="policy_accepted"` ⇒ authority `None` ⇒ parity (proves no `delivery_policy` reuse).
- Stale snapshot (`row_version`/`design_digest` no longer matches live gate) ⇒ `None` ⇒ parity.
- Bootstrap threads both kwargs to `AIAgent`; `external_runtime` constructs the broker iff both are armed.
- Flag-OFF on a live session → no change until next claim (assert sealed authority unchanged); next claim boots parity.
- `gate invalidate` on an active run → run drained (SIGTERM path), exec root cleaned, re-claim in parity.

---

## 7. B77 — Axis N: executable loopback + container-scoped process authority

The task allows either removing Axis N from this approval or fully specifying it. v9 keeps Axis N **specified but unpublished-until-proof**: Option B is a real, fully-specified selectable option, but its digest is only publishable once a live proof artifact exists (§2.2b, §7.4). This satisfies both B72 (a genuine A/B choice exists in the design) and B77 (Axis N is fully specified), while preserving the invariant "Axis N OFF until a live IPv4+IPv6 loopback proof."

### 7.1 Trusted pre-exec loopback bring-up (before capability drop)

The reviewer's gap: v8 "drops all capabilities and blocks namespace operations" but never says who brings `lo` UP. Design: loopback is configured by the **container init as a trusted pre-exec step, inside the fresh net namespace, before dropping `CAP_NET_ADMIN` and before exec'ing the acceptance suite**:

1. `crun` creates the container with a **fresh** net namespace containing only `lo` (no veth, no CNI, no host netns join).
2. The OCI `process` runs a tiny trusted init that: `ip link set lo up` (requires `CAP_NET_ADMIN`, held only for this step); verifies routes are empty and no non-`lo` interface exists; then **drops all capabilities**, sets `no_new_privileges=true`, and applies the seccomp profile blocking `mount`/`unshare`/`setns`/`clone(CLONE_NEW*)` before `execve` of the test command. The workload thus runs with `lo` UP and **zero** capability to add interfaces, routes, or namespaces.
3. Readback (fail-closed to parity): net ns inode ≠ host; only `lo` present and UP; empty route table; `connect()` to a non-loopback address and to the host gateway is denied; `bind`/`connect` on `127.0.0.1`/`::1` succeed. Any failure ⇒ deny/parity.

### 7.2 Container-scoped process provenance

The reviewer's gap: `_cleanup_exact_child_process` (`kanban_db.py:13136-13160`) identifies a process by `pid` + `process_started_at` only — two same-task sessions are not container-proven. For Axis N processes, extend the owned-resource identity to include **`broker_generation_id`** (a fresh uuid per `VerifierExecBroker` instance), **`container_id`** (the crun container id), and **`net_ns_inode`**. `_cleanup_exact_child_process` (used only as a backstop) additionally verifies these; the primary teardown is container-scoped (§7.3), so a reused pid in a different container/generation is rejected.

### 7.3 Teardown

Axis N teardown is **container-first**: `crun kill --all` + `crun delete` on the broker's container id reaps the PID-namespace init, which reaps every descendant and destroys the net namespace (loopback included). The broker then removes the exec root by exact identity (§5) and marks all owned resources (exec_root + any process rows) cleaned. The dispatcher sweep is the backstop. No pgid/fd scan is used as containment.

### 7.4 Live-proof binding in authority/state

Option B's `axis_n_live_proof` (§2.2a) references a **registered proof artifact** — a recorded live run (Colima up) demonstrating: bind/connect on `127.0.0.1:PORT` and `[::1]:PORT` succeed; connect to an external IP and to the VM gateway is denied; `0.0.0.0`/`::` are **not** exposed to the LAN. `publish_gate_options` includes Option B's digest **only if** a proof artifact with the matching sha256 is registered on the board; otherwise B is unpublished and unselectable. Thus Axis N cannot be armed until the live proof exists, and the proof is bound into the immutable option the human signs.

### 7.5 Tests (B77) — deferred to live proof
The IPv4+IPv6 loopback-only netns proof and its container-provenance/teardown tests require Colima running and are the Slice-4b gate; they are **not** run this cycle (terminal unavailable, t_390a9f80). Until then Option B stays unpublished. Option A (Axis E, `lo` DOWN, deny-all) carries the full §5/§6 test suite and is the only human-selectable option now.

---

## 8. Implementation slices (design only; each independently verifiable)

- **Slice 2 (prerequisite):** exact-SHA `git archive <sha>` materialization into a verifier-owned exec root; credential-alias rejection at materialization. *Verify:* materialized tree hash == reviewed commit tree; credential path rejected.
- **Slice 3a — authority & approval (B72, B73):** option artifacts + `publish_gate_options` + `approved_option_digest` on `approve_architecture_gate` + `HUMAN_APPROVAL_SURFACES`/`operator_console` OOB-code verb. *Verify:* §2.3, §3.3 tests.
- **Slice 3b — resource lifecycle (B75):** `verifier_exec_root` kind + `reserve`/`identify` APIs + `_cleanup_exact_verifier_exec_root` + `reserved` state. *Verify:* §5.3 crash tests.
- **Slice 3c — broker wiring (B74):** `VerifierExecBroker` constructed beside `file_broker`, in `_options` + `resources`, `_teardown_session` on terminal paths. *Verify:* §4.3 tests.
- **Slice 3d — flag threading & rollback (B76):** strict `verifier_execution` snapshot + `KanbanRoutePreflight` return + two `AIAgent` kwargs + drain protocol. *Verify:* §6.3 tests.
- **Slice 4a — Axis E execution:** fresh PID+mount+net(+IPC/UTS/cgroup) ns, `lo` DOWN, deny-all, reviewer tool set, mandatory readback. *Verify:* namespace readback denies external + gateway; golden reviewer parity fixture+hash unchanged when disarmed.
- **Slice 4b — Axis N (unpublished until proof) (B77):** loopback bring-up before cap drop, container provenance, container-first teardown, live-proof publication gate. *Verify:* §7.5 (deferred to live proof).

## 9. Acceptance criteria

1. Human selects Option A or B via **separate immutable option digests**; a digest ∉ published set denies; replay with a different option denies; both choices tested (B72).
2. Approval is mintable **only** by the `operator_console` OOB-code boundary; worker/PTY/delegated invocations provably cannot reach it; `surface="cli"` no longer approves (B73).
3. Exactly one `VerifierExecBroker` per session, constructed beside `file_broker`, present in options and resources, torn down on normal/auth/terminal-error/kill/drain, with a dispatcher-sweep backstop (B74).
4. Exec-root registration is crash-safe via reserved→identified with a deterministic token path; a crash between mkdir and identify leaves no orphan; dedicated cleanup never runs as a process (B75).
5. Master flag is a literal-`True` check threaded through the exact `AIAgent` constructor; authority is a strict `human_approved`-only snapshot (never `policy_accepted`); rollback is next-claim-only with a defined drain for active runs (B76).
6. Axis N is fully specified (loopback bring-up before cap drop, container/broker-generation process provenance, container-first teardown) and remains **unpublished/unselectable** until a registered live IPv4+IPv6 loopback-only proof; Axis E (`lo` DOWN, deny-all) is the only selectable option now (B77).
7. Disarmed verifier == current reviewer byte-for-byte (golden Seatbelt fixture+hash); credential reads denied at broker **and** kernel/mount layer; default OFF; Slice-2 exact-SHA prerequisite retained.

## 10. Verification plan

- Unit/behavior tests per §2.3, §3.3, §4.3, §5.3, §6.3 (design targets; no source written this cycle).
- Golden reviewer-parity fixture + hash: disarmed verifier options are byte-identical to reviewer.
- Reviewer-reported focused baseline (152/152 across 9 files, excluding the tracked hardlink host-state test t_390a9f80) must stay green once implemented.
- Live IPv4+IPv6 loopback-only netns proof (Colima up) gates Slice-4b / Option B publication — deferred.
- Independent GPT-5.6 Sol re-review of **this exact ADR attachment digest** must return no open P1 before human approval (`t_0577d6d7`).

## 11. Rollout / rollback

- **Rollout:** staged. Slice 2 → Slice 3a–3d → Slice 4a (Axis E). Slice 4b/Option B publication only after the live proof. `agent.verifier_isolated_capability` default OFF; per-gate `human_approved` + sealed snapshot required to arm.
- **Rollback:** disable. Flag OFF ⇒ next-claim parity; `gate invalidate` ⇒ drain active runs (SIGTERM→SIGKILL) + resource sweep ⇒ parity. No schema rollback needed (new columns/kind are additive and nullable).

## 12. Human approval gate

`human_approval_required = true`. This ADR must NOT be implemented until: (a) independent GPT-5.6 Sol re-review of this exact attachment digest returns no open P1, and (b) the human approves via the `operator_console` OOB-code boundary on `t_0577d6d7`, selecting Option A (Option B unavailable until live proof). Scheduler status is never authority.

## 13. Blocker resolution summary

| Blocker | Resolution (source-grounded) |
|---|---|
| **B72** | Axis choice removed from the closed handoff field set (`kanban_db.py:4038-4045`); two separately published immutable option digests; `publish_gate_options` records them; `approve_architecture_gate` (4180) gains `selected_option_digest` validated ∈ published set with replay/tamper denial. |
| **B73** | Split approval surface: `HUMAN_APPROVAL_SURFACES={"operator_console"}` disjoint from the forgeable `AUTHENTICATED_APPROVAL_SURFACES` (196); a single OOB one-time-code verb is the sole minter, refuses worker env (`kanban_runtime_contract:31-37`), and requires a secret the agent cannot observe (mount-jailed / gateway-DM). No worker gate tool exists (`kanban_tools.py`). |
| **B74** | `VerifierExecBroker` constructed beside `file_broker` (`external_runtime.py:416-420`), captured by `_options` (422) and added to `resources` (456); `_teardown_session` closes/pops on every terminal path (fixes 485-494 leak); dispatcher sweep backstop. |
| **B75** | New `verifier_exec_root` kind (136) + dedicated cleanup branch (13175-13180); reserved→identified two-phase with deterministic token path + `identify` CAS (missing update path added); crash test at the mkdir/identify boundary. |
| **B76** | Strict `verifier_execution` snapshot requiring `human_approved` (not `policy_accepted`, avoiding 7090-7093); preflight return captured (cli_agent_setup_mixin:342-353); two literal-value `AIAgent` kwargs (495); next-claim-only rollback + drain for active runs. |
| **B77** | Loopback brought UP by trusted container init before cap drop; process provenance = (broker_generation_id, container_id, net_ns_inode) beyond pid+birth (13136-13160); container-first teardown; Option B unpublished until a registered live IPv4+IPv6 proof. |
