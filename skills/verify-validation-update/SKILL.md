---
name: verify-validation-update
description: Model-check that update-validation never releases a finish intent whose restore or commit already happened, and that a finish stopped by a failing validator or a kill always reaches its acceptance through update-validation and the same finish. Use when a change touches update_validation(), _effectless_finish(), _recorded_finish(), _resolve_files() restore actions, or the intent, commit and validation steps of finish() in src/workspace_lifecycle/service.py.
license: MIT
---

# Verify validation update

**Rules.**

- `NoUnrecordedEffect`: until the acceptance, a file the finish removed and a commit the finish made stay recorded by its intent.
- `FinishCompletes` (a liveness property): if the owner keeps running finish, and runs `update-validation` whenever it would succeed, the task is accepted.

**Harm when they break.** `update-validation` drops a finish that already removed a generated file or made its commit, so the next finish no longer knows about that effect; or a task whose validator was wrong can never be finished without hand repair of the state.

`update-preflight` and `update-required-checks` refuse while any intent is pending, so they cannot interleave with a finish; this model leaves them out. `tests/test_validation_update.py` drives fixed orders. `model/ValidationUpdate.tla` is a TLA+ model of the task checkout part of `finish()`, one plan restore action, the commit, validation with a failing validator, and `update_validation()`. TLC checks every order with up to two kills of the finish process at any step.

## When to use

Run the check when a diff touches any covered code below. To see what changed since the model was last confirmed:

```sh
sync=$(git log -1 --format=%H -- skills/verify-validation-update)
git diff $sync -- src/workspace_lifecycle/service.py | grep -nE "^@@|def update_validation|_effectless_finish|_recorded_finish|_FINISH_INTENT_FIELDS|commit_tree|committed|initial_head|'restoring'|'resolved'|_index_paths"
```

Empty output means the model still describes the code.

## Coverage

Sync commit: the last commit that changed this skill folder, as `sync` above finds it. Each commit that changes this folder states that the model describes the covered code at that commit. So commit a change here only after you have read the covered code and confirmed that every action still matches it.

| Code at the sync commit (`service.py`) | Model |
| --- | --- |
| `finish()` opens the intent, or adopts a commit that matches `commit_tree` (lines 1070-1087) | `Enter` |
| `_resolve_files()` restore: phase `restoring` saved, file restored or removed, phase `resolved` saved (826-849) | `RestoreIntent`, `RestoreEffect`, `RestoreDone` |
| `finish()` `git add` of the commit paths (1135-1136) | `Stage` |
| `finish()` `commit_tree` saved (1140) | `Tree` |
| `finish()` `git commit` (1143) | `Commit` |
| `finish()` `committed` saved (1148) | `Committed` |
| `finish()` `_validate`; a failure keeps the intent (1152) | `Validate` |
| `finish()` push and acceptance, intent dropped (1167-1188) | `Record` |
| `update_validation()` (354-384), `_effectless_finish()` (391-398), `_recorded_finish()` (401-419), the index check (377-378) | `UpdateValidation`, `Effectless`, `Recorded` |
| process killed; the state file stays | `Kill` |

## Run

Requirements: Python 3.14, a Java runtime, and `tla2tools.jar` from TLA+ v1.7.4 (`https://github.com/tlaplus/tlaplus/releases/download/v1.7.4/tla2tools.jar`, SHA-256 `936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88`). The script refuses any other TLC version, because a different TLC can report a different path.

From the repository root:

```sh
python -B skills/verify-validation-update/scripts/check.py --mutants --java JAVA --tla2tools TLA2TOOLS_JAR
```

`--java` and `--tla2tools` fall back to the `JAVA` and `TLA2TOOLS` environment variables. It takes about 6 seconds. Exit 0 means every result matched; 1 means a mismatch; 2 means TLC could not run.

Each output line is JSON. The script checks two configurations in `model/`, a plan with a restore entry (`with-restore`) and one with only commit entries (`commit-only`); both rules hold in each. `FinishCompletes` is checked under the fairness in `Spec`: the finish steps are weakly fair and `UpdateValidation`, which is enabled only when it would succeed, is strongly fair. A temporal violation is reported as `FinishCompletes`.

With `--mutants`, it also breaks one check at a time in a copy of the model and requires each break to be caught by its rule. The mutants stand for: `_effectless_finish` ignoring recorded actions, the restore phase saved after the file is removed, a failed validation dropping the intent, finish not adopting its own commit that matches `commit_tree`, and `_recorded_finish` refusing a recorded commit.

`_effectless_finish`'s `commit_tree` test, `_recorded_finish`'s `commit_tree` test without `committed`, and the index check each back up the others: a kill between `git add` and `git commit` leaves the index staged or HEAD moved, which the other checks see. So no mutant stands for any one of them.

## Read a result

Read every result with the `/statechart` skill, with `model/ValidationUpdate.tla` and the configuration in question. For a chart in the reader's words, give it `model/chart.cfg` (commit only, one kill, no liveness property) and the view `model/chart.mjs`; the view draws every running step as one state. `/statechart` tells each invariant violation as its shortest path in plain words; for a `FinishCompletes` violation read TLC's lasso in `tlc.out`, whose `Back to state` line marks the loop that never reaches the acceptance.

A `match: false` line or a mutant with `caught: false` needs a decision:

1. **A real bug.** Replay the path against the real code with the fixtures of `tests/test_validation_update.py`: patch the function an action names to raise where the path has `Kill`, call `finish()` or `update_validation()` again for each later step, and compare the saved state with the rule. If the replay reaches the end state, it is a bug. File it as an Issue with the path and the replay. Do not weaken the rule or the model to make it pass.
2. **A modeling error.** If the replay cannot reach the end state, the model allows a step the code does not have. Find the action that differs from the code and fix it against the Coverage table.
3. **A mutant that is no longer caught.** Either the model lost the check that the mutant removes, or `MUTANTS` in `scripts/check.py` no longer matches the model text, which the script reports as `model text not found once`.

## Change the model with the code

When a change to covered code is intended, update the action that the Coverage table names, so that one action is still one saved state change or one Git or filesystem effect. Update the table's line numbers and the mutant text if they moved. A configuration that is known to break gets the rule name in `EXPECTED` in `scripts/check.py`, next to the Issue that tracks it.

To prove that the check still catches a break after you change it, break the real code on a scratch copy, for example drop `and intent.get('actions') == {}` from `_effectless_finish()`. Make the same edit to the matching definition (`Effectless` drops `/\ intent.phase = None`) and run the script. It must report a mismatch. Then undo both edits and confirm that the script exits 0 again.

## What the model leaves out

- Bounds: one restore entry, one commit, one validator correction, up to two kills.
- Archive entries, `--revise-plan-evidence`, `--revise-plan-head` (`head_revision`), synchronization, plan exceptions and dirt checks are not modeled.
- The owner supplies the current HEAD to `update-validation`; a wrong `--expected-head` is refused and changes nothing.
- The integration after the acceptance is the subject of `verify-finish-integration`.
- A pass covers this model under these bounds, not the whole lifecycle.
