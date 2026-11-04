---
name: verify-run-lease-exclusion
description: Model-check that finish, hold, sync and retire never change a task while another session's command still runs in its checkout. Use when a change touches run leases in src/workspace_lifecycle/leases.py (run, guard, release, _own_use, _mutation, _lock), the allow_use callers of guard, or the automatic lease release in cli.py.
license: MIT
---

# Verify run lease exclusion

**Rule.** While a mutation (finish, hold, sync, retire, or any other `guard()` caller) changes a task, no command started by another `run()` session of that task is still running.

**Harm when it breaks.** The lifecycle commits, integrates or retires a checkout under an agent that is still editing it, so that agent's work is lost or mixed into the wrong commit.

Tests drive a few fixed orders of these steps. `model/RunLeases.tla` is a TLA+ model of the lock and receipt steps in `leases.py`, and TLC checks every order of them for two sessions, one outside mutation, a mutation from inside each session, and one `lease-release`, with a supervisor allowed to die at any step.

## When to use

Run the check when a diff touches any covered code below. To see what changed since the model was last confirmed:

```sh
sync=$(git log -1 --format=%H -- skills/verify-run-lease-exclusion)
git diff $sync -- src/workspace_lifecycle/leases.py
git diff $sync -- src/workspace_lifecycle/service.py src/workspace_lifecycle/cli.py src/workspace_lifecycle/producers.py src/workspace_lifecycle/compat.py | grep -nE "lease|guard|allow_use"
```

Empty output from both means the model still describes the code.

## Coverage

Sync commit: the last commit that changed this skill folder, as `sync` above finds it. Each commit that changes this folder states that the model describes the covered code at that commit. So commit a change here only after you have read the covered code and confirmed that every action still matches it.

| Code at the sync commit | Model |
| --- | --- |
| `leases.py` `run()`: register lock, mutation lock probe, shared use lock, first `_save` (lines 434-448) | `RunRegister`, `RunProbe`, `RunShare`, `RunSaveReceipt` |
| `run()` Windows `windows_job` save before `Popen`, and the job handle the command inherits (459-467) | `RunSaveJob`, constant `Windows = TRUE` |
| `run()` `Popen` with the use token in its environment, then `child_pid`/`child_identity`/`process_group` save (496-502) | `RunSpawn`, `RunRecordChild` |
| `run()` wait for child and descendants, `_remove(receipt)`, lock close (504-531) | `ChildExits`, `RunComplete`, `RunUnlock` |
| supervisor killed at any point; the OS drops its locks | `SupervisorCrashes` |
| `_mutation()` and `guard()` without `allow_use` (148-177) | `OutEnter`, `OutReadUses`, `OutExclusive`, `OutDone` |
| `guard(allow_use=True)` with `_own_use()` (169-171, 371-386); callers in `service.py`, `producers.py` | `InEnter`, `InDecide`, `InDone` |
| `release()`: owner check, second `_read`, child/group/job checks, `_token_active` (536-564); `lease-release` and the `completion_release` loop in `cli.py` (325-330) | `RelEnter`, `RelPick`, `RelCheck`, constant `Release` |

## Run

Requirements: Python 3.14, a Java runtime, and `tla2tools.jar` from TLA+ v1.7.4 (`https://github.com/tlaplus/tlaplus/releases/download/v1.7.4/tla2tools.jar`, SHA-256 `936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88`). The script refuses any other TLC version, because a different TLC can report a different path.

From the repository root:

```sh
python -B skills/verify-run-lease-exclusion/scripts/check.py --mutants --java JAVA --tla2tools TLA2TOOLS_JAR
```

`--java` and `--tla2tools` fall back to the `JAVA` and `TLA2TOOLS` environment variables. It takes about 7 seconds. Exit 0 means every result matched; 1 means a mismatch; 2 means TLC could not run.

Each output line is JSON. The script checks five configurations in `model/`, and every rule holds in each:

| Configuration | Meaning |
| --- | --- |
| `protocol` | runs, mutations and supervisor deaths, no `lease-release` |
| `release-after-death-windows`, `release-after-death-linux` | `lease-release` only while no supervisor lives |
| `release-any-time-windows`, `release-any-time-linux` | `lease-release` at any time, also while a supervisor starts its command |

With `--mutants`, it also breaks one check at a time in a copy of the model and requires each break to be caught. The mutants stand for removing the mutation lock probe in `run()`, `if uses: raise` in `guard()`, `len(uses) == 1` in `guard()`, the descendants wait in `run()`, and the child and job checks in `release()`. Three more stand for the parts of the #77 fix: the job handle the command inherits, `_token_active()`, and the second `_read()` in `release()`.

## Read a result

Read every result with the `/statechart` skill, with `model/RunLeases.tla` and the configuration in question. For a chart in the reader's words, give it `model/chart.cfg` (one session) and the view `model/chart.mjs`. `/statechart` tells each violation as its shortest path in plain words.

A `match: false` line or a mutant with `caught: false` needs a decision:

1. **A real bug.** Replay the path against the real `leases.py`, one step per action, as `crashing_supervisor` in `tests/test_leases.py` does: pause a process at a step by wrapping the function the action names, and end the supervisor with `os._exit` where the path has `SupervisorCrashes`. On Windows, start that supervisor with `sys._base_executable`: a venv launcher kills its job, and with it the command, when the supervisor dies. If the replay ends inside `guard()` with the other command still running, it is a bug. File it as an Issue with the path and the replay. Do not weaken the rule or the model to make it pass.
2. **A modeling error.** If the replay cannot reach the end state, the model allows a step the code does not have. Find the action that differs from the code and fix it against the Coverage table.
3. **A mutant that is no longer caught.** Either the model lost the check that the mutant removes, or `MUTANTS` in `scripts/check.py` no longer matches the model text, which the script reports as `model text not found once`.

## Change the model with the code

When a change to covered code is intended, update the action that the Coverage table names, so that one action is still one lock call, one receipt write or one process event. Update the table's line numbers and the mutant text if they moved. A configuration that is known to break gets the rule name in `EXPECTED` in `scripts/check.py`, next to the Issue that tracks it.

To prove that the check still catches a break after you change it, break the real code on a scratch copy, for example delete the `if uses:` raise in `guard()`. Make the same edit to the matching action (`OutExclusive` drops `seen = {}`) and run the script. It must report a mismatch. Then undo both edits and confirm that the script exits 0 again.

## What the model leaves out

- Bounds: two sessions, one outside mutation, one `lease-release`, each able to repeat. More of each is not checked.
- `_uses()` reads every receipt at once. In the code it reads them one by one, but while a mutation holds the mutation lock receipts can only disappear, so the order does not matter for the rule.
- A command and all its descendants are one process. Descendants that leave the process group and outlive both the child and the supervisor on Linux are not modeled; `release()` does not see them by design and relies on its evidence.
- The command keeps the use token in its environment and, on Windows, its inherited job handle. A process that clears its environment or closes that handle is not modeled, nor are descendants that outlive the command after doing so. `release()` then relies on its evidence.
- A run that joins its owner's Windows job (`_joined_run`), receipts of 0.4 supervisors, the legacy `agent-branches` registry, and the process checks themselves (`_identity`, `_windows_active`, `_token_active`) are assumed correct.
- A pass covers this model under these bounds, not the whole lifecycle.
