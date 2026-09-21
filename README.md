# Workspace lifecycle

`workspace-lifecycle` is an independently installable Python 3.10+ package for
the workspace contract that Git does not provide. Build or install it from this
directory; version `0.2.1` is not published to PyPI. It imports no agent-rules
checkout, private runtime, rule placement or inventory code.

Git owns worktree creation, branch and HEAD identity, upstream/default discovery,
normal merges, locks and removal. The package records only task identity,
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
changed commit identity. No state editing or discard is needed.

Retirement requires the exact accepted result, completed integration, explicit
external-user release, unchanged worktree identity and accepted HEAD, and an
invocation outside the target worktree. It verifies the full filesystem, removes only empty unowned directories with
empty-only operations, rechecks the target, and asks Git to remove the linked worktree, and preserves
the branch and receipt. `retire --pending` retries only durable requests; it does
not scan for cleanup. A lease is separate from Git's administrative lock.

`run` supervises one task checkout. Linux uses a native subreaper and Windows a
native job to track descendants. A normal child exit does not prove that an
arbitrary detached external user released the workspace; explicit release
evidence is still required. On Windows, a caller whose current directory remains
inside the task still holds a directory handle; `--users-released` cannot turn
that use into a removable worktree. Release that caller before retrying pending
retirement. `lease-status` inspects a lease and
`lease-release` recovers exactly one dead-owner lease after review.

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

## Source ownership

This repository is the current editing and distribution owner. Initial source
was extracted from `yasuyuki/agent-rules` revision
`f6f2b027700a14232c87cc36287387242fa1fddf`, `packages/workspace-lifecycle/`.
Historical revisions remain in that repository. Version 0.2.1 retains the 0.2.0
state schema and public CLI. Empty directories are identified by filesystem
contents, never names. Nonempty unowned data, links, reparse points, mounts,
submodules and special files still refuse retirement. A concurrent file creation
or failed Git removal preserves the retirement request for an explicit retry.
