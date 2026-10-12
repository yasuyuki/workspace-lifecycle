# Shared task use

Two agents work in the same task at once, for example Codex and Claude started
by a runtime launcher in one task worktree. Neither launch is refused. A change
that would affect both, here `hold`, is refused while they run, with a recovery
that names how to inspect them. After both exit the change proceeds. A launch
from a checkout that no task is bound to runs the command directly with a
notice and leaves lifecycle state untouched.

Contract sources:

- `README.md`, the paragraphs on `run`, `lease-status` and `resolve-run`.
- `src/workspace_lifecycle/leases.py`, `run`, `guard` and `status`.
- `src/workspace_lifecycle/cli.py`, `_resolve_run` and the refusal output of `main`.
- `tests/test_dirty.py`, `test_two_sessions_of_one_task_run_together_and_hold_off_mutation`
  and `test_resolve_run_launches_legacy_and_unbound_checkouts_unsupervised`.
- [Issue #21](https://github.com/yasuyuki/workspace-lifecycle/issues/21), the acceptance list.

## Prerequisites

Python 3.10+ with `venv` and `pip`, Git, and network access for the setuptools
build backend. The helper builds everything else in its scratch directory: the
venv, a bare remote `remote.git` on branch `trunk`, its clone `root`, the task
worktree `topic` and the recorder `session.py`. The commands run with
`PYTHONPATH` and every `WORKSPACE_LIFECYCLE_*` variable removed and
`PYTHONIOENCODING=utf-8` set.

## Operations and fixed expectations

`wl` is the venv's installed `workspace-lifecycle`. A session runs
`session.py NAME GATE OUT CODE`. It writes `NAME.json` with its cwd,
`WORKSPACE_LIFECYCLE_TASK` and `WORKSPACE_LIFECYCLE_USE`, then `NAME.ready`.
It waits while the gate file exists, prints `NAME` and exits `CODE`.

| Step | Command and cwd | Expected observation |
| --- | --- | --- |
| Install | `<venv python> -m pip install --no-deps <copy>` | Installed `leases.py` and `cli.py` bytes equal the checkout's. `wl --version` equals `pyproject.toml`. |
| Begin | `wl --repo root begin --task shared ... --worktree topic` in `root` | Exit 0, `ok: true`, and `topic` exists. |
| Two sessions | `wl resolve-run --cwd topic --launch-cwd topic -- python session.py NAME GATE OUT 0` for NAME `first` and `second`, both in `topic` | Both write `.ready` while neither has exited. |
| Inspect | `wl --repo topic lease-status --task shared` | `busy: true` and two uses with distinct tokens. |
| Mutation refused | `wl --repo topic hold --task shared --reason verify --next-action none` | Exit 2. Stderr JSON `error` is `task is in use by another session: shared`, and `recovery` contains `lease-status --task shared`. |
| Sessions complete | Remove the gate | Each exits 0 and prints its name. Each record has cwd `topic`, task `shared`, and a use token from the listed two. |
| Released | `lease-status` again, then the same `hold` | `{"busy": false, "uses": []}`, then `hold` exits 0. |
| Unbound launch | `wl resolve-run --cwd root --launch-cwd root -- python session.py unbound "" OUT 41` in `root` | Exit 41 and stdout `unbound`. The first stderr line is `{"notice": {"lifecycle": "unmanaged", "repo": <root>, "reason": "checkout is not bound to a lifecycle task", "guards": "integration into trunk"}}`. The record has no task. The SHA-256 of `state.json` is unchanged. |

## Substituted boundaries and limits

The sessions are a synthetic recorder, not Codex or Claude, and they are
started by the helper rather than by agent-runtime. The remote is a local bare
repository, so push and network Git are untested; no step pushes. Only the OS
running the helper is proven. A Windows pass says nothing about the Linux
subreaper path, and the reverse also holds. Console signals, a crashed
supervisor and a 0.4 supervisor in the same clone are not exercised. The other
entries in the [feature map](README.md) stay not-run.
