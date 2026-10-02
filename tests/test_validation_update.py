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
        self.assertEqual(receipt['identity']['worktree'], str(self.f.topic))
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

    def test_pending_failed_finish_is_preserved(self):
        plan = self.f.topic.parent / 'plan.json'
        plan.write_text('{}')
        missing = ['git', 'cat-file', '-e', 'HEAD:missing-required-file']
        self.update(validation=missing)
        with self.assertRaisesRegex(LifecycleError, 'configured validation failed'):
            service.finish(self.f.topic, task='current', plan_path=str(plan), result_ref='issue/11')
        before = self.state.read_bytes()
        with self.assertRaisesRegex(LifecycleError, 'pending'):
            self.update(expected_validation=missing)
        self.assertEqual(self.state.read_bytes(), before)
        self.assertIn('intent', service.status(self.f.topic, 'current'))

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
