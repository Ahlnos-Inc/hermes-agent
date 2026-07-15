# BUILD-487: Durable bounded Kanban continuation

## Decision

Hermes Kanban remains the only workflow authority. The existing orchestrator
continues to compile task graphs, the board continues to own task state, and
the dispatcher continues to own claims, immutable RunSpecs, spawn receipts,
PID attachment, and dependency promotion. Durable continuation adds a
run-scoped execution contract to that path; it does not introduce another
queue, scheduler, session manager, or completion authority.

The feature is opt-in through `kanban.continuation.enabled`. Runs without a
continuation manifest retain their previous lifecycle exactly.

## Problem

Long work previously conflated a bounded worker attempt with the lifetime of
the task. Iteration, goal-turn, or runtime budget exhaustion counted as a task
failure. Repeating that twice could auto-block valid work even when each
attempt made progress. Review findings lived mainly in prose, so a late P0/P1
could be missed after implementation reopened. Context grew through repeated
handoffs, while ad hoc cleanup guessed tmux names from assignees. Provider
instructions were advisory and could be bypassed by a fallback route.

## Authority map

| Concern | Durable authority | Continuation behavior |
|---|---|---|
| Objective and dependency graph | Existing Kanban task graph | Referenced, never copied into a new queue |
| External acceptance and status | Jira | Jira keys become explicit authority references |
| Per-attempt route and tools | Existing immutable RunSpec | Preserved and checked as before |
| Bounded worker context | `continuation_manifests` | Immutable, digest-addressed snapshot per run |
| Critical review state | `continuation_blockers` | Live P0/P1 completion gate with resolution evidence |
| Execution budget | Current `task_runs` epoch | Checkpoint and requeue; does not consume task failures |
| Process/worktree/tmux cleanup | `continuation_owned_resources` | Exact task/run/claim and birth identity only |
| Provider selection | Sealed manifest policy | Primary, complete fallback chain, and runtime switches checked |

## Claim and bootstrap sequence

1. The existing dispatcher selects and atomically claims a ready task.
2. Existing workspace resolution completes and the exact path is persisted.
3. If continuation is enabled, Hermes creates one immutable manifest for the
   current `task_id` and `run_id`.
4. The manifest contains the objective, checkbox acceptance criteria, explicit
   `Decision:` comments, Jira/Kanban/artifact references, provider policy, and
   Git HEAD/branch/dirty digest when the workspace is a repository.
5. The context compiler renders a required core plus a bounded current working
   set. The core may not be truncated. The default total is 48 KiB; omitted
   working evidence remains retrievable through authority references and a
   source digest.
6. The existing spawn receipt receives the manifest digest. After the existing
   PID start gate opens, CLI preflight reloads the active task/run, validates
   the digest and Git checkpoint, and checks the requested provider before
   provider construction.
7. Agent attachment checks the whole fallback chain. Every later runtime route
   observation checks the active provider before the switch proceeds.

Any deterministic preparation or bootstrap failure is persisted as a typed
event and fails closed. `hermes kanban show` and `kanban_show` expose the active
digest, byte budget, open blockers, and most recent bootstrap failure.

## Execution epochs and convergence

An iteration, goal-turn, or maximum-runtime budget bounds one execution epoch,
not the task. A manifested run closes with `outcome=checkpointed`, the task
returns to `ready`, and the unchanged dispatcher may claim a new run with a
fresh context snapshot. `consecutive_failures` is not incremented.

There is no fixed number of productive implementation/review cycles. Hermes
blocks only after three consecutive checkpoints have the same semantic digest.
The digest uses summary, explicit stable progress evidence, and Git state; it
ignores volatile PID, elapsed-time, and trigger wording. This bounds a true
no-progress loop without making a long but productive task impossible.

## Review gate

`kanban_comment` can add a typed P0-P3 review finding or resolve one with a
required evidence reference in the same tool invocation. Open P0/P1 findings are read inside the
same transaction that would mark a task done. Completion stays closed when one
or many critical findings exist, including a finding added after implementation
was reopened. New findings do not mutate the immutable prompt; they are visible
through status and become part of the next run's snapshot. P2/P3 are advisory.

## Cleanup safety

Assignee-derived tmux guessing is removed. A resource is eligible for cleanup
only when its creator registered an exact task, run, claim, type, and immutable
identity. Tmux cleanup re-reads both session id and creation time before kill;
child processes use PID plus process birth time; worktrees must be listed by the
owning Git repository. Missing or mismatched identity fails closed and emits a
queryable cleanup event. Unregistered resources are never touched.

## Configuration and rollout

The upstream default is disabled. The Ahlnos managed configuration enables the
feature for all Hermes profiles with a 16 KiB core and 48 KiB total budget. Its
sealed policy denies both `openrouter` and the `nous` alias backed by the same
provider plugin. Empty allow means other configured providers remain available.

Rollout does not require a schema migration command: normal database
initialization creates the additive tables. A safe cutover is:

1. Merge and install the Hermes code.
2. Validate and sync the managed configuration.
3. Drain the sole orchestrator dispatcher using the existing gateway procedure.
4. Restart only through the established multi-profile supervisor path.
5. Canary one orchestrator-created multi-profile graph and verify prepared,
   runtime-observed, checkpoint/reclaim, blocker, and completion events.

Rollback is configuration-only: disable `kanban.continuation.enabled`. Existing
manifested runs remain auditable; new runs immediately use the legacy path.

## Verification contract

Automated coverage must keep these cases green:

- legacy disabled behavior;
- orchestrator-compiled multi-profile graph through the existing dispatcher;
- canonical manifest readback and bounded context;
- denied primary and fallback providers, including a persisted failure event;
- Git drift between preparation and bootstrap;
- multiple and late P0/P1 findings with evidence-backed resolution;
- more than two productive epochs and three identical no-progress checkpoints;
- exact tmux identity mismatch without a kill;
- goal, iteration, and runtime budget compatibility;
- existing RunSpec, spawn-attach, context-budget, and Kanban tool suites.
