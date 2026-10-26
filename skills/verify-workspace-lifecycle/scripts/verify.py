#!/usr/bin/env python3
"""Install a workspace-lifecycle checkout into a disposable venv, run two sessions
of one task and an unbound checkout through its installed `resolve-run`, and
retain the proof."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import time


TASK = "shared"
UNMANAGED_EXIT = 41
# A session records what the lifecycle gave it, signals readiness and waits for
# the gate, so both sessions are provably alive at the same time.
SESSION = """import json, os, pathlib, sys, time
name, gate, out = sys.argv[1], sys.argv[2], pathlib.Path(sys.argv[3])
record = {'name': name, 'cwd': os.getcwd(), 'task': os.environ.get('WORKSPACE_LIFECYCLE_TASK'),
          'use': os.environ.get('WORKSPACE_LIFECYCLE_USE')}
(out / (name + '.json')).write_text(json.dumps(record), encoding='utf-8')
(out / (name + '.ready')).write_text('ready', encoding='utf-8')
while gate and pathlib.Path(gate).exists():
    time.sleep(0.05)
print(name)
sys.exit(int(sys.argv[4]))
"""
EXPECTATIONS = {
    "install": "The checkout installs into a new venv; installed leases.py and cli.py bytes equal the checkout's and --version equals pyproject.",
    "begin": "`begin --task shared` in a fresh clone exits 0 with ok true and creates the topic worktree.",
    "concurrent-sessions": "Two `resolve-run` sessions in the task worktree are alive together; lease-status lists two uses with distinct tokens and busy true.",
    "mutation-refused": "While both run, `hold --task shared` exits 2 with error `task is in use by another session: shared` and a recovery naming `lease-status --task shared`.",
    "sessions-complete": "Both sessions exit 0, print their name, ran in the worktree with WORKSPACE_LIFECYCLE_TASK=shared and the two listed use tokens.",
    "released": "Afterwards lease-status lists no use, busy false, and `hold` exits 0.",
    "unbound-launch": "`resolve-run` in the primary checkout runs the command unsupervised: exit %d, its stdout, a first stderr line notice "
                      "{lifecycle: unmanaged, reason: checkout is not bound to a lifecycle task, guards: integration into trunk}, "
                      "no task in its environment, and state.json unchanged." % UNMANAGED_EXIT,
}
NOT_RUN = ["held-task launch notice", "detached task worktree", "legacy-registry launch", "crashed use and lease-release",
           "run --task", "finish and retire under shared use", "Windows Job reentry of nested agents",
           "0.4 supervisor coexistence", "console signals", "other OS than the one running the helper"]
SKIP = shutil.ignore_patterns(".git", "build", "dist", "*.egg-info", "__pycache__", ".venv")
DEADLINE = 60  # seconds for two fixture sessions to start; startup takes about one second.


def sha(data):
    return hashlib.sha256(data).hexdigest()


def writable_retry(function, path, _):
    # Git writes object files read-only; Windows refuses to delete them otherwise.
    os.chmod(path, stat.S_IWRITE)
    function(path)


def command(argv, cwd, **kwargs):
    return subprocess.run(argv, cwd=cwd, text=True, encoding="utf-8", errors="replace",
                          capture_output=True, **kwargs)


def doctor(repo):
    if sys.version_info < (3, 14):
        raise ValueError("Python 3.14 or newer is required")
    pyproject = repo / "pyproject.toml"
    if not pyproject.is_file() or 'name = "workspace-lifecycle"' not in pyproject.read_text(encoding="utf-8"):
        raise ValueError("--repo is not a workspace-lifecycle checkout: " + str(repo))
    if shutil.which("git") is None:
        raise ValueError("Git is required")
    revision = command(["git", "rev-parse", "HEAD"], repo)
    status = command(["git", "status", "--porcelain", "--untracked-files=normal"], repo)
    readable = not (revision.returncode or status.returncode)
    inputs = [pyproject, *sorted((repo / "src").rglob("*.py"))]
    return {
        "revision": revision.stdout.strip() if readable else "unknown",
        "dirty": bool(status.stdout.strip()) if readable else "unknown",
        "inputs_sha256": {p.relative_to(repo).as_posix(): sha(p.read_bytes()) for p in inputs},
        "helper_sha256": sha(Path(__file__).read_bytes()),
        "git": command(["git", "--version"], repo).stdout.strip(),
        "python": platform.python_version(), "platform": platform.platform(),
        "entry_point": "installed `workspace-lifecycle` console entry: begin, resolve-run, lease-status, hold",
    }


def run(repo, evidence_root=None, doctor_only=False, agent="unknown"):
    repo = Path(repo).resolve()
    if evidence_root is not None:
        evidence_root = Path(evidence_root).resolve()
        evidence_root.mkdir(parents=True, exist_ok=True)
    evidence = Path(tempfile.mkdtemp(prefix="workspace-lifecycle-evidence-", dir=evidence_root))
    result = {
        "run_id": evidence.name, "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "blocked", "feature": "shared-task-use", "feature_status": "not-run",
        "agent": agent, "expectations": EXPECTATIONS, "commands": [], "assertions": [],
        "artifacts": [], "not_run": NOT_RUN, "cleanup": {"ok": True, "scratch": None},
    }
    scratch = gate = None
    sessions = []

    def record():
        (evidence / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n",
                                              encoding="utf-8")

    def assertion(name, expected, observed):
        ok = observed == expected
        result["assertions"].append({"name": name, "expected": expected, "observed": observed, "ok": ok})
        record()
        if not ok:
            raise AssertionError(name)

    def log(label, argv, cwd, code, stdout, stderr):
        result["commands"].append({"label": label, "argv": argv, "cwd": str(cwd), "exit_code": code,
                                   "stdout": stdout, "stderr": stderr})
        record()

    def execute(label, argv, cwd, **kwargs):
        proc = command(argv, cwd, **kwargs)
        log(label, argv, cwd, proc.returncode, proc.stdout, proc.stderr)
        return proc

    def keep(path, name=None):
        shutil.copy2(path, evidence / (name or path.name))
        result["artifacts"].append(name or path.name)
        record()

    record()  # Expectations exist before the first operation.
    try:
        result["target"] = doctor(repo)
        if doctor_only:
            result["status"] = "not-run"
            return result, evidence
        scratch = Path(tempfile.mkdtemp(prefix="workspace-lifecycle-drive-")).resolve()
        result["cleanup"]["scratch"] = str(scratch)
        source = scratch / "source"
        shutil.copytree(repo, source, ignore=SKIP)
        venv = scratch / "venv"
        if execute("venv", [sys.executable, "-m", "venv", str(venv)], scratch).returncode:
            raise ValueError("venv creation failed")
        bindir = venv / ("Scripts" if os.name == "nt" else "bin")
        suffix = ".exe" if os.name == "nt" else ""
        python = bindir / ("python" + suffix)
        if execute("install", [str(python), "-m", "pip", "install", "--no-deps", "--disable-pip-version-check",
                               "-q", str(source)], scratch).returncode:
            raise ValueError("pip install of the checkout failed (the setuptools build backend download needs network)")
        lifecycle = str(bindir / ("workspace-lifecycle" + suffix))
        env = {k: v for k, v in os.environ.items()
               if k != "PYTHONPATH" and not k.startswith("WORKSPACE_LIFECYCLE_")}
        env["PYTHONIOENCODING"] = "utf-8"

        result["status"] = result["feature_status"] = "fail"
        for name in ("leases.py", "cli.py"):
            installed = command([str(python), "-c", "import workspace_lifecycle.%s as m; print(m.__file__)" % name[:-3]],
                                scratch, env=env)
            assertion("installed %s matches checkout" % name, sha((repo / "src/workspace_lifecycle" / name).read_bytes()),
                      sha(Path(installed.stdout.strip()).read_bytes()) if not installed.returncode else installed.stderr)
        expected_version = next(line.split('"')[1] for line in
                                (repo / "pyproject.toml").read_text(encoding="utf-8").splitlines()
                                if line.startswith("version"))
        assertion("installed version", expected_version,
                  execute("version", [lifecycle, "--version"], scratch, env=env).stdout.strip())

        remote, root, topic = scratch / "remote.git", scratch / "root", scratch / "topic"
        identity = ["-c", "user.name=verify", "-c", "user.email=verify@example.invalid"]
        for label, argv, cwd in (
                ("setup remote", ["git", "init", "-q", "--bare", "--initial-branch=trunk", str(remote)], scratch),
                ("setup clone", ["git", "clone", "-q", str(remote), str(root)], scratch),
                ("setup commit", ["git", *identity, "commit", "-q", "--allow-empty", "-m", "base"], root),
                ("setup push", ["git", "push", "-q", "origin", "trunk"], root)):
            if execute(label, argv, cwd, env=env).returncode:
                raise ValueError(label + " failed")
        begun = execute("begin", [lifecycle, "--repo", str(root), "begin", "--task", TASK,
                                  "--request", "verify/shared-use", "--remote", "origin", "--branch", "topic/" + TASK,
                                  "--worktree", str(topic), "--validation-json", '["git", "diff", "--check"]',
                                  "--preflight-json", '["git", "-C", "{repo}", "status"]'], root, env=env)
        assertion("begin exit code", 0, begun.returncode)
        assertion("begin ok", True, json.loads(begun.stdout)["ok"])
        assertion("topic worktree exists", True, topic.is_dir())

        out = scratch / "sessions"
        out.mkdir()
        gate = scratch / "gate"
        gate.write_text("hold", encoding="utf-8")
        script = scratch / "session.py"
        script.write_text(SESSION, encoding="utf-8")
        keep(script)
        for name in ("first", "second"):
            argv = [lifecycle, "resolve-run", "--cwd", str(topic), "--launch-cwd", str(topic), "--",
                    sys.executable, str(script), name, str(gate), str(out), "0"]
            sessions.append((name, argv, subprocess.Popen(argv, cwd=topic, text=True, encoding="utf-8",
                                                          errors="replace", stdout=subprocess.PIPE,
                                                          stderr=subprocess.PIPE, env=env)))
        deadline = time.monotonic() + DEADLINE
        while not all((out / (name + ".ready")).exists() for name, _, _ in sessions):
            exited = [name for name, _, child in sessions if child.poll() is not None]
            if exited or time.monotonic() > deadline:
                assertion("both sessions alive together", [], exited or ["timeout"])
            time.sleep(0.05)
        assertion("both sessions alive together", [], [name for name, _, child in sessions if child.poll() is not None])

        status = execute("lease-status while running", [lifecycle, "--repo", str(topic), "lease-status", "--task", TASK],
                         topic, env=env)
        lease = json.loads(status.stdout)["lease"]
        tokens = sorted(use["token"] for use in lease["uses"])
        assertion("lease-status busy", True, lease["busy"])
        assertion("lease-status lists two distinct uses", 2, len(set(tokens)))

        held = execute("hold while running", [lifecycle, "--repo", str(topic), "hold", "--task", TASK,
                                              "--reason", "verify", "--next-action", "none"], topic, env=env)
        assertion("hold refused exit code", 2, held.returncode)
        refusal = json.loads(held.stderr)
        assertion("hold refusal error", "task is in use by another session: " + TASK, refusal.get("error"))
        assertion("hold refusal recovery names lease-status", True,
                  "lease-status --task " + TASK in refusal.get("recovery", ""))

        gate.unlink()
        finished = []
        for name, argv, child in sessions:
            stdout, stderr = child.communicate(timeout=DEADLINE)
            log("session " + name, argv, topic, child.returncode, stdout, stderr)
            finished.append((name, child.returncode, stdout.strip()))
        sessions = []
        assertion("sessions exit 0 and print their name", [("first", 0, "first"), ("second", 0, "second")], finished)
        records = [json.loads((out / (name + ".json")).read_text(encoding="utf-8")) for name in ("first", "second")]
        for item in records:
            keep(out / (item["name"] + ".json"), "session-" + item["name"] + ".json")
        assertion("sessions ran in the task worktree", [str(topic)] * 2,
                  [str(Path(item["cwd"]).resolve()) for item in records])
        assertion("sessions received the task", [TASK] * 2, [item["task"] for item in records])
        assertion("sessions hold the listed uses", tokens, sorted(item["use"] for item in records))

        after = json.loads(execute("lease-status after", [lifecycle, "--repo", str(topic), "lease-status",
                                                          "--task", TASK], topic, env=env).stdout)["lease"]
        assertion("no use remains", {"busy": False, "uses": []}, after)
        released = execute("hold after release", [lifecycle, "--repo", str(topic), "hold", "--task", TASK,
                                                  "--reason", "verify", "--next-action", "none"], topic, env=env)
        assertion("hold after release exit code", 0, released.returncode)

        common = Path(command(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], root).stdout.strip())
        state = common / "workspace-lifecycle" / "state.json"
        before = sha(state.read_bytes())
        unbound_argv = [lifecycle, "resolve-run", "--cwd", str(root), "--launch-cwd", str(root), "--",
                        sys.executable, str(script), "unbound", "", str(out), str(UNMANAGED_EXIT)]
        unbound = execute("unbound launch", unbound_argv, root, env=env)
        assertion("unbound exit code", UNMANAGED_EXIT, unbound.returncode)
        assertion("unbound stdout", "unbound", unbound.stdout.strip())
        notice = json.loads(unbound.stderr.splitlines()[0]).get("notice") if unbound.stderr.strip() else None
        assertion("unbound notice", {"lifecycle": "unmanaged", "repo": str(root),
                                     "reason": "checkout is not bound to a lifecycle task",
                                     "guards": "integration into trunk"}, notice)
        unbound_record = json.loads((out / "unbound.json").read_text(encoding="utf-8"))
        keep(out / "unbound.json", "session-unbound.json")
        assertion("unbound command has no task", None, unbound_record["task"])
        assertion("unbound launch leaves state unchanged", before, sha(state.read_bytes()))
        keep(state, "state.json")
        result["status"] = result["feature_status"] = "pass"
    except (OSError, ValueError, AssertionError, KeyError, IndexError, subprocess.TimeoutExpired) as exc:
        result["error"] = type(exc).__name__ + ": " + str(exc)
        # Preparation errors stay blocked; mismatches after Drive stay fail.
    finally:
        if scratch is not None:
            if gate is not None:
                gate.unlink(missing_ok=True)
            for name, argv, child in sessions:
                try:
                    stdout, stderr = child.communicate(timeout=DEADLINE)
                except subprocess.TimeoutExpired:
                    child.kill()  # Only this run's own session process.
                    stdout, stderr = child.communicate()
                log("session " + name, argv, scratch, child.returncode, stdout, stderr)
            try:
                shutil.rmtree(scratch, onerror=writable_retry)
                result["cleanup"]["ok"] = not scratch.exists()
            except OSError as exc:
                result["cleanup"].update(ok=False, error=str(exc))
        if not result["cleanup"]["ok"]:
            result["status"] = "fail"
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        result["evidence_retained"] = all((evidence / item).exists() for item in result["artifacts"])
        if not result["evidence_retained"]:
            result["status"] = "fail"
        record()
    return result, evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True, help="workspace-lifecycle checkout to install and verify")
    parser.add_argument("--evidence-root", type=Path, help="parent of the new evidence directory (default: system temp)")
    parser.add_argument("--doctor-only", action="store_true", help="identify inputs only; feature stays not-run")
    parser.add_argument("--agent", default="unknown", help="driving agent recorded in result.json")
    args = parser.parse_args()
    result, evidence = run(args.repo, args.evidence_root, args.doctor_only, args.agent)
    print(json.dumps({"status": result["status"], "result": str(evidence / "result.json")}, indent=2))
    return {"pass": 0, "fail": 1, "blocked": 2, "not-run": 3}[result["status"]]


if __name__ == "__main__":
    sys.exit(main())
