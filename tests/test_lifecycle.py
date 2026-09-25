import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from workspace_lifecycle.errors import LifecycleError
from workspace_lifecycle.service import begin, finish, reclaim, retire, retire_pending, status


def run(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); base = Path(self.temp.name)
        self.remote = base / "remote.git"; self.root = base / "root"; self.topic = base / "topic"
        run(base, "init", "--bare", "--initial-branch=trunk", str(self.remote))
        run(base, "clone", str(self.remote), str(self.root))
        run(self.root, "config", "user.name", "Test"); run(self.root, "config", "user.email", "test@example.invalid")
        (self.root / "README").write_text("base\n")
        run(self.root, "add", "README"); run(self.root, "commit", "-m", "base"); run(self.root, "push", "origin", "trunk")
        self.remote_default = "origin"
        push = Path(__file__).parents[1] / "src" / "workspace_lifecycle" / "push.py"
        self.preflight = [sys.executable, "-m", "workspace_lifecycle.push", "{repo}", "--user-intent", "push"]

    def tearDown(self): self.temp.cleanup()

    def source_plan(self, workspace, name):
        import hashlib
        return {"commit": [{"path": name, "classification": "source", "owner": self.task,
                             "evidence": "created by this disposable task", "safe_to_commit": True,
                             "sha256": hashlib.sha256((workspace / name).read_bytes()).hexdigest()}]}

    def test_begin_finish_merge_and_retire(self):
        begin(self.root, task="one", request="issue/1", remote="origin", branch="topic/one", worktree=str(self.topic), validation=["git", "diff", "--check"], preflight=self.preflight)
        (self.topic / "feature.txt").write_text("done\n")
        plan = Path(self.temp.name) / "plan-one.json"
        self.task = "one"; plan.write_text(json.dumps(self.source_plan(self.topic, "feature.txt")))
        result = finish(self.topic, task="one", plan_path=str(plan), result_ref="issue/1")
        self.assertTrue(result["accepted"]); self.assertEqual(result["integration"]["destination"], "trunk")
        self.assertEqual((self.root / "feature.txt").read_text(), "done\n")
        self.assertEqual(status(self.topic, "one")["completion"], "accepted")
        retired = retire(self.root, task="one", result_ref="issue/1", users_released=True)
        self.assertTrue(retired["retired"]); self.assertFalse(self.topic.exists())
        receipt = retired['receipt']
        self.assertEqual((Path(receipt['recovery_path']) / 'feature.txt').read_text(), 'done\n')
        self.assertTrue(Path(receipt['admin_archive_path']).is_dir())
        self.assertNotIn('topic/one', run(self.root, 'worktree', 'list', '--porcelain'))

    def test_finish_rejects_unknown_dirty(self):
        begin(self.root, task="two", request="issue/2", remote="origin", branch="topic/two", worktree=str(self.topic), validation=["git", "diff", "--check"], preflight=self.preflight)
        (self.topic / "private.txt").write_text("keep\n")
        plan = Path(self.temp.name) / "plan-two.json"; plan.write_text("{}")
        with self.assertRaises(LifecycleError): finish(self.topic, task="two", plan_path=str(plan), result_ref="issue/2")
        self.assertTrue((self.topic / "private.txt").exists())

    def test_parent_child_requires_parent_acceptance(self):
        parent = Path(self.temp.name) / "parent"; child = Path(self.temp.name) / "child"
        begin(self.root, task="parent", request="i/p", remote="origin", branch="topic/parent", worktree=str(parent), validation=["git", "diff", "--check"], preflight=self.preflight)
        begin(self.root, task="child", request="i/c", remote="origin", branch="topic/child", worktree=str(child), parent="parent", dependencies=["parent"], validation=["git", "diff", "--check"], preflight=self.preflight)
        (child / "x").write_text("x")
        self.task = "child"; plan = Path(self.temp.name) / "plan-child.json"; plan.write_text(json.dumps(self.source_plan(child, "x")))
        with self.assertRaises(LifecycleError): finish(child, task="child", plan_path=str(plan), result_ref="i/c")

    def test_parent_child_reaches_non_main_default(self):
        parent = Path(self.temp.name) / "parent"; child = Path(self.temp.name) / "child"
        begin(self.root, task="parent", request="i/p", remote="origin", branch="topic/parent", worktree=str(parent), validation=["git", "diff", "--check"], preflight=self.preflight)
        (parent / "parent.txt").write_text("parent\n"); self.task = "parent"
        parent_plan = Path(self.temp.name) / "parent-plan.json"; parent_plan.write_text(json.dumps(self.source_plan(parent, "parent.txt")))
        finish(parent, task="parent", plan_path=str(parent_plan), result_ref="i/p")
        begin(self.root, task="child", request="i/c", remote="origin", branch="topic/child", worktree=str(child), parent="parent", dependencies=["parent"], validation=["git", "diff", "--check"], preflight=self.preflight)
        (child / "child.txt").write_text("child\n"); self.task = "child"
        child_plan = Path(self.temp.name) / "child-plan.json"; child_plan.write_text(json.dumps(self.source_plan(child, "child.txt")))
        result = finish(child, task="child", plan_path=str(child_plan), result_ref="i/c")
        self.assertIn("parent", result["integration"])
        self.assertEqual((self.root / "child.txt").read_text(), "child\n")
        self.assertEqual(run(self.root, "branch", "--show-current"), "trunk")

    def test_merge_conflict_can_be_resolved_and_retried(self):
        begin(self.root, task="conflict", request="i/x", remote="origin", branch="topic/conflict", worktree=str(self.topic), validation=["git", "diff", "--check"], preflight=self.preflight)
        (self.root / "README").write_text("default side\n"); run(self.root, "add", "README"); run(self.root, "commit", "-m", "default edit"); run(self.root, "push", "origin", "trunk")
        (self.topic / "README").write_text("topic side\n"); self.task = "conflict"
        plan = Path(self.temp.name) / "conflict-plan.json"; plan.write_text(json.dumps(self.source_plan(self.topic, "README")))
        with self.assertRaises(LifecycleError): finish(self.topic, task="conflict", plan_path=str(plan), result_ref="i/x")
        self.assertTrue(run(self.root, "rev-parse", "--verify", "MERGE_HEAD"))
        (self.root / "README").write_text("resolved\n"); run(self.root, "add", "README"); run(self.root, "commit", "-m", "resolve")
        result = finish(self.topic, task="conflict", plan_path=str(plan), result_ref="i/x")
        self.assertTrue(result["accepted"])
        self.assertEqual((self.root / "README").read_text(), "resolved\n")

    def test_retire_request_replays_after_native_lock_failure(self):
        begin(self.root, task="retire-retry", request="i/r", remote="origin", branch="topic/retire-retry", worktree=str(self.topic), validation=["git", "diff", "--check"], preflight=self.preflight)
        (self.topic / "r.txt").write_text("r\n"); self.task = "retire-retry"
        plan = Path(self.temp.name) / "retire-plan.json"; plan.write_text(json.dumps(self.source_plan(self.topic, "r.txt")))
        finish(self.topic, task="retire-retry", plan_path=str(plan), result_ref="i/r")
        run(self.root, "worktree", "lock", "--reason", "test retained use", str(self.topic))
        with self.assertRaises(LifecycleError): retire(self.root, task="retire-retry", result_ref="i/r", users_released=True)
        run(self.root, "worktree", "unlock", str(self.topic))
        replay = retire_pending(self.root)
        self.assertEqual(replay["pending"][0]["task"], "retire-retry")
        self.assertTrue(replay["pending"][0]["retired"])

    def test_status_from_default_discovers_registered_tasks(self):
        begin(self.root, task="discover", request="i/d", remote="origin", branch="topic/discover", worktree=str(self.topic), validation=["git", "diff", "--check"], preflight=self.preflight)
        listing = status(self.root)
        self.assertEqual(listing["tasks"], [{"task": "discover", "completion": "active", "retire_pending": False}])

    def test_validation_failure_preserves_task_for_retry(self):
        gate = Path(self.temp.name) / "validation.ok"
        validation = [sys.executable, "-c", "import pathlib,sys; sys.exit(not pathlib.Path(" + repr(str(gate)) + ").exists())"]
        begin(self.root, task="validate", request="i/v", remote="origin", branch="topic/validate", worktree=str(self.topic), validation=validation, preflight=self.preflight)
        (self.topic / "v.txt").write_text("v\n"); self.task = "validate"
        plan = Path(self.temp.name) / "validate-plan.json"; plan.write_text(json.dumps(self.source_plan(self.topic, "v.txt")))
        with self.assertRaises(LifecycleError): finish(self.topic, task="validate", plan_path=str(plan), result_ref="i/v")
        self.assertTrue((self.topic / "v.txt").exists())
        gate.write_text("ok\n")
        result = finish(self.topic, task="validate", plan_path=str(plan), result_ref="i/v")
        self.assertTrue(result["accepted"])

    def test_retire_refuses_ignored_data(self):
        (self.root / ".git" / "info" / "exclude").write_text("private.generated\n")
        begin(self.root, task="ignored", request="i/g", remote="origin", branch="topic/ignored", worktree=str(self.topic), validation=["git", "diff", "--check"], preflight=self.preflight)
        (self.topic / "g.txt").write_text("g\n"); self.task = "ignored"
        plan = Path(self.temp.name) / "ignored-plan.json"; plan.write_text(json.dumps(self.source_plan(self.topic, "g.txt")))
        finish(self.topic, task="ignored", plan_path=str(plan), result_ref="i/g")
        (self.topic / "private.generated").write_text("do not erase\n")
        with self.assertRaises(LifecycleError): retire(self.root, task="ignored", result_ref="i/g", users_released=True)
        self.assertTrue((self.topic / "private.generated").exists())

    def test_retire_rejects_unaccepted_task(self):
        begin(self.root, task="unaccepted", request="i/u", remote="origin", branch="topic/unaccepted", worktree=str(self.topic), validation=["git", "diff", "--check"], preflight=self.preflight)
        with self.assertRaises(LifecycleError): retire(self.root, task="unaccepted", result_ref="i/u", users_released=True)
        self.assertTrue(self.topic.exists())

    def test_child_then_parent_retirement(self):
        parent = Path(self.temp.name) / "parent"; child = Path(self.temp.name) / "child"
        begin(self.root, task="p", request="i/p", remote="origin", branch="topic/p", worktree=str(parent), validation=["git", "diff", "--check"], preflight=self.preflight)
        (parent / "p.txt").write_text("p\n"); self.task = "p"
        pplan = Path(self.temp.name) / "p.json"; pplan.write_text(json.dumps(self.source_plan(parent, "p.txt")))
        finish(parent, task="p", plan_path=str(pplan), result_ref="i/p")
        begin(self.root, task="c", request="i/c", remote="origin", branch="topic/c", worktree=str(child), parent="p", dependencies=["p"], validation=["git", "diff", "--check"], preflight=self.preflight)
        (child / "c.txt").write_text("c\n"); self.task = "c"
        cplan = Path(self.temp.name) / "c.json"; cplan.write_text(json.dumps(self.source_plan(child, "c.txt")))
        finish(child, task="c", plan_path=str(cplan), result_ref="i/c")
        with self.assertRaises(LifecycleError): retire(self.root, task="p", result_ref="i/p", users_released=True)
        self.assertTrue(retire(self.root, task="c", result_ref="i/c", users_released=True)["retired"])
        self.assertTrue(retire(self.root, task="p", result_ref="i/p", users_released=True)["retired"])

    def test_push_hold_then_retry_uses_same_finish_intent(self):
        gate = Path(self.temp.name) / "allow-push"
        code = ("import json,pathlib,subprocess; g=pathlib.Path(" + repr(str(gate)) + "); "
                "b=subprocess.check_output(['git','branch','--show-current'],text=True).strip(); "
                "print(json.dumps({'decision':'push','reason':'approved','push_argv':['git','push','origin','HEAD:refs/heads/'+b]} "
                "if g.exists() else {'decision':'hold','reason':'waiting'}))")
        hold_preflight = [sys.executable, "-c", code, "{repo}"]
        begin(self.root, task="hold", request="i/h", remote="origin", branch="topic/hold", worktree=str(self.topic), validation=["git", "diff", "--check"], preflight=hold_preflight)
        (self.topic / "h.txt").write_text("h\n"); self.task = "hold"
        plan = Path(self.temp.name) / "hold.json"; plan.write_text(json.dumps(self.source_plan(self.topic, "h.txt")))
        with self.assertRaises(LifecycleError): finish(self.topic, task="hold", plan_path=str(plan), result_ref="i/h")
        gate.write_text("go\n")
        result = finish(self.topic, task="hold", plan_path=str(plan), result_ref="i/h")
        self.assertTrue(result["accepted"])


class RecoveryBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = LifecycleTest('runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        f = self.fixture
        begin(f.root, task='boundary', request='issue/boundary', remote='origin', branch='topic/boundary',
              worktree=str(f.topic), validation=['git', 'diff', '--check'], preflight=f.preflight)
        path = f.topic / 'feature'; path.write_text('verified')
        import hashlib
        self.plan = Path(f.temp.name) / 'boundary.json'
        self.plan.write_text(json.dumps({'commit': [{'path': 'feature', 'classification': 'source',
            'owner': 'boundary', 'evidence': 'fixture authored file', 'safe_to_commit': True,
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}]}))
        finish(f.topic, task='boundary', plan_path=str(self.plan), result_ref='issue/boundary')

    def test_compat_refuses_retiring_task(self):
        from workspace_lifecycle.compat import registered_checkout
        f = self.fixture
        retire(f.root, task='boundary', result_ref='issue/boundary', users_released=True, request_only=True)
        with self.assertRaises(LifecycleError):
            with registered_checkout(f.topic):
                self.fail('retiring checkout admitted')

    def test_compat_refuses_duplicate_git_checkout(self):
        from workspace_lifecycle.compat import registered_checkout
        f = self.fixture; duplicate = Path(f.temp.name) / 'duplicate'
        run(f.root, 'worktree', 'add', '--detach', str(duplicate), 'topic/boundary')
        run(duplicate, 'symbolic-ref', 'HEAD', 'refs/heads/topic/boundary')
        with self.assertRaises(LifecycleError):
            with registered_checkout(duplicate):
                self.fail('duplicate identity admitted')

    def test_absence_before_authorized_move_is_not_success(self):
        f = self.fixture
        retire(f.root, task='boundary', result_ref='issue/boundary', users_released=True, request_only=True)
        run(f.root, 'worktree', 'remove', str(f.topic))
        with self.assertRaises(LifecycleError):
            retire(f.root, task='boundary', result_ref='issue/boundary')
        self.assertIn('boundary', [item['task'] for item in status(f.root)['tasks']])

    def test_move_before_state_save_recovers_receipt(self):
        from unittest.mock import patch
        from workspace_lifecycle import service
        f = self.fixture; original = service.git
        def interrupted(repo, *args, **kwargs):
            result = original(repo, *args, **kwargs)
            if args[:2] == ('worktree', 'move'):
                raise OSError('crash after actual native move')
            return result
        with patch.object(service, 'git', side_effect=interrupted):
            with self.assertRaises(LifecycleError):
                retire(f.root, task='boundary', result_ref='issue/boundary', users_released=True)
        self.assertFalse(f.topic.exists())
        self.assertEqual(status(f.root)['tasks'][0]['retire_pending'], True)
        self.assertTrue(retire(f.root, task='boundary', result_ref='issue/boundary')['retired'])
        self.assertEqual(status(f.root)['tasks'], [])

    def test_hidden_untracked_data_is_retained(self):
        f = self.fixture
        run(f.root, 'config', 'status.showUntrackedFiles', 'no')
        private = f.topic / 'private.data'; private.write_text('must survive')
        with self.assertRaises(LifecycleError):
            retire(f.root, task='boundary', result_ref='issue/boundary', users_released=True)
        self.assertEqual(private.read_text(), 'must survive')

    def test_hidden_tracked_modification_is_retained(self):
        f = self.fixture
        run(f.topic, 'update-index', '--assume-unchanged', 'feature')
        changed = f.topic / 'feature'; changed.write_text('unsaved user data')
        with self.assertRaisesRegex(LifecycleError, 'assume-unchanged'):
            retire(f.root, task='boundary', result_ref='issue/boundary', users_released=True)
        self.assertEqual(changed.read_text(), 'unsaved user data')

    def test_new_head_is_not_the_accepted_retirement_identity(self):
        f = self.fixture
        (f.topic / 'feature').write_text('later work')
        run(f.topic, 'add', 'feature'); run(f.topic, 'commit', '-m', 'later task work')
        with self.assertRaisesRegex(LifecycleError, 'branch changed'):
            retire(f.root, task='boundary', result_ref='issue/boundary', users_released=True)
        self.assertTrue(f.topic.exists())

    def test_task_hold_preserves_work_until_explicit_resolution(self):
        from workspace_lifecycle import service
        f = self.fixture
        service.hold(f.topic, 'boundary', 'user hold', 'await explicit release')
        with self.assertRaisesRegex(LifecycleError, 'held'):
            finish(f.topic, task='boundary', plan_path=str(self.plan), result_ref='issue/boundary')
        service.release_hold(f.topic, 'boundary', 'fixture explicit user release')
        self.assertTrue(finish(f.topic, task='boundary', plan_path=str(self.plan), result_ref='issue/boundary')['accepted'])

    def test_other_user_lease_blocks_retirement(self):
        from workspace_lifecycle import leases
        f = self.fixture
        with leases.guard(f.topic, 'boundary'):
            with self.assertRaises(LifecycleError):
                retire(f.root, task='boundary', result_ref='issue/boundary', users_released=True)
        self.assertTrue(f.topic.exists())

    def test_own_session_can_record_hold_and_next_start_refuses(self):
        f = self.fixture
        command = [sys.executable, '-m', 'workspace_lifecycle', '--repo', str(f.topic),
                   'run', '--task', 'boundary', '--cwd', str(f.topic), '--', sys.executable, '-c']
        held = subprocess.run([*command, "from workspace_lifecycle.service import hold; hold('.', 'boundary', 'user hold', 'wait for user')"],
                              capture_output=True, text=True)
        self.assertEqual(held.returncode, 0, held.stderr)
        refused = subprocess.run([*command, "raise SystemExit(99)"], capture_output=True, text=True)
        self.assertEqual(refused.returncode, 2)
        self.assertIn('held', refused.stderr)


class EmptyDirectoryRetirementTests(unittest.TestCase):
    def setUp(self):
        self.boundary = RecoveryBoundaryTests('runTest')
        self.boundary.setUp()
        self.addCleanup(self.boundary.doCleanups)
        self.f = self.boundary.fixture

    def retire(self):
        return retire(self.f.root, task='boundary', result_ref='issue/boundary', users_released=True)

    def test_arbitrary_ignored_empty_directory(self):
        (self.f.root / '.git/info/exclude').write_text('arbitrary/\n')
        (self.f.topic / 'arbitrary').mkdir()
        receipt = self.retire()['receipt']
        self.assertTrue((Path(receipt['recovery_path']) / 'arbitrary').is_dir())

    def test_nested_empty_directories_and_build_examples(self):
        (self.f.root / '.git/info/exclude').write_text('*.egg-info/\ndist/\n')
        for name in ['random/deep/leaf', 'sample.egg-info', 'dist/empty']:
            (self.f.topic / name).mkdir(parents=True)
        receipt = self.retire()['receipt']
        for name in ['random/deep/leaf', 'sample.egg-info', 'dist/empty']:
            self.assertTrue((Path(receipt['recovery_path']) / name).is_dir())

    def payload_refusal(self, name, ignored=False):
        if ignored: (self.f.root / '.git/info/exclude').write_text('ignored/\n')
        path = self.f.topic / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b'private bytes\x00')
        with self.assertRaises(LifecycleError): self.retire()
        self.assertEqual(path.read_bytes(), b'private bytes\x00')

    def test_nonempty_ignored_data_preserved(self):
        self.payload_refusal('ignored/private', ignored=True)

    def test_nonempty_untracked_data_preserved(self):
        self.payload_refusal('untracked/private')

    def test_hidden_payload_preserved(self):
        self.payload_refusal('hidden/.payload')

    def test_complete_scan_precedes_move(self):
        from unittest.mock import patch
        empty = self.f.topic / 'empty'; empty.mkdir()
        (self.f.topic / 'private').write_bytes(b'keep')
        from workspace_lifecycle import service
        original = service.git
        def no_move(repo, *args, **kwargs):
            if args[:2] == ('worktree', 'move'): self.fail('dirty worktree moved')
            return original(repo, *args, **kwargs)
        with patch.object(service, 'git', side_effect=no_move):
            with self.assertRaises(LifecycleError): self.retire()
        self.assertTrue(empty.is_dir())

    def test_symlink_is_retained(self):
        path = self.f.topic / 'link'
        try: path.symlink_to(self.f.root, target_is_directory=True)
        except OSError as exc: self.skipTest(str(exc))
        with self.assertRaises(LifecycleError): self.retire()
        self.assertTrue(path.is_symlink())

    @unittest.skipUnless(sys.platform == 'win32', 'Windows junction')
    def test_junction_is_retained(self):
        path = self.f.topic / 'junction'
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(path), str(self.f.root)], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.addCleanup(path.rmdir)
        with self.assertRaises(LifecycleError): self.retire()
        self.assertTrue(path.exists())

    def test_mount_is_refused_before_descent(self):
        from unittest.mock import patch
        from workspace_lifecycle import service
        path = self.f.topic / 'mount'; path.mkdir(); path = path.resolve()
        original = service.os.path.ismount
        with patch.object(service.os.path, 'ismount', side_effect=lambda p: Path(p) == path or original(p)):
            with self.assertRaisesRegex(LifecycleError, 'mounted'): self.retire()
        self.assertTrue(path.is_dir())

    @unittest.skipIf(sys.platform == 'win32', 'POSIX FIFO')
    def test_special_file_is_retained(self):
        import os
        path = self.f.topic / 'pipe'; os.mkfifo(path)
        with self.assertRaises(LifecycleError): self.retire()
        self.assertTrue(path.exists())

    def test_submodule_refusal(self):
        from unittest.mock import patch
        from workspace_lifecycle import service
        original = service.git
        def git_with_submodule(repo, *args, **kwargs):
            if args == ('submodule', 'status', '--recursive'): return 'submodule present'
            return original(repo, *args, **kwargs)
        with patch.object(service, 'git', side_effect=git_with_submodule):
            with self.assertRaisesRegex(LifecycleError, 'submodule'): self.retire()
        self.assertTrue(self.f.topic.exists())

    def test_file_appearing_before_move_survives_in_recovery(self):
        from unittest.mock import patch
        path = self.f.topic / 'race'; path.mkdir(); path = path.resolve()
        from workspace_lifecycle import service
        original = service.git
        def create_before_move(repo, *args, **kwargs):
            if args[:2] == ('worktree', 'move'): (path / 'new').write_bytes(b'racing payload')
            return original(repo, *args, **kwargs)
        with patch.object(service, 'git', side_effect=create_before_move):
            receipt = self.retire()['receipt']
        self.assertEqual((Path(receipt['recovery_path']) / 'race/new').read_bytes(), b'racing payload')

    def test_native_move_failure_can_resume_without_cleanup(self):
        from unittest.mock import patch
        from workspace_lifecycle import service
        path = self.f.topic / 'empty'; path.mkdir()
        original = service.git
        def refuse_move(repo, *args, **kwargs):
            if args[:2] == ('worktree', 'move'): raise LifecycleError('native move failed')
            return original(repo, *args, **kwargs)
        with patch.object(service, 'git', side_effect=refuse_move):
            with self.assertRaisesRegex(LifecycleError, 'native move failed'): self.retire()
        self.assertTrue(path.exists())
        self.assertTrue(status(self.f.root)['tasks'][0]['retire_pending'])
        receipt = retire_pending(self.f.root)['pending'][0]['receipt']
        self.assertTrue((Path(receipt['recovery_path']) / 'empty').is_dir())

    def test_new_content_after_move_remains_in_recovery_and_old_path(self):
        from unittest.mock import patch
        from workspace_lifecycle import service
        original = service.git
        def changed(repo, *args, **kwargs):
            result = original(repo, *args, **kwargs)
            if args[:2] == ('worktree', 'move'):
                (Path(args[3]) / 'later').write_bytes(b'new in recovery')
                self.f.topic.mkdir()
                (self.f.topic / 'later').write_bytes(b'new at original')
            return result
        with patch.object(service, 'git', side_effect=changed):
            receipt = self.retire()['receipt']
        self.assertEqual((Path(receipt['recovery_path']) / 'later').read_bytes(), b'new in recovery')
        self.assertEqual((self.f.topic / 'later').read_bytes(), b'new at original')

    def test_replaced_recovery_identity_is_refused(self):
        from unittest.mock import patch
        from workspace_lifecycle import service
        original = service.git
        def changed(repo, *args, **kwargs):
            result = original(repo, *args, **kwargs)
            if args[:2] == ('worktree', 'move'):
                moved = Path(args[3]); displaced = moved.with_name(moved.name + '-held')
                moved.rename(displaced); moved.mkdir()
            return result
        with patch.object(service, 'git', side_effect=changed):
            with self.assertRaisesRegex(LifecycleError, 'recovery payload identity changed'): self.retire()
        self.assertTrue(status(self.f.root)['tasks'][0]['retire_pending'])

    def test_tracked_content_and_git_identity_preserved(self):
        path = self.f.topic / 'empty'; path.mkdir()
        before = (self.f.topic / '.git').read_bytes()
        receipt = self.retire()['receipt']; recovery = Path(receipt['recovery_path'])
        self.assertEqual((recovery / '.git').read_bytes(), before)
        self.assertEqual((recovery / 'feature').read_text(), 'verified')
        self.assertTrue((recovery / 'empty').is_dir())


    def test_real_submodule_refused(self):
        from workspace_lifecycle import service
        run(self.f.topic, '-c', 'protocol.file.allow=always', 'submodule', 'add', str(self.f.remote), 'sub')
        run(self.f.topic, 'commit', '-am', 'fixture submodule')
        with self.assertRaisesRegex(LifecycleError, 'submodule'):
            service._retirement_contents(self.f.topic)
        self.assertEqual((self.f.topic / 'sub/README').read_text(), 'base\n')

    def test_tracked_nested_parent_and_empty_child_preserved(self):
        from workspace_lifecycle import service
        parent = self.f.topic / 'owned'; parent.mkdir()
        (parent / 'tracked').write_bytes(b'owned')
        run(self.f.topic, 'add', 'owned/tracked'); run(self.f.topic, 'commit', '-m', 'fixture nested tracked')
        empty = parent / 'empty'; empty.mkdir()
        service._retirement_contents(self.f.topic)
        self.assertEqual((parent / 'tracked').read_bytes(), b'owned')
        self.assertTrue(empty.is_dir())

    def test_ignored_snapshot_directory_needs_filesystem_proof(self):
        from unittest.mock import patch
        from workspace_lifecycle import service
        empty = self.f.topic / 'arbitrary'; empty.mkdir()
        original = service._snapshot
        def ignored_entry(repo):
            result = original(repo)
            if empty.exists(): result['arbitrary/'] = '!!'
            return result
        with patch.object(service, '_snapshot', side_effect=ignored_entry):
            self.assertTrue(self.retire()['retired'])


class RetainedRetirementTests(unittest.TestCase):
    def setUp(self):
        self.boundary = RecoveryBoundaryTests('runTest')
        self.boundary.setUp()
        self.addCleanup(self.boundary.doCleanups)
        self.f = self.boundary.fixture

    def retire(self):
        return retire(self.f.root, task='boundary', result_ref='issue/boundary', users_released=True)

    def request(self):
        from workspace_lifecycle.state import locked_state
        with locked_state(self.f.root) as (_, state):
            return dict(state['tasks']['boundary']['retire'])

    def assert_recovered(self):
        result = retire_pending(self.f.root)['pending'][0]
        self.assertTrue(result['retired'], result)
        receipt = result['receipt']
        self.assertEqual((Path(receipt['recovery_path']) / 'feature').read_text(), 'verified')
        self.assertTrue(Path(receipt['admin_archive_path']).is_dir())
        self.assertFalse(self.f.topic.exists())
        self.assertNotIn(Path(receipt['recovery_path']).as_posix(), run(self.f.root, 'worktree', 'list', '--porcelain'))
        self.assertEqual(retire(self.f.root, task='boundary', result_ref='issue/boundary')['receipt'], receipt)
        return receipt

    def test_requested_phase_resumes(self):
        retire(self.f.root, task='boundary', result_ref='issue/boundary', users_released=True, request_only=True)
        self.assertEqual(self.request()['phase'], 'requested')
        self.assert_recovered()

    def test_moving_phase_resumes_before_move(self):
        from workspace_lifecycle import service
        original_save = service.save_state
        def interrupted(directory, state):
            original_save(directory, state)
            if state['tasks']['boundary'].get('retire', {}).get('phase') == 'moving':
                raise OSError('crash before move')
        with patch.object(service, 'save_state', side_effect=interrupted):
            with self.assertRaises(LifecycleError): self.retire()
        self.assertEqual(self.request()['phase'], 'moving')
        self.assertTrue(self.f.topic.is_dir())
        self.assert_recovered()

    def test_recovery_held_and_archiving_phase_resume(self):
        from workspace_lifecycle import service
        original_save = service.save_state
        for phase in ('recovery-held', 'archiving-admin'):
            with self.subTest(phase=phase):
                fixture = RecoveryBoundaryTests('runTest'); fixture.setUp()
                try:
                    f = fixture.fixture
                    def interrupted(directory, state):
                        original_save(directory, state)
                        if state['tasks']['boundary'].get('retire', {}).get('phase') == phase:
                            raise OSError('crash at ' + phase)
                    with patch.object(service, 'save_state', side_effect=interrupted):
                        with self.assertRaises(LifecycleError):
                            retire(f.root, task='boundary', result_ref='issue/boundary', users_released=True)
                    self.assertEqual(retire_pending(f.root)['pending'][0]['retired'], True)
                finally:
                    fixture.doCleanups()

    def test_admin_rename_after_execution_and_final_receipt_retry(self):
        from workspace_lifecycle import service
        original_rename = Path.rename
        def interrupted(source, target):
            result = original_rename(source, target)
            if Path(target).name == 'admin': raise OSError('crash after admin rename')
            return result
        with patch.object(Path, 'rename', interrupted):
            with self.assertRaises(LifecycleError): self.retire()
        receipt = self.assert_recovered()
        self.assertEqual(run(self.f.root, 'rev-parse', 'refs/heads/topic/boundary'), receipt['commit'])

    def test_final_receipt_save_interruption_retries_without_binding(self):
        from workspace_lifecycle import service
        original_save = service.save_state
        def interrupted(directory, state):
            if 'boundary' in state.get('retired', {}):
                raise OSError('crash before final state save')
            return original_save(directory, state)
        with patch.object(service, 'save_state', side_effect=interrupted):
            with self.assertRaises(LifecycleError): self.retire()
        binding = subprocess.run(['git', '-C', str(self.f.root), 'config', '--get',
                                  'branch.topic/boundary.workspaceTask'], capture_output=True)
        self.assertEqual(binding.returncode, 1)
        self.assert_recovered()

    def test_bytes_added_after_admin_archive_survive(self):
        retire(self.f.root, task='boundary', result_ref='issue/boundary', users_released=True, request_only=True)
        recovery = Path(self.request()['recovery_path'])
        original_rename = Path.rename
        def add_after_archive(source, target):
            result = original_rename(source, target)
            if Path(target).name == 'admin':
                (recovery / 'after-admin').write_bytes(b'held bytes')
            return result
        with patch.object(Path, 'rename', add_after_archive):
            receipt = self.retire()['receipt']
        self.assertEqual((Path(receipt['recovery_path']) / 'after-admin').read_bytes(), b'held bytes')

    def test_recovery_destination_collision_refuses_without_overwrite(self):
        retire(self.f.root, task='boundary', result_ref='issue/boundary', users_released=True, request_only=True)
        recovery = Path(self.request()['recovery_path'])
        recovery.write_bytes(b'foreign')
        with self.assertRaises(LifecycleError): self.retire()
        self.assertEqual(recovery.read_bytes(), b'foreign')
        self.assertTrue(self.f.topic.is_dir())

    def test_admin_archive_collision_refuses_without_overwrite(self):
        retire(self.f.root, task='boundary', result_ref='issue/boundary', users_released=True, request_only=True)
        archive = Path(self.request()['admin_archive_path'])
        archive.write_bytes(b'foreign')
        with self.assertRaises(LifecycleError): self.retire()
        self.assertEqual(archive.read_bytes(), b'foreign')
        self.assertTrue(Path(self.request()['admin_original_path']).is_dir())

    def test_admin_archive_can_restore_exact_registration_in_fixture(self):
        receipt = self.retire()['receipt']
        archive = Path(receipt['admin_archive_path']); admin = Path(receipt['admin_original_path'])
        archive.rename(admin)
        recovery = Path(receipt['recovery_path'])
        self.assertEqual(run(recovery, 'rev-parse', 'HEAD'), receipt['commit'])
        self.assertIn(recovery.as_posix(), run(self.f.root, 'worktree', 'list', '--porcelain'))

    def test_process_exit_at_each_boundary_resumes_same_request(self):
        script = '''
import os, sys
from pathlib import Path
from workspace_lifecycle import service
point, repo = sys.argv[1:]
save = service.save_state
git = service.git
rename = Path.rename
def stop_after_save(directory, state):
    save(directory, state)
    request = state['tasks'].get('boundary', {}).get('retire', {})
    if point == request.get('phase') or (point == 'retired' and 'boundary' in state.get('retired', {})):
        os._exit(37)
def stop_after_move(path, *args, **kwargs):
    result = git(path, *args, **kwargs)
    if point == 'after-move' and args[:2] == ('worktree', 'move'):
        os._exit(37)
    return result
def stop_after_archive(source, target):
    result = rename(source, target)
    if point == 'after-archive' and Path(target).name == 'admin':
        os._exit(37)
    return result
service.save_state = stop_after_save
service.git = stop_after_move
Path.rename = stop_after_archive
service.retire(repo, task='boundary', result_ref='issue/boundary', users_released=True)
'''
        for point in ('requested', 'moving', 'after-move', 'recovery-held',
                      'archiving-admin', 'after-archive', 'retired'):
            with self.subTest(point=point):
                fixture = RecoveryBoundaryTests('runTest'); fixture.setUp()
                try:
                    f = fixture.fixture
                    exited = subprocess.run([sys.executable, '-c', script, point, str(f.root)],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    self.assertEqual(exited.returncode, 37, exited.stderr)
                    if point == 'retired':
                        self.assertEqual(retire(f.root, task='boundary', result_ref='issue/boundary')['receipt']['phase'], 'retired')
                    else:
                        result = retire_pending(f.root)['pending'][0]
                        self.assertTrue(result['retired'], result)
                        self.assertEqual((Path(result['receipt']['recovery_path']) / 'feature').read_text(), 'verified')
                finally:
                    fixture.doCleanups()

    def test_other_worktree_and_read_only_git_overlap(self):
        from workspace_lifecycle import service
        other = Path(self.f.temp.name) / 'other'
        run(self.f.root, 'worktree', 'add', '-b', 'topic/other', str(other))
        other_admin = Path(run(other, 'rev-parse', '--path-format=absolute', '--git-dir'))
        before = (run(other, 'rev-parse', 'HEAD'), (other / '.git').read_bytes(),
                  (other_admin / 'index').read_bytes(), (other_admin / 'config.worktree').read_bytes()
                  if (other_admin / 'config.worktree').exists() else b'')
        observed = []; stop = threading.Event(); move_boundary = threading.Event(); admin_boundary = threading.Event()
        def reader():
            while not stop.is_set():
                for location, arguments in ((self.f.root, ('worktree', 'list', '--porcelain')),
                                            (self.f.topic, ('rev-parse', 'HEAD'))):
                    process = subprocess.run(['git', '-C', str(location), *arguments],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'})
                    observed.append((move_boundary.is_set(), admin_boundary.is_set(), process.returncode, process.stderr))
        original = service.git
        def slow_boundary(repo, *args, **kwargs):
            if args[:2] == ('worktree', 'move'):
                move_boundary.set(); time.sleep(0.1)
            return original(repo, *args, **kwargs)
        original_rename = Path.rename
        def slow_archive(source, target):
            if Path(target).name == 'admin':
                admin_boundary.set(); time.sleep(0.1)
            return original_rename(source, target)
        worker = threading.Thread(target=reader); worker.start()
        try:
            time.sleep(0.05)
            with patch.object(service, 'git', side_effect=slow_boundary), patch.object(Path, 'rename', slow_archive):
                try: result = self.retire()
                except LifecycleError: result = None
            time.sleep(0.05)
        finally:
            stop.set(); worker.join(timeout=5)
        receipt = result['receipt'] if result else retire_pending(self.f.root)['pending'][0]['receipt']
        self.assertGreater(len(observed), 2)
        self.assertTrue(any(during for during, _, _, _ in observed))
        if admin_boundary.is_set():
            self.assertTrue(any(during for _, during, _, _ in observed))
        self.assertTrue(all(code == 0 or error for _, _, code, error in observed))
        self.assertEqual((run(other, 'rev-parse', 'HEAD'), (other / '.git').read_bytes(),
                          (other_admin / 'index').read_bytes(), (other_admin / 'config.worktree').read_bytes()
                          if (other_admin / 'config.worktree').exists() else b''), before)
        self.assertEqual(run(self.f.root, 'rev-parse', 'refs/heads/topic/boundary'), receipt['commit'])
        failed = subprocess.run(['git', '-C', receipt['recovery_path'], 'rev-parse', 'HEAD'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertNotEqual(failed.returncode, 0)


class ReclaimTest(unittest.TestCase):
    def setUp(self):
        self.host = LifecycleTest()
        self.host.setUp()
        self.root = self.host.root
        self.topic = self.host.topic
        self.temp = self.host.temp
        self.preflight = self.host.preflight

    def tearDown(self):
        self.host.tearDown()

    def source_plan(self, workspace, name):
        self.host.task = self.task
        return self.host.source_plan(workspace, name)

    def test_reclaim_removes_matching_payload_and_keeps_commit(self):
        begin(self.root, task="one", request="issue/1", remote="origin", branch="topic/one", worktree=str(self.topic), validation=["git", "diff", "--check"], preflight=self.preflight)
        (self.topic / "feature.txt").write_text("done\n")
        plan = Path(self.temp.name) / "plan-one.json"
        self.task = "one"; plan.write_text(json.dumps(self.source_plan(self.topic, "feature.txt")))
        finish(self.topic, task="one", plan_path=str(plan), result_ref="issue/1")
        retired = retire(self.root, task="one", result_ref="issue/1", users_released=True)
        receipt = retired['receipt']
        commit = receipt['commit']
        result = reclaim(self.root, task="one", result_ref="issue/1", preservation_evidence="issue/1 preservation complete")
        self.assertTrue(result['reclaimed'])
        self.assertFalse(result['receipt']['recovery_held'])
        self.assertFalse(Path(receipt['recovery_path']).exists())
        self.assertFalse(Path(receipt['admin_archive_path']).exists())
        self.assertEqual(run(self.root, 'rev-parse', 'refs/heads/topic/one'), commit)
        again = reclaim(self.root, task="one", result_ref="issue/1", preservation_evidence="issue/1 preservation complete")
        self.assertTrue(again['reclaimed'])

    def test_reclaim_holds_bytes_added_after_retirement(self):
        begin(self.root, task="one", request="issue/1", remote="origin", branch="topic/one", worktree=str(self.topic), validation=["git", "diff", "--check"], preflight=self.preflight)
        (self.topic / "feature.txt").write_text("done\n")
        plan = Path(self.temp.name) / "plan-one.json"
        self.task = "one"; plan.write_text(json.dumps(self.source_plan(self.topic, "feature.txt")))
        finish(self.topic, task="one", plan_path=str(plan), result_ref="issue/1")
        receipt = retire(self.root, task="one", result_ref="issue/1", users_released=True)['receipt']
        (Path(receipt['recovery_path']) / 'later').write_text('extra\n')
        with self.assertRaises(LifecycleError):
            reclaim(self.root, task="one", result_ref="issue/1", preservation_evidence="issue/1 preservation complete")
        self.assertTrue((Path(receipt['recovery_path']) / 'later').exists())

    def test_reclaim_resumes_after_authorized_save_before_rename(self):
        begin(self.root, task="one", request="issue/1", remote="origin", branch="topic/one", worktree=str(self.topic), validation=["git", "diff", "--check"], preflight=self.preflight)
        (self.topic / "feature.txt").write_text("done\n")
        plan = Path(self.temp.name) / "plan-one.json"
        self.task = "one"; plan.write_text(json.dumps(self.source_plan(self.topic, "feature.txt")))
        finish(self.topic, task="one", plan_path=str(plan), result_ref="issue/1")
        receipt = retire(self.root, task="one", result_ref="issue/1", users_released=True)['receipt']
        state_path = Path(run(self.root, 'rev-parse', '--path-format=absolute', '--git-common-dir')) / 'workspace-lifecycle' / 'state.json'
        state = json.loads(state_path.read_text())
        recovery = Path(receipt['recovery_path'])
        state['retired']['one']['preservation_evidence'] = 'issue/1 preservation complete'
        state['retired']['one']['reclaim_phase'] = 'authorized'
        state['retired']['one']['reclaim_staging'] = str(recovery.parent / (recovery.name + '.reclaim'))
        state_path.write_text(json.dumps(state))
        result = reclaim(self.root, task="one", result_ref="issue/1", preservation_evidence="issue/1 preservation complete")
        self.assertTrue(result['reclaimed'])
        self.assertFalse(recovery.exists())
        self.assertFalse(Path(receipt['admin_archive_path']).exists())
        self.assertEqual(run(self.root, 'rev-parse', 'refs/heads/topic/one'), receipt['commit'])

    def test_reclaim_refuses_without_manifest(self):
        begin(self.root, task="one", request="issue/1", remote="origin", branch="topic/one", worktree=str(self.topic), validation=["git", "diff", "--check"], preflight=self.preflight)
        (self.topic / "feature.txt").write_text("done\n")
        plan = Path(self.temp.name) / "plan-one.json"
        self.task = "one"; plan.write_text(json.dumps(self.source_plan(self.topic, "feature.txt")))
        finish(self.topic, task="one", plan_path=str(plan), result_ref="issue/1")
        retire(self.root, task="one", result_ref="issue/1", users_released=True)
        state_path = Path(run(self.root, 'rev-parse', '--path-format=absolute', '--git-common-dir')) / 'workspace-lifecycle' / 'state.json'
        state = json.loads(state_path.read_text())
        del state['retired']['one']['content_manifest']
        state_path.write_text(json.dumps(state))
        with self.assertRaises(LifecycleError):
            reclaim(self.root, task="one", result_ref="issue/1", preservation_evidence="issue/1 preservation complete")
        self.assertTrue(Path(state['retired']['one']['recovery_path']).exists())
