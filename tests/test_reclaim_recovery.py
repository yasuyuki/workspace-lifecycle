"""End-to-end recovery boundaries for resumable reclaim (issue #146)."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from workspace_lifecycle.errors import LifecycleError
from workspace_lifecycle import service
from workspace_lifecycle.service import begin, finish, reclaim, retire

import test_lifecycle as lifecycle


class ReclaimRecoveryTests(unittest.TestCase):
    evidence = "issue/146 preservation complete"

    def setUp(self):
        self.fixture = lifecycle.LifecycleTest('runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root = self.fixture.root
        self.topic = self.fixture.topic
        self.temp = self.fixture.temp

    def _retire(self, task='one'):
        begin(self.root, task=task, request='issue/146', remote='origin',
              branch='topic/' + task, worktree=str(self.topic),
              validation=['git', 'diff', '--check'], preflight=self.fixture.preflight)
        (self.topic / 'first.txt').write_text('first\n')
        (self.topic / 'nested').mkdir()
        (self.topic / 'nested' / 'second.txt').write_text('second\n')
        self.fixture.task = task
        plan = Path(self.temp.name) / (task + '-plan.json')
        plan.write_text(json.dumps({
            'commit': [
                self.fixture.source_plan(self.topic, 'first.txt')['commit'][0],
                self.fixture.source_plan(self.topic, 'nested/second.txt')['commit'][0],
            ]
        }))
        finish(self.topic, task=task, plan_path=str(plan), result_ref='issue/146')
        return retire(self.root, task=task, result_ref='issue/146', users_released=True)['receipt']

    def _state(self):
        common = subprocess.check_output(
            ['git', '-C', str(self.root), 'rev-parse', '--path-format=absolute', '--git-common-dir'],
            text=True).strip()
        return json.loads((Path(common) / 'workspace-lifecycle' / 'state.json').read_text())

    def _wait_for_marker(self, child, marker):
        deadline = time.monotonic() + 15
        while not marker.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(marker.exists(), 'reclaim child did not reach its durable checkpoint')
        self.assertIsNone(child.poll(), 'reclaim child exited before termination')

    def _killed_cli_reclaim(self, checkpoint, *, require_phase=None, occurrence=1):
        child = self._paused_cli_reclaim(checkpoint, require_phase=require_phase, occurrence=occurrence)
        marker = child.lifecycle_marker
        child.kill()
        child.wait(timeout=15)
        self.assertNotEqual(child.returncode, 0)
        return marker

    def _paused_cli_reclaim(self, checkpoint, *, require_phase=None, occurrence=1):
        marker = Path(self.temp.name) / ('paused-' + checkpoint)
        state = Path(subprocess.check_output(
            ['git', '-C', str(self.root), 'rev-parse', '--path-format=absolute', '--git-common-dir'],
            text=True).strip()) / 'workspace-lifecycle' / 'state.json'
        script = r'''
import json, sys, time
from pathlib import Path
from workspace_lifecycle import reclamation
from workspace_lifecycle.cli import main
repo, marker, state, event, phase, wanted = sys.argv[1:]
matches = 0
def hook(found, path):
    global matches
    if found == event and (not phase or json.loads(Path(state).read_text())['retired']['one']['reclaim_phase'] == phase):
        matches += 1
        if matches == int(wanted):
            Path(marker).write_text(path)
            while True: time.sleep(.05)
reclamation._checkpoint = hook
raise SystemExit(main(['--repo', repo, 'reclaim', '--task', 'one', '--result-ref', 'issue/146',
                       '--preservation-evidence', 'issue/146 preservation complete']))
'''
        child = subprocess.Popen([sys.executable, '-c', script, str(self.root), str(marker),
                                  str(state), checkpoint, require_phase or '', str(occurrence)],
                                 env={**os.environ, 'PYTHONPATH': str(Path(__file__).parents[1] / 'src')})
        self.addCleanup(lambda: child.poll() is None and child.kill())
        self._wait_for_marker(child, marker)
        child.lifecycle_marker = marker
        return child

    def _reclaim_pending_cli(self):
        # The pending command must resume only the receipt already authorized by
        # the killed child; it receives no new task, reference, or evidence.
        completed = subprocess.run(
            [sys.executable, '-m', 'workspace_lifecycle', '--repo', str(self.root), 'reclaim', '--pending'],
            text=True, capture_output=True, env={**os.environ, 'PYTHONPATH': str(Path(__file__).parents[1] / 'src')})
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        return json.loads(completed.stdout)

    def test_killed_payload_removal_resumes_and_preserves_unrelated_state(self):
        receipt = self._retire()
        immutable_content = copy.deepcopy(receipt['content_manifest'])
        immutable_admin = copy.deepcopy(receipt['admin_manifest'])
        # The second removal is in the middle of a multi-file payload.  The
        # unlink has happened, but its completed status has not been saved.
        child = self._paused_cli_reclaim('after-member-remove', require_phase='renamed', occurrence=2)
        paused = self._state()['retired']['one']
        self.assertEqual(paused['content_manifest'], immutable_content)
        self.assertEqual(paused['admin_manifest'], immutable_admin)
        self.assertTrue(any(row.get('intent') for row in paused['payload_progress']['members'].values()))

        # The reclaimer holds only the per-task lease; another task can make a
        # normal durable update while this task is paused inside deletion.
        other = Path(self.temp.name) / 'other'
        begin(self.root, task='other', request='issue/147', remote='origin', branch='topic/other',
              worktree=str(other), validation=['git', 'diff', '--check'], preflight=self.fixture.preflight)
        service.hold(other, 'other', 'test concurrent state update', 'resume after reclaim')
        self.assertEqual(service.status(self.root, 'other')['completion'], 'active')

        child.kill()
        child.wait(timeout=15)

        resumed = self._reclaim_pending_cli()
        self.assertTrue(resumed['pending'][0]['reclaimed'], resumed)
        final = self._state()
        self.assertEqual(final['tasks']['other']['hold']['reason'], 'test concurrent state update')
        self.assertEqual(final['retired']['one']['content_manifest'], immutable_content)
        self.assertEqual(final['retired']['one']['admin_manifest'], immutable_admin)
        self.assertEqual(final['retired']['one']['reclaim_phase'], 'reclaimed')

    def test_simultaneous_reclaim_of_one_task_is_refused_by_its_lease(self):
        self._retire()
        child = self._paused_cli_reclaim('before-member-remove', require_phase='renamed')
        competing = subprocess.run(
            [sys.executable, '-m', 'workspace_lifecycle', '--repo', str(self.root), 'reclaim', '--pending'],
            text=True, capture_output=True, env={**os.environ, 'PYTHONPATH': str(Path(__file__).parents[1] / 'src')})
        self.assertNotEqual(competing.returncode, 0)
        self.assertIn('another lifecycle operation', competing.stdout + competing.stderr)
        try:
            child.kill()
            child.wait(timeout=15)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=15)
        self.assertTrue(self._reclaim_pending_cli()['pending'][0]['reclaimed'])

    def test_killed_admin_removal_rejects_replacement_and_keeps_receipt(self):
        receipt = self._retire()
        marker = self._killed_cli_reclaim('before-member-remove', require_phase='admin-removing')
        state = self._state()['retired']['one']
        archive = Path(receipt['admin_archive_path'])
        # At this point the checkpoint has an intent and the targeted name is a
        # deterministic quarantine entry. Replacing it must not be treated as a
        # completed deletion on the next run.
        if os.name == 'nt':
            replacement = archive / marker.read_text()
        else:
            member = next(item for item in state['admin_progress']['members'].values()
                          if item.get('status') == 'quarantined')
            archive_root = archive if archive.exists() else archive.parent / state['admin_progress']['root']['tombstone']
            replacement = next(archive_root.rglob(member['tombstone']))
        replacement.unlink()
        replacement.write_text('replacement\n')
        failed = subprocess.run(
            [sys.executable, '-m', 'workspace_lifecycle', '--repo', str(self.root), 'reclaim', '--pending'],
            text=True, capture_output=True, env={**os.environ, 'PYTHONPATH': str(Path(__file__).parents[1] / 'src')})
        self.assertNotEqual(failed.returncode, 0)
        self.assertRegex(failed.stdout + failed.stderr,
                         '(quarantined (member identity|file)|handle identity) changed')
        self.assertTrue(replacement.exists())
        after = self._state()['retired']['one']
        self.assertEqual(after['admin_manifest'], receipt['admin_manifest'])
        self.assertEqual(after['reclaim_phase'], 'admin-removing')

    def test_killed_after_admin_root_removal_completes_from_durable_intent(self):
        receipt = self._retire()
        self._killed_cli_reclaim('after-root-remove', require_phase='admin-removing')
        state = self._state()['retired']['one']
        self.assertTrue(state['admin_progress']['root']['remove_intent'])
        self.assertFalse(Path(receipt['admin_archive_path']).exists())
        resumed = self._reclaim_pending_cli()
        self.assertTrue(resumed['pending'][0]['reclaimed'])
        self.assertEqual(self._state()['retired']['one']['reclaim_phase'], 'reclaimed')

    def test_save_failure_before_member_removal_keeps_payload(self):
        receipt = self._retire()
        original_save = service.save_state
        calls = {'count': 0}

        def fail_before_delete(*args, **kwargs):
            calls['count'] += 1
            if calls['count'] == 3:
                raise OSError('simulated durable state failure')
            return original_save(*args, **kwargs)

        with patch.object(service, 'save_state', side_effect=fail_before_delete):
            with self.assertRaisesRegex(LifecycleError, 'simulated durable state failure'):
                reclaim(self.root, task='one', result_ref='issue/146', preservation_evidence=self.evidence)
        recovery = Path(receipt['recovery_path'])
        staging = recovery.parent / (recovery.name + '.reclaim')
        retained = staging if staging.exists() else recovery
        self.assertTrue((retained / 'first.txt').exists())
        self.assertTrue((retained / 'nested' / 'second.txt').exists())

    def test_missing_member_without_durable_intent_fails_closed(self):
        receipt = self._retire()
        recovery = Path(receipt['recovery_path'])
        (recovery / 'first.txt').unlink()
        with self.assertRaises(LifecycleError):
            reclaim(self.root, task='one', result_ref='issue/146', preservation_evidence=self.evidence)
        self.assertTrue(Path(receipt['admin_archive_path']).exists())
        self.assertEqual(self._state()['retired']['one']['reclaim_phase'], 'requested')

    @unittest.skipUnless(os.name == 'nt', 'requires a Windows share-mode handle')
    def test_windows_handle_without_delete_share_leaves_reclaim_pending_until_closed(self):
        """Use CreateFileW rather than Python's permissive file sharing mode."""
        import ctypes
        from ctypes import wintypes

        receipt = self._retire()
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                wintypes.HANDLE]
        create_file.restype = wintypes.HANDLE
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        handle = create_file(str(Path(receipt['recovery_path']) / 'first.txt'), 0x80000000,
                             0x00000001 | 0x00000002, None, 3, 0x80, None)
        self.assertNotEqual(handle, wintypes.HANDLE(-1).value)
        try:
            with self.assertRaises(LifecycleError):
                reclaim(self.root, task='one', result_ref='issue/146', preservation_evidence=self.evidence)
            self.assertNotEqual(self._state()['retired']['one']['reclaim_phase'], 'reclaimed')
        finally:
            self.assertTrue(close_handle(handle))
        result = reclaim(self.root, task='one', result_ref='issue/146', preservation_evidence=self.evidence)
        self.assertTrue(result['reclaimed'])

    def test_finish_release_survives_callback_crash_and_retire_pending_resumes(self):
        begin(self.root, task='one', request='issue/146', remote='origin', branch='topic/one',
              worktree=str(self.topic), validation=['git', 'diff', '--check'], preflight=self.fixture.preflight)
        (self.topic / 'finish.txt').write_text('done\n')
        self.fixture.task = 'one'
        plan = Path(self.temp.name) / 'finish-plan.json'
        plan.write_text(json.dumps({'commit': [self.fixture.source_plan(self.topic, 'finish.txt')['commit'][0]]}))
        with patch.object(service, '_integrate', side_effect=OSError('simulated callback crash')):
            with self.assertRaisesRegex(OSError, 'simulated callback crash'):
                finish(self.topic, task='one', plan_path=str(plan), result_ref='issue/146', users_released=True)
        self.assertEqual(self._state()['tasks']['one']['completion_release']['result_ref'], 'issue/146')
        with self.assertRaises(LifecycleError):
            service.before_run(self.topic, 'one')
        replayed = service.retire_pending(self.root)
        self.assertTrue(replayed['pending'][0]['retired'])
        self.assertNotIn('one', self._state()['tasks'])

    @unittest.skipUnless(os.name == 'posix' and Path('/proc').is_dir(), 'requires POSIX lease identity checks')
    def test_resolve_run_recovers_killed_released_supervisor_without_spawning(self):
        begin(self.root, task='one', request='issue/146', remote='origin', branch='topic/one',
              worktree=str(self.topic), validation=['git', 'diff', '--check'], preflight=self.fixture.preflight)
        marker = Path(self.temp.name) / 'finish-gap.pid'
        script = r'''
import hashlib, json, os, time
from pathlib import Path
from workspace_lifecycle import service
from workspace_lifecycle.service import finish
repo = Path(os.environ['WORKSPACE_LIFECYCLE_REPO'])
task = os.environ['WORKSPACE_LIFECYCLE_TASK']
marker = Path(os.environ['MARKER'])
source = repo / 'source.txt'; source.write_text('source\n')
plan = Path(os.environ['PLAN'])
plan.write_text(json.dumps({'commit': [{'path': 'source.txt', 'classification': 'source', 'owner': task,
    'evidence': 'child fixture', 'safe_to_commit': True,
    'sha256': hashlib.sha256(source.read_bytes()).hexdigest()}]}))
def stop(*args):
    marker.write_text(str(os.getpid()))
    while True: time.sleep(.05)
service._integrate = stop
finish(repo, task=task, plan_path=str(plan), result_ref='issue/146', users_released=True)
'''
        command = [sys.executable, '-m', 'workspace_lifecycle', '--repo', str(self.topic), 'run',
                   '--task', 'one', '--cwd', str(self.topic), '--', sys.executable, '-c', script]
        parent = subprocess.Popen(command, env={**os.environ, 'PYTHONPATH': str(Path(__file__).parents[1] / 'src'),
                                                 'MARKER': str(marker), 'PLAN': str(Path(self.temp.name) / 'plan.json')})
        self.addCleanup(lambda: parent.poll() is None and parent.kill())
        self._wait_for_marker(parent, marker)
        parent.kill(); parent.wait(timeout=15)
        child_pid = int(marker.read_text())
        os.kill(child_pid, 9)
        deadline = time.monotonic() + 10
        while Path('/proc', str(child_pid)).exists() and time.monotonic() < deadline:
            stat = Path('/proc', str(child_pid), 'stat')
            if stat.exists() and stat.read_text().rsplit(')', 1)[1].split()[0] == 'Z':
                break
            time.sleep(.02)
        recovered = subprocess.run([sys.executable, '-m', 'workspace_lifecycle', 'resolve-run',
                                    '--cwd', str(self.topic), '--launch-cwd', str(self.topic), '--',
                                    sys.executable, '-c', 'raise SystemExit(99)'], text=True, capture_output=True,
                                   env={**os.environ, 'PYTHONPATH': str(Path(__file__).parents[1] / 'src')})
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertFalse(self.topic.exists())


if __name__ == '__main__':
    unittest.main()
