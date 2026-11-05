---
name: verify-required-checks
description: Model-check which remote checks finish --wait-checks waits for and when it integrates - only after every check the task requires, the default branch declares and the commit declares succeeded on the commit; a refusal it could make at its start comes before the plan deletes files; and an owner who follows each refusal's recovery reaches integration. Use when a change touches select(), require(), source(), declared(), current(), watch() or wait() in src/workspace_lifecycle/checks.py, or _check_policy(), _gated_checks(), _await_checks(), the gate in _integrate(), the candidate, restore and push steps of finish(), or update_required_checks() in src/workspace_lifecycle/service.py.
license: MIT
---

# Verify required checks

**Rules.**

- `GateHolds`: finish integrates a commit only after every check the task requires, the remote default branch declares and the commit's own `.github/required-checks.json` declares succeeded on that commit.
- `NoEffectBeforeKnownRefusal`: a refusal that finish could already make from what it read at its start comes before the plan's `restore` entries delete files.
- `FinishCompletes` (a liveness property): if the owner keeps running finish and does what each refusal's recovery says, the task is integrated.

**Harm when they break.** A broken `GateHolds` puts a commit on the default branch whose required CI never passed. A broken `NoEffectBeforeKnownRefusal` deletes the plan's files and then refuses, so the same plan no longer matches (#67). A broken `FinishCompletes` keeps the owner waiting hours for a check that cannot appear on the commit, while the recovery says to keep waiting (#73).

`model/RequiredChecks.tla` is a TLA+ model of one task on one topic branch with three check names: `a`, which every commit runs and the default branch declares; `x`, which another task adds to the default branch (workflow job and declaration) after this task began; and `o`, which this task declares and runs itself. A check run appears on a commit only for a job in that commit's workflow, and only if GitHub turned the push into a workflow run; the model can lose that push event. TLC checks every order of these steps.

## When to use

Run the check when a diff touches any covered code below. To see what changed since the model was last confirmed:

```sh
sync=$(git log -1 --format=%H -- skills/verify-required-checks)
git diff $sync -- src/workspace_lifecycle/checks.py src/workspace_lifecycle/service.py | grep -nE "^@@|def (select|require|source|declared|current|watch|wait|_states|_check_policy|_gated_checks|_await_checks|update_required_checks)|PENDING|gated_checks|checks\.(verify|wait|require|select|source)|_resolve_files\(repo|candidate = |pushed = _push|prepare=True|recovery|keep waiting"
```

Empty output means the model still describes the code.

## Coverage

Sync commit: the last commit that changed this skill folder, as `sync` above finds it. Each commit that changes this folder states that the model describes the covered code at that commit. So commit a change here only after you have read the covered code and confirmed that every action still matches it.

| Code at the sync commit | Model |
| --- | --- |
| `service.py` `begin()`: `checks.current()` and `checks.select()` take the default branch declaration (245-246) | `Init`, `required` |
| `service.py` `finish()` resumes an acceptance or candidate of the current HEAD at `_integrate()` | `Pre` |
| `service.py` `finish()` `_resolve_files()` runs the plan's restore entries (1209) before any required-check decision | `Resolve`, `restored` |
| `service.py` `finish()` `candidate` from `_gated_checks()` (1232, 1530-1540), which reads `[]` when the policy refuses; `_push()` and the acceptance or candidate record (1256) | `Push` |
| `checks.py` `_states()`: a check is `missing` until a run of its name exists on the commit (173-189) | `runs`, `Push` |
| `service.py` `finish()` `_integrate(prepare=True)` (1281); `_check_policy()` with `checks.current()` and `checks.require()` (1524-1527, `checks.py` 73-77, 104-109); `gated_checks` with `checks.source()` `added` (1716, `checks.py` 80-101); `checks.verify()` (1722) | `Integrate`, `Added`, `Gate` |
| `service.py` `_await_checks()` (1543-1563); `checks.watch()` treats `missing` as pending until the deadline and `checks.wait()` answers "the accepted commit is kept; run the same finish again to keep waiting" (`checks.py` 202-245); `_withdraw_prepared()` (1289) | `Wait` |
| `service.py` `_integrate(reuse=...)` pushes to the default branch | `Final`, `gateHeld` |
| `service.py` `update_required_checks()`: the new set must include the current declaration (458) and a candidate is dropped (460) | `Recover` after `"omit"` |
| another task's integration adds a job and its declaration to the default branch | `Evolve` |
| the owner merges the remote default branch into the topic, or starts the workflow again by another push | `Sync`, `Retrigger` (no fairness: no message says to) |

## Run

Requirements: Python 3.14, a Java runtime, and `tla2tools.jar` from TLA+ v1.7.4 (`https://github.com/tlaplus/tlaplus/releases/download/v1.7.4/tla2tools.jar`, SHA-256 `936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88`). The script refuses any other TLC version, because a different TLC can report a different path.

From the repository root:

```sh
python -B skills/verify-required-checks/scripts/check.py --mutants --java JAVA --tla2tools TLA2TOOLS_JAR
```

`--java` and `--tla2tools` fall back to the `JAVA` and `TLA2TOOLS` environment variables. It takes about 5 seconds. Exit 0 means every result matched `EXPECTED` in `scripts/check.py`; 1 means a mismatch; 2 means TLC could not run.

Each output line is JSON, one per configuration in `model/`:

| Configuration | Default branch | Push events | Expected at the sync commit |
| --- | --- | --- | --- |
| `steady` | unchanged | all arrive | every rule holds, with or without the task's own declaration |
| `default-adds-check` | gains `x` | all arrive | `FinishCompletes` breaks (#73) |
| `default-adds-check-effects` | gains `x` | all arrive | `NoEffectBeforeKnownRefusal` breaks (#67) |
| `lost-event` | unchanged | one is lost | `FinishCompletes` breaks (#73) |

With `--mutants`, it also breaks one check at a time in a copy of the model and requires `GateHolds` to catch each: integration without `checks.require()`; integration without the commit's `added` checks; a wait that counts a missing check as done.

## Read a result

Read every result with the `/statechart` skill, with `model/RequiredChecks.tla`, `model/chart.cfg` and the view `model/chart.mjs`. It tells each invariant violation as its shortest path in plain words and charts how a task moves between work, refusal, waiting and integration; the `FinishCompletes` dead end shows as the loop from "requires a check this commit cannot show" through the timeout back to itself, whose only exit is the merge no message names. For a `FinishCompletes` violation, read TLC's lasso in the check output.

The findings at the sync commit, as steps in the code's words:

- **#73, `default-adds-check`.** Another task adds a job and its declaration to the default branch. finish pushes the task's commit, then `_check_policy()` refuses with `required checks omit [x]`. The owner runs `update-required-checks` as told. The next finish resumes the acceptance and `checks.wait()` polls `x`, which the commit's workflow never runs, until the timeout, and says to run the same finish again to keep waiting. Every rerun does the same. Only merging the default branch into the topic, which no message names, leads out.
- **#73, `lost-event`.** GitHub never turns the topic push into a workflow run, so every required check stays `missing`; the timeout says to keep waiting, and the same finish never pushes again. Only starting the workflow again by another push leads out (seen on 2026-11-06 for `validation-outputs-38`).
- **#67, `default-adds-check-effects`.** The default branch already declares `x` when finish starts, so `_check_policy()` would refuse; finish still runs `_resolve_files()`, which deletes the restore entries' files, and refuses only in `_integrate()`.

A `match: false` line or a mutant with `caught: false` needs a decision:

1. **A real bug.** Replay the path against the real code. `RequiredChecksTest` in `tests/test_required_checks.py` drives finish against a fake GitHub (`FakeGitHub`) whose check runs you set per commit; `test_declaration_added_after_begin_closes_the_gate_until_updated` starts from a declaration added after `begin`. Call `finish()` once per `Start`, `update_required_checks()` for `Recover` after `"omit"`, and leave a check without runs for a lost push. If the replay reaches the end state, it is a bug. File it as an Issue with the path and the replay, and add the rule to `EXPECTED` with the Issue number. Do not weaken a rule or the model to make it pass.
2. **A modeling error.** If the replay cannot reach the end state, the model allows a step the code does not have. Find the action that differs from the code and fix it against the Coverage table.
3. **A mutant that is no longer caught.** Either the model lost the check that the mutant removes, or `MUTANTS` in `scripts/check.py` no longer matches the model text, which the script reports as `model text not found once`.

When a fix for #73 or #67 lands, the configuration's expected value changes to `None` in the same change, and the model gains the step the fix adds (for example a refusal that names the merge, or a policy check before `Resolve`).

## Change the model with the code

When a change to covered code is intended, update the action that the Coverage table names, so that one action is still one policy read, one push, one wait or one file effect. Update the table's line numbers and the mutant text if they moved.

To prove that the check still catches a break after you change it, break the real code on a scratch copy, for example replace `gated_checks = required_checks + (proposed['added'] if proposed else [])` in `_integrate()` with `gated_checks = required_checks`. Then `test_declaration_added_by_the_task_gates_its_own_commit`, `test_success_before_a_new_head_does_not_cover_the_added_declaration` and `test_changed_declaration_requires_new_checks_without_dropping_old_ones` fail. Make the same edit to the model (`Gate == required`) and run the script; the `steady` line must report a mismatch (`GateHolds`). Then undo both edits and confirm that the script exits 0 again.

## What the model leaves out

- Bounds: three check names, two topic commits, at most one lost push event; CI runs end in success.
- Failed checks, reruns of a failed check and `required checks failed` are not modeled; nor are GitHub's API errors (`ChecksUnavailable`).
- Which events branch protection counts (#52) is GitHub's rule, not the code's; the model takes every run on the commit as counted.
- `checks.source()` is taken as `added = the commit's declaration minus the task's checks`; the merge-base comparison only matters when the commit inherits a declaration, and then this set is empty.
- After `update-required-checks` drops a candidate, the real finish pushes the same commit again; GitHub starts no workflow for an unchanged ref, so the model keeps the old runs.
- Kills, the deferred merge intent and `_withdraw_prepared()` across kills are the subject of `verify-finish-integration`; the checks after integration (`_await_integration_checks()`) and parent task targets are not modeled.
- A pass covers this model under these bounds, not the whole lifecycle.
