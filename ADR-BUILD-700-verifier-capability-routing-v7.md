# ADR BUILD-700 — Verifier capability routing (v7)

- Status: DESIGN ONLY — proposed, awaiting independent GPT-5.6 Sol re-review then human approval. No source changed; no human approval claimed.
- Version: v7. Supersedes v6 `sha256:5e93c072b6bae2c7e5c863b8485fa4fd75826b6fa6a8b3070ae24111ec07cc02`.
- Source of truth: re-derived from live source at HEAD `9665015f2bb77a88cae398900f1be53c648c29c5` (clean `main`), not from the v6 text.
- Incident: BUILD-700. Human approval card: `t_0577d6d7`. Independent Sol re-review card: `t_cd03338f`.
- Resolves reviewer P1 blockers **B63, B64, B65, B66** and their dependents **B53, B55**. Preserves the v6 improvements the reviewer accepted (B52/B56/B57/B61/B62).

> Authority note (binds this whole document): approval authority is the architecture-gate
> `design_digest` computed by `kanban_db.canonicalize_architecture_handoff` +
> `architecture_handoff_digest` from the architect's completion metadata — **not** this file's
> prose, **not** any approval-card status/comment, and **not** this file's incidental byte hash.
> This ADR's design is fully encoded in the canonical handoff (chosen_approach / slices /
> acceptance_criteria / verification_plan / rollout / rollback), so the gate `design_digest`
> transitively binds the design. The ADR file hash is a companion integrity reference only; it is
> pinned by the reviewer/orchestrator terminal (see "Deliverable & environment note").

---

## 0. What changed since v6 (blocker-by-blocker)

v6 was a correct *direction* (Axis E deny-all-network; Axis N netns-only default-OFF; reviewer
parity; `git archive` materialization). Sol's v6 verdict was **MODIFY** with four P1s. v7 makes
each mechanically implementable against the real code and stops describing a boundary the kernel
cannot enforce.

| Blocker | v6 defect | v7 correction (section) |
|---|---|---|
| B63 | Authority bound to an `architecture_gates` row that does not exist for this workflow; approval card cannot perform the authoritative action. | §A: the supported orchestrator gate sequence that actually creates the gate, accepts the canonical v7 handoff, and publishes a real `gate_id` + `design_digest`; ADR SHA bound into the handoff. |
| B64 / B53 | Axis E "filesystem-binding drain scan" cannot see a synchronous test that daemonizes, `chdir`s away, and closes root fds; `killpg`/pgid cannot reap it. | §B: Axis E execution is placed on a **kernel ownership boundary a descendant cannot escape** — a Linux PID namespace hosted in the Colima/Lima VM (namespace-init reaper). No fs/fd/pgid scan is treated as containment. Bare Darwin cannot arm Axis E → fail-closed to reviewer parity. |
| B65 | `verifier_exec_root` is not a supported owned-resource kind; the cleanup dispatcher would treat a path as a `child_process`; no `active→cleaning→cleaned` lifecycle; no completion gate on required resources. | §C: a first-class `verifier_exec_root` resource kind + identity, an explicit `active→cleaning→cleaned` state machine (cleaned only after verified deletion), interrupted-cleanup recovery, exact path+inode containment, and a DB completion gate that rejects any required resource not `cleaned`. |
| B66 / B55 | Strict flag + sealed RunSpec not threaded through bootstrap; `policy_accepted` equivalence conflicts with the human-only requirement. | §D: exact edit list and data flow from `DEFAULT_CONFIG["agent"].verifier_isolated_capability` (strict `is True`) and a **new** `run_spec_json.verifier_execution` snapshot that requires `gate.state == 'human_approved'` (not `policy_accepted`), snapshotted once at SDK-session bootstrap; missing/malformed/unknown/mismatch → verifier routed **exactly** as reviewer. |

---

## 1. Problem (recap, source-anchored)

Two layers, both at HEAD `9665015f2`:

- **L1 — introspection misclassification** (Slice 1, low-risk, unchanged, outside this gate): the
  read-only terminal rejects benign version/help probes. Already agreed low-risk; ships under
  standard review. Not re-litigated here.
- **L2 — verifier cannot verify**: `agent/claude_sdk_session.py:25`
  `_READ_ONLY_WORKER_PROFILES = frozenset({"reviewer", "verifier"})` pins `verifier` to the exact
  reviewer capability: tool set `{terminal, read_file}` (`:62-66`), credential-denied file broker
  (`:131-137`), no `process`, and a `(deny network*)` Seatbelt profile
  (`claude_workspace_terminal.py:365-366`) over a read-only source mirror
  (`dispatch_read_only_workspace_terminal`, `:984-1013`; background rejected at `:993-994`). A
  verifier therefore cannot obtain the exact target commit in an owned checkout, run an acceptance
  suite, or probe a service it starts.

The goal of Slices 2–4 is to let a **verifier** (and only a verifier), **only when a human has
approved this exact design**, run the acceptance suite against the reviewed commit inside a
disposable, owned, contained execution environment — while `reviewer` stays byte-for-byte
unchanged and source-under-review stays immutable.

---

## 2. Model: two axes on one kernel boundary

The capability is split into two independently-armed axes. Both execute inside the **Linux VM**
(Colima/Lima) because bare Darwin gives a non-root process no inescapable process- or
network-ownership boundary (see §B). Neither axis runs on bare Darwin.

- **Axis E (execution/process containment).** Materialize the exact reviewed commit into a
  verifier-owned disposable **exec root**, then run the acceptance suite **synchronously** inside a
  **PID namespace** whose init is the reaper. Network is **deny-all** (no configured interfaces).
  Tool surface is exactly reviewer's `{terminal, read_file}` — **no `process`, no `write_file`, no
  background, no external egress**. Difference from reviewer is solely the terminal *dispatch
  target* (a contained, materialized, writable exec root vs. the read-only source mirror).
- **Axis N (loopback network).** An *additive* grant on top of Axis E: a fresh **network namespace**
  with loopback (`lo`) up so a service the verifier starts on `127.0.0.1`/`::1` is reachable **only
  inside the namespace** (no `veth`, no NAT, no external egress), plus `process` for background
  server lifecycle. **Default OFF and unarmed until a live IPv4+IPv6 netns proof with Colima
  running.** Remote acceptance criteria are delivered as task evidence, never fetched.

Decision requested from the human gate:

- **Option A (recommended interim): authorize the Axis E *design*.** Arming still requires
  `human_approved` gate + strict config flag + a green live PID-namespace reap proof on a running
  VM.
- **Option B: authorize Axis E + Axis N *design*.** Approving Option B authorizes only the design;
  Axis N arming additionally requires the live IPv4+IPv6 netns proof (Colima currently **stopped** →
  unproven).

No forced synthesis: if the human prefers A over B (or neither), that is a clean, supported
terminal state — Axis N simply stays unarmed.

---

## A. Authorization: the real architecture gate (resolves B63)

### A.1 Current fact (acknowledged)

There is **no persisted `architecture_gates` row** for `t_fc20700d`, `t_ca1a2ced`, `t_0577d6d7`, or
`t_cd03338f`, nor for this v7 architect task. The `architecture_gates` table exists
(`kanban_db.py:1876-1902`) and the full authority chain is implemented, but **no gate was ever
opened for this workflow**. Therefore the human approval card `t_0577d6d7` **cannot** perform the
authoritative action v6 named: `approve_architecture_gate` (`:4180-4235`) requires an existing gate
in state `validated_awaiting_approval` and an exact `design_digest`. Card prose/status is not
authority. This is B63 and it is a real gap, not a wording issue.

### A.2 Supported orchestrator sequence (the fix)

The front-door/orchestrator lane (not this architect worker) must run this exact sequence. All
function names below are live in `hermes_cli/kanban_db.py`.

1. **Open a gate bound to a v7 architect task.** Before/at dispatch of the v7 architect task
   `T_arch`, the orchestrator opens a gate:
   `open_architecture_gate(conn, architect_task_id=T_arch, context, mode='enforce')`
   → inserts an `architecture_gates` row in state `'open'` (`:3993-4011`), bound to `T_arch`,
   `board_key`, `creator_principal`, `request_scope_id`, `session_id`, `workflow_key`. (The unique
   partial index `idx_architecture_gates_active_scope`, `:2015-2018`, guarantees one active gate per
   scope; `:3984-3992` refuses to open a gate while an ungated run is already running in scope.)
2. **Architect completes with the canonical handoff.** The architect (this lane) completes `T_arch`
   with the version-1 handoff metadata (`role='architect'`, `design_depth='formal'`,
   `chosen_approach`, `alternatives_rejected`, `slices`, `acceptance_criteria`, `verification_plan`,
   `human_approval_required=true`, `rollout`, `rollback`), which must validate under
   `canonicalize_architecture_handoff` (`:4034-4072`) and **bind the ADR SHA** (§A.4).
3. **Accept the handoff → mint the digest.** Completion of a gated architect task accepts the
   handoff **atomically inside `complete_task`** (`:8817-8837` calls
   `_accept_architecture_handoff_in_txn`, `:4092-4146`): it canonicalizes the metadata, computes
   `design_digest = architecture_handoff_digest(...)` (`:4075-4089`, a SHA-256 over
   `{policy_version, canonicalization_version, trusted_scope, architect_task_id, accepted_run_id,
   canonical_handoff_json}`), and transitions the gate to `validated_awaiting_approval` because
   `human_approval_required` is true (`:4117-4121`). **A malformed handoff rolls the completion
   back** (`:8824-8827`), keeping the architect run alive to correct — so a real `gate_id` and
   `design_digest` now exist and are readable.
4. **Publish `gate_id` + `design_digest` on the human card.** The orchestrator surfaces the real
   `gate_id` and `design_digest` (read via `get_architecture_gate_for_task(conn, T_arch)`) onto
   `t_0577d6d7`. This is the value the human approves — not the ADR prose.
5. **Human approves the exact digest.** The human calls, through an authenticated surface,
   `approve_architecture_gate(conn, gate_id, human_context, design_digest)` (`:4180-4235`):
   requires `context.actor_type == 'human'`, `context.surface in AUTHENTICATED_APPROVAL_SURFACES`,
   gate state `validated_awaiting_approval`, and `digest == gate.design_digest`; on success →
   `human_approved`. Exact re-submission is idempotent; every other replay denies.
6. **Issue the implementation + verifier graph.** Post-approval, the orchestrator (and only the
   orchestrator: `context.actor_type == 'orchestrator_agent'`, `profile == 'orchestrator'`,
   `phase == 'graph_issuance'`) calls `issue_architecture_graph(conn, gate_id, context, tasks,
   idempotency_key=...)` (`:4267-4397`), which requires `gate.state == 'human_approved'`
   (`:4339-4340`) and, in one transaction, creates the implementation + verifier tasks and records
   `architecture_graph_issuances(gate_id, task_ids, ...)` (`:1920-1926`, `:4390-4395`). The verifier
   task is created here; **no task-keyed capability exists before this point** — this is why an
   approval-card grant is impossible and correct.

### A.3 Gate→verifier linkage (no new column)

`get_verifier_execution_gate(conn, task_id)` resolves the governing gate for a verifier task by
looking up the `architecture_graph_issuances` row whose `task_ids` JSON contains `task_id`, then
loading that `gate_id`. This reuses the issuance ledger already written at `:4390-4395`; **zero
DDL** for linkage. (Mirror of the existing `get_delivery_architecture_gate` used in `complete_task`
at `:8653`.)

### A.4 Binding the ADR SHA into the canonical handoff

Requirement: the canonical handoff must bind the exact ADR file so a downstream implementer can
prove "the approved gate corresponds to exactly this ADR." Two-part answer:

- **Zero-DDL interim (used by this run's handoff):** embed the ADR artifact digest inside an
  **already-allowed** field so it enters `canonical_handoff_json` and thus `design_digest`. This
  ADR's handoff carries a leading slice
  `{"id":"S0-design-reference","adr_artifact":"ADR-BUILD-700-verifier-capability-routing-v7.md",
  "adr_sha256":"<hex>","verification":"sha256sum ADR-...-v7.md == <hex>"}` and repeats the digest in
  `chosen_approach`. Because `slices` and `chosen_approach` are in the `allowed` set
  (`:4038-4041`) and are hashed into `design_digest`, the ADR bytes are transitively bound.
- **First-class primitive (recommended additive edit):** extend `canonicalize_architecture_handoff`
  (`:4038-4049`) `allowed`/`required` sets with a `design_artifacts` field — a list of
  `{"path": str, "sha256": /^[0-9a-f]{64}$/}` — validated and included in the returned `handoff`
  dict (`:4070`). This makes the ADR digest a validated authority field rather than free-text.
  Additive to the version-1 canonicalization; because it changes the digest domain it is gated
  behind `canonicalization_version` (`ARCHITECTURE_GATE_CANONICALIZATION_VERSION`), so existing
  gates are unaffected.

---

## B. Axis E process containment: an inescapable kernel boundary (resolves B64, B53)

### B.1 Why v6's containment was insufficient (accepted)

v6 forbade the terminal `background` flag (`dispatch_read_only_workspace_terminal` already rejects
background at `claude_workspace_terminal.py:993-994`) and proposed a filesystem-binding "drain
scan." Sol is correct: forbidding the *tool's* background flag does **not** forbid a **synchronous**
test command from `fork()`+`setsid()`+second-`fork()` (daemonizing), `chdir("/")`, and closing all
inherited fds. Such a descendant is reparented to init, leaves the caller's process group and
session, and holds **no** open handle under the exec root — so any scan of cwd / open-fds / pgid /
process ancestry misses it, and `killpg`/psutil-tree walks cannot reap it. On bare Darwin a non-root
process has **no** cgroup and **no** PID namespace, so there is no OS primitive to enumerate-and-kill
"everything this run spawned." **A filesystem/fd/pgid scan is not containment and v7 does not claim
it is.**

### B.2 The boundary: PID namespace with a reaper init (Linux VM)

Axis E execution runs inside a **Linux PID namespace** created in the Colima/Lima VM
(`unshare --pid --fork --mount-proc` with a namespace **init** as PID 1, or an equivalent
`crun`/`runc` container whose init reaps). Guarantees the kernel enforces and a descendant cannot
escape:

- A PID-namespace-1 process is the reaper for every descendant. When init receives `SIGKILL`, the
  kernel sends `SIGKILL` to **every** process in that namespace, including daemonized / re-parented /
  `setsid` children — reparenting stays *inside* the namespace and cannot cross the boundary.
- The exec root is a bind mount visible only inside the namespace's mount namespace; the acceptance
  suite runs against it; teardown kills init → kernel reaps all → then the host-side exec-root
  directory is deleted (§C).
- A non-privileged Darwin host cannot create this boundary → **on bare Darwin Axis E cannot arm**
  and the verifier falls closed to exact reviewer parity (§D). This is the honest consequence of
  B64: the capability is only available where the kernel can enforce it.

### B.3 Tool surface under Axis E (unchanged from reviewer names)

Axis E keeps the reviewer tool set **exactly** `{terminal, read_file}` (`claude_sdk_session.py:65`):
**no `process`**, **no `write_file`**, **no background** (the synchronous command runs to completion;
the namespace bounds any children it spawns). `process` (background server lifecycle) is an Axis N
concern only. The only capability difference vs reviewer is the terminal **dispatch target**: a new
`dispatch_verifier_exec_terminal(...)` runs the (single, foreground) command inside the PID
namespace against the materialized exec root, instead of `dispatch_read_only_workspace_terminal`
running against the read-only source mirror. Everything else — file broker credential denial,
disallowed `Task/WebFetch/WebSearch` (`:209`), `permission_mode='acceptEdits'`, deny-all network — is
identical.

### B.4 Materialization (preserved from v6)

Exec-root population is the exact-SHA `git archive <sha> | tar -x` into the exec root (no `.git`, no
`config`, no `objects/info/alternates`, no `remotes`, no pointer files), followed by a pre-run
tree-digest integrity assert and explicit rejection of regular-file Git config/alternates credential
aliases and escaping symlinks. `shutil.copytree` of the working tree is rejected (it would copy
`.git` alias/credential surfaces). This is unchanged from v6 (B56/B57 resolved) and remains a Slice-2
prerequisite: Axis E is unsafe to arm until Slice 2 delivers a standalone immutable exact-SHA
checkout with durable objects (BUILD-674 residual-risk class).

---

## C. Owned exec-root resource lifecycle & completion gate (resolves B65)

### C.1 Current fact (acknowledged)

`CONTINUATION_RESOURCE_KINDS = {"tmux_session", "worktree", "child_process"}`
(`kanban_db.py:136`). There is **no** `verifier_exec_root` kind, so
`register_owned_run_resource` (`:13022-13035`) would raise `unknown resource kind`. The cleanup
dispatcher `cleanup_owned_run_resources` (`:13163-13205`) dispatches `if tmux / elif worktree / else
child_process` — its **`else` is a catch-all**, so a new kind would be wrongly treated as a
`child_process` (the exact hazard B65 names). Cleanup is a single synchronous op → single `UPDATE`
to `cleaned | identity_mismatch | cleanup_failed` (`:13183-13189`); there is **no `cleaning`
state** and the sweep `cleanup_terminal_run_resources` (`:13208-13238`) re-picks **only**
`state = 'active'`. `complete_task` (`:8626-8838`) gates on quarantine, open critical continuation
blockers, delivery-policy epoch, and publication readback, but **never checks that owned resources
were cleaned**. All four are real gaps.

### C.2 New resource kind + identity

- Add `"verifier_exec_root"` to `CONTINUATION_RESOURCE_KINDS` (`:136`).
- Identity JSON (content-addressed by the existing `content_digest`, `:13040-13041`):
  `{"root_path": <abs>, "dev": <st_dev>, "inode": <st_ino>, "scratch_base": <abs>, "vm_context":
  <colima|lima profile>, "pid_ns_token": <opaque>}`. `root_path` must be absolute and a strict
  subpath of the designated per-run `scratch_base` (which is **outside** the source workspace —
  same invariant enforced for the read-only mirror at `claude_workspace_terminal.py:1003-1004`).
  Registered via `register_owned_run_resource(..., kind='verifier_exec_root',
  cleanup_policy='on_terminal')`, whose owner check `task_runs(id,task_id,claim_lock)` (`:13044-13049`)
  already binds the resource to the exact run/claim — **no path/session-name inference is authority**
  (the table's stated contract, `:1976-1977`).

### C.3 State machine: `active → cleaning → cleaned`

Extend the resource `state` domain (a `CONTINUATION_RESOURCE_STATES` set) to
`{active, cleaning, cleaned, identity_mismatch, cleanup_failed}` and add a `verifier_exec_root`
cleanup that is **crash-recoverable and only marks `cleaned` after verified deletion**:

1. **Containment check (before any deletion).** `lstat(root_path)`; require: absolute; strict
   subpath of `scratch_base`; not a symlink; `st_dev == identity.dev` **and** `st_ino ==
   identity.inode`. Any mismatch → state `identity_mismatch`, `cleanup_error` set, **no deletion**,
   and the resource is treated as *not cleaned* (blocks completion, §C.4). This defeats
   path-replacement / symlink-swap attacks (mirrors the birth-identity check in
   `_cleanup_exact_child_process`, `:13146-13147`, and tmux identity match at `:13107-13108`).
2. **Enter `cleaning`.** In a `write_txn`, CAS `state='active' → 'cleaning'` and stamp a
   `cleanup_started_at`. This durable marker is what makes an interrupted cleanup recoverable.
3. **Tear down containment, then delete.** Kill the PID-namespace init (kernel reaps all
   descendants, §B.2), then `shutil.rmtree(root_path)`. `rmtree` is idempotent on a partially
   deleted tree (a crash between step 2 and completion leaves a half-deleted tree that a re-run
   finishes).
4. **Verify absence, then `cleaned`.** Re-`lstat`; only if `not root_path.exists()` CAS
   `state='cleaning' → 'cleaned'`, `cleaned_at=now`. If the path still exists → `cleanup_failed`
   with `cleanup_error`.
5. **Dispatch explicitly (no catch-all).** Change `cleanup_owned_run_resources` (`:13175-13180`) to
   an explicit `elif resource.kind == 'verifier_exec_root': _cleanup_exact_verifier_exec_root(...)`
   and make the final `else` **raise/refuse** an unknown kind rather than defaulting to
   `child_process`.

### C.4 Interrupted-cleanup recovery + completion gate

- **Recovery sweep.** `cleanup_terminal_run_resources` (`:13223-13228`) must re-pick
  `verifier_exec_root` rows in state `active` **or** `cleaning` **or** `cleanup_failed` for runs with
  `ended_at IS NOT NULL`, so a crash mid-`rmtree` is retried to a terminal `cleaned`/`cleanup_failed`.
- **Immediate cleanup in the completion path.** `complete_task` drives
  `cleanup_owned_run_resources(conn, task_id, run_id)` for the completing run before the status CAS.
- **DB completion gate (new).** Inside the `complete_task` write transaction (`:8626`+, beside the
  `open_critical_continuation_blockers` guard at `:8633-8652`), after cleanup, query
  `continuation_owned_resources` for the run where `kind = 'verifier_exec_root'` and `state !=
  'cleaned'`. If any exist → append a `completion_blocked` event
  `{"reason": "owned_verifier_exec_root_not_cleaned", "resource_ids": [...]}` and `return False`.
  This puts the destructive-resource guarantee in the **DB completion kernel**, so CLI, model-tool,
  and any future writer are all bound by it (same reasoning the delivery/publication gates cite at
  `:8698-8701`).

---

## D. Fail-closed classifier: strict flag + sealed RunSpec, snapshotted once (resolves B66, B55)

Two independent keys must both hold to route a verifier into Axis E; either missing/malformed/
unknown → verifier routed **exactly** as reviewer (fail-closed). The two keys are separated so an
approved gate cannot elevate a deployment whose operator did not enable the master switch, and the
master switch cannot elevate without an approved gate.

### D.1 Key 1 — master config flag (strict `is True`)

Add to `DEFAULT_CONFIG["agent"]` (`hermes_cli/config.py:993`):

```python
# Master switch for the BUILD-700 verifier isolated-execution capability.
# Strict bool: only literal True arms it. Absent/"true"/1/"1"/None stay OFF.
"verifier_isolated_capability": False,
```

Read at worker bootstrap via `cfg_get("agent.verifier_isolated_capability")` and gated with **identity**
`resolved is True` (not truthiness), so a YAML string `"true"`, int `1`, or any non-bool cannot arm
it. This is the local, per-deployment master switch.

### D.2 Key 2 — sealed `run_spec_json.verifier_execution` (requires `human_approved`)

The immutable per-run contract is `task_runs.run_spec_json` (`:1804`), built by `_build_run_spec`
(`:7180-7237`; current keys `version, profile, requested_route, toolsets, delivery_policy`). Add a
**new, additive** key `verifier_execution` alongside `delivery_policy` (no DDL — JSON on an existing
column; legacy NULL/absent runs degrade to `disposition:"none"`).

- **New builder `_verifier_execution_snapshot(gate)`** — modeled on `_delivery_policy_snapshot`
  (`:7070-7104`) but with the security-critical difference: it treats **only** `gate.state ==
  'human_approved'` as authorizing. It must **not** reuse `_delivery_policy_snapshot`, whose
  `enforcing_approved` disposition deliberately treats `policy_accepted` **and** `human_approved`
  as equivalent (`:7090-7093`) — acceptable for delivery policy, **unsafe** for this
  security-sensitive capability. Shape:
  - not armed → `{"version":1, "disposition":"none", "gate_id":None, "design_digest":None,
    "axis_e":False, "axis_n":False}`
  - armed → `{"version":1, "disposition":"human_approved", "gate_id":..., "design_digest":...,
    "axis_e":True, "axis_n":<bool, default False>}`, produced **only** when: the task is a verifier
    task, its governing gate (§A.3 `get_verifier_execution_gate`) is `human_approved`, the approved
    canonical handoff authorized verifier isolated execution, and `gate.enforcement_mode in
    ARCHITECTURE_GATE_ENFORCING_MODES`.
- **New validator `validate_verifier_execution_snapshot(value)`** — modeled on
  `validate_delivery_policy_snapshot` (`:7107-7177`): `version == 1`; `disposition in
  {"none","human_approved"}`; for `none`, all authority fields must be `None`; for
  `human_approved`, `gate_id`/`design_digest` present, `design_digest` matches `^[0-9a-f]{64}$`,
  `axis_e is True`, `axis_n` a bool. Any deviation raises → treated as disarmed.

### D.3 Threading path (snapshot once at SDK-session bootstrap)

Exact seams, in order, all fail-closed:

1. **Preflight load (bootstrap).** `hermes_cli/cli_agent_setup_mixin.py:342-348` already calls
   `preflight_kanban_cli_route(...)`, which loads the sealed spec via `load_active_run_spec()`
   (`kanban_runtime_contract.py:165`). Extend `preflight_kanban_cli_route` (`:157-227`) to, after
   route validation, compute `verifier_execution = validate_verifier_execution_snapshot(
   spec.get("verifier_execution"))` and return it on the spec. A raise → disarmed. This runs **before
   any AIAgent/provider/client construction** (`cli_agent_setup_mixin.py:334-336`), so a mismatch
   cannot produce provider side effects.
2. **AIAgent carries the decision.** Store the validated decision once on the agent
   (`agent._verifier_execution`) at bootstrap. Because `run_spec_json` is immutable and loaded once,
   this is a single snapshot for the session's life — cache-safe, no mid-session mutation.
3. **External runtime closure.** `agent/external_runtime.py:422-448` `_options(resume)` factory
   passes `worker_profile` (resolved once at `:415`) into `build_claude_agent_options`; add an
   explicit `verifier_execution=getattr(agent, "_verifier_execution", None)` argument. The
   `ClaudeAgentSdkSession` is constructed once (`:451-463`) so the closure captures the decision
   once (snapshot-once).
4. **SDK routing.** In `agent/claude_sdk_session.py`, replace the boolean `_is_read_only_worker`
   (`:28-32`) with `_resolve_worker_capability(capability_mode, worker_profile,
   verifier_execution)` returning one of:
   - `REVIEWER_READ_ONLY` — `worker_profile == 'reviewer'`.
   - `VERIFIER_READ_ONLY` — `worker_profile == 'verifier'` **and not** (master flag `is True` **and**
     `verifier_execution.disposition == 'human_approved'` **and** `axis_e is True`). This is the
     **fail-closed default** and is byte-for-byte reviewer parity.
   - `VERIFIER_ISOLATED_EXEC` — `worker_profile == 'verifier'` **and** both keys hold. Axis N
     sub-capability armed only additionally when `verifier_execution.axis_n is True` **and** the live
     netns proof gate is satisfied (default OFF).
   `_mcp_tool_names` (`:62-66`): `REVIEWER_READ_ONLY` and `VERIFIER_READ_ONLY` both →
   `{terminal, read_file}`. `VERIFIER_ISOLATED_EXEC` (Axis E) → also `{terminal, read_file}` (no
   `process`/`write_file`); `process` is added **only** under armed Axis N.
5. **File broker.** `deny_credential_reads` must stay `True` for `verifier` in **all three** modes.
   Generalize the `_is_read_only_worker` usage at `:131-137` and the duplicate at
   `external_runtime.py:416-420` to `_denies_credentials(worker_profile)` = `worker_profile in
   {"reviewer","verifier"}`, independent of exec mode, so credentials are denied in both the source
   checkout and the exec-root mirror, for caller-supplied and internal brokers.
6. **Terminal override.** `_transform` (`:141-152`) and `handler_overrides` (`:161-188`): add the
   `VERIFIER_ISOLATED_EXEC` branch routing `terminal` to `dispatch_verifier_exec_terminal(...)`
   (§B.3) against the owned exec root inside the PID namespace; `REVIEWER_READ_ONLY`/
   `VERIFIER_READ_ONLY` keep the existing `dispatch_read_only_workspace_terminal` override unchanged.

### D.4 Fail-closed truth table

| master flag `is True` | sealed `verifier_execution` | verifier route |
|---|---|---|
| any | absent / NULL (legacy) | `VERIFIER_READ_ONLY` (reviewer parity) |
| any | malformed / unknown disposition | `VERIFIER_READ_ONLY` |
| any | `disposition == 'none'` | `VERIFIER_READ_ONLY` |
| any | `human_approved` but gate not `human_approved` at claim (epoch mismatch) | `VERIFIER_READ_ONLY` |
| `False` / absent / `"true"` / `1` | `human_approved` | `VERIFIER_READ_ONLY` |
| `True` (identity) | `human_approved`, `axis_e True`, VM PID-ns proof green | `VERIFIER_ISOLATED_EXEC` (Axis E) |

`reviewer` is never affected by any cell (byte-for-byte parity is an invariant, §E).

---

## E. Preserved v6 improvements (invariants — item 5)

1. **No bare-Darwin loopback Seatbelt.** `build_workspace_seatbelt_profile` stays deny-default with
   `(deny network*)` when `allow_network` is false (`claude_workspace_terminal.py:357-366`); there
   is **no** loopback Seatbelt mode. Loopback exists only inside Axis N's VM network namespace.
2. **Axis E is deny-all network.** No configured interfaces in the PID namespace; no egress.
3. **Axis N is netns-only, default OFF**, unarmed until a live IPv4+IPv6 loopback-only proof with
   Colima running (Colima currently **stopped** → unproven; Docker/Colima/Lima installed).
4. **No coder fallthrough; no `write_file`; no `process` under Axis E.** `verifier` never resolves
   to the coder bucket or the writable `build_workspace_terminal_args` path; there is a dedicated
   verifier mode (§D.4) rather than removing `verifier` from a read-only set globally (which would
   fall through to writable coder routing — B56/parity).
5. **Exact reviewer parity when disarmed.** In `REVIEWER_READ_ONLY`/`VERIFIER_READ_ONLY` the
   generated Seatbelt profile, tool list, handler set, background rejection, and credential behavior
   are byte-for-byte identical (asserted by a golden fixture + hash, §G).
6. **Exact-SHA `git archive` materialization**, pre-run tree integrity, `.git`-metadata and
   credential-alias rejection, escaping-symlink rejection (§B.4).

---

## F. Slices (each independently verifiable)

- **Slice 1 (low-risk, ships now, unchanged, outside this gate):** benign-introspection
  classification in `agent/claude_workspace_terminal.py`. Additive allow-list; existing rejection
  tests unchanged. Not part of Options A/B.
- **Slice A (authorization plumbing — B63):** `get_verifier_execution_gate` (§A.3); optional
  additive `design_artifacts` handoff field (§A.4). No behavior change to non-verifier routing.
  Verification: gate open→accept→approve→issue happy path yields a readable `gate_id`/`design_digest`;
  `policy_accepted` (human_approval_required=false) does **not** arm verifier execution.
- **Slice 2 (materialization — B56/B57, prerequisite to arming):** exact-SHA `git archive` exec-root
  population, tree-digest integrity, `.git`/alias/symlink rejection; depends on durable coder-object
  staging (BUILD-674). Verification: §G materialization tests.
- **Slice C (exec-root resource lifecycle — B65):** `verifier_exec_root` kind, identity, `active→
  cleaning→cleaned` machine, interrupted-cleanup recovery, explicit dispatch (no `child_process`
  fallthrough), DB completion gate. Verification: §G lifecycle tests.
- **Slice E (Axis E containment + routing — B64/B53/B66/B55):** config flag; `_verifier_execution_
  snapshot`/validator; `_build_run_spec` seal; preflight/AIAgent/external_runtime/claude_sdk_session/
  file-broker/terminal threading; PID-namespace exec dispatch; deny-all network. Arming gated on
  human_approved + flag + live PID-ns reap proof. Verification: §G routing + containment tests.
- **Slice N (Axis N loopback — Option B, deferred, default OFF):** fresh netns with loopback + `process`
  for background server lifecycle; no external egress. **Unarmed until a live IPv4+IPv6 netns proof.**

---

## G. Verification plan / proof obligations

Design-only now; these are the mandatory obligations for the implementation cards.

- **Detached-escape containment (B64/B53):** a `setsid`/double-fork child that `chdir`s away and
  closes all fds under Axis E is killed by PID-namespace init teardown (kernel reap); assert no
  descendant survives after teardown. Prove the negative on bare Darwin: with no VM, Axis E cannot
  arm and the verifier is byte-for-byte reviewer.
- **Interrupted cleanup recovery (B65):** simulate a crash between `state='cleaning'` and root
  deletion; the sweep re-picks `cleaning`/`cleanup_failed`, finishes `rmtree` idempotently, and only
  then reaches `cleaned`.
- **Completion gate (B65):** `complete_task` returns `False` with `owned_verifier_exec_root_not_
  cleaned` when a required resource is `active`/`cleaning`/`cleanup_failed`; succeeds only when all
  required `verifier_exec_root` resources are `cleaned`.
- **Containment/identity (B65):** foreign-run cleanup denial and path-replacement/symlink swap →
  `identity_mismatch`, no deletion, completion blocked (dev+inode+non-symlink+subpath asserted).
- **Authorization equivalence (B66):** `policy_accepted` cannot arm (only `human_approved` does);
  absent flag / string `"true"` / int `1` cannot arm; malformed/missing `verifier_execution`
  snapshot → exact reviewer parity; delivery epoch mismatch → reviewer parity.
- **Reviewer parity (invariant):** reviewer golden Seatbelt fixture + hash unchanged; reviewer
  tool list/handlers/background-rejection/credential behavior byte-for-byte unchanged across the
  change.
- **Credential denial (E.6):** credential reads denied for verifier in source checkout **and** exec
  root, for caller-supplied and internal brokers; denied in `git archive` source/export/mirror.
- **Network (E.1–E.3):** Axis E has no network (`(deny network*)` retained; no configured
  interface). Axis N deferred: live IPv4+IPv6 loopback-only netns proof required before arming;
  external connect denied (EPERM); `0.0.0.0`/`::` binds do not expose the host LAN.
- **Materialization (B56/B57):** exec root has no `.git`/`config`/`alternates`/`remotes`/pointer
  files; pre-run tree-digest matches the target commit; regular-file credential aliases and escaping
  symlinks rejected.
- **Baseline:** reviewer-reported focused baseline (138/138 excluding the tracked
  `.worktrees/t_67f31d19` hardlink host-state test, `t_390a9f80`) stays green; new tests pass.

---

## H. Rollout / rollback

- **Rollout: staged.** Slice 1 already low-risk. Slices A/2/C/E (Option A) behind strict-bool
  `agent.verifier_isolated_capability` (default OFF) and armed only after human-approved gate +
  green acceptance tests + a live PID-namespace reap proof on a running VM + Sol re-review clean.
  Slice N (Axis N) additionally gated on the live IPv4+IPv6 netns proof.
- **Rollback: revert.** Disable = flag OFF (`is True` fails) and/or governing gate not
  `human_approved`, which restores exact reviewer parity via `VERIFIER_READ_ONLY`.
  `run_spec_json.verifier_execution` is an additive optional JSON key (no data migration).
  Re-adding the pure boolean `_is_read_only_worker` for `verifier` fully reverts routing.
  `verifier_exec_root` cleanup rows are inert once the kind is unused; no destructive migration.

---

## I. Human approval gate + required orchestrator follow-up

- **This ADR is design-only.** No source changed; no human approval claimed or implied.
- **Required orchestrator actions (cannot be done by this worker lane):**
  1. Open an architecture gate on a v7 architect task and accept the canonical handoff (§A.2) to
     mint the real `gate_id` + `design_digest`.
  2. **Only after** that digest exists: post the superseding v7 decision (with `gate_id`/
     `design_digest`) on `t_0577d6d7`, and dispatch/re-arm the independent GPT-5.6 Sol re-review
     `t_cd03338f` against the exact v7 design (the reviewer pins the attached ADR bytes with its own
     terminal).
  3. On `human_approved`, `issue_architecture_graph` the implementation cards (assignee: coder) +
     verifier card for the approved slices; keep Axis N unarmed until its live proof.
- **Do not approve Option A or B** until the independent Sol re-review of the exact v7 design returns
  with no open P1.

---

## Deliverable & environment note

This run's workspace **terminal is environmentally blocked**: the shared `dir` workspace
`/Users/nicholas/.hermes/hermes-agent` contains a sibling task's worktree
(`.worktrees/t_67f31d19/node_modules/@esbuild/darwin-arm64/bin/esbuild`) that is a **hard-linked
regular file**, which `_reject_linked_workspace_files` (`claude_workspace_terminal.py:997`) refuses,
so no shell command (including `sha256sum`) can run. This is the exact failure already tracked as
`t_390a9f80`. Consequently this run cannot compute the ADR file byte digest; the authoritative
binding remains the gate `design_digest` (server-computed from the canonical handoff), and the
attached ADR bytes are to be pinned by the reviewer/orchestrator terminal during Sol re-review. All
source line references above were read directly from HEAD `9665015f2` via the file reader and are
current.
