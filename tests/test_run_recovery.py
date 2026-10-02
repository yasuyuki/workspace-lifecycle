"""Managed-run recovery stays local to the selected task (issue #9)."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from workspace_lifecycle import leases
from workspace_lifecycle.git import worktree_records
from workspace_lifecycle.service import begin, finish
from workspace_lifecycle.state import locked_state, save_state

import test_lifecycle as lifecycle


class RunRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = lifecycle.LifecycleTest('runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root = self.fixture.root
        self.topic = self.fixture.topic
        self.base = Path(self.fixture.temp.name)
        self.remote = self.fixture.remote
        self.environment = dict(os.environ)

    def start(self, task, worktree=None):
        begin(self.root, task=task, request='issue/' + task, remote='origin',
              branch='topic/' + task, worktree=str(worktree or self.topic),
              validation=['git', 'diff', '--check'], preflight=self.fixture.preflight)

    def cli(self, *args, cwd=None):
        return subprocess.run([sys.executable, '-m', 'workspace_lifecycle', *args],
                              text=True, capture_output=True, env=self.environment, cwd=cwd)

    def without_remote(self):
        offline = self.base / 'offline.git'
        self.remote.rename(offline)
        self.addCleanup(lambda: offline.exists() and offline.rename(self.remote))

    def remove_default_worktree(self):
        lifecycle.run(self.root, 'checkout', '-b', 'integration/hold')
        self.assertFalse(any(record.get('branch') == 'refs/heads/trunk'
                             for record in worktree_records(self.root)))

    def restore_default_worktree(self):
        lifecycle.run(self.root, 'checkout', 'trunk')

    def test_active_run_and_resolve_run_do_not_require_remote_default(self):
        self.start('active')
        self.remove_default_worktree()
        records = worktree_records(self.root)
        self.without_remote()
        direct = self.cli('--repo', str(self.topic), 'run', '--task', 'active', '--cwd', str(self.topic), '--',
                          sys.executable, '-c', 'import os,sys; print(os.getcwd()); print("direct"); sys.exit(17)')
        self.assertEqual(direct.returncode, 17, direct.stderr)
        self.assertEqual(direct.stdout.splitlines(), [str(self.topic.resolve()), 'direct'])
        self.assertIsNone(leases.status(self.topic, 'active')['receipt'])

        resolved = self.cli('resolve-run', '--cwd', str(self.topic), '--launch-cwd', str(self.topic), '--',
                            sys.executable, '-c', 'import os,sys; print(os.getcwd()); print("resolved"); sys.exit(19)')
        self.assertEqual(resolved.returncode, 19, resolved.stderr)
        self.assertEqual(resolved.stdout.splitlines(), [str(self.topic.resolve()), 'resolved'])
        self.assertIsNone(leases.status(self.topic, 'active')['receipt'])
        self.assertEqual(worktree_records(self.root), records)

    def test_active_run_does_not_replay_another_tasks_pending_release(self):
        self.start('selected')
        with locked_state(self.root) as (directory, state):
            state['tasks']['other'] = {**state['tasks']['selected'],
                                       'completion_release': {'result_ref': 'issue/other'}}
            save_state(directory, state)

        child = self.cli('--repo', str(self.topic), 'run', '--task', 'selected', '--cwd', str(self.topic), '--',
                         sys.executable, '-c', 'print("selected")')
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(child.stdout, 'selected\n')
        with locked_state(self.root) as (_, state):
            self.assertEqual(state['tasks']['other']['completion_release'], {'result_ref': 'issue/other'})
            self.assertNotIn('recovery_failure', state['tasks']['other']['completion_release'])

    def test_run_rejects_a_pending_task_bound_to_another_worktree_before_recovery(self):
        self.start('a')
        other = self.base / 'other'
        self.start('b', other)
        with locked_state(self.root) as (directory, state):
            state['tasks']['b']['completion_release'] = {'result_ref': 'issue/b'}
            save_state(directory, state)

        sentinel = self.base / 'wrong-task-spawned'
        refused = self.cli('--repo', str(self.topic), 'run', '--task', 'b', '--cwd', str(self.topic), '--',
                           sys.executable, '-c', 'from pathlib import Path; Path(%r).write_text("spawned")' % str(sentinel))
        self.assertEqual(refused.returncode, 2, refused.stderr)
        self.assertIn('exact bound task checkout', refused.stderr)
        self.assertFalse(sentinel.exists())
        with locked_state(self.root) as (_, state):
            self.assertEqual(state['tasks']['b']['completion_release'], {'result_ref': 'issue/b'})
        self.assertTrue(other.exists())

    def test_selected_pending_release_is_durable_when_remote_is_missing_then_recovers(self):
        task = 'pending'
        self.start(task)
        owned = self.topic / 'owned.txt'
        owned.write_text('owned\n')
        self.fixture.task = task
        plan = self.base / 'pending-plan.json'
        plan.write_text(json.dumps({'commit': [self.fixture.source_plan(self.topic, 'owned.txt')['commit'][0]]}))
        finish(self.topic, task=task, plan_path=str(plan), result_ref='issue/pending')
        with locked_state(self.root) as (directory, state):
            state['tasks'][task]['completion_release'] = {'result_ref': 'issue/pending'}
            save_state(directory, state)

        sentinel = self.base / 'spawned'
        self.remove_default_worktree()
        self.without_remote()
        refused = self.cli('resolve-run', '--cwd', str(self.topic), '--launch-cwd', str(self.topic), '--',
                           sys.executable, '-c', 'from pathlib import Path; Path(%r).write_text("spawned")' % str(sentinel),
                           cwd=self.topic)
        self.assertEqual(refused.returncode, 2, refused.stderr)
        self.assertFalse(sentinel.exists())
        with locked_state(self.root) as (_, state):
            release = state['tasks'][task]['completion_release']
            self.assertEqual(release['result_ref'], 'issue/pending')
            self.assertTrue(release['recovery_failure'])

        self.remote.parent.joinpath('offline.git').rename(self.remote)
        still_mainless = self.cli('resolve-run', '--cwd', str(self.topic), '--launch-cwd', str(self.topic), '--',
                                  sys.executable, '-c', 'from pathlib import Path; Path(%r).write_text("spawned")' % str(sentinel),
                                  cwd=self.topic)
        self.assertEqual(still_mainless.returncode, 2, still_mainless.stderr)
        self.assertIn('worktree', still_mainless.stderr)
        self.assertFalse(sentinel.exists())

        self.restore_default_worktree()
        if os.name == 'nt':
            recovered = self.cli('--repo', str(self.topic), 'run', '--task', task, '--cwd', str(self.topic), '--',
                                 sys.executable, '-c', 'from pathlib import Path; Path(%r).write_text("spawned")' % str(sentinel),
                                 cwd=self.root)
        else:
            recovered = self.cli('resolve-run', '--cwd', str(self.topic), '--launch-cwd', str(self.topic), '--',
                                 sys.executable, '-c', 'from pathlib import Path; Path(%r).write_text("spawned")' % str(sentinel),
                                 cwd=self.topic)
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertFalse(sentinel.exists())


if __name__ == '__main__':
    unittest.main()
