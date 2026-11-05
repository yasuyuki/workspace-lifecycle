---
name: verify-validation-output
description: Model-check that finish's dirt checks accept only a clean checkout, never refuse a file that the same run's validator wrote, and integrate a task whose owner keeps rerunning finish and deleting what each refusal names. Use when a change touches the dirt snapshot, _unexcused_dirty(), _exception(), _initial_target_dirt() or the _validate() call in finish(), _target_snapshot() or _target_dirt(), or the target validation and `integration changed during final validation` check in _integrate() in src/workspace_lifecycle/service.py.
license: MIT
---

# Verify validation output

**Rules.**

- `AcceptedClean`: finish accepts a task only when its checkout holds no dirt, including private data another session of the task wrote while the validator ran.
- `NoSelfInflictedRefusal`: a refusal never names a file that the validation of the same run created.
- `FinishCompletes` (a liveness property): if the owner keeps running finish, and after each refusal deletes the reproducible files it names and archives the private ones, the task is integrated. Running `update-validation` is allowed but never required.

**Harm when they break.** A finish that skips the dirt check accepts and later retires a checkout with unsaved data. A refusal of the validator's own output repeats on every rerun: deleting the file does not help, because the validator writes it again, so the task cannot be finished through the normal entry. This has stopped tasks in four repositories (#38, #89, agent-rules#74, agent-environment#24 and #32).

`model/ValidationOutput.tla` is a TLA+ model of one task with one owned change. Its checkout can also hold `valout` (ignored output the validator writes, such as `__pycache__/*.pyc`), `manual` (ignored output of a test the agent ran by hand) and `foreign` (private data another session writes during finish, which the run lease of finish allows). The integration target is the default branch checkout or a parent task's checkout. TLC checks every order of these steps.

## When to use

Run the check when a diff touches any covered code below. To see what changed since the model was last confirmed:

```sh
sync=$(git log -1 --format=%H -- skills/verify-validation-output)
git diff $sync -- src/workspace_lifecycle/service.py | grep -nE "^@@|_snapshot|_unexcused_dirty|_exception|_initial_target_dirt|_target_dirt|_target_snapshot|_validate|final_dirty|leftover|candidate|integration changed during final validation|allow_use"
```

Empty output means the model still describes the code.

## Coverage

Sync commit: the last commit that changed this skill folder, as `sync` above finds it. Each commit that changes this folder states that the model describes the covered code at that commit. So commit a change here only after you have read the covered code and confirmed that every action still matches it.

| Code at the sync commit (`service.py`) | Model |
| --- | --- |
| `finish()` takes its run lease with `allow_use=True` (980), so another session of the task can write meanwhile | `Foreign` |
| `finish()` resumes an accepted task straight at `_integrate()` (`accepted_resume`, 1041) | `Pre`, accepted branch |
| `finish()` refuses dirt the plan does not cover before any effect: `_snapshot`, `uncovered`, `_unexcused_dirty`, `_exception` (1105-1112, 648-664, 667-674) | `Pre`, `leftover` |
| `finish()` refuses target dirt before any effect: `_initial_target_dirt()` (1116, 677-699) | `Pre`, `TargetDirt` |
| `_resolve_files()` moves archive entries out (1127, 766-) | `Resolve` |
| `finish()` `git commit` of the commit paths (1143) | `Commit` |
| `finish()` `evidence = None if candidate else _validate(...)` (1152) | `Validate` |
| `finish()` `final_dirty` refused by `_exception()`, else push and acceptance (1158-1167) | `Post` |
| `_target_snapshot()`: a default branch target does not count ignored data, a parent task target does (49-58) | `TargetDirt` |
| `_integrate()` `_target_dirt()` before the merge (1694-1696, 702-706) | `Integrate` |
| `_integrate()` `_validate(target_worktree, item['validation'])` (1805) | `ItValidate` |
| `_integrate()` `integration changed during final validation`; the merge is withdrawn and the files stay (1815-1816) | `ItCheck` |
| the owner deletes named files with `guarded_delete`, or archives them with a revised plan | `Recover` |
| the owner runs tests by hand in the task checkout | `Manual` |
| `update_validation()` (342-) | `UpdateValidation` |

## Run

Requirements: Python 3.14, a Java runtime, and `tla2tools.jar` from TLA+ v1.7.4 (`https://github.com/tlaplus/tlaplus/releases/download/v1.7.4/tla2tools.jar`, SHA-256 `936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88`). The script refuses any other TLC version, because a different TLC can report a different path.

From the repository root:

```sh
python -B skills/verify-validation-output/scripts/check.py --mutants --java JAVA --tla2tools TLA2TOOLS_JAR
```

`--java` and `--tla2tools` fall back to the `JAVA` and `TLA2TOOLS` environment variables. It takes about 6 seconds. Exit 0 means every result matched `EXPECTED` in `scripts/check.py`; 1 means a mismatch; 2 means TLC could not run.

Each output line is JSON, one per configuration in `model/`:

| Configuration | Validator | Target, gating | Expected at the sync commit |
| --- | --- | --- | --- |
| `clean` | writes nothing | both, gated or not; one manual test run, one foreign write | every rule holds |
| `candidate-default-writes` | writes output | default branch, gated by required checks | every rule holds |
| `task-writes` | writes output | both, not gated | `NoSelfInflictedRefusal` breaks: #38 |
| `task-writes-loop` | writes output | default, not gated; only `FinishCompletes` | `FinishCompletes` breaks: #38 |
| `candidate-parent-writes` | writes output | parent task, gated | `NoSelfInflictedRefusal` breaks: #89 |

With `--mutants`, it also breaks one check at a time in a copy of the model and requires each break to be caught by its rule: finish accepting without the dirt check after validation (`AcceptedClean`), a default branch target counting its ignored data, as before #24 (`NoSelfInflictedRefusal`), and archive entries staying in the checkout (`FinishCompletes`).

## Read a result

Read every result with the `/statechart` skill, with `model/ValidationOutput.tla`, `model/chart.cfg` and the view `model/chart.mjs`. It tells each invariant violation as its shortest path in plain words and charts how a task moves between work, refusal and integration, including the `update-validation` escape. For a `FinishCompletes` violation, read TLC's lasso in the output of `check.py`'s run; the loop is refusal, delete, finish, refusal.

A `match: false` line or a mutant with `caught: false` needs a decision:

1. **A real bug.** Replay the path against the real code. For the task checkout, use the fixture of `tests/test_dirty.py`: commit a `.gitignore` for `cache/`, begin with a validator such as `[sys.executable, "-c", "import pathlib; p=pathlib.Path('cache'); p.mkdir(exist_ok=True); (p/'m.pyc').write_bytes(b'x')"]`, and call `finish()` once per `Start`, deleting what the refusal names for each `Recover`. For a target, use `RequiredChecksTest` in `tests/test_required_checks.py` with `required_checks=[TEST]` and add a successful check for the pushed commit with `self.github.add`. Run the scratch test with `PYTHONPATH` set to the absolute `src` and `tests` paths, because the push preflight runs in a child process. If the replay reaches the end state, it is a bug. File it as an Issue with the path and the replay, and add the rule to `EXPECTED` with the Issue number. Do not weaken a rule or the model to make it pass.
2. **A modeling error.** If the replay cannot reach the end state, the model allows a step the code does not have. Find the action that differs from the code and fix it against the Coverage table.
3. **A mutant that is no longer caught.** Either the model lost the check that the mutant removes, or `MUTANTS` in `scripts/check.py` no longer matches the model text, which the script reports as `model text not found once`.

When #38 or #89 is fixed, the configuration's expected value becomes `None`. Confirm the fix with the same configuration before you change `EXPECTED`.

## Change the model with the code

When a change to covered code is intended, update the action that the Coverage table names, so that one action is still one dirt check, one validation or one file effect. Update the table's line numbers and the mutant text if they moved.

To prove that the check still catches a break after you change it, break the real code on a scratch copy, for example set `ignored=True` in `_target_snapshot()`. Make the same edit to the model (`TargetDirt == target`) and run the script. `candidate-default-writes` must report a mismatch. Then undo both edits and confirm that the script exits 0 again.

## What the model leaves out

- Bounds: one owned change, one validator output path, one manual test run and one foreign write per configuration.
- Kills and resumption are the subject of `verify-validation-update` and `verify-finish-integration`; this model runs each step to its end.
- Plan restore entries are not modeled: restore runs in `_resolve_files()`, before validation, so it acts like `Recover` and cannot remove output the validator writes later. The replay of #38 confirms this.
- Plan exceptions (`completion: exception`), producer owner receipts, primary checkout waivers, preserved paths, synchronization, merge conflicts and the wait for required checks are not modeled.
- The parent task's own validator (`parent['validation']`, 1806) is taken to write nothing, because the parent's own finish already refuses one that does.
- A pass covers this model under these bounds, not the whole lifecycle.
