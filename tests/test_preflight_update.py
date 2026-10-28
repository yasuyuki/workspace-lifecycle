"""The public preflight migration preserves an existing task's Git and work state."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest import mock

import workspace_lifecycle
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


class LifecyclePythonTests(unittest.TestCase):
    """A task whose preflight names {lifecycle_python} survives install changes untouched."""
    def setUp(self):
        self.f = test_lifecycle.LifecycleTest('runTest')
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.base = Path(self.f.temp.name)
        self.f.task = 'moved'
        self.placeholder = ['{lifecycle_python}', '-m', 'workspace_lifecycle.push', '{repo}',
                            '--user-intent', 'push']

    def install(self, name, *, without=()):
        """An isolated lifecycle install: its own interpreter and its own package copy."""
        home = self.base / name
        subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(home)], check=True)
        python = home / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
        purelib = subprocess.run([str(python), '-c', 'import sysconfig; print(sysconfig.get_paths()["purelib"])'],
                                 check=True, capture_output=True, text=True).stdout.strip()
        package = Path(purelib) / 'workspace_lifecycle'
        shutil.copytree(Path(workspace_lifecycle.__file__).parent, package,
                        ignore=shutil.ignore_patterns('__pycache__'))
        for module in without:
            (package / module).unlink()
        return python

    def cli(self, python, *argv, repo=None):
        return subprocess.run([str(python), '-m', 'workspace_lifecycle', '--repo', str(repo or self.f.topic), *argv],
                              capture_output=True, text=True, cwd=self.base)

    def begin(self, python, preflight):
        begun = self.cli(python, 'begin', '--task', 'moved', '--request', 'issue/25', '--remote', 'origin',
                         '--branch', 'topic/moved', '--worktree', str(self.f.topic),
                         '--validation-json', '["git", "diff", "--check"]',
                         '--preflight-json', json.dumps(preflight), repo=self.f.root)
        self.assertEqual(begun.returncode, 0, begun.stderr)

    def plan(self, name):
        (self.f.topic / name).write_text(name + '\n')
        plan = self.base / (name + '.plan.json')
        plan.write_text(json.dumps(self.f.source_plan(self.f.topic, name)))
        return str(plan)

    def finish(self, python, plan):
        return self.cli(python, 'finish', '--task', 'moved', '--plan', plan, '--result-ref', 'issue/25')

    def remote_branches(self):
        return test_lifecycle.run(self.f.root, 'ls-remote', '--heads', 'origin')

    def test_finish_uses_the_new_install_after_the_old_one_is_gone(self):
        old, new = self.install('old'), self.install('new')
        incomplete = self.install('incomplete', without=('push.py',))
        self.begin(old, self.placeholder)
        shutil.rmtree(old.parents[1])
        plan = self.plan('moved.txt')
        before = self.remote_branches()
        refused = self.finish(incomplete, plan)
        self.assertEqual(refused.returncode, 2, refused.stdout)
        self.assertIn('push preflight failed', refused.stderr)
        self.assertEqual(self.remote_branches(), before)
        finished = self.finish(new, plan)
        self.assertEqual(finished.returncode, 0, finished.stderr)
        self.assertTrue(json.loads(finished.stdout)['accepted'])
        self.assertEqual((self.f.root / 'moved.txt').read_text(), 'moved.txt\n')
        self.assertEqual(status(self.f.topic, 'moved')['preflight'], self.placeholder)

    def test_preflight_runs_on_the_finishing_interpreter_with_other_elements_unchanged(self):
        new = self.install('new')
        observed = self.base / 'observed.json'
        script = ('import json, subprocess, sys; '
                  'open(sys.argv[2], "a").write(json.dumps({"python": sys.executable, "argv": sys.argv[1:]}) + "\\n"); '
                  'b = subprocess.check_output(["git", "branch", "--show-current"], text=True).strip(); '
                  'print(json.dumps({"decision": "push", "reason": "fixture", '
                  '"push_argv": ["git", "push", "origin", "HEAD:refs/heads/" + b]}))')
        self.begin(sys.executable, ['{lifecycle_python}', '-c', script, '{repo}', str(observed), 'x{lifecycle_python}'])
        finished = self.finish(new, self.plan('seen.txt'))
        self.assertEqual(finished.returncode, 0, finished.stderr)
        calls = [json.loads(line) for line in observed.read_text().splitlines()]
        self.assertEqual({Path(call['python']).resolve() for call in calls}, {new.resolve()})
        self.assertEqual({Path(call['argv'][0]).resolve() for call in calls},
                         {self.f.topic.resolve(), self.f.root.resolve()})
        self.assertEqual({tuple(call['argv'][1:]) for call in calls}, {(str(observed), 'x{lifecycle_python}')})

    def test_pinned_install_fails_closed_until_one_cas_update(self):
        old, new = self.install('old'), self.install('new')
        pinned = [str(old), '-m', 'workspace_lifecycle.push', '{repo}', '--user-intent', 'push']
        self.begin(old, pinned)
        migrated = self.cli(new, 'update-preflight', '--task', 'moved',
                            '--expected-preflight-json', json.dumps(pinned),
                            '--preflight-json', json.dumps(self.placeholder), '--evidence', 'issue/25 migration')
        self.assertEqual(migrated.returncode, 0, migrated.stderr)
        self.assertEqual(status(self.f.topic, 'moved')['preflight_update']['from'], pinned)
        shutil.rmtree(old.parents[1])
        finished = self.finish(new, self.plan('migrated.txt'))
        self.assertEqual(finished.returncode, 0, finished.stderr)
        self.assertTrue(json.loads(finished.stdout)['accepted'])

    def test_missing_pinned_interpreter_refuses_push(self):
        gone = self.base / 'gone' / 'python'
        self.begin(sys.executable, [str(gone), '-m', 'workspace_lifecycle.push', '{repo}'])
        refused = self.finish(sys.executable, self.plan('t.txt'))
        self.assertEqual(refused.returncode, 2, refused.stdout)
        self.assertNotIn('topic/moved', self.remote_branches())

    def test_unknown_own_interpreter_refuses_push(self):
        self.begin(sys.executable, self.placeholder)
        plan = self.plan('t.txt')
        with mock.patch('sys.executable', ''):
            with self.assertRaisesRegex(LifecycleError, 'own interpreter'):
                finish(self.f.topic, task='moved', plan_path=plan, result_ref='issue/25')
        self.assertNotIn('topic/moved', self.remote_branches())

    def test_finish_pushes_only_when_decision_and_exit_status_agree(self):
        from workspace_lifecycle.service import _push
        self.begin(sys.executable, self.placeholder)
        decided = lambda decision: json.dumps(
            {'decision': decision, 'reason': 'FIXTURE', 'remote': 'origin', 'destination': 'topic/moved',
             **({'push_argv': ['git', 'push', 'origin', 'HEAD:refs/heads/topic/moved']} if decision == 'push' else {})})
        def scripted(stdout, code, before=''):
            return [sys.executable, '-c', f'import subprocess, sys; {before}sys.stdout.write({stdout!r}); sys.exit({code})', '{repo}']
        cli = [sys.executable, '-m', 'workspace_lifecycle.push', '{repo}']
        cases = [(cli + ['--user-intent', 'hold'], 'push blocked: USER_HOLD'),
                 (cli + ['--temporary'], 'push blocked: TEMPORARY_COMMIT'),
                 (scripted(decided('hold'), 10), 'push blocked: FIXTURE'),
                 (scripted(decided('ask'), 11), 'push blocked: FIXTURE'),
                 (scripted(decided('hold'), 0), 'push blocked: FIXTURE'),
                 (scripted(decided('ask'), 0), 'push blocked: FIXTURE'),
                 (scripted(decided('push'), 10), 'exit 10 contradicts decision push'),
                 (scripted(decided('hold'), 11), 'exit 11 contradicts decision hold'),
                 (scripted(decided('maybe'), 0), 'did not emit a decision'),
                 (scripted('noise\n' + decided('push'), 0), 'did not emit a decision'),
                 (scripted('[]', 0), 'did not emit a decision'),
                 (scripted('', 1), 'push preflight failed with exit 1'),
                 (scripted(decided('push'), 0, 'subprocess.run(["git", "remote", "set-url", "origin", "elsewhere"]); '),
                  'remote URL changed during push preflight'),
                 (scripted(decided('push'), 0, 'subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "moved"]); '),
                  'HEAD or branch changed')]
        url = test_lifecycle.run(self.f.topic, 'remote', 'get-url', 'origin')
        tip = test_lifecycle.run(self.f.topic, 'rev-parse', 'HEAD')
        for preflight, message in cases:
            with self.subTest(message=message, preflight=preflight[2:]):
                with self.assertRaisesRegex(LifecycleError, message):
                    _push(self.f.topic, preflight, 'topic/moved', 'origin', tip)
                test_lifecycle.run(self.f.topic, 'remote', 'set-url', 'origin', url)
                test_lifecycle.run(self.f.topic, 'reset', '-q', '--hard', tip)
                self.assertNotIn('topic/moved', self.remote_branches())
        pushed = _push(self.f.topic, scripted(decided('push'), 0), 'topic/moved', 'origin', tip)
        self.assertEqual((pushed['decision'], pushed['remote_observed']), ('push', tip))


if __name__ == '__main__':
    unittest.main()
