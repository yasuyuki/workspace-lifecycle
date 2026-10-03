import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_lifecycle
from workspace_lifecycle import adoption, preserved, producers, service
from workspace_lifecycle.errors import LifecycleError
from workspace_lifecycle.state import locked_state


class PreservedDataTest(unittest.TestCase):
    def setUp(self):
        self.f = test_lifecycle.LifecycleTest('runTest')
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.root = self.f.root
        test_lifecycle.run(self.root, 'switch', '-c', 'topic/primary')
        self.integration = Path(self.f.temp.name) / 'integration'
        test_lifecycle.run(self.root, 'worktree', 'add', str(self.integration), 'trunk')
        self.other = test_lifecycle.LifecycleTest('runTest')
        self.other.setUp()
        self.addCleanup(self.other.tearDown)

    @staticmethod
    def identity(path):
        info = path.lstat()
        return [info.st_dev, info.st_ino]

    def retired(self, name='foreign', *, legacy=False):
        if legacy:
            payload = self.root / 'legacy-data' / name
            payload.mkdir(parents=True)
            admin = Path(self.other.temp.name) / (name + '-archive')
            admin.mkdir()
            (admin / 'HEAD').write_text('retained admin')
            (payload / '.git').write_text('gitdir: ' + str(Path(self.other.temp.name) / 'missing-admin') + '\n')
            (payload / 'private').write_text('retained old content')
            receipt = {'task': name, 'device': -1, 'recovery_held': True}
        else:
            workspace = self.root / name
            service.begin(self.other.root, task=name, request='issue/foreign', remote='origin',
                branch='topic/' + name, worktree=str(workspace),
                validation=['git', 'diff', '--check'], preflight=self.other.preflight)
            (workspace / 'feature').write_text('accepted foreign source')
            self.other.task = name
            plan = Path(self.other.temp.name) / (name + '-plan.json')
            plan.write_text(json.dumps(self.other.source_plan(workspace, 'feature')))
            service.finish(workspace, task=name, plan_path=str(plan), result_ref='issue/foreign')
            receipt = service.retire(self.other.root, task=name, result_ref='issue/foreign', users_released=True)['receipt']
            payload = Path(receipt['recovery_path'])
            admin = Path(receipt['admin_archive_path'])
        receipt_file = Path(self.other.temp.name) / (name + '-receipt.json')
        receipt_file.write_text(json.dumps(receipt, sort_keys=True))
        entry = {'path': payload.relative_to(self.root).as_posix(), 'owner': 'foreign-common/' + name,
                 'receipt_ref': str(receipt_file), 'evidence': 'private current ownership/anchor inspection',
                 'identity': self.identity(payload),
                 'admin_archive': {'path': str(admin), 'identity': self.identity(admin)},
                 'unresolved': ['old receipt lacks manifests/remote; device mismatch remains unverified'] if legacy else []}
        return entry, payload, admin, receipt_file

    def adopt(self, entries, **changes):
        values = dict(task='primary', request='issue/primary', remote='origin', branch='topic/primary',
            worktree=str(self.root), expected_head=test_lifecycle.run(self.root, 'rev-parse', 'HEAD'),
            evidence='continuing original work', validation=['git', 'diff', '--check'],
            preflight=self.f.preflight, preserved_data=entries)
        values.update(changes)
        return adoption.adopt_existing(self.root, **values)

    def tree(self, *roots):
        return {str(path): (self.identity(path), path.read_bytes())
                for root in roots for path in ([root] if root.is_file() else root.rglob('*')) if path.is_file()}

    def feature_plan(self):
        self.f.task = 'primary'
        (self.root / 'new-source').write_text('root change')
        plan = Path(self.f.temp.name) / 'root-plan.json'
        plan.write_text(json.dumps(self.f.source_plan(self.root, 'new-source')))
        return plan

    def test_native_retired_and_unconfirmed_legacy_are_opaque_and_unchanged(self):
        good, payload, admin, receipt = self.retired()
        old, old_payload, old_admin, old_receipt = self.retired('old', legacy=True)
        before = self.tree(payload, admin, receipt, old_payload, old_admin, old_receipt)
        # Prove adoption never consults either retired checkout through live Git.
        original = adoption.top
        def top(path):
            self.assertNotIn(Path(path), (payload, old_payload))
            return original(path)
        (self.root / 'README').write_text('original dirty baseline')
        test_lifecycle.run(self.root, 'add', 'README')
        index = Path(test_lifecycle.run(self.root, 'rev-parse', '--path-format=absolute', '--git-path', 'index'))
        index_bytes = index.read_bytes()
        with patch.object(adoption, 'top', side_effect=top):
            result = self.adopt([good, old], hold_reason='original pending acceptance', next_action='real validator review')
        self.assertFalse(result['accepted'])
        self.assertEqual(index.read_bytes(), index_bytes)
        item = service.status(self.root, 'primary')
        self.assertEqual(item['adoption']['preserved_data'], [good, old])
        self.assertEqual(set(item['adoption']['protected_paths']), {good['path'], old['path']})
        self.assertNotIn('acceptance', item)
        with self.assertRaisesRegex(LifecycleError, 'held'):
            service.before_run(self.root, 'primary')
        with locked_state(self.root) as (_, state):
            self.assertEqual(state.get('retired', {}), {})
        self.assertEqual(before, self.tree(payload, admin, receipt, old_payload, old_admin, old_receipt))

    def test_root_finish_and_dependent_managed_run_preserve_foreign_data(self):
        entry, payload, admin, receipt = self.retired()
        before = self.tree(payload, admin, receipt)
        self.adopt([entry])
        child = self.root / 'consumer'
        test_lifecycle.run(self.root, 'worktree', 'add', '-b', 'topic/consumer', str(child))
        result = adoption.adopt_existing(self.root, task='consumer', request='issue/consumer',
            remote='origin', branch='topic/consumer', worktree=str(child),
            expected_head=test_lifecycle.run(child, 'rev-parse', 'HEAD'), evidence='original dependent task',
            validation=['git', 'diff', '--check'], preflight=self.f.preflight,
            parent='primary', dependencies=['primary'])
        self.assertFalse(result['accepted'])
        for cwd, task in [(self.root, 'primary'), (child, 'consumer')]:
            command = subprocess.run([sys.executable, '-m', 'workspace_lifecycle', 'resolve-run',
                '--cwd', str(cwd), '--launch-cwd', str(cwd), '--', sys.executable, '-c',
                'import os; print(os.environ["WORKSPACE_LIFECYCLE_TASK"])'], capture_output=True, text=True)
            self.assertEqual(command.returncode, 0, command.stderr)
            self.assertEqual(command.stdout.strip(), task)
        # An active child's untracked checkout remains owned by that child.
        (self.root / '.git' / 'info' / 'exclude').write_text('consumer/\n')
        completed = service.finish(self.root, task='primary', plan_path=str(self.feature_plan()), result_ref='issue/primary')
        self.assertTrue(completed['accepted'])
        self.assertEqual((self.integration / 'new-source').read_text(), 'root change')
        self.assertEqual(before, self.tree(payload, admin, receipt))
        with self.assertRaisesRegex(LifecycleError, 'retired receipt'):
            service.reclaim(self.root, task='foreign', result_ref='issue/foreign', preservation_evidence='no authority')

    def test_finish_all_categories_reject_before_reading_data_including_alias(self):
        entry, payload, admin, receipt = self.retired('old', legacy=True)
        self.adopt([entry])
        before = self.tree(payload, admin, receipt)
        plan = Path(self.f.temp.name) / 'bad-plan.json'
        for category in ('commit', 'restore', 'archive'):
            for name in (entry['path'] + '/private', entry['path'] + '//private', 'legacy-data'):
                with self.subTest(category=category, name=name):
                    plan.write_text(json.dumps({category: [{'path': name}]}))
                    with patch.object(service, '_sha', side_effect=AssertionError('must not read foreign file')):
                        with self.assertRaisesRegex(LifecycleError, 'preserved data'):
                            service.finish(self.root, task='primary', plan_path=str(plan), result_ref='issue/primary')
        self.assertEqual(before, self.tree(payload, admin, receipt))

    def test_producer_output_and_managed_cwd_cannot_enter_boundary(self):
        entry, payload, _, _ = self.retired('old', legacy=True)
        self.adopt([entry])
        with self.assertRaisesRegex(LifecycleError, 'nested repository'):
            producers.register(self.root, 'primary', 'bad-owner', 'g1',
                str(producers.task_receipt_dir(self.root, 'primary') / 'g1.json'),
                [str(payload / 'new-output')], [sys.executable, '-c', 'pass'])
        for command in ('run', 'resolve-run'):
            argv = [sys.executable, '-m', 'workspace_lifecycle', '--repo', str(self.root), command]
            argv += ['--task', 'primary'] if command == 'run' else ['--launch-cwd', str(payload)]
            run = subprocess.run([*argv, '--cwd', str(payload), '--', sys.executable, '-c', 'raise AssertionError()'], capture_output=True, text=True)
            self.assertNotEqual(run.returncode, 0)
            self.assertIn('foreign preserved data', run.stderr)

    def interrupt(self, entries):
        original = adoption.save_state
        def stop(directory, state):
            original(directory, state)
            raise OSError('interrupt after durable intent')
        with patch.object(adoption, 'save_state', side_effect=stop):
            with self.assertRaises(LifecycleError):
                self.adopt(entries)

    def test_interrupted_identical_intent_resumes_without_content_ownership(self):
        entry, payload, _, _ = self.retired('old', legacy=True)
        self.interrupt([entry])
        # Foreign owner's data changes do not make root its content owner.
        (payload / 'private').write_text('foreign owner still owns this file')
        self.assertFalse(self.adopt([entry])['accepted'])

    def test_interrupted_payload_replacement_keeps_original_intent(self):
        entry, payload, _, _ = self.retired('old', legacy=True)
        self.interrupt([entry])
        payload.rename(payload.with_name('held-original'))
        payload.mkdir()
        with self.assertRaisesRegex(LifecycleError, 'identity mismatch'):
            self.adopt([entry])
        with locked_state(self.root) as (_, state):
            self.assertEqual(state['intents']['primary']['desired']['preserved_data'], [entry])
            self.assertNotIn('primary', state['tasks'])

    def test_interrupted_marker_or_admin_replacement_is_detected(self):
        entry, payload, admin, _ = self.retired('old', legacy=True)
        self.interrupt([entry])
        marker = payload / '.git'
        original = marker.read_bytes()
        marker.write_text('gitdir: ' + str(Path(self.other.temp.name) / 'different-missing-admin') + '\n')
        with self.assertRaisesRegex(LifecycleError, 'snapshot changed'):
            self.adopt([entry])
        marker.write_bytes(original)
        admin.rename(admin.with_name('held-admin'))
        admin.mkdir()
        with self.assertRaisesRegex(LifecycleError, 'identity mismatch'):
            self.adopt([entry])

    def test_unknown_broken_git_outside_boundary_is_still_refused(self):
        entry, _, _, _ = self.retired('old', legacy=True)
        unknown = self.root / '.workspace-lifecycle-recovery' / 'unknown'
        unknown.mkdir(parents=True)
        (unknown / '.git').write_text('gitdir: /unknown-missing-admin\n')
        with self.assertRaises(LifecycleError):
            self.adopt([entry])
        self.assertTrue((unknown / '.git').exists())

    def test_active_checkout_cannot_be_declared_preserved(self):
        entry, _, _, _ = self.retired('old', legacy=True)
        active = self.root / 'active'
        test_lifecycle.run(self.root, 'worktree', 'add', '-b', 'topic/active', str(active))
        entry.update(path='active', identity=self.identity(active))
        with self.assertRaisesRegex(LifecycleError, 'active Git worktree'):
            self.adopt([entry])
        # Independent live Git isn't in our native worktree list, but its admin
        # is present and must never be treated as retired either.
        independent = self.root / 'independent'
        test_lifecycle.run(self.other.root, 'worktree', 'add', '-b', 'topic/independent', str(independent))
        entry.update(path='independent', identity=self.identity(independent))
        with self.assertRaisesRegex(LifecycleError, 'active Git admin'):
            self.adopt([entry])

    def test_links_mounts_reparse_and_path_escape_are_refused(self):
        entry, payload, _, _ = self.retired('old', legacy=True)
        for location in (payload, payload.parent, Path(entry['admin_archive']['path'])):
            with patch.object(preserved.os.path, 'ismount', side_effect=lambda p, location=location: Path(p) == location):
                with self.assertRaisesRegex(LifecycleError, 'mount'):
                    self.adopt([entry])
        with patch.object(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400, create=True):
            actual = Path.lstat
            def reparse(path, *args, **kwargs):
                info = actual(path, *args, **kwargs)
                if path == payload:
                    from types import SimpleNamespace
                    return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
                return info
            with patch.object(Path, 'lstat', reparse):
                with self.assertRaisesRegex(LifecycleError, 'reparse'):
                    self.adopt([entry])
        entry['path'] = '../escape'
        with self.assertRaisesRegex(LifecycleError, 'canonical relative'):
            self.adopt([entry])

    @unittest.skipIf(os.name == 'nt', 'directory symlink permission differs on Windows')
    def test_link_boundary_cannot_be_adopted(self):
        entry, payload, _, _ = self.retired('old', legacy=True)
        held = payload.with_name('held')
        payload.rename(held)
        payload.symlink_to(held, target_is_directory=True)
        with self.assertRaisesRegex(LifecycleError, 'link'):
            self.adopt([entry])

    def test_current_identity_is_required_and_old_conditions_cannot_change_on_retry(self):
        entry, _, _, _ = self.retired('old', legacy=True)
        self.interrupt([entry])
        entry['unresolved'] = []
        with self.assertRaisesRegex(LifecycleError, 'exact requested identity'):
            self.adopt([entry])

    def test_in_workspace_admin_is_protected_without_traversal(self):
        entry, payload, admin, receipt = self.retired('old', legacy=True)
        inside = self.root / 'foreign-admin'
        admin.rename(inside)
        entry['admin_archive'] = {'path': str(inside), 'identity': self.identity(inside)}
        before = self.tree(payload, inside, receipt)
        self.adopt([entry])
        item = service.status(self.root, 'primary')
        self.assertIn('foreign-admin', item['adoption']['protected_paths'])
        plan = Path(self.f.temp.name) / 'admin-plan.json'
        plan.write_text(json.dumps({'archive': [{'path': 'foreign-admin/HEAD'}]}))
        with self.assertRaisesRegex(LifecycleError, 'preserved data'):
            service.finish(self.root, task='primary', plan_path=str(plan), result_ref='issue/primary')
        self.assertEqual(before, self.tree(payload, inside, receipt))

    def test_post_adoption_anchor_change_blocks_run_and_finish(self):
        entry, payload, _, _ = self.retired('old', legacy=True)
        self.adopt([entry])
        (payload / '.git').write_text('gitdir: ' + str(Path(self.other.temp.name) / 'other-missing-admin') + '\n')
        for action in (lambda: service.before_run(self.root, 'primary'),
                       lambda: service.finish(self.root, task='primary', plan_path=str(self.feature_plan()), result_ref='issue/primary')):
            with self.assertRaisesRegex(LifecycleError, 'boundary changed'):
                action()

    def test_sync_cannot_introduce_tracked_source_under_protected_boundary(self):
        entry, _, _, _ = self.retired('old', legacy=True)
        self.adopt([entry])
        test_lifecycle.run(self.root, 'push', 'origin', 'topic/primary')
        clone = Path(self.f.temp.name) / 'writer'
        test_lifecycle.run(self.root, 'clone', str(self.f.remote), str(clone))
        test_lifecycle.run(clone, 'config', 'user.name', 'Test')
        test_lifecycle.run(clone, 'config', 'user.email', 'test@example.invalid')
        test_lifecycle.run(clone, 'switch', 'topic/primary')
        target = clone / entry['path'] / 'intruder'
        target.parent.mkdir(parents=True)
        target.write_text('incoming unauthorized source')
        test_lifecycle.run(clone, 'add', '.')
        test_lifecycle.run(clone, 'commit', '-m', 'incoming')
        test_lifecycle.run(clone, 'push', 'origin', 'topic/primary')
        before = test_lifecycle.run(self.root, 'rev-parse', 'HEAD')
        with self.assertRaisesRegex(LifecycleError, 'Git changes overlap'):
            service.synchronize(self.root, task='primary', expected_head=before,
                remote_head=test_lifecycle.run(clone, 'rev-parse', 'HEAD'), evidence='reviewed source')
        self.assertEqual(test_lifecycle.run(self.root, 'rev-parse', 'HEAD'), before)

    def test_linked_checkout_cannot_retire_foreign_payload(self):
        linked = Path(self.f.temp.name) / 'linked'
        test_lifecycle.run(self.root, 'worktree', 'add', '-b', 'topic/linked', str(linked))
        self.root = linked
        entry, payload, admin, receipt = self.retired('old', legacy=True)
        before = self.tree(payload, admin, receipt)
        self.adopt([entry], worktree=str(linked), branch='topic/linked')
        result = service.finish(linked, task='primary', plan_path=str(self.feature_plan()), result_ref='issue/primary')
        self.assertTrue(result['accepted'])
        with self.assertRaisesRegex(LifecycleError, 'foreign preserved data cannot retire'):
            service.retire(self.integration, task='primary', result_ref='issue/primary', users_released=True)
        self.assertEqual(before, self.tree(payload, admin, receipt))

    def test_cli_adoption_exposes_contract_and_preserves_unaccepted_state(self):
        entry, _, _, _ = self.retired('old', legacy=True)
        command = subprocess.run([sys.executable, '-m', 'workspace_lifecycle', '--repo', str(self.root),
            'adopt-existing', '--task', 'primary', '--request', 'issue/primary', '--remote', 'origin',
            '--branch', 'topic/primary', '--worktree', str(self.root),
            '--expected-head', test_lifecycle.run(self.root, 'rev-parse', 'HEAD'),
            '--evidence', 'original owner evidence', '--validation-json', '["git","diff","--check"]',
            '--preflight-json', json.dumps(self.f.preflight), '--preserved-data-json', json.dumps([entry])],
            capture_output=True, text=True)
        self.assertEqual(command.returncode, 0, command.stderr)
        self.assertFalse(json.loads(command.stdout)['accepted'])
        self.assertEqual(service.status(self.root, 'primary')['adoption']['preserved_data'], [entry])

    def test_tracked_source_cannot_be_hidden_by_boundary_contract(self):
        entry, payload, _, _ = self.retired('old', legacy=True)
        test_lifecycle.run(self.root, 'add', str((payload / 'private').relative_to(self.root)))
        with self.assertRaisesRegex(LifecycleError, 'tracked source'):
            self.adopt([entry])

    def test_binding_interruption_preserves_intent_until_exact_resume(self):
        entry, payload, _, _ = self.retired('old', legacy=True)
        original = adoption._capture
        marker = payload / '.git'
        raw = marker.read_bytes()
        calls = 0
        def change_after_binding(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 3:
                marker.write_text('gitdir: ' + str(Path(self.other.temp.name) / 'changed-admin') + '\n')
            return original(*args, **kwargs)
        with patch.object(adoption, '_capture', side_effect=change_after_binding):
            with self.assertRaisesRegex(LifecycleError, 'changed after binding'):
                self.adopt([entry])
        self.assertEqual(test_lifecycle.run(self.root, 'config', '--get', 'branch.topic/primary.workspaceTask'), 'primary')
        with self.assertRaisesRegex(LifecycleError, 'unknown task'):
            service.before_run(self.root, 'primary')
        marker.write_bytes(raw)
        self.assertFalse(self.adopt([entry])['accepted'])


if __name__ == '__main__':
    unittest.main()
