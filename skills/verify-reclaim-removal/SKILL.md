---
name: verify-reclaim-removal
description: Model-check that reclaim deletes only the retired payload bytes it captured, records a member removed only once it is gone, removes the payload directory only when empty, and resumes after a crash at any step. Use when a change touches reclaim() in src/workspace_lifecycle/service.py, or remove_tree(), _remove_tree_windows(), _remove_tree_windows_bound(), _win_open(), _resolve_root() or _validate_resuming_names() in reclamation.py.
license: MIT
---

# Verify reclaim removal

**Rules.**

- `OnlyCapturedBytesDeleted`: reclaim deletes only the object it captured at retirement, with the bytes it captured.
- `RemovedMeansGone`: a member is recorded as removed only once the captured object is gone.
- `RootGoneOnlyEmpty`: the payload directory is removed only when it is empty.
- `NoRefusalWithoutWriter`: without any writer, a crash and rerun at any step never strands reclaim in a refusal.

**Harm when they break.** Reclaim deletes a file someone wrote or changed in the retired payload, so their data is lost; or the receipt claims a removal that did not happen; or an interrupted reclaim can never finish without hand repair.

Tests replay a few crash points through the `_checkpoint` hook. `model/ReclaimRemoval.tla` is a TLA+ model of the deletion steps of `reclaim()` and `remove_tree()` for a payload that holds one captured file. TLC checks every order of these steps with a crash at any step followed by a rerun, and a writer that edits, replaces with the same bytes, deletes or creates the file under the names it knows.

## When to use

Run the check when a diff touches any covered code below. To see what changed since the model was last confirmed:

```sh
sync=$(git log -1 --format=%H -- skills/verify-reclaim-removal)
git diff $sync -- src/workspace_lifecycle/reclamation.py
git diff $sync -- src/workspace_lifecycle/service.py | grep -nE "^@@|reclaim|staging|_tree_manifest|remove_tree|capture"
```

Empty output from both means the model still describes the code. Changes only to `capture()`, `_scan_linux()`, `_scan_windows()` or the identity helpers change what a match means, not the order of steps; read them against the assumptions below.

## Coverage

Sync commit: the last commit that changed this skill folder, as `sync` above finds it. Each commit that changes this folder states that the model describes the covered code at that commit. So commit a change here only after you have read the covered code and confirmed that every action still matches it.

| Code at the sync commit | Model |
| --- | --- |
| `service.py` `reclaim()` phase `requested`: capture compared with the receipt, phase `authorized` (lines 2306-2322) | `Auth` |
| `reclaim()` manifest check of the recovery path before the rename (2323-2334) | `StageCheck` |
| `reclaim()` `recovery.rename(staging)` (2328) | `StageRename` |
| `reclaim()` identity and manifest check of staging, phase `renamed` (2335-2344) | `StagedCheck` |
| `reclaim()` phase `payload-removed` (2352-2353) | `Finish` |
| `reclamation.py` `remove_tree()` root already gone (603-618), `_remove_tree_windows()` (480-486) | `Remove` |
| `remove_tree()` root rename intent and `os.rename` to the root quarantine name (619-631) | `RootIntent`, `RootRename` |
| `remove_tree()` capture or `_validate_resuming_names` (633-637) | `Scan` |
| `remove_tree()` member name and quarantine name states (646-683) | `Member` |
| `remove_tree()` member intent, `os.rename` to the quarantine name (684-689) | `Intent`, `Rename` |
| `remove_tree()` quarantined identity, status `quarantined` (692-696) | `CheckId` |
| `remove_tree()` identity, links and digest on the descriptor (698-706) | `Digest` |
| `remove_tree()` `os.unlink` (709) | `Unlink` |
| member status `removed` (724-725, Windows 556) | `Removed` |
| root `remove_intent` and `os.rmdir` (728-747, Windows 558-570) | `RootRmIntent`, `Rmdir` |
| `_remove_tree_windows_bound()` names and missing members (506-522) | `WMember` |
| Windows member intent (523-524) | `WIntent` |
| `_win_open()` with DELETE and read-only sharing (249-261; 530) | `WOpen`; writers are shut out in `HandleHeld` |
| identity, links and `_win_hash` through the handle, status `quarantined` (531-545) | `WCheck` |
| `_win_delete()` on the handle (546-550) | `WDelete` |
| process killed; a Windows handle closes, progress records stay | `Crash` |

## Run

Requirements: Python 3.14, a Java runtime, and `tla2tools.jar` from TLA+ v1.7.4 (`https://github.com/tlaplus/tlaplus/releases/download/v1.7.4/tla2tools.jar`, SHA-256 `936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88`). The script refuses any other TLC version, because a different TLC can report a different path.

From the repository root:

```sh
python -B skills/verify-reclaim-removal/scripts/check.py --mutants --java JAVA --tla2tools TLA2TOOLS_JAR
```

`--java` and `--tla2tools` fall back to the `JAVA` and `TLA2TOOLS` environment variables. It takes about 8 seconds. Exit 0 means every result matched; 1 means a mismatch; 2 means TLC could not run.

Each output line is JSON. The script checks four configurations in `model/`, and every rule holds in each:

| Configuration | Meaning |
| --- | --- |
| `linux-contract`, `windows-contract` | up to two writes under the recovery path, before reclaim renames it to staging; this is what the owner release contract allows |
| `linux-stray`, `windows-stray` | the writer also reaches the staging path, though not the quarantine names |

With `--mutants`, it also breaks one check at a time in a copy of the model and requires each break to be caught by its rule. The mutants stand for: the member status `removed` saved before the unlink, the Linux member intent not saved before its rename, the Windows member intent not saved before the delete, `_win_open` sharing write access, `_win_hash` skipped, and the root removed with its contents.

Several checks back each other up, so removing one alone breaks no rule in the model: the identity check after the Linux rename and the identity check on the descriptor; the Linux digest check and the capture check in `Scan`, since no modeled writer reaches the root quarantine name; and the staging manifest check and the per-member checks. Each layer still holds alone, so no mutant stands for them.

## Read a result

Read every result with the `/statechart` skill, with `model/ReclaimRemoval.tla` and the configuration in question. For a chart in the reader's words, give it `model/chart.cfg` (Linux, one write under the recovery path) and the view `model/chart.mjs`; the view hides crash steps, which return to the same state. `/statechart` tells each violation as its shortest path in plain words.

A `match: false` line or a mutant with `caught: false` needs a decision:

1. **A real bug.** Replay the path against the real `reclamation.py` with the fixtures of `tests/test_reclamation.py`: patch `reclamation._checkpoint` to raise at the event closest to the crash step, change files between calls where the path has a writer step, and call `remove_tree()` again for a rerun. If the replay deletes or records what the rule forbids, it is a bug. File it as an Issue with the path and the replay. Do not weaken the rule or the model to make it pass.
2. **A modeling error.** If the replay cannot reach the end state, the model allows a step the code does not have. Find the action that differs from the code and fix it against the Coverage table.
3. **A mutant that is no longer caught.** Either the model lost the check that the mutant removes, or `MUTANTS` in `scripts/check.py` no longer matches the model text, which the script reports as `model text not found once`.

## Change the model with the code

When a change to covered code is intended, update the action that the Coverage table names, so that one action is still one saved progress record or one filesystem effect. Update the table's line numbers and the mutant text if they moved. A configuration that is known to break gets the rule name in `EXPECTED` in `scripts/check.py`, next to the Issue that tracks it.

To prove that the check still catches a break after you change it, break the real code on a scratch copy, for example drop `or _win_hash(handle) != member["digest"]` in `_remove_tree_windows_bound()`. Make the same edit to the matching action (`WCheck` drops `/\ content["o1"] = "orig"`) and run the script. It must report a mismatch. Then undo both edits and confirm that the script exits 0 again.

## What the model leaves out

- Bounds: one captured file in the payload root, up to two writes, any number of crashes and reruns.
- The admin archive removal (phases `payload-removed` to `admin-removed`) repeats `remove_tree()` on another tree and is not modeled separately. `retire()`, which moves the checkout to its recovery path and writes the manifest, is not modeled.
- Nested directories, hard links, links, mounts and nested repositories: the capture and scan refuse them, and the model assumes they do.
- Writers into the quarantine names, and processes that keep a file open across the release, are outside the owner release contract and are not modeled; the README says these are not made safe.
- The receipt and the per-task progress journal are one record here. The journal file order (`reclaim-progress`) and concurrent changes of the receipt by another process are not modeled.
- `capture()`, `same_capture()`, `same_identity()`, the digests and the Windows handle semantics are assumed correct.
- A pass covers this model under these bounds, not the whole lifecycle.
