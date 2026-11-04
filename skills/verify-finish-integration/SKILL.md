---
name: verify-finish-integration
description: Model-check that finish and its integration into the default branch push only work whose validations and required checks succeeded, never leave an unpushed integration that no intent records, and can resume after a kill at any step. Use when a change touches finish(), _integrate(), _await_checks(), _withdraw() or _withdraw_prepared() in src/workspace_lifecycle/service.py, or verify() in checks.py.
license: MIT
---

# Verify finish integration

**Rules.**

- `OnlyCheckedWorkReachesDefault`: a task commit reaches the remote default branch only after the validation in the default checkout passed on it, and either its required checks succeeded on that exact commit (gated task) or the validation in the task checkout passed (task without required checks).
- `NoForgottenPreparedMerge`: the default branch checkout never holds an unpushed task commit that no pending merge intent records.
- `IntegratedIsPushed`: a recorded integration is on the remote default branch.
- `FixCanFinish`: an integration prepared ahead of its checks never outlives the task HEAD it was prepared for, so finish can start again after a fix commit.

**Harm when they break.** Unchecked work lands on `main` and turns it red for everyone; a forgotten local merge is pushed unchecked by the next task that integrates; the state claims an integration that `main` lacks; or an agent's fix can never be finished.

Tests drive fixed orders of these steps. `model/FinishIntegration.tla` is a TLA+ model of one task finishing into the remote default branch. TLC checks every order of the owner's finish runs (plain, `--wait-checks`, `--revise-plan-head`), a fix commit, CI results and reruns, another task integrating into the same default checkout, and a kill of the finish process at any step.

## When to use

Run the check when a diff touches any covered code below. To see what changed since the model was last confirmed:

```sh
sync=$(git log -1 --format=%H -- skills/verify-finish-integration)
git diff $sync -- src/workspace_lifecycle/service.py | grep -nE "^@@|intent|_integrate|_withdraw|_await_checks|gate|proof|deferred|_push|_validate|_refuse_pending_merge"
git diff $sync -- src/workspace_lifecycle/checks.py | grep -nE "^@@|def verify|unmet"
```

Empty output from both means the model still describes the code. Hunks only in `retire()`, `reclaim()`, `update_*()` or `adopt` code are outside this model.

## Coverage

Sync commit: the last commit that changed this skill folder, as `sync` above finds it. Each commit that changes this folder states that the model describes the covered code at that commit. So commit a change here only after you have read the covered code and confirmed that every action still matches it.

| Code at the sync commit (`service.py` unless named) | Model |
| --- | --- |
| `finish()` intent and resume decisions: merge intent vs saved record, finish intent, `--revise-plan-head`, `_acceptance_holds` (lines 988-1072) | `FinEnter` |
| `finish()` candidate skips `_validate` (1147-1150) | `FinValidate` |
| `finish()` `_push` of the topic (1165) | `FinPush` |
| `finish()` saves candidate or acceptance, pops the intent (1166-1186) | `FinRecord` |
| `_integrate()` state read and `checks.verify` outside the state lock; `withdraw()` before a refusal (1603-1631); `checks.py` `verify()` (192-199) | `IntVerify` |
| `_integrate()` identity and proof checks, `_refuse_pending_merge`, target synchronization, merge intent save (1632-1721) | `IntLock` |
| `_integrate()` fast-forward or merge into the default checkout (1722-1779) | `IntMerge` |
| `_integrate()` validation in the target; `_withdraw` on failure (1795-1808) | `IntValidate` |
| `_integrate()` return `prepared` without check evidence, or record the evidence (1812-1819) | `IntGate` |
| `_integrate()` candidate becomes acceptance (1820-1824) | `IntAccept` |
| `_integrate()` `_push` of the default checkout (1825) | `IntPush` |
| `_integrate()` save `integrated`, pop the intent (1826-1837) | `IntRecord` |
| `finish()` wait, reuse or `_withdraw_prepared` (1187-1202); `_await_checks()` (1449-1469) | `Await` |
| `_withdraw()` remote check and `git checkout -B` (1558-1584) | `Withdraw` |
| `_withdraw()` pops the intent (1585) | `WithdrawPop` |
| another task's `_integrate()` into the same default checkout | `OtherIntegrates` |
| process killed; leases and locks drop, the state file stays | `Kill` |

## Run

Requirements: Python 3.14, a Java runtime, and `tla2tools.jar` from TLA+ v1.7.4 (`https://github.com/tlaplus/tlaplus/releases/download/v1.7.4/tla2tools.jar`, SHA-256 `936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88`). The script refuses any other TLC version, because a different TLC can report a different path.

From the repository root:

```sh
python -B skills/verify-finish-integration/scripts/check.py --mutants --java JAVA --tla2tools TLA2TOOLS_JAR
```

`--java` and `--tla2tools` fall back to the `JAVA` and `TLA2TOOLS` environment variables. It takes about 15 seconds. Exit 0 means every result matched; 1 means a mismatch; 2 means TLC could not run.

Each output line is JSON. The script checks four configurations in `model/`:

| Configuration | Meaning | Expected |
| --- | --- | --- |
| `gated` | required checks, kills, another task | the first three rules hold |
| `ungated` | no required checks, kills, another task | all four rules hold |
| `gated-fix-no-kill` | required checks, no kills | `FixCanFinish` holds |
| `gated-fix-after-kill` | required checks, kills | `FixCanFinish` breaks: #80 |

With `--mutants`, it also breaks one check at a time in a copy of the model and requires each break to be caught by its rule. The mutants stand for: `checks.verify` accepting unsucceeded checks, `_integrate` pushing a prepared integration without check evidence, `_withdraw` dropping the intent without `git checkout -B`, another task skipping `_refuse_pending_merge`, finish skipping the task checkout validation without required checks, `_integrate` skipping the validation in the target, and `integrated` saved before the push.

The proof check in `_integrate` ("required check evidence does not match") and the refusal after `checks.verify` in the same function back each other up: removing either one alone breaks no rule in the model, because the task lease keeps the accepted commit from changing between them. So no mutant stands for either one alone.

## Read a result

Read every result with the `/statechart` skill, with `model/FinishIntegration.tla` and the configuration in question. For a chart in the reader's words, give it `model/FinishChart.tla` with `model/chart.cfg` (no kills) or `model/chart-kill.cfg` (kills, which shows the #80 path), and the view `model/chart.mjs`. `/statechart` tells each violation as its shortest path in plain words.

A `match: false` line or a mutant with `caught: false` needs a decision:

1. **A real bug.** Replay the path against the real code, one step per action, with the fixtures in `tests/test_required_checks.py`: `self.start()` and `self.refused()` give a pushed candidate, `self.github.add()` sets a check result, and a call to `service._integrate(..., prepare=True)` without the wait that follows stands for a finish killed after `IntLock` or `IntGate`. Put the replay in a temporary `tests/test_*.py` and run it with `python -B tests/run.py <module>.<Class>.<test>`; delete it afterwards. If the replay reaches the end state, it is a bug. File it as an Issue with the path and the replay. Do not weaken the rule or the model to make it pass.
2. **A modeling error.** If the replay cannot reach the end state, the model allows a step the code does not have. Find the action that differs from the code and fix it against the Coverage table.
3. **A mutant that is no longer caught.** Either the model lost the check that the mutant removes, or `MUTANTS` in `scripts/check.py` no longer matches the model text, which the script reports as `model text not found once`.

## Change the model with the code

When a change to covered code is intended, update the action that the Coverage table names, so that one action is still one saved state change, one Git effect or one remote event. Update the table's line numbers and the mutant text if they moved. A configuration that is known to break gets the rule name in `EXPECTED` in `scripts/check.py`, next to the Issue that tracks it. When #80 is fixed, `gated-fix-after-kill` must hold: change its `EXPECTED` entry to `None`.

To prove that the check still catches a break after you change it, break the real code on a scratch copy, for example delete `_refuse_pending_merge(state, target_branch)` in `_integrate()`. Make the same edit to the matching action (`OtherIntegrates` drops `/\ intent.kind # "merge"`) and run the script. It must report a mismatch. Then undo both edits and confirm that the script exits 0 again.

## What the model leaves out

- Bounds: one task, one fix commit, one other task, any number of finish runs, CI reruns and kills.
- The integration target is the remote default branch. A parent task as target (`integration.kind == 'task'`), dependencies, holds and owner receipts are not modeled.
- Commits are names and checkouts are sets of names. Merge conflicts, `--no-ff` merges left for the operator, ignored-file collisions, target dirt, synchronization receipts, a moved remote topic, push rejections and `finish --revise-plan-evidence` are not modeled; each refuses and leaves the saved state as it was.
- A validation result depends only on the source commit. Reuse of the prepared validation is not modeled; the model validates again.
- The task's own commit by finish (plan `commit` entries), plan preservation actions, `_acceptance_holds` beyond the HEAD, record and topic tip, and the checks after integration (`_await_integration_checks`) are not modeled.
- `checks.verify`, `checks.wait` and GitHub's answers are assumed correct: a check result is one of pending, success and failure, and success is final.
- A pass covers this model under these bounds, not the whole lifecycle.
