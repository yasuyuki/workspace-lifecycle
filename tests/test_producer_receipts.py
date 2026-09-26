import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
import venv

from workspace_lifecycle.errors import LifecycleError
from workspace_lifecycle import producers
from workspace_lifecycle.service import begin, finish, hold, retire, retire_pending

import test_lifecycle as lifecycle


class ProducerReceiptTests(unittest.TestCase):
    def setUp(self):
        self.fixture = lifecycle.LifecycleTest('runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root, self.topic, self.temp = self.fixture.root, self.fixture.topic, self.fixture.temp
        begin(self.root, task='one', request='issue/146', remote='origin', branch='topic/one',
              worktree=str(self.topic), validation=['git', 'diff', '--check'], preflight=self.fixture.preflight)
        self.receipt = producers.task_receipt_dir(self.topic, 'one') / 'generation.json'
        self.output = Path(self.temp.name) / 'outside.wav'

    def _provider(self, *, fail=False, remove=True):
        script = Path(self.temp.name) / ('fail-provider.py' if fail else 'provider.py')
        if fail:
            body = 'import sys; print("owner failure", file=sys.stderr); raise SystemExit(7)\n'
        else:
            removal = 'output.unlink() if output.exists() else None\n' if remove else ''
            body = (
                'import json, pathlib, sys\n'
                'receipt, generation, result, output = sys.argv[1:5]\n'
                'receipt, output = pathlib.Path(receipt), pathlib.Path(output)\n'
                + removal +
                'receipt.parent.mkdir(parents=True, exist_ok=True)\n'
                'receipt.write_text(json.dumps({"generation": str(generation), "owner": "funkot-wav", "output": str(output), "receipt": str(receipt), "state": "reclaimed", "hold": False, "source_revision": "test", "inputs": [], "identity": {}, "sha256": "a" * 64, "accepted_proof": str(result), "released_proof": str(result)}))\n'
                'print(json.dumps({"reclaimed": True, "generation": str(generation), "receipt": str(receipt)}))\n')
        script.write_text(body)
        # Registration stores the canonical output. Windows TEMP can contain
        # an 8.3 alias, which must not leak into the provider's exact receipt.
        return [sys.executable, str(script), str(self.receipt), 'gen-1', '{result_ref}', str(self.output.resolve())]

    def _register(self, *, completion=None):
        return producers.register(self.topic, 'one', 'funkot-wav', 'gen-1', str(self.receipt),
                                  [str(self.output)], completion or self._provider())

    def _finish(self):
        (self.topic / 'source.txt').write_text('source\n')
        self.fixture.task = 'one'
        plan = Path(self.temp.name) / 'plan.json'
        plan.write_text(json.dumps(self.fixture.source_plan(self.topic, 'source.txt')))
        return finish(self.topic, task='one', plan_path=str(plan), result_ref='issue/146', users_released=True)

    def test_register_then_finish_invokes_exact_provider_and_keeps_unrelated_task(self):
        self._register()
        self.output.write_bytes(b'generated')
        replay = self._register()
        self.assertEqual(replay['generation'], 'gen-1')
        other = Path(self.temp.name) / 'other'
        begin(self.root, task='other', request='issue/147', remote='origin', branch='topic/other',
              worktree=str(other), validation=['git', 'diff', '--check'], preflight=self.fixture.preflight)
        result = self._finish()
        self.assertTrue(result['accepted'])
        self.assertTrue(result['producers']['results'][0]['reclaimed'])
        self.assertTrue(result['retirement']['retired'])
        durable = json.loads(self.receipt.read_text())
        self.assertEqual(durable['generation'], 'gen-1')
        self.assertEqual(durable['released_proof'], 'issue/146')
        self.assertEqual(producers.retry(self.root, 'other')['results'], [])

    def test_completion_runs_with_the_registered_virtual_environment(self):
        environment = Path(self.temp.name) / 'private-owner-environment'
        venv.EnvBuilder(with_pip=False, symlinks=os.name != 'nt').create(environment)
        python = environment / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        site_packages = Path(subprocess.check_output(
            [str(python), '-I', '-c', 'import sysconfig; print(sysconfig.get_path("purelib"))'],
            text=True).strip())
        module = 'lifecycle_test_private_owner'
        (site_packages / (module + '.py')).write_text('GENERATION = "gen-1"\n')
        unavailable = subprocess.run(
            [sys._base_executable, '-I', '-c', 'import ' + module],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertNotEqual(unavailable.returncode, 0)
        self.assertIn('ModuleNotFoundError', unavailable.stderr)
        completion = self._provider()
        script = Path(completion[1])
        script.write_text('from ' + module + ' import GENERATION\n'
                          'assert GENERATION == "gen-1"\n' + script.read_text())
        self._register(completion=[str(python), '-I', *completion[1:]])
        self.output.write_bytes(b'generated')
        result = self._finish()
        self.assertEqual(result['producers']['results'],
                         [{'generation': 'gen-1', 'reclaimed': True}])
        self.assertTrue(result['retirement']['retired'])
        self.assertFalse(self.output.exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows short-path aliases')
    def test_provider_confirms_canonical_output_from_short_path(self):
        import ctypes
        short_path = ctypes.windll.kernel32.GetShortPathNameW
        short_path.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
        short_path.restype = ctypes.c_uint32
        buffer = ctypes.create_unicode_buffer(32768)
        length = short_path(str(self.output.parent), buffer, len(buffer))
        if not length or length >= len(buffer):
            self.skipTest('native short path unavailable')
        alias = Path(buffer.value) / self.output.name
        if str(alias) == str(alias.resolve()):
            self.skipTest('filesystem did not provide a distinct short-path alias')
        self.output = alias
        registered = self._register()
        self.output.write_bytes(b'generated')
        result = self._finish()
        self.assertTrue(result['producers']['results'][0]['reclaimed'])
        durable = json.loads(self.receipt.read_text())
        self.assertEqual(durable['output'], registered['outputs'][0])

    def test_held_or_accepted_task_cannot_register(self):
        hold(self.topic, 'one', 'review needed', 'release explicitly')
        with self.assertRaises(LifecycleError):
            self._register()

        # A separate normally accepted task remains ineligible for a new owner
        # generation even when it has no output.
        other = Path(self.temp.name) / 'accepted'
        begin(self.root, task='accepted', request='issue/148', remote='origin', branch='topic/accepted',
              worktree=str(other), validation=['git', 'diff', '--check'], preflight=self.fixture.preflight)
        (other / 'source.txt').write_text('source\n')
        self.fixture.task = 'accepted'
        plan = Path(self.temp.name) / 'accepted-plan.json'
        plan.write_text(json.dumps(self.fixture.source_plan(other, 'source.txt')))
        finish(other, task='accepted', plan_path=str(plan), result_ref='issue/148')
        with self.assertRaises(LifecycleError):
            producers.register(other, 'accepted', 'funkot-wav', 'g',
                               str(producers.task_receipt_dir(other, 'accepted') / 'r.json'),
                               [str(Path(self.temp.name) / 'accepted.wav')], self._provider())

    def test_registration_refuses_absent_tracked_output_and_receipt_outside_task_root(self):
        # Remove the tracked file from disk and index: it remains a tracked path
        # for the registration check while satisfying the absent-output precondition.
        from test_lifecycle import run
        run(self.topic, 'update-index', '--assume-unchanged', 'README')
        (self.topic / 'README').unlink()
        with self.assertRaises(LifecycleError):
            producers.register(self.topic, 'one', 'funkot-wav', 'tracked', str(self.receipt),
                               [str(self.topic / 'README')], self._provider())
        with self.assertRaises(LifecycleError):
            producers.register(self.topic, 'one', 'funkot-wav', 'outside-receipt', str(Path(self.temp.name) / 'r.json'),
                               [str(self.output)], self._provider())

    def test_receipt_directory_failure_prevents_generation_registration(self):
        root = producers.task_receipt_dir(self.topic, 'one')
        root.mkdir(parents=True)
        blocker = root / 'not-a-directory'
        blocker.write_text('block')
        with self.assertRaises(LifecycleError):
            producers.register(self.topic, 'one', 'funkot-wav', 'blocked', str(blocker / 'receipt.json'),
                               [str(self.output)], self._provider())
        self.assertFalse(self.output.exists())

    def test_receipt_link_and_overlapping_generation_are_refused(self):
        root = producers.task_receipt_dir(self.topic, 'one')
        root.mkdir(parents=True)
        outside = Path(self.temp.name) / 'outside-receipts'
        outside.mkdir()
        link = root / 'linked'
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest('native symlink unavailable: ' + str(exc))
        with self.assertRaises(LifecycleError):
            producers.register(self.topic, 'one', 'funkot-wav', 'linked', str(link / 'receipt.json'),
                               [str(self.output)], self._provider())
        self._register()
        with self.assertRaises(LifecycleError):
            producers.register(self.topic, 'one', 'funkot-wav', 'other-generation',
                               str(root / 'other.json'), [str(self.output)], self._provider())
        nested = Path(self.temp.name) / 'nested-output'
        with self.assertRaises(LifecycleError):
            producers.register(self.topic, 'one', 'funkot-wav', 'overlapping-list',
                               str(root / 'nested.json'), [str(nested), str(nested / 'child.wav')], self._provider())

    def test_failed_callback_is_durable_and_retries_same_generation(self):
        self._register(completion=self._provider(fail=True))
        (self.topic / 'source.txt').write_text('source\n')
        self.fixture.task = 'one'
        plan = Path(self.temp.name) / 'plan.json'
        plan.write_text(json.dumps(self.fixture.source_plan(self.topic, 'source.txt')))
        initial = finish(self.topic, task='one', plan_path=str(plan), result_ref='issue/146', users_released=True)
        self.assertFalse(initial['producers']['results'][0]['reclaimed'])
        # Replace only the registered executable contents; the generation and
        # argv are unchanged, so the retry proves it did not scan or select a new owner.
        failed_script = Path(self._provider(fail=True)[1])
        good_script = Path(self._provider()[1])
        failed_script.write_text(good_script.read_text())
        result = producers.retry(self.topic, 'one')
        self.assertEqual(result['results'], [{'generation': 'gen-1', 'reclaimed': True}])

    def test_late_retire_keeps_failed_owner_release_pending_then_replays(self):
        self._register(completion=self._provider(fail=True))
        (self.topic / 'source.txt').write_text('source\n')
        self.fixture.task = 'one'
        plan = Path(self.temp.name) / 'plan.json'
        plan.write_text(json.dumps(self.fixture.source_plan(self.topic, 'source.txt')))
        accepted = finish(self.topic, task='one', plan_path=str(plan), result_ref='issue/146')
        self.assertTrue(accepted['accepted'])
        pending = retire(self.root, task='one', result_ref='issue/146', users_released=True)
        self.assertTrue(pending['pending'])
        self.assertFalse(pending['producers']['results'][0]['reclaimed'])
        failed_script = Path(self._provider(fail=True)[1])
        good_script = Path(self._provider()[1])
        failed_script.write_text(good_script.read_text())
        replayed = retire_pending(self.root)
        self.assertTrue(replayed['pending'][0]['retired'])

    def test_receipt_claim_with_live_output_stays_pending(self):
        self._register(completion=self._provider(remove=False))
        self.output.write_bytes(b'not reclaimed')
        result = self._finish()
        self.assertFalse(result['producers']['results'][0]['reclaimed'])
        self.assertTrue(self.output.exists())

    def test_retry_refuses_branch_changed_after_exact_acceptance(self):
        self._register(completion=self._provider(fail=True))
        (self.topic / 'source.txt').write_text('source\n')
        self.fixture.task = 'one'
        plan = Path(self.temp.name) / 'plan.json'
        plan.write_text(json.dumps(self.fixture.source_plan(self.topic, 'source.txt')))
        finish(self.topic, task='one', plan_path=str(plan), result_ref='issue/146', users_released=True)
        from test_lifecycle import run
        (self.topic / 'post-acceptance.txt').write_text('changed\n')
        run(self.topic, 'add', 'post-acceptance.txt')
        run(self.topic, 'commit', '-m', 'changed after acceptance')
        with self.assertRaisesRegex(LifecycleError, 'changed accepted branch'):
            producers.retry(self.topic, 'one')


if __name__ == '__main__':
    unittest.main()
