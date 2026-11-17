---
name: verify-abandon-retirement
description: Model-check that `retire --abandon` never leads to a push or integration, refuses a held task, retires only the recorded clean branch HEAD, and that reclaim removes an abandoned checkout only while its branch keeps that commit. Use when a change touches retire(), reclaim() or the abandonment refusal in finish() in src/workspace_lifecycle/service.py, or the `--abandon` option in cli.py.
license: MIT
---

# Verify abandon retirement

**Rules.**

- `NothingPushedAfterAbandonment`: once abandonment is requested, nothing is pushed for the task.
- `AbandonedNeverIntegrated`: an abandoned task's commit never reaches the remote default.
- `NoAbandonmentWhileHeld`: a held task is not abandoned until `release-hold` records the decision.
- `RetiresRecordedCleanCommit`: retirement moves exactly the branch HEAD the abandonment recorded, with no unsaved data.
- `ReclaimLosesNothing`: reclaim removes the payload only while a kept branch still holds its commit.

**Harm when they break.** A fixture commit that must not ship reaches `main`; a hold placed for a person's decision is bypassed; work committed or written after the decision is moved away as if it were the abandoned state; or reclaim deletes the only copy of a commit.

`model/AbandonRetirement.tla` is a TLA+ model of one task. Commits form one line, `0..MaxCommit`. The user can commit, edit, place or release a hold, start a finish that records an integration, and delete or reset the branch after retirement. A recorded integration can still be pushed or withdrawn at any later step, as `_settle_stale_integration()` does at finish entry. TLC checks every order of these steps.

## When to use

Run the check when a diff touches any covered code below. To see what changed since the model was last confirmed:

```sh
sync=$(git log -1 --format=%H -- skills/verify-abandon-retirement)
git diff $sync -- src/workspace_lifecycle/service.py | grep -nE "^@@|abandon|def retire|def reclaim|intents|hold|refs/heads|_retirement_contents"
git diff $sync -- src/workspace_lifecycle/cli.py | grep -n "abandon"
```

Empty output means the model still describes the code.

## Coverage

Sync commit: the last commit that changed this skill folder, as `sync` above finds it. Each commit that changes this folder states that the model describes the covered code at that commit. So commit a change here only after you have read the covered code and confirmed that every action still matches it.

| Code at the sync commit | Model |
| --- | --- |
| `service.py` `retire()` with `abandon`: hold refusal, pending finish (`intents`, `completion_release`), integrated and producer refusals, `abandoned = {'reason', 'commit': branch HEAD}` saved in the request (lines 2180-2195, 2265-2266) | `AbandonRequested` |
| `retire()` replay: hold refusal, `retire expected accepted HEAD; branch changed`, `_retirement_contents()`, `git worktree move` (2183-2186, 2220-2221, 2310-2312) | `Retires` |
| `finish()` refusal `task was abandoned` (1099-1100) | `FinishStarts` is enabled only while `phase = "active"` |
| `finish()` merge intent saved, pushed or withdrawn, also by `_settle_stale_integration()` | `FinishStarts`, `FinishPushes`, `FinishWithdraws` |
| `hold()`, `release_hold()` | `Hold` |
| `reclaim()` `abandoned commit is no longer kept by its branch` (2495-2498), then payload removal | `Reclaims` |
| user Git commands: commit in the checkout, `git branch -D` or `-f` after retirement | `UserCommits`, `UserEdits`, `UserMovesBranch` |

## Run

Requirements: Python 3.14, a Java runtime, and `tla2tools.jar` from TLA+ v1.7.4 (`https://github.com/tlaplus/tlaplus/releases/download/v1.7.4/tla2tools.jar`, SHA-256 `936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88`). The script refuses any other TLC version.

From the repository root:

```sh
python -B skills/verify-abandon-retirement/scripts/check.py --mutants --java JAVA --tla2tools TLA2TOOLS_JAR
```

`--java` and `--tla2tools` fall back to the `JAVA` and `TLA2TOOLS` environment variables. It takes about 6 seconds. Exit 0 means every result matched; 1 means a mismatch; 2 means TLC could not run.

Each output line is JSON. The configuration `checks` (`MaxCommit = 2`) must hold every rule. With `--mutants`, the script breaks one check at a time in a copy of the model and requires each break to be caught by its rule: abandonment that ignores a pending finish, finish that runs after abandonment, abandonment that ignores a hold, retirement without the branch HEAD check, retirement without the contents check, and reclaim without the branch check. `tests/test_abandonment.py` replays each of these paths against the real code.

## Read a result

Read every result with the `/statechart` skill, with `model/AbandonRetirement.tla`. For a chart in the reader's words, give it `model/chart.cfg` (`MaxCommit = 1`) and the view `model/chart.mjs`; the view hides edits of unsaved data, which stay in one chart state. `/statechart` tells each violation as its shortest path in plain words.

A `match: false` line or a mutant with `caught: false` needs a decision:

1. **A real bug.** Replay the path against the real code with the fixtures of `tests/test_abandonment.py`, one call or Git command per step. If the replay pushes, integrates, moves a changed checkout or reclaims what the rule forbids, it is a bug. File it as an Issue with the path and the replay. Do not weaken the rule or the model to make it pass.
2. **A modeling error.** If the replay cannot reach the end state, the model allows a step the code does not have. Find the action that differs from the code and fix it against the Coverage table.
3. **A mutant that is no longer caught.** Either the model lost the guard the mutant removes, or `MUTANTS` in `scripts/check.py` no longer matches the model text, which the script reports as `model text not found once`.

## Change the model with the code

When a change to covered code is intended, update the action the Coverage table names, and the table's line numbers and the mutant text if they moved. To prove that the check still catches a break, break the real code on a scratch copy, for example replace `if kept is None or git(repo, 'merge-base', ...)` in `reclaim()` with `if False:`, and make the same edit to the model (`Reclaims` drops `/\ Contains(branch, recorded)`). Both `tests/test_abandonment.py` and the script must report it. Then undo both edits and confirm that the script exits 0 again.

## What the model leaves out

- Bounds: one task, commits on one line up to `MaxCommit`, any number of holds, edits and finishes.
- `Retires` is one step. The window between the contents check and `git worktree move`, and crashes inside it, belong to `verify-retire-move`. Member deletion during reclaim belongs to `verify-reclaim-removal`.
- `Reclaims` checks the branch and removes in one step. A branch deleted by hand while reclaim runs is the same window that the remote default check of an accepted task has.
- Retirement of accepted, integrated tasks, producer generations, parent and dependency tasks, and foreign preserved data are refused or unchanged by abandonment and are not modeled.
- A pass covers this model under these bounds, not the whole lifecycle.
