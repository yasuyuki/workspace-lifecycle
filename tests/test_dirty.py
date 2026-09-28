import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from workspace_lifecycle.errors import LifecycleError
from workspace_lifecycle import leases
from workspace_lifecycle.service import begin, finish, hold, reclaim, retire
from workspace_lifecycle.state import locked_state, save_state


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()


class DirtyLifecycleTest(unittest.TestCase):
    """Independent disposable remote fixture; it does not inherit lifecycle tests."""
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.base = Path(self.temporary.name)
        self.remote, self.root, self.topic = self.base / "remote.git", self.base / "root", self.base / "topic"
        git(self.base, "init", "--bare", "--initial-branch=trunk", str(self.remote))
        git(self.base, "clone", str(self.remote), str(self.root))
        git(self.root, "config", "user.name", "Test"); git(self.root, "config", "user.email", "test@example.invalid")
        (self.root / "base.txt").write_text("base\n")
        (self.root / "generated.txt").write_text("generated base\n")
        git(self.root, "add", "."); git(self.root, "commit", "-m", "base"); git(self.root, "push", "origin", "trunk")
        self.preflight = [sys.executable, "-m", "workspace_lifecycle.push", "{repo}", "--user-intent", "push"]

    def tearDown(self): self.temporary.cleanup()

    def start(self, task="dirty", validation=None):
        begin(self.root, task=task, request="issue/" + task, remote="origin", branch="topic/" + task,
              worktree=str(self.topic), validation=validation or ["git", "diff", "--check"], preflight=self.preflight)
        return task

    def entry(self, task, name, kind="source", **extra):
        value = {"path": name, "classification": kind, "owner": task, "evidence": "disposable task evidence",
                 "sha256": hashlib.sha256((self.topic / name).read_bytes()).hexdigest()}
        if kind == "source": value["safe_to_commit"] = True
        value.update(extra); return value

    def plan(self, name, value):
        path = self.base / name; path.write_text(json.dumps(value)); return path

    def resolve_run(self, effective, launch, *argv):
        environment = dict(os.environ)
        environment.pop('PYTHONPATH', None)
        return subprocess.run([sys.executable, '-m', 'workspace_lifecycle.cli',
                               'resolve-run', '--cwd', str(effective),
                               '--launch-cwd', str(launch), '--', *argv],
                              text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=environment)

    def test_resolve_run_unmanaged_nested_preserves_launch_cwd_without_state(self):
        nested = self.root / 'nested'; nested.mkdir()
        launch = self.base / 'launch'; launch.mkdir()
        child = self.resolve_run(nested, launch, sys.executable, '-c',
                                 'import os,sys; print(os.getcwd()); sys.exit(19)')
        self.assertEqual(child.returncode, 19, child.stderr)
        self.assertEqual(Path(child.stdout.strip()).resolve(), launch.resolve())
        common = Path(git(self.root, 'rev-parse', '--path-format=absolute', '--git-common-dir'))
        self.assertFalse((common / 'workspace-lifecycle').exists())

    @unittest.skipIf(os.name == 'nt', 'POSIX exec signal identity')
    def test_resolve_run_unmanaged_exec_preserves_signal_status(self):
        environment = dict(os.environ,
                           PYTHONPATH=str(Path(__file__).parents[1] / 'src'))
        child = subprocess.Popen([sys.executable, '-m', 'workspace_lifecycle.cli',
                                  'resolve-run', '--cwd', str(self.root),
                                  '--launch-cwd', str(self.root), '--', sys.executable, '-c',
                                  'import time; print("ready", flush=True); time.sleep(30)'],
                                 text=True, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, env=environment)
        self.assertEqual(child.stdout.readline().strip(), 'ready')
        child.terminate()
        self.assertEqual(child.wait(timeout=5), -15)
        child.communicate()

    def test_resolve_run_managed_nested_supplies_finish_context(self):
        task = self.start('resolved')
        nested = self.topic / 'nested'; nested.mkdir()
        child = self.resolve_run(nested, self.topic, sys.executable, '-c',
                                 'import json,os; value=json.loads(os.environ["WORKSPACE_LIFECYCLE_CONTEXT"]); '
                                 'assert value["version"] == 1 and value["repo"] == os.environ["WORKSPACE_LIFECYCLE_REPO"] and value["task"] == "resolved"; '
                                 'assert value["finish_argv"][-3:] == ["finish","--task","resolved"]; '
                                 'assert os.environ["WORKSPACE_LIFECYCLE_TASK"] == "resolved"')
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(child.stdout, '')
        hold(self.topic, task, 'fixture hold', 'await explicit release')
        refused = self.resolve_run(nested, self.topic, sys.executable, '-c',
                                   'raise SystemExit(99)')
        self.assertEqual(refused.returncode, 2)
        self.assertIn('held', refused.stderr)

    @unittest.skipUnless(os.name == 'nt', 'Windows Job reentry')
    def test_nested_resolve_run_shares_one_managed_use(self):
        task = self.start('joined')
        inner = ('import os; assert os.environ["WORKSPACE_LIFECYCLE_TASK"] == "joined"; '
                 'print("joined child")')
        leader = ('import os,subprocess,sys; from workspace_lifecycle import leases; '
                  'repo=os.environ["WORKSPACE_LIFECYCLE_REPO"]; '
                  'task=os.environ["WORKSPACE_LIFECYCLE_TASK"]; '
                  'before=leases.status(repo,task)["receipt"]; '
                  'result=subprocess.run([sys.executable,"-m","workspace_lifecycle.cli",'
                  '"resolve-run","--cwd",repo,"--launch-cwd",repo,"--",'
                  'sys.executable,"-c",%r],capture_output=True,text=True); '
                  'assert result.returncode == 0, result.stderr; '
                  'assert leases.status(repo,task)["receipt"] == before; '
                  'print(result.stdout.strip())' % inner)
        result = self.resolve_run(self.topic, self.topic, sys.executable, '-c', leader)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'joined child')
        self.assertIsNone(leases.status(self.topic, task)['receipt'])

    def test_resolve_run_refuses_legacy_and_managed_unbound(self):
        common = Path(git(self.root, 'rev-parse', '--path-format=absolute', '--git-common-dir'))
        legacy = common / 'agent-branches'; legacy.mkdir()
        (legacy / 'state.json').write_text('{}')
        refused = self.resolve_run(self.root, self.root, sys.executable, '-c', 'raise SystemExit(99)')
        self.assertEqual(refused.returncode, 2)
        self.assertIn('legacy', refused.stderr)
        (legacy / 'state.json').unlink(); legacy.rmdir()
        (common / 'workspace-lifecycle').mkdir()
        unbound = self.resolve_run(self.root, self.root, sys.executable, '-c', 'raise SystemExit(99)')
        self.assertEqual(unbound.returncode, 2)
        self.assertIn('binding', unbound.stderr)

    def test_resolve_run_refuses_corrupt_git_boundary(self):
        broken = self.base / 'broken'; broken.mkdir(); (broken / '.git').mkdir()
        refused = self.resolve_run(broken, broken, sys.executable, '-c',
                                   'raise SystemExit(99)')
        self.assertEqual(refused.returncode, 2)
        self.assertIn('invalid Git workspace', refused.stderr)

    def test_resolve_run_child_finish_defers_retirement_until_lease_release(self):
        task = self.start('child-finish')
        script = '''
import hashlib, json, os, tempfile
from pathlib import Path
from workspace_lifecycle.service import finish
repo = Path(os.environ['WORKSPACE_LIFECYCLE_REPO'])
target = repo / 'owned.txt'
target.write_text('owned\\n')
plan = Path(tempfile.mkstemp(suffix='.json')[1])
plan.write_text(json.dumps({'commit': [{'path': 'owned.txt', 'classification': 'source',
    'owner': os.environ['WORKSPACE_LIFECYCLE_TASK'], 'evidence': 'child-owned fixture',
    'safe_to_commit': True, 'sha256': hashlib.sha256(target.read_bytes()).hexdigest()}]}))
finish(repo, task=os.environ['WORKSPACE_LIFECYCLE_TASK'], plan_path=str(plan),
       result_ref='issue/child-finish', users_released=True)
'''
        child = self.resolve_run(self.topic, self.topic, sys.executable, '-c', script)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertFalse(self.topic.exists())
        self.assertEqual(git(self.root, 'show', 'HEAD:owned.txt'), 'owned')

    def test_foreign_staged_change_is_never_mixed_into_source_commit(self):
        task = self.start(); (self.topic / "owned.txt").write_text("owned\n"); (self.topic / "foreign.txt").write_text("foreign\n")
        git(self.topic, "add", "foreign.txt")
        plan = self.plan("foreign.json", {"commit": [self.entry(task, "owned.txt")]})
        with self.assertRaises(LifecycleError): finish(self.topic, task=task, plan_path=str(plan), result_ref="issue/dirty")
        self.assertTrue((self.topic / "foreign.txt").exists())
        self.assertIn("foreign.txt", git(self.topic, "diff", "--cached", "--name-only"))

    def test_owner_attestation_and_ignored_source_are_refused(self):
        task = self.start(); (self.topic / "owned.txt").write_text("owned\n")
        wrong = self.plan("wrong.json", {"commit": [self.entry("other", "owned.txt")]})
        with self.assertRaises(LifecycleError): finish(self.topic, task=task, plan_path=str(wrong), result_ref="issue/dirty")
        (self.root / ".git" / "info" / "exclude").write_text("ignored.txt\n")
        (self.topic / "ignored.txt").write_text("private\n")
        ignored = self.plan("ignored.json", {"commit": [self.entry(task, "ignored.txt")]})
        with self.assertRaises(LifecycleError): finish(self.topic, task=task, plan_path=str(ignored), result_ref="issue/dirty")

    def test_restore_tracked_generated_file_with_regeneration_evidence(self):
        task = self.start(); (self.topic / "generated.txt").write_bytes(b"generated changed\n")
        plan = self.plan("restore.json", {"restore": [self.entry(task, "generated.txt", "reproducible", regeneration={"evidence": "rebuild command verified"})]})
        result = finish(self.topic, task=task, plan_path=str(plan), result_ref="issue/dirty")
        self.assertTrue(result["accepted"])
        self.assertEqual((self.topic / "generated.txt").read_text(), "generated base\n")

    def test_private_archive_copies_to_authorized_external_store_before_cleanup(self):
        task = self.start(); (self.topic / "owned.txt").write_text("owned\n"); (self.topic / "secret.bin").write_bytes(b"secret")
        store = self.base / "safe-store"; store.mkdir()
        plan = self.plan("archive.json", {"commit": [self.entry(task, "owned.txt")], "archive": [self.entry(task, "secret.bin", "private", store=str(store), approval_evidence="approved store")]})
        result = finish(self.topic, task=task, plan_path=str(plan), result_ref="issue/dirty")
        self.assertTrue(result["accepted"]); self.assertEqual((store / "secret.bin").read_bytes(), b"secret")
        self.assertFalse((self.topic / "secret.bin").exists())

    def test_safe_owned_action_runs_before_valid_exception(self):
        task = self.start(); (self.topic / "owned.txt").write_text("owned\n"); (self.topic / "unknown.txt").write_text("unknown\n")
        alternatives = [{"category": name, "irreversible_harm": "loss", "evidence": "reviewed"}
                        for name in ("commit", "restore", "archive", "owner-resolution")]
        plan = self.plan("exception.json", {"commit": [self.entry(task, "owned.txt")], "exception": {"reviewed_all_alternatives": True, "alternatives": alternatives, "remaining_owner": "another user", "next_action": "owner resolves unknown.txt"}})
        result = finish(self.topic, task=task, plan_path=str(plan), result_ref="issue/dirty")
        self.assertEqual(result["completion"], "exception")
        self.assertTrue(git(self.topic, "log", "--format=%s", "-1"))
        self.assertTrue((self.topic / "unknown.txt").exists())

    def test_changed_plan_digest_is_refused_after_failed_validation(self):
        gate = self.base / "gate"
        validation = [sys.executable, "-c", "import pathlib,sys;sys.exit(not pathlib.Path(" + repr(str(gate)) + ").exists())"]
        task = self.start(validation=validation); (self.topic / "owned.txt").write_text("owned\n")
        plan = self.plan("digest.json", {"commit": [self.entry(task, "owned.txt")]})
        with self.assertRaises(LifecycleError): finish(self.topic, task=task, plan_path=str(plan), result_ref="issue/dirty")
        altered = self.plan("digest.json", {"commit": [dict(self.entry(task, "owned.txt"), evidence="changed review")]})
        gate.write_text("open")
        with self.assertRaises(LifecycleError): finish(self.topic, task=task, plan_path=str(altered), result_ref="issue/dirty")

    def test_interrupted_commit_receipt_resumes_exact_native_commit(self):
        task = self.start(); (self.topic / "owned.txt").write_text("owned\n")
        plan = self.plan("interrupt.json", {"commit": [self.entry(task, "owned.txt")]})
        import workspace_lifecycle.service as service
        actual, tripped = service.git, {"value": False}
        def interrupted(repo, *args, **kwargs):
            outcome = actual(repo, *args, **kwargs)
            if args[:1] == ("commit",) and not tripped["value"]:
                tripped["value"] = True
                raise KeyboardInterrupt("simulated interruption after native commit")
            return outcome
        with patch("workspace_lifecycle.service.git", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt): finish(self.topic, task=task, plan_path=str(plan), result_ref="issue/dirty")
        result = finish(self.topic, task=task, plan_path=str(plan), result_ref="issue/dirty")
        self.assertTrue(result["accepted"])
        self.assertEqual(git(self.topic, "show", "--format=%s", "-s"), "workspace lifecycle completion")

    def test_supervised_cli_run_then_finish_auto_retires(self):
        task = self.start("supervised")
        environment = dict(__import__("os").environ, PYTHONPATH=str(Path(__file__).parents[1] / "src"))
        child = subprocess.run([sys.executable, "-m", "workspace_lifecycle.cli", "--repo", str(self.topic),
                                "run", "--task", task, "--cwd", str(self.topic), "--", sys.executable, "-c", "pass"],
                               text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment)
        self.assertEqual(child.returncode, 0, child.stderr)
        (self.topic / "owned.txt").write_text("owned\n")
        plan = self.plan("supervised.json", {"commit": [self.entry(task, "owned.txt")]})
        result = finish(self.topic, task=task, plan_path=str(plan), result_ref="issue/supervised", users_released=True)
        self.assertTrue(result["retirement"]["retired"])
        self.assertFalse(self.topic.exists())

    def test_archive_partial_copy_is_resumed_before_source_removal(self):
        task = self.start(); (self.topic / "owned.txt").write_text("owned\n"); (self.topic / "secret.bin").write_bytes(b"secret bytes")
        store = self.base / "archive"; store.mkdir()
        plan = self.plan("partial.json", {"commit": [self.entry(task, "owned.txt")], "archive": [self.entry(task, "secret.bin", "private", store=str(store), approval_evidence="approved")]})
        import workspace_lifecycle.service as service
        def partial(source, destination, *args, **kwargs):
            destination.write(source.read(3)); destination.flush()
            raise KeyboardInterrupt("copy interrupted")
        with patch("workspace_lifecycle.service.shutil.copyfileobj", side_effect=partial):
            with self.assertRaises(KeyboardInterrupt): finish(self.topic, task=task, plan_path=str(plan), result_ref="issue/dirty")
        self.assertTrue((self.topic / "secret.bin").exists())
        self.assertEqual((store / "secret.bin").read_bytes(), b"sec")
        result = finish(self.topic, task=task, plan_path=str(plan), result_ref="issue/dirty")
        self.assertTrue(result["accepted"])
        self.assertEqual((store / "secret.bin").read_bytes(), b"secret bytes")
        self.assertFalse((self.topic / "secret.bin").exists())

    def test_untracked_generated_file_is_removed_only_by_exact_restore_plan(self):
        task = self.start(); (self.topic / "owned.txt").write_text("owned\n"); (self.topic / "generated.out").write_text("generated\n")
        plan = self.plan("untracked.json", {"commit": [self.entry(task, "owned.txt")], "restore": [self.entry(task, "generated.out", "reproducible", regeneration={"evidence": "rebuild tested"})]})
        result = finish(self.topic, task=task, plan_path=str(plan), result_ref="issue/dirty")
        self.assertTrue(result["accepted"])
        self.assertFalse((self.topic / "generated.out").exists())

    def test_owned_tracked_source_deletion_uses_exact_before_image(self):
        task = self.start(); before = (self.topic / "base.txt").read_bytes(); (self.topic / "base.txt").unlink()
        deletion = {"path": "base.txt", "classification": "source", "owner": task, "evidence": "owned removal", "safe_to_commit": True,
                    "deleted": True, "before_sha256": hashlib.sha256(before).hexdigest()}
        plan = self.plan("delete.json", {"commit": [deletion]})
        result = finish(self.topic, task=task, plan_path=str(plan), result_ref="issue/dirty")
        self.assertTrue(result["accepted"])
        self.assertFalse((self.topic / "base.txt").exists())
        with self.assertRaises(subprocess.CalledProcessError):
            git(self.topic, "cat-file", "-e", "HEAD:base.txt")

    def test_interrupted_begin_after_native_worktree_add_resumes_exact_identity(self):
        import workspace_lifecycle.service as service
        actual, tripped = service.git, {"value": False}
        def interrupted(repo, *args, **kwargs):
            outcome = actual(repo, *args, **kwargs)
            if args[:1] == ("config",) and not tripped["value"]:
                tripped["value"] = True
                raise KeyboardInterrupt("binding interruption")
            return outcome
        with patch("workspace_lifecycle.service.git", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt): self.start("begin-retry")
        self.assertTrue(self.topic.exists())
        result = self.start("begin-retry")
        self.assertEqual(result, "begin-retry")
        self.assertEqual(git(self.topic, "branch", "--show-current"), "topic/begin-retry")

    def test_unknown_dirty_can_be_resolved_by_explicit_reviewed_plan_revision(self):
        task = self.start()
        (self.topic / 'owned.txt').write_text('owned\n')
        plan = self.plan("revision.json", {})
        with self.assertRaises(LifecycleError):
            finish(self.topic, task=task, plan_path=str(plan), result_ref='issue/dirty')
        plan.write_text(json.dumps({'commit': [self.entry(task, 'owned.txt')]}))
        with self.assertRaises(LifecycleError):
            finish(self.topic, task=task, plan_path=str(plan), result_ref='issue/dirty')
        result = finish(self.topic, task=task, plan_path=str(plan), result_ref='issue/dirty',
                        revision_evidence='ownership investigation confirms task-authored source')
        self.assertTrue(result['accepted'])


    def _stale_finish_intent(self, task='stale'):
        gate = self.base / (task + '-validation-gate')
        validation = [sys.executable, '-c',
                      'import pathlib,sys;sys.exit(not pathlib.Path(' + repr(str(gate)) + ').exists())']
        self.start(task, validation=validation)
        original = self.plan(task + '-original.json', {'commit': []})
        with self.assertRaises(LifecycleError):
            finish(self.topic, task=task, plan_path=str(original), result_ref='issue/' + task)
        return original, gate

    def _owner_commit(self, name, value):
        (self.topic / name).write_text(value)
        git(self.topic, 'add', name)
        git(self.topic, 'commit', '-m', 'owner ' + name)
        return git(self.topic, 'rev-parse', 'HEAD')

    def _intent(self, task):
        with locked_state(self.topic) as (_, state):
            return json.loads(json.dumps(state['intents'][task]))

    def test_stale_finish_head_revision_recovers_and_preserves_receipt(self):
        task = 'stale-recovery'
        original, gate = self._stale_finish_intent(task)
        old_intent = self._intent(task)
        self._owner_commit('owner-b.txt', 'B\n')
        revised_head = self._owner_commit('owner-c.txt', 'C\n')
        revised = self.plan(task + '-revised.json', {})
        gate.write_text('open\n')

        result = finish(self.topic, task=task, plan_path=str(revised), result_ref='issue/' + task,
                        revision_head=revised_head,
                        revision_evidence='issue review records the owner commits B through C')

        self.assertTrue(result['accepted'])
        self.assertEqual(result['commit'], revised_head)
        self.assertEqual(result['integration']['destination'], 'trunk')
        self.assertEqual((self.root / 'owner-c.txt').read_text(), 'C\n')
        with locked_state(self.topic) as (_, state):
            item = state['tasks'][task]
            replacement = item['finish_receipts'][-1]
            intent = state['intents'].get(task)
        self.assertIsNone(intent)
        self.assertEqual(replacement['initial_head'], old_intent['initial_head'])
        self.assertEqual(replacement['old_initial_head'], old_intent['initial_head'])
        self.assertEqual(replacement['new_initial_head'], revised_head)
        self.assertEqual(replacement['revision_evidence'], 'issue review records the owner commits B through C')
        self.assertEqual(replacement['identity'], old_intent['identity'])
        self.assertIn('revised_at', replacement)
        self.assertIn('revised_plan_digest', replacement)
        self.assertIn('revision_identity', replacement)
        self.assertEqual(replacement['revision_identity']['task'], task)
        self.assertEqual(replacement['revision_identity']['branch'], 'topic/' + task)
        self.assertNotEqual(replacement['plan_digest'], replacement['revised_plan_digest'])

        retired = retire(self.root, task=task, result_ref='issue/' + task, users_released=True)
        self.assertTrue(retired['retired'])
        self.assertEqual(retired['receipt']['finish_receipts'][-1], replacement)
        reclaimed = reclaim(self.root, task=task, result_ref='issue/' + task,
                            preservation_evidence='issue review preserves the retired receipt')
        self.assertTrue(reclaimed['reclaimed'])
        self.assertEqual(reclaimed['receipt']['finish_receipts'][-1], replacement)


    def test_stale_finish_head_revision_accepts_legacy_intent_without_identity(self):
        task = 'stale-legacy'
        original, gate = self._stale_finish_intent(task)
        with locked_state(self.topic) as (directory, state):
            original_digest = state['intents'][task]['plan_digest']
            state['intents'][task].pop('identity')
            save_state(directory, state)
        self._owner_commit('owner-b.txt', 'B\n')
        revised_head = self._owner_commit('owner-c.txt', 'C\n')
        gate.write_text('open\n')

        result = finish(self.topic, task=task, plan_path=str(original), result_ref='issue/' + task,
                        revision_head=revised_head,
                        revision_evidence='issue review records the exact descendant head')

        self.assertTrue(result['accepted'])
        self.assertEqual(result['commit'], revised_head)
        with locked_state(self.topic) as (_, state):
            receipt = state['tasks'][task]['finish_receipts'][-1]
        self.assertNotIn('identity', receipt)
        self.assertEqual(receipt['plan_digest'], original_digest)
        self.assertEqual(receipt['revised_plan_digest'], original_digest)
        self.assertEqual(receipt['revision_identity']['task'], task)
        self.assertEqual(receipt['revision_identity']['branch'], 'topic/' + task)

    def test_stale_finish_head_revision_rejects_started_effects_and_identity_changes(self):
        task = 'stale-guards'
        _, _ = self._stale_finish_intent(task)
        revised_head = self._owner_commit('owner.txt', 'owner\n')
        revised = self.plan(task + '-revised.json', {})
        evidence = 'issue review records the exact descendant head'

        with locked_state(self.topic) as (directory, state):
            state['intents'][task]['actions'] = {'owned.txt': {'phase': 'resolved'}}
            save_state(directory, state)
        with self.assertRaisesRegex(LifecycleError, 'no started effects'):
            finish(self.topic, task=task, plan_path=str(revised), result_ref='issue/' + task,
                   revision_head=revised_head, revision_evidence=evidence)
        with locked_state(self.topic) as (directory, state):
            state['intents'][task]['actions'] = {}
            state['intents'][task]['committed'] = None
            save_state(directory, state)
        with self.assertRaisesRegex(LifecycleError, 'no started effects'):
            finish(self.topic, task=task, plan_path=str(revised), result_ref='issue/' + task,
                   revision_head=revised_head, revision_evidence=evidence)
        with locked_state(self.topic) as (directory, state):
            state['intents'][task].pop('committed')
            state['intents'][task]['commit_tree'] = None
            save_state(directory, state)
        with self.assertRaisesRegex(LifecycleError, 'no started effects'):
            finish(self.topic, task=task, plan_path=str(revised), result_ref='issue/' + task,
                   revision_head=revised_head, revision_evidence=evidence)
        with locked_state(self.topic) as (_, state):
            original_identity = json.loads(json.dumps(state['intents'][task]['identity']))
        for field, value in (('task', 'other-task'), ('branch', 'topic/other'),
                             ('worktree', str(self.base / 'other-worktree'))):
            with self.subTest(identity_field=field):
                with locked_state(self.topic) as (directory, state):
                    state['intents'][task].pop('commit_tree', None)
                    state['intents'][task]['identity'] = dict(original_identity, **{field: value})
                    save_state(directory, state)
                with self.assertRaisesRegex(LifecycleError, 'identity changed'):
                    finish(self.topic, task=task, plan_path=str(revised), result_ref='issue/' + task,
                           revision_head=revised_head, revision_evidence=evidence)

    def test_stale_finish_head_revision_requires_current_descendant_and_durable_evidence(self):
        task = 'stale-inputs'
        _, _ = self._stale_finish_intent(task)
        revised_head = self._owner_commit('owner.txt', 'owner\n')
        revised = self.plan(task + '-revised.json', {})
        with self.assertRaises(LifecycleError):
            finish(self.topic, task=task, plan_path=str(revised), result_ref='issue/' + task,
                   revision_head=revised_head, revision_evidence=' ')
        with self.assertRaises(LifecycleError):
            finish(self.topic, task=task, plan_path=str(revised), result_ref='issue/' + task,
                   revision_head=revised_head)
        with self.assertRaises(LifecycleError):
            finish(self.topic, task=task, plan_path=str(revised), result_ref='other-result',
                   revision_head=revised_head, revision_evidence='issue review records the exact descendant head')

        old_head = self._intent(task)['initial_head']
        git(self.root, 'checkout', '--orphan', 'unrelated-' + task)
        (self.root / 'unrelated.txt').write_text('unrelated\n')
        git(self.root, 'add', '-A')
        git(self.root, 'commit', '-m', 'unrelated owner history')
        unrelated = git(self.root, 'rev-parse', 'HEAD')
        git(self.topic, 'reset', '--hard', unrelated)
        with self.assertRaisesRegex(LifecycleError, 'descend'):
            finish(self.topic, task=task, plan_path=str(revised), result_ref='issue/' + task,
                   revision_head=unrelated, revision_evidence='issue review records an unrelated head')
        self.assertNotEqual(old_head, unrelated)

    def test_stale_finish_head_revision_refuses_active_operations_and_unmerged_index(self):
        task = 'stale-operations'
        _, _ = self._stale_finish_intent(task)
        revised_head = self._owner_commit('owner.txt', 'owner\n')
        revised = self.plan(task + '-revised.json', {})
        evidence = 'issue review records the exact descendant head'
        for marker in ('MERGE_HEAD', 'REBASE_HEAD', 'CHERRY_PICK_HEAD', 'rebase-merge'):
            path = Path(git(self.topic, 'rev-parse', '--path-format=absolute', '--git-path', marker))
            if marker == 'rebase-merge':
                path.mkdir()
            else:
                path.write_text(revised_head + '\n')
            with self.assertRaisesRegex(LifecycleError, 'existing Git operation'):
                finish(self.topic, task=task, plan_path=str(revised), result_ref='issue/' + task,
                       revision_head=revised_head, revision_evidence=evidence)
            if path.is_dir():
                path.rmdir()
            else:
                path.unlink()

        (self.root / 'base.txt').write_text('root\n')
        git(self.root, 'add', 'base.txt')
        git(self.root, 'commit', '-m', 'root conflicts with topic')
        (self.topic / 'base.txt').write_text('topic\n')
        git(self.topic, 'add', 'base.txt')
        git(self.topic, 'commit', '-m', 'topic conflicts with root')
        merge = subprocess.run(['git', '-C', str(self.topic), 'merge', 'trunk'], text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertNotEqual(merge.returncode, 0)
        with self.assertRaisesRegex(LifecycleError, 'unmerged'):
            finish(self.topic, task=task, plan_path=str(revised), result_ref='issue/' + task,
                   revision_head=revised_head, revision_evidence=evidence)
        git(self.topic, 'merge', '--abort')

    def test_stale_finish_head_revision_refuses_race_and_retries_exact_recovery(self):
        task = 'stale-race'
        _, gate = self._stale_finish_intent(task)
        initial_head = self._intent(task)['initial_head']
        self._owner_commit('owner-b.txt', 'B\n')
        revised_head = self._owner_commit('owner-c.txt', 'C\n')
        revised = self.plan(task + '-revised.json', {})
        evidence = 'issue review records the exact descendant head'
        gate.write_text('open\n')
        import workspace_lifecycle.service as service
        actual, raced = service.git, {'value': False}

        def move_after_ancestry(repo, *args, **kwargs):
            outcome = actual(repo, *args, **kwargs)
            if args[:3] == ('merge-base', '--is-ancestor', initial_head) and not raced['value']:
                raced['value'] = True
                (self.topic / 'raced.txt').write_text('race\n')
                git(self.topic, 'add', 'raced.txt')
                git(self.topic, 'commit', '-m', 'owner wins revision race')
            return outcome

        with patch('workspace_lifecycle.service.git', side_effect=move_after_ancestry):
            with self.assertRaisesRegex(LifecycleError, 'HEAD or identity changed'):
                finish(self.topic, task=task, plan_path=str(revised), result_ref='issue/' + task,
                       revision_head=revised_head, revision_evidence=evidence)
        self.assertTrue(raced['value'])
        intent = self._intent(task)
        self.assertNotIn('head_revision', intent)
        self.assertEqual(intent['initial_head'], initial_head)

        current = git(self.topic, 'rev-parse', 'HEAD')
        result = finish(self.topic, task=task, plan_path=str(revised), result_ref='issue/' + task,
                        revision_head=current, revision_evidence=evidence)
        self.assertTrue(result['accepted'])



    def test_stale_finish_head_revision_rejects_late_validation_race(self):
        task = 'stale-late-race'
        _, gate = self._stale_finish_intent(task)
        self._owner_commit('owner-b.txt', 'B\n')
        revised_head = self._owner_commit('owner-c.txt', 'C\n')
        revised = self.plan(task + '-revised.json', {})
        gate.write_text('open\n')
        import workspace_lifecycle.service as service
        actual, validations = service._validate, {'count': 0}

        def move_after_postvalidation(repo, argv):
            outcome = actual(repo, argv)
            validations['count'] += 1
            if validations['count'] == 2:
                self._owner_commit('owner-d.txt', 'D\n')
            return outcome

        with patch('workspace_lifecycle.service._validate', side_effect=move_after_postvalidation):
            with self.assertRaises(LifecycleError):
                finish(self.topic, task=task, plan_path=str(revised), result_ref='issue/' + task,
                       revision_head=revised_head,
                       revision_evidence='issue review records the exact descendant head')
        self.assertEqual(validations['count'], 2)
        with locked_state(self.topic) as (_, state):
            self.assertNotIn('acceptance', state['tasks'][task])
            self.assertEqual(state['intents'][task]['initial_head'], revised_head)
        self.assertFalse((self.root / 'owner-d.txt').exists())


    def test_stale_finish_head_revision_rejects_owner_commit_before_lifecycle_commit(self):
        task = 'stale-commit-race'
        _, gate = self._stale_finish_intent(task)
        self._owner_commit('owner-b.txt', 'B\n')
        revised_head = self._owner_commit('owner-c.txt', 'C\n')
        (self.topic / 'owned.txt').write_text('owned\n')
        revised = self.plan(task + '-revised.json', {'commit': [self.entry(task, 'owned.txt')]})
        gate.write_text('open\n')
        import workspace_lifecycle.service as service
        actual, raced = service.git, {'value': False}

        def owner_before_lifecycle_commit(repo, *args, **kwargs):
            if args[:1] == ('commit',) and not raced['value']:
                raced['value'] = True
                (self.topic / 'owner-d.txt').write_text('D\n')
                git(self.topic, 'add', 'owner-d.txt')
                git(self.topic, 'commit', '--only', '-m', 'owner commits D first', 'owner-d.txt')
            return actual(repo, *args, **kwargs)

        with patch('workspace_lifecycle.service.git', side_effect=owner_before_lifecycle_commit):
            with self.assertRaises(LifecycleError):
                finish(self.topic, task=task, plan_path=str(revised), result_ref='issue/' + task,
                       revision_head=revised_head,
                       revision_evidence='issue review records the exact descendant head')
        self.assertTrue(raced['value'])
        with locked_state(self.topic) as (_, state):
            item = state['tasks'][task]
            intent = state['intents'][task]
            self.assertNotIn('acceptance', item)
            self.assertEqual(len(item['finish_receipts']), 1)
            self.assertEqual(intent['initial_head'], revised_head)
            self.assertEqual(intent['head_revision']['head'], revised_head)
            self.assertNotIn('committed', intent)
        self.assertFalse((self.root / 'owner-d.txt').exists())
        self.assertFalse((self.root / 'owned.txt').exists())


    def test_stale_finish_head_revision_rejects_commit_receipt_race(self):
        task = 'stale-receipt-race'
        _, gate = self._stale_finish_intent(task)
        self._owner_commit('owner-b.txt', 'B\n')
        revised_head = self._owner_commit('owner-c.txt', 'C\n')
        (self.topic / 'owned.txt').write_text('owned\n')
        revised = self.plan(task + '-revised.json', {'commit': [self.entry(task, 'owned.txt')]})
        gate.write_text('open\n')
        import workspace_lifecycle.service as service
        actual, raced = service.git, {'value': False}

        def owner_after_commit_verification(repo, *args, **kwargs):
            outcome = actual(repo, *args, **kwargs)
            if (args[:1] == ('rev-parse',) and args[1].endswith('^{tree}')
                    and not raced['value']):
                raced['value'] = True
                (self.topic / 'owner-d.txt').write_text('D\n')
                git(self.topic, 'add', 'owner-d.txt')
                git(self.topic, 'commit', '-m', 'owner commits D after receipt verification')
            return outcome

        with patch('workspace_lifecycle.service.git', side_effect=owner_after_commit_verification):
            with self.assertRaises(LifecycleError):
                finish(self.topic, task=task, plan_path=str(revised), result_ref='issue/' + task,
                       revision_head=revised_head,
                       revision_evidence='issue review records the exact descendant head')
        self.assertTrue(raced['value'])
        with locked_state(self.topic) as (_, state):
            item = state['tasks'][task]
            intent = state['intents'][task]
            self.assertNotIn('acceptance', item)
            self.assertEqual(len(item['finish_receipts']), 1)
            self.assertEqual(intent['initial_head'], revised_head)
            self.assertEqual(intent['head_revision']['head'], revised_head)
            self.assertEqual(intent['committed'], git(self.topic, 'rev-parse', 'HEAD~1'))
        self.assertFalse((self.root / 'owner-d.txt').exists())
        self.assertFalse((self.root / 'owned.txt').exists())

    def test_stale_finish_head_revision_retries_interrupted_integration(self):
        task = 'stale-integration'
        _, gate = self._stale_finish_intent(task)
        self._owner_commit('owner-b.txt', 'B\n')
        revised_head = self._owner_commit('owner-c.txt', 'C\n')
        revised = self.plan(task + '-revised.json', {})
        evidence = 'issue review records the exact descendant head'
        gate.write_text('open\n')
        import workspace_lifecycle.service as service
        actual, interrupted = service._integrate, {'value': False}

        def fail_once(*args, **kwargs):
            if not interrupted['value']:
                interrupted['value'] = True
                raise LifecycleError('simulated interruption after acceptance')
            return actual(*args, **kwargs)

        with patch('workspace_lifecycle.service._integrate', side_effect=fail_once):
            with self.assertRaisesRegex(LifecycleError, 'after acceptance'):
                finish(self.topic, task=task, plan_path=str(revised), result_ref='issue/' + task,
                       revision_head=revised_head, revision_evidence=evidence)
        with locked_state(self.topic) as (_, state):
            self.assertNotIn(task, state['intents'])
            receipt_count = len(state['tasks'][task]['finish_receipts'])
            receipt = state['tasks'][task]['finish_receipts'][-1]
            self.assertEqual(state['tasks'][task]['acceptance']['commit'], revised_head)
        result = finish(self.topic, task=task, plan_path=str(revised), result_ref='issue/' + task,
                        revision_head=revised_head, revision_evidence=evidence)
        self.assertTrue(result['accepted'])
        self.assertEqual(result['integration']['destination'], 'trunk')
        with locked_state(self.topic) as (_, state):
            self.assertEqual(len(state['tasks'][task]['finish_receipts']), receipt_count)
            self.assertEqual(state['tasks'][task]['finish_receipts'][-1], receipt)

    def test_stale_finish_head_revision_retries_and_resumes_after_own_commit(self):
        task = 'stale-resume'
        _, gate = self._stale_finish_intent(task)
        self._owner_commit('owner-b.txt', 'B\n')
        revised_head = self._owner_commit('owner-c.txt', 'C\n')
        (self.topic / 'owned.txt').write_text('owned\n')
        revised = self.plan(task + '-revised.json', {'commit': [self.entry(task, 'owned.txt')]})
        evidence = 'issue review records the exact descendant head'
        gate.write_text('open\n')
        import workspace_lifecycle.service as service
        actual, interrupted = service.git, {'value': False}

        def crash_after_own_commit(repo, *args, **kwargs):
            outcome = actual(repo, *args, **kwargs)
            if args[:1] == ('commit',) and not interrupted['value']:
                interrupted['value'] = True
                raise KeyboardInterrupt('crash after lifecycle commit')
            return outcome

        with patch('workspace_lifecycle.service.git', side_effect=crash_after_own_commit):
            with self.assertRaises(KeyboardInterrupt):
                finish(self.topic, task=task, plan_path=str(revised), result_ref='issue/' + task,
                       revision_head=revised_head, revision_evidence=evidence)
        with self.assertRaises(LifecycleError):
            finish(self.topic, task=task, plan_path=str(revised), result_ref='issue/' + task,
                   revision_head=git(self.topic, 'rev-parse', 'HEAD'), revision_evidence=evidence)
        with self.assertRaises(LifecycleError):
            finish(self.topic, task=task, plan_path=str(revised), result_ref='issue/' + task,
                   revision_head=revised_head, revision_evidence='different evidence')
        result = finish(self.topic, task=task, plan_path=str(revised), result_ref='issue/' + task,
                        revision_head=revised_head, revision_evidence=evidence)
        self.assertTrue(result['accepted'])
        self.assertEqual(git(self.topic, 'show', 'HEAD:owned.txt'), 'owned')
