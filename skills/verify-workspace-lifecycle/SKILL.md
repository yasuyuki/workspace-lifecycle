---
name: verify-workspace-lifecycle
description: Verify that a workspace-lifecycle checkout, installed as a package, lets two sessions of one task run together through `resolve-run`, refuses a mutation while they run, and runs an unbound checkout unsupervised, with disposable data and retained evidence.
license: MIT
---

# Verify workspace-lifecycle

Install the selected checkout into a new venv and drive its installed
`workspace-lifecycle` command the way a runtime launcher and an agent do. The
helper begins a task in a disposable clone, then starts two `resolve-run`
sessions in the task worktree and inspects them with `lease-status` and `hold`.
It also launches from the clone's unbound primary checkout. The sessions are a
synthetic recorder; no agent CLI is used. Read [the feature map](features/README.md)
for what this proves and what it leaves not-run.

Requirements: Python 3.14 with `venv` and `pip`, Git, and network access for
pip to fetch the setuptools build backend. No authentication, remote service or
private configuration is used.

From the selected `workspace-lifecycle` checkout:

```sh
python skills/verify-workspace-lifecycle/scripts/verify.py --repo . \
  --evidence-root /absolute/evidence/parent --agent "<driving agent and model>"
```

`python` is the interpreter you choose to build the venv. On Windows give its
absolute path and write the command on one line. When `dirty` is true, the
revision does not identify what ran; report `inputs_sha256` with it. For a
distributed copy of this skill, run its own `scripts/verify.py` and still pass
the checkout with `--repo`. `--doctor-only` records the target identity and
exits without driving. `--help` is the authority for options.

## What the helper does

1. **Doctor.** Records the checkout revision and dirty state (`unknown` outside
   Git), SHA-256 of `pyproject.toml`, every `src/**/*.py` and the helper, and
   the Git and Python versions. A missing input is `blocked`.
2. **Launch.** Copies the checkout into a new scratch directory, creates a venv
   and runs `pip install --no-deps` from that copy. The checkout itself gets no
   build output. It asserts that the installed `leases.py` and `cli.py` bytes
   and `--version` match the checkout.
3. **Drive.** Creates a bare remote and a clone, and runs `begin` with the
   installed command. It then runs the operations in
   [shared task use](features/shared-task-use.md) with the expectations fixed
   there.
4. **Evidence.** Writes `result.json` before the first operation and after
   every command and assertion. It copies the session recorder, each
   session's record and the final `state.json` beside it. Exit codes alone
   never pass; each step compares JSON output, the recorder's environment and
   the state hash with literal values.
5. **Cleanup.** Ends only the sessions it started, then removes its own scratch
   directory (venv, source copy, remote, clone and worktree), including after a
   failure. It then checks that every listed artifact still exists.

The helper prints `{"status", "result"}`. Exit codes 0, 1, 2 and 3 mean
`pass`, `fail`, `blocked` and `not-run`. Before you report, read
`result.json`. Check `status`, each assertion, `cleanup.ok`,
`evidence_retained` and `not_run`. A failed cleanup or missing artifact is
`fail` even when every assertion held. Each session that needs proof runs the
helper itself; an earlier `result.json` is not current evidence.

`tests/test_verification_skill.py` checks the detector and runs with the
package suite. The checkout passes. A disposable copy that drops the unbound
launch notice but keeps the command's exit code fails. A checkout path that is
not this package is blocked, and Doctor-only is not-run. These tests do not
replace a feature run.
