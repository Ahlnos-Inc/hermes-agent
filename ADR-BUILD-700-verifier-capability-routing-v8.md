# ADR BUILD-700 — Verifier capability routing (v8)

- Status: DESIGN ONLY — proposed, awaiting independent GPT-5.6 Sol re-review then human approval. No source changed; no human approval claimed.
- Version: v8. Supersedes v7 `sha256:89a84b3052abad9e7d1a68c2e16829a4e2dfadcc1a1887ad64fb3de75e4c7cc1`.
- Source of truth: re-derived from live source at HEAD `9665015f2bb77a88cae398900f1be53c648c29c5` (`main`, dirty only by this untracked ADR), not from the v7 text.
- Incident: BUILD-700. Human approval card: `t_0577d6d7`. Independent Sol re-review card: `t_cd03338f`.
- Resolves reviewer P1 blockers **B67, B68, B69, B70, B71**. Preserves every invariant the reviewer accepted through v7 (bare-Darwin Seatbelt `(deny network*)`, byte-for-byte reviewer parity, no coder fallthrough, no `write_file`, Axis E has no `process`/background, Axis N OFF until proof, Slice-2 exact-SHA `git archive` prerequisite).

> Authority note (binds this whole document): approval authority is the architecture-gate
> `design_digest` computed by `kanban_db.canonicalize_architecture_handoff` +
> `architecture_handoff_digest` from the architect's completion metadata — **not** this file's
> prose, **not** any approval-card status/comment, and **not** this file's incidental byte hash.
> The design (chosen_approach / slices / acceptance_criteria / verification_plan / rollout /
> rollback) plus the **validated structured `verifier_execution_authority`** field (§B68) are
> encoded in the canonical handoff, so the gate `design_digest` transitively binds both the design
> and the approved-axes decision. The ADR file hash is a companion integrity reference bound *inside*
> that structured field; it is not itself the authority.

---

## 0. What changed since v7 (blocker-by-blocker)

v7's direction was accepted (two axes on a kernel boundary; a real orchestrator gate sequence; a
first-class `verifier_exec_root` resource with an `active→cleaning→cleaned` machine and a DB
completion gate; a strict-bool flag + sealed RunSpec). Sol's v7 verdict was **MODIFY** with five
P1s. v8 makes each mechanically implementable against the real code and removes every claim the
kernel or the current caller graph cannot back.

| Blocker | v7 defect | v8 correction (section) |
|---|---|---|
| **B67** | Axis E ran in a **PID namespace** and called network "deny-all (no configured interfaces)". A PID+mount namespace still shares the **host network namespace**: the test can reach host `localhost` services, the VM gateway, and the LAN. PID-ns containment does **not** imply network isolation. | §E1: Axis E runs in **fresh PID + mount + network (+ IPC/UTS/cgroup) namespaces**. The net ns has **only `lo`, left administratively DOWN**. No `veth`, no NAT, no host networking. Exact launcher (`crun` OCI runtime inside the Colima VM), exact privileges/capabilities, and a mandatory **namespace readback**: deny (fail closed to reviewer parity) if any namespace is missing/host-shared or any interface/route is present. |
| **B68** | Which axes were approved lived in ADR **prose** (Option A vs B) and a lone `axis_n` bool; the digest did not structurally bind the approved-axes decision, and the strict flag was under-specified. | §B68: the canonical handoff carries a **validated structured `verifier_execution_authority`** object (approved axes as typed bools + ADR artifact `{path, sha256}`), hashed into `design_digest` — no prose parsing. A **separate strict-bool config** decision is threaded through preflight → AIAgent → external_runtime → claude_sdk_session. Missing/unknown/malformed/**stale** (digest/epoch mismatch) authority, or a non-literal-`True` flag, produce exact verifier=reviewer parity at both verifier and reviewer. |
| **B69** | v7 §A described `open/approve/issue` DB functions as an implementable workflow, but **no authenticated production caller invokes them** (`hermes kanban` exposes no gate verb; nothing calls `approve_architecture_gate` from a human surface or `issue_architecture_graph` from an orchestrator surface). A DB function with no caller is not a workflow. | §B69: design the **executable authenticated callers** on the *current supported* `create_task` / `kanban_create` / `open_architecture_gate` path — a new authenticated `hermes kanban gate {open,approve,reject,issue}` verb family with actor/surface attestation, and trusted-orchestrator graph issuance wired into the existing task-creation path. Bind the exact ADR artifact SHA and the selected axis authority into the approved handoff. |
| **B70** | The exec-root lifecycle lived only as DB rows (§C). There was **no session-owned component** that actually creates/materializes the root, launches and owns the VM namespace init, scopes tool calls to it, and tears it down on every exit path (incl. auth reset / session close), nor a defined crash-window reconciliation. | §B70: a first-class **`VerifierExecBroker`** — a `ClaudeAgentSdkSession` resource (peer of `WorkspaceFileBroker` / `WorkerProcessBroker`) — that owns exec-root creation/materialization, VM container init launch/ownership, birth-safe identity registration, foreground/background/`process` scoping, and teardown on normal exit / error / kill / timeout / auth reset / session close, with an explicit crash matrix (including crash **after** successful deletion but **before** the DB state update). |
| **B71** | Confinement leaned on **file-broker credential denial**, which only governs the `read_file`/`write_file` MCP tools. The `terminal` tool runs a **raw shell**; a shell can `cat` any path the mount namespace exposes. Denial must be at the kernel/mount layer, not the broker. | §B71: a **minimal non-root Linux mount/env/fd/capability boundary** — read-only toolchain mounts + writable exec-root/runtime scratch only; **no** host home/profile/config/keychains/SSH/credential sockets/Docker/Colima sockets; sanitized env; closed inherited fds; `no_new_privileges`; dropped caps; no mount/ns escape. Credential paths/aliases rejected during archive materialization, and terminal-level credential reads are proof-tested in source, export, and exec root. |

---

## 1. Problem (recap, source-anchored)

Two layers, both at HEAD `9665015f2`:

- **L1 — introspection misclassification** (Slice 1, low-risk, unchanged, outside this gate): the
  read-only terminal rejects benign version/help probes. Ships under standard review. Not
  re-litigated here.
- **L2 — verifier cannot verify**: `agent/claude_sdk_session.py:25`
  `_READ_ONLY_WORKER_PROFILES = frozenset({"reviewer", "verifier"})` pins `verifier` to the exact
  reviewer capability: worker tool set `{terminal, read_file}` (`_mcp_tool_names`, `:62-66`),
  credential-denied file broker (`build_claude_agent_options` `:131-137`), no `process`, and — for
  the model-callable terminal — `dispatch_read_only_workspace_terminal`
  (`claude_workspace_terminal.py:972-1013`) running a **read-only mirror** of source under a
  `(deny network*)` Seatbelt profile (`build_workspace_seatbelt_profile:353-370`; background rejected
  at `:993-994`). A verifier therefore cannot obtain the exact target commit in an owned checkout,
  run an acceptance suite, or probe a service it starts.

Goal of Slices 2–4: let a **verifier** (and only a verifier), **only when a human has approved this
exact design + axes**, run the acceptance suite against the reviewed commit inside a disposable,
owned, kernel-contained environment — while `reviewer` stays byte-for-byte unchanged and
source-under-review stays immutable.

---

## 2. Model: two axes on one kernel boundary (unchanged intent, corrected mechanism)

Both axes execute inside the **Linux VM** (Colima/Lima). Bare Darwin gives a non-root process no
inescapable process- **or** network-ownership boundary, so **neither axis runs on bare Darwin**; on
bare Darwin the verifier fails closed to exact reviewer parity (§B68/§E).

- **Axis E (execution/process containment).** Materialize the exact reviewed commit into a
  verifier-owned disposable **exec root**, then run the acceptance suite **synchronously** inside
  **fresh PID + mount + network namespaces** whose init is the reaper. The network namespace has
  **only `lo`, left DOWN** — deny-all, no egress, no host reachability. Tool surface is exactly
  reviewer's `{terminal, read_file}` — **no `process`, no `write_file`, no background, no egress**.
  The only difference from reviewer is the terminal *dispatch target*.
- **Axis N (loopback network).** An *additive* grant on Axis E: inside the **same fresh network
  namespace**, bring `lo` **UP** so a service the verifier starts on `127.0.0.1`/`::1` is reachable
  **only inside the namespace** (still no `veth`, no NAT, no external egress), plus `process` for
  background server lifecycle. **Default OFF and unarmed until a live IPv4+IPv6 loopback-only netns
  proof with Colima running.** Remote acceptance criteria are delivered as task evidence, never
  fetched.

Decision requested from the human gate (encoded structurally, §B68):

- **Option A (recommended interim): approve Axis E only** → `verifier_execution_authority.axes =
  {"axis_e": true, "axis_n": false}`.
- **Option B: approve Axis E + Axis N** → `{"axis_e": true, "axis_n": true}`; Axis N arming still
  additionally requires the live IPv4+IPv6 netns proof (Colima currently **stopped** → unproven).

No forced synthesis: A over B (or neither) is a clean supported terminal state — the structured
`axes` object simply records the human's choice and Axis N stays unarmed.

---

## B69. Authorization: real, executable, authenticated callers (resolves B69)

### B69.1 Current fact (acknowledged, corrected from v7)

The `architecture_gates` table and the authority chain functions exist in
`hermes_cli/kanban_db.py` (`open_architecture_gate`, `approve_architecture_gate`,
`issue_architecture_graph`, plus `_accept_architecture_handoff_in_txn` reached from `complete_task`).
**But there is no authenticated production caller of the two human-/orchestrator-authoritative
steps.** `hermes_cli/kanban.py` (`build_parser`) exposes **no** gate subcommand — no `open`, no
`approve`, no `reject`, no `issue`. No CLI verb, model tool, gateway command, or dispatcher path
invokes `approve_architecture_gate` from an `actor_type == 'human'` surface or
`issue_architecture_graph` from an `actor_type == 'orchestrator_agent'` surface. v7 described these
functions *as if* they were an implementable workflow; they are library primitives with no
executable front door. That is B69, and it is a real caller gap, not wording. **The only currently
executable, authenticated authority-relevant primitives are `create_task`/`kanban_create` (task
issuance) and the handoff acceptance reached transitively through `complete_task`.**

### B69.2 The fix — build the authenticated caller surface on the supported path

Design a new authenticated `hermes kanban gate` verb family in `hermes_cli/kanban.py`, delegating to
the existing `kanban_db` primitives, plus a trusted-orchestrator issuance step wired onto the
existing `create_task`/`kanban_create` path. Every step carries an explicit
`ApprovalContext`/`OrchestratorContext` (actor_type, surface, principal) so the DB primitives'
existing guards actually receive an authenticated caller instead of never being called.

1. **`hermes kanban gate open`** (orchestrator/front-door lane). Wraps
   `open_architecture_gate(conn, architect_task_id=T_arch, context, mode='enforce')`; the context is
   built from the dispatcher-attested `creator_principal`/`session_id`/`workflow_key`. Establishes the
   gate row (`state='open'`) bound to `T_arch` before the architect runs; the unique partial index
   `idx_architecture_gates_active_scope` keeps one active gate per scope.
2. **Architect completes with the canonical handoff.** This lane completes `T_arch` with version-1
   handoff metadata (`role='architect'`, `design_depth='formal'`, `chosen_approach`,
   `alternatives_rejected`, `slices`, `acceptance_criteria`, `verification_plan`,
   `human_approval_required=true`, `rollout`, `rollback`) **plus the validated structured
   `verifier_execution_authority`** field (§B68). `complete_task` atomically accepts the handoff via
   `_accept_architecture_handoff_in_txn`, canonicalizes it, computes
   `design_digest = architecture_handoff_digest(...)`, and — because `human_approval_required` — moves
   the gate to `validated_awaiting_approval`. A malformed handoff rolls completion back and keeps the
   architect run alive to correct.
3. **`hermes kanban gate publish`** (orchestrator). Reads the real `gate_id`/`design_digest` via
   `get_architecture_gate_for_task(conn, T_arch)` and surfaces them onto the human card `t_0577d6d7`.
   This is the value the human approves — not ADR prose.
4. **`hermes kanban gate approve` / `hermes kanban gate reject`** (authenticated **human** surface —
   the executable front door B69 requires). `approve` calls
   `approve_architecture_gate(conn, gate_id, human_context, design_digest)`; `reject` calls the
   symmetric denial transition. The command MUST construct `human_context` with
   `actor_type == 'human'` and `surface ∈ AUTHENTICATED_APPROVAL_SURFACES`, and MUST refuse to run
   from an agent/worker principal. The design constraint (open item for the security reviewer):
   `AUTHENTICATED_APPROVAL_SURFACES` must be reachable **only** through an authenticated operator
   entrypoint (interactive TTY confirmation + operator principal, or the dashboard's authenticated
   session token), never through the model tool surface or a dispatcher-spawned worker. The DB
   already enforces `context.actor_type == 'human'`, gate state `validated_awaiting_approval`, and
   `digest == gate.design_digest`; this verb is what finally *calls it* with a real human context.
   Exact re-submission is idempotent; every other replay denies.
5. **`hermes kanban gate issue`** (trusted **orchestrator** surface, on the `create_task` path).
   Post-approval the orchestrator lane (`actor_type == 'orchestrator_agent'`,
   `profile == 'orchestrator'`, `phase == 'graph_issuance'`) calls
   `issue_architecture_graph(conn, gate_id, context, tasks, idempotency_key=...)`, which requires
   `gate.state == 'human_approved'` and, in one transaction, creates the implementation + verifier
   tasks (the same insert path `create_task`/`kanban_create` use) and records
   `architecture_graph_issuances(gate_id, task_ids, ...)`. The **verifier task is created here**; no
   task-keyed capability exists before this point — which is exactly why an approval-card grant is
   impossible and why issuance, not the approval card, mints the verifier.

### B69.3 Gate→verifier linkage (no new column)

`get_verifier_execution_gate(conn, task_id)` resolves the governing gate for a verifier task by
looking up the `architecture_graph_issuances` row whose `task_ids` JSON contains `task_id`, then
loading that `gate_id`. Reuses the issuance ledger written in step 5; **zero DDL** for linkage
(mirror of the existing `get_delivery_architecture_gate` used in `complete_task`).

### B69.4 Binding the exact ADR artifact SHA + selected axis authority

The approved handoff must bind (a) the exact ADR bytes and (b) the human-selected axes, so a
downstream implementer can prove "the approved gate corresponds to exactly this ADR and these axes."
Both live in the **validated structured** field (§B68), so both are hashed into `design_digest`:

```
"verifier_execution_authority": {
  "version": 1,
  "adr_artifact": {"path": "ADR-BUILD-700-verifier-capability-routing-v8.md",
                   "sha256": "<64-hex of the attached v8 file>"},
  "axes": {"axis_e": true, "axis_n": <human choice, default false>}
}
```

The ADR file never contains its own hash (impossible); the hash is computed on the attached file and
placed here in the handoff metadata. Because this field is in the canonicalization `allowed`/hashed
set, the ADR bytes and the axis decision are transitively bound into the authority the human signs.

---

## B68. Structured authority + strict-bool config, threaded and fail-closed (resolves B68)

Two independent keys must both hold to route a verifier into Axis E; either missing / malformed /
unknown / **stale** → verifier routed **exactly** as reviewer (fail-closed). Keys are separated so an
approved gate cannot elevate a deployment whose operator did not enable the master switch, and the
master switch cannot elevate without an approved gate.

### B68.1 Key 1 — validated structured authority (not prose)

- **Canonicalization.** Extend `canonicalize_architecture_handoff` `allowed`/`required` sets with the
  `verifier_execution_authority` object above and **validate it structurally**: `version == 1`;
  `adr_artifact.path` a non-empty relative path; `adr_artifact.sha256` matches `^[0-9a-f]{64}$`;
  `axes` a dict of exactly `{axis_e: bool, axis_n: bool}` with `axis_e is True` whenever the field is
  present (a gate that authorizes nothing must omit the field entirely). Any deviation raises → the
  handoff is malformed → `complete_task` rolls back (no digest minted). This makes the approved axes
  **validated structured data hashed into `design_digest`**, never prose parsed from the ADR.
- Because the domain of `design_digest` changes, gate it behind
  `ARCHITECTURE_GATE_CANONICALIZATION_VERSION` so pre-existing gates are unaffected.

### B68.2 Key 2 — master config flag (strict `is True`)

Add to `DEFAULT_CONFIG["agent"]` (`hermes_cli/config.py`, the `agent` block that begins at `:993`):

```python
# Master switch for the BUILD-700 verifier isolated-execution capability.
# Strict bool: only literal True arms it. Absent/"true"/1/"1"/None stay OFF.
"verifier_isolated_capability": False,
```

Read once at worker bootstrap via `cfg_get("agent.verifier_isolated_capability")` and gated with
**identity** `resolved is True` (not truthiness): a YAML string `"true"`, int `1`, or any non-bool
cannot arm it. This is the local, per-deployment master switch, **independent** of the gate.

### B68.3 Key 3 — sealed `run_spec_json.verifier_execution` (requires fresh `human_approved`)

The immutable per-run contract is `task_runs.run_spec_json`, built by `_build_run_spec` (current keys
`version, profile, requested_route, toolsets, delivery_policy`). Add a **new, additive** key
`verifier_execution` alongside `delivery_policy` (no DDL; legacy NULL/absent runs degrade to
`disposition:"none"`).

- **New builder `_verifier_execution_snapshot(gate)`** — modeled on `_delivery_policy_snapshot` but
  with the security-critical difference that it treats **only** `gate.state == 'human_approved'` as
  authorizing (it must **not** reuse `_delivery_policy_snapshot`, whose `enforcing_approved`
  disposition deliberately treats `policy_accepted` and `human_approved` as equivalent — safe for
  delivery, unsafe here). It **reads the approved axes from the gate's accepted
  `verifier_execution_authority`**, not from config or prose. Shape:
  - not armed → `{"version":1,"disposition":"none","gate_id":None,"design_digest":None,"axis_e":False,"axis_n":False}`
  - armed → `{"version":1,"disposition":"human_approved","gate_id":...,"design_digest":<gate.design_digest>,"axis_e":True,"axis_n":<from authority.axes>}`, produced **only** when the task is a verifier task, its governing gate (§B69.3) is `human_approved`, the accepted handoff carried `verifier_execution_authority.axes.axis_e is True`, and `gate.enforcement_mode ∈ ARCHITECTURE_GATE_ENFORCING_MODES`.
- **New validator `validate_verifier_execution_snapshot(value)`** — `version == 1`; `disposition ∈
  {"none","human_approved"}`; for `none`, all authority fields `None`/`False`; for `human_approved`,
  `gate_id`/`design_digest` present, `design_digest` matches `^[0-9a-f]{64}$`, `axis_e is True`,
  `axis_n` a bool. Any deviation raises → treated as disarmed.
- **Staleness (B68 "stale authority").** At verifier **claim** time, re-resolve the governing gate
  and require `live_gate.state == 'human_approved'` **and** `live_gate.design_digest ==
  spec.verifier_execution.design_digest`. If the gate was re-opened, re-approved with a new digest,
  or superseded (epoch advance), the sealed snapshot is **stale** → disarm → `VERIFIER_READ_ONLY`.
  This binds the run to the exact digest the human signed, not merely to "some approved gate."

### B68.4 Threading path (snapshot once at SDK-session bootstrap; cache-safe)

Exact seams, in order, all fail-closed:

1. **Preflight (bootstrap).** `hermes_cli/cli_agent_setup_mixin.py` already calls the kanban route
   preflight, which loads the sealed spec. Extend it to compute `verifier_execution =
   validate_verifier_execution_snapshot(spec.get("verifier_execution"))` **and** the staleness
   recheck (§B68.3), and to read `agent.verifier_isolated_capability` with `is True`. Any
   raise/mismatch → disarmed. This runs **before any AIAgent/provider/client construction**, so a
   mismatch produces no provider side effects.
2. **AIAgent carries the decision.** Store the validated, de-staled decision once on the agent
   (`agent._verifier_execution`) at bootstrap. Because `run_spec_json` is immutable and loaded once,
   this is a single snapshot for the session's life — cache-safe, no mid-session mutation, no toolset
   swap.
3. **External runtime closure.** `agent/external_runtime.py` `_options(resume)` factory (`:422-448`)
   passes `worker_profile` (`:415`) into `build_claude_agent_options`; add an explicit
   `verifier_execution=getattr(agent, "_verifier_execution", None)` argument. The
   `ClaudeAgentSdkSession` is constructed once (`:451-463`) so the closure captures the decision once.
4. **SDK routing.** In `agent/claude_sdk_session.py`, replace the boolean `_is_read_only_worker`
   (`:28-32`) with `_resolve_worker_capability(capability_mode, worker_profile, verifier_execution,
   master_flag)` returning one of:
   - `REVIEWER_READ_ONLY` — `worker_profile == 'reviewer'`.
   - `VERIFIER_READ_ONLY` — `worker_profile == 'verifier'` **and not** (`master_flag is True` **and**
     `verifier_execution.disposition == 'human_approved'` **and** `axis_e is True`). Fail-closed
     default; byte-for-byte reviewer parity.
   - `VERIFIER_ISOLATED_EXEC` — `worker_profile == 'verifier'` **and** both keys hold. Axis N
     sub-capability armed only additionally when `verifier_execution.axis_n is True` **and** the live
     netns proof gate is satisfied (default OFF).
   `_mcp_tool_names` (`:62-66`): `REVIEWER_READ_ONLY` and `VERIFIER_READ_ONLY` both →
   `{terminal, read_file}`. `VERIFIER_ISOLATED_EXEC` (Axis E) → also `{terminal, read_file}` (no
   `process`/`write_file`); `process` is added **only** under armed Axis N.
5. **File broker.** `deny_credential_reads` stays `True` for `verifier` in **all three** modes.
   Generalize the `_is_read_only_worker` usage at `claude_sdk_session.py:131-137` and the duplicate at
   `external_runtime.py:416-420` to `_denies_credentials(worker_profile) = worker_profile ∈
   {"reviewer","verifier"}`, independent of exec mode. (Necessary but **not sufficient** — see §B71.)
6. **Terminal override.** `_transform` (`:141-152`) and `handler_overrides` (`:161-188`): add the
   `VERIFIER_ISOLATED_EXEC` branch routing `terminal` to the **`VerifierExecBroker`** (§B70) against
   the owned exec root inside the fresh namespaces; `REVIEWER_READ_ONLY`/`VERIFIER_READ_ONLY` keep the
   existing `dispatch_read_only_workspace_terminal` override unchanged.

### B68.5 Fail-closed truth table

| master flag `is True` | sealed `verifier_execution` | staleness | verifier route |
|---|---|---|---|
| any | absent / NULL (legacy) | — | `VERIFIER_READ_ONLY` (parity) |
| any | malformed / unknown disposition | — | `VERIFIER_READ_ONLY` |
| any | `disposition == 'none'` | — | `VERIFIER_READ_ONLY` |
| any | `human_approved` | live gate not `human_approved` **or** digest mismatch | `VERIFIER_READ_ONLY` |
| `False`/absent/`"true"`/`1` | `human_approved` | fresh | `VERIFIER_READ_ONLY` |
| `True` (identity) | `human_approved`, `axis_e True` | fresh, digest match, VM PID+mount+net-ns proof green | `VERIFIER_ISOLATED_EXEC` (Axis E) |

`reviewer` is never affected by any cell (byte-for-byte parity is an invariant, §Invariants).

---

## B70. Session-owned execution broker: `VerifierExecBroker` (resolves B70)

### B70.1 Component & placement

Add `agent/verifier_exec_broker.py::VerifierExecBroker`, a **session-owned resource** constructed in
`build_claude_agent_options` (only under `VERIFIER_ISOLATED_EXEC`) and passed to
`ClaudeAgentSdkSession(resources=[file_broker, verifier_exec_broker, ...])`. It is a peer of
`WorkspaceFileBroker` and `WorkerProcessBroker` (`agent/claude_process_scope.py`) and, like them,
holds the trusted identity in a closure the model's arguments never carry. It exposes:

- `dispatch_verifier_exec_terminal(arguments)` — the Axis E foreground terminal handler wired into
  `handler_overrides["terminal"]` (§B68.4 step 6).
- `process` scoping (Axis N only) — a `WorkerProcessBroker`-style handler bound to processes launched
  **inside** the container's namespaces, refusing any session not born under this broker's container.
- `close()` — idempotent teardown, called by `ClaudeAgentSdkSession.close()` (which iterates
  `self._resources` and calls `.close()` on each, `claude_sdk_session.py:251-257`).

### B70.2 Owned lifecycle (birth-safe order)

The broker owns creation → materialization → launch → scoping → teardown, and registers the durable
DB resource (§C `verifier_exec_root`) **before** any destructive step so the recovery sweep can
always find and clean a partial state:

1. **Reserve deterministic root.** `scratch_base = get_hermes_home()/"cache"/"claude-agent-sdk"/
   "verifier-exec-runs"` (outside the source workspace — same invariant as
   `dispatch_read_only_workspace_terminal:999-1004`), `root_path = scratch_base/f"run-{run_id}"`.
2. **Register resource (`active`) FIRST.** `register_owned_run_resource(..., kind='verifier_exec_root',
   cleanup_policy='on_terminal')` with identity `{root_path, scratch_base, vm_context}` and a
   placeholder for `dev/inode`/`container_id`/`net_ns`; owner bound to `(run_id, task_id, claim_lock)`
   via `task_runs`. A crash before this point left nothing on disk; after it, the sweep owns cleanup.
3. **Create + stamp identity.** `mkdir(root_path, 0700)`, then update the resource identity with
   `st_dev`/`st_ino` (content-addressed via the existing `content_digest`).
4. **Materialize (Slice-2 prerequisite).** Exact-SHA `git archive <sha> | tar -x` into `root_path`
   (no `.git`/`config`/`alternates`/`remotes`/pointer files), pre-run tree-digest integrity assert,
   reject regular-file Git config/alternate credential aliases and escaping symlinks. `shutil.copytree`
   of the working tree is rejected. (§B71 adds the credential-path rejection during this step.)
5. **Launch + own the VM namespace init.** Start the `crun` container (§E1) whose PID-1 init is the
   reaper; record `container_id`, init `pid`, and the container's `net_ns` inode into the resource
   identity. The broker holds the only authoritative handle to this container.
6. **Scope tool calls.** Foreground `terminal` (Axis E) and, under Axis N, background/`process` calls
   execute **inside** this container/namespaces only. `process` sessions carry `owner_task_id` and are
   refused if not born under this broker (reuse `WorkerProcessBroker`'s ownership check).

### B70.3 Teardown triggers (all exit paths)

`close()` (idempotent) tears down on **every** path:
- **Normal turn/session end** — `ClaudeAgentSdkSession.close()` iterates resources.
- **Error / kill / timeout** — attempt-budget `AttemptDeadlineExceeded` and any run-turn exception
  route through the session's resource cleanup; the broker also registers an `atexit`/finally guard.
- **Auth reset** — `external_runtime._clear_auth_state()` (`:465-472`) already pops and `close()`s the
  failed session; because the broker is a session resource, its teardown runs there too.
- **Session close** — explicit `close()`.

Teardown sequence: **kill the container init** (kernel reaps all namespace descendants — daemonized /
`setsid` / double-forked children cannot escape the PID namespace), then `shutil.rmtree(root_path)`,
then **verify absence**, then CAS the DB row `cleaning → cleaned`. See §C for the DB state machine and
completion gate.

### B70.4 Crash-window reconciliation matrix

| Crash point | On-disk state | DB row state | Recovery |
|---|---|---|---|
| before step 2 | nothing | none | nothing to clean |
| between 2 and 3 | none | `active`, no dev/inode | sweep: `rmtree` no-op → absence → `cleaned` |
| during materialize (4) | partial tree | `active` | sweep: kill (if any) + `rmtree` idempotent → absence → `cleaned` |
| container running (5/6) | full tree, live init | `active` | sweep: identity-check, kill init, `rmtree`, verify → `cleaned` |
| mid-`rmtree` after `cleaning` CAS | partial tree | `cleaning` | sweep re-picks `cleaning`; `rmtree` idempotent → absence → `cleaned` |
| **after successful delete, before DB CAS** | **absent** | **`cleaning`** | sweep re-picks `cleaning`; re-`lstat` shows absence → CAS `cleaning → cleaned` (deletion already done; idempotent) |
| identity mismatch (path swap/symlink) | foreign inode | `active`/`cleaning` | `identity_mismatch`, **no deletion**, blocks completion |

The "successful delete before DB update" row is the specific window B70 calls out: because `cleaned`
is only ever reached **after a re-`lstat` proves absence**, a crash there is safe — the sweep observes
absence and completes the CAS. `cleaned` therefore always implies proved absence, and the DB
completion gate (§C.4) requires `cleaned` for every required `verifier_exec_root`.

---

## C. Owned exec-root resource lifecycle & completion gate (preserved from v7; now broker-driven)

### C.1 Current fact (acknowledged)

`CONTINUATION_RESOURCE_KINDS = {"tmux_session", "worktree", "child_process"}`
(`kanban_db.py:136`). No `verifier_exec_root` kind, so `register_owned_run_resource` raises
`unknown resource kind`; the cleanup dispatcher `cleanup_owned_run_resources` has a `child_process`
**catch-all `else`** that would mistreat a new kind (the B65 hazard); there is **no `cleaning`
state**; the sweep `cleanup_terminal_run_resources` re-picks **only** `state='active'`; and
`complete_task` never checks that owned resources were cleaned.

### C.2 New resource kind + identity

- Add `"verifier_exec_root"` to `CONTINUATION_RESOURCE_KINDS` (`:136`).
- Identity JSON (content-addressed by the existing `content_digest`): `{"root_path": <abs>,
  "dev": <st_dev>, "inode": <st_ino>, "scratch_base": <abs>, "vm_context": <colima|lima profile>,
  "container_id": <opaque>, "net_ns": <inode>}`. `root_path` absolute and a strict subpath of
  `scratch_base` (outside the source workspace). Registered with `cleanup_policy='on_terminal'`;
  owner bound to `(run_id, task_id, claim_lock)` via `task_runs` — no path/session-name inference is
  authority.

### C.3 State machine: `active → cleaning → cleaned`

Extend the state domain to `{active, cleaning, cleaned, identity_mismatch, cleanup_failed}` and add a
`verifier_exec_root` cleanup that is crash-recoverable and only marks `cleaned` after verified
deletion:

1. **Containment check (before any deletion).** `lstat(root_path)`; require absolute; strict subpath
   of `scratch_base`; not a symlink; `st_dev == identity.dev` **and** `st_ino == identity.inode`. Any
   mismatch → `identity_mismatch`, `cleanup_error` set, **no deletion**, treated as *not cleaned*.
2. **Enter `cleaning`.** In a `write_txn`, CAS `active → cleaning`, stamp `cleanup_started_at`.
3. **Tear down, then delete.** Kill the container init (kernel reaps all, §E1), then
   `shutil.rmtree(root_path)` (idempotent on a partially-deleted tree).
4. **Verify absence, then `cleaned`.** Re-`lstat`; only if `not root_path.exists()` CAS
   `cleaning → cleaned`, `cleaned_at=now`; else `cleanup_failed`.
5. **Dispatch explicitly (no catch-all).** `cleanup_owned_run_resources` gets an explicit
   `elif resource.kind == 'verifier_exec_root': _cleanup_exact_verifier_exec_root(...)`; the final
   `else` **raises/refuses** an unknown kind rather than defaulting to `child_process`.

### C.4 Interrupted-cleanup recovery + completion gate

- **Recovery sweep.** `cleanup_terminal_run_resources` re-picks `verifier_exec_root` rows in state
  `active` **or** `cleaning` **or** `cleanup_failed` for runs with `ended_at IS NOT NULL`.
- **Immediate cleanup in the completion path.** `complete_task` drives
  `cleanup_owned_run_resources(conn, task_id, run_id)` before the status CAS.
- **DB completion gate (new).** Inside the `complete_task` write transaction (beside the
  `open_critical_continuation_blockers` guard), after cleanup, query for the run's
  `verifier_exec_root` rows where `state != 'cleaned'`. If any exist → append a `completion_blocked`
  event `{"reason":"owned_verifier_exec_root_not_cleaned","resource_ids":[...]}` and `return False`.
  This puts the destructive-resource guarantee in the DB completion kernel, so CLI, model-tool, and
  any future writer are all bound. **DB completion requires proved absence (`cleaned`).**

---

## E. Kernel boundary: fresh PID + mount + network namespaces (resolves B67)

### E1. The boundary and the exact launcher

Axis E execution runs inside a **`crun` OCI container** created in the **Colima/Lima Linux VM**
(rootless is acceptable inside the VM; the broker uses the VM's container runtime, not the macOS
host). The OCI runtime config creates **fresh** namespaces:

- **PID namespace** — init (PID 1) is the reaper; `SIGKILL` to init kernel-kills every descendant
  including daemonized / re-parented / `setsid` children (reparenting stays *inside* the namespace).
- **Mount namespace** — private propagation; only the mounts in §B71 are visible.
- **Network namespace** — **fresh and empty except `lo`**, and **`lo` is left administratively DOWN**
  for Axis E. **No `veth` pair, no bridge, no NAT, no host networking.** This is the correction to
  B67: a PID/mount namespace shares the host net ns unless net is *also* unshared, so v8 unshares net
  and asserts it.
- **IPC + UTS + cgroup namespaces** — fresh, to prevent SysV IPC / hostname / cgroup escape signals.

Privileges/capabilities: run as a **non-root** uid inside a **user namespace**, drop **all**
capabilities (`capabilities: {bounding/effective/permitted/inheritable: []}`), `no_new_privileges:
true`, a default-deny **seccomp** profile that additionally blocks `mount`, `unshare`, `setns`,
`pivot_root`, `ptrace`, and `bpf` (no mount/ns escape), read-only rootfs, `maskedPaths`/`readonlyPaths`
for `/proc` sub-nodes. No `CAP_SYS_ADMIN`, so the confined process cannot create/join namespaces or
mount.

### E2. Mandatory namespace readback (fail closed)

Immediately after launch and **before** dispatching any acceptance command, the broker reads back and
**asserts**, denying (tear down + fall closed to `VERIFIER_READ_ONLY` parity) on any failure:

- **Namespaces are fresh & not host-shared.** `readlink /proc/<init_pid>/ns/{pid,mnt,net,ipc,uts}` all
  differ from the host/VM-init namespace inodes.
- **Network is empty & down.** Inside the net ns, the only interface is `lo`; `lo` is **DOWN**; the
  routing table is empty; no default route; no addresses beyond the unconfigured `lo`. (Axis E.)
- **No egress.** A probe connect to an external address and to the VM gateway both fail (no route).
- **Confinement holds.** `capsh`/`/proc/<pid>/status` show no capabilities; `NoNewPrivs: 1`; the
  credential-mount denials of §B71 are present (a read of a would-be credential path fails).

If any namespace setup or readback fails, the run is **not** elevated — it does not silently fall back
to host networking; it falls back to exact reviewer parity. **No claim that PID-namespace membership
implies network isolation is made anywhere in the design.**

### E3. Axis N (deferred, Option B, default OFF)

Axis N reuses the **same** fresh network namespace and only additionally brings **`lo` UP** (still no
`veth`/NAT/egress) so services on `127.0.0.1`/`::1` are reachable **inside** the namespace, and adds
`process` for background server lifecycle. **Unarmed until a live IPv4+IPv6 loopback-only netns proof
with Colima running** (Colima currently **stopped** → unproven). Proof obligations: a service bound to
`0.0.0.0`/`::` inside the ns is reachable on `127.0.0.1`/`::1` **inside** the ns but **not** from the
VM host or LAN (no `veth`), and external connect is denied (`EPERM`/no route).

---

## B71. Linux confinement & credentials: mount/env/fd/capability boundary (resolves B71)

**Why file-broker denial is insufficient.** `WorkspaceFileBroker.deny_credential_reads` only governs
the `read_file`/`write_file` MCP tool handlers (`build_claude_agent_options:131-137,178-187`). Under
Axis E the `terminal` tool runs a **raw shell inside the container**; that shell can `cat`, `open()`,
or `python -c` any path the **mount namespace** exposes, entirely bypassing the broker. Confinement
must therefore be enforced at the **kernel/mount layer**, not the broker.

### B71.1 Mounts (deny-by-default)

The container rootfs is assembled from an explicit allow-list; nothing else is mounted:

- **Read-only:** only the minimal toolchain the acceptance suite needs — a pinned interpreter
  (python), `git`, `node` (if the suite needs it), `bash`, and coreutils — bind-mounted **read-only**
  from the VM's own toolchain image, plus read-only system libs. Mirror of the toolchain-root allow
  logic in `build_workspace_terminal_args` (`:820-834`) but expressed as OCI mounts.
- **Writable:** only the **exec root** (`root_path`) and a per-run **runtime scratch** (a `tmpfs`
  `HOME`/`TMPDIR`, mirroring the isolated `terminal_home`/`terminal_tmp` of `:888-896`). `UV_LINK_MODE=copy`.
- **Explicitly NOT mounted (must be absent inside the container):** the macOS host home / any
  Colima-provided `/Users` mount, `~/.hermes` / profile config, OS keychains, `~/.ssh`, `~/.aws`,
  `~/.config`, `~/.netrc`, `~/.git-credentials`, `.env`/credential files, **and no `docker.sock` /
  `colima.sock` / any credential/agent socket** (`SSH_AUTH_SOCK` unset). Colima's default home mount
  into the VM is **not** propagated into the container mount namespace.

### B71.2 Environment (sanitized allow-list)

Start from `/usr/bin/env -i` semantics: pass only an allow-list — `PATH` (toolchain only), `HOME`
(scratch), `LANG`/`LC_*`, `TERM`, `TMPDIR` (scratch), `TZ`, `USER`/`LOGNAME` — mirroring
`allowed_env_keys` (`:866-877`). Force `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=/dev/null`, no
`GIT_*` credential helpers, `SSH_AUTH_SOCK` unset. No inherited secrets, tokens, or provider keys.

### B71.3 File descriptors, privileges, escape

- **Closed inherited fds.** The child starts with only `0,1,2` bound to controlled pipes;
  `O_CLOEXEC` on everything else. No inherited credential-socket fd.
- **No privilege escalation.** `no_new_privileges: true`; all capabilities dropped; non-root uid in a
  user namespace; seccomp blocks `mount`/`unshare`/`setns`/`pivot_root`/`ptrace`/`bpf`. The confined
  process cannot mount the excluded paths back in, cannot join another namespace, and cannot escape.

### B71.4 Materialization + proof-tested credential denial

- **Reject credential paths/aliases during archive materialization** (extends §B70.2 step 4 / v7
  §B.4): the exact-SHA `git archive` export and the exec-root tree are scanned to reject regular-file
  Git config/alternate/credential aliases, `.netrc`/`.git-credentials`/`.env`-class files, and
  escaping symlinks **before** the container is allowed to run against the tree.
- **Terminal-level credential read tests (proof obligations, §G):** inside the running Axis E
  container, `cat ~/.ssh/id_*`, `cat ~/.hermes/.env`, `env | grep -iE 'key|token|secret'`, `ls
  /var/run/docker.sock /var/run/colima*`, and an attempt to read the source checkout's / export's /
  exec-root's credential paths all **fail** (ENOENT/EACCES) — proving denial holds at the shell, not
  just the file broker.

---

## Invariants (preserved; item 6)

1. **No bare-Darwin loopback Seatbelt.** `build_workspace_seatbelt_profile` stays deny-default with
   `(deny network*)` when `allow_network` is false (`claude_workspace_terminal.py:353-370`); there is
   **no** loopback Seatbelt mode. Loopback exists only inside Axis N's VM network namespace.
2. **Axis E is deny-all network.** Fresh, empty net namespace, `lo` DOWN, no egress.
3. **Axis N is netns-only, default OFF**, unarmed until a live IPv4+IPv6 loopback-only proof with
   Colima running (currently **stopped** → unproven).
4. **No coder fallthrough; no `write_file`; no `process` under Axis E.** `verifier` never resolves to
   the coder bucket or the writable `build_workspace_terminal_args` path; there is a dedicated
   verifier mode rather than removing `verifier` from a read-only set globally.
5. **Exact reviewer parity when disarmed.** In `REVIEWER_READ_ONLY`/`VERIFIER_READ_ONLY` the Seatbelt
   profile, tool list, handler set, background rejection, and credential behavior are byte-for-byte
   identical (asserted by a golden fixture + hash, §G). `reviewer` is never affected by any routing cell.
6. **Exact-SHA `git archive` materialization** is a Slice-2 prerequisite to arming (immutable
   standalone checkout, durable objects; BUILD-674 residual-risk class); pre-run tree integrity,
   `.git`-metadata/credential-alias rejection, escaping-symlink rejection.

---

## F. Slices (each independently verifiable)

- **Slice 1 (low-risk, ships now, unchanged, outside this gate):** benign-introspection classification
  in `agent/claude_workspace_terminal.py`. Additive allow-list; existing rejection tests unchanged.
- **Slice A (authorization plumbing — B69):** `hermes kanban gate {open,publish,approve,reject,issue}`
  authenticated verbs + contexts; `get_verifier_execution_gate` linkage; validated structured
  `verifier_execution_authority` in `canonicalize_architecture_handoff`. Verification: open→accept→
  publish→approve→issue happy path yields a readable `gate_id`/`design_digest`; `approve` refuses a
  non-human/non-authenticated surface; `policy_accepted` does **not** arm.
- **Slice 2 (materialization — prerequisite):** exact-SHA `git archive` exec-root population,
  tree-digest integrity, `.git`/alias/symlink + credential-path rejection; depends on durable
  coder-object staging (BUILD-674). Verification: §G materialization tests.
- **Slice C (exec-root resource lifecycle — B70/B65):** `verifier_exec_root` kind, identity,
  `active→cleaning→cleaned` machine, interrupted-cleanup recovery, explicit dispatch (no
  `child_process` fallthrough), DB completion gate. Verification: §G lifecycle + crash-matrix tests.
- **Slice E (Axis E containment + routing — B67/B68/B71):** config flag; validated structured
  authority; `_verifier_execution_snapshot`/validator + staleness; `_build_run_spec` seal; preflight/
  AIAgent/external_runtime/claude_sdk_session/file-broker/terminal threading; `VerifierExecBroker`;
  fresh PID+mount+net-ns `crun` launch + readback; mount/env/fd/cap confinement. Arming gated on
  human_approved + strict flag + fresh digest + live PID+mount+net-ns readback proof. Verification:
  §G routing + containment + confinement tests.
- **Slice N (Axis N loopback — Option B, deferred, default OFF):** `lo` UP in the same net ns +
  `process`; no `veth`/NAT/egress. **Unarmed until a live IPv4+IPv6 netns proof.**

---

## G. Verification plan / proof obligations

Design-only now; these are the mandatory obligations for the implementation cards.

- **Namespace containment (B67):** with Colima running, launch the Axis E container; assert
  `/proc/<init>/ns/{pid,mnt,net,ipc,uts}` differ from host; net ns has only `lo` and `lo` is DOWN;
  routing table empty; external + VM-gateway connect denied. A `setsid`/double-fork child that
  `chdir`s away and closes all fds is killed by init teardown (kernel reap); assert no descendant
  survives. On bare Darwin (no VM) Axis E cannot arm → verifier byte-for-byte reviewer.
- **Namespace readback fail-closed (B67):** simulate a missing/host-shared net ns or a non-empty
  interface list → the broker denies and routes `VERIFIER_READ_ONLY`; **never** falls back to host
  networking.
- **Broker lifecycle + crash matrix (B70):** each row of §B70.4, including crash **after** delete
  **before** DB CAS (sweep re-picks `cleaning`, observes absence, reaches `cleaned`); teardown on
  normal end, error, kill/timeout, auth reset (`_clear_auth_state`), and session `close()`.
- **Completion gate (B70/B65):** `complete_task` returns `False` with
  `owned_verifier_exec_root_not_cleaned` while any required resource is `active`/`cleaning`/
  `cleanup_failed`; succeeds only when all are `cleaned` (proved absence).
- **Containment/identity (B65):** foreign-run cleanup denial and path-replacement/symlink swap →
  `identity_mismatch`, no deletion, completion blocked (dev+inode+non-symlink+subpath asserted).
- **Structured authority + strictness (B68):** approved axes read from validated
  `verifier_execution_authority` (not prose); `policy_accepted` cannot arm; absent flag / string
  `"true"` / int `1` cannot arm; malformed/missing snapshot → parity; **stale** authority (digest/epoch
  mismatch at claim) → parity; the digest binds the exact ADR sha + axis choice.
- **Authenticated caller surface (B69):** `hermes kanban gate approve` succeeds only with an
  `actor_type=='human'` authenticated surface and refuses agent/worker principals; `issue` requires
  `gate.state=='human_approved'` and an orchestrator context; the verifier task is created only by
  `issue`.
- **Linux confinement / credentials (B71):** inside the running Axis E container, credential paths and
  sockets are absent/unreadable at the **shell** (not just the file broker); sanitized env carries no
  secrets; inherited fds closed; `no_new_privileges` + dropped caps + seccomp block
  `mount`/`unshare`/`setns`; credential aliases rejected at archive materialization.
- **Reviewer parity (invariant):** reviewer golden Seatbelt fixture + hash unchanged; reviewer tool
  list/handlers/background-rejection/credential behavior byte-for-byte unchanged.
- **Network (invariants 1–3):** bare-Darwin `(deny network*)` retained; Axis E has no interfaces up;
  Axis N deferred, loopback-only proof required before arming; `0.0.0.0`/`::` binds do not expose the
  VM host or LAN.
- **Baseline:** reviewer-reported focused baseline stays green (excluding the tracked
  `.worktrees/t_67f31d19` hardlink host-state test, `t_390a9f80`); new tests pass.

---

## H. Rollout / rollback

- **Rollout: staged.** Slice 1 already low-risk. Slices A/2/C/E (Option A) behind strict-bool
  `agent.verifier_isolated_capability` (default OFF) and armed only after a human-approved gate (fresh
  digest) + green acceptance tests + a live PID+mount+net-namespace readback proof on a running VM +
  Sol re-review clean. Slice N (Axis N) additionally gated on the live IPv4+IPv6 netns proof.
- **Rollback: revert.** Disable = flag OFF (`is True` fails) and/or governing gate not `human_approved`
  and/or a stale digest, which restores exact reviewer parity via `VERIFIER_READ_ONLY`.
  `run_spec_json.verifier_execution` and `verifier_execution_authority` are additive optional keys (no
  data migration). Re-adding the pure boolean `_is_read_only_worker` for `verifier` fully reverts
  routing. `verifier_exec_root` cleanup rows are inert once the kind is unused; no destructive migration.

---

## Deliverable & environment note

- **Design only.** No source changed; no human approval claimed. This ADR proposes edits; it does not
  make them.
- **ADR SHA binding.** The exact sha256 of this attached v8 file is bound into the canonical handoff's
  `verifier_execution_authority.adr_artifact.sha256` (§B69.4/§B68.1), which is hashed into the gate
  `design_digest`. The file's own byte hash is a companion integrity reference, not the authority.
- **Live-proof status.** Linux PID+mount+net-namespace proofs and the Axis N IPv4+IPv6 loopback proof
  require a running Colima VM (currently **stopped**) and a Linux kernel; they are implementation
  arming preconditions, not architect-run artifacts, and are specified as mandatory §G obligations.
