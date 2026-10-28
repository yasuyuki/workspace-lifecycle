# Workspace lifecycle

Git worktrees make parallel tasks possible, but Git does not know when a task is
accepted, whether another process still uses its checkout, or who owns generated
files. `workspace-lifecycle` records those decisions per task. It can create a
linked worktree, run a task under a process-use lease, validate and integrate a
reviewed result, then retire the checkout after its users release it.

Start with `begin` for a new task or `adopt-existing` for a checkout already in
use. Use `status` to inspect it and `run` to supervise a command. `finish`
requires an explicit file-ownership plan and result reference; it can commit,
push, and integrate the reviewed result. `retire` requires that accepted result
and an explicit release from external users. See `workspace-lifecycle --help`
and each subcommand's `--help` for the exact arguments.

Retirement removes a linked checkout from Git's active worktree list while
**retaining its payload and Git admin data in recovery storage**. It does not
immediately recover disk space. `reclaim` removes only the exact retired data
covered by preservation evidence and unchanged retirement manifests; refusals
remain pending for review. This safety tradeoff matters if checkouts contain
large build outputs. Existing primary checkouts cannot be retired.

`workspace-lifecycle` is an independently installable Python 3.14 package for
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

To test the source of a checkout, run `python -B tests/run.py` from it with
Python 3.14 and Git on `PATH`; it needs no other package. It copies the
checkout's tracked and untracked, non-ignored files to a temporary directory, so
uncommitted changes are tested and no bytecode or build output is left behind.
A disposable virtual environment imports that copy, never an installed
`workspace-lifecycle`, and each test runs in its own process. Inherited
`WORKSPACE_LIFECYCLE_*` bindings, `PYTHONPATH`, `PYTHONHOME` and `VIRTUAL_ENV`
are removed from the tests' environment. Name unittest modules or tests to run a
subset; `-j` sets the number of concurrent tests, by default 8 or the processor
count if lower. Failed tests are printed with their output and the exit code is
1; a test that fails to import or an empty selection also fails. CI runs the
same entry with `--wheel` on the built wheel. To validate a lifecycle task of
this repository with it, register for example:

```console
--validation-json '["C:/path/to/python.exe", "-B", "tests/run.py"]'
```

## Lifecycle

Begin a task with an explicit request, remote, branch, absolute worktree,
validation argv and preflight argv. See `begin --help` for validation checkout
placeholders. A validation runs in its checkout with the caller's environment
minus every `WORKSPACE_LIFECYCLE_*` variable, whatever the letter case, so a
session bound to another task cannot change its result; it receives no task
binding of its own. The preflight must contain the literal
`{repo}` placeholder. An element equal to `{lifecycle_python}` runs with the
interpreter of the lifecycle that is finishing the task, so a preflight such as
`["{lifecycle_python}", "-m", "workspace_lifecycle.push", "{repo}"]` keeps
working after the lifecycle install moves or changes version. Name an absolute
interpreter instead when the preflight belongs to a different runtime. `status` reports the selected task and its live Git state;
`hold` records a reason and next action. Parent and dependency tasks must
already exist and are checked again before integration.

The bundled preflight `python -m workspace_lifecycle.push <repo>` prints one
JSON object with `decision` (`push`, `hold` or `ask`), `reason`, `remote` and
`destination`, plus `push_argv` only for `push`. Only `push` exits 0. `hold`
exits 10, `ask` exits 11, and 2 is invalid usage, so `preflight && git push ...`
pushes nothing after `hold` or `ask`. Run the reported `push_argv` rather than
a hand-written push. After `ask`, resolve the missing information or obtain the
user's decision before running the preflight again. Finish reports `push
blocked: <reason>` for `hold` and `ask`, including exit 0 from an older
preflight, and refuses without pushing when the exit status contradicts the
decision or stdout is not one decision object.

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
to its original validator. A finish that failed before recording any effect
(no archive, restore, staged index, commit tree, commit or synchronization) on
the same checkout and HEAD is released by the correction: its intent moves to
the task's finish receipts with the correction, and the next ordinary `finish`
starts a new one. A finish whose validation failed after its commit and completed
actions stays pending with them. The correction then requires HEAD to be that
recorded commit, and the next `finish` with the same plan validates and pushes
it. Resolve any other pending operation through its existing recovery contract
first.

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

For an explicitly identified foreign retired payload, use optional
`adopt-existing --preserved-data-json` with an array of objects. Each object
requires these exact fields:

| Field | Required observation or reference |
| --- | --- |
| `path` | Canonical relative path of the individual retired payload, outside `.git`; no recovery-directory name is special. |
| `owner` | Foreign owner identity, distinct from the adopting task. |
| `receipt_ref` | Durable reference to that owner's existing retirement receipt; the receipt stays with its owner. |
| `evidence` | Durable reference to the explicit protection contract and current payload/admin/path inspection. |
| `identity` | Current payload directory `[st_dev, st_ino]`, from this host. |
| `admin_archive` | Object with absolute `path` and current directory `identity: [st_dev, st_ino]` for the existing archived admin. |
| `unresolved` | Array of outstanding conditions; empty only if the protection evidence has no outstanding conditions. |

The owner must first map each receipt, payload and archived admin using existing
private evidence and quiesce native writers. Use current identities for the
boundary, preserving old device disagreements, missing manifests/remotes and
unknown reclaim eligibility verbatim in `unresolved` and the referenced evidence.
Do not repair the marker, rewrite a receipt, move data, or borrow a guessed
validator to obtain adoption. This contract grants protection only: it does not
certify retirement completeness, acceptance, integrity or permission to reclaim.

This narrow boundary requires an existing regular native `.git` file naming an
absolute former admin path that is absent, plus the explicitly identified
existing archived admin directory. A live admin, active worktree, tracked source,
link/reparse/mount traversal, changed anchor or marker is refused. Normal live
nested Git keeps its existing independent protection; unknown broken Git outside
the exact declared boundaries still stops adoption. Payload and admin interiors
remain opaque: there is no manifest recapture or content ownership transfer.
Links or mounts *inside* opaque data are never traversed or authorized by this
contract; the referenced owner evidence retains any unverified conditions.

The existing task adoption record stores only boundary identities, references,
unresolved conditions and the marker fingerprint. There is no additional ledger
and no receipt/content import. Named payloads and any archived admins inside the
checkout join its existing `protected_paths`; Git dirty inventory excludes those
exact paths without adding Git ignore rules. Existing branch/HEAD/index and dirt
outside them stay unchanged. Retrying an interrupted adoption requires the same
contract and anchors, including the same unresolved conditions.

Managed entry checks, finish and producer registration recheck the anchors.
Commit/restore/archive source paths, archive destinations and producer outputs
cannot overlap a protected
boundary, even by an ancestor path or spelling alias. Producer checks also protect
external archived admins and boundaries belonging to other tasks or unfinished
adoptions in the same Git common. Existing active producer claims stop adoption
of an overlapping foreign preservation boundary. Sync and child integration
reject incoming Git changes there before merging. A checkout retaining foreign
preserved data cannot retire, so its retirement/reclaim never acquires that data.
Use the existing owner separately for any future recovery/reclaim. Managed cwd
selection cannot enter the preserved data, including external archived admins
and other tasks' or pending adoptions' boundaries in the same common. Lexical cwd checks run before link resolution so a path
inside preserved data cannot escape through an internal link. Arbitrary native commands are not a
filesystem sandbox: keep their permissions and the real validator consistent
with the referenced owner boundary, and keep external writers quiescent.

An active `agent-branches/state.json` prevents adoption. Stop the old consumers
in an explicit maintenance interval, preserve and verify state/hooks/config and
rollback evidence, then detach the old authority before adopting continuing
tasks in dependency order. No legacy schema is imported. Historical residue
without continuing work does not belong in this API. After adoption, use the
ordinary `status`, `run`, reviewed `finish`, integration and retirement contract;
unmet acceptance or holds remain unmet, including for already-pushed branches.

Finish names the bound task worktree with `--repo` and takes a durable result
reference and a JSON plan. Run it from a directory outside that worktree when it
should also retire it: a checkout cannot move while a shell's current directory
is inside it on Windows, so `finish --users-released` started inside the
worktree records the retirement request and returns `retirement.next`, the
`cwd` and `argv` of the one `retire` command that completes retirement and its
reclaim from outside. The plan may contain `commit`, `restore`, `archive`, and `exception`
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
Without that record, dirt no plan entry covers, and dirt or a merge already in
an untouched integration target, are refused before validation, commit or push,
naming the first paths. Such a refusal of a finish with no pending intent records none, so
a corrected plan needs no revision evidence. Dirt that validation creates is
still refused afterwards.

A repository that relies on remote CI declares the checks every task's
integration needs in a tracked `.github/required-checks.json` on its remote
default branch: a JSON array of `{"app", "name"}` GitHub check runs such as
`{"app": "github-actions", "name": "test (ubuntu-latest, 3.14)"}`. The
repository's workflow defines the checks; the declaration names them exactly.
`begin` and `adopt-existing` read it from the freshly fetched remote default
commit, also for a child task, so an unaccepted change in a task or parent
branch cannot weaken it. Without `--required-checks-json` the task requires the
declared set. An explicit array may add checks but must include every declared
one, so `[]` is refused while a declaration exists. Only an absent file means no
declaration; an invalid file, a non-regular entry or an unreachable remote is
refused. The task records the declaration's commit, blob and checks as
`required_checks_policy`.

Before creating an integration merge, finish reads the declaration again from
the remote default branch. A check declared since the task began refuses
integration until `update-required-checks` adds it; a removed declaration
leaves the task's recorded requirement in place until an update removes it.
`update-required-checks` applies the same rule against the current declaration
and records it with the change. When the accepted commit itself changes the
declaration from both the remote default one and the one at their merge base,
finish reads that declaration from the commit's Git tree and also requires each
check it declares; checks the task already requires stay required, so a removal
or rename cannot weaken the gate. An invalid or non-regular declaration there is
refused. The merge intent and integration record keep it as
`source_checks_policy` with its base and `added` checks. A pending integration
keeps the decision it started with. Finish then asks GitHub through the `gh` CLI for those check runs on the exact
accepted commit in the repository the remote pushes to. Only the latest run of
each app and name counts, and only `completed` with conclusion `success` passes.
Runs of one workflow for both `push` and `pull_request` on the same commit share
that identity, so the newer one decides, as in GitHub branch protection. A remote
with several push URLs is refused.
A task whose integration requires checks is not validated in its own checkout.
Finish commits, pushes the topic at once so its checks start, and records the
pushed commit as `candidate`, not as an acceptance. The candidate becomes the
task's `acceptance` only when the checks succeeded on that exact commit and the
target checkout validated the exact commit about to be pushed there; the
acceptance then records that run as `validation.integration` beside the check
evidence. So one local validation runs per finish, on the target's final HEAD,
and a check the validator runs but CI lacks still precedes every push to the
target. A dependency, a child integrating into this task, retirement and owner
receipts all see no acceptance until then. When the target has moved, that
validated commit is the merge commit, not the topic commit: the merge's own
content is what reaches the target, and the topic commit is covered by its
checks. Tasks without required checks, and tasks whose checks cannot be read
when finish starts (an unreadable declaration or a non-GitHub push URL), still
validate their checkout before the push and accept there. Integration then
refuses on its own reading as before. `update-required-checks` drops a
candidate, so the next finish decides again for the new requirement.
Queued, running, failed, cancelled, skipped, neutral, missing or unavailable
results refuse integration, leave the candidate or acceptance in place and change no target.
Retry the same finish after CI completes; a new HEAD needs its own successful
checks. `finish --wait-checks [SECONDS]` instead waits for them after
the push, holding no lifecycle lock or lease, and then integrates as usual.
It asks GitHub every 30 seconds and stops as soon as any required check
completes without success, naming each failed check with its state, run ID and
GitHub URL. Missing, queued and running checks keep it waiting until SECONDS
have passed; the default, 21600, is GitHub Actions' default job timeout.
While those checks run, finish already integrates the accepted commit into the
target checkout (fast-forward or merge) and validates it there, but pushes
nothing until the checks succeed. The push then reuses that validation when the
target commit and checkout are unchanged; a synchronization with a moved remote
validates again. Meanwhile the target holds this pending integration, so no
other task integrates into it. A failure, timeout, unavailable status, refusal
or interruption leaves the candidate in place and returns the target to its
HEAD before the integration, so repeating the finish resumes from the
candidate. A finish without the option withdraws such an interrupted
integration when the checks have not succeeded, or pushes it after validating
again when they have. State changes are reported on
stderr; stdout keeps the one final JSON result. Without the option finish
refuses at once, as before. That retry reuses the saved candidate or acceptance and neither validates nor pushes
the task checkout again when the checkout is completely clean and its HEAD,
plan, result reference, checkout identity, validation and preflight argv,
remote fetch and push URLs and remote topic tip are those it
recorded. Any difference, data waived at the push, or an acceptance saved by
an earlier version without these inputs finishes again. With `--users-released`,
a candidate's release is saved only after integration. The interrupted merge keeps that proof and is refused if the remote's
repository changes. Tasks without required checks never query GitHub. Change an
existing task's requirement with `update-required-checks`, which compares the
exact current array and records durable evidence; it is refused while an
operation is pending. Topic HEAD success does not prove the merge result itself.
With `--wait-checks`, finish therefore waits again, up to SECONDS and by the same
rules, for the required checks on the exact commit it pushed to the remote
default branch. When the task integrates through a parent, that is the parent's
push to the default branch, never the merge into the parent branch. That commit
requires each task's checks on the way, the checks its accepted commit added,
and the checks its own `.github/required-checks.json` declares, so a later
declaration on the default branch counts and nothing is weakened. Only runs
pushed to the default branch count, so when integration fast-forwarded the
default branch to the accepted commit, that commit's runs from the task branch
do not. Runs on a later default-branch commit, of another app or of another
name do not count either. The result is `integration_checks` in the finish JSON and in
the integration record of that edge (`recorded_on` names its task): the
destination, full commit SHA, repository, each check's state, run ID and URL,
and `state`. `success` and `undeclared` (nothing is declared, so nothing is
claimed) let finish continue to `--users-released` retirement. `failed` (at the
first failed check), `pending` (SECONDS ran out) and `error` (status
unavailable) exit 1 with `"ok": false` and a `next` action; `--users-released`
saves no retirement request, so `retire --pending` leaves the task. The
integration stays recorded and is not repeated: run the same finish
again to wait or to record a rerun check, and fix a failure in a new task or
revert the merge. The wait holds no lifecycle lock or lease; the result is
recorded only if that integration record is unchanged. Without the option
finish does not look at the integrated commit.
Repositories without a declaration behave as before, except that begin, adoption and finish fetch the remote default
branch to look for one.

Finish runs the owned actions, commits only the planned source, validates once
on that exact HEAD, runs the configured push preflight, and verifies the remote
tip. A failed validation keeps the local commit and completed actions in the
pending intent and pushes nothing. Retry the same plan after correcting the
validator, or revise the plan to commit a fix on top. It then
follows the declared parent or remote-default integration edge. When the
target HEAD, after synchronizing with its remote, is an ancestor of the accepted
commit, Git `merge --ff-only` moves the target to that commit and the integrated
commit is the accepted commit itself. Otherwise the normal Git
`merge --no-ff --no-commit` contract makes a merge commit. Finish validates the
resulting commit in the target checkout, pushes and records the result. A
fast-forward still validates in the target, which is another checkout and so
another validation input, and the default branch still runs its own push CI on
that commit. A failed validation leaves the
unpushed integration and its pending intent in the target; finish of the same
task resumes it. Meanwhile, no other task integrates into that target and the task
owning a target branch cannot finish.
Validator output is refused by the dirty check that follows validation. A hold, dependency failure, changed accepted HEAD, dirty target, conflict,
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

`run` supervises one use of a task checkout. Several uses of the same task,
such as two agents working in it, run at the same time; each has its own
receipt. Mutations such as `finish`, `hold`, `sync` and `retire` do not wait;
they are refused while another use is live or an unreleased receipt remains,
and the refusal's `recovery` names `lease-status` and `lease-release`. Inside a
use, a mutation proceeds only when that use is the task's sole one. A new use
is refused only while a mutation of the task is in progress. Linux uses a
native subreaper and Windows a native job to track descendants. A normal child exit does not prove that an
arbitrary detached external user released the workspace; explicit release
evidence is still required. On Windows, a caller whose current directory remains
inside the task still holds a directory handle; `--users-released` cannot turn
that use into a removable worktree. Release that caller before retrying pending
retirement. `lease-status` lists the task's uses and
`lease-release` recovers exactly one dead-owner use after review. A crashed
use does not stop new uses; it stops mutations until it is released. A
0.4 supervisor's exclusive lease and receipt still count as a use, but a 0.4
client cannot see newer uses, so supervise one clone with one version. On
Windows a managed agent started inside a use's Job joins that use; on Linux it
is a second use, so its own `finish` is refused while the outer session runs.
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
state and delegates managed work to the same `run` supervisor. A checkout of a
lifecycle repository that no task is bound to, such as the primary checkout,
and a repository with a legacy registry also run unmanaged; one JSON `notice`
on stderr says that no lifecycle supervises them. That includes a task worktree
whose branch is detached or no longer bound: retirement and the other mutations
find a task's checkout only through its binding, so none of them can act on it.
The exception is a checkout on a remote default branch, or on any branch when a
remote's default is not recorded locally: integration merges into it, so the
session holds an `@default` use, its notice says so, and default-branch
integration is refused until it exits. A held task still starts, with its hold in a notice; the hold keeps refusing `finish` and `sync`. Refusals are a JSON object
on stderr with `error` and, where one exists, `recovery`. Managed children receive the bound repository and
task and a fixed `finish_argv` prefix in `WORKSPACE_LIFECYCLE_CONTEXT`, plus
`WORKSPACE_LIFECYCLE_REPO` and
`WORKSPACE_LIFECYCLE_TASK`; they must still call `finish` with a reviewed plan
and durable result reference. Use command help for the exact arguments.

Use the installed command's `--help` for exact arguments and JSON output. The
package's tests and workflow build an isolated wheel and run its tests from
outside the source checkout on Linux and Windows with Python 3.14.

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

Version 0.4.9 adds explicit foreign preserved-data boundaries to existing-work
adoption, with current anchor checks and unchanged unresolved owner conditions.
It reuses task protection through managed entry, finish, producer, sync and
integration, and refuses retirement of a checkout retaining these boundaries.

Version 0.5.0 lets several sessions of one task run at once and refuses only
the mutations that would act under them. `lease-status` reports a `uses` list
instead of one `receipt`. Unbound checkouts, legacy-registry repositories and
held tasks launch with a stderr notice instead of a refusal, and refusals carry
`recovery`.

Version 0.5.1 also launches a repository that has both a legacy registry and
lifecycle state unmanaged; 0.5.0 still refused it.

Version 0.8.0 resolves a preflight element equal to `{lifecycle_python}` to the
running lifecycle's interpreter. A task whose preflight names its own install
by path needs one `update-preflight` to this form; later install changes need
none. 0.8.0 also requires the checks a task adds to
`.github/required-checks.json` on that task's own accepted commit.

Version 0.8.1 validates once per unchanged checkout within one finish. A clean
task checkout whose owned actions changed nothing reuses its first run, and the
integration target reuses its merge validation unless synchronization followed.
The receipt marks such a stage with `reused`.

Version 0.8.2 validates the integration target once, on the merge commit it
pushes. 0.8.1 validated the uncommitted merge and pushed the commit made from it
unvalidated, so a validator reading HEAD's message or parents could pass a commit
it rejects. A target holding another task's pending merge refuses new integration
and the finish of its own task.

Version 0.8.3 resumes integration from a saved acceptance. Repeating finish
after required checks were missing or running no longer validates and pushes the
unchanged task checkout again or replaces its acceptance. Acceptances saved by
earlier versions lack the recorded finish inputs and are accepted once more.

Version 0.8.5 adds `finish --wait-checks`, which waits for the accepted
commit's required checks and stops at the first failed one.

Version 0.8.6 makes `finish --wait-checks` also wait for the required checks
on the commit it pushed to the remote default branch and report them as
`integration_checks`, separately from the integration. A failed, unfinished or
unavailable result exits 1 without retiring; repeating the finish only checks
again.

Version 0.8.8 integrates and validates in the target while `finish
--wait-checks` waits for the accepted commit's checks, and pushes once they
succeed, instead of starting that work after them.

Version 0.8.9 no longer validates the task checkout of a task whose
integration requires checks. It pushes the commit as a `candidate`, and the
candidate is accepted only after its checks succeed and the target validates
the commit it pushes. Acceptances saved by earlier versions resume unchanged.
0.8.8 ignores a candidate and validates and accepts that task again, but refuses to resume
an integration that 0.8.9 prepared for a candidate; finish or withdraw it with
0.8.9 before a rollback.

Version 0.8.10 starts fewer Git processes for the same checks. Queries made at
the same moment are read in one call, and one operation resolves each
checkout's common directory and each remote's default branch once; the next
operation resolves them again. Checks at different moments, such as HEAD,
dirt, the remote URL around a push and the remote branch tip, still query Git
each time. Task state is unchanged, so 0.8.9 resumes any task directly.

Version 0.8.11 makes `workspace_lifecycle.push` exit 10 for `hold` and 11 for
`ask` instead of 0, so a shell `&&` stops before a withheld push. The JSON is
unchanged. A caller that treated any nonzero exit as a failed preflight now
sees 10 or 11 with the decision on stdout. Finish also accepts exit 0 for
`hold` and `ask`, so a task whose preflight names an older install still
reports the reason. Task state is unchanged. A 0.8.10 finish running a 0.8.11
preflight by absolute path still refuses `hold` and `ask`, but reports them as
`push preflight failed`.

Version 0.8.7 fast-forwards an integration target that has not moved past the
accepted commit instead of making a merge commit with the same tree, so the
default branch carries the accepted commit's SHA. `integration_checks` counts
only runs pushed to the default branch.

Version 0.8.4 makes `finish --users-released` started inside its task worktree
return the retirement request with `retirement.next`, the exact `retire`
command and directory that complete retirement and reclaim, instead of failing
its own retirement on the current directory. Started outside the worktree with
`--repo`, the same finish retires and reclaims in one command.
