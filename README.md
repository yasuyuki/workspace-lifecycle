# Workspace lifecycle

`workspace-lifecycle` is an independently installable Python 3.10+ package for
the workspace contract that Git does not provide. Build or install it from this
directory; it is not published to PyPI. It imports no agent-rules
checkout, private runtime, rule placement or inventory code.

Git owns worktree creation and relocation, branch and HEAD identity,
upstream/default discovery, normal merges and locks. The package records only task identity,
request, parent/dependencies, validation and push preflight, acceptance,
explicit holds, retirement requests and process-use leases. A legacy registry is
rejected; there is no automatic migration. Installing the package does not adopt
an existing consumer or deploy a live hook/runtime.

From this directory, install into an isolated Python environment:

```console
python -m pip install .
workspace-lifecycle --help
```

## Lifecycle

Begin a task with an explicit request, remote, branch, absolute worktree,
validation argv and preflight argv. The preflight must contain the literal
`{repo}` placeholder. `status` reports the selected task and its live Git state;
`hold` records a reason and next action. Parent and dependency tasks must
already exist and are checked again before integration.

`update-preflight` changes one existing current task's preflight argv without
adopting it again. Supply `--task`, the exact saved argv as
`--expected-preflight-json`, a nonempty `--preflight-json` containing literal
`{repo}`, and a durable `--evidence` reference. The command rejects stale argv,
active use leases and pending operations. It records the latest from/to argv and
evidence while preserving acceptance, holds, dirty baseline, Git identity and
all other task fields. Compare old and new policy decisions in the task checkout
before a migration; this command does not run either preflight or push.

Use `update-validation` from the bound task checkout to correct a misregistered
validator before acceptance. Review the old and replacement commands against the
same correctness requirements, then use the command's help for the exact argv
and full HEAD comparison inputs. The replacement runs in both the candidate and
integration checkout during ordinary `finish`; prefer checkout-relative inputs
that inspect the source being accepted. Do not point it at an unaccepted source
placed in main just to make validation pass.

The correction retains the task, dirty ownership, holds, dependencies, and
preflight. Each correction records both commands, identity, HEAD, and evidence.
It does not execute validation or grant acceptance. Active use, pending lifecycle
or Git operations, and accepted tasks are refused. Old acceptance remains tied
to its original validator. Resolve a pending operation through its existing
recovery contract before attempting a correction.

Use `adopt-existing` instead of `begin` to continue an existing checkout, including a nondefault primary checkout or a
linked checkout nested below it.
Supply `--task`, `--request`, `--remote`, `--branch`, absolute `--worktree`,
the full commit OID as `--expected-head`, `--validation-json`,
`--preflight-json` (including `{repo}`), and a durable `--evidence` reference
explaining why this Git identity belongs to this continuing request. Optional
`--parent` and repeated `--dependency` refer only to existing current tasks.
An existing execution hold requires both `--hold-reason` and `--next-action`;
being unaccepted alone does not require a hold. Preserve fixed source pins and
outstanding product acceptance explicitly in the request and validation.

Adoption does not run validation or create acceptance/integration receipts.
Existing dirty, staged, untracked and ignored files remain baseline-owned,
and `finish` cannot claim them as this task's commit, restore or archive work.
Git administrative locks are preserved. Nested repositories remain separate ownership
boundaries: adoption snapshots their checkout/Git identity and HEAD, without
traversing or owning their contents. Their dirty state belongs to their own
contract. Finish plans cannot operate inside a nested Git checkout. Primary
checkouts can continue and finish, but cannot request or execute retirement. Because a primary
checkout is never retired, its finish does not block on ignored data or on baseline entries whose
status is unchanged; the acceptance records their count and digest. Default branch checkouts, conflicting
bindings, active Git operations, protected indexes, and unsafe filesystem paths
are refused. An interrupted adoption retains an exact contract and filesystem,
index and dirty-content snapshot: retry the same arguments, with the same
identity and content. Changes are refused without discarding the intent or
silently establishing a new baseline. A binding written before interruption
is not an accepted task; runtime use remains refused until adoption completes.
Keep native Git writers and external filesystem users quiescent during adoption:
the lifecycle lease serializes lifecycle clients, not arbitrary outside writers.

An active `agent-branches/state.json` prevents adoption. Stop the old consumers
in an explicit maintenance interval, preserve and verify state/hooks/config and
rollback evidence, then detach the old authority before adopting continuing
tasks in dependency order. No legacy schema is imported. Historical residue
without continuing work does not belong in this API. After adoption, use the
ordinary `status`, `run`, reviewed `finish`, integration and retirement contract;
unmet acceptance or holds remain unmet, including for already-pushed branches.

Finish from the bound task worktree with a durable result reference and a JSON
plan. The plan may contain `commit`, `restore`, `archive`, and `exception`
only. Each file entry names an exact relative path, classification, task owner,
ownership evidence and SHA-256. `commit` entries additionally require
`safe_to_commit: true`; ignored, unknown, secret, user-owned and other data are
never committed automatically. `restore` requires verified `regeneration`
evidence. `archive` requires an existing absolute authorized store outside every
Git worktree, approval evidence, and readback verification before the source is
removed. A remaining dirty tree can finish only through the strict exception
record: all four alternatives (`commit`, `restore`, `archive`, and
`owner-resolution`) must be reviewed with irreversible-harm evidence, a remaining
owner and a next action.

Finish validates before and after the owned actions, commits only the planned
source, runs the configured push preflight, and verifies the remote tip. It then
follows the declared parent or remote-default integration edge using the normal
Git `merge --no-ff --no-commit` contract, revalidates, pushes and records the
result. A hold, dependency failure, changed accepted HEAD, dirty target, conflict,
or failed validation remains a refusal to resolve in the existing task. Retry the
same plan after repairing the operation. If the ownership review or required
cleanup changes, `--revise-plan-evidence` explicitly records that decision and
retains completed effects; it cannot replace an unresolved archive/restore or a
changed commit identity. For an untouched pending finish whose owner added
commits, supply `--revise-plan-head` with the exact descendant commit OID and
`--revise-plan-evidence` with the durable review. The old intent and old/new
HEADs remain in the task's finish receipts, including after retirement. The
review can also replace the plan; the result reference stays the same. Once
this revision is saved, retries must use that HEAD, evidence and reviewed plan.
Active Git operations, changed task/worktree identity, preservation actions,
and lifecycle-generated commits prevent this recovery. No state editing or
discard is needed.

### Continuing the same remote topic

A task denotes one deliverable, not one agent session. Resume its existing
branch and worktree. Across clones and hosts, the request must identify one
current update owner for that same remote topic; handing over ownership requires
quiescing the previous writer and recording the handover in that request. Local
leases serialize lifecycle users of one clone, not writers on other hosts.
Separate only independent deliverables that actually need concurrent updates.

`sync --task TASK --expected-head LOCAL_OID --remote-head REMOTE_OID --evidence REF`
repairs the existing task using its registered remote and same-name branch.
Both OIDs are full commit IDs; the durable evidence identifies the continuing
request, update owner and reviewed merge. Inspect the current local/remote OIDs
and dirty ownership before invoking it. Equal or locally ahead histories stay
unchanged; lagging history fast-forwards; divergence uses an ordinary merge.
Synchronization does not push, accept a task, or integrate it into its parent.
Use normal push or the existing `finish` afterward, as required by the task.
Git 2.38+ is required for synchronization's native `merge-tree` contract.

A manually started merge can be adopted when its HEAD and MERGE_HEAD are those
exact local/source OIDs. The source must still be preserved in the actual
same-name remote, including when that remote advanced after the merge began.
The native expected merge tree and conflict paths are pinned: resolve conflicts
in the same checkout and stage only those resolutions, then repeat the identical
sync arguments. Unrelated dirty/staged paths, nonconflict changes, wrong sources,
rewritten remote history, other operations and changing identities are refused.
Completed or interrupted merges retain their exact parents/tree and are resumed
without creating another merge commit. If the remote advances during validation,
retry the pinned synchronization, then synchronize its result with the new tip.
Native writers must remain quiescent during these operations.

After `finish` commits but its push is rejected, use this same task's `sync`,
then repeat the same finish plan and result reference. The original finish
commit, plan and completed preservation actions remain intact. Revalidation
covers the new merged HEAD; already committed source entries are not committed
again even if a reviewed conflict resolution changed their contents. A push with
an uncertain response or an immediately advanced remote succeeds only after
Git proves that remote preserves the exact validated commit. The third-party
remote tip is recorded as observed, never substituted for the accepted commit.
Integration uses the same synchronization operation to resume its own target;
parent acceptance changes only after the complete child/parent validation and
normal push. Conflicts and interruptions remain in the existing task.

Retirement requires the exact accepted result, completed integration, explicit
external-user release, unchanged worktree identity and accepted HEAD, and an
invocation outside the target worktree. It checks preexisting dirty data, then
moves the linked worktree to a recovery path on the same filesystem with
`git worktree move`. It archives only that worktree's Git admin directory under
`workspace-lifecycle/recovery-admin` in the common Git directory. The payload,
empty directories, branch, commit and admin directory remain available for
recovery, while the target leaves the active Git worktree list. The retired
receipt records both recovery paths and identities; no payload or admin bytes
are deleted. `retire --pending` retries durable requests after interruption.
`finish` and `retire` accept `--reclaim-preservation-evidence`. Finish may
store that evidence before external users are released. Acceptance and an
explicit final-user release persist a pending request in the same state change,
so interruption before the retirement callback does not lose the request.
Retirement with preservation evidence immediately attempts reclamation.

`reclaim --pending` resumes only recorded requests; it neither discovers targets
nor authorizes new deletions. The original retirement manifests and captured
member identities remain unchanged. Each exact removal intent is persisted
before its filesystem effect. Missing members are accepted only when that
member's removal was intended. Changed objects, unknown bytes, links, mounts,
nested repositories and files with multiple hard links remain held. Failure
reasons and the original request survive for the next ordinary owner entry.
An already absent intended member is never counted as newly removed.

Retirement and reclamation retain a task lease while network, hashing and
filesystem operations run outside the common state lock. Short compare-and-swap
updates re-read current state and replace only the selected task's records.
Linux disposal uses pinned directory descriptors, no-follow opens and mount
identities, with a private quarantine namespace. Windows verifies and deletes
through the same restrictive file handle; sharing conflicts leave requests
pending. Both require every producer and external user to obey the owner lease
and release contract. Arbitrary writers into the private quarantine namespace
are not made safe by hashing or renaming. Unsupported filesystem capabilities
fail closed. Explicit `reclaim` also remains available for late evidence.

A lease is separate from Git's administrative lock.

`run` supervises one task checkout. Linux uses a native subreaper and Windows a
native job to track descendants. A normal child exit does not prove that an
arbitrary detached external user released the workspace; explicit release
evidence is still required. On Windows, a caller whose current directory remains
inside the task still holds a directory handle; `--users-released` cannot turn
that use into a removable worktree. Release that caller before retrying pending
retirement. `lease-status` inspects a lease and
`lease-release` recovers exactly one dead-owner lease after review.
On Windows, another process in that use's Job can start a managed agent with
the inherited exact use token. It shares the existing receipt; the outer
supervisor releases it after all Job members exit. A process outside the Job
cannot join by copying the token.

Ordinary task use needs no default-branch checkout or remote-default lookup.
Startup and exit retry only the selected task's recorded recovery, from the
primary checkout. Integration still requires its actual destination checkout.
Failed recovery remains pending and refuses new use of that released task;
other tasks can continue. Explicit `retire --pending` and `reclaim --pending`
process recorded requests across the repository.

Runtime launchers use `resolve-run` with the already selected effective cwd and
the original invocation cwd. It runs unmanaged work without creating lifecycle
state, refuses legacy or incomplete managed bindings, and delegates managed work
to the same `run` supervisor. Managed children receive the bound repository and
task and a fixed `finish_argv` prefix in `WORKSPACE_LIFECYCLE_CONTEXT`, plus
`WORKSPACE_LIFECYCLE_REPO` and
`WORKSPACE_LIFECYCLE_TASK`; they must still call `finish` with a reviewed plan
and durable result reference. Use command help for the exact arguments.

Use the installed command's `--help` for exact arguments and JSON output. The
package's tests and workflow build an isolated wheel and run its tests from
outside the source checkout on Linux and Windows with Python 3.10 and 3.12.

The checkout shim is a one-way entry to this package, not a legacy schema
adapter. Existing `agent-branches` hooks must stay on their pinned source until
an explicit migration verifies task bindings, accepted identities, pending
operations, leases and a rollback source. This version neither installs replacement
Git hooks nor intercepts arbitrary direct Git commands or forced process exits.
The runtime owner can use the CLI without importing placement or inventory code.

## Producer completion receipts

Managed `run` and `resolve-run` children receive `owner_receipt_argv` and an
external `owner_receipt_dir` in `WORKSPACE_LIFECYCLE_CONTEXT`. A producer registers
its exact generation, durable receipt, absent output paths and completion argv
before writing output. These are receipts on the existing task record, not a
filesystem discovery service. `register-owner-receipt` rejects tracked source,
preexisting data and changes to an already bound generation.

A reviewed finish may accept source while these exact declared outputs remain.
After validation, push and integration, acceptance plus explicit user release
makes the registered owner callbacks pending. Callbacks receive the same exact
generation and result reference on every retry and must preserve their own
identity/hash/provenance evidence outside the retiring tree. Reclamation must
be confirmed both by the owner result and by its durable receipt before the
task can retire. Failures remain pending and normal owner startup/completion
retries them. Unaccepted tasks, held tasks and unfinished output use do not
invoke deletion. The lifecycle package never infers artifact ownership from
mtime, ignored files or directory names.

## Source ownership

This repository is the current editing and distribution owner. Initial source
was extracted from `yasuyuki/agent-rules` revision
`f6f2b027700a14232c87cc36287387242fa1fddf`, `packages/workspace-lifecycle/`.
Historical revisions remain in that repository. Version 0.3.1 retains the 0.2.1
state schema and existing public CLI, extending existing-work adoption to
nondefault primary and nested checkouts with separate repository ownership.
The pre-retirement filesystem scan distinguishes empty unowned directories from
untracked or ignored files by contents. Nonempty unowned data, links, reparse
points, mounts, submodules and special files refuse retirement before the move.
Later bytes are retained in the original or recovery path. A failed move or admin
archive leaves a durable retirement request for an explicit retry.

Version 0.3.2 decodes Git text output explicitly as UTF-8, including runtime
root discovery and completion pushes on Windows. It preserves the state schema,
CLI, supervisor and inherited environment; no encoding wrapper is required.

Version 0.3.3 retains the version 1 state schema and existing CLI. Retirement
holds linked worktree payload and exact Git admin information in recovery,
including bytes that appear after the initial dirty check. It resumes each
durable phase without editing state and does not perform physical cleanup.

Version 0.3.4 adds the exact argv compare-and-swap `update-preflight` command
for current tasks. The version 1 state schema and recovery retirement remain.

Version 0.3.5 adds `reclaim`. It deletes a retired payload and its archived
admin only when preservation evidence is supplied and both trees still match
the manifests recorded at retirement. Retirement itself still does not delete.

Version 0.3.6 lets `finish` and `retire` accept `--reclaim-preservation-evidence`
and start the same reclaim after a successful retirement when that evidence is
present. Retirement remains no-delete; safety refusals keep the retired payload.

Version 0.4.0 adds resumable member deletion intents, task-scoped reclamation
transactions, pending-request durability across finish callbacks, and exact
producer completion receipts on existing tasks. Older retirement manifests
are preserved; they are never rebuilt from a partially deleted tree.

Version 0.4.1 preserves the registered callback executable path so a virtual
environment keeps its own Python modules during completion and reclamation.

Version 0.4.2 lets Windows descendants safely share one supervised task use.
The owner remains responsible for normal receipt cleanup; abnormal recovery
still requires explicit review.

Version 0.4.4 lets an adopted primary checkout finish while it keeps ignored
workspace data and unchanged baseline entries.
