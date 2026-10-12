# Feature map

| Feature | User entry point | State in this verifier |
| --- | --- | --- |
| [Shared task use](shared-task-use.md) | Installed `workspace-lifecycle resolve-run` in a task worktree and in an unbound primary checkout, with `lease-status` and `hold` | Driven by `scripts/verify.py` |
| Held task launch | `resolve-run` in a held task, which starts with a hold notice while `finish` stays refused | not-run. Covered by `tests/test_dirty.py`. |
| Detached task worktree | `resolve-run` in a task worktree whose branch is detached, which runs unmanaged | not-run. Covered by `tests/test_dirty.py`. |
| Legacy registry launch | `resolve-run` in a repository with `agent-branches/state.json` | not-run. Covered by `tests/test_dirty.py`. |
| Crashed use recovery | A killed supervisor's receipt, then `lease-release --task --token --evidence` | not-run. Covered by `tests/test_leases.py`. |
| Mutation inside a sole use | `finish` or `hold` from inside the task's only session | not-run. Covered by `tests/test_leases.py` and `tests/test_dirty.py`. |
| Lifecycle flow | `begin`, `finish`, `retire`, `reclaim`, `sync` and the other subcommands | not-run |

A row marked not-run has no evidence from this verifier. Add a profile
for one when a change to it needs proof through its own user entry point.
