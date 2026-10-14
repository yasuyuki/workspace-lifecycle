"""Validation corrections retain the continuing task and require real validation."""
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from workspace_lifecycle import service
from workspace_lifecycle.errors import LifecycleError
from workspace_lifecycle.git import head, git
from workspace_lifecycle.leases import guard
from workspace_lifecycle.state import state_dir
import test_lifecycle


class ValidationUpdateTests(unittest.TestCase):
    def setUp(self):
        self.f = test_lifecycle.LifecycleTest('runTest')
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.old = ['git', 'diff', '--check', '--', 'obsolete-input']
        self.new = ['git', 'diff', '--check']
        service.begin(self.f.root, task='current', request='issue/11', remote='origin',
                      branch='topic/current', worktree=str(self.f.topic),
                      validation=self.old, preflight=self.f.preflight)
        self.head = head(self.f.topic)
        self.state = state_dir(self.f.topic) / 'state.json'

    def update(self, **overrides):
        args = dict(task='current', expected_head=self.head,
                    expected_validation=self.old, validation=self.new, evidence='issue/11 correction')
        args.update(overrides)
        return service.update_validation(self.f.topic, **args)

    def test_public_command_preserves_task_and_dirty_state(self):
        (self.f.topic / 'dirty.txt').write_text('retain work\n')
        service.hold(self.f.topic, 'current', 'external acceptance pending', 'await owner')
        before = service.status(self.f.topic, 'current')
        command = [sys.executable, '-m', 'workspace_lifecycle', '--repo', str(self.f.topic),
                   'update-validation', '--task', 'current', '--expected-head', self.head,
                   '--expected-validation-json', json.dumps(self.old),
                   '--validation-json', json.dumps(self.new), '--evidence', 'issue/11 correction']
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        after = service.status(self.f.topic, 'current')
        self.assertEqual(after['validation'], self.new)
        receipt = after.pop('validation_updates')[0]
        self.assertEqual(receipt['from'], self.old)
        self.assertEqual(receipt['to'], self.new)
        self.assertEqual(receipt['evidence'], 'issue/11 correction')
        self.assertEqual(receipt['head'], self.head)
        self.assertEqual(receipt['identity']['worktree'], str(self.f.topic.resolve()))
        before.pop('validation'); after.pop('validation')
        self.assertEqual(after, before)
        self.assertEqual((self.f.topic / 'dirty.txt').read_text(), 'retain work\n')
        stale = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(stale.returncode, 2)
        self.assertIn('CAS mismatch', stale.stderr)

    def test_invalid_arguments_leave_state_unchanged(self):
        before = self.state.read_bytes()
        for args in (dict(expected_validation=[]), dict(validation=[]), dict(validation=['git', '']),
                     dict(evidence=' '), dict(expected_head='HEAD'), dict(task='absent')):
            with self.subTest(args=args), self.assertRaises(LifecycleError):
                self.update(**args)
            self.assertEqual(self.state.read_bytes(), before)

    def test_subdirectory_command_records_worktree_root_identity(self):
        child = self.f.topic / 'subdirectory'
        child.mkdir()
        command = [sys.executable, '-m', 'workspace_lifecycle', '--repo', str(child),
                   'update-validation', '--task', 'current', '--expected-head', self.head,
                   '--expected-validation-json', json.dumps(self.old),
                   '--validation-json', json.dumps(self.new), '--evidence', 'issue/11 correction']
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        identity = service.status(self.f.topic, 'current')['validation_updates'][0]['identity']
        root = self.f.topic.resolve()
        self.assertEqual(identity['worktree'], str(root))
        self.assertEqual(identity['worktree_id'], [root.stat().st_dev, root.stat().st_ino])

    def test_empty_old_registration_can_be_corrected(self):
        from workspace_lifecycle.state import locked_state, save_state
        with locked_state(self.f.topic) as (directory, state):
            state['tasks']['current']['validation'] = []
            save_state(directory, state)
        self.update(expected_validation=[])
        self.assertEqual(service.status(self.f.topic, 'current')['validation'], self.new)

    def test_active_use_and_git_operations_are_refused(self):
        before = self.state.read_bytes()
        with guard(self.f.topic, 'current'):
            with self.assertRaisesRegex(LifecycleError, 'in use'):
                self.update()
        for name in ('MERGE_HEAD', 'index.lock'):
            path = Path(git(self.f.topic, 'rev-parse', '--path-format=absolute', '--git-path', name))
            path.write_text(self.head + '\n')
            try:
                with self.assertRaisesRegex(LifecycleError, 'existing Git operation'):
                    self.update()
            finally:
                path.unlink()
        self.assertEqual(self.state.read_bytes(), before)

    def test_wrong_checkout_and_stale_head_are_refused(self):
        before = self.state.read_bytes()
        with self.assertRaisesRegex(LifecycleError, 'bound task worktree'):
            service.update_validation(self.f.root, task='current', expected_head=self.head,
                                      expected_validation=self.old, validation=self.new, evidence='issue/11')
        git(self.f.topic, 'commit', '--allow-empty', '-m', 'new work')
        with self.assertRaisesRegex(LifecycleError, 'HEAD mismatch'):
            self.update()
        self.assertEqual(self.state.read_bytes(), before)

    def test_identity_change_during_update_is_refused(self):
        original = service._finish_identity
        count = 0
        def changed(*args):
            nonlocal count
            identity = original(*args)
            count += 1
            if count > 1:
                identity['worktree_id'] = [0, 0]
            return identity
        before = self.state.read_bytes()
        with patch.object(service, '_finish_identity', side_effect=changed):
            with self.assertRaisesRegex(LifecycleError, 'identity changed'):
                self.update()
        self.assertEqual(self.state.read_bytes(), before)

    def failed_finish(self):
        """Register a validator that cannot pass and leave its untouched finish intent."""
        missing = ['git', 'cat-file', '-e', 'HEAD:missing-required-file']
        self.update(validation=missing)
        (self.f.topic / 'feature.txt').write_text('done\n')
        self.f.task = 'current'
        self.plan = self.f.topic.parent / 'plan.json'
        self.plan.write_text(json.dumps(self.f.source_plan(self.f.topic, 'feature.txt')))
        with self.assertRaisesRegex(LifecycleError, 'configured validation failed'):
            service.finish(self.f.topic, task='current', plan_path=str(self.plan), result_ref='issue/31')
        self.intent = service.status(self.f.topic, 'current')['intent']
        return missing

    def edit_intent(self, change):
        from workspace_lifecycle.state import locked_state, save_state
        with locked_state(self.f.topic) as (directory, state):
            change(state['intents']['current'])
            save_state(directory, state)

    def test_effectless_failed_finish_is_corrected_accepted_and_retired(self):
        missing = self.failed_finish()
        self.update(expected_validation=missing, evidence='issue/31 correction')
        current = service.status(self.f.topic, 'current')
        self.assertNotIn('intent', current)
        released = current['finish_receipts'][-1]
        self.assertEqual({k: released[k] for k in self.intent}, self.intent)
        self.assertEqual(released['validation_update']['from'], missing)
        self.assertEqual(released['validation_update']['to'], self.new)
        self.assertEqual(released['validation_update']['evidence'], 'issue/31 correction')
        self.assertEqual((self.f.topic / 'feature.txt').read_text(), 'done\n')
        service.update_preflight(self.f.topic, task='current', expected_preflight=self.f.preflight,
                                 preflight=list(self.f.preflight), evidence='issue/31 preflight')
        service.update_required_checks(self.f.topic, task='current', expected=[],
                                       required_checks=[], evidence='issue/31 checks')
        result = service.finish(self.f.topic, task='current', plan_path=str(self.plan), result_ref='issue/31')
        self.assertTrue(result['accepted'])
        self.assertEqual(service.status(self.f.topic, 'current')['acceptance']['validation']['postcommit']['argv'], self.new)
        self.assertEqual((self.f.root / 'feature.txt').read_text(), 'done\n')
        retired = service.retire(self.f.root, task='current', result_ref='issue/31', users_released=True)
        self.assertTrue(retired['retired'])
        self.assertEqual(retired['receipt']['finish_receipts'][-1], released)

    def test_finish_intent_with_effects_or_other_identity_is_kept(self):
        missing = self.failed_finish()
        other = '0' * len(self.head)
        changes = {
            'commit tree': lambda i: i.update(commit_tree=other),
            'commit': lambda i: i.update(committed=other),
            'archive': lambda i: i['actions'].update({'feature.txt': {'kind': 'archive', 'phase': 'copying'}}),
            'restore': lambda i: i['actions'].update({'feature.txt': {'kind': 'restore', 'phase': 'resolved'}}),
            'synchronization': lambda i: i.update(synchronization={}),
            'unknown field': lambda i: i.update(unexpected=True),
            'missing identity': lambda i: i.pop('identity'),
            'other identity': lambda i: i['identity'].update(worktree_id=[0, 0]),
            'other initial HEAD': lambda i: i.update(initial_head=other),
            'other revision HEAD': lambda i: i.update(head_revision={'head': other, 'evidence': 'x'}),
            'merge': lambda i: i.update(kind='merge'),
        }
        for name, change in changes.items():
            with self.subTest(name):
                self.edit_intent(change)
                before = self.state.read_bytes()
                with self.assertRaisesRegex(LifecycleError, 'pending|accepted'):
                    self.update(expected_validation=missing)
                self.assertEqual(self.state.read_bytes(), before)
                self.edit_intent(lambda i: (i.clear(), i.update(json.loads(json.dumps(self.intent)))))
        self.update(expected_validation=missing)
        self.assertNotIn('intent', service.status(self.f.topic, 'current'))

    def test_effectless_release_refuses_staged_index_lease_and_stale_cas(self):
        missing = self.failed_finish()
        before = self.state.read_bytes()
        git(self.f.topic, 'add', 'feature.txt')
        with self.assertRaisesRegex(LifecycleError, 'pending'):
            self.update(expected_validation=missing)
        git(self.f.topic, 'reset', '-q', '--', 'feature.txt')
        with guard(self.f.topic, 'current'):
            with self.assertRaisesRegex(LifecycleError, 'in use'):
                self.update(expected_validation=missing)
        with self.assertRaisesRegex(LifecycleError, 'CAS mismatch'):
            self.update(expected_validation=self.old)
        with patch.object(service, 'save_state', side_effect=OSError('interrupted save')):
            with self.assertRaisesRegex(LifecycleError, 'interrupted save'):
                self.update(expected_validation=missing)
        self.assertEqual(self.state.read_bytes(), before)
        self.update(expected_validation=missing)
        with self.assertRaisesRegex(LifecycleError, 'CAS mismatch'):
            self.update(expected_validation=missing)
        self.assertEqual(len(service.status(self.f.topic, 'current')['finish_receipts']), 1)

    def test_atomic_save_interruption_preserves_registration(self):
        before = self.state.read_bytes()
        with patch.object(service, 'save_state', side_effect=OSError('interrupted save')):
            with self.assertRaisesRegex(LifecycleError, 'interrupted save'):
                self.update()
        self.assertEqual(self.state.read_bytes(), before)
        self.update()
        self.assertEqual(service.status(self.f.topic, 'current')['validation'], self.new)

    def test_real_validator_and_acceptance_are_not_reinterpreted(self):
        (self.f.topic / 'required.json').write_text('{"source": "candidate", "required": true}\n')
        self.new = [sys.executable, '-c', "import json,pathlib; "
                    "assert json.loads(pathlib.Path('required.json').read_text()) "
                    "== {'source': 'candidate', 'required': True}"]
        self.update()
        plan = self.f.topic.parent / 'plan.json'
        self.f.task = 'current'
        plan.write_text(json.dumps(self.f.source_plan(self.f.topic, 'required.json')))
        self.assertTrue(service.finish(self.f.topic, task='current', plan_path=str(plan),
                                       result_ref='issue/11')['accepted'])
        current = service.status(self.f.topic, 'current')
        self.assertEqual(current['acceptance']['validation']['postcommit']['argv'], self.new)
        self.assertEqual(current['integrated']['validation']['argv'], self.new)
        before = self.state.read_bytes()
        with self.assertRaisesRegex(LifecycleError, 'accepted'):
            self.update(expected_validation=self.new, validation=['git', 'diff', '--check'])
        self.assertEqual(self.state.read_bytes(), before)

    def test_different_task_and_detached_checkout_are_refused(self):
        other = self.f.topic.parent / 'other'
        service.begin(self.f.root, task='other', request='issue/other', remote='origin',
                      branch='topic/other', worktree=str(other),
                      validation=self.old, preflight=self.f.preflight)
        before = self.state.read_bytes()
        with self.assertRaisesRegex(LifecycleError, 'bound task worktree'):
            self.update(task='other')
        git(self.f.topic, 'switch', '--detach', self.head)
        with self.assertRaises(LifecycleError):
            self.update()
        self.assertEqual(self.state.read_bytes(), before)

    def test_multiple_corrections_keep_previous_receipts(self):
        self.update()
        self.update(expected_validation=self.new, validation=self.old, evidence='issue/11 follow-up')
        receipts = service.status(self.f.topic, 'current')['validation_updates']
        self.assertEqual(len(receipts), 2)
        self.assertEqual(receipts[0]['evidence'], 'issue/11 correction')
        self.assertEqual(receipts[1]['evidence'], 'issue/11 follow-up')


if __name__ == '__main__':
    unittest.main()
