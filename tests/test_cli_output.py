"""Public CLI output retains durable evidence without printing full receipts."""
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from workspace_lifecycle import service

import test_lifecycle as lifecycle


class CompactCliOutputTests(unittest.TestCase):
    def setUp(self):
        self.fixture = lifecycle.LifecycleTest('runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root, self.topic = self.fixture.root, self.fixture.topic
        self.base = Path(self.fixture.temp.name)

    def start(self, task='compact'):
        service.begin(self.root, task=task, request='issue/' + task, remote='origin',
                      branch='topic/' + task, worktree=str(self.topic),
                      validation=['git', 'diff', '--check'], preflight=self.fixture.preflight)
        self.fixture.task = task
        (self.topic / 'feature.txt').write_text(task + '\n')
        plan = self.base / (task + '-plan.json')
        plan.write_text(json.dumps(self.fixture.source_plan(self.topic, 'feature.txt')))
        return plan

    def invoke(self, *args, cwd=None, repo=None):
        completed = subprocess.run([sys.executable, '-m', 'workspace_lifecycle',
                                    '--repo', str(repo or self.root), *args], text=True,
                                   capture_output=True, cwd=cwd or self.root)
        self.assertTrue(completed.stdout, 'public CLI must write one JSON outcome: ' + completed.stderr)
        return completed, json.loads(completed.stdout)

    def receipt_ref(self, task):
        common = Path(lifecycle.run(self.root, 'rev-parse', '--path-format=absolute',
                                    '--git-common-dir'))
        return {'path': str((common / 'workspace-lifecycle' / 'state.json').resolve()),
                'selector': ['retired', task]}

    def assert_ref(self, value, task):
        self.assertEqual(set(value), {'path', 'selector'},
                         'receipt_ref must expose exactly path and selector')
        self.assertEqual(value, self.receipt_ref(task),
                         'receipt_ref must identify the durable retired receipt')

    def assert_compact(self, value, location='outcome'):
        if isinstance(value, dict):
            for key, child in value.items():
                self.assertTrue(key == 'receipt_ref' or 'receipt' not in key,
                                location + ' must not expose a receipt body')
                self.assert_compact(child, location + '.' + key)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                self.assert_compact(child, location + '[' + str(index) + ']')

    def read_receipt(self, reference):
        value = json.loads(Path(reference['path']).read_text())
        for key in reference['selector']:
            value = value[key]
        return value

    def assert_complete_receipt(self, reference):
        receipt = self.read_receipt(reference)
        self.assertTrue(receipt['content_manifest'])
        self.assertTrue(receipt['admin_manifest'])
        self.assertTrue(receipt['payload_progress'])
        self.assertTrue(receipt['admin_progress'])
        self.assertEqual(receipt['reclaim_phase'], 'reclaimed')

    def test_finish_retires_and_reclaims_with_a_durable_compact_receipt_ref(self):
        plan = self.start()
        completed, body = self.invoke('finish', '--task', 'compact', '--plan', str(plan),
                                      '--result-ref', 'issue/compact', '--users-released',
                                      '--reclaim-preservation-evidence', 'issue/compact evidence', repo=self.topic)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(body['ok']); self.assertTrue(body['accepted'])
        retirement = body['retirement']
        self.assertEqual(set(retirement), {'task', 'retired', 'branch', 'receipt_ref', 'reclaim'},
                         'finish retirement must expose only its public outcome and receipt_ref')
        self.assertEqual(set(retirement['reclaim']), {'task', 'reclaimed', 'receipt_ref'},
                         'finish reclaim must expose only its public outcome and receipt_ref')
        self.assertTrue(retirement['retired']); self.assertTrue(retirement['reclaim']['reclaimed'])
        self.assert_ref(retirement['receipt_ref'], 'compact')
        self.assertEqual(retirement['reclaim']['receipt_ref'], retirement['receipt_ref'])
        self.assert_compact(body)
        self.assert_complete_receipt(retirement['receipt_ref'])
        self.assertFalse(self.topic.exists(), 'a retired task path is intentionally absent')

    def test_retire_and_idempotent_reclaim_keep_one_durable_ref(self):
        plan = self.start()
        finished, finish_body = self.invoke('finish', '--task', 'compact', '--plan', str(plan),
                                            '--result-ref', 'issue/compact', repo=self.topic)
        self.assertEqual(finished.returncode, 0, finished.stderr); self.assertTrue(finish_body['accepted'])
        retired, retire_body = self.invoke('retire', '--task', 'compact', '--result-ref', 'issue/compact',
                                            '--users-released')
        self.assertEqual(retired.returncode, 0, retired.stderr)
        self.assertEqual(set(retire_body), {'ok', 'task', 'retired', 'branch', 'receipt_ref'},
                         'retire must expose exactly its compact public outcome')
        self.assert_ref(retire_body['receipt_ref'], 'compact')
        reclaimed, reclaim_body = self.invoke('reclaim', '--task', 'compact', '--result-ref', 'issue/compact',
                                               '--preservation-evidence', 'issue/compact evidence')
        again, again_body = self.invoke('reclaim', '--task', 'compact', '--result-ref', 'issue/compact',
                                        '--preservation-evidence', 'issue/compact evidence')
        self.assertEqual(reclaimed.returncode, 0, reclaimed.stderr); self.assertEqual(again.returncode, 0, again.stderr)
        for body in (reclaim_body, again_body):
            self.assertEqual(set(body), {'ok', 'task', 'reclaimed', 'receipt_ref'},
                             'reclaim must expose exactly its compact public outcome')
            self.assertTrue(body['reclaimed']); self.assert_ref(body['receipt_ref'], 'compact')
            self.assert_compact(body)
        self.assertEqual(reclaim_body['receipt_ref'], retire_body['receipt_ref'])
        self.assertEqual(again_body['receipt_ref'], retire_body['receipt_ref'])
        self.assert_complete_receipt(retire_body['receipt_ref'])

    def test_pending_retirement_keeps_compact_ref_and_failure_exit_status(self):
        plan = self.start()
        finished, _ = self.invoke('finish', '--task', 'compact', '--plan', str(plan), '--result-ref', 'issue/compact', repo=self.topic)
        self.assertEqual(finished.returncode, 0, finished.stderr)
        requested, requested_body = self.invoke('retire', '--task', 'compact', '--result-ref', 'issue/compact',
                                                '--users-released', '--request',
                                                '--reclaim-preservation-evidence', 'issue/compact evidence')
        self.assertEqual(requested.returncode, 0, requested.stderr)
        self.assertEqual(requested_body, {'ok': True, 'task': 'compact', 'pending': True})
        from workspace_lifecycle.cli import main
        with patch.object(service, 'reclaim', side_effect=OSError('controlled reclaim fault')):
            with redirect_stdout(io.StringIO()) as out:
                code = main(['--repo', str(self.root), 'retire', '--pending'])
        body = json.loads(out.getvalue())
        self.assertEqual(code, 1, 'pending reclaim failures must retain the CLI failure status')
        self.assertEqual(set(body), {'ok', 'pending'}, 'pending retirement must expose only public outcomes')
        self.assertFalse(body['ok']); self.assertEqual(len(body['pending']), 1)
        entry = body['pending'][0]
        self.assertEqual(set(entry), {'task', 'retired', 'branch', 'receipt_ref', 'reclaim'},
                         'pending retirement must preserve its recovery reference without a receipt body')
        self.assertTrue(entry['retired']); self.assert_ref(entry['receipt_ref'], 'compact')
        self.assertEqual(entry['reclaim'], {'task': 'compact', 'reclaimed': False,
                                            'error': 'controlled reclaim fault',
                                            'receipt_ref': self.receipt_ref('compact')})
        self.assert_compact(body)
        with patch.object(service, 'reclaim', side_effect=OSError('controlled reclaim retry fault')):
            with redirect_stdout(io.StringIO()) as out:
                code = main(['--repo', str(self.root), 'reclaim', '--pending'])
        failed = json.loads(out.getvalue())
        self.assertEqual(code, 1); self.assertFalse(failed['ok'])
        self.assertEqual(failed['pending'], [{'task': 'compact', 'reclaimed': False,
            'error': 'controlled reclaim retry fault', 'receipt_ref': self.receipt_ref('compact')}])
        completed, reclaimed = self.invoke('reclaim', '--pending')
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(reclaimed['ok'])
        self.assertEqual(len(reclaimed['pending']), 1)
        entry = reclaimed['pending'][0]
        self.assertEqual(set(entry), {'task', 'reclaimed', 'receipt_ref'})
        self.assertTrue(entry['reclaimed']); self.assert_ref(entry['receipt_ref'], 'compact')
        self.assert_complete_receipt(entry['receipt_ref'])

    def test_finish_in_task_directory_preserves_next_command_then_returns_compact_ref(self):
        plan = self.start()
        completed, body = self.invoke('finish', '--task', 'compact', '--plan', str(plan),
                                      '--result-ref', 'issue/compact', '--users-released',
                                      '--reclaim-preservation-evidence', 'issue/compact evidence', cwd=self.topic, repo=self.topic)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        retirement = body['retirement']
        self.assertEqual(set(retirement), {'pending', 'retired', 'reason', 'next'},
                         'in-task finish must retain its exact public pending outcome')
        self.assertTrue(retirement['pending']); self.assertFalse(retirement['retired'])
        self.assertEqual(set(retirement['next']), {'cwd', 'argv'},
                         'retirement next must retain its exact external invocation')
        self.assertEqual(retirement['next']['cwd'], str(self.root.resolve()))
        self.assertEqual(retirement['next']['argv'], [sys.executable, '-m', 'workspace_lifecycle', '--repo',
                                                       str(self.root.resolve()), 'retire', '--task', 'compact',
                                                       '--result-ref', 'issue/compact'])
        resumed = subprocess.run(retirement['next']['argv'], text=True, capture_output=True,
                                 cwd=retirement['next']['cwd'])
        resumed_body = json.loads(resumed.stdout)
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(set(resumed_body), {'ok', 'task', 'retired', 'branch', 'receipt_ref', 'reclaim'},
                         'the external next command must return a compact retired outcome')
        self.assert_ref(resumed_body['receipt_ref'], 'compact')
        self.assert_compact(resumed_body)

    def test_managed_recovery_stderr_is_compact_and_keeps_child_result(self):
        self.start('managed')
        reference = self.receipt_ref('managed')
        with service.locked_state(self.root) as (directory, state):
            state.setdefault('retired', {})['managed'] = {'content_manifest': [['feature.txt', 'file', 'hash']],
                'admin_manifest': [['index', 'file', 'hash']],
                'payload_progress': {'members': {'feature.txt': {'state': 'kept'}}},
                'admin_progress': {'members': {'index': {'state': 'kept'}}}, 'reclaim_phase': 'requested'}
            service.save_state(directory, state)
        report = {'retire': {'pending': []}, 'reclaim': {'pending': [
            {'task': 'managed', 'reclaimed': False, 'receipt': self.read_receipt(reference),
             'error': 'controlled recovery fault',
             'future_internal_detail': {'members': ['private member detail']}}]}}
        from workspace_lifecycle import cli, leases
        for command in ('run', 'resolve-run'):
            with self.subTest(command=command):
                recoveries = iter(({'terminal': False, 'error': None, 'report': {}},
                                   {'terminal': False, 'error': 'controlled recovery fault', 'report': report}))
                def child(*_args, **_kwargs):
                    print('child stdout'); print('child stderr', file=sys.stderr); return 23
                argv = (['--repo', str(self.topic), 'run', '--task', 'managed', '--cwd', str(self.topic), '--']
                        if command == 'run' else ['resolve-run', '--cwd', str(self.topic), '--launch-cwd', str(self.topic), '--'])
                argv += [sys.executable, '-c', 'ignored']
                with patch.object(cli, '_recover_selected_task', side_effect=lambda *_: next(recoveries)), \
                     patch.object(leases, 'run', side_effect=child), \
                     redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
                    code = cli.main(argv)
                lines = err.getvalue().splitlines()
                self.assertEqual(code, 23); self.assertEqual(out.getvalue(), 'child stdout\n')
                self.assertEqual(lines[0], 'child stderr')
                management = json.loads(lines[1])
                self.assertEqual(set(management), {'retire', 'reclaim'},
                                 command + ' recovery report must expose exactly its two outcomes')
                self.assertEqual(management['retire'], {'pending': []})
                entry = management['reclaim']['pending'][0]
                self.assertEqual(set(entry), {'task', 'reclaimed', 'receipt_ref', 'error'},
                                 command + ' recovery entry must replace receipt with receipt_ref')
                self.assertEqual(entry['task'], 'managed')
                self.assertFalse(entry['reclaimed'])
                self.assertEqual(entry['error'], 'controlled recovery fault')
                self.assert_ref(entry['receipt_ref'], 'managed')
                self.assert_compact(management, command + ' recovery stderr')


if __name__ == '__main__':
    unittest.main()
