import hashlib
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from workspace_lifecycle.cli import main
from workspace_lifecycle.errors import LifecycleError
from workspace_lifecycle.service import begin, finish, hold, reclaim, release_hold, retire, retire_pending, status


def run(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()


class AbandonmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); base = Path(self.temp.name)
        self.remote = base / "remote.git"; self.root = base / "root"; self.topic = base / "topic"
        run(base, "init", "--bare", "--initial-branch=trunk", str(self.remote))
        run(base, "clone", str(self.remote), str(self.root))
        run(self.root, "config", "user.name", "Test"); run(self.root, "config", "user.email", "test@example.invalid")
        (self.root / "README").write_text("base\n")
        run(self.root, "add", "README"); run(self.root, "commit", "-m", "base"); run(self.root, "push", "origin", "trunk")
        self.preflight = preflight = [sys.executable, "-m", "workspace_lifecycle.push", "{repo}", "--user-intent", "push"]
        begin(self.root, task="trial", request="i/t", remote="origin", branch="topic/trial", worktree=str(self.topic),
              validation=["git", "diff", "--check"], preflight=preflight)

    def tearDown(self): self.temp.cleanup()

    def remote_refs(self):
        return run(self.root, "ls-remote", str(self.remote))

    def commit_trial(self):
        (self.topic / "trial.txt").write_text("must not reach trunk\n")
        run(self.topic, "add", "trial.txt"); run(self.topic, "commit", "-q", "-m", "trial")
        return run(self.topic, "rev-parse", "HEAD")

    def test_abandoned_commit_retires_and_reclaims_without_reaching_the_remote(self):
        commit = self.commit_trial(); before = self.remote_refs()
        result = retire(self.root, task="trial", result_ref="i/t#decision", users_released=True,
                        abandon="fixture commit must not be integrated", preservation_evidence="i/t#kept")
        self.assertTrue(result["retired"]); self.assertTrue(result["reclaim"]["reclaimed"])
        self.assertFalse(self.topic.exists())
        self.assertEqual(self.remote_refs(), before)
        self.assertEqual(run(self.root, "rev-parse", "trunk"), run(self.root, "rev-parse", "origin/trunk"))
        self.assertEqual(run(self.root, "rev-parse", "topic/trial"), commit)
        receipt = result["receipt"]
        self.assertEqual(receipt["abandoned"], {"reason": "fixture commit must not be integrated", "commit": commit})
        self.assertEqual(receipt["phase"], "reclaimed")
        self.assertFalse(Path(receipt["recovery_path"]).exists())
        self.assertNotIn("trial", json.dumps(status(self.root)["tasks"]))

    def test_unchanged_task_retires_at_its_base(self):
        base = run(self.topic, "rev-parse", "HEAD")
        result = retire(self.root, task="trial", result_ref="i/t#decision", users_released=True, abandon="nothing to keep")
        self.assertEqual(result["receipt"]["abandoned"]["commit"], base)
        self.assertTrue(Path(result["receipt"]["recovery_path"]).is_dir())
        self.assertNotIn("reclaim", result)

    def test_reclaim_holds_an_abandoned_payload_whose_branch_was_deleted(self):
        self.commit_trial()
        result = retire(self.root, task="trial", result_ref="i/t#decision", users_released=True, abandon="unwanted")
        run(self.root, "branch", "-D", "topic/trial")
        with self.assertRaisesRegex(LifecycleError, "no longer kept by its branch"):
            reclaim(self.root, task="trial", result_ref="i/t#decision", preservation_evidence="i/t#kept")
        self.assertTrue(Path(result["receipt"]["recovery_path"]).is_dir())

    def test_held_task_needs_the_hold_released_first(self):
        hold(self.root, "trial", "fixture", "decide whether to keep it")
        with self.assertRaisesRegex(LifecycleError, "release-hold"):
            retire(self.root, task="trial", result_ref="i/t#decision", users_released=True, abandon="unwanted")
        self.assertTrue(self.topic.exists())
        release_hold(self.root, "trial", "i/t#decision")
        self.assertTrue(retire(self.root, task="trial", result_ref="i/t#decision", users_released=True, abandon="unwanted")["retired"])

    def test_dirty_checkout_keeps_the_request_and_finish_is_refused(self):
        commit = self.commit_trial()
        (self.topic / "notes.txt").write_text("unsaved\n")
        with self.assertRaisesRegex(LifecycleError, "dirty"):
            retire(self.root, task="trial", result_ref="i/t#decision", users_released=True, abandon="unwanted")
        self.assertEqual(status(self.root, "trial")["retire"]["abandoned"]["commit"], commit)
        plan = Path(self.temp.name) / "plan.json"; plan.write_text("{}")
        with self.assertRaisesRegex(LifecycleError, "abandoned"):
            finish(self.topic, task="trial", plan_path=str(plan), result_ref="i/t")
        (self.topic / "notes.txt").unlink()
        replay = retire_pending(self.root)["pending"][0]
        self.assertTrue(replay["retired"]); self.assertEqual(replay["receipt"]["abandoned"]["commit"], commit)
        self.assertEqual(self.remote_refs().count("topic/trial"), 0)

    def test_commit_after_the_request_is_not_retired_as_abandoned(self):
        self.commit_trial()
        (self.topic / "notes.txt").write_text("unsaved\n")
        with self.assertRaises(LifecycleError):
            retire(self.root, task="trial", result_ref="i/t#decision", users_released=True, abandon="unwanted")
        run(self.topic, "add", "notes.txt"); run(self.topic, "commit", "-q", "-m", "later")
        with self.assertRaisesRegex(LifecycleError, "branch changed"):
            retire(self.root, task="trial", result_ref="i/t#decision")
        self.assertTrue(self.topic.exists())

    def test_pending_finish_is_completed_or_withdrawn_first(self):
        topic = Path(self.temp.name) / "pending"
        begin(self.root, task="pending", request="i/p", remote="origin", branch="topic/pending", worktree=str(topic),
              validation=[sys.executable, "-c", "raise SystemExit(1)"], preflight=self.preflight)
        (topic / "p.txt").write_text("p\n")
        plan = Path(self.temp.name) / "plan.json"
        plan.write_text(json.dumps({"commit": [{"path": "p.txt", "classification": "source", "owner": "pending",
            "evidence": "fixture", "safe_to_commit": True,
            "sha256": hashlib.sha256((topic / "p.txt").read_bytes()).hexdigest()}]}))
        with self.assertRaises(LifecycleError):
            finish(topic, task="pending", plan_path=str(plan), result_ref="i/p")
        self.assertIn("intent", status(self.root, "pending"))
        with self.assertRaisesRegex(LifecycleError, "pending finish"):
            retire(self.root, task="pending", result_ref="i/p#decision", users_released=True, abandon="unwanted")
        self.assertNotIn("retire", status(self.root, "pending"))

    def test_abandonment_needs_released_users_and_a_reason(self):
        for kwargs in ({"abandon": "unwanted"}, {"abandon": " ", "users_released": True}):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(LifecycleError, "reason and released"):
                retire(self.root, task="trial", result_ref="i/t#decision", **kwargs)
        self.assertNotIn("retire", status(self.root, "trial"))

    def test_cli_abandons_without_merge_or_push(self):
        commit = self.commit_trial(); before = self.remote_refs()
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["--repo", str(self.root), "retire", "--task", "trial", "--result-ref", "i/t#decision",
                         "--users-released", "--abandon", "fixture", "--reclaim-preservation-evidence", "i/t#kept"])
        result = json.loads(output.getvalue())
        self.assertEqual(code, 0); self.assertTrue(result["retired"]); self.assertTrue(result["reclaim"]["reclaimed"])
        self.assertEqual(self.remote_refs(), before)
        self.assertEqual(run(self.root, "rev-parse", "topic/trial"), commit)


if __name__ == "__main__":
    unittest.main()
