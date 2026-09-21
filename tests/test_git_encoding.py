"""Git's UTF-8 protocol must not depend on the process text locale."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from workspace_lifecycle import git, leases, push, service


class GitEncodingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "日本語 root"
        self.remote = self.base / "日本語 remote.git"
        self.topic = self.base / "日本語 topic"
        self.native("init", "--bare", "--initial-branch=日本語-main", str(self.remote))
        self.native("clone", str(self.remote), str(self.root))
        self.native("config", "user.name", "Test", repo=self.root)
        self.native("config", "user.email", "test@example.invalid", repo=self.root)
        (self.root / "README").write_text("base\n", encoding="utf-8")
        self.native("add", "README", repo=self.root)
        self.native("commit", "-m", "base", repo=self.root)
        self.native("push", "origin", "HEAD", repo=self.root)

    def native(self, *args, repo=None):
        return subprocess.run(["git", "-C", str(repo or self.base), *args],
                              encoding="utf-8", capture_output=True, check=True).stdout.strip()

    def test_git_readers_ignore_cp932_default(self):
        # Simulate the affected locale even on UTF-8 CI hosts.
        native_run = subprocess.run

        def cp932_run(argv, **kwargs):
            if kwargs.get("text") and kwargs.get("encoding") is None:
                kwargs["encoding"] = "cp932"
            return native_run(argv, **kwargs)

        with patch.object(subprocess, "run", side_effect=cp932_run):
            self.assertEqual(git.top(self.root), self.root)
            self.assertEqual(git.current_branch(self.root), "日本語-main")
            self.assertEqual(push.run_git(self.root, "branch", "--show-current"), "日本語-main")
            self.assertEqual(push.default_from_ls_remote(self.root, "origin"), "日本語-main")
            lock, _ = leases._paths(self.root, "encoding")
            self.assertTrue(lock.is_relative_to(self.root / ".git"))
            service._push(self.root, [sys.executable, "-m", "workspace_lifecycle.push",
                                      "{repo}", "--user-intent", "push"], "日本語-main", "origin")

    def test_japanese_managed_root_advertised_finish_and_unmanaged(self):
        environment = os.environ.copy()
        # No UTF-8 mode workaround may be inherited by the advertised finish child.
        environment.pop("PYTHONUTF8", None)
        command = [sys.executable, "-X", "utf8=0", "-m", "workspace_lifecycle"]
        unmanaged = subprocess.run(
            [*command, "resolve-run", "--cwd", str(self.root),
             "--launch-cwd", str(self.root), "--", sys.executable, "-c",
             "raise SystemExit(23)"], env=environment, capture_output=True)
        self.assertEqual(unmanaged.returncode, 23, unmanaged.stderr)
        self.assertFalse((self.root / ".git" / "workspace-lifecycle").exists())
        preflight = [sys.executable, "-m", "workspace_lifecycle.push", "{repo}",
                     "--user-intent", "push"]
        begun = subprocess.run(
            [*command, "--repo", str(self.root), "begin", "--task", "encoding",
             "--request", "Japanese Git path regression", "--remote", "origin",
             "--branch", "topic/日本語", "--worktree", str(self.topic),
             "--validation-json", json.dumps(["git", "diff", "--check"]),
             "--preflight-json", json.dumps(preflight)],
            env=environment, capture_output=True)
        self.assertEqual(begun.returncode, 0, begun.stderr)
        child = self.base / "finish_child.py"
        child.write_text(
            "import hashlib,json,os,pathlib,subprocess\n"
            "c=json.loads(os.environ['WORKSPACE_LIFECYCLE_CONTEXT'])\n"
            "r=pathlib.Path(c['repo']); p=r/'feature.txt'; p.write_bytes(b'done\\n')\n"
            "plan=r.parent/'plan.json'\n"
            "plan.write_text(json.dumps({'commit':[{'path':'feature.txt',"
            "'classification':'source','owner':c['task'],'evidence':'test-owned source',"
            "'safe_to_commit':True,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}]}),"
            "encoding='utf-8')\n"
            "subprocess.run([*c['finish_argv'],'--plan',str(plan),"
            "'--result-ref','test/encoding'],cwd=r,check=True)\n",
            encoding="utf-8")
        ran = subprocess.run(
            [*command, "resolve-run", "--cwd", str(self.topic),
             "--launch-cwd", str(self.topic), "--", sys.executable, str(child)],
            env=environment, capture_output=True)
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertEqual((self.root / "feature.txt").read_text(encoding="utf-8"), "done\n")
        retired = subprocess.run(
            [*command, "--repo", str(self.root), "retire", "--task", "encoding",
             "--result-ref", "test/encoding", "--users-released"],
            cwd=self.base, env=environment, capture_output=True)
        self.assertEqual(retired.returncode, 0, retired.stderr)
        self.assertFalse(self.topic.exists())


if __name__ == "__main__":
    unittest.main()
