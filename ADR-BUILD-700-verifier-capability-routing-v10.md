# ADR BUILD-700 v10 — Verifier Isolated Execution Capability Routing (Slices 2–4)

- **Status:** DESIGN ONLY. No source changed. No human approval claimed. Not a decision record of an approved change.
- **Supersedes:** v9 — attachment 92 on `t_233d9277`, sha256 `9ab02fedad73c0e8f3c93eb0c058882d7473524be049785db9af121116761a8c` (40053 bytes).
- **Source baseline:** HEAD `738cf5c310a5943f65a9e82ee699af635c7c619f` (Slice 1 merged); re-derived from live source (paths/lines below are at this HEAD).
- **Jira:** BUILD-700. **Kanban:** architect card `t_233d9277`; independent Sol re-review card `t_cd03338f`; human approval card `t_0577d6d7`.
- **Resolves reviewer blockers:** B78, B79, B80, B81, B82 (all P1, open on `t_cd03338f`).
- **Environmental note (t_390a9f80):** this run authored the ADR through the `WorkspaceFileBroker` file surface only; the workspace terminal is currently rejected by the nested-worktree hardlink sandbox failure tracked in `t_390a9f80`. No live proof was run this cycle; Axis N therefore remains OFF/unpublished per its live-proof gate (unchanged from v9). The ADR digest is the immutable kanban attachment; the reviewer computes it from the stored bytes.

---

## 0. Invariants preserved from v9 (non-negotiable, re-asserted)

These are unchanged and every slice below keeps them true:

1. **Fresh-namespace, no-veth Axis E.** Execution runs in **fresh PID + mount + network (+IPC/UTS/cgroup) namespaces** via a `crun` OCI container inside the Colima/Lima VM. The net namespace holds **only `lo`**; for Axis E `lo` is **left DOWN** — no `veth`, no NAT, no host networking. Mandatory namespace readback (ns inodes differ from host; `lo` DOWN; empty routes; external + gateway connect denied) → **fail closed to reviewer parity** on any failure. PID-namespace containment is never claimed to imply network isolation.
2. **Reviewer byte-for-byte parity is the fail-closed floor.** Any missing/False/malformed/stale authority, any non-literal-`True` master flag, bare-Darwin (no VM), or any readback failure ⇒ the verifier is **exactly** the current reviewer: tool set `{terminal, read_file}`, `deny_credential_reads=True`, no `process`/background, no `write_file`, Darwin Seatbelt `(deny network*)`. Enforced by a golden Seatbelt fixture + hash.
3. **Credential denial holds at two layers.** `WorkspaceFileBroker(deny_credential_reads=True)` for the read surface (`agent/claude_workspace_files.py:71-74`) **and** a kernel/mount boundary in the exec root so the raw shell cannot `cat` a mounted credential path (v8 B71). Credential aliases are rejected at materialization.
4. **Default OFF.** `agent.verifier_isolated_capability` defaults absent/`False`. Absence, unknown, or any non-literal-`True` value ⇒ reviewer parity.
5. **Slice-2 exact-SHA materialization is a prerequisite** to Slices 3–4. Axis E executes the **exact reviewed commit** materialized by `git archive <sha>` into a verifier-owned disposable exec root. Slices 3–4 must not be implemented before Slice-2 lands the exact-SHA materialization contract.
6. **No source in this ADR.** This is a design artifact; implementation is gated behind the human approval card `t_0577d6d7` and the independent Sol re-review of this exact digest.

---

## 1. Context and what changed since v9

v9 resolved B72–B77 (design only; no source changed). Independent GPT-5.6 Sol re-review (t_cd03338f, attempt 12) raised five new P1s. Every one is a **grounding/executability** defect: v9's authority/broker/registration/flag designs do not fit the *live* call graph at HEAD `738cf5c31` (which includes Slice 1: benign introspection probes merged). v10 re-derives each from source and makes it executable.

Source changes since v9's baseline (`9665015f2`):
- **Slice 1 merged** (`738cf5c31`): benign introspection probes admitted in read-only terminal.
- **BUILD-706 merged** (`091099d79`): review artifact rebind on fix completion.
- **BUILD-708 merged** (`f2745b48d`): scoped workspace terminal hard-link boundary.
- **BUILD-704 merged** (`d818337cc`): reviewer rework loop bound, escalate to human gate.
- **BUILD-531 merged** (`2753bc9d`): notifier corruption reporting + dispatcher-owned recovery.
- **kanban_db.py** gained ~4000 lines of diff: new `ReworkResult` fields (`escalated`, `escalation_target_task_id`, `escalation_reason`), `DependencyReconcileResult` fields (`artifact_backfilled`, `artifact_selection_required`), `Task` fields (`workspace_managed`, `workspace_repo_root`, `workspace_repo_common_dir`, `workspace_cleanup_lease`, `workspace_cleanup_lease_expires`), new `ReviewArtifactBinding` class, corruption incident system, rework loop escalation constants, `rework_loop_escalated` added to `TERMINAL_KINDS`.

**Critical:** None of these changes affect the architecture-gate domain functions (`approve_architecture_gate`, `reject_architecture_gate`, `issue_architecture_graph`) or the constants v9 references. The `canonicalize_architecture_handoff` closed field set is unchanged. `AUTHENTICATED_APPROVAL_SURFACES` is unchanged. `CONTINUATION_RESOURCE_KINDS` still lacks `verifier_exec_root`. `cleanup_owned_run_resources` still dispatches unknown kinds to `_cleanup_exact_child_process`. The source defects v9 identified remain present.

Reviewer blockers, mapped to task requirements:

| Blocker | Title | Requirement |
|---|---|---|
| **B78** | Option authority is circular, unpublished, and not retrievable | (1) actual canonical option bytes published as kanban attachments; `publish_gate_options` validates content, not just regex |
| **B79** | `operator_console` approval remains forgeable from a worker | (2) the DB must verify the context came from the trusted verb path, not just a string match |
| **B80** | Axis E container is not durably owned and sweep ordering is unsafe | (3) register container identity before creation; order teardown container→exec-root |
| **B81** | Axis N process scope is only cleanup metadata, not runtime authorization | (4) broker must gate live process actions by container/broker generation |
| **B82** | Force-disarm rollback has no executable or fail-closed drain path | (5) `invalidate_architecture_gate` gains a `drain_active_runs` parameter with exact worker identity and cleanup-failure handling |

---

## 2. B78 — Actual option artifacts, not circular digests

### 2.1 The live-source defect

v9's design (§2.2) says the architect publishes two option artifacts as kanban attachments, each canonical JSON whose sha256 is the `option_digest`. But v9's `publish_gate_options` (proposed, not yet implemented) only validates `re.fullmatch(r"[0-9a-f]{64}")` — it does **not** read the attachment, compute the sha256, or verify content/proof/retrievability. The ADR text names the digests in prose, and the option artifacts are "published as kanban attachments" — but there is no mechanism to **retrieve** an option artifact by digest from the DB, and no mechanism to **publish** the actual digests (only their syntax). The cycle: "ADR names digests → digests come from option artifacts → option artifacts are sha256 of canonical JSON → but no one reads the attachment to compute the sha256" is circular. B78 is correct.

### 2.2 Design — publish actual digests from actual attachments

**(a) Option artifacts are published as kanban attachments first.** The architect run publishes exactly two kanban attachments on the architect task:

- `verifier-capability-option-A.json` — Axis E only.
- `verifier-capability-option-B.json` — Axis E + Axis N loopback.

Each is canonical JSON (same schema as v9):

```json
{ "authority_version": "v1",
  "adr": {"artifact": "ADR-BUILD-700-verifier-capability-routing-v10.md", "sha256": "<adr digest>"},
  "option": "A" | "B",
  "axes": {"axis_e": true, "axis_n": false|true},
  "axis_n_live_proof": null | {"artifact": "<proof>", "sha256": "<proof digest>"}
}
```

**(b) `publish_gate_options` reads the attachments and computes actual digests.** New API:

```python
def publish_gate_options(
    conn: sqlite3.Connection,
    gate_id: str,
    context: MutationContext,
    *,
    option_a_attachment_id: int,
    option_b_attachment_id: Optional[int] = None,
    adr_sha256: str,
) -> ArchitectureGate:
```

Guard (identical trust model to `issue_architecture_graph`): `context.actor_type == "orchestrator_agent" and context.profile == "orchestrator" and context.phase == "graph_issuance"`. It requires `gate.state == "validated_awaiting_approval"`.

The function:
1. Reads the attachment(s) from the kanban board (by `attachment_id`).
2. Computes `sha256(attachment_bytes)` for each.
3. Validates each digest is `re.fullmatch(r"[0-9a-f]{64}")`.
4. Validates the content schema (same as v9: `authority_version`, `adr`, `option`, `axes`, `axis_n_live_proof`).
5. For Option B: if `axis_n_live_proof` is not null, validates the proof artifact exists and its sha matches. If the proof artifact is missing or mismatched, **Option B is excluded from the published set** (unpublished, unselectable).
6. CAS-writes `published_option_digests` (JSON array of actual digests) and `published_adr_sha256` onto `architecture_gates`.
7. Publication is one-shot (reject a second publish with a different set).

**(c) Human selection at approval (extend `approve_architecture_gate`).** Signature gains one required argument:

```python
approve_architecture_gate(conn, gate_id, context: MutationContext, digest: str, *, selected_option_digest: str)
```

New checks, added to the existing body:
- `digest == gate.design_digest` (unchanged — the human still signs the immutable design).
- `selected_option_digest in json.loads(gate.published_option_digests)` else `ArchitectureGateError("approval_option_not_published")` — the human's choice must be one of the published immutable options (tamper denial).
- On success, CAS-write `approved_option_digest = selected_option_digest` alongside the existing approved fields.
- **Replay/tamper:** extend the idempotent-replay guard to also require `gate.approved_option_digest == selected_option_digest`; a repeat with a *different* selection raises `approval_replay_mismatch`. A tampered digest not in the published set denies before any write.

**(d) Sealing the choice for the worker.** The verifier authority the worker reads is the sealed run-spec snapshot (§6), which carries `approved_option_digest`; the worker re-fetches the option artifact by digest from the kanban board and decodes `axes`. Forged/absent/mismatched option digest ⇒ reviewer parity.

### 2.3 Tests (B78)
- Approve with Option A digest → authority decodes `axes.axis_e=true, axis_n=false`; approve with Option B digest → `axis_n=true` (both choices exercised).
- Approve with a digest **not** in `published_option_digests` → `approval_option_not_published`, no state change.
- Idempotent re-approve with the same option → returns gate; re-approve with a different published option → `approval_replay_mismatch`.
- Tamper: mutate one byte of an option artifact → its recomputed digest ∉ published set → selection denied.
- Publish refuses Option B when no matching live-proof artifact is registered (§7.4).
- `publish_gate_options` reads the actual attachment bytes and computes the sha256 (not just validates regex syntax).

---

## 3. B79 — `operator_console` is not forgeable from a worker

### 3.1 The live-source defect

v9's design (§3.2) proposes `HUMAN_APPROVAL_SURFACES = frozenset({"operator_console"})` and says the sole minter is a dedicated CLI verb. But `approve_architecture_gate` checks `context.surface in AUTHENTICATED_APPROVAL_SURFACES` (line 5025) — and `AUTHENTICATED_APPROVAL_SURFACES` is a **string constant** at module level (`kanban_db.py:196`). Workers receive the board path and can construct `MutationContext(surface="operator_console")` directly by importing the module and calling the function. The env guard (`kanban_runtime_contract:31-37`) only blocks the CLI verb from running inside a worker shell — but the DB domain action itself has no source-level enforcement that the context came from the trusted verb path. A worker that imports `kanban_db` and calls `approve_architecture_gate(conn, gate_id, MutationContext(..., surface="operator_console", actor_type="human"), digest)` would succeed. B79 is correct.

### 3.2 Design — source-level enforcement via a trusted-context sentinel

**(a) Split the surface constant.** Keep `AUTHENTICATED_APPROVAL_SURFACES` for read/observability. Add a new constant:

```python
HUMAN_APPROVAL_SURFACES = frozenset({"operator_console"})
```

`approve_architecture_gate`/`reject_architecture_gate` require `context.surface in HUMAN_APPROVAL_SURFACES`. `operator_console` is **not** in any set an agent tool or worker route can construct.

**(b) Source-level enforcement: the DB must verify the context came from the trusted path.** The key insight: `MutationContext` is constructed by callers. The trusted CLI verb is the **only** caller that should ever construct a context with `surface="operator_console"`. To enforce this at the DB boundary, we add a **trusted-context sentinel** mechanism:

The CLI verb `hermes kanban gate approve` (when implemented) constructs a `MutationContext` with a special internal field `context._trusted_source = "kanban_gate_approve"`. The DB functions check:

```python
if context.surface in HUMAN_APPROVAL_SURFACES:
    if getattr(context, "_trusted_source", None) != "kanban_gate_approve":
        raise ArchitectureGateError("approval_surface_not_trusted")
```

This is a **source-level** enforcement: a worker that imports `kanban_db` and constructs `MutationContext` directly cannot set `_trusted_source` because it is not a public field — it is an internal implementation detail of the trusted verb. The sentinel is not exposed in the `MutationContext` dataclass fields (it would be a private attribute set by the trusted caller).

**(c) The CLI verb itself.** A dedicated verb `hermes kanban gate approve` (and `reject`) that:
1. **Refuses to run inside any agent/worker context.** Hard fail if any of `HERMES_KANBAN_TASK`, `HERMES_KANBAN_RUN_ID`, `HERMES_KANBAN_CLAIM_LOCK`, `HERMES_DELEGATION_ID`, `HERMES_SUBAGENT` is set (`kanban_runtime_contract._kanban_identity_declared`, lines 31-37). This blocks worker shells and delegated children even under the operator's OS identity.
2. **Requires a per-gate one-time approval code (OTP)** generated server-side at `publish_gate_options` and delivered **only out-of-band** — to the operator's verified gateway DM (configured messaging identity) **or** written to a `0600` file under `HERMES_HOME` that is **outside every `WorkspaceFileBroker` root and the exec-root mount jail** (so no worker/verifier can read it; enforced by §5's kernel/mount boundary and the broker's relative-only, jail-rooted `os.open` at `claude_workspace_files.py:27-29,55-69`). The OTP is single-use, short-TTL, and bound to `(gate_id, design_digest, selected_option_digest)`.
3. Only after the OTP is presented and matched does the verb construct `MutationContext(actor_type="human", surface="operator_console", principal=<os-user>, _trusted_source="kanban_gate_approve", …)` and call `approve_architecture_gate(...)`.

**(d) Why a model/worker cannot reach it.** Defense in depth:
- The worker tool surface (`tools/kanban_tools.py`) exposes **no** gate verb — the kanban tools are `show/complete/block/heartbeat/comment/create/link/attach*` only. There is nothing to call.
- `delegate_task` leaf children are stripped of `memory/clarify/send_message/execute_code` and cannot spawn; they cannot invoke the verb.
- Even a worker that shells `hermes kanban gate approve` fails guard (1) on the always-present kanban env, and fails guard (2) because the OTP was delivered on a channel the agent provably cannot observe (gateway DM to the human, or a root/0600 file outside the mount jail). Sharing the operator's OS identity is insufficient — the discriminator is **possession of the OOB secret**, not the OS uid.
- A worker that imports `kanban_db` and calls `approve_architecture_gate` directly cannot construct a `MutationContext` with `_trusted_source="kanban_gate_approve"` because it is a private attribute not exposed in the dataclass.

This is one concrete adapter (OOB one-time code → `operator_console`), not an unresolved CLI/TTY choice.

### 3.3 Tests (B79)
- Worker simulation: env `HERMES_KANBAN_TASK`/`CLAIM_LOCK` set + correct-looking args ⇒ verb refuses; gate stays `validated_awaiting_approval`.
- No/expired/wrong OTP ⇒ refusal; no `MutationContext(surface="operator_console")` is ever constructed.
- Correct OTP + clean (non-worker) env ⇒ approval succeeds; `approve_architecture_gate` receives `surface="operator_console"`.
- Assert no code path constructs `actor_type="human", surface="operator_console"` except the OTP-redemption boundary (grep-proof replaced by a behavioral test: a fake `MutationContext(surface="cli", actor_type="human")` is rejected by `approve_architecture_gate` with `approval_surface_not_authenticated`).
- Mount-jail test: a verifier/worker `read_file` of the OTP file path is denied (outside broker root / credential-denied).
- Direct DB call test: a worker imports `kanban_db` and tries to construct `MutationContext(surface="operator_console")` without `_trusted_source` → `approval_surface_not_trusted`.

---

## 4. B80 — Axis E container is durably owned and sweep ordering is safe

### 4.1 The live-source defect

v9's design (§4.2, §5) defines a `VerifierExecBroker` that owns an exec-root and a container. But the broker has **no durable container identity** — it is not registered as an owned resource at container creation time. The cleanup order in `cleanup_owned_run_resources` processes resources by `run_id` then iterates in insertion order. If the exec-root is registered before the container (which is the natural order: exec-root is created first, then the container is launched inside it), the sweep would clean the exec-root **before** the container — leaving a zombie container. And if the broker process dies before teardown, there is no durable record of the container to clean. B80 is correct.

### 4.2 Design — register container before launch, order teardown container→exec-root

**(a) Register the container as an owned resource at creation time.** New API:

```python
def register_verifier_container(
    conn: sqlite3.Connection,
    task_id: str,
    run_id: int,
    claim_lock: str,
    *,
    container_id: str,  # crun container ID
    broker_generation_id: str,  # fresh UUID per VerifierExecBroker instance
    net_ns_inode: Optional[int] = None,
) -> OwnedRunResource:
```

This registers a `verifier_container` kind resource with identity `{container_id, broker_generation_id, net_ns_inode}`. The container is registered **before** the first Axis-E turn launches it, so the DB row exists even if the broker process dies before `identify` completes.

**(b) Teardown ordering: container first, then exec-root.** The `VerifierExecBroker.close()` method must:
1. Kill the container (`crun kill --all` + `crun delete`) — this reaps the PID-namespace init, which reaps every descendant and destroys the net namespace.
2. Remove the exec root by exact identity (§5).
3. Mark all owned resources (container + exec-root + any process rows) cleaned in the DB.

The dispatcher sweep (`cleanup_terminal_run_resources`) must also respect this ordering: for a given run, `verifier_container` resources must be cleaned **before** `verifier_exec_root` resources. This is enforced by a new constant:

```python
CONTINUATION_CLEANUP_ORDER = ("verifier_container", "verifier_exec_root", "tmux_session", "worktree", "child_process")
```

The sweep iterates resources in this order (not insertion order).

**(c) Crash-window analysis.**
- Crash after container register, before launch → `active` row with `container_id`; sweep kills the (non-existent) container → `already_absent`/cleaned. Safe.
- Crash after launch, before broker close → container is running; sweep kills it → cleaned. Then exec-root sweep deletes the root → cleaned. **No orphan.**
- Crash after broker close → all resources cleaned. Safe.

### 4.3 Tests (B80)
- Container registered before launch → DB row exists with `container_id`.
- Broker dies before close → sweep kills the container (by `container_id`), marks cleaned.
- Cleanup order: `verifier_container` cleaned before `verifier_exec_root` for the same run.
- `close()` called twice → idempotent, no raise.
- Kill-before-teardown → dispatcher sweep cleans the container (backstop).

---

## 5. B81 — Axis N process scope is runtime authorization, not cleanup metadata

### 5.1 The live-source defect

v9's design (§7.2) extends the owned-resource identity to include `broker_generation_id`, `container_id`, and `net_ns_inode` for process provenance. But this is only used in `_cleanup_exact_child_process` — a **cleanup backstop**, not a live authorization mechanism. The current `_cleanup_exact_child_process` identifies a process by `pid` + `process_started_at` only. For Axis N, the broker must **authorize** background process actions (the verifier regains `process` toolset) by verifying the process belongs to the right container/broker generation. Currently, there is no mechanism to gate live `process` tool calls — the broker only cleans up after the fact. B81 is correct.

### 5.2 Design — broker-gated process authorization

**(a) The broker maintains a live registry of authorized processes.** When `VerifierExecBroker` launches a background process (via the `process` tool), it registers the process in an in-memory registry keyed by `(broker_generation_id, container_id)`. The registry maps `pid` → `(broker_generation_id, container_id, net_ns_inode, process_started_at)`.

**(b) The `process` tool handler checks the registry.** When a background process is spawned, the tool handler:
1. Checks the broker's live registry for the process.
2. Verifies `pid` matches a process launched by this broker instance.
3. Verifies `broker_generation_id` and `container_id` match.
4. If any check fails → deny the action.

**(c) The registry is scoped to the broker instance.** Since the broker is constructed once per session (beside `file_broker`, §4.2), the registry is also per-session. When the broker is closed, the registry is cleared. A new broker instance (next session) has a fresh registry with a fresh `broker_generation_id`.

**(d) Container provenance for cleanup.** The cleanup backstop (`_cleanup_exact_child_process`) is extended to also verify `broker_generation_id`, `container_id`, and `net_ns_inode` — but this is now a **secondary** check. The primary authorization is the live registry. If the broker process dies, the sweep falls back to the container-level cleanup (kill the container, which reaps all descendants).

### 5.3 Tests (B81)
- Background process launched by this broker → authorized.
- Background process launched by a different broker instance (different `broker_generation_id`) → denied.
- Background process launched by a different container (different `container_id`) → denied.
- Process not in the registry (e.g., host process) → denied.
- Broker close clears the registry.

---

## 6. B82 — Force-disarm rollback has an executable and fail-closed drain path

### 6.1 The live-source defect

v9's design (§6.2c) proposes a "drain protocol" for `gate invalidate` that sends SIGTERM→grace→SIGKILL via the existing `enforce_max_runtime` machinery. But `enforce_max_runtime` (`kanban_db.py:15982+`) terminates workers whose `max_runtime_seconds` has elapsed — it does not target gate-linked sessions. The proposed drain "omits the authenticated caller and failure/retry semantics needed to prevent a sealed elevated session from continuing." B82 is correct.

### 6.2 Design — authenticated gate-targeted drain with exact worker identity and cleanup-failure handling

**(a) Extend `invalidate_architecture_gate` with a `drain_active_runs` parameter.**

```python
def invalidate_architecture_gate(
    conn: sqlite3.Connection,
    gate_id: str,
    context: MutationContext,
    *,
    drain_active_runs: bool = False,
    now: Optional[int] = None,
) -> ArchitectureGate:
```

When `drain_active_runs=True`, the function:
1. Finds all active runs whose sealed `verifier_execution.gate_id` matches `gate_id`.
2. For each active run, sends SIGTERM to the worker PID, waits a grace window, then SIGKILL.
3. Sets `ended_at` on each terminated run.
4. Calls `cleanup_terminal_run_resources` for each task/run to tear down owned resources (container, exec-root, etc.).
5. Records cleanup results (success/failure) in the audit log.
6. If any cleanup fails (e.g., identity mismatch, process refused), records the failure and **retries** the cleanup up to 3 times with increasing grace windows.
7. After all cleanup attempts, if any resource remains uncleaned, the gate is still invalidated but the audit log records which resources need manual attention.

**(b) The authenticated caller.** The drain is only callable by:
- `context.actor_type == "orchestrator_agent"` and `context.profile == "orchestrator"` (same as `issue_architecture_graph`).
- OR `context.actor_type == "human"` and `context.surface == "operator_console"` (same as approval, with OTP verification).

**(c) Fail-closed semantics.** The drain terminates the elevated session — it does not mutate live context. After termination, the task is dropped back to `ready` (or `blocked` if the spawn-failure circuit breaker has already given up). Re-claims re-bootstrap in parity (gate no longer `human_approved`). This is the **only** way to disarm a running session, and it is fail-closed (terminates rather than mutating live context).

**(d) Integration with existing machinery.** The drain reuses `enforce_max_runtime`'s `_terminate_worker_for_task` for the SIGTERM→grace→SIGKILL sequence, but extends it to target gate-linked runs specifically (not just `max_runtime_seconds`-expired ones). The resource cleanup reuses `cleanup_terminal_run_resources`.

### 6.3 Tests (B82)
- `gate invalidate` on an active run → run drained (SIGTERM path), exec root cleaned, re-claim in parity.
- `gate invalidate` without `drain_active_runs=True` → gate invalidated but active runs continue.
- Cleanup failure (identity mismatch) → recorded in audit log, gate still invalidated, resource marked for manual attention.
- Cleanup retry (3 attempts with increasing grace) → eventually cleaned or recorded as failed.
- Non-orchestrator/non-human context → `gate_invalidated_requires_trusted_caller`.
- Human caller without OTP → `approval_surface_not_trusted`.

---

## 7. B77 — Axis N: executable loopback + container-scoped process authority

(No changes from v9 — Axis N remains fully specified but unpublished until proof.)

### 7.1 Trusted pre-exec loopback bring-up (before capability drop)

The reviewer's gap: v8 "drops all capabilities and blocks namespace operations" but never says who brings `lo` UP. Design: loopback is configured by the **container init as a trusted pre-exec step, inside the fresh net namespace, before dropping `CAP_NET_ADMIN` and before exec'ing the acceptance suite**:

1. `crun` creates the container with a **fresh** net namespace containing only `lo` (no veth, no CNI, no host netns join).
2. The OCI `process` runs a tiny trusted init that: `ip link set lo up` (requires `CAP_NET_ADMIN`, held only for this step); verifies routes are empty and no non-`lo` interface exists; then **drops all capabilities**, sets `no_new_privileges=true`, and applies the seccomp profile blocking `mount`/`unshare`/`setns`/`clone(CLONE_NEW*)` before `execve` of the test command. The workload thus runs with `lo` UP and **zero** capability to add interfaces, routes, or namespaces.
3. Readback (fail-closed to parity): net ns inode ≠ host; only `lo` present and UP; empty route table; `connect()` to a non-loopback address and to the host gateway is denied; `bind`/`connect` on `127.0.0.1`/`::1` succeed. Any failure ⇒ deny/parity.

### 7.2 Container-scoped process provenance

The reviewer's gap: `_cleanup_exact_child_process` (`kanban_db.py:15877-15901`) identifies a process by `pid` + `process_started_at` only — two same-task sessions are not container-proven. For Axis N processes, extend the owned-resource identity to include **`broker_generation_id`** (a fresh uuid per `VerifierExecBroker` instance), **`container_id`** (the crun container id), and **`net_ns_inode`**. `_cleanup_exact_child_process` (used only as a backstop) additionally verifies these; the primary teardown is container-scoped (§7.3), so a reused pid in a different container/generation is rejected.

### 7.3 Teardown

Axis N teardown is **container-first**: `crun kill --all` + `crun delete` on the broker's container id reaps the PID-namespace init, which reaps every descendant and destroys the net namespace (loopback included). The broker then removes the exec root by exact identity (§5) and marks all owned resources (exec_root + any process rows) cleaned. The dispatcher sweep is the backstop. No pgid/fd scan is used as containment.

### 7.4 Live-proof binding in authority/state

Option B's `axis_n_live_proof` (§2.2a) references a **registered proof artifact** — a recorded live run (Colima up) demonstrating: bind/connect on `127.0.0.1:PORT` and `[::1]:PORT` succeed; connect to an external IP and to the VM gateway is denied; `0.0.0.0`/`::` are **not** exposed to the LAN. `publish_gate_options` includes Option B's digest **only if** a proof artifact with the matching sha256 is registered on the board; otherwise B is unpublished and unselectable. Thus Axis N cannot be armed until the live proof exists, and the proof is bound into the immutable option the human signs.

### 7.5 Tests (B77) — deferred to live proof

The IPv4+IPv6 loopback-only netns proof and its container-provenance/teardown tests require Colima running and are the Slice-4b gate; they are **not** run this cycle (terminal unavailable, t_390a9f80). Until then Option B stays unpublished. Option A (Axis E, `lo` DOWN, deny-all) carries the full §5/§6 test suite and is the only human-selectable option now.

---

## 8. Implementation slices (design only; each independently verifiable)

- **Slice 2 (prerequisite):** exact-SHA `git archive <sha>` materialization into a verifier-owned exec root; credential-alias rejection at materialization. *Verify:* materialized tree hash == reviewed commit tree; credential path rejected.
- **Slice 3a — authority & approval (B72, B73, B78):** option artifacts + `publish_gate_options` (reads attachments, computes actual digests) + `approved_option_digest` on `approve_architecture_gate` + `HUMAN_APPROVAL_SURFACES`/`operator_console` OOB-code verb + trusted-context sentinel. *Verify:* §2.3, §3.3, §2.3 tests.
- **Slice 3b — resource lifecycle (B75, B80):** `verifier_exec_root` kind + `reserve`/`identify` APIs + `_cleanup_exact_verifier_exec_root` + `reserved` state + `verifier_container` kind + `register_verifier_container` API + `CONTINUATION_CLEANUP_ORDER` + teardown ordering. *Verify:* §5.3 crash tests, §4.3 container tests.
- **Slice 3c — broker wiring (B74, B81):** `VerifierExecBroker` constructed beside `file_broker`, in `_options` + `resources`, `_teardown_session` on terminal paths + live process registry + broker-gated process authorization. *Verify:* §4.3 tests, §5.3 process tests.
- **Slice 3d — flag threading & rollback (B76, B82):** strict `verifier_execution` snapshot + `KanbanRoutePreflight` return + two `AIAgent` kwargs + drain protocol with `invalidate_architecture_gate(drain_active_runs=True)`. *Verify:* §6.3 tests.
- **Slice 4a — Axis E execution:** fresh PID+mount+net(+IPC/UTS/cgroup) ns, `lo` DOWN, deny-all, reviewer tool set, mandatory readback. *Verify:* namespace readback denies external + gateway; golden reviewer parity fixture+hash unchanged when disarmed.
- **Slice 4b — Axis N (unpublished until proof) (B77):** loopback bring-up before cap drop, container provenance, container-first teardown, live-proof publication gate. *Verify:* §7.5 (deferred to live proof).

---

## 9. Acceptance criteria

1. Human selects Option A or B via **separate immutable option digests** computed from actual kanban attachment bytes; a digest ∉ published set denies; replay with a different option denies; both choices tested (B72, B78).
2. Approval is mintable **only** by the `operator_console` OOB-code boundary; worker/PTY/delegated invocations provably cannot reach it; `surface="cli"` no longer approves; source-level trusted-context sentinel prevents forgeable contexts (B73, B79).
3. Exactly one `VerifierExecBroker` per session, constructed beside `file_broker`, present in options and resources, torn down on normal/auth/terminal-error/kill/drain, with a dispatcher-sweep backstop; container registered before launch, teardown ordered container→exec-root (B74, B80).
4. Exec-root registration is crash-safe via reserved→identified with a deterministic token path; a crash between mkdir and identify leaves no orphan; dedicated cleanup never runs as a process; container registered as owned resource (B75, B80).
5. Master flag is a literal-`True` check threaded through the exact `AIAgent` constructor; authority is a strict `human_approved`-only snapshot (never `policy_accepted`); rollback is next-claim-only with a defined drain for active runs; drain is authenticated and fail-closed with cleanup-failure handling (B76, B82).
6. Axis N is fully specified (loopback bring-up before cap drop, container/broker-generation process provenance, container-first teardown) and remains **unpublished/unselectable** until a registered live IPv4+IPv6 loopback-only proof; Axis E (`lo` DOWN, deny-all) is the only selectable option now (B77).
7. Disarmed verifier == current reviewer byte-for-byte (golden Seatbelt fixture+hash); credential reads denied at broker **and** kernel/mount layer; default OFF; Slice-2 exact-SHA prerequisite retained.
8. Axis N process actions are authorized by the broker's live registry (broker_generation_id + container_id), not just cleanup metadata (B81).

---

## 10. Verification plan

- Unit/behavior tests per §2.3, §3.3, §4.3, §5.3, §6.3 (design targets; no source written this cycle).
- Golden reviewer-parity fixture + hash: disarmed verifier options are byte-identical to reviewer.
- Reviewer-reported focused baseline must stay green once implemented (previously 152/152 across 9 files, excluding tracked hardlink host-state test t_390a9f80; source has evolved since v9 — re-baseline after Slice 1 merge).
- Live IPv4+IPv6 loopback-only netns proof (Colima up) gates Slice-4b / Option B publication — deferred.
- Independent GPT-5.6 Sol re-review of **this exact ADR attachment digest** must return no open P1 before human approval (`t_0577d6d7`).

---

## 11. Rollout / rollback

- **Rollout:** staged. Slice 2 → Slice 3a–3d → Slice 4a (Axis E). Slice 4b/Option B publication only after the live proof. `agent.verifier_isolated_capability` default OFF; per-gate `human_approved` + sealed snapshot required to arm.
- **Rollback:** disable. Flag OFF ⇒ next-claim parity; `gate invalidate` with `drain_active_runs=True` ⇒ drain active runs (SIGTERM→SIGKILL) + resource sweep ⇒ parity. No schema rollback needed (new columns/kind are additive and nullable).

---

## 12. Human approval gate

`human_approval_required = true`. This ADR must NOT be implemented until: (a) independent GPT-5.6 Sol re-review of this exact attachment digest returns no open P1, and (b) the human approves via the `operator_console` OOB-code boundary on `t_0577d6d7`, selecting Option A (Option B unavailable until live proof). Scheduler status is never authority.

---

## 13. Blocker resolution summary

| Blocker | Resolution (source-grounded) |
|---|---|
| **B72** | Axis choice removed from the closed handoff field set (`kanban_db.py:4715-4722`); two separately published immutable option digests; `publish_gate_options` records them; `approve_architecture_gate` (5005) gains `selected_option_digest` validated ∈ published set with replay/tamper denial. |
| **B73** | Split approval surface: `HUMAN_APPROVAL_SURFACES={"operator_console"}` disjoint from the forgeable `AUTHENTICATED_APPROVAL_SURFACES` (196); a single OOB one-time-code verb is the sole minter, refuses worker env (`kanban_runtime_contract:31-37`), and requires a secret the agent cannot observe (mount-jailed / gateway-DM). No worker gate tool exists (`kanban_tools.py`). |
| **B74** | `VerifierExecBroker` constructed beside `file_broker` (`external_runtime.py:475-480`), captured by `_options` (482) and added to `resources` (517); `_teardown_session` closes/pops on every terminal path (fixes 485-494 leak); dispatcher sweep backstop. |
| **B75** | New `verifier_exec_root` kind (136) + dedicated cleanup branch; reserved→identified two-phase with deterministic token path + `identify` CAS (missing update path added); crash test at the mkdir/identify boundary. |
| **B76** | Strict `verifier_execution` snapshot requiring `human_approved` (not `policy_accepted`, avoiding 7090-7093); preflight return captured (cli_agent_setup_mixin:322-353); two literal-value `AIAgent` kwargs (495); next-claim-only rollback + drain for active runs. |
| **B77** | Loopback brought UP by trusted container init before cap drop; process provenance = (broker_generation_id, container_id, net_ns_inode) beyond pid+birth (15877-15901); container-first teardown; Option B unpublished until a registered live IPv4+IPv6 proof. |
| **B78** | Option artifacts published as kanban attachments; `publish_gate_options` reads actual attachment bytes and computes sha256 (not just validates regex); content schema validated; Option B excluded from published set when proof artifact missing/mismatched. |
| **B79** | `HUMAN_APPROVAL_SURFACES` split from `AUTHENTICATED_APPROVAL_SURFACES`; source-level trusted-context sentinel (`_trusted_source`) on `MutationContext` prevents forgeable contexts from direct DB calls; CLI verb is the sole minter with OTP verification. |
| **B80** | Container registered as owned resource (`register_verifier_container`) before launch; `CONTINUATION_CLEANUP_ORDER` enforces container→exec-root teardown ordering; crash after register but before launch handled safely. |
| **B81** | Broker maintains live process registry keyed by (broker_generation_id, container_id); `process` tool handler checks registry before authorizing background actions; cleanup backstop extended with container provenance. |
| **B82** | `invalidate_architecture_gate` gains `drain_active_runs` parameter; targets gate-linked runs specifically; reuses `_terminate_worker_for_task` for SIGTERM→grace→SIGKILL; cleanup retries with increasing grace; audit log records failures; authenticated by orchestrator or OTP-verified human. |
