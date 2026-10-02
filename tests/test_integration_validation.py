"""Integration validates the candidate after the target contains its source."""
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
    return subprocess.check_output(['git', '-C', str(repo), *args],
                                   stderr=subprocess.PIPE, text=True).strip()


class IntegrationValidationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.remote, self.root, self.topic, self.other = [
            self.base / name for name in ('remote.git', 'root', 'topic', 'other')]
        git(self.base, 'init', '--bare', '--initial-branch=trunk', str(self.remote))
        git(self.base, 'clone', str(self.remote), str(self.root))
        self.configure(self.root)
        self.commit(self.root, 'base', 'base\n')
        git(self.root, 'push', 'origin', 'trunk')
        self.validation = [sys.executable, '-c',
                           "from pathlib import Path; assert Path('candidate').read_text() == 'candidate accepted\\n'; "
                           "assert not Path('candidate-deny').exists()"]
        self.preflight = [sys.executable, '-m', 'workspace_lifecycle.push',
                          '{repo}', '--user-intent', 'push']
        git(self.base, 'clone', str(self.remote), str(self.other))
        self.configure(self.other)
        self.plan = self.base / 'plan.json'
        self.plan.write_text('{}')

    def begin_candidate(self, parent=None, dirty=False):
        service.begin(self.root, task='candidate', request='issue/12', remote='origin',
                      branch='topic/candidate', worktree=str(self.topic),
                      parent=parent, validation=self.validation, preflight=self.preflight)
        if dirty:
            (self.topic / 'candidate').write_text('candidate accepted\n')
            self.plan.write_text(json.dumps({'commit': [{
                'path': 'candidate', 'classification': 'source', 'owner': 'candidate',
                'evidence': 'review/candidate-source', 'safe_to_commit': True,
                'sha256': hashlib.sha256((self.topic / 'candidate').read_bytes()).hexdigest()}]}))
        else:
            self.source = self.commit(self.topic, 'candidate', 'candidate accepted\n')

    def begin_parent(self):
        self.parent = self.base / 'parent'
        self.parent_validation = [sys.executable, '-c',
                                  "from pathlib import Path; assert Path('parent').read_text() == 'parent accepted\\n'; "
                                  "assert not Path('parent-deny').exists()"]
        service.begin(self.root, task='parent', request='issue/parent', remote='origin',
                      branch='topic/parent', worktree=str(self.parent),
                      validation=self.parent_validation, preflight=self.preflight)
        self.commit(self.parent, 'parent', 'parent accepted\n')
        service.finish(self.parent, task='parent', plan_path=str(self.plan), result_ref='result/parent')
        git(self.other, 'fetch', 'origin')
        git(self.other, 'checkout', '-b', 'topic/parent', 'origin/topic/parent')
        self.begin_candidate(parent='parent')

    def legacy_pending(self, target):
        native = service._synchronize

        def old_validation(repo, **kwargs):
            if Path(repo) == target and not (target / 'candidate').exists():
                kwargs['validation'] = [self.validation] + [
                    argv for argv in kwargs['validation'] if argv != self.validation]
            return native(repo, **kwargs)

        with patch.object(service, '_synchronize', side_effect=old_validation):
            with self.assertRaisesRegex(LifecycleError, 'configured validation failed'):
                self.finish()
        state = service.status(self.topic, 'candidate')
        self.source = state['acceptance']['commit']
        self.assertNotIn('integrated', state)
        self.assertNotIn('commit', state['integration_synchronization']['synchronization'])
        return state

    def configure(self, repo):
        git(repo, 'config', 'user.name', 'Test')
        git(repo, 'config', 'user.email', 'test@example.invalid')

    def commit(self, repo, path, content):
        (repo / path).write_text(content)
        git(repo, 'add', '--', path)
        git(repo, 'commit', '-m', path)
        return git(repo, 'rev-parse', 'HEAD')

    def remote_commit(self, path='remote', content='remote accepted\n', branch='trunk'):
        oid = self.commit(self.other, path, content)
        git(self.other, 'push', 'origin', 'HEAD:refs/heads/' + branch)
        return oid

    def finish(self):
        return service.finish(self.topic, task='candidate', plan_path=str(self.plan),
                              result_ref='result/12')

    def test_remote_fast_forward_then_candidate_merge_and_push(self):
        self.begin_candidate()
        remote = self.remote_commit()
        result = self.finish()
        self.assertTrue(result['accepted'])
        merged = result['integration']['commit']
        self.assertEqual(git(self.root, 'rev-list', '--parents', '-1', merged).split()[1:],
                         [remote, self.source])
        self.assertEqual((self.root / 'candidate').read_text(), 'candidate accepted\n')
        self.assertEqual((self.root / 'remote').read_text(), 'remote accepted\n')
        self.assertEqual(git(self.remote, 'rev-parse', 'refs/heads/trunk'), merged)

    def test_legacy_failed_fast_forward_resumes_original_source_plan_and_result(self):
        self.begin_candidate(dirty=True)
        plan = self.plan.read_bytes()
        local = git(self.root, 'rev-parse', 'HEAD')
        remote = self.remote_commit()
        before = self.legacy_pending(self.root)
        pending = before['integration_synchronization']['synchronization']
        self.assertEqual((pending['local'], pending['source']), (local, remote))
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), remote)
        result = self.finish()
        after = service.status(self.topic, 'candidate')
        resumed = after['integration_synchronization']['synchronization']
        self.assertEqual((resumed['local'], resumed['source'], resumed['at']),
                         (pending['local'], pending['source'], pending['at']))
        self.assertEqual(resumed['commit'], remote)
        self.assertEqual(after['acceptance']['commit'], self.source)
        self.assertEqual(result['commit'], self.source)
        self.assertEqual(result['result_ref'], 'result/12')
        self.assertEqual(self.plan.read_bytes(), plan)
        self.assertEqual(git(self.topic, 'rev-list', '--count', self.source + '..HEAD'), '0')
        self.assertEqual((self.root / 'candidate').read_text(), 'candidate accepted\n')
        self.assertEqual(git(self.remote, 'rev-parse', 'refs/heads/trunk'), result['integration']['commit'])

    def test_new_remote_tip_is_synchronized_before_candidate_after_legacy_pending(self):
        self.validation[-1] += "; assert Path('base').read_text() == 'base\\n'"
        self.begin_candidate()
        original = self.remote_commit('base', 'incompatible target\n')
        before = self.legacy_pending(self.root)
        newest = self.remote_commit('base', 'base\n')
        result = self.finish()
        merged = result['integration']['commit']
        self.assertEqual((self.root / 'base').read_text(), 'base\n')
        self.assertEqual((self.root / 'candidate').read_text(), 'candidate accepted\n')
        self.assertEqual(git(self.root, 'merge-base', '--is-ancestor', newest, merged), '')
        history = service.status(self.topic, 'candidate')['integration_synchronization']['synchronization_history']
        self.assertEqual(history[0]['source'], original)
        self.assertEqual(history[0]['local'], before['integration_synchronization']['synchronization']['local'])
        self.assertEqual(git(self.remote, 'rev-parse', 'refs/heads/trunk'), merged)

    def test_target_divergence_merges_remote_before_candidate(self):
        self.begin_candidate()
        local = self.commit(self.root, 'local', 'local accepted\n')
        remote = self.remote_commit()
        result = self.finish()
        target = git(self.root, 'rev-parse', result['integration']['commit'] + '^1')
        self.assertEqual(git(self.root, 'rev-list', '--parents', '-1', target).split()[1:], [local, remote])
        self.assertEqual((self.root / 'candidate').read_text(), 'candidate accepted\n')
        self.assertEqual((self.root / 'local').read_text(), 'local accepted\n')
        self.assertEqual((self.root / 'remote').read_text(), 'remote accepted\n')

    def test_target_remote_conflict_resolves_in_same_pending_finish(self):
        self.begin_candidate()
        local = self.commit(self.root, 'base', 'local default\n')
        remote = self.remote_commit('base', 'remote default\n')
        with self.assertRaises(LifecycleError):
            self.finish()
        before = service.status(self.topic, 'candidate')
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), local)
        self.assertEqual(git(self.root, 'rev-parse', 'MERGE_HEAD'), remote)
        (self.root / 'base').write_text('resolved default\n')
        git(self.root, 'add', '--', 'base')
        result = self.finish()
        after = service.status(self.topic, 'candidate')
        self.assertEqual(after['acceptance']['commit'], before['acceptance']['commit'])
        self.assertEqual((self.root / 'base').read_text(), 'resolved default\n')
        self.assertEqual((self.root / 'candidate').read_text(), 'candidate accepted\n')
        self.assertEqual(git(self.remote, 'rev-parse', 'refs/heads/trunk'), result['integration']['commit'])

    def test_foreign_dirty_during_legacy_pending_is_preserved_and_refused(self):
        self.begin_candidate()
        remote = self.remote_commit()
        before = self.legacy_pending(self.root)
        (self.root / 'foreign').write_text('retain user data\n')
        with self.assertRaises(LifecycleError):
            self.finish()
        after = service.status(self.topic, 'candidate')
        self.assertEqual((self.root / 'foreign').read_text(), 'retain user data\n')
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), remote)
        self.assertEqual(after['integration_synchronization'], before['integration_synchronization'])
        self.assertNotIn('integrated', after)
        self.assertFalse((self.root / 'candidate').exists())

    def test_foreign_head_during_legacy_pending_is_preserved_and_refused(self):
        self.begin_candidate()
        self.remote_commit()
        before = self.legacy_pending(self.root)
        foreign = self.commit(self.root, 'foreign', 'retain foreign commit\n')
        with self.assertRaises(LifecycleError):
            self.finish()
        after = service.status(self.topic, 'candidate')
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), foreign)
        self.assertEqual((self.root / 'foreign').read_text(), 'retain foreign commit\n')
        self.assertEqual(after['integration_synchronization'], before['integration_synchronization'])
        self.assertNotIn('integrated', after)

    def test_parent_validator_runs_before_candidate_and_both_on_merged_result(self):
        self.begin_parent()
        remote = self.remote_commit(branch='topic/parent')
        observations = []
        native = service._validate

        def validate(repo, argv):
            if Path(repo) == self.parent:
                observations.append((argv, git(repo, 'rev-parse', 'HEAD'),
                                     (repo / 'candidate').exists()))
            return native(repo, argv)

        with patch.object(service, '_validate', side_effect=validate):
            result = self.finish()
        merged = result['integration']['commit']
        self.assertEqual(observations[0], (self.parent_validation, remote, False))
        self.assertIn((self.validation, remote, True), observations)
        self.assertIn((self.parent_validation, remote, True), observations)
        self.assertIn((self.validation, merged, True), observations)
        self.assertIn((self.parent_validation, merged, True), observations)
        self.assertEqual((self.root / 'candidate').read_text(), 'candidate accepted\n')
        self.assertEqual(git(self.remote, 'rev-parse', 'refs/heads/topic/parent'), merged)
        self.assertEqual(service.status(self.parent, 'parent')['acceptance']['commit'], merged)

    def test_real_parent_validation_failure_blocks_precandidate_sync(self):
        self.begin_parent()
        remote = self.remote_commit('parent', 'invalid parent\n', branch='topic/parent')
        with self.assertRaisesRegex(LifecycleError, 'configured validation failed'):
            self.finish()
        state = service.status(self.topic, 'candidate')
        self.assertEqual(git(self.parent, 'rev-parse', 'HEAD'), remote)
        self.assertEqual((self.parent / 'parent').read_text(), 'invalid parent\n')
        self.assertFalse((self.parent / 'candidate').exists())
        self.assertNotIn('integrated', state)
        self.assertNotIn('commit', state['integration_synchronization']['synchronization'])

    def postcandidate_remote_race(self, path, content):
        native = service._push
        raced = []

        def push(repo, preflight, branch, remote=None, expected_head=None):
            if branch == 'topic/parent' and not raced:
                raced.append(self.remote_commit(path, content, branch='topic/parent'))
            return native(repo, preflight, branch, remote, expected_head)

        with patch.object(service, '_push', side_effect=push):
            with self.assertRaises(subprocess.CalledProcessError):
                self.finish()
        return raced[0], service.status(self.topic, 'candidate')['intent']['merged']

    def test_postcandidate_resync_validates_both_contracts_and_pushes(self):
        self.begin_parent()
        remote, original = self.postcandidate_remote_race('later', 'later accepted\n')
        observations = []
        native = service._validate

        def validate(repo, argv):
            result = native(repo, argv)
            if Path(repo) == self.parent and (repo / 'later').exists():
                observations.append((argv, (repo / 'candidate').read_text(),
                                     (repo / 'parent').read_text(), (repo / 'later').read_text()))
            return result

        with patch.object(service, '_validate', side_effect=validate):
            result = self.finish()
        merged = result['integration']['commit']
        self.assertEqual(git(self.parent, 'rev-list', '--parents', '-1', merged).split()[1:], [original, remote])
        for argv in (self.validation, self.parent_validation):
            self.assertIn((argv, 'candidate accepted\n', 'parent accepted\n', 'later accepted\n'), observations)
        self.assertEqual(git(self.remote, 'rev-parse', 'refs/heads/topic/parent'), merged)
        self.assertEqual((self.root / 'later').read_text(), 'later accepted\n')

    def test_postcandidate_resync_candidate_failure_blocks_push_and_integration(self):
        self.assert_postcandidate_failure('candidate-deny')

    def test_postcandidate_resync_parent_failure_blocks_push_and_integration(self):
        self.assert_postcandidate_failure('parent-deny')

    def assert_postcandidate_failure(self, path):
        self.begin_parent()
        accepted_parent = service.status(self.parent, 'parent')['acceptance']['commit']
        remote, original = self.postcandidate_remote_race(path, 'validation must fail\n')
        with self.assertRaisesRegex(LifecycleError, 'configured validation failed'):
            self.finish()
        state = service.status(self.topic, 'candidate')
        self.assertEqual((self.parent / 'candidate').read_text(), 'candidate accepted\n')
        self.assertEqual((self.parent / path).read_text(), 'validation must fail\n')
        self.assertEqual(git(self.parent, 'rev-parse', 'HEAD'), original)
        self.assertEqual(git(self.parent, 'rev-parse', 'MERGE_HEAD'), remote)
        self.assertEqual(git(self.remote, 'rev-parse', 'refs/heads/topic/parent'), remote)
        self.assertEqual(service.status(self.parent, 'parent')['acceptance']['commit'], accepted_parent)
        self.assertNotIn('integrated', state)
        self.assertNotIn('commit', state['intent']['synchronization'])


if __name__ == '__main__':
    unittest.main()
