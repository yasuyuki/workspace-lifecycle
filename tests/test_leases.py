"""Native process fixtures; Windows exercises jobs, Linux exercises process groups."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from workspace_lifecycle import leases


class LeaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='lifecycle lease ')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / 'repo'
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)

    def supervisor(self, script):
        code = ('from workspace_lifecycle import leases; import sys; '
                'sys.exit(leases.run(sys.argv[1], "task", [sys.executable, "-c", sys.argv[2]], sys.argv[1]))')
        child = subprocess.Popen([sys.executable, '-c', code, str(self.repo), script],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(self.stop, child)
        return child

    @staticmethod
    def stop(child):
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        if child.stdout:
            child.stdout.close()
        if child.stderr:
            child.stderr.close()

    def use(self):
        uses = leases.status(self.repo, 'task')['uses']
        self.assertLessEqual(len(uses), 1, uses)
        return uses[0] if uses else None

    def until(self, predicate):
        deadline = time.monotonic() + 15
        while not predicate():
            if time.monotonic() > deadline:
                self.fail('native lease fixture did not reach expected state')
            time.sleep(.02)

    def test_mutation_guard_excludes_process_and_releases_on_exit(self):
        gate = self.repo / 'gate'
        script = 'import pathlib,time; p=pathlib.Path("gate"); p.write_text("ready");\nwhile p.exists(): time.sleep(.02)'
        child = self.supervisor(script)
        self.until(gate.exists)
        self.assertTrue(leases.is_in_use(self.repo, 'task'))
        with self.assertRaises(ValueError):
            with leases.guard(self.repo, 'task'):
                self.fail('active task was acquired')
        gate.unlink()
        stdout, stderr = child.communicate(timeout=15)
        self.assertEqual(child.returncode, 0, stderr.decode())
        self.assertFalse(leases.is_in_use(self.repo, 'task'))
        with leases.guard(self.repo, 'task'):
            pass

    def test_concurrent_uses_share_a_task_and_mutation_waits_for_all(self):
        script = ('import os,pathlib,sys,time; p=pathlib.Path(sys.argv[1]); p.write_text("ready")\n'
                  'while p.exists(): time.sleep(.02)')
        code = ('from workspace_lifecycle import leases; import sys; '
                'sys.exit(leases.run(sys.argv[1], "task", [sys.executable, "-c", sys.argv[2], sys.argv[3]], sys.argv[1]))')
        gates = [self.repo / name for name in ('one', 'two')]
        children = []
        for gate in gates:
            child = subprocess.Popen([sys.executable, '-c', code, str(self.repo), script, str(gate)],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.addCleanup(self.stop, child)
            children.append(child)
        self.until(lambda: all(gate.exists() for gate in gates))
        status = leases.status(self.repo, 'task')
        self.assertEqual((status['busy'], len({use['token'] for use in status['uses']})), (True, 2))
        for allow_use in (False, True):
            with self.assertRaisesRegex(ValueError, 'in use by another session') as refused:
                with leases.guard(self.repo, 'task', allow_use=allow_use):
                    self.fail('mutation entered a task with live sessions')
            self.assertIn('lease-status --task task', refused.exception.recovery)
        gates[0].unlink()
        children[0].communicate(timeout=15)
        self.assertEqual(len(leases.status(self.repo, 'task')['uses']), 1)
        with self.assertRaises(ValueError):
            with leases.guard(self.repo, 'task'):
                self.fail('mutation entered while one session remains')
        gates[1].unlink()
        children[1].communicate(timeout=15)
        self.assertEqual([child.returncode for child in children], [0, 0])
        with leases.guard(self.repo, 'task'):
            pass

    def test_sole_own_session_may_mutate_but_not_beside_another(self):
        mutate = ('from workspace_lifecycle import leases; import os,sys\n'
                  'try:\n'
                  '    with leases.guard(os.getcwd(), "task", allow_use=True): sys.exit(0)\n'
                  'except ValueError as exc: print(exc); sys.exit(7)')
        code = ('from workspace_lifecycle import leases; import sys; '
                'sys.exit(leases.run(sys.argv[1], "task", [sys.executable, "-c", sys.argv[2]], sys.argv[1]))')
        alone = subprocess.run([sys.executable, '-c', code, str(self.repo), mutate], capture_output=True, text=True)
        self.assertEqual(alone.returncode, 0, alone.stdout + alone.stderr)
        gate = self.repo / 'other'
        other = self.supervisor('import pathlib,time; p=pathlib.Path("other"); p.write_text("ready")\n'
                                'while p.exists(): time.sleep(.02)')
        self.until(gate.exists)
        beside = subprocess.run([sys.executable, '-c', code, str(self.repo), mutate], capture_output=True, text=True)
        self.assertEqual((beside.returncode, beside.stdout.strip()), (7, 'task is in use by another session: task'),
                         beside.stderr)
        gate.unlink()
        other.communicate(timeout=15)

    def test_running_mutation_and_older_supervisor_refuse_new_use_with_recovery(self):
        lock, legacy = leases._paths(self.repo, 'task')
        with leases.guard(self.repo, 'task'):
            with self.assertRaisesRegex(ValueError, 'being changed by another lifecycle operation') as refused:
                leases.run(self.repo, 'task', [sys.executable, '-c', 'raise SystemExit(5)'], self.repo)
        self.assertIn('finish or retire', refused.exception.recovery)
        with leases._lock(lock):
            with self.assertRaisesRegex(ValueError, 'older workspace-lifecycle supervisor'):
                leases.run(self.repo, 'task', [sys.executable, '-c', 'raise SystemExit(5)'], self.repo)
        leases._save(legacy, {'token': 'f' * 32, 'owner_pid': os.getpid(), 'owner_identity': 'gone', 'task': 'task'})
        self.assertEqual(leases.status(self.repo, 'task')['uses'][0]['token'], 'f' * 32)
        with self.assertRaisesRegex(ValueError, 'unreleased task use'):
            with leases.guard(self.repo, 'task'):
                self.fail('0.4 receipt admitted a mutation')
        leases.release(self.repo, 'task', 'f' * 32, 'fixture owner identity never existed')
        self.assertEqual(leases.status(self.repo, 'task')['uses'], [])
        with leases.guard(self.repo, 'task'):
            pass

    def test_receipt_reader_serializes_atomic_replacement(self):
        receipt = self.repo / 'receipt.json'
        leases._save(receipt, {'generation': 'old'})
        load = json.load
        writers = []
        def start_writer_while_reading(stream):
            code = ('from workspace_lifecycle.leases import _save; from pathlib import Path; import sys; '
                    'print("ready",flush=True); _save(Path(sys.argv[1]), {"generation":"new"})')
            writer = subprocess.Popen([sys.executable, '-c', code, str(receipt)],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            writers.append(writer)
            self.addCleanup(self.stop, writer)
            self.assertEqual(writer.stdout.readline().rstrip(b'\r\n'), b'ready')
            self.assertIsNone(writer.poll())
            return load(stream)
        with patch.object(leases.json, 'load', side_effect=start_writer_while_reading):
            self.assertEqual(leases._read(receipt), {'generation': 'old'})
        _, stderr = writers[0].communicate(timeout=15)
        self.assertEqual(writers[0].returncode, 0, stderr.decode())
        self.assertEqual(leases._read(receipt), {'generation': 'new'})

    def test_grandchild_outlives_leader_and_keeps_retirement_blocked(self):
        gate = self.repo / 'descendant'
        grandchild = 'import pathlib,time; p=pathlib.Path("descendant"); p.write_text("ready");\nwhile p.exists(): time.sleep(.02)'
        leader = 'import subprocess,sys; subprocess.Popen([sys.executable,"-c",%r])' % grandchild
        child = self.supervisor(leader)
        self.until(gate.exists)
        self.assertTrue(leases.is_in_use(self.repo, 'task'))
        self.assertIsNone(child.poll())
        gate.unlink()
        stdout, stderr = child.communicate(timeout=15)
        self.assertEqual(child.returncode, 0, stderr.decode())
        self.assertFalse(leases.is_in_use(self.repo, 'task'))

    @unittest.skipIf(os.name == 'nt', 'Linux subreaper fixture; Windows uses native job inheritance')
    def test_detached_grandchild_keeps_lease_until_exit(self):
        gate = self.repo / 'detached'
        grandchild = 'import pathlib,time; p=pathlib.Path("detached"); p.write_text("ready");\nwhile p.exists(): time.sleep(.02)'
        leader = 'import subprocess,sys; subprocess.Popen([sys.executable,"-c",%r],start_new_session=True)' % grandchild
        child = self.supervisor(leader)
        self.until(gate.exists)
        self.assertTrue(leases.is_in_use(self.repo, 'task'))
        time.sleep(.2)
        self.assertIsNone(child.poll())
        gate.unlink()
        stdout, stderr = child.communicate(timeout=15)
        self.assertEqual(child.returncode, 0, stderr.decode())
        self.assertFalse(leases.is_in_use(self.repo, 'task'))

    def test_killed_supervisor_never_implies_release(self):
        gate = self.repo / 'gate'
        script = 'import pathlib,time; p=pathlib.Path("gate"); p.write_text("ready");\nwhile p.exists(): time.sleep(.02)'
        child = self.supervisor(script)
        self.until(gate.exists)
        self.until(lambda: 'child_pid' in (self.use() or {}))
        token = self.use()['token']
        child.kill()
        child.wait(timeout=10)
        self.assertTrue(leases.is_in_use(self.repo, 'task'))
        with self.assertRaisesRegex(ValueError, 'unreleased task use'):
            with leases.guard(self.repo, 'task'):
                self.fail('stale receipt admitted a mutation')
        self.assertEqual(leases.run(self.repo, 'task', [sys.executable, '-c', 'pass'], self.repo), 0)
        with self.assertRaises(ValueError):
            leases.release(self.repo, 'task', token, '')
        receipt = self.use()
        # Windows can terminate the launcher and its child together. Assert
        # live-user refusal only when the native child identity is still live.
        if receipt.get('child_identity') is not None and leases._identity(receipt['child_pid']) == receipt['child_identity']:
            with self.assertRaises(ValueError):
                leases.release(self.repo, 'task', token, 'external inspection')
        gate.unlink()
        child.communicate(timeout=15)
        receipt = self.use()
        self.until(lambda: leases._identity(receipt['child_pid']) != receipt['child_identity'])
        leases.release(self.repo, 'task', token, 'fixture child exited; no detached users')
        self.assertFalse(leases.is_in_use(self.repo, 'task'))

    def test_release_requires_exact_receipt_and_cannot_release_live_owner(self):
        gate = self.repo / 'release-gate'
        child = self.supervisor('import pathlib,time; p=pathlib.Path("release-gate"); p.write_text("ready");\nwhile p.exists(): time.sleep(.02)')
        self.until(gate.exists)
        try:
            receipt = self.use()
            if child.poll() is not None:
                self.fail('supervisor exited with live child: ' + child.stderr.read().decode())
            with self.assertRaises(ValueError):
                leases.release(self.repo, 'task', receipt['token'], 'claimed release')
            self.assertIsNone(child.poll())
        finally:
            gate.unlink(missing_ok=True)
        child.communicate(timeout=10)
        self.assertEqual(child.returncode, 0)

    @unittest.skipUnless(os.name == 'nt', 'Windows named Job reentry')
    def test_job_member_joins_one_use_but_copied_token_does_not(self):
        gate = self.repo / 'join-gate'
        gate.write_text('hold')
        ready = self.repo / 'joined-ready'
        joined = ('from workspace_lifecycle import leases; import os,sys,pathlib; '
                  'repo=os.getcwd(); '
                  'with_guard=leases.guard(repo,"task",allow_use=True); '
                  'with_guard.__enter__(); with_guard.__exit__(None,None,None); '
                  'sys.exit(leases.run(repo,"task",[sys.executable,"-c",'
                  '"import pathlib; pathlib.Path(\'joined-child\').write_text(\'ok\')"],repo))')
        leader = ('import pathlib,subprocess,sys,time; '
                  'subprocess.run([sys.executable,"-c",%r],check=True); '
                  'pathlib.Path("joined-ready").write_text("ok"); '
                  'gate=pathlib.Path("join-gate"); '
                  '\nwhile gate.exists(): time.sleep(.02)' % joined)
        child = self.supervisor(leader)
        try:
            self.until(ready.exists)
            self.assertEqual((self.repo / 'joined-child').read_text(), 'ok')
            receipt = self.use()
            self.assertTrue(leases.status(self.repo, 'task')['busy'])
            with patch.dict(os.environ, {'WORKSPACE_LIFECYCLE_USE': receipt['token']}):
                separate = ('import os,sys; sys.exit(0 if os.environ["WORKSPACE_LIFECYCLE_USE"] != %r else 9)'
                            % receipt['token'])
                self.assertEqual(leases.run(self.repo, 'task', [sys.executable, '-c', separate], self.repo), 0)
                with self.assertRaises(ValueError):
                    with leases.guard(self.repo, 'task', allow_use=True):
                        self.fail('copied token entered mutation guard')
            self.assertEqual(leases.status(self.repo, 'task')['uses'], [receipt])
        finally:
            gate.unlink(missing_ok=True)
        _, stderr = child.communicate(timeout=15)
        self.assertEqual(child.returncode, 0, stderr.decode())
        self.assertEqual(leases.status(self.repo, 'task')['uses'], [])

    @unittest.skipUnless(os.name == 'nt', 'Windows named Job reentry')
    def test_join_rejects_mismatched_or_uncertain_identity(self):
        gate = self.repo / 'join-gate'
        child = self.supervisor('import pathlib,time; p=pathlib.Path("join-gate"); '
                                'p.write_text("ready");\nwhile p.exists(): time.sleep(.02)')
        self.until(gate.exists)
        try:
            receipt = self.use()
            with patch.dict(os.environ, {'WORKSPACE_LIFECYCLE_USE': receipt['token']}):
                with patch.object(leases, '_windows_job_member', return_value=True):
                    self.assertTrue(leases._own_use(receipt, 'task'))
                    for change in ({'task': 'other'}, {'token': 'stale'},
                                   {'owner_identity': 'reused PID'},
                                   {'owner_pid': None}, {'windows_job': 'other'}):
                        self.assertFalse(leases._own_use({**receipt, **change}, 'task'), change)
                    with patch.object(leases, '_identity', return_value=None):
                        self.assertFalse(leases._own_use(receipt, 'task'))
                with patch.object(leases, '_windows_job_member', return_value=False):
                    self.assertFalse(leases._own_use(receipt, 'task'))
            with patch.dict(os.environ, {'WORKSPACE_LIFECYCLE_USE': 'stale'}):
                self.assertFalse(leases._own_use(receipt, 'task'))
        finally:
            gate.unlink(missing_ok=True)
        child.communicate(timeout=15)
        self.assertEqual(child.returncode, 0)


if __name__ == '__main__':
    unittest.main()
