"""Regression coverage for explicit adoption of existing linked worktrees."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from workspace_lifecycle.adoption import adopt_existing
from workspace_lifecycle.errors import LifecycleError
from workspace_lifecycle.service import finish, hold, release_hold, retire, status
import test_lifecycle


class AdoptionTests(unittest.TestCase):
    """Compose the lifecycle fixture; do not inherit its unrelated test cases."""

    def setUp(self):
        self.fixture = test_lifecycle.LifecycleTest('runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.f = self.fixture
        self.topic = self.f.topic
        test_lifecycle.run(self.f.root, 'worktree', 'add', '-b', 'topic/adopt', str(self.topic))

    def adopt(self, task='adopt', **changes):
        worktree = Path(changes.get('worktree', self.topic))
        values = dict(task=task, request='issue/adopt', remote='origin', branch='topic/adopt',
                      worktree=str(worktree), expected_head=test_lifecycle.run(worktree, 'rev-parse', 'HEAD'),
                      evidence='existing checked-out work reviewed', validation=['git', 'diff', '--check'],
                      preflight=self.f.preflight)
        values.update(changes)
        return adopt_existing(self.f.root, **values)

    def plan(self, task, path, text):
        path.write_text(text)
        result = Path(self.f.temp.name) / (task + '.json')
        result.write_text(json.dumps({'commit': [{'path': path.name, 'classification': 'source',
            'owner': task, 'evidence': 'adopted task owns this new source', 'safe_to_commit': True,
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}]}))
        return result

    def test_adopt_status_cli_run_finish_merge_and_retire(self):
        accepted = self.adopt()
        self.assertFalse(accepted['accepted'])
        self.assertEqual(status(self.topic, 'adopt')['adoption']['evidence'], 'existing checked-out work reviewed')
        command = [sys.executable, '-m', 'workspace_lifecycle', '--repo', str(self.topic), 'run',
                   '--task', 'adopt', '--cwd', str(self.topic), '--', sys.executable, '-c', 'pass']
        result = subprocess.run(command, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = self.plan('adopt', self.topic / 'feature.txt', 'adopted work\n')
        completed = finish(self.topic, task='adopt', plan_path=str(plan), result_ref='issue/adopt')
        self.assertTrue(completed['accepted'])
        self.assertEqual((self.f.root / 'feature.txt').read_text(), 'adopted work\n')
        self.assertTrue(retire(self.f.root, task='adopt', result_ref='issue/adopt', users_released=True)['retired'])
        self.assertFalse(self.topic.exists())

    def test_adoption_is_unaccepted_and_does_not_run_validation(self):
        self.adopt(validation=['this-command-must-never-run-during-adoption'])
        item = status(self.topic, 'adopt')
        self.assertEqual(item['completion'], 'active')
        self.assertNotIn('acceptance', item)
        self.assertNotIn('integrated', item)

    def test_mount_and_target_identity_replacement_are_refused(self):
        from workspace_lifecycle import adoption
        with patch.object(adoption.os.path, 'ismount', side_effect=lambda path: Path(path) == self.topic):
            with self.assertRaisesRegex(LifecycleError, 'mounted'):
                self.adopt()
        # An interrupted durable intent is tied to this physical checkout, not its path text.
        original = adoption.save_state
        def crash_after_intent(directory, state):
            original(directory, state); raise OSError('crash after intent')
        with patch.object(adoption, 'save_state', side_effect=crash_after_intent):
            with self.assertRaises(LifecycleError): self.adopt()
        replacement = Path(self.f.temp.name) / 'replacement'
        test_lifecycle.run(self.f.root, 'worktree', 'remove', '--force', str(self.topic))
        test_lifecycle.run(self.f.root, 'worktree', 'add', str(replacement), 'topic/adopt')
        with self.assertRaisesRegex(LifecycleError, 'exact requested identity|snapshot changed|identity'):
            self.adopt(worktree=str(replacement))

    def test_cross_drive_commonpath_fallback_does_not_reject_valid_checkout(self):
        from workspace_lifecycle import adoption
        with patch.object(adoption.os.path, 'commonpath', side_effect=ValueError('different drives')), \
             patch.object(adoption.os.path, 'ismount', return_value=False):
            self.assertFalse(self.adopt()['accepted'])

    def test_subprocess_exit_after_persisted_intent_releases_lock_for_exact_retry(self):
        expected = test_lifecycle.run(self.topic, 'rev-parse', 'HEAD')
        code = '''
import os
import sys
from workspace_lifecycle import adoption
original = adoption.save_state
def stop(directory, state):
    original(directory, state)
    os._exit(73)
adoption.save_state = stop
adoption.adopt_existing(sys.argv[1], task='adopt', request='issue/adopt', remote='origin',
    branch='topic/adopt', worktree=sys.argv[2], expected_head=sys.argv[3], evidence='subprocess crash proof',
    validation=['never-run'], preflight=['never-run', '{repo}'])
'''
        crashed = subprocess.run([sys.executable, '-c', code, str(self.f.root), str(self.topic), expected],
                                 text=True, capture_output=True)
        self.assertEqual(crashed.returncode, 73, crashed.stderr)
        from workspace_lifecycle.state import locked_state
        with locked_state(self.f.root) as (_, state):
            self.assertEqual(state['intents']['adopt']['kind'], 'adopt-existing')
        recovered = self.adopt(evidence='subprocess crash proof', validation=['never-run'],
                               preflight=['never-run', '{repo}'])
        self.assertFalse(recovered['accepted'])

    def test_interrupted_intent_detects_head_and_staged_index_drift(self):
        from workspace_lifecycle import adoption
        original = adoption.save_state
        def crash_after_intent(directory, state):
            original(directory, state); raise OSError('crash after intent')
        expected = test_lifecycle.run(self.topic, 'rev-parse', 'HEAD')
        with patch.object(adoption, 'save_state', side_effect=crash_after_intent):
            with self.assertRaises(LifecycleError): self.adopt(expected_head=expected)
        (self.topic / 'README').write_text('new HEAD')
        test_lifecycle.run(self.topic, 'add', 'README'); test_lifecycle.run(self.topic, 'commit', '-m', 'drift')
        with self.assertRaisesRegex(LifecycleError, 'snapshot changed|identity mismatch'):
            self.adopt(expected_head=expected)

    def test_interrupted_intent_detects_staged_index_only_drift(self):
        from workspace_lifecycle import adoption
        original = adoption.save_state
        def crash_after_intent(directory, state):
            original(directory, state); raise OSError('crash after intent')
        (self.topic / 'README').write_text('stage one\n')
        test_lifecycle.run(self.topic, 'add', 'README')
        with patch.object(adoption, 'save_state', side_effect=crash_after_intent):
            with self.assertRaises(LifecycleError): self.adopt()
        # HEAD and porcelain's staged status remain `M `, while the index blob changes.
        (self.topic / 'README').write_text('stage two\n')
        test_lifecycle.run(self.topic, 'add', 'README')
        with self.assertRaisesRegex(LifecycleError, 'snapshot changed'):
            self.adopt()

    def test_interrupted_intent_detects_new_ignored_empty_directory(self):
        from workspace_lifecycle import adoption
        (self.f.root / '.git' / 'info' / 'exclude').write_text('generated/\n')
        original = adoption.save_state
        def crash_after_intent(directory, state):
            original(directory, state); raise OSError('crash after intent')
        with patch.object(adoption, 'save_state', side_effect=crash_after_intent):
            with self.assertRaises(LifecycleError): self.adopt()
        (self.topic / 'generated' / 'empty').mkdir(parents=True)
        with self.assertRaisesRegex(LifecycleError, 'snapshot changed'):
            self.adopt()

    def test_dirty_staged_untracked_and_ignored_are_baseline_and_cannot_be_finished_or_retired(self):
        (self.f.root / '.git' / 'info' / 'exclude').write_text('private.generated\n')
        (self.topic / 'README').write_text('staged baseline\n'); test_lifecycle.run(self.topic, 'add', 'README')
        (self.topic / 'untracked.txt').write_text('untracked baseline\n')
        (self.topic / 'private.generated').write_text('ignored baseline\n')
        self.adopt()
        plan = self.plan('adopt', self.topic / 'feature.txt', 'new work\n')
        with self.assertRaises(LifecycleError):
            finish(self.topic, task='adopt', plan_path=str(plan), result_ref='issue/adopt')
        self.assertEqual((self.topic / 'README').read_text(), 'staged baseline\n')
        self.assertTrue((self.topic / 'untracked.txt').exists())
        self.assertTrue((self.topic / 'private.generated').exists())
        with self.assertRaises(LifecycleError):
            retire(self.f.root, task='adopt', result_ref='issue/adopt', users_released=True)

    def test_finish_cannot_claim_baseline_as_commit_restore_or_archive(self):
        store = Path(self.f.temp.name) / 'authorized-private-store'; store.mkdir()
        for kind in ('commit', 'restore', 'archive'):
            task = 'baseline-' + kind
            workspace = Path(self.f.temp.name) / task
            branch = 'topic/' + task
            test_lifecycle.run(self.f.root, 'worktree', 'add', '-b', branch, str(workspace))
            dirty = workspace / 'baseline.txt'; dirty.write_text('preexisting ' + kind)
            self.adopt(task=task, branch=branch, worktree=str(workspace),
                       expected_head=test_lifecycle.run(workspace, 'rev-parse', 'HEAD'))
            entry = {'path': 'baseline.txt', 'classification': {'commit': 'source', 'restore': 'reproducible',
                     'archive': 'private'}[kind], 'owner': task, 'evidence': 'cannot claim user baseline',
                     'sha256': hashlib.sha256(dirty.read_bytes()).hexdigest()}
            if kind == 'commit': entry['safe_to_commit'] = True
            if kind == 'restore': entry['regeneration'] = {'evidence': 'rebuild reviewed'}
            if kind == 'archive':
                entry.update(store=str(store), approval_evidence='user authorized preservation', name=task + '.txt')
            plan = Path(self.f.temp.name) / (task + '.json'); plan.write_text(json.dumps({kind: [entry]}))
            with self.subTest(kind=kind):
                with self.assertRaisesRegex(LifecycleError, 'preexisting dirty'):
                    finish(workspace, task=task, plan_path=str(plan), result_ref='issue/' + task)
                self.assertEqual(dirty.read_text(), 'preexisting ' + kind)

    def test_identity_mismatch_default_duplicate_current_and_retired_are_refused(self):
        head = test_lifecycle.run(self.topic, 'rev-parse', 'HEAD')
        with self.assertRaises(LifecycleError): self.adopt(expected_head='0' * 40)
        with self.assertRaises(LifecycleError): self.adopt(branch='trunk')
        with self.assertRaises(LifecycleError): self.adopt(branch='topic/other')
        duplicate = Path(self.f.temp.name) / 'duplicate'
        test_lifecycle.run(self.f.root, 'worktree', 'add', '--detach', str(duplicate), 'topic/adopt')
        test_lifecycle.run(duplicate, 'symbolic-ref', 'HEAD', 'refs/heads/topic/adopt')
        with self.assertRaises(LifecycleError): self.adopt()
        test_lifecycle.run(self.f.root, 'worktree', 'remove', '--force', str(duplicate))
        self.adopt()
        with self.assertRaises(LifecycleError): self.adopt()
        # A retired task name remains an identity, even if its branch is still present.
        plan = self.plan('adopt', self.topic / 'f', 'f')
        finish(self.topic, task='adopt', plan_path=str(plan), result_ref='issue/adopt')
        retire(self.f.root, task='adopt', result_ref='issue/adopt', users_released=True)
        recreated = Path(self.f.temp.name) / 'recreated'
        test_lifecycle.run(self.f.root, 'worktree', 'add', str(recreated), 'topic/adopt')
        with self.assertRaises(LifecycleError):
            self.adopt(worktree=str(recreated), expected_head=head)

    def test_legacy_state_and_operation_or_protected_index_are_refused(self):
        legacy = self.f.root / '.git' / 'agent-branches'; legacy.mkdir()
        (legacy / 'state.json').write_text('{}')
        with self.assertRaisesRegex(LifecycleError, 'legacy'):
            self.adopt()
        (legacy / 'state.json').unlink(); legacy.rmdir()
        git_dir = Path(test_lifecycle.run(self.topic, 'rev-parse', '--absolute-git-dir'))
        for marker in ('MERGE_HEAD', 'REBASE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD',
                       'BISECT_START', 'index.lock', 'rebase-merge', 'rebase-apply', 'sequencer'):
            path = git_dir / marker
            with self.subTest(marker=marker):
                if marker in ('rebase-merge', 'rebase-apply', 'sequencer'):
                    path.mkdir()
                else:
                    path.write_text('interrupted operation\n')
                with self.assertRaisesRegex(LifecycleError, 'operation'):
                    self.adopt()
                if path.is_dir(): path.rmdir()
                else: path.unlink()
        test_lifecycle.run(self.topic, 'update-index', '--assume-unchanged', 'README')
        with self.assertRaisesRegex(LifecycleError, 'index'):
            self.adopt()

    def test_primary_checkout_and_unmerged_index_are_not_adoptable(self):
        with self.assertRaises(LifecycleError):
            self.adopt(worktree=str(self.f.root), branch='trunk')
        blob = test_lifecycle.run(self.topic, 'hash-object', '-w', 'README')
        process = subprocess.run(['git', '-C', str(self.topic), 'update-index', '--index-info'],
                                 input='100644 ' + blob + ' 1\tREADME\n', text=True, check=True)
        try:
            with self.assertRaisesRegex(LifecycleError, 'unmerged'):
                self.adopt()
        finally:
            test_lifecycle.run(self.topic, 'reset', '--', 'README')

    def test_dependencies_parent_and_hold_are_recorded_and_enforced(self):
        with self.assertRaisesRegex(LifecycleError, 'dependencies'):
            self.adopt(dependencies=['missing-task'])
        parent = Path(self.f.temp.name) / 'parent'
        test_lifecycle.run(self.f.root, 'worktree', 'add', '-b', 'topic/parent', str(parent))
        self.adopt(task='parent', branch='topic/parent', worktree=str(parent),
                   expected_head=test_lifecycle.run(parent, 'rev-parse', 'HEAD'))
        hold(parent, 'parent', 'waiting on review', 'release with evidence')
        with self.assertRaises(LifecycleError):
            self.adopt(task='child', parent='parent', dependencies=['parent'])
        # The task itself can be born held, but no work may start until explicit release.
        other = Path(self.f.temp.name) / 'held'
        test_lifecycle.run(self.f.root, 'worktree', 'add', '-b', 'topic/held', str(other))
        item = self.adopt(task='held', branch='topic/held', worktree=str(other),
                          expected_head=test_lifecycle.run(other, 'rev-parse', 'HEAD'),
                          hold_reason='await owner', next_action='obtain release evidence')
        self.assertFalse(item['accepted'])
        self.assertIn('hold', status(other, 'held'))

    def test_unaccepted_dependency_blocks_adopted_child_integration(self):
        parent = Path(self.f.temp.name) / 'dependency'
        test_lifecycle.run(self.f.root, 'worktree', 'add', '-b', 'topic/dependency', str(parent))
        self.adopt(task='dependency', branch='topic/dependency', worktree=str(parent),
                   expected_head=test_lifecycle.run(parent, 'rev-parse', 'HEAD'))
        self.adopt(task='child', parent='dependency', dependencies=['dependency'])
        plan = self.plan('child', self.topic / 'child.txt', 'must wait for dependency\n')
        with self.assertRaisesRegex(LifecycleError, 'dependency'):
            finish(self.topic, task='child', plan_path=str(plan), result_ref='issue/child')
        self.assertFalse((self.f.root / 'child.txt').exists())

    def test_interrupted_state_save_retries_only_if_snapshot_is_identical(self):
        from workspace_lifecycle import adoption
        original = adoption.save_state
        calls = 0
        def crash_once(directory, state):
            nonlocal calls
            calls += 1
            original(directory, state)
            if calls == 1:
                raise OSError('simulated interruption after intent write')
        with patch.object(adoption, 'save_state', side_effect=crash_once):
            with self.assertRaises(LifecycleError): self.adopt()
        # No live task exists yet, so inspect the durable operation intent directly.
        from workspace_lifecycle.state import locked_state
        with locked_state(self.f.root) as (_, state):
            self.assertEqual(state['intents']['adopt']['kind'], 'adopt-existing')
        self.adopt()
        self.assertEqual(status(self.topic, 'adopt')['live']['head'], test_lifecycle.run(self.topic, 'rev-parse', 'HEAD'))

    def test_pending_intent_refuses_changed_request_or_expected_head(self):
        from workspace_lifecycle import adoption
        original = adoption.save_state
        def crash_after_intent(directory, state):
            original(directory, state)
            raise OSError('interrupted after durable intent')
        with patch.object(adoption, 'save_state', side_effect=crash_after_intent):
            with self.assertRaises(LifecycleError): self.adopt()
        with self.assertRaisesRegex(LifecycleError, 'exact requested identity'):
            self.adopt(request='issue/different')
        with self.assertRaisesRegex(LifecycleError, 'exact requested identity'):
            self.adopt(expected_head='0' * 40)

    def test_interrupted_binding_rejects_head_or_dirty_drift_but_retry_preserves_identity(self):
        from workspace_lifecycle import adoption
        dirty = self.topic / 'baseline.txt'
        dirty.write_text('before')
        original = adoption.git
        def crash_after_binding(repo, *args, **kwargs):
            value = original(repo, *args, **kwargs)
            if args[:3] == ('config', '--local', 'branch.topic/adopt.workspaceTask'):
                raise OSError('simulated interruption after binding')
            return value
        with patch.object(adoption, 'git', side_effect=crash_after_binding):
            with self.assertRaises(LifecycleError): self.adopt()
        # Porcelain status remains ??; only the captured dirty bytes changed.
        dirty.write_text('after!')
        with self.assertRaisesRegex(LifecycleError, 'snapshot changed'):
            self.adopt()

    def test_binding_interruption_retries_when_status_index_and_content_are_unchanged(self):
        from workspace_lifecycle import adoption
        (self.topic / 'baseline.txt').write_text('same dirty status and bytes')
        original = adoption.git
        tripped = False
        def crash_once(repo, *args, **kwargs):
            nonlocal tripped
            value = original(repo, *args, **kwargs)
            if not tripped and args[:3] == ('config', '--local', 'branch.topic/adopt.workspaceTask'):
                tripped = True
                raise OSError('simulated interruption after binding')
            return value
        with patch.object(adoption, 'git', side_effect=crash_once):
            with self.assertRaises(LifecycleError): self.adopt()
        before = status(self.f.root)['tasks']
        self.adopt()
        after = status(self.topic, 'adopt')
        self.assertEqual(before, [])
        self.assertEqual(after['live']['dirty'], {'baseline.txt': '??'})
        self.assertEqual((self.topic / 'baseline.txt').read_text(), 'same dirty status and bytes')

    @unittest.skipUnless(hasattr(os, 'symlink'), 'symlink support unavailable')
    def test_symlink_and_reparse_paths_are_refused(self):
        linked = Path(self.f.temp.name) / 'linked-topic'
        try:
            os.symlink(self.topic, linked, target_is_directory=True)
        except OSError as exc:
            self.skipTest(str(exc))
        with self.assertRaisesRegex(LifecycleError, 'link|reparse'):
            self.adopt(worktree=str(linked))

    @unittest.skipUnless(hasattr(os, 'symlink'), 'symlink support unavailable')
    def test_dirty_symlink_leaf_is_preserved_as_baseline_content(self):
        # Package-manager style ignored .bin entries are leaves, not traversal paths.
        (self.f.root / '.git' / 'info' / 'exclude').write_text('node_modules/\n')
        bin_dir = self.topic / 'node_modules' / '.bin'; bin_dir.mkdir(parents=True)
        link = bin_dir / 'tool'
        try:
            os.symlink('../tool-package/bin/tool', link)
        except OSError as exc:
            self.skipTest(str(exc))
        self.adopt()
        baseline = status(self.topic, 'adopt')['baseline_dirty']
        self.assertEqual(baseline['node_modules/.bin/tool'], '!!')
        plan = Path(self.f.temp.name) / 'empty.json'; plan.write_text('{}')
        with self.assertRaises(LifecycleError):
            finish(self.topic, task='adopt', plan_path=str(plan), result_ref='issue/adopt')
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), '../tool-package/bin/tool')

    @unittest.skipUnless(os.name == 'nt', 'Windows junction test')
    def test_windows_junction_worktree_path_is_refused_without_symlink_privilege(self):
        junction = Path(self.f.temp.name) / 'junction-topic'
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(junction), str(self.topic)],
                                text=True, capture_output=True)
        if result.returncode:
            self.skipTest(result.stderr or result.stdout)
        with self.assertRaisesRegex(LifecycleError, 'link|reparse'):
            self.adopt(worktree=str(junction))


if __name__ == '__main__':
    unittest.main()
