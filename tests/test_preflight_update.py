"""The public preflight migration preserves an existing task's Git and work state."""
import json
import subprocess
import sys
import unittest

from workspace_lifecycle.errors import LifecycleError
from workspace_lifecycle.leases import guard
from workspace_lifecycle.service import begin, finish, hold, retire, status, update_preflight
from workspace_lifecycle.state import locked_state, save_state
import test_lifecycle


class PreflightUpdateTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_lifecycle.LifecycleTest('runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.f = self.fixture
        begin(self.f.root, task='current', request='issue/16', remote='origin',
              branch='topic/current', worktree=str(self.f.topic),
              validation=['git', 'diff', '--check'], preflight=self.f.preflight)
        (self.f.topic / 'dirty.txt').write_text('retain this work\n')
        self.old = list(self.f.preflight)
        self.new = [sys.executable, '-m', 'workspace_lifecycle.push', '{repo}',
                    '--user-intent', 'hold']

    def test_public_command_updates_only_preflight_and_evidence(self):
        before = status(self.f.topic, 'current')
        command = [sys.executable, '-m', 'workspace_lifecycle', '--repo', str(self.f.topic),
                   'update-preflight', '--task', 'current',
                   '--expected-preflight-json', json.dumps(self.old),
                   '--preflight-json', json.dumps(self.new), '--evidence', 'issue/16 migration']
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        after = status(self.f.topic, 'current')
        self.assertEqual(after['preflight'], self.new)
        self.assertEqual(after['preflight_update']['from'], self.old)
        self.assertEqual(after['preflight_update']['to'], self.new)
        self.assertEqual(after['preflight_update']['evidence'], 'issue/16 migration')
        for key in ('preflight', 'preflight_update'):
            before.pop(key, None); after.pop(key, None)
        self.assertEqual(after, before)
        self.assertEqual((self.f.topic / 'dirty.txt').read_text(), 'retain this work\n')
        # A stale caller cannot overwrite the first migration.
        stale = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(stale.returncode, 2)
        self.assertIn('CAS mismatch', stale.stderr)

    def test_invalid_input_and_unknown_task_are_rejected(self):
        cases = [([], self.new, 'expected preflight'),
                 (self.old, [], 'preflight'),
                 (self.old, ['python', '-m', 'workspace_lifecycle.push'], '{repo}'),
                 (self.old, ['python', '', '{repo}'], 'preflight')]
        for expected, new, reason in cases:
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(LifecycleError, reason):
                    update_preflight(self.f.topic, task='current', expected_preflight=expected,
                                     preflight=new, evidence='issue/16')
        with self.assertRaisesRegex(LifecycleError, 'evidence'):
            update_preflight(self.f.topic, task='current', expected_preflight=self.old,
                             preflight=self.new, evidence=' ')
        with self.assertRaisesRegex(LifecycleError, 'unknown task'):
            update_preflight(self.f.topic, task='retired-or-absent', expected_preflight=self.old,
                             preflight=self.new, evidence='issue/16')
        self.assertEqual(status(self.f.topic, 'current')['preflight'], self.old)

    def test_active_lease_and_pending_operation_are_rejected(self):
        with guard(self.f.topic, 'current'):
            with self.assertRaisesRegex(LifecycleError, 'in use'):
                update_preflight(self.f.topic, task='current', expected_preflight=self.old,
                                 preflight=self.new, evidence='issue/16')
        for kind in ('begin', 'adopt', 'finish'):
            with locked_state(self.f.topic) as (directory, state):
                state['intents']['current'] = {'kind': kind}
                save_state(directory, state)
            with self.assertRaisesRegex(LifecycleError, 'pending'):
                update_preflight(self.f.topic, task='current', expected_preflight=self.old,
                                 preflight=self.new, evidence='issue/16')
        with locked_state(self.f.topic) as (directory, state):
            state['intents'].pop('current')
            state['tasks']['current']['retire'] = {'phase': 'requested'}
            save_state(directory, state)
        with self.assertRaisesRegex(LifecycleError, 'pending'):
            update_preflight(self.f.topic, task='current', expected_preflight=self.old,
                             preflight=self.new, evidence='issue/16')
        self.assertEqual(status(self.f.topic, 'current')['preflight'], self.old)

    def test_accepted_held_task_keeps_receipts_and_retired_task_is_refused(self):
        (self.f.topic / 'dirty.txt').unlink()
        feature = self.f.topic / 'feature.txt'
        feature.write_text('accepted work\n')
        self.f.task = 'current'
        plan = self.f.topic.parent / 'preflight-plan.json'
        plan.write_text(json.dumps(self.f.source_plan(self.f.topic, 'feature.txt')))
        finish(self.f.topic, task='current', plan_path=str(plan), result_ref='issue/16')
        hold(self.f.topic, 'current', 'external review', 'wait for owner')
        before = status(self.f.topic, 'current')
        update_preflight(self.f.topic, task='current', expected_preflight=self.old,
                         preflight=self.new, evidence='issue/16 owner migration')
        after = status(self.f.topic, 'current')
        self.assertEqual(after['acceptance'], before['acceptance'])
        self.assertEqual(after['hold'], before['hold'])
        self.assertEqual(after['integrated'], before['integrated'])
        self.assertEqual(after['live'], before['live'])
        self.assertEqual(after['baseline_dirty'], before['baseline_dirty'])
        # The accepted task remains current until its explicit retirement.
        from workspace_lifecycle.service import release_hold
        release_hold(self.f.root, 'current', 'fixture owner released hold')
        retire(self.f.root, task='current', result_ref='issue/16', users_released=True)
        with self.assertRaisesRegex(LifecycleError, 'unknown task'):
            update_preflight(self.f.root, task='current', expected_preflight=self.new,
                             preflight=self.old, evidence='issue/16')


if __name__ == '__main__':
    unittest.main()
