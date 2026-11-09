---
name: verify-validation-update
description: Model-check that update-validation and update-preflight never release a finish intent whose restore or commit already happened, and that a finish stopped by a failing validator, a failing preflight or a kill always reaches its acceptance through those corrections and the same finish. Use when a change touches update_validation(), update_preflight(), _correctable_finish(), _effectless_finish(), _recorded_finish(), _resolve_files() restore actions, or the intent, commit, validation and push steps of finish() in src/workspace_lifecycle/service.py.
license: MIT
---

# Verify validation update

**Rules.**

- `NoUnrecordedEffect`: until the acceptance, a file the finish removed and a commit the finish made stay recorded by its intent.
- `FinishCompletes` (a liveness property): if the owner keeps running finish, and runs `update-validation` and `update-preflight` whenever they would succeed, the task is accepted.

**Harm when they break.** A correction drops a finish that already removed a generated file or made its commit, so the next finish no longer knows about that effect; or a task whose validator or preflight was wrong can never be finished without hand repair of the state (#31, #69).

`update-required-checks` refuses while any intent is pending, so it cannot interleave with a finish; this model leaves it out. `tests/test_validation_update.py` and `tests/test_preflight_update.py` drive fixed orders. `model/ValidationUpdate.tla` is a TLA+ model of the task checkout part of `finish()`, one plan restore action, the commit, validation with a failing validator, the push with a failing preflight, `update_validation()` and `update_preflight()`. Each run starts with the validator and the preflight each right or wrong. TLC checks every order with up to two kills of the finish process at any step.

## When to use

Run the check when a diff touches any covered code below. To see what changed since the model was last confirmed:

```sh
sync=$(git log -1 --format=%H -- skills/verify-validation-update)
git diff $sync -- src/workspace_lifecycle/service.py | grep -nE "^@@|def update_validation|def update_preflight|_correctable_finish|_effectless_finish|_recorded_finish|_FINISH_INTENT_FIELDS|commit_tree|committed|initial_head|'restoring'|'resolved'|_index_paths|_push\("
```

Empty output means the model still describes the code.

## Coverage

Sync commit: the last commit that changed this skill folder, as `sync` above finds it. Each commit that changes this folder states that the model describes the covered code at that commit. So commit a change here only after you have read the covered code and confirmed that every action still matches it.

| Code at the sync commit (`service.py`) | Model |
| --- | --- |
| `finish()` opens the intent, or adopts a commit that matches `commit_tree` (lines 1179-1197) | `Enter` |
| `_resolve_files()` restore: phase `restoring` saved, file restored or removed, phase `resolved` saved (950-958) | `RestoreIntent`, `RestoreEffect`, `RestoreDone` |
| `finish()` `git add` of the commit paths (1252-1253); an empty commit plan goes straight to validation | `Stage` |
| `finish()` `commit_tree` saved (1257) | `Tree` |
| `finish()` `git commit` (1260) | `Commit` |
| `finish()` `committed` saved (1265) | `Committed` |
| `finish()` `_run_validation`; a failure keeps the intent (1269-1270) | `Validate` |
| `finish()` `_push()` runs the preflight; a refusal keeps the intent (1291) | `Push` |
| `finish()` acceptance saved, intent dropped (1292-1312) | `Record` |
| `update_validation()` (374-435), `_effectless_finish()` (440-447), `_recorded_finish()` (450-468), the index check | `UpdateValidation`, `Effectless`, `Recorded` |
| `update_preflight()` (321-350) and `_correctable_finish()` (353-371) | `UpdatePreflight` |
| process killed; the state file stays | `Kill` |

## Run

Requirements: Python 3.14, a Java runtime, and `tla2tools.jar` from TLA+ v1.7.4 (`https://github.com/tlaplus/tlaplus/releases/download/v1.7.4/tla2tools.jar`, SHA-256 `936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88`). The script refuses any other TLC version, because a different TLC can report a different path.

From the repository root:

```sh
python -B skills/verify-validation-update/scripts/check.py --mutants --java JAVA --tla2tools TLA2TOOLS_JAR
```

`--java` and `--tla2tools` fall back to the `JAVA` and `TLA2TOOLS` environment variables. It takes about 6 seconds. Exit 0 means every result matched; 1 means a mismatch; 2 means TLC could not run.

Each output line is JSON. The script checks three configurations in `model/`, a plan with a restore entry (`with-restore`), one with only commit entries (`commit-only`) and an empty plan (`empty-plan`); both rules hold in each. `FinishCompletes` is checked under the fairness in `Spec`: the finish steps are weakly fair, and `UpdateValidation` and `UpdatePreflight`, each enabled only when it would succeed, are strongly fair. A temporal violation is reported as `FinishCompletes`.

With `--mutants`, it also breaks one check at a time in a copy of the model and requires each break to be caught by its rule. The mutants stand for: `_effectless_finish` ignoring recorded actions, the restore phase saved after the file is removed, a failed validation dropping the intent, finish not adopting its own commit that matches `commit_tree`, `_recorded_finish` refusing a recorded commit, `update_preflight` refusing every pending finish as it did before #69, and `_correctable_finish` releasing a finish whose commit is recorded.

`_effectless_finish`'s `commit_tree` test, `_recorded_finish`'s `commit_tree` test without `committed`, and the index check each back up the others: a kill between `git add` and `git commit` leaves the index staged or HEAD moved, which the other checks see. So no mutant stands for any one of them.

## Read a result

Read every result with the `/statechart` skill, with `model/ValidationUpdate.tla` and the configuration in question. For a chart in the reader's words, give it `model/chart.cfg` (commit only, one kill, no liveness property) and the view `model/chart.mjs`; the view draws every running step as one state, splits stopped finishes into those with and without a recorded effect, and marks a wrong preflight. A stopped state with a wrong preflight and no `update-preflight` arrow out is the #69 trap. `/statechart` tells each invariant violation as its shortest path in plain words; for a `FinishCompletes` violation read TLC's lasso in `tlc.out`, whose `Back to state` line marks the loop that never reaches the acceptance.

A `match: false` line or a mutant with `caught: false` needs a decision:

1. **A real bug.** Replay the path against the real code with the fixtures of `tests/test_validation_update.py` or `tests/test_preflight_update.py`: patch the function an action names to raise where the path has `Kill`, call `finish()`, `update_validation()` or `update_preflight()` again for each later step, and compare the saved state with the rule. If the replay reaches the end state, it is a bug. File it as an Issue with the path and the replay. Do not weaken the rule or the model to make it pass.
2. **A modeling error.** If the replay cannot reach the end state, the model allows a step the code does not have. Find the action that differs from the code and fix it against the Coverage table.
3. **A mutant that is no longer caught.** Either the model lost the check that the mutant removes, or `MUTANTS` in `scripts/check.py` no longer matches the model text, which the script reports as `model text not found once`.

## Change the model with the code

When a change to covered code is intended, update the action that the Coverage table names, so that one action is still one saved state change or one Git or filesystem effect. Update the table's line numbers and the mutant text if they moved. A configuration that is known to break gets the rule name in `EXPECTED` in `scripts/check.py`, next to the Issue that tracks it.

To prove that the check still catches a break after you change it, break the real code on a scratch copy, for example drop `and intent.get('actions') == {}` from `_effectless_finish()`. Make the same edit to the matching definition (`Effectless` drops `/\ intent.phase = None`) and run the script. It must report a mismatch. Then undo both edits and confirm that the script exits 0 again.

## What the model leaves out

- Bounds: one restore entry, one commit, one validator correction, one preflight correction, up to two kills.
- Archive entries, `--revise-plan-evidence`, `--revise-plan-head` (`head_revision`), synchronization, plan exceptions and dirt checks are not modeled.
- The owner supplies the current HEAD to `update-validation`; a wrong `--expected-head` is refused and changes nothing. `update-preflight` reads HEAD itself.
- A preflight that decides `hold` or `ask`, and a push that fails after the preflight allowed it, are modeled like a preflight that cannot start: nothing is recorded and the intent stays.
- The integration after the acceptance is the subject of `verify-finish-integration`.
- A pass covers this model under these bounds, not the whole lifecycle.
