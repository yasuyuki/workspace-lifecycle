---
name: verify-parent-integration
description: Model-check that a child task's integration into its parent task, and the parent's integration into the default branch, accept only the parent's own reviewed work, validate the child in the parent before the parent accepts it, record only pushed commits, and bring the child to the default branch when its finish is run again after a kill. Use when a change touches _integrate(), _edge_leases() or _refuse_pending_merge() in src/workspace_lifecycle/service.py, or the merge-intent and acceptance steps of finish() for a task that has a parent or children.
license: MIT
---

# Verify parent integration

**Rules.**

- `ParentAcceptsOnlyItsOwn`: the parent's own work in its accepted commit is work that a parent `finish` accepted.
- `ChildCheckedInParent`: the child's work enters the parent's accepted commit only after the child's and the parent's validation ran on it in the parent checkout.
- `AcceptedIsPushed`: the parent's accepted commit is on the parent's remote topic.
- `ChildRecordInParent`: the child's `integrated` record names work that the parent's accepted commit holds.
- `DefaultGetsAccepted`: the remote default branch receives only commits that the parent accepted.
- `ChildReachesDefault` (a liveness property): running the child's `finish` again, and the parent's when it runs, always brings the child's work to the default branch.

**Harm when they break.** Unreviewed parent work reaches the default branch through a child; the child's tests never run on the combined tree; a recorded acceptance points at a commit no remote holds; a child counts as integrated while the parent dropped it; or a child, and with it its parent, can never be integrated or retired.

`tests/test_lifecycle.py` drives fixed orders. `model/ParentIntegration.tla` is a TLA+ model of `_integrate()` for one child (`--parent p`) of one parent task, followed by the parent's edge into the remote default branch. A commit is the set of works it holds: `p` the parent's accepted work, `c` the child's, `m` a later commit by the parent's session. TLC checks every order of the child's `finish`, the parent's `finish`, one commit by the parent's session, and up to two kills at any step, with retries.

## When to use

Run the check when a diff touches any covered code below. To see what changed since the model was last confirmed:

```sh
sync=$(git log -1 --format=%H -- skills/verify-parent-integration)
git diff $sync -- src/workspace_lifecycle/service.py | grep -nE "^@@|def _integrate|def _edge_leases|def _refuse_pending_merge|parent\[|parent and|dependenc|intent\['merged'\]|state\['intents'\]|_push\(target_worktree|direction\['kind'\]"
```

Empty output means the model still describes the code. Changes only in synchronization, required checks or ignored-file collisions do not change the order of steps modeled here.

## Coverage

Sync commit: the last commit that changed this skill folder, as `sync` above finds it. Each commit that changes this folder states that the model describes the covered code at that commit. So commit a change here only after you have read the covered code and confirmed that every action still matches it.

| Code at the sync commit (`service.py`) | Model |
| --- | --- |
| `_edge_leases()` (lines 1539-1548): the child's lease and the parent's, the parent without `allow_use` | `CStart` takes `lockP` |
| `_integrate()` source and dependency checks (1632-1651), existing record (1666-1669), pending intent identity (1671-1674), `if parent and target_head != parent['acceptance']['commit']` (1693), merge intent save (1715-1721) | `CLock` |
| target HEAD checks, fast-forward or `--no-ff` merge committed at once (1729-1779) | `CMerge`, `CMerged` |
| child and parent validation in the parent checkout, HEAD unchanged (1795-1811) | `CValidate` |
| `_push(target_worktree, ...)` of the parent branch (1825) | `CPush` |
| child `integrated` and parent `acceptance` in one save, intent popped (1826-1837) | `CRecord` |
| `result['parent'] = _integrate(repo, direction['task'], ...)` (1839-1840) | the child process continues with `PStart` |
| `finish()` `_refuse_pending_merge(state, branch)` (986); a pending merge intent needs HEAD at the accepted commit (1040-1044); a new HEAD accepted (verify-validation-update) | `FStart`, `FEnter` |
| the parent's own `_integrate()` into the default branch: source HEAD (1643-1644), existing record, pending intent identity (1674), intent save, merge, validation, push, record | `PStart`, `PLock`, `PMerge`, `PPush`, `PRecord` |
| `run()` in the parent checkout, refused only while a lifecycle mutation holds the parent's lease | `SessionCommits` |
| process killed; the OS drops its leases | `Kill` |

## Run

Requirements: Python 3.14, a Java runtime, and `tla2tools.jar` from TLA+ v1.7.4 (`https://github.com/tlaplus/tlaplus/releases/download/v1.7.4/tla2tools.jar`, SHA-256 `936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88`). The script refuses any other TLC version, because a different TLC can report a different path.

From the repository root:

```sh
python -B skills/verify-parent-integration/scripts/check.py --mutants --java JAVA --tla2tools TLA2TOOLS_JAR
```

`--java` and `--tla2tools` fall back to the `JAVA` and `TLA2TOOLS` environment variables. It takes about 7 seconds. Exit 0 means every result matched; 1 means a mismatch; 2 means TLC could not run.

Each output line is JSON. The script checks five configurations in `model/`. Each also checks `ChildReachesDefault`:

| Configuration | Meaning | Expected |
| --- | --- | --- |
| `child-only` | only the child's `finish` runs; kills anywhere | every rule holds |
| `parent-finish` | the parent's `finish` also runs; no kill inside the parent's edge to the default branch | every rule holds |
| `parent-session` | as `parent-finish`, and the parent's session commits once while no lifecycle process holds the parent | `ChildReachesDefault` breaks: after a kill between the child's merge and its record, the session's commit makes the child's `finish` refuse (`target moved outside the recorded normal merge`) and the parent's `finish` refuse (`pending integration of c holds ...`). Known bug #86 |
| `parent-edge-kill` | as `parent-finish`, with kills inside the parent's edge | `ChildReachesDefault` breaks: the parent's merge intent for its old accepted commit stays, the child moves the parent's acceptance, and every parent edge refuses (`pending integration identity differs`). Known bug #85 |
| `parent-dependency` | as `child-only`, and the child also names the parent with `--dependency` | `ChildReachesDefault` breaks: after a kill between the child's merge and its record, the dependency check refuses (`dependency changed after acceptance: p`). Known bug #87 |

Every invariant holds in every configuration; only the liveness property breaks where the table says.

`ChildReachesDefault` is checked under the fairness in `Spec`: the child's owner keeps running the child's `finish` (`CStart` is strongly fair), the parent's owner keeps running the parent's `finish` when that process exists, and a running process keeps going. A temporal violation is reported as `ChildReachesDefault`.

With `--mutants`, it also breaks one check at a time in a copy of the model and requires each break to be caught by its rule. The mutants stand for: the child merging onto a parent HEAD the parent did not accept, the parent's `finish` ignoring a pending child merge, the child's record not moving the parent's acceptance, and the record saved before the push.

## Read a result

Read every result with the `/statechart` skill, with `model/ParentIntegration.tla` and the configuration in question. For a chart in the reader's words, give it `model/chart.cfg` (both finishes, one kill outside the parent's edge, no session commit) and the view `model/chart.mjs`. The chart has three regions: the child, the parent's acceptance, and the default branch. Refusals, kills and lease steps stay in one state and are not drawn. `/statechart` tells each invariant violation as its shortest path in plain words; for a `ChildReachesDefault` violation read TLC's lasso in `tlc.out`, whose `Back to state` line marks the loop that never reaches the default branch.

A `match: false` line or a mutant with `caught: false` needs a decision:

1. **A real bug.** Replay the path against the real code in a disposable repository built like `tests/test_lifecycle.py` `test_child_then_parent_retirement` (`begin` the parent, `finish` it, `begin --parent p` the child): patch `service._push` to raise before or after the push where the path has `Kill` next to `CPush` or `PPush`, run `git commit` in the parent checkout where it has `SessionCommits`, and call `finish()` of the child or the parent for each `CStart` or `FStart`. If the replay reaches the end state, it is a bug. File it as an Issue with the path and the replay. Do not weaken the rule or the model to make it pass.
2. **A modeling error.** If the replay cannot reach the end state, the model allows a step the code does not have. Find the action that differs from the code and fix it against the Coverage table.
3. **A mutant that is no longer caught.** Either the model lost the check that the mutant removes, or `MUTANTS` in `scripts/check.py` no longer matches the model text, which the script reports as `model text not found once`.

## Change the model with the code

When a change to covered code is intended, update the action that the Coverage table names, so that one action is still one saved state change, one Git effect or one lease decision. Update the table's line numbers and the mutant text if they moved. A configuration that is known to break gets the rule name in `EXPECTED` in `scripts/check.py`, next to the Issue that tracks it. When #85, #86 or #87 is fixed, its configuration must hold: change its `EXPECTED` entry to `None` and model the new check or recovery that the fix adds.

To prove that the check still catches a break after you change it, break the real code on a scratch copy, for example delete `_refuse_pending_merge(state, branch)` in `finish()`. Make the same edit to the matching action (`FEnter` replaces `IF cIntent.open \/ (dIntent` with `IF FALSE \/ (dIntent`) and run the script. It must report a mismatch. Then undo both edits and confirm that the script exits 0 again.

## What the model leaves out

- Bounds: one parent, one child, one session commit, up to two kills; any number of retries.
- Required checks, `--wait-checks` and prepared integrations are the subject of `verify-finish-integration`; here neither edge has required checks. Synchronization with a moved remote, merge conflicts left for the operator, ignored-file collisions and target dirt are not modeled; each refuses before an effect.
- Validation always passes. A failing validation and `update-validation` are the subject of `verify-validation-update`, as are the steps inside the parent's `finish` that accept a new HEAD, which are one step here.
- A grandparent chain, several children of one parent, and other dependencies are not modeled.
- The child's own topic, acceptance and candidate states are assumed done before the child's edge starts.
- A pass covers this model under these bounds, not the whole lifecycle.
