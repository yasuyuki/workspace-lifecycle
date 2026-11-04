---
name: verify-adopt-existing
description: Model-check that adopt-existing gives one branch to at most one task, never leaves a branch binding without its task or pending intent, saves a task only while the checkout matches the snapshot its baseline records, and completes on retry after a kill at any step. Use when a change touches adopt_existing() or _bindings() in src/workspace_lifecycle/adoption.py.
license: MIT
---

# Verify adopt-existing

**Rules.**

- `OneTaskPerBranch`: at most one task adopts the branch.
- `NoOrphanBinding`: the branch binding (`branch.<branch>.workspaceTask`) names a task that exists or whose adoption intent is pending.
- `CheckoutMatchesBeforeSave`: a task is saved only while the checkout still holds the dirty content its baseline records.
- `AdoptionCompletes` (a liveness property): without a writer, retrying the same request always ends with an adopted task.

**Harm when they break.** Two tasks claim one checkout and both finish it; a binding without a task blocks every later adoption of the branch; finish treats content written after the snapshot as preexisting dirt and never owns it; or an interrupted adoption can never complete.

`tests/test_adoption.py` drives fixed orders. `model/AdoptExisting.tla` is a TLA+ model of `adopt_existing()` for two requests, tasks `a` and `b`, that adopt the same branch checkout. TLC checks every order with up to two kills at any step, retries with the same arguments, and one change of the checkout's dirty content.

## When to use

Run the check when a diff touches any covered code below. To see what changed since the model was last confirmed:

```sh
sync=$(git log -1 --format=%H -- skills/verify-adopt-existing)
git diff $sync -- src/workspace_lifecycle/adoption.py | grep -nE "^@@|def adopt_existing|def _bindings|intent|snapshot|_capture|workspaceTask|save_state|pending"
```

Empty output means the model still describes the code. Changes only inside `_capture()` change what a snapshot holds, not the order of steps.

## Coverage

Sync commit: the last commit that changed this skill folder, as `sync` above finds it. Each commit that changes this folder states that the model describes the covered code at that commit. So commit a change here only after you have read the covered code and confirmed that every action still matches it.

| Code at the sync commit (`adoption.py`) | Model |
| --- | --- |
| leases and `locked_state` around the body (lines 221-224) | `Start` takes `lock`; every refusal and `Kill` drop it |
| refusals of a pending intent with other arguments, another pending task on the branch or worktree, `_bindings(..., bool(intent))`, the first `_capture`, the intent save (225-259) | `Enter` |
| `snapshot != intent['snapshot']` and the second `_capture` (260-261) | `Compare` |
| `_bindings(..., True)` and `git config branch.<branch>.workspaceTask` (262-263) | `Bind` |
| the third `_capture` (264-265) | `Recheck` |
| the task saved with `baseline_dirty`, the intent popped (267-281) | `Save` |
| `_bindings()` (173-183) | the binding condition in `Enter` |
| process killed; the OS drops its leases and the state lock | `Kill` |

## Run

Requirements: Python 3.14, a Java runtime, and `tla2tools.jar` from TLA+ v1.7.4 (`https://github.com/tlaplus/tlaplus/releases/download/v1.7.4/tla2tools.jar`, SHA-256 `936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88`). The script refuses any other TLC version, because a different TLC can report a different path.

From the repository root:

```sh
python -B skills/verify-adopt-existing/scripts/check.py --mutants --java JAVA --tla2tools TLA2TOOLS_JAR
```

`--java` and `--tla2tools` fall back to the `JAVA` and `TLA2TOOLS` environment variables. It takes about 5 seconds. Exit 0 means every result matched; 1 means a mismatch; 2 means TLC could not run.

Each output line is JSON. The script checks three configurations in `model/`:

| Configuration | Meaning | Expected |
| --- | --- | --- |
| `no-writer` | kills and retries only; also checks `AdoptionCompletes` | every rule holds |
| `contract-writer` | the checkout changes once while no adoption runs, as the README requires of native writers | every rule holds |
| `stray-writer` | the checkout changes once at any time | `CheckoutMatchesBeforeSave` breaks: a change after the last capture and before the save is not seen. The README requires native writers to stay quiescent during adoption, so this is the stated boundary, not a bug |

`AdoptionCompletes` is checked under the fairness in `Spec`: each owner retries its own request (`Start` is strongly fair per task) and a running request keeps going. A temporal violation is reported as `AdoptionCompletes`.

With `--mutants`, it also breaks one check at a time in a copy of the model and requires each break to be caught by its rule. The mutants stand for: `_bindings` ignoring an existing binding, a retry that does not pass `bool(intent)` to `_bindings`, and a save that drops the intent without saving the task.

The snapshot comparison in `Compare` and the capture after binding in `Recheck` back each other up against a change between two runs, and the refusal of another pending task on the branch is backed up by `_bindings` once that task has bound. So no mutant stands for any one of them.

## Read a result

Read every result with the `/statechart` skill, with `model/AdoptExisting.tla` and the configuration in question. For a chart in the reader's words, give it `model/chart.cfg` (one change by a writer within the contract, one kill) and the view `model/chart.mjs`; the chart follows request `a` and names request `b` only once it has been adopted. `/statechart` tells each invariant violation as its shortest path in plain words; for an `AdoptionCompletes` violation read TLC's lasso in `tlc.out`, whose `Back to state` line marks the loop that never adopts.

A `match: false` line or a mutant with `caught: false` needs a decision:

1. **A real bug.** Replay the path against the real `adopt_existing()` in a disposable clone, as the adoption tests do: patch the function an action names (`_capture`, `git`, `save_state`) to raise where the path has `Kill`, change a dirty file where the path has `WriterChanges`, and call `adopt_existing()` again with the same arguments for each retry. If the replay reaches the end state, it is a bug. File it as an Issue with the path and the replay. Do not weaken the rule or the model to make it pass.
2. **A modeling error.** If the replay cannot reach the end state, the model allows a step the code does not have. Find the action that differs from the code and fix it against the Coverage table.
3. **A mutant that is no longer caught.** Either the model lost the check that the mutant removes, or `MUTANTS` in `scripts/check.py` no longer matches the model text, which the script reports as `model text not found once`.

## Change the model with the code

When a change to covered code is intended, update the action that the Coverage table names, so that one action is still one saved state change, one Git effect or one capture. Update the table's line numbers and the mutant text if they moved. A configuration that is known to break gets the rule name in `EXPECTED` in `scripts/check.py`, next to the Issue that tracks it or, as for `stray-writer`, the documented boundary.

To prove that the check still catches a break after you change it, break the real code on a scratch copy, for example call `_bindings(repo, task, branch, False)` in place of `_bindings(repo, task, branch, bool(intent))`. Make the same edit to the matching condition (`Enter` replaces `~(intent[t] # None /\ binding = t)` with `~FALSE`) and run the script. It must report a mismatch. Then undo both edits and confirm that the script exits 0 again.

## What the model leaves out

- Bounds: two requests for one branch, up to two kills, one change of the checkout.
- The dirty content is one value. Preserved data, protected nested checkouts, parents, dependencies, holds and required checks are not modeled; each is checked once inside the locked body and refuses without an effect.
- `begin` creating a task on the same branch is not modeled.
- `_capture()` itself (mounts, links, regular files, digests) is assumed correct.
- A pass covers this model under these bounds, not the whole lifecycle.
