import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from workspace_lifecycle.errors import LifecycleError
from workspace_lifecycle.service import begin, finish, retire, retire_pending, status


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

    def test_absence_before_authorized_remove_is_not_success(self):
        f = self.fixture
        retire(f.root, task='boundary', result_ref='issue/boundary', users_released=True, request_only=True)
        run(f.root, 'worktree', 'remove', str(f.topic))
        with self.assertRaisesRegex(LifecycleError, 'disappeared before authorized'):
            retire(f.root, task='boundary', result_ref='issue/boundary')
        self.assertIn('boundary', [item['task'] for item in status(f.root)['tasks']])

    def test_absence_after_authorized_remove_recovers_receipt(self):
        from unittest.mock import patch
        from workspace_lifecycle import service
        f = self.fixture; original = service.git
        def interrupted(repo, *args, **kwargs):
            result = original(repo, *args, **kwargs)
            if args[:2] == ('worktree', 'remove'):
                raise OSError('crash after actual native removal')
            return result
        with patch.object(service, 'git', side_effect=interrupted):
            with self.assertRaises(LifecycleError):
                retire(f.root, task='boundary', result_ref='issue/boundary', users_released=True)
        self.assertFalse(f.topic.exists())
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
        self.assertTrue(self.retire()['retired'])

    def test_nested_empty_directories_and_build_examples(self):
        (self.f.root / '.git/info/exclude').write_text('*.egg-info/\ndist/\n')
        for name in ['random/deep/leaf', 'sample.egg-info', 'dist/empty']:
            (self.f.topic / name).mkdir(parents=True)
        self.assertTrue(self.retire()['retired'])

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

    def test_complete_scan_precedes_any_cleanup(self):
        from unittest.mock import patch
        empty = self.f.topic / 'empty'; empty.mkdir()
        (self.f.topic / 'private').write_bytes(b'keep')
        with patch.object(Path, 'rmdir') as remove:
            with self.assertRaises(LifecycleError): self.retire()
            remove.assert_not_called()
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

    def test_file_appearing_at_rmdir_survives(self):
        from unittest.mock import patch
        path = self.f.topic / 'race'; path.mkdir(); path = path.resolve()
        original = Path.rmdir
        def create_before_remove(target):
            if target == path: (target / 'new').write_bytes(b'racing payload')
            return original(target)
        with patch.object(Path, 'rmdir', create_before_remove):
            with self.assertRaises(LifecycleError): self.retire()
        self.assertEqual((path / 'new').read_bytes(), b'racing payload')
        self.assertTrue(status(self.f.root)['tasks'][0]['retire_pending'])

    def test_cleanup_then_native_remove_failure_can_resume(self):
        from unittest.mock import patch
        from workspace_lifecycle import service
        path = self.f.topic / 'empty'; path.mkdir()
        original = service.git
        def refuse_remove(repo, *args, **kwargs):
            if args[:2] == ('worktree', 'remove'): raise LifecycleError('native remove failed')
            return original(repo, *args, **kwargs)
        with patch.object(service, 'git', side_effect=refuse_remove):
            with self.assertRaisesRegex(LifecycleError, 'native remove failed'): self.retire()
        self.assertFalse(path.exists())
        self.assertTrue(status(self.f.root)['tasks'][0]['retire_pending'])
        self.assertTrue(retire_pending(self.f.root)['pending'][0]['retired'])

    def test_post_cleanup_new_content_refuses_native_removal(self):
        from unittest.mock import patch
        from workspace_lifecycle import service
        (self.f.topic / 'empty').mkdir()
        original = service._remove_empty_directories
        def changed(workspace, candidates):
            original(workspace, candidates)
            (workspace / 'late').mkdir()
        with patch.object(service, '_remove_empty_directories', side_effect=changed):
            with self.assertRaisesRegex(LifecycleError, 'changed after'): self.retire()
        self.assertTrue((self.f.topic / 'late').is_dir())

    def test_replaced_directory_identity_is_refused(self):
        from unittest.mock import patch
        from workspace_lifecycle import service
        path = self.f.topic / 'empty'; path.mkdir()
        original = service._remove_empty_directories
        def changed(workspace, candidates):
            path.rename(workspace / 'moved')
            path.mkdir()
            original(workspace, candidates)
        with patch.object(service, '_remove_empty_directories', side_effect=changed):
            with self.assertRaisesRegex(LifecycleError, 'identity changed'): self.retire()
        self.assertTrue(path.is_dir())

    def test_tracked_ancestors_and_git_identity_untouched_by_cleanup(self):
        from unittest.mock import patch
        from workspace_lifecycle import service
        path = self.f.topic / 'empty'; path.mkdir()
        before = (self.f.topic / '.git').read_bytes()
        candidates = service._retirement_contents(self.f.topic)
        self.assertEqual([entry[0] for entry in candidates], [path])
        service._remove_empty_directories(self.f.topic, candidates)
        self.assertEqual((self.f.topic / '.git').read_bytes(), before)
        self.assertEqual((self.f.topic / 'feature').read_text(), 'verified')


    def test_real_submodule_refused(self):
        from workspace_lifecycle import service
        run(self.f.topic, '-c', 'protocol.file.allow=always', 'submodule', 'add', str(self.f.remote), 'sub')
        run(self.f.topic, 'commit', '-am', 'fixture submodule')
        with self.assertRaisesRegex(LifecycleError, 'submodule'):
            service._retirement_contents(self.f.topic)
        self.assertEqual((self.f.topic / 'sub/README').read_text(), 'base\n')

    def test_tracked_nested_parent_not_a_cleanup_candidate(self):
        from workspace_lifecycle import service
        parent = self.f.topic / 'owned'; parent.mkdir()
        (parent / 'tracked').write_bytes(b'owned')
        run(self.f.topic, 'add', 'owned/tracked'); run(self.f.topic, 'commit', '-m', 'fixture nested tracked')
        empty = parent / 'empty'; empty.mkdir()
        candidates = service._retirement_contents(self.f.topic)
        self.assertEqual([path for path, _ in candidates], [empty])
        service._remove_empty_directories(self.f.topic, candidates)
        self.assertEqual((parent / 'tracked').read_bytes(), b'owned')
