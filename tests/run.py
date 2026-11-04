"""Run this checkout's tests in parallel against its own source, outside the checkout.

The tracked and untracked, non-ignored files are copied to a temporary directory
whose disposable virtual environment imports that copy, or installs --wheel.
Each test runs in its own process. The checkout receives no bytecode or build
output, and inherited lifecycle bindings do not reach the tests.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

ROOT = Path(__file__).resolve().parents[1]

COLLECT = r"""
import json, sys, unittest
loader = unittest.TestLoader()
suite = loader.loadTestsFromNames(sys.argv[1:]) if sys.argv[1:] else loader.discover(".", pattern="test*.py")
def flatten(test):
    if isinstance(test, unittest.TestSuite):
        for child in test: yield from flatten(child)
    else: yield test.id()
print(json.dumps({"ids": list(flatten(suite)), "errors": loader.errors}))
"""

RUN = r"""
import json, sys, unittest
result = unittest.TextTestRunner(stream=sys.stderr, verbosity=2).run(
    unittest.defaultTestLoader.loadTestsFromName(sys.argv[1]))
print(json.dumps({"run": result.testsRun, "ok": result.wasSuccessful(), "skipped": len(result.skipped),
                  "expected_failures": len(result.expectedFailures)}))
"""


def test_environment():
    environment = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith("WORKSPACE_LIFECYCLE_")
                   and key.upper() not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}}
    # Commits, merges and fetches in the disposable repositories would each start
    # an automatic maintenance process; no test exercises it.
    count = int(environment.get("GIT_CONFIG_COUNT") or 0)
    environment.update({"GIT_CONFIG_COUNT": str(count + 1), f"GIT_CONFIG_KEY_{count}": "maintenance.auto",
                        f"GIT_CONFIG_VALUE_{count}": "false"})
    if os.name == "nt":
        # Git for Windows' cmd\git.exe relaunches the real git.exe for every call,
        # and the tests make tens of thousands of calls. The identical git.exe in
        # libexec\git-core made the tests 1.75 times slower than the launcher (#63).
        exec_path = Path(subprocess.run(["git", "--exec-path"], check=True, text=True,
                                        stdout=subprocess.PIPE).stdout.strip())
        bin_dir = exec_path.parents[1] / "bin"
        if not (bin_dir / "git.exe").is_file():
            sys.exit(f"git.exe not found in {bin_dir}, the bin of git --exec-path {exec_path}")
        environment["PATH"] = os.pathsep.join([str(bin_dir), environment.get("PATH", "")])
    return environment


def source_files():
    listed = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", "--cached", "--others",
                             "--exclude-standard"], check=True, stdout=subprocess.PIPE).stdout
    return [name for name in listed.decode().split("\0") if name and (ROOT / name).is_file()]


def prepare(base, wheel, environment):
    source = base / "source"
    for name in source_files():
        (source / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, source / name)
    runtime = base / "runtime"
    subprocess.run([sys.executable, "-m", "venv", *([] if wheel else ["--without-pip"]), str(runtime)],
                   check=True, env=environment)
    python = runtime / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if wheel:
        subprocess.run([str(python), "-m", "pip", "install", "--no-index", "--quiet", str(wheel)],
                       check=True, env=environment)
        subprocess.run([str(python.with_name("workspace-lifecycle")), "--version"], check=True, env=environment)
    else:
        purelib = subprocess.run([str(python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
                                 check=True, text=True, stdout=subprocess.PIPE, env=environment).stdout.strip()
        (Path(purelib) / "workspace_lifecycle_source.pth").write_text(str(source / "src") + "\n")
    return python, source / "tests"


def run_one(python, tests, environment, test_id):
    started = time.perf_counter()
    completed = subprocess.run([str(python), "-c", RUN, test_id], cwd=tests, env=environment,
                               text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        outcome = json.loads(completed.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        outcome = {"run": 0, "ok": False, "skipped": 0, "expected_failures": 0}
    outcome["ok"] = outcome["ok"] and outcome["run"] == 1 and completed.returncode == 0
    outcome.update(id=test_id, seconds=time.perf_counter() - started,
                   output=completed.stdout + completed.stderr)
    return outcome


def load_timings(path):
    try:
        timings = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"--timings {path}: {error}") from None
    if not (isinstance(timings, dict) and all(
            isinstance(seconds, (int, float)) and not isinstance(seconds, bool) and seconds >= 0
            for seconds in timings.values())):
        raise ValueError(f"--timings {path} must map test ids to non-negative seconds")
    return timings


def shards(ids, count, timings):
    # An untimed test weighs as much as the slowest one, so a new test starts early
    # instead of extending a shard's tail.
    unknown = max(timings.values(), default=1.0)
    weight = lambda test_id: timings.get(test_id, unknown)
    loads, assigned = [0.0] * count, [[] for _ in range(count)]
    for test_id in sorted(ids, key=lambda test_id: (-weight(test_id), test_id)):
        lightest = min(range(count), key=lambda shard: (loads[shard], shard))
        assigned[lightest].append(test_id)
        loads[lightest] += weight(test_id)
    return assigned


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("names", nargs="*", help="unittest names relative to tests/; default: discover all")
    parser.add_argument("-j", "--jobs", type=int, default=min(8, os.cpu_count() or 1),
                        help="concurrent test processes (default: %(default)s)")
    parser.add_argument("--wheel", type=Path, help="install this wheel instead of importing the copied src")
    parser.add_argument("--shard", metavar="K/N", default="1/1",
                        help="run the Kth of N shards that together run the selection once (default: %(default)s)")
    parser.add_argument("--timings", type=Path, help="JSON of test id to seconds that balances shards and order")
    parser.add_argument("--report", type=Path, help="write the seconds of each test run here as JSON")
    arguments = parser.parse_args(argv)
    if arguments.wheel and not arguments.wheel.is_file():
        parser.error(f"--wheel is not a file: {arguments.wheel}")
    try:
        timings = load_timings(arguments.timings) if arguments.timings else {}
    except ValueError as error:
        parser.error(str(error))
    index, _, count = arguments.shard.partition("/")
    if not (index.isdigit() and count.isdigit() and 1 <= int(index) <= int(count)):
        parser.error(f"--shard must be K/N with 1 <= K <= N: {arguments.shard}")
    index, count = int(index), int(count)
    started = time.perf_counter()
    environment = test_environment()
    with tempfile.TemporaryDirectory(prefix="lifecycle-tests-", ignore_cleanup_errors=True) as base:
        python, tests = prepare(Path(base), arguments.wheel and arguments.wheel.resolve(), environment)
        collected = subprocess.run([str(python), "-c", COLLECT, *arguments.names], cwd=tests, env=environment,
                                   text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if collected.returncode:
            sys.stderr.write(collected.stdout + collected.stderr); return 1
        listing = json.loads(collected.stdout)
        selected = shards(listing["ids"], count, timings)[index - 1]
        if listing["errors"] or not selected:
            sys.stderr.write("".join(listing["errors"]) or "no tests collected\n"); return 1
        with ThreadPoolExecutor(max(1, arguments.jobs)) as pool:
            outcomes = list(pool.map(lambda test_id: run_one(python, tests, environment, test_id), selected))
    if arguments.report:
        arguments.report.write_text(json.dumps(
            {outcome["id"]: round(outcome["seconds"], 1) for outcome in outcomes}, indent=1, sort_keys=True) + "\n",
            encoding="utf-8", newline="\n")
    failed = [outcome for outcome in outcomes if not outcome["ok"]]
    for outcome in failed:
        sys.stderr.write(f"FAIL {outcome['id']}\n{outcome['output']}\n")
    slowest = max(outcomes, key=lambda outcome: outcome["seconds"])
    print(f"Ran {len(outcomes)} of {len(listing['ids'])} tests (shard {index}/{count}) in {time.perf_counter() - started:.1f}s with {arguments.jobs} jobs: "
          f"{len(outcomes) - len(failed)} passed, {len(failed)} failed, "
          f"skipped={sum(outcome['skipped'] for outcome in outcomes)}, "
          f"expected failures={sum(outcome['expected_failures'] for outcome in outcomes)}; "
          f"slowest {slowest['id']} {slowest['seconds']:.1f}s")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
