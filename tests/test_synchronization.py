"""Real Git synchronization contracts, run against an installed package."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from workspace_lifecycle import service
from workspace_lifecycle.errors import LifecycleError


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], stderr=subprocess.PIPE, text=True).strip()


class SynchronizationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.remote, self.root, self.topic, self.other = [self.base / p for p in ('remote.git', 'root', 'topic', 'other')]
        git(self.base, 'init', '--bare', '--initial-branch=trunk', str(self.remote))
        git(self.base, 'clone', str(self.remote), str(self.root)); self.configure(self.root)
        self.commit(self.root, 'base', 'base\n'); git(self.root, 'push', 'origin', 'trunk')
        service.begin(self.root, task='same', request='issue/same', remote='origin', branch='topic/same',
                      worktree=str(self.topic), validation=['git', 'diff', '--check'],
                      preflight=[sys.executable, '-m', 'workspace_lifecycle.push', '{repo}', '--user-intent', 'push'])
        git(self.topic, 'push', 'origin', 'HEAD:refs/heads/topic/same')
        git(self.base, 'clone', '--branch', 'topic/same', str(self.remote), str(self.other)); self.configure(self.other)
        self.plan = self.base / 'plan.json'; self.plan.write_text('{}')

    def configure(self, repo):
        git(repo, 'config', 'user.name', 'Test'); git(repo, 'config', 'user.email', 'test@example.invalid')

    def commit(self, repo, path, content):
        (repo / path).write_text(content); git(repo, 'add', '--', path); git(repo, 'commit', '-m', path)
        return git(repo, 'rev-parse', 'HEAD')

    def remote_commit(self, path='remote', content='remote\n'):
        oid = self.commit(self.other, path, content); git(self.other, 'push', 'origin', 'HEAD:refs/heads/topic/same')
        return oid

    def sync(self, local=None, remote=None):
        return service.synchronize(self.topic, task='same', expected_head=local or git(self.topic, 'rev-parse', 'HEAD'),
                                   remote_head=remote or git(self.other, 'rev-parse', 'HEAD'), evidence='review/same-owner')

    def finish(self):
        return service.finish(self.topic, task='same', plan_path=str(self.plan), result_ref='result/same')

    def test_equal_ahead_fast_forward_and_divergence_remain_unaccepted(self):
        self.sync(); local = self.commit(self.topic, 'local', 'local\n'); self.sync()
        remote = self.remote_commit(); result = self.sync(local, remote); merged = git(self.topic, 'rev-parse', 'HEAD')
        self.assertEqual(git(self.topic, 'rev-list', '--parents', '-1', merged).split()[1:], [local, remote])
        self.assertEqual(result['commit'], merged)
        self.assertNotIn('acceptance', service.status(self.topic, 'same')); self.assertNotIn('integrated', service.status(self.topic, 'same'))
        git(self.topic, 'push', 'origin', 'HEAD:refs/heads/topic/same'); git(self.other, 'pull', '--ff-only')
        remote = self.remote_commit('next', 'next\n'); self.sync(merged, remote)
        self.assertEqual(git(self.topic, 'rev-parse', 'HEAD'), remote)

    def test_postcommit_push_rejection_normal_merge_and_same_finish(self):
        (self.topic / 'local').write_text('local\n')
        self.plan.write_text(json.dumps({'commit': [{'path': 'local', 'owner': 'same', 'evidence': 'review/source',
            'classification': 'source', 'safe_to_commit': True, 'sha256': hashlib.sha256((self.topic / 'local').read_bytes()).hexdigest()}]}))
        remote = self.remote_commit()
        with self.assertRaises(subprocess.CalledProcessError): self.finish()
        before = service.status(self.topic, 'same')['intent']; original = before['committed']
        self.sync(original, remote); after = service.status(self.topic, 'same')['intent']
        self.assertEqual(after['committed'], original); self.assertEqual(after['actions'], before['actions']); self.assertEqual(after['at'], before['at'])
        result = self.finish(); self.assertTrue(result['accepted'])
        self.assertEqual(git(self.topic, 'rev-list', '--first-parent', '--count', original + '..HEAD'), '1')
        self.assertEqual(git(self.remote, 'rev-parse', 'refs/heads/topic/same'), result['commit'])
        self.assertEqual((self.root / 'remote').read_text(), 'remote\n')

    def test_manual_conflicting_merge_is_adopted_and_resumed(self):
        local = self.commit(self.topic, 'base', 'local\n'); remote = self.remote_commit('base', 'remote\n')
        git(self.topic, 'fetch', 'origin', 'refs/heads/topic/same')
        with self.assertRaises(subprocess.CalledProcessError): git(self.topic, 'merge', '--no-ff', '--no-commit', remote)
        with self.assertRaises(LifecycleError): self.sync(local, remote)
        (self.topic / 'base').write_text('resolved both\n'); git(self.topic, 'add', 'base')
        self.sync(local, remote); self.assertEqual((self.topic / 'base').read_text(), 'resolved both\n')
        self.sync(local, remote)

    def test_dirty_and_stale_remote_are_preserved(self):
        remote = self.remote_commit(); local = git(self.topic, 'rev-parse', 'HEAD')
        (self.topic / 'foreign').write_text('keep\n')
        with self.assertRaises(LifecycleError): self.sync(local, remote)
        self.assertEqual((self.topic / 'foreign').read_text(), 'keep\n'); (self.topic / 'foreign').unlink()
        with self.assertRaises(LifecycleError): self.sync(local, local)
        self.assertEqual(git(self.topic, 'rev-parse', 'HEAD'), local)

    def test_remote_race_during_validation_keeps_intent(self):
        local = self.commit(self.topic, 'local', 'local\n'); remote = self.remote_commit(); validate = service._validate
        def advance(repo, argv):
            result = validate(repo, argv); self.remote_commit('raced', 'raced\n'); return result
        with patch.object(service, '_validate', side_effect=advance):
            with self.assertRaises(LifecycleError): self.sync(local, remote)
        self.assertNotIn('acceptance', service.status(self.topic, 'same')); self.assertEqual(git(self.topic, 'rev-parse', 'HEAD'), local)

    def test_integration_fast_forwards_remote_target_before_normal_merge(self):
        self.commit(self.topic, 'local', 'local\n'); git(self.other, 'checkout', 'trunk')
        remote = self.commit(self.other, 'default', 'remote default\n'); git(self.other, 'push', 'origin', 'trunk')
        result = self.finish(); self.assertTrue(result['accepted'])
        self.assertEqual(git(self.root, 'merge-base', '--is-ancestor', remote, 'HEAD'), '')
        self.assertEqual((self.root / 'default').read_text(), 'remote default\n')

    def test_remote_race_retry_then_additional_sync(self):
        local = self.commit(self.topic, 'local', 'local\n'); remote = self.remote_commit()
        validate = service._validate
        def advance(repo, argv):
            result = validate(repo, argv); self.remote_commit('raced', 'raced\n'); return result
        with patch.object(service, '_validate', side_effect=advance):
            with self.assertRaises(LifecycleError): self.sync(local, remote)
        first = self.sync(local, remote)['commit']
        latest = git(self.other, 'rev-parse', 'HEAD')
        second = self.sync(first, latest)['commit']
        self.assertEqual(git(self.topic, 'merge-base', '--is-ancestor', latest, second), '')
        self.assertNotIn('acceptance', service.status(self.topic, 'same'))

    def test_sync_rejects_unrelated_staged_changes_on_retry(self):
        local = self.commit(self.topic, 'base', 'local\n'); remote = self.remote_commit('base', 'remote\n')
        with self.assertRaises(LifecycleError): self.sync(local, remote)
        (self.topic / 'base').write_text('resolved\n'); git(self.topic, 'add', 'base')
        (self.topic / 'unrelated').write_text('someone else\n'); git(self.topic, 'add', 'unrelated')
        with self.assertRaisesRegex(LifecycleError, 'outside the recorded conflict'): self.sync(local, remote)
        self.assertEqual(git(self.topic, 'rev-parse', 'HEAD'), local)
        self.assertEqual((self.topic / 'unrelated').read_text(), 'someone else\n')

    def test_wrong_manual_source_and_unrelated_history_are_rejected(self):
        local = self.commit(self.topic, 'local', 'local\n'); remote = self.remote_commit()
        wrong = self.commit(self.root, 'wrong', 'wrong\n')
        git(self.topic, 'merge', '--no-ff', '--no-commit', wrong)
        with self.assertRaisesRegex(LifecycleError, 'manual merge source'): self.sync(local, remote)
        self.assertEqual(git(self.topic, 'rev-parse', 'MERGE_HEAD'), wrong)

    def test_unrelated_remote_history_is_rejected(self):
        # Move only this disposable remote fixture to an independently rooted history.
        git(self.other, 'checkout', '--orphan', 'unrelated')
        git(self.other, 'commit', '-m', 'independent root')
        unrelated = git(self.other, 'rev-parse', 'HEAD')
        git(self.other, 'push', 'origin', 'HEAD:refs/heads/unrelated')
        git(self.remote, 'update-ref', 'refs/heads/topic/same', unrelated)
        with self.assertRaisesRegex(LifecycleError, 'unrelated histories'): self.sync(remote=unrelated)

    def test_head_race_during_sync_validation_preserves_foreign_commit(self):
        local = self.commit(self.topic, 'local', 'local\n'); remote = self.remote_commit(); validate = service._validate
        foreign = []
        def advance(repo, argv):
            result = validate(repo, argv)
            git(repo, 'commit', '-m', 'native writer completed merge')
            foreign.append(git(repo, 'rev-parse', 'HEAD'))
            return result
        with patch.object(service, '_validate', side_effect=advance):
            with self.assertRaises(LifecycleError): self.sync(local, remote)
        self.assertEqual(git(self.topic, 'rev-parse', 'HEAD'), foreign[0])
        self.assertNotIn('acceptance', service.status(self.topic, 'same'))

    def test_sync_commit_response_loss_replays_without_recommit(self):
        local = self.commit(self.topic, 'local', 'local\n'); remote = self.remote_commit()
        native = service.git
        def lose(repo, *args, **kwargs):
            result = native(repo, *args, **kwargs)
            if args[:1] == ('commit',): raise OSError('lost commit response')
            return result
        with patch.object(service, 'git', side_effect=lose):
            with self.assertRaises(LifecycleError): self.sync(local, remote)
        committed = git(self.topic, 'rev-parse', 'HEAD')
        self.assertEqual(self.sync(local, remote)['commit'], committed)

    def test_push_response_loss_proves_saved_commit(self):
        self.commit(self.topic, 'local', 'local\n'); native = subprocess.run
        def lose(argv, **kwargs):
            result = native(argv, **kwargs)
            if argv[:2] == ['git', 'push']:
                raise subprocess.CalledProcessError(1, argv, stderr='response lost')
            return result
        with patch.object(service.subprocess, 'run', side_effect=lose):
            result = self.finish()
        self.assertTrue(result['accepted'])
        self.assertTrue(service.status(self.topic, 'same')['acceptance']['push']['response_recovered'])

    def test_remote_advances_after_push_without_accepting_third_party_head(self):
        original = self.commit(self.topic, 'local', 'local\n'); native = subprocess.run
        advanced = []
        def race(argv, **kwargs):
            result = native(argv, **kwargs)
            if argv[:2] == ['git', 'push'] and argv[-1] == 'HEAD:refs/heads/topic/same' and not advanced:
                git(self.other, 'pull', '--ff-only'); advanced.append(self.remote_commit('later', 'later\n'))
            return result
        with patch.object(service.subprocess, 'run', side_effect=race): result = self.finish()
        self.assertEqual(result['commit'], original)
        accepted = service.status(self.topic, 'same')['acceptance']
        self.assertEqual(accepted['commit'], original)
        self.assertEqual(accepted['push']['remote_observed'], advanced[0])
        self.assertFalse((self.root / 'later').exists())

    def test_preserved_archive_and_changed_source_survive_finish_sync(self):
        (self.topic / 'base').write_text('local\n'); (self.topic / 'private').write_text('retain privately\n')
        store = self.base / 'store'; store.mkdir()
        self.plan.write_text(json.dumps({'commit': [{'path': 'base', 'owner': 'same', 'evidence': 'review/source',
            'classification': 'source', 'safe_to_commit': True, 'sha256': hashlib.sha256((self.topic / 'base').read_bytes()).hexdigest()}],
            'archive': [{'path': 'private', 'owner': 'same', 'evidence': 'review/private', 'classification': 'private',
                'sha256': hashlib.sha256((self.topic / 'private').read_bytes()).hexdigest(), 'store': str(store),
                'approval_evidence': 'review/preservation'}]}))
        remote = self.remote_commit('base', 'remote\n')
        with self.assertRaises(subprocess.CalledProcessError): self.finish()
        intent = service.status(self.topic, 'same')['intent']; original = intent['committed']
        self.assertEqual(intent['actions']['private']['phase'], 'resolved')
        with self.assertRaises(LifecycleError): self.sync(original, remote)
        (self.topic / 'base').write_text('resolved both\n'); git(self.topic, 'add', 'base')
        self.sync(original, remote); self.finish()
        receipt = service.status(self.topic, 'same')['finish_receipts'][-1]
        self.assertEqual(receipt['actions'], intent['actions']); self.assertEqual(receipt['committed'], original)
        self.assertEqual(Path(receipt['actions']['private']['destination']).read_text(), 'retain privately\n')

    def test_integration_sync_conflict_resumes_same_finish(self):
        self.commit(self.topic, 'local', 'local\n')
        local = self.commit(self.root, 'base', 'local default\n')
        git(self.other, 'checkout', 'trunk')
        remote = self.commit(self.other, 'base', 'remote default\n'); git(self.other, 'push', 'origin', 'trunk')
        with self.assertRaises(LifecycleError): self.finish()
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), local)
        (self.root / 'base').write_text('resolved default\n'); git(self.root, 'add', 'base')
        result = self.finish(); self.assertTrue(result['accepted'])
        self.assertEqual(git(self.root, 'merge-base', '--is-ancestor', remote, 'HEAD'), '')
        self.assertEqual((self.root / 'local').read_text(), 'local\n')

    def test_manual_merge_started_before_remote_advance_is_adopted(self):
        local = self.commit(self.topic, 'local', 'local\n'); source = self.remote_commit()
        git(self.topic, 'fetch', 'origin', 'refs/heads/topic/same')
        git(self.topic, 'merge', '--no-ff', '--no-commit', source)
        newer = self.remote_commit('newer', 'newer\n')
        first = self.sync(local, source)['commit']
        self.sync(first, newer)
        self.assertEqual((self.topic / 'newer').read_text(), 'newer\n')

    def test_remote_rollback_after_completed_sync_is_refused(self):
        old = git(self.other, 'rev-parse', 'HEAD'); remote = self.remote_commit()
        self.sync(remote=remote)
        git(self.remote, 'update-ref', 'refs/heads/topic/same', old, remote)
        with self.assertRaisesRegex(LifecycleError, 'rewritten'): self.sync(remote=old)

    def test_sync_remote_url_change_during_validation_is_refused(self):
        local = self.commit(self.topic, 'local', 'local\n'); remote = self.remote_commit()
        replacement = self.base / 'replacement.git'; git(self.base, 'clone', '--bare', str(self.remote), str(replacement))
        validate = service._validate
        def change(repo, argv):
            result = validate(repo, argv); git(repo, 'remote', 'set-url', 'origin', str(replacement)); return result
        with patch.object(service, '_validate', side_effect=change):
            with self.assertRaises(LifecycleError): self.sync(local, remote)
        self.assertEqual(git(self.topic, 'rev-parse', 'HEAD'), local)

    def test_completed_exact_retry_rejects_remote_rollback(self):
        old = git(self.other, 'rev-parse', 'HEAD'); remote = self.remote_commit()
        self.sync(old, remote)
        git(self.remote, 'update-ref', 'refs/heads/topic/same', old, remote)
        with self.assertRaisesRegex(LifecycleError, 'no longer preserves'): self.sync(old, remote)

    def test_manual_rename_merge_uses_native_merge_contract(self):
        git(self.topic, 'mv', 'base', 'renamed'); git(self.topic, 'commit', '-m', 'rename')
        local = git(self.topic, 'rev-parse', 'HEAD'); remote = self.remote_commit('base', 'remote edit\n')
        git(self.topic, 'fetch', 'origin', 'refs/heads/topic/same')
        git(self.topic, 'merge', '--no-ff', '--no-commit', remote)
        self.sync(local, remote)
        self.assertEqual((self.topic / 'renamed').read_text(), 'remote edit\n')

    def test_integration_postcommit_push_rejection_conflict_resumes(self):
        self.commit(self.topic, 'base', 'topic edit\n')
        git(self.other, 'checkout', 'trunk')
        native = service._push; raced = []
        def race(repo, preflight, branch, remote=None, expected_head=None):
            if branch == 'trunk' and not raced:
                raced.append(self.commit(self.other, 'base', 'remote default edit\n'))
                git(self.other, 'push', 'origin', 'trunk')
            return native(repo, preflight, branch, remote, expected_head)
        with patch.object(service, '_push', side_effect=race):
            with self.assertRaises(subprocess.CalledProcessError): self.finish()
        original_merge = service.status(self.topic, 'same')['intent']['merged']
        with self.assertRaises(LifecycleError): self.finish()
        (self.root / 'base').write_text('resolved integration\n'); git(self.root, 'add', 'base')
        result = self.finish(); self.assertTrue(result['accepted'])
        self.assertEqual(git(self.root, 'merge-base', '--is-ancestor', original_merge, 'HEAD'), '')
        self.assertEqual(git(self.root, 'merge-base', '--is-ancestor', raced[0], 'HEAD'), '')
