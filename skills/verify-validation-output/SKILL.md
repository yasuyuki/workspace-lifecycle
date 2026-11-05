---
name: verify-validation-output
description: Model-check that finish's dirt checks accept only a clean checkout, remove only the declared outputs the same run's validator created, never remove another session's data, and integrate a task whose owner keeps rerunning finish and doing what each refusal names. Use when a change touches the dirt snapshot, _unexcused_dirty(), _exception(), _initial_target_dirt() or the validation call in finish(), _run_validation(), _created_hint(), _output_patterns(), _target_snapshot() or _target_dirt(), the target validation and `integration changed during final validation` check in _integrate(), or validation_outputs in begin() and update_validation() in src/workspace_lifecycle/service.py.
license: MIT
---

# Verify validation output

**Rules.**

- `AcceptedClean`: finish accepts a task only when its checkout holds no dirt, including private data another session of the task wrote while the validator ran.
- `NoSelfInflictedRefusal`: once the task declares its validator outputs, a refusal never names one that the validation of the same run created.
- `ForeignKept`: finish never removes data another session wrote.
- `FinishCompletes` (a liveness property): if the owner keeps running finish and, after each refusal, deletes the reproducible files it names, archives the private ones, and declares the outputs it says the validation created, the task is integrated. Replacing the validator is allowed but never required.

**Harm when they break.** A finish that skips the dirt check accepts and later retires a checkout with unsaved data. A cleanup that removes more than the run's declared outputs deletes another session's work. A refusal of the validator's own output repeats on every rerun, because the validator writes it again: before #38 and #89 were fixed, this stopped tasks in four repositories (agent-rules#74, agent-environment#24 and #32).

`model/ValidationOutput.tla` is a TLA+ model of one task with one owned change. Its checkout can also hold `valout` (ignored output the validator writes), `manual` (ignored output of a test the agent ran by hand) and `foreign` (private data another session writes during finish, which the run lease of finish allows). The integration target is the default branch checkout or a parent task's checkout. TLC checks every order of these steps.

## When to use

Run the check when a diff touches any covered code below. To see what changed since the model was last confirmed:

```sh
sync=$(git log -1 --format=%H -- skills/verify-validation-output)
git diff $sync -- src/workspace_lifecycle/service.py | grep -nE "^@@|_snapshot|_unexcused_dirty|_exception|_initial_target_dirt|_target_dirt|_target_snapshot|_validate|_run_validation|_created_hint|_output_patterns|validation_outputs|outputs_only|final_dirty|leftover|candidate|integration changed during final validation|allow_use"
```

Empty output means the model still describes the code.

## Coverage

Sync commit: the last commit that changed this skill folder, as `sync` above finds it. Each commit that changes this folder states that the model describes the covered code at that commit. So commit a change here only after you have read the covered code and confirmed that every action still matches it.

| Code at the sync commit (`service.py`) | Model |
| --- | --- |
| `begin()` stores `validation_outputs` checked by `_output_patterns()` (199-, 556-565) | `Init`, `declared` |
| `finish()` takes its run lease with `allow_use=True` (1058), so another session of the task can write meanwhile | `Foreign` |
| `finish()` resumes an accepted task straight at `_integrate()` (`accepted_resume`, 1119) | `Pre`, accepted branch |
| `finish()` refuses dirt the plan does not cover before any effect: `_snapshot`, `uncovered`, `_unexcused_dirty`, `_exception` (1183-1190, 726-742, 745-752) | `Pre`, `leftover` |
| `finish()` refuses target dirt before any effect: `_initial_target_dirt()` (1194, 755-777) | `Pre`, `TargetDirt` |
| `_resolve_files()` moves archive entries out (1205, 844-) | `Resolve` |
| `finish()` `git commit` of the commit paths (1221) | `Commit` |
| `finish()` `evidence = None if candidate else _run_validation(...)` (1230); the validator run in `_run_validation()` (573-584) | `Validate` |
| `_run_validation()` removes ignored regular files that are new and match a declared pattern (585-600) | `Clean`, `ItClean`, `Sweep` |
| `finish()` `final_dirty` refused by `_exception()` with `_created_hint()`, else push and acceptance (1237-1252, 603-609) | `Post` |
| `_target_snapshot()`: a default branch target does not count ignored data, a parent task target does (49-58) | `TargetDirt` |
| `_integrate()` `_target_dirt()` before the merge, only when no merge intent is pending (1772, 1782) | `Integrate`, `pending` |
| `_integrate()` `_run_validation(target_worktree, item['validation'], ...)` (1895) | `ItValidate`, `ItClean` |
| `_integrate()` `integration changed during final validation: <paths>`; the merge intent stays (1909-1913) | `ItCheck` |
| the owner deletes named files with `guarded_delete`, archives them with a revised plan, or declares outputs with `update_validation()` and an unchanged argv, which `outputs_only` (372) allows also for an accepted task and beside a pending merge | `Recover` |
| the owner runs tests by hand in the task checkout | `Manual` |
| `update_validation()` to another validator (346-) | `UpdateValidation` |

## Run

Requirements: Python 3.14, a Java runtime, and `tla2tools.jar` from TLA+ v1.7.4 (`https://github.com/tlaplus/tlaplus/releases/download/v1.7.4/tla2tools.jar`, SHA-256 `936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88`). The script refuses any other TLC version, because a different TLC can report a different path.

From the repository root:

```sh
python -B skills/verify-validation-output/scripts/check.py --mutants --java JAVA --tla2tools TLA2TOOLS_JAR
```

`--java` and `--tla2tools` fall back to the `JAVA` and `TLA2TOOLS` environment variables. It takes about 8 seconds. Exit 0 means every result matched `EXPECTED` in `scripts/check.py`; 1 means a mismatch; 2 means TLC could not run.

Each output line is JSON, one per configuration in `model/`. Every configuration starts with outputs declared or not, and every rule holds in each at the sync commit:

| Configuration | Validator | Target, gating |
| --- | --- | --- |
| `clean` | writes nothing | both, gated or not; one manual test run, one foreign write |
| `task-writes` | writes output | both, not gated; one manual test run, one foreign write |
| `candidate-default-writes` | writes output | default branch, gated by required checks |
| `candidate-parent-writes` | writes output | parent task, gated (#89) |

With `--mutants`, it also breaks one check at a time in a copy of the model and requires each break to be caught by its rule: finish accepting without the dirt check after validation (`AcceptedClean`); the cleanup leaving declared outputs (`NoSelfInflictedRefusal`); the cleanup removing a file the run did not create from its declaration (`ForeignKept`); a refusal not saying the validation created the file, and `update-validation` refusing a declaration beside a pending merge (`FinishCompletes`, the #38 and #89 dead ends); archive entries staying in the checkout (`FinishCompletes`).

## Read a result

Read every result with the `/statechart` skill, with `model/ValidationOutput.tla`, `model/chart.cfg` and the view `model/chart.mjs`. It tells each invariant violation as its shortest path in plain words and charts how a task moves between work, refusal, declaration and integration. For a `FinishCompletes` violation, read TLC's lasso; the loop is refusal, delete, finish, refusal.

A `match: false` line or a mutant with `caught: false` needs a decision:

1. **A real bug.** Replay the path against the real code. `ValidatorOutputTest` in `tests/test_dirty.py` drives the task checkout with a validator that writes `cache/m.pyc`; `test_a_gated_child_declares_its_validator_output_after_the_parent_refuses_it` in `tests/test_required_checks.py` drives a gated child into its parent. Call `finish()` once per `Start`, and for `Recover` delete what the refusal names or call `update_validation(..., outputs=[...])` with the same argv. If the replay reaches the end state, it is a bug. File it as an Issue with the path and the replay, and add the rule to `EXPECTED` with the Issue number. Do not weaken a rule or the model to make it pass.
2. **A modeling error.** If the replay cannot reach the end state, the model allows a step the code does not have. Find the action that differs from the code and fix it against the Coverage table.
3. **A mutant that is no longer caught.** Either the model lost the check that the mutant removes, or `MUTANTS` in `scripts/check.py` no longer matches the model text, which the script reports as `model text not found once`.

## Change the model with the code

When a change to covered code is intended, update the action that the Coverage table names, so that one action is still one dirt check, one validation, one cleanup or one file effect. Update the table's line numbers and the mutant text if they moved.

To prove that the check still catches a break after you change it, break the real code on a scratch copy, for example replace the pattern test `not any(PurePosixPath(name).full_match(pattern) for pattern in outputs)` in `_run_validation()` with `not outputs`. Then `test_files_outside_the_declaration_stay_and_are_refused` fails, because finish removes `cache/secret.env` and accepts. Make the same edit to the model (`Sweep(paths) == IF declared THEN paths \cap (made \cup {"foreign"}) ELSE {}`) and run the script. The `clean` and `task-writes` lines must report a mismatch (`ForeignKept`). Then undo both edits and confirm that the script exits 0 again.

## What the model leaves out

- Bounds: one owned change, one validator output path, one manual test run and one foreign write per configuration.
- A declared pattern that also matches another session's file removes it. The model takes the owner's declaration to name only files the validator regenerates; `foreign` stands for private data outside it.
- `PYTHONDONTWRITEBYTECODE=1` in `_validate()` is not modeled; for a Python validator it makes `TaskWrites` false.
- Synchronization validation (`_synchronize()`, 1465) uses the same `_run_validation()` but is not modeled.
- Kills and resumption are the subject of `verify-validation-update` and `verify-finish-integration`; this model runs each step to its end.
- Plan restore entries are not modeled: restore runs in `_resolve_files()`, before validation, so it cannot remove output the validator writes later.
- Plan exceptions (`completion: exception`), producer owner receipts, primary checkout waivers, preserved paths, merge conflicts and the wait for required checks are not modeled.
- The parent task's own validator (`parent['validation']`) is taken to write nothing, because the parent's own finish already refuses one that does.
- A pass covers this model under these bounds, not the whole lifecycle.
