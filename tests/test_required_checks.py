import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from workspace_lifecycle import checks
from workspace_lifecycle.errors import LifecycleError
from workspace_lifecycle.service import begin, finish, status, update_required_checks


def run(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()


TEST = {"app": "github-actions", "name": "test (ubuntu-latest, 3.10)"}
LINT = {"app": "github-actions", "name": "lint"}


class FakeGitHub:
    """Check runs by (repository, commit, name), as GitHub would list them."""

    def __init__(self):
        self.runs = {}; self.queries = []; self.unavailable = False

    def add(self, repository, commit, check, status="completed", conclusion="success", app=None):
        rows = self.runs.setdefault((repository, commit, check["name"]), [])
        rows.append({"id": 1000 + sum(map(len, self.runs.values())), "name": check["name"],
                     "head_sha": commit, "status": status,
                     "conclusion": conclusion if status == "completed" else None,
                     "app": app or check["app"]})

    def __call__(self, repository, commit, name):
        self.queries.append((repository, commit, name))
        if self.unavailable:
            raise LifecycleError("required check status unavailable: HTTP 502")
        return list(self.runs.get((repository, commit, name), []))


class RequiredChecksTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); base = Path(self.temp.name)
        self.remote = base / "remote.git"; self.root = base / "root"; self.topic = base / "topic"
        run(base, "init", "--bare", "--initial-branch=trunk", str(self.remote))
        run(base, "clone", str(self.remote), str(self.root))
        run(self.root, "config", "user.name", "Test"); run(self.root, "config", "user.email", "test@example.invalid")
        (self.root / "README").write_text("base\n")
        run(self.root, "add", "README"); run(self.root, "commit", "-m", "base"); run(self.root, "push", "origin", "trunk")
        self.preflight = [sys.executable, "-m", "workspace_lifecycle.push", "{repo}", "--user-intent", "push"]
        self.github = FakeGitHub()
        self.names = {str(self.remote): "example/repo"}
        real_git = checks.git

        def repository(repo, remote):
            url = real_git(repo, "remote", "get-url", "--push", remote)
            if url not in self.names:
                raise LifecycleError("required checks need a GitHub remote; push URL is " + json.dumps(url))
            return self.names[url]

        for target, value in (("workspace_lifecycle.checks._runs", self.github),
                              ("workspace_lifecycle.checks.repository", repository)):
            patcher = patch(target, value); patcher.start(); self.addCleanup(patcher.stop)

    def tearDown(self): self.temp.cleanup()

    def start(self, task, required=(TEST,), validation=("git", "diff", "--check")):
        begin(self.root, task=task, request="issue/" + task, remote="origin", branch="topic/" + task,
              worktree=str(self.topic), validation=list(validation), preflight=self.preflight,
              required_checks=[dict(c) for c in required])
        (self.topic / "feature.txt").write_text(task + "\n")
        import hashlib
        self.plan = Path(self.temp.name) / ("plan-" + task + ".json")
        self.plan.write_text(json.dumps({"commit": [{
            "path": "feature.txt", "classification": "source", "owner": task,
            "evidence": "created by this disposable task", "safe_to_commit": True,
            "sha256": hashlib.sha256((self.topic / "feature.txt").read_bytes()).hexdigest()}]}))

    def finish(self, task):
        return finish(self.topic, task=task, plan_path=str(self.plan), result_ref="issue/" + task)

    def trunk(self):
        return run(self.root, "ls-remote", str(self.remote), "refs/heads/trunk").split()[0]

    def refused(self, task, message):
        before = self.trunk()
        with self.assertRaisesRegex(LifecycleError, message):
            self.finish(task)
        self.assertEqual(self.trunk(), before)
        self.assertFalse((self.root / "feature.txt").exists())
        self.assertNotIn("integrated", status(self.topic, task))

    def accepted(self, task):
        return status(self.topic, task)["acceptance"]["commit"]

    def test_repository_without_required_checks_integrates_without_querying(self):
        self.start("plain", required=())
        result = self.finish("plain")
        self.assertEqual(result["integration"]["destination"], "trunk")
        self.assertEqual(self.github.queries, [])
        self.assertNotIn("required_checks", status(self.topic, "plain"))

    def test_unsucceeded_checks_refuse_integration_until_success(self):
        self.start("gate", required=(TEST, LINT))
        self.refused("gate", "missing")
        commit = self.accepted("gate")
        self.assertEqual(self.github.queries[0], ("example/repo", commit, TEST["name"]))
        self.github.add("example/repo", commit, LINT)
        for status_, conclusion in (("queued", None), ("in_progress", None), ("completed", "failure"),
                                    ("completed", "cancelled"), ("completed", "skipped"),
                                    ("completed", "neutral"), ("completed", "timed_out"),
                                    ("completed", "action_required")):
            with self.subTest(status=status_, conclusion=conclusion):
                self.github.add("example/repo", commit, TEST, status_, conclusion)
                self.refused("gate", '"state": "' + (conclusion or status_) + '"')
        self.github.add("example/repo", commit, TEST)
        result = self.finish("gate")
        self.assertEqual(result["integration"]["destination"], "trunk")
        self.assertEqual((self.root / "feature.txt").read_text(), "gate\n")
        proof = status(self.topic, "gate")["integrated"]["remote_checks"]
        self.assertEqual(proof["repository"], "example/repo")
        self.assertEqual(proof["commit"], commit)
        self.assertEqual([(c["name"], c["state"]) for c in proof["checks"]],
                         [(TEST["name"], "success"), (LINT["name"], "success")])

    def test_success_on_another_commit_or_app_does_not_count(self):
        self.start("exact")
        self.refused("exact", "missing")
        base = run(self.root, "rev-parse", "trunk")
        self.github.add("example/repo", base, TEST)
        self.github.add("example/repo", self.accepted("exact"), TEST, app="other-app")
        self.refused("exact", "missing")

    def test_only_the_latest_run_counts(self):
        self.start("latest")
        self.refused("latest", "missing")
        commit = self.accepted("latest")
        self.github.add("example/repo", commit, TEST)
        self.github.add("example/repo", commit, TEST, "completed", "failure")
        self.refused("latest", '"state": "failure"')
        self.github.add("example/repo", commit, TEST)
        self.assertEqual(self.finish("latest")["integration"]["destination"], "trunk")

    def test_unavailable_status_or_foreign_response_refuses(self):
        self.start("unavailable")
        self.github.unavailable = True
        self.refused("unavailable", "unavailable")
        self.github.unavailable = False
        commit = self.accepted("unavailable")
        self.github.add("example/repo", commit, TEST)
        self.github.runs[("example/repo", commit, TEST["name"])][0]["head_sha"] = "0" * 40
        self.refused("unavailable", "not bound to the requested commit")

    def test_non_github_remote_refuses(self):
        self.start("foreign")
        del self.names[str(self.remote)]
        self.refused("foreign", "GitHub remote")

    def test_new_head_requires_its_own_checks(self):
        self.start("moved")
        self.refused("moved", "missing")
        first = self.accepted("moved")
        self.github.add("example/repo", first, TEST)
        (self.topic / "feature.txt").write_text("changed\n")
        run(self.topic, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "commit", "-qam", "change after checks")
        self.plan.write_text("{}")
        self.refused("moved", "missing")
        self.assertNotEqual(self.accepted("moved"), first)

    def test_interrupted_merge_keeps_its_proof_and_rejects_a_changed_remote(self):
        marker = Path(self.temp.name) / "block-target-validation"
        validation = [sys.executable, "-c",
                      "import os,sys; sys.exit(os.path.exists(sys.argv[1]) and os.getcwd() == sys.argv[2])",
                      str(marker), str(self.root.resolve())]
        self.start("resume", validation=validation)
        self.refused("resume", "missing")
        commit = self.accepted("resume")
        self.github.add("example/repo", commit, TEST)
        marker.write_text("")
        with self.assertRaisesRegex(LifecycleError, "configured validation failed"):
            self.finish("resume")
        intent = status(self.topic, "resume")["intent"]
        self.assertEqual((intent["kind"], intent["remote_checks"]["commit"]), ("merge", commit))
        marker.unlink()
        fork = Path(self.temp.name) / "fork.git"
        run(self.temp.name, "clone", "--bare", "-q", str(self.remote), str(fork))
        self.names[str(fork)] = "example/fork"
        run(self.root, "remote", "set-url", "origin", str(fork))
        with self.assertRaisesRegex(LifecycleError, "exact accepted commit and remote"):
            self.finish("resume")
        run(self.root, "remote", "set-url", "origin", str(self.remote))
        self.github.unavailable = True
        result = self.finish("resume")
        self.assertEqual(result["integration"]["destination"], "trunk")
        self.assertEqual(status(self.topic, "resume")["integrated"]["remote_checks"]["commit"], commit)

    def test_child_with_dependency_is_gated_before_its_parent_edge(self):
        parent = Path(self.temp.name) / "parent"
        begin(self.root, task="parent", request="issue/parent", remote="origin", branch="topic/parent",
              worktree=str(parent), validation=["git", "diff", "--check"], preflight=self.preflight)
        (parent / "parent.txt").write_text("parent\n")
        import hashlib
        parent_plan = Path(self.temp.name) / "plan-parent.json"
        parent_plan.write_text(json.dumps({"commit": [{
            "path": "parent.txt", "classification": "source", "owner": "parent",
            "evidence": "created by this disposable task", "safe_to_commit": True,
            "sha256": hashlib.sha256((parent / "parent.txt").read_bytes()).hexdigest()}]}))
        finish(parent, task="parent", plan_path=str(parent_plan), result_ref="issue/parent")
        begin(self.root, task="child", request="issue/child", remote="origin", branch="topic/child",
              worktree=str(self.topic), parent="parent", dependencies=["parent"],
              validation=["git", "diff", "--check"], preflight=self.preflight, required_checks=[TEST])
        (self.topic / "child.txt").write_text("child\n")
        self.plan = Path(self.temp.name) / "plan-child.json"
        self.plan.write_text(json.dumps({"commit": [{
            "path": "child.txt", "classification": "source", "owner": "child",
            "evidence": "created by this disposable task", "safe_to_commit": True,
            "sha256": hashlib.sha256((self.topic / "child.txt").read_bytes()).hexdigest()}]}))
        parent_head = run(parent, "rev-parse", "HEAD")
        with self.assertRaisesRegex(LifecycleError, "missing"):
            self.finish("child")
        self.assertEqual(run(parent, "rev-parse", "HEAD"), parent_head)
        self.github.add("example/repo", self.accepted("child"), TEST)
        result = self.finish("child")
        self.assertEqual(result["integration"]["parent"]["destination"], "trunk")
        self.assertEqual((self.root / "child.txt").read_text(), "child\n")

    def test_update_requires_exact_previous_checks_and_no_pending_operation(self):
        self.start("update", required=())
        with self.assertRaisesRegex(LifecycleError, "CAS mismatch"):
            update_required_checks(self.root, task="update", expected=[TEST], required_checks=[TEST], evidence="issue/29")
        with self.assertRaisesRegex(LifecycleError, "durable evidence"):
            update_required_checks(self.root, task="update", expected=[], required_checks=[TEST], evidence=" ")
        update_required_checks(self.root, task="update", expected=[], required_checks=[TEST], evidence="issue/29")
        record = status(self.topic, "update")
        self.assertEqual(record["required_checks"], [TEST])
        self.assertEqual(record["required_checks_updates"][-1]["evidence"], "issue/29")
        self.refused("update", "missing")
        update_required_checks(self.root, task="update", expected=[TEST], required_checks=[], evidence="issue/29")
        self.assertEqual(self.finish("update")["integration"]["destination"], "trunk")

    def test_update_refuses_pending_operation(self):
        marker = Path(self.temp.name) / "block"
        marker.write_text("")
        validation = [sys.executable, "-c",
                      "import os,sys; sys.exit(os.path.exists(sys.argv[1]) and os.getcwd() == sys.argv[2])",
                      str(marker), str(self.root.resolve())]
        self.start("pending", required=(), validation=validation)
        with self.assertRaises(LifecycleError):
            self.finish("pending")
        with self.assertRaisesRegex(LifecycleError, "pending task operation"):
            update_required_checks(self.root, task="pending", expected=[], required_checks=[TEST], evidence="issue/29")

    def test_invalid_required_checks_are_refused_at_begin(self):
        for value in ([{"name": "test"}], [TEST, dict(TEST)], [{"app": "github-actions", "name": " "}], {"app": "x"}):
            with self.subTest(value=value), self.assertRaises(LifecycleError):
                begin(self.root, task="bad", request="issue/bad", remote="origin", branch="topic/bad",
                      worktree=str(self.topic), validation=["git", "diff", "--check"],
                      preflight=self.preflight, required_checks=value)
        self.assertFalse(self.topic.exists())


class RepositoryIdentityTest(unittest.TestCase):
    def test_github_push_urls(self):
        for url in ("https://github.com/yasuyuki/agent-rules.git", "https://github.com/yasuyuki/agent-rules",
                    "git@github.com:yasuyuki/agent-rules.git", "ssh://git@github.com/yasuyuki/agent-rules.git"):
            with self.subTest(url=url):
                self.assertEqual(checks.github_repository(url), "yasuyuki/agent-rules")
        for url in ("https://example.com/yasuyuki/agent-rules.git", "/srv/git/agent-rules.git",
                    "https://github.com/yasuyuki", "https://github.com/a/b/c"):
            with self.subTest(url=url), self.assertRaises(LifecycleError):
                checks.github_repository(url)

    def test_gh_failure_or_unreadable_output_is_unavailable(self):
        for returncode, stdout in ((1, ""), (0, "not json\n")):
            completed = subprocess.CompletedProcess([], returncode, stdout, "HTTP 502")
            with self.subTest(returncode=returncode), patch("workspace_lifecycle.checks.subprocess.run", return_value=completed), \
                    self.assertRaisesRegex(LifecycleError, "unavailable"):
                checks._runs("example/repo", "a" * 40, "test")
        with patch("workspace_lifecycle.checks.subprocess.run", side_effect=FileNotFoundError("gh")), \
                self.assertRaisesRegex(LifecycleError, "unavailable"):
            checks._runs("example/repo", "a" * 40, "test")


if __name__ == "__main__":
    unittest.main()
