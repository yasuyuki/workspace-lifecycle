import json
import re
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from workspace_lifecycle import checks, service
from workspace_lifecycle.errors import LifecycleError
from workspace_lifecycle.adoption import adopt_existing
from workspace_lifecycle.service import begin, finish, status, update_preflight, update_required_checks


def run(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()


TEST = {"app": "github-actions", "name": "test (ubuntu-latest, 3.12)"}
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
        self.real_repository = checks.repository

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
              required_checks=None if required is None else [dict(c) for c in required])
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

    def declare(self, content):
        """Publish a repository declaration on trunk; None removes it."""
        path = self.root / ".github" / "required-checks.json"
        if content is None:
            run(self.root, "rm", "-q", ".github/required-checks.json")
        else:
            path.parent.mkdir(exist_ok=True)
            path.write_text(content if isinstance(content, str) else json.dumps(content))
            run(self.root, "add", ".github/required-checks.json")
        run(self.root, "commit", "-qm", "declare required checks")
        run(self.root, "push", "-q", "origin", "trunk")
        return run(self.root, "rev-parse", "HEAD")

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

    def test_several_push_urls_refuse(self):
        self.start("mirrors")
        run(self.root, "remote", "set-url", "--add", "--push", "origin", str(self.remote))
        run(self.root, "remote", "set-url", "--add", "--push", "origin", str(Path(self.temp.name) / "mirror.git"))
        with self.assertRaisesRegex(LifecycleError, "exactly one push URL"):
            self.real_repository(self.root, "origin")

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

    def counted(self, task):
        log = Path(self.temp.name) / ("validations-" + task)
        self.start(task, validation=[sys.executable, "-c",
            "import os,sys; open(sys.argv[1], 'a').write(os.getcwd() + '\\n')", str(log)])
        self.refused(task, "missing")
        return lambda where: log.read_text().splitlines().count(str(where))

    def test_resume_after_checks_reuses_the_saved_acceptance(self):
        validations = self.counted("resume")
        accepted = status(self.topic, "resume")["acceptance"]
        commit = accepted["commit"]
        self.github.add("example/repo", commit, TEST, "queued", None)
        self.refused("resume", '"state": "queued"')
        self.github.add("example/repo", commit, TEST)
        with patch("workspace_lifecycle.service._push", wraps=service._push) as pushes:
            result = self.finish("resume")
        self.assertEqual(validations(self.topic), 2, "no topic validation after acceptance")
        self.assertEqual([Path(call.args[0]) for call in pushes.call_args_list], [self.root.resolve()])
        self.assertEqual(status(self.topic, "resume")["acceptance"], accepted)
        self.assertEqual(validations(self.root.resolve()), 1)
        self.assertEqual(result["integration"]["commit"], self.trunk())
        self.assertEqual(run(self.root, "rev-parse", "HEAD^2"), commit)

    def accepts_again(self, task, change, refusal="missing"):
        validations = self.counted(task)
        accepted = status(self.topic, task)["acceptance"]
        change()
        self.refused(task, refusal)
        self.assertGreater(validations(self.topic), 2, "the changed finish validated again")
        return accepted

    def test_resume_without_saved_finish_input_accepts_again(self):
        def legacy():
            from workspace_lifecycle.state import locked_state, save_state
            with locked_state(self.topic) as (directory, state):
                del state["tasks"]["legacy"]["acceptance"]["finish_input"]; save_state(directory, state)
        accepted = self.accepts_again("legacy", legacy)
        self.assertNotEqual(status(self.topic, "legacy")["acceptance"]["accepted_at"], accepted["accepted_at"])

    def test_resume_with_another_result_or_plan_accepts_again(self):
        def other_result():
            self.finish = lambda name: finish(self.topic, task=name, plan_path=str(self.plan), result_ref="issue/other")
        self.accepts_again("result", other_result)
        self.assertEqual(status(self.topic, "result")["acceptance"]["result_ref"], "issue/other")
        del self.finish
        self.tearDown(); self.setUp()

        def other_plan():
            plan = json.loads(self.plan.read_text()); plan["commit"][0]["evidence"] = "reviewed again"
            self.plan.write_text(json.dumps(plan))
        self.accepts_again("plan", other_plan)

    def test_resume_with_another_preflight_accepts_again(self):
        changed = self.preflight + ["--user-intent", "push"]
        self.accepts_again("preflight", lambda: update_preflight(
            self.topic, task="preflight", expected_preflight=self.preflight, preflight=changed, evidence="issue/preflight"))

    def test_resume_with_another_push_url_accepts_again(self):
        mirror = Path(self.temp.name) / "mirror.git"
        run(self.root, "clone", "-q", "--bare", str(self.remote), str(mirror))
        self.accepts_again("push-url", lambda: run(self.topic, "remote", "set-url", "--push", "origin", str(mirror)),
                           "PUSH_REPOSITORY_MISMATCH")

    def test_resume_with_unowned_dirt_validates_and_refuses(self):
        self.accepts_again("dirty", lambda: (self.topic / "stray.txt").write_text("unowned\n"), "unresolved dirty")

    def test_resume_with_a_moved_topic_remote_does_not_reuse_acceptance(self):
        validations = self.counted("moved-remote")
        commit = self.accepted("moved-remote")
        self.github.add("example/repo", commit, TEST)
        foreign = run(self.root, "commit-tree", "-p", commit, "-m", "foreign", commit + "^{tree}")
        run(self.root, "push", "-q", "origin", foreign + ":refs/heads/topic/moved-remote")
        self.finish("moved-remote")
        self.assertEqual(validations(self.topic), 3)
        self.assertEqual(status(self.topic, "moved-remote")["acceptance"]["push"]["remote_observed"], foreign)

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

    def test_declared_checks_are_inherited_and_gate_integration(self):
        declared_at = self.declare([TEST, LINT])
        self.start("inherit", required=None)
        record = status(self.topic, "inherit")
        self.assertEqual(record["required_checks"], [TEST, LINT])
        policy = record["required_checks_policy"]
        self.assertEqual((policy["path"], policy["commit"], policy["checks"]),
                         (".github/required-checks.json", declared_at, [TEST, LINT]))
        self.assertEqual(policy["blob"], run(self.root, "rev-parse", declared_at + ":.github/required-checks.json"))
        self.refused("inherit", "missing")
        commit = self.accepted("inherit")
        self.github.add("example/repo", commit, TEST)
        self.github.add("example/repo", commit, LINT, "completed", "failure")
        self.refused("inherit", '"state": "failure"')
        self.github.add("example/repo", commit, LINT)
        self.assertEqual(self.finish("inherit")["integration"]["destination"], "trunk")
        integrated = status(self.topic, "inherit")["integrated"]
        self.assertEqual(integrated["required_checks_policy"]["commit"], declared_at)
        self.assertEqual([c["name"] for c in integrated["remote_checks"]["checks"]], [TEST["name"], LINT["name"]])

    def test_explicit_checks_must_include_the_declaration(self):
        self.declare([TEST])
        for explicit in ([], [LINT]):
            with self.subTest(explicit=explicit), self.assertRaisesRegex(LifecycleError, "omit"):
                self.start("weak", required=explicit)
            self.assertFalse(self.topic.exists())
            self.assertIsNone(run_optional(self.root, "rev-parse", "--verify", "refs/heads/topic/weak"))
        self.start("strong", required=(LINT, TEST))
        self.assertEqual(status(self.topic, "strong")["required_checks"], [LINT, TEST])
        commit = self.accepted_after_refusal("strong")
        self.github.add("example/repo", commit, TEST)
        self.refused("strong", "missing")
        self.github.add("example/repo", commit, LINT)
        self.assertEqual(self.finish("strong")["integration"]["destination"], "trunk")

    def accepted_after_refusal(self, task):
        self.refused(task, "missing")
        return self.accepted(task)

    def test_invalid_or_unreadable_declaration_refuses_begin(self):
        for content, message in (("not json", "invalid"), ('{"app": "x", "name": "y"}', "invalid"),
                                 (json.dumps([TEST, TEST]), "distinct"), ('[{"name": "x"}]', "invalid")):
            with self.subTest(content=content):
                self.declare(content)
                with self.assertRaisesRegex(LifecycleError, message):
                    self.start("bad", required=None)
                with self.assertRaisesRegex(LifecycleError, message):
                    self.start("bad", required=(TEST,))
                self.assertFalse(self.topic.exists())
        self.declare(None)
        link = subprocess.run(["git", "-C", str(self.root), "hash-object", "-w", "--stdin"], input="elsewhere.json",
                              check=True, text=True, capture_output=True).stdout.strip()
        run(self.root, "update-index", "--add", "--cacheinfo", "120000," + link + ",.github/required-checks.json")
        run(self.root, "commit", "-qm", "link"); run(self.root, "push", "-q", "origin", "trunk")
        with self.assertRaisesRegex(LifecycleError, "regular file"):
            self.start("bad", required=None)
        run(self.root, "remote", "set-url", "origin", str(Path(self.temp.name) / "missing.git"))
        with self.assertRaises(LifecycleError):
            self.start("bad", required=None)
        self.assertFalse(self.topic.exists())
        self.assertIsNone(run_optional(self.root, "config", "--get-regexp", "^branch\\..*\\.workspacetask$"))

    def test_task_checkout_cannot_weaken_the_declaration(self):
        self.declare([TEST])
        self.start("local", required=None)
        (self.topic / ".github" / "required-checks.json").write_text("[]")
        run(self.topic, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "commit", "-qam", "weaken in the topic")
        self.refused("local", "missing")
        with self.assertRaisesRegex(LifecycleError, "omit"):
            update_required_checks(self.root, task="local", expected=[TEST], required_checks=[], evidence="issue/33")
        self.assertEqual(status(self.topic, "local")["required_checks"], [TEST])

    def test_declaration_added_after_begin_closes_the_gate_until_updated(self):
        self.start("legacy", required=None)
        self.assertNotIn("required_checks", status(self.topic, "legacy"))
        self.assertIsNone(status(self.topic, "legacy")["required_checks_policy"]["blob"])
        declared_at = self.declare([TEST])
        self.refused("legacy", "omit")
        self.assertEqual(self.github.queries, [])
        with self.assertRaisesRegex(LifecycleError, "omit"):
            update_required_checks(self.root, task="legacy", expected=[], required_checks=[LINT], evidence="issue/33")
        update_required_checks(self.root, task="legacy", expected=[], required_checks=[TEST], evidence="issue/33")
        record = status(self.topic, "legacy")
        self.assertEqual(record["required_checks_policy"]["commit"], declared_at)
        self.assertEqual(record["required_checks_updates"][-1]["policy"]["checks"], [TEST])
        self.github.add("example/repo", self.accepted("legacy"), TEST)
        self.assertEqual(self.finish("legacy")["integration"]["destination"], "trunk")

    def test_removed_declaration_keeps_the_task_requirement_until_updated(self):
        self.declare([TEST])
        self.start("removed", required=None)
        self.declare(None)
        self.refused("removed", "missing")
        update_required_checks(self.root, task="removed", expected=[TEST], required_checks=[], evidence="issue/33")
        self.assertEqual(self.finish("removed")["integration"]["destination"], "trunk")

    def test_child_inherits_the_default_branch_declaration_not_its_parent_branch(self):
        declared_at = self.declare([TEST])
        parent = Path(self.temp.name) / "parent"
        begin(self.root, task="parent", request="issue/parent", remote="origin", branch="topic/parent",
              worktree=str(parent), validation=["git", "diff", "--check"], preflight=self.preflight)
        (parent / ".github" / "required-checks.json").write_text("[]")
        run(parent, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qam", "weaken")
        begin(self.root, task="child", request="issue/child", remote="origin", branch="topic/child",
              worktree=str(self.topic), parent="parent", validation=["git", "diff", "--check"],
              preflight=self.preflight)
        record = status(self.topic, "child")
        self.assertEqual((record["required_checks"], record["required_checks_policy"]["commit"]), ([TEST], declared_at))
        self.assertEqual(run(self.topic, "rev-parse", "HEAD"), run(parent, "rev-parse", "HEAD"))

    def test_adoption_inherits_the_declaration(self):
        self.declare([TEST])
        run(self.root, "worktree", "add", "-q", "-b", "topic/adopt", str(self.topic))
        values = dict(task="adopt", request="issue/adopt", remote="origin", branch="topic/adopt",
                      worktree=str(self.topic), expected_head=run(self.topic, "rev-parse", "HEAD"),
                      evidence="existing checked-out work reviewed", validation=["git", "diff", "--check"],
                      preflight=self.preflight)
        with self.assertRaisesRegex(LifecycleError, "omit"):
            adopt_existing(self.root, required_checks=[], **values)
        adopt_existing(self.root, **values)
        self.assertEqual(status(self.topic, "adopt")["required_checks"], [TEST])


    def propose(self, content):
        """Commit a declaration change in the task topic; None deletes it."""
        path = self.topic / ".github" / "required-checks.json"
        if content is None:
            run(self.topic, "rm", "-q", ".github/required-checks.json")
        else:
            path.parent.mkdir(exist_ok=True)
            path.write_text(content if isinstance(content, str) else json.dumps(content))
            run(self.topic, "add", ".github/required-checks.json")
        run(self.topic, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "commit", "-qm", "propose required checks")

    def test_declaration_added_by_the_task_gates_its_own_commit(self):
        self.start("introduce", required=None)
        self.propose([TEST])
        self.refused("introduce", "missing")
        self.assertNotIn("required_checks", status(self.topic, "introduce"))
        commit = self.accepted("introduce")
        self.github.add("example/repo", run(self.root, "rev-parse", "trunk"), TEST)
        self.github.add("example/repo", commit, TEST, app="other-app")
        self.refused("introduce", "missing")
        for status_, conclusion in (("in_progress", None), ("completed", "skipped"), ("completed", "failure")):
            with self.subTest(status=status_, conclusion=conclusion):
                self.github.add("example/repo", commit, TEST, status_, conclusion)
                self.refused("introduce", '"state": "' + (conclusion or status_) + '"')
        self.github.add("example/repo", commit, TEST)
        self.assertEqual(self.finish("introduce")["integration"]["destination"], "trunk")
        integrated = status(self.topic, "introduce")["integrated"]
        proposed = integrated["source_checks_policy"]
        self.assertEqual((proposed["commit"], proposed["checks"], proposed["added"], proposed["base"]["blob"]),
                         (commit, [TEST], [TEST], None))
        self.assertEqual(proposed["blob"], run(self.root, "rev-parse", commit + ":.github/required-checks.json"))
        self.assertEqual([(c["name"], c["state"]) for c in integrated["remote_checks"]["checks"]],
                         [(TEST["name"], "success")])

    def test_interrupted_merge_keeps_the_added_declaration_proof(self):
        marker = Path(self.temp.name) / "block-target-validation"
        validation = [sys.executable, "-c",
                      "import os,sys; sys.exit(os.path.exists(sys.argv[1]) and os.getcwd() == sys.argv[2])",
                      str(marker), str(self.root.resolve())]
        self.start("resume-added", required=None, validation=validation)
        self.propose([TEST])
        self.refused("resume-added", "missing")
        self.github.add("example/repo", self.accepted("resume-added"), TEST)
        marker.write_text("")
        with self.assertRaisesRegex(LifecycleError, "configured validation failed"):
            self.finish("resume-added")
        self.assertEqual(status(self.topic, "resume-added")["intent"]["source_checks_policy"]["added"], [TEST])
        marker.unlink()
        self.github.unavailable = True
        self.assertEqual(self.finish("resume-added")["integration"]["destination"], "trunk")
        integrated = status(self.topic, "resume-added")["integrated"]
        self.assertEqual(integrated["source_checks_policy"]["added"], [TEST])
        self.assertEqual([c["name"] for c in integrated["remote_checks"]["checks"]], [TEST["name"]])

    def test_success_before_a_new_head_does_not_cover_the_added_declaration(self):
        self.start("stale", required=None)
        self.propose([TEST])
        self.refused("stale", "missing")
        self.github.add("example/repo", self.accepted("stale"), TEST)
        self.propose([TEST, LINT])
        self.plan.write_text("{}")
        self.refused("stale", "missing")

    def test_changed_declaration_requires_new_checks_without_dropping_old_ones(self):
        self.declare([TEST])
        self.start("rename", required=None)
        self.propose([LINT])
        self.refused("rename", "missing")
        commit = self.accepted("rename")
        self.github.add("example/repo", commit, TEST)
        self.refused("rename", re.escape(LINT["name"]))
        self.github.runs.clear()
        self.github.add("example/repo", commit, LINT)
        self.refused("rename", re.escape(TEST["name"]))
        self.github.add("example/repo", commit, TEST)
        self.assertEqual(self.finish("rename")["integration"]["destination"], "trunk")
        integrated = status(self.topic, "rename")["integrated"]
        self.assertEqual(integrated["source_checks_policy"]["added"], [LINT])
        self.assertEqual([c["name"] for c in integrated["remote_checks"]["checks"]], [TEST["name"], LINT["name"]])

    def test_removed_declaration_in_the_task_keeps_the_declared_checks(self):
        self.declare([TEST])
        self.start("drop", required=None)
        self.propose(None)
        self.refused("drop", "missing")
        self.github.add("example/repo", self.accepted("drop"), TEST)
        self.assertEqual(self.finish("drop")["integration"]["destination"], "trunk")
        proposed = status(self.topic, "drop")["integrated"]["source_checks_policy"]
        self.assertEqual((proposed["blob"], proposed["added"]), (None, []))

    def test_broken_declaration_in_the_task_refuses_integration(self):
        self.start("broken", required=None)
        self.propose("not json")
        self.refused("broken", "invalid")
        self.propose(json.dumps([TEST, TEST]))
        self.plan.write_text("{}")
        self.refused("broken", "distinct")
        link = subprocess.run(["git", "-C", str(self.topic), "hash-object", "-w", "--stdin"], input="elsewhere.json",
                              check=True, text=True, capture_output=True).stdout.strip()
        run(self.topic, "update-index", "--cacheinfo", "120000," + link + ",.github/required-checks.json")
        run(self.topic, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "link")
        run(self.topic, "checkout", "--", ".github/required-checks.json")
        self.refused("broken", "regular file")
        self.assertEqual(self.github.queries, [])

def run_optional(cwd, *args):
    result = subprocess.run(["git", "-C", str(cwd), *args], text=True, capture_output=True)
    return result.stdout.strip() if result.returncode == 0 else None


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
