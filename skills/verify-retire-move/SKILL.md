---
name: verify-retire-move
description: Model-check that retire never moves a task checkout while a run uses it, never puts data the task does not own into the retired manifest that reclaim deletes, keeps Git's admin entry pointing at the checkout, saves the retired receipt only after the checkout left Git's active worktrees, and completes on retry after a kill. Use when a change touches retire(), _retirement_contents(), _retirement_transaction(), retire_pending() or before_run() in src/workspace_lifecycle/service.py, or guard() in leases.py.
license: MIT
---

# Verify retire move

**Rules.**

- `NoMoveInUse`: a checkout is never moved while a run of its task is active in it.
- `NoUnownedDataRetired`: the retired receipt's content manifest holds only tracked content.
- `AdminFindsCheckout`: Git's admin entry (`.git/worktrees/<name>/gitdir`) names the path where the checkout is, except inside `git worktree move`.
- `ReceiptOnlyWhenRetired`: the retired receipt is saved only after the checkout is at its recovery path, its admin entry is archived, its branch is at the accepted commit and its branch binding is unset.
- `RetireCompletes` (a liveness property): once retirement is requested, `retire --pending` always completes it.

**Harm when they break.** A command keeps running in a directory that moved under it; `reclaim` deletes untracked or ignored files the user never handed over, because they are in the manifest; Git no longer finds the retired checkout and every retry is refused; a retired task leaves its branch bound or its checkout listed as active; or a retirement request blocks the task forever.

`tests/test_lifecycle.py` drives fixed orders. `model/RetireMove.tla` is a TLA+ model of `retire()` for one accepted, integrated task. TLC checks every order of: the task's own session running commands through `run`, writing untracked data or committing while its run is active, asking for retirement from inside (`finish --users-released`), and `retire` from outside with up to two kills at any step and retries.

## When to use

Run the check when a diff touches any covered code below. To see what changed since the model was last confirmed:

```sh
sync=$(git log -1 --format=%H -- skills/verify-retire-move)
git diff $sync -- src/workspace_lifecycle/service.py src/workspace_lifecycle/leases.py | grep -nE "^@@|def retire|def _retirement|def before_run|def guard|phase|persist|worktree', 'move'|rename\(archive|workspaceTask|_retirement_record|request_only|item.get\('retire'\)"
```

Empty output means the model still describes the code. Changes only inside `_retirement_identity()`, `_retirement_pointer()` or `_tree_manifest()` change how a check looks, not the order of steps.

## Coverage

Sync commit: the last commit that changed this skill folder, as `sync` above finds it. Each commit that changes this folder states that the model describes the covered code at that commit. So commit a change here only after you have read the covered code and confirmed that every action still matches it.

| Code at the sync commit (`service.py` unless named) | Model |
| --- | --- |
| `retire(request_only=True)` under `_lease(repo, task, allow_use=True)` (lines 1996-2083), called by `finish --users-released` inside the worktree | `AskInside` |
| `_lease(repo, task)` without `allow_use` (1996); `leases.py` `guard()` refusal `if uses:` (164-177) | `Start` requires no active run and takes `lock` |
| accepted result, integration and `head(repo, 'refs/heads/' + branch) != expected` (2001-2026); the new request with phase `requested` and `persist()` (2036-2073) | `Enter` |
| payload still at the original path: registered worktree, branch, HEAD, admin and pointer checks; `_retirement_contents()` in phase `requested`; phase `moving` (2100-2114); `_retirement_contents()` (1849-1884) | `Check` |
| `git worktree move` (2115): Git renames the directory, then writes the new path into the admin `gitdir` | `Rename`, `Pointer` |
| recovery registered at the accepted HEAD, phases `recovery-held` and `archiving-admin` (2116-2129) | `Held` |
| `admin.rename(archive)` (2130) | `Archive` |
| archived pointer, neither path registered (`_retirement_record`), branch at the accepted commit, binding unset (2133-2145) | `Unbind` |
| manifests, retired receipt, task and intent removed, one `persist()` (2146-2171) | `Receipt` |
| `before_run()` refusal of a task with a retirement request (2441-2460), `check_run_identity()`; the shared use lock in `leases.py` `run()` (421-448) | `RunStarts` |
| process killed; the OS drops its lease | `Kill` |
| `begin()` of another task whose `--worktree` is the freed original path (199-286) | `OtherBegins` |

## Run

Requirements: Python 3.14, a Java runtime, and `tla2tools.jar` from TLA+ v1.7.4 (`https://github.com/tlaplus/tlaplus/releases/download/v1.7.4/tla2tools.jar`, SHA-256 `936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88`). The script refuses any other TLC version, because a different TLC can report a different path.

From the repository root:

```sh
python -B skills/verify-retire-move/scripts/check.py --mutants --java JAVA --tla2tools TLA2TOOLS_JAR
```

`--java` and `--tla2tools` fall back to the `JAVA` and `TLA2TOOLS` environment variables. It takes about 6 seconds. Exit 0 means every result matched; 1 means a mismatch; 2 means TLC could not run.

Each output line is JSON. The script checks five configurations in `model/`:

| Configuration | Meaning | Expected |
| --- | --- | --- |
| `no-writer` | runs, requests, kills and retries; also checks `RetireCompletes` | every rule holds |
| `contract-writer` | the session writes untracked data or commits, up to twice, only while its run is active | every rule holds |
| `stray-writer` | the session writes at any time | `NoUnownedDataRetired` breaks: a write after the contents check reaches the manifest. `--users-released` states that no user writes the checkout any more, so this is the stated boundary, not a bug |
| `git-move-kill` | a kill can fall between Git's rename and its `gitdir` write | `AdminFindsCheckout` breaks; every retry is refused with `recovery Git identity changed`. Known bug #82 |
| `path-reuse` | another task can `begin` at the original path once it is free | `RetireCompletes` breaks; every retry is refused with `target remains registered as an active Git worktree`. Known bug #83 |

`RetireCompletes` is checked under the fairness in `Spec`: the owner keeps retrying `retire --pending` (`Start` is strongly fair), a running retire keeps going, and a run ends. A temporal violation is reported as `RetireCompletes`.

With `--mutants`, it also breaks one check at a time in a copy of the model and requires each break to be caught by its rule. The mutants stand for: `guard()` admitting a retire while a run is active, `before_run()` admitting a run on a task with a retirement request, retire skipping `_retirement_contents()`, and a receipt saved without unsetting the binding.

The accepted-HEAD checks in `Enter`, `Check`, `Held` and `Unbind` back each other up: a commit after the request is refused by whichever runs next. So no mutant stands for any one of them. `_retirement_contents()` runs only in phase `requested`; a retry in phase `moving` relies on `before_run()` refusing every new run, which the second mutant shows.

## Read a result

Read every result with the `/statechart` skill, with `model/RetireMove.tla` and the configuration in question. For a chart in the reader's words, give it `model/chart.cfg` (one write by the session within the contract, one kill) and the view `model/chart.mjs`. The chart has three regions: the retirement, the checkout's content, and the run. Refusals and kills stay in one state and are not drawn. `/statechart` tells each invariant violation as its shortest path in plain words; for a `RetireCompletes` violation read TLC's lasso in `tlc.out`, whose `Back to state` line marks the loop that never retires.

A `match: false` line or a mutant with `caught: false` needs a decision:

1. **A real bug.** Replay the path against the real `retire()` in a disposable repository built like `tests/test_lifecycle.py` `LifecycleTest` (`begin`, `finish`, then `retire(..., users_released=True)`): patch `service.git` to raise after or inside `worktree move` where the path has `Kill`, write a file in the checkout where the path has `SessionWrites`, call `begin()` for another task where it has `OtherBegins`, and call `retire_pending()` for each retry. If the replay reaches the end state, it is a bug. File it as an Issue with the path and the replay. Do not weaken the rule or the model to make it pass.
2. **A modeling error.** If the replay cannot reach the end state, the model allows a step the code does not have. Find the action that differs from the code and fix it against the Coverage table.
3. **A mutant that is no longer caught.** Either the model lost the check that the mutant removes, or `MUTANTS` in `scripts/check.py` no longer matches the model text, which the script reports as `model text not found once`.

## Change the model with the code

When a change to covered code is intended, update the action that the Coverage table names, so that one action is still one saved phase, one Git or filesystem effect, or one lease decision. Update the table's line numbers and the mutant text if they moved. A configuration that is known to break gets the rule name in `EXPECTED` in `scripts/check.py`, next to the Issue that tracks it. When #82 is fixed, `git-move-kill` must hold: change its `EXPECTED` entry to `None` and model the repair the fix adds. When #83 is fixed, do the same for `path-reuse`.

To prove that the check still catches a break after you change it, break the real code on a scratch copy, for example delete the line `_retirement_contents(workspace)` in `retire()`. Make the same edit to the matching action (`Check` replaces `request = "requested" /\ content # "clean"` with `FALSE`) and run the script. It must report a mismatch. Then undo both edits and confirm that the script exits 0 again.

## What the model leaves out

- Bounds: one task, one session, up to two writes and two kills; any number of runs and retries.
- Content is one value, clean or dirty. Links, mounts, submodules, ignored files and nested repositories are each one refusal inside `_retirement_contents()` and are assumed correct.
- Owner callbacks (`owner_receipts`, `completion_release`), holds, dependencies and children, preservation evidence and the `reclaim()` that follows are not modeled. `reclaim()` is the subject of `verify-reclaim-removal`; run lease exclusion itself is the subject of `verify-run-lease-exclusion`, and here a run start is one step.
- Identity checks (`_retirement_identity`, `_retirement_matches`, `_retirement_pointer`) and `_tree_manifest()` are assumed correct; a replaced directory is not modeled.
- Writers other than the session (native Git, editors, another user) are outside the `--users-released` contract and appear only in `stray-writer`.
- A pass covers this model under these bounds, not the whole lifecycle.
