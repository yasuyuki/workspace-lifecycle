"""Integration validates the candidate after the target contains its source."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import late_data
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

    def counted_finish(self, after=None):
        runs = []
        native = service._validate

        def validate(repo, argv):
            evidence = native(repo, argv)
            runs.append(Path(repo))
            if after:
                after(Path(repo), runs)
            return evidence

        with patch.object(service, '_validate', side_effect=validate):
            return self.finish(), runs

    def test_unchanged_candidate_validates_once_in_each_checkout(self):
        self.begin_candidate()
        result, runs = self.counted_finish()
        self.assertEqual(runs, [self.topic, self.root])
        state = service.status(self.topic, 'candidate')
        validation = state['acceptance']['validation']
        self.assertEqual(list(validation), ['postcommit'])
        self.assertEqual(validation['postcommit']['returncode'], 0)
        self.assertNotIn('reused', state['integrated']['validation'])
        self.assertEqual(state['integrated']['validation']['returncode'], 0)
        self.assertEqual(git(self.remote, 'rev-parse', 'refs/heads/trunk'), result['integration']['commit'])

    def test_validation_does_not_inherit_the_calling_session_binding(self):
        observed = self.base / 'observed'
        self.validation[-1] += (
            "; import json, os, sys; open(sys.argv[2], 'a').write(json.dumps({"
            "'leaked': sorted(k for k in os.environ if k.upper().startswith('WORKSPACE_LIFECYCLE_')), "
            "'sentinel': os.environ.get('VALIDATION_SENTINEL'), 'path': os.environ.get('PATH'), "
            "'cwd': os.getcwd(), 'repo': sys.argv[1]}) + '\\n')")
        self.validation += ['{repo}', str(observed)]
        self.begin_candidate()
        foreign = {'WORKSPACE_LIFECYCLE_TASK': 'other-task', 'WORKSPACE_LIFECYCLE_REPO': str(self.other),
                   'WORKSPACE_LIFECYCLE_CONTEXT': json.dumps({'task': 'other-task', 'repo': str(self.other)}),
                   'WORKSPACE_LIFECYCLE_USE': 'f' * 32, 'WORKSPACE_LIFECYCLE_FUTURE_BINDING': 'other-task',
                   'Workspace_Lifecycle_Mixed_Case': 'other-task', 'VALIDATION_SENTINEL': 'kept'}
        with patch.dict(os.environ, foreign):
            result, runs = self.counted_finish()
        self.assertEqual(runs, [self.topic, self.root])
        self.assertEqual(git(self.remote, 'rev-parse', 'refs/heads/trunk'), result['integration']['commit'])
        seen = [json.loads(line) for line in observed.read_text().splitlines()]
        self.assertEqual([(Path(row['cwd']).resolve(), Path(row['repo'])) for row in seen],
                         [(self.topic, self.topic), (self.root, self.root)])
        for row in seen:
            self.assertEqual((row['leaked'], row['sentinel'], row['path']), ([], 'kept', os.environ['PATH']))

    def test_advanced_base_validates_merged_content_once(self):
        self.begin_candidate()
        self.remote_commit()
        _, runs = self.counted_finish()
        self.assertEqual(runs, [self.topic, self.root])
        self.assertEqual((self.root / 'remote').read_text(), 'remote accepted\n')

    def source_plan(self, name):
        return {'commit': [{
            'path': name, 'classification': 'source', 'owner': 'candidate',
            'evidence': 'review/candidate-source', 'safe_to_commit': True,
            'sha256': hashlib.sha256((self.topic / name).read_bytes()).hexdigest()}]}

    def test_finish_commit_validates_the_final_topic_head_once(self):
        observed = self.base / 'observed'
        self.validation[-1] += ("; import subprocess, sys; open(sys.argv[1], 'a').write("
                                "subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True))")
        self.validation.append(str(observed))
        self.begin_candidate(dirty=True)
        initial = git(self.topic, 'rev-parse', 'HEAD')
        result, runs = self.counted_finish()
        self.assertEqual(runs, [self.topic, self.root])
        state = service.status(self.topic, 'candidate')
        accepted = state['acceptance']['commit']
        self.assertEqual(git(self.topic, 'rev-list', '--parents', '-1', accepted).split()[1:], [initial])
        self.assertEqual(observed.read_text().split(), [accepted, result['integration']['commit']])
        self.assertEqual(list(state['acceptance']['validation']), ['postcommit'])
        self.assertEqual(git(self.remote, 'rev-parse', 'refs/heads/topic/candidate'), accepted)

    def test_restore_and_commit_validate_the_topic_once(self):
        self.begin_candidate(dirty=True)
        (self.topic / 'base').write_text('regenerated\n')
        plan = json.loads(self.plan.read_text())
        plan['restore'] = [{'path': 'base', 'classification': 'reproducible', 'owner': 'candidate',
                            'evidence': 'review/regenerated', 'regeneration': {'evidence': 'rebuild verified'},
                            'sha256': hashlib.sha256((self.topic / 'base').read_bytes()).hexdigest()}]
        self.plan.write_text(json.dumps(plan))
        _, runs = self.counted_finish()
        self.assertEqual(runs, [self.topic, self.root])
        self.assertEqual((self.topic / 'base').read_text(), 'base\n')

    def test_rejected_restore_only_finish_is_corrected_and_resumed(self):
        rejecting = [sys.executable, '-c', 'raise SystemExit(1)']
        self.validation[:] = rejecting
        self.begin_candidate()
        (self.topic / 'base').write_text('regenerated\n')
        self.plan.write_text(json.dumps({'restore': [{
            'path': 'base', 'classification': 'reproducible', 'owner': 'candidate',
            'evidence': 'review/regenerated', 'regeneration': {'evidence': 'rebuild verified'},
            'sha256': hashlib.sha256((self.topic / 'base').read_bytes()).hexdigest()}]}))
        with self.assertRaisesRegex(LifecycleError, 'configured validation failed'):
            self.finish()
        intent = service.status(self.topic, 'candidate')['intent']
        self.assertEqual([a['phase'] for a in intent['actions'].values()], ['resolved'])
        self.assertNotIn('committed', intent)
        self.assertEqual((self.topic / 'base').read_text(), 'base\n')
        self.assertEqual(git(self.remote, 'for-each-ref', 'refs/heads/topic/candidate'), '')
        service.update_validation(self.topic, task='candidate', expected_head=self.source,
                                  expected_validation=rejecting, validation=[sys.executable, '-c', 'pass'],
                                  evidence='issue/39 correction')
        self.assertEqual(service.status(self.topic, 'candidate')['intent'], intent)
        result, runs = self.counted_finish()
        self.assertEqual(runs, [self.topic, self.root])
        self.assertEqual(result['commit'], self.source)
        self.assertEqual(git(self.remote, 'rev-parse', 'refs/heads/topic/candidate'), self.source)

    def test_validator_dirt_is_refused_after_one_run(self):
        self.begin_candidate()

        runs = []

        def generate(repo, runs_so_far):
            runs[:] = runs_so_far
            if repo == self.topic and len(runs) == 1:
                (repo / 'generated.pyc').write_bytes(b'')

        with self.assertRaisesRegex(LifecycleError, 'unresolved dirty'):
            self.counted_finish(generate)
        self.assertEqual(runs, [self.topic])
        self.assertNotIn('acceptance', service.status(self.topic, 'candidate'))
        self.assertEqual(git(self.remote, 'for-each-ref', 'refs/heads/topic/candidate'), '')

    def test_rejected_finish_commit_pushes_nothing_until_corrected(self):
        self.validation[:] = [sys.executable, '-c',
                              "import subprocess, sys; subject = subprocess.check_output("
                              "['git', 'log', '-1', '--format=%s'], text=True); "
                              "sys.exit(subject.strip() == 'workspace lifecycle completion')"]
        rejecting = list(self.validation)
        self.begin_candidate(dirty=True)
        initial = git(self.topic, 'rev-parse', 'HEAD')
        trunk = git(self.remote, 'rev-parse', 'refs/heads/trunk')
        for _ in range(2):
            with self.assertRaisesRegex(LifecycleError, 'configured validation failed'):
                self.finish()
            state = service.status(self.topic, 'candidate')
            rejected = state['intent']['committed']
            self.assertEqual(git(self.topic, 'rev-parse', 'HEAD'), rejected)
            self.assertEqual(git(self.topic, 'rev-list', '--parents', '-1', rejected).split()[1:], [initial])
            self.assertNotIn('acceptance', state)
            self.assertEqual(git(self.remote, 'for-each-ref', 'refs/heads/topic/candidate'), '')
            self.assertEqual(git(self.remote, 'rev-parse', 'refs/heads/trunk'), trunk)
        second = self.base / 'second'
        empty = self.base / 'empty.json'
        empty.write_text('{}')
        service.begin(self.root, task='second', request='issue/13', remote='origin',
                      branch='topic/second', worktree=str(second),
                      validation=[sys.executable, '-c', 'pass'], preflight=self.preflight)
        self.configure(second)
        self.commit(second, 'second', 'second\n')
        self.assertTrue(service.finish(second, task='second', plan_path=str(empty), result_ref='result/13')['accepted'])
        trunk = git(self.remote, 'rev-parse', 'refs/heads/trunk')
        self.assertNotEqual(subprocess.run(['git', '-C', str(self.remote), 'merge-base', '--is-ancestor',
                                            rejected, trunk]).returncode, 0)
        service.update_validation(self.topic, task='candidate', expected_head=rejected,
                                  expected_validation=rejecting, validation=[sys.executable, '-c', 'pass'],
                                  evidence='issue/39 correction')
        result, runs = self.counted_finish()
        self.assertEqual(runs, [self.topic, self.root])
        self.assertEqual(result['commit'], rejected)
        self.assertEqual(git(self.remote, 'rev-parse', 'refs/heads/topic/candidate'), rejected)
        self.assertEqual(git(self.remote, 'rev-parse', 'refs/heads/trunk'), result['integration']['commit'])

    def test_rejected_finish_commit_is_fixed_by_a_revised_plan(self):
        self.begin_candidate(dirty=True)
        (self.topic / 'candidate').write_text('candidate broken\n')
        self.plan.write_text(json.dumps(self.source_plan('candidate')))
        with self.assertRaisesRegex(LifecycleError, 'configured validation failed'):
            self.finish()
        rejected = git(self.topic, 'rev-parse', 'HEAD')
        self.assertEqual(service.status(self.topic, 'candidate')['intent']['committed'], rejected)
        self.assertEqual(git(self.remote, 'for-each-ref', 'refs/heads/topic/candidate'), '')
        (self.topic / 'candidate').write_text('candidate accepted\n')
        self.plan.write_text(json.dumps(self.source_plan('candidate')))
        runs = []
        native = service._validate

        def validate(repo, argv):
            runs.append(Path(repo))
            return native(repo, argv)

        with patch.object(service, '_validate', side_effect=validate):
            result = service.finish(self.topic, task='candidate', plan_path=str(self.plan),
                                    result_ref='result/12', revision_evidence='review/candidate-fix')
        self.assertEqual(runs, [self.topic, self.root])
        self.assertEqual(git(self.topic, 'rev-list', '--parents', '-1', result['commit']).split()[1:], [rejected])
        self.assertEqual(git(self.remote, 'rev-parse', 'refs/heads/topic/candidate'), result['commit'])
        self.assertEqual((self.root / 'candidate').read_text(), 'candidate accepted\n')

    def test_rejected_parent_commit_blocks_child_integration(self):
        self.begin_parent()
        accepted = git(self.remote, 'rev-parse', 'refs/heads/topic/parent')
        trunk = git(self.remote, 'rev-parse', 'refs/heads/trunk')
        (self.parent / 'parent-deny').write_text('rejected parent change\n')
        plan = self.base / 'parent-plan.json'
        plan.write_text(json.dumps({'commit': [{
            'path': 'parent-deny', 'classification': 'source', 'owner': 'parent',
            'evidence': 'review/parent-source', 'safe_to_commit': True,
            'sha256': hashlib.sha256((self.parent / 'parent-deny').read_bytes()).hexdigest()}]}))
        with self.assertRaisesRegex(LifecycleError, 'configured validation failed'):
            service.finish(self.parent, task='parent', plan_path=str(plan), result_ref='result/parent2')
        rejected = git(self.parent, 'rev-parse', 'HEAD')
        self.assertNotEqual(rejected, accepted)
        with self.assertRaises(LifecycleError):
            self.finish()
        self.assertEqual(git(self.remote, 'rev-parse', 'refs/heads/topic/parent'), accepted)
        self.assertEqual(git(self.remote, 'rev-parse', 'refs/heads/trunk'), trunk)
        self.assertEqual(service.status(self.parent, 'parent')['acceptance']['commit'], accepted)
        self.assertEqual(git(self.parent, 'rev-parse', 'HEAD'), rejected)

    def assert_refused_untouched(self, refusal):
        head = git(self.topic, 'rev-parse', 'HEAD')
        with self.assertRaisesRegex(LifecycleError, refusal):
            self.finish()
        state = service.status(self.topic, 'candidate')
        self.assertEqual(git(self.topic, 'rev-parse', 'HEAD'), head)
        self.assertEqual(git(self.remote, 'for-each-ref', 'refs/heads/topic/candidate'), '')
        self.assertNotIn('acceptance', state)
        self.assertNotIn('intent', state)

    def test_preexisting_unowned_dirt_is_refused_before_validation(self):
        self.begin_candidate(dirty=True)
        (self.topic / '__pycache__').mkdir()
        (self.topic / '__pycache__' / 'm.pyc').write_bytes(b'')
        (self.topic / 'stray.txt').write_text('unowned\n')
        git(self.topic, 'config', 'core.excludesFile', str(self.base / 'ignore'))
        (self.base / 'ignore').write_text('__pycache__/\n')
        with patch.object(service, '_validate', side_effect=AssertionError('validated')):
            self.assert_refused_untouched(r'completion exception: __pycache__/m\.pyc, stray\.txt;')

    def test_preexisting_target_dirt_is_refused_before_validation(self):
        self.begin_candidate(dirty=True)
        (self.root / 'stray.txt').write_text('unowned\n')
        with patch.object(service, '_validate', side_effect=AssertionError('validated')):
            self.assert_refused_untouched('integration target has unrelated dirty or merge state: stray.txt')
        (self.root / 'stray.txt').unlink()
        self.remote_commit('side', 'side\n', branch='side')
        git(self.root, 'fetch', 'origin', 'side')
        git(self.root, 'merge', '--no-ff', '--no-commit', 'FETCH_HEAD')
        with patch.object(service, '_validate', side_effect=AssertionError('validated')):
            self.assert_refused_untouched('merge state: MERGE_HEAD, side')
        git(self.root, 'merge', '--abort')
        self.assertTrue(self.finish()['accepted'])

    def test_remote_advance_during_merge_validates_synchronized_result(self):
        self.begin_candidate()
        self.remote_commit('early', 'early\n')
        raced = []
        native = service._merge_by_fast_forward

        def advance(repo, theirs, *args):
            merged = native(repo, theirs, *args)
            if theirs == self.source and not raced:
                raced.append(self.remote_commit())
            return merged

        with patch.object(service, '_merge_by_fast_forward', side_effect=advance):
            result, runs = self.counted_finish()
        merged = result['integration']['commit']
        self.assertEqual(runs, [self.topic, self.root, self.root])
        self.assertIn(raced[0], git(self.root, 'rev-list', '--parents', '-1', merged).split())
        self.assertNotIn('reused', service.status(self.topic, 'candidate')['integrated']['validation'])

    def test_validator_rejecting_the_merge_commit_blocks_push(self):
        self.validation[:] = [sys.executable, '-c',
                              "import subprocess, sys; subject = subprocess.check_output("
                              "['git', 'log', '-1', '--format=%s'], text=True); "
                              "sys.exit(subject.startswith('Merge workspace task'))"]
        self.begin_candidate()
        base = self.remote_commit()
        for _ in range(2):
            with self.assertRaisesRegex(LifecycleError, 'configured validation failed'):
                self.finish()
            state = service.status(self.topic, 'candidate')
            self.assertEqual(git(self.remote, 'rev-parse', 'refs/heads/trunk'), base)
            self.assertNotIn('integrated', state)
            merged = state['intent']['merged']
            self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), merged)
            self.assertEqual(git(self.root, 'rev-list', '--parents', '-1', merged).split()[1:],
                             [base, self.source])
        second = self.base / 'second'
        service.begin(self.root, task='second', request='issue/13', remote='origin',
                      branch='topic/second', worktree=str(second),
                      validation=[sys.executable, '-c', 'pass'], preflight=self.preflight)
        self.configure(second)
        self.commit(second, 'second', 'second\n')
        with self.assertRaisesRegex(LifecycleError, 'pending integration'):
            service.finish(second, task='second', plan_path=str(self.plan), result_ref='result/13')
        self.assertEqual(git(self.remote, 'rev-parse', 'refs/heads/trunk'), base)
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), merged)

    def test_parent_finish_refuses_a_rejected_child_integration(self):
        self.validation[:] = [sys.executable, '-c',
                              "import os, sys; sys.exit(os.path.basename(os.getcwd()) == 'parent')"]
        self.begin_parent()
        accepted = git(self.remote, 'rev-parse', 'refs/heads/topic/parent')
        trunk = git(self.remote, 'rev-parse', 'refs/heads/trunk')
        with self.assertRaisesRegex(LifecycleError, 'configured validation failed'):
            self.finish()
        merged = service.status(self.topic, 'candidate')['intent']['merged']
        self.assertEqual(git(self.parent, 'rev-parse', 'HEAD'), merged)
        self.assertEqual(merged, self.source, 'the parent fast-forwarded and still validated its own checkout')
        with self.assertRaisesRegex(LifecycleError, 'pending integration of candidate'):
            service.finish(self.parent, task='parent', plan_path=str(self.plan), result_ref='result/parent2')
        self.assertEqual(git(self.remote, 'rev-parse', 'refs/heads/topic/parent'), accepted)
        self.assertEqual(git(self.remote, 'rev-parse', 'refs/heads/trunk'), trunk)
        self.assertEqual(service.status(self.parent, 'parent')['acceptance']['commit'], accepted)

    def test_final_validation_observes_the_pushed_merge_commit(self):
        observed = self.base / 'observed'
        self.validation[:] = [sys.executable, '-c',
                              "import subprocess, sys; open(sys.argv[1], 'a').write(subprocess.check_output("
                              "['git', 'rev-list', '--parents', '-1', 'HEAD'], text=True))", str(observed)]
        self.begin_candidate()
        base = self.remote_commit()
        result = self.finish()
        merged = result['integration']['commit']
        self.assertEqual(observed.read_text().splitlines()[-1].split(), [merged, base, self.source])
        self.assertEqual(git(self.remote, 'rev-parse', 'refs/heads/trunk'), merged)
        self.assertNotIn('reused', service.status(self.topic, 'candidate')['integrated']['validation'])

    def test_unmoved_target_fast_forwards_to_the_accepted_commit(self):
        self.begin_candidate()
        base = git(self.remote, 'rev-parse', 'refs/heads/trunk')
        with patch.object(service, '_push', wraps=service._push) as pushes:
            result, runs = self.counted_finish()
        self.assertEqual(result['integration']['commit'], self.source)
        self.assertEqual(git(self.remote, 'rev-parse', 'refs/heads/trunk'), self.source)
        self.assertEqual(git(self.remote, 'rev-list', '--merges', '--count', base + '..trunk'), '0')
        self.assertEqual(runs, [self.topic, self.root], 'the target checkout is a different validation input')
        self.assertEqual([Path(call.args[0]) for call in pushes.call_args_list], [self.topic, self.root])
        integrated = service.status(self.topic, 'candidate')['integrated']
        self.assertEqual((integrated['commit'], integrated['source'], integrated['destination']),
                         (self.source, self.source, 'trunk'))
        with patch.object(service, '_push', side_effect=AssertionError('pushed')):
            again, runs = self.counted_finish()
        self.assertEqual((again['integration']['commit'], runs), (self.source, []))

    def test_fast_forward_validation_reads_the_target_checkout(self):
        self.validation[:] = [sys.executable, '-c', "import os, sys; sys.exit(os.path.basename(os.getcwd()) == 'root')"]
        self.begin_candidate()
        base = git(self.remote, 'rev-parse', 'refs/heads/trunk')
        with self.assertRaisesRegex(LifecycleError, 'configured validation failed'):
            self.finish()
        state = service.status(self.topic, 'candidate')
        self.assertEqual((state['intent']['merged'], git(self.root, 'rev-parse', 'HEAD')), (self.source, self.source))
        self.assertEqual(git(self.remote, 'rev-parse', 'refs/heads/trunk'), base)
        self.assertNotIn('integrated', state)

    def test_fast_forward_refuses_hidden_target_changes(self):
        self.begin_candidate()
        base = git(self.remote, 'rev-parse', 'refs/heads/trunk')
        git(self.root, 'update-index', '--skip-worktree', 'base')
        (self.root / 'base').write_text('hidden local change\n')
        with self.assertRaisesRegex(LifecycleError, 'skip-worktree'):
            self.finish()
        self.assertEqual((git(self.root, 'rev-parse', 'HEAD'), git(self.remote, 'rev-parse', 'refs/heads/trunk')),
                         (base, base))
        self.assertEqual((self.root / 'base').read_text(), 'hidden local change\n')

    def test_interrupted_fast_forward_resumes_without_a_merge_commit(self):
        self.begin_candidate()
        native = service.git

        def interrupted(repo, *args, **kwargs):
            output = native(repo, *args, **kwargs)
            if '--ff-only' in args and Path(repo) == self.root:
                raise LifecycleError('interrupted after the fast-forward')
            return output

        with patch.object(service, 'git', side_effect=interrupted):
            with self.assertRaisesRegex(LifecycleError, 'interrupted after the fast-forward'):
                self.finish()
        state = service.status(self.topic, 'candidate')
        self.assertNotIn('merged', state['intent'])
        self.assertEqual(git(self.root, 'rev-parse', 'HEAD'), self.source)
        result, runs = self.counted_finish()
        self.assertEqual(result['integration']['commit'], self.source)
        self.assertEqual(git(self.remote, 'rev-parse', 'refs/heads/trunk'), self.source)
        self.assertEqual(runs, [self.root])

    def test_remote_advance_during_fast_forward_merges_without_force(self):
        self.begin_candidate()
        native = service.git
        raced = []

        def advance(repo, *args, **kwargs):
            output = native(repo, *args, **kwargs)
            if '--ff-only' in args and not raced:
                raced.append(self.remote_commit())
            return output

        with patch.object(service, 'git', side_effect=advance):
            result, runs = self.counted_finish()
        merged = result['integration']['commit']
        self.assertEqual(sorted(git(self.root, 'rev-list', '--parents', '-1', merged).split()[1:]),
                         sorted([self.source, raced[0]]))
        self.assertEqual(git(self.remote, 'rev-parse', 'refs/heads/trunk'), merged)
        self.assertEqual(runs, [self.topic, self.root, self.root])

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
        with self.assertRaisesRegex(LifecycleError, 'conflicts in base; start it there'):
            self.finish()
        before = service.status(self.topic, 'candidate')
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')
        subprocess.run(['git', '-C', str(self.root), 'merge', '--no-ff', '--no-commit', remote], capture_output=True)
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
        self.assertEqual(observations[1:], [(self.validation, merged, True), (self.parent_validation, merged, True)])
        self.assertNotIn('reused', service.status(self.parent, 'parent')['acceptance']['validation'])
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


    def keep_cache(self, rules='cache/\n*.log\n'):
        """Ignored data in the default checkout that belongs to no task."""
        (self.root / '.git' / 'info' / 'exclude').write_text(rules)
        for name, data in (('cache/shared.txt', b'shared\n'), ('cache/deep/state.bin', b'\0state'),
                           ('build.log', b'log\n')):
            (self.root / name).parent.mkdir(parents=True, exist_ok=True)
            (self.root / name).write_bytes(data)
        return self.cache_state()

    def cache_state(self):
        """Bytes and mode of each file in the root that Git does not track."""
        names = git(self.root, 'ls-files', '-z', '--others', '--ignored', '--exclude-standard').split('\0')
        return {name: ((self.root / name).read_bytes(), (self.root / name).stat().st_mode)
                for name in sorted(filter(None, names))}

    def force_commit(self, repo, path, content):
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(content)
        git(repo, 'add', '--force', '--', path)
        git(repo, 'commit', '-m', path)

    def tracked_tools(self):
        (self.root / 'tools').mkdir()
        self.commit(self.root, 'tools/run.py', 'run\n')
        git(self.root, 'push', 'origin', 'trunk')
        self.keep_cache()
        (self.root / 'tools' / 'run.log').write_text('kept\n')
        return self.cache_state()

    def test_ignored_target_data_stays_through_fast_forward(self):
        cache = self.keep_cache()
        self.begin_candidate()
        result = self.finish()
        self.assertTrue(result['accepted'])
        self.assertEqual(result['integration']['commit'], self.source, 'fast-forward keeps the accepted SHA')
        self.assertEqual(git(self.remote, 'rev-parse', 'refs/heads/trunk'), self.source)
        self.assertEqual(self.cache_state(), cache)
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')
        self.assertEqual(len(git(self.root, 'worktree', 'list', '--porcelain').split('\n\n')), 2)

    def test_ignored_target_data_stays_through_synchronization_and_merge(self):
        cache = self.keep_cache()
        self.begin_candidate()
        remote = self.remote_commit()
        result = self.finish()
        merged = result['integration']['commit']
        self.assertEqual(git(self.root, 'rev-list', '--parents', '-1', merged).split()[1:], [remote, self.source])
        self.assertEqual(git(self.remote, 'rev-parse', 'refs/heads/trunk'), merged)
        self.assertEqual(self.cache_state(), cache)

    def assert_collision(self, refusal, *, merge=False):
        """Refused before the target takes the incoming change; its ignored data unchanged."""
        if merge:
            self.remote_commit('side', 'side\n')
        before = self.cache_state()
        with self.assertRaisesRegex(LifecycleError, refusal):
            self.finish()
        self.assertEqual(self.cache_state(), before)
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')
        self.assertFalse((self.root / 'candidate').exists())
        self.assertNotIn('integrated', service.status(self.topic, 'candidate'))

    def collides_at_incoming_file(self, merge):
        self.keep_cache()
        self.begin_candidate()
        self.force_commit(self.topic, 'build.log', 'incoming\n')
        self.assert_collision(r'build\.log \(ignored data at incoming path\)', merge=merge)
        # The refusal keeps the pending integration; moving the data aside resumes it.
        (self.root / 'build.log').rename(self.base / 'build.log')
        self.assertTrue(self.finish()['accepted'])
        self.assertEqual((self.root / 'build.log').read_text(), 'incoming\n')
        self.assertEqual((self.root / 'cache' / 'deep' / 'state.bin').read_bytes(), b'\0state')

    def test_fast_forward_over_an_ignored_file_is_refused(self):
        self.collides_at_incoming_file(merge=False)

    def test_merge_over_an_ignored_file_is_refused(self):
        # Git's merge overwrites ignored files even with --no-overwrite-ignore.
        self.collides_at_incoming_file(merge=True)

    def test_incoming_file_inside_an_ignored_directory_is_refused(self):
        self.keep_cache()
        self.begin_candidate()
        self.force_commit(self.topic, 'cache/new.txt', 'incoming\n')
        self.assert_collision(r'cache \(ignored data on the path of cache/new\.txt\)', merge=True)

    def test_incoming_file_over_an_ignored_directory_is_refused(self):
        self.keep_cache()
        self.begin_candidate()
        self.force_commit(self.topic, 'cache', 'incoming\n')
        self.assert_collision(r'cache \(ignored data at incoming path\)')

    def test_incoming_directory_over_an_ignored_file_is_refused(self):
        self.keep_cache()
        self.begin_candidate()
        self.force_commit(self.topic, 'build.log/x', 'incoming\n')
        self.assert_collision(r'build\.log \(ignored data on the path of build\.log/x\)', merge=True)

    def test_tracked_directory_replaced_by_a_file_keeps_its_ignored_content(self):
        self.tracked_tools()
        self.begin_candidate()
        git(self.topic, 'rm', '-q', 'tools/run.py')
        self.commit(self.topic, 'tools', 'incoming\n')
        self.assert_collision(r'tools \(ignored data in a directory replaced by tools\)')
        self.assertEqual((self.root / 'tools' / 'run.log').read_text(), 'kept\n')

    def test_deleting_a_tracked_directory_keeps_its_ignored_content(self):
        cache = self.tracked_tools()
        self.begin_candidate()
        git(self.topic, 'rm', '-q', 'tools/run.py')
        git(self.topic, 'commit', '-m', 'drop tools')
        self.assertTrue(self.finish()['accepted'])
        self.assertEqual(self.cache_state(), cache)

    def test_link_on_an_incoming_path_is_refused(self):
        self.keep_cache('cache/\n*.log\nlinked\n')
        outside = self.base / 'outside'
        outside.mkdir()
        (outside / 'kept.txt').write_text('kept\n')
        link = self.root / 'linked'
        if os.name == 'nt':
            import _winapi
            _winapi.CreateJunction(str(outside), str(link))
        else:
            link.symlink_to(outside, target_is_directory=True)
        self.begin_candidate()
        self.force_commit(self.topic, 'linked/new.txt', 'incoming\n')
        with self.assertRaisesRegex(LifecycleError, r'linked \(link or reparse point on the path of linked/new\.txt\)'):
            self.finish()
        self.assertEqual([path.name for path in outside.iterdir()], ['kept.txt'])
        self.assertNotIn('integrated', service.status(self.topic, 'candidate'))

    def test_case_alias_of_ignored_data_is_refused_where_the_filesystem_folds_case(self):
        cache = self.keep_cache()
        self.begin_candidate()
        self.force_commit(self.topic, 'Cache/new.txt', 'incoming\n')
        if (self.root / 'CACHE').exists():
            self.assert_collision(r'Cache \(ignored data on the path of Cache/new\.txt\)')
        else:
            self.assertTrue(self.finish()['accepted'])
            self.assertEqual({name: value for name, value in self.cache_state().items() if name in cache}, cache)

    def test_ignored_data_does_not_excuse_other_target_state(self):
        self.keep_cache()
        self.begin_candidate(dirty=True)
        cases = (
            ('base', lambda: (self.root / 'base').write_text('changed\n'),
             lambda: git(self.root, 'checkout', '--', 'base')),
            ('staged.txt', lambda: ((self.root / 'staged.txt').write_text('s\n'), git(self.root, 'add', 'staged.txt')),
             lambda: (git(self.root, 'rm', '-q', '--cached', 'staged.txt'), (self.root / 'staged.txt').unlink())),
            ('stray.txt', lambda: (self.root / 'stray.txt').write_text('unowned\n'),
             lambda: (self.root / 'stray.txt').unlink()))
        for name, damage, repair in cases:
            with self.subTest(name=name):
                damage()
                with patch.object(service, '_validate', side_effect=AssertionError('validated')):
                    self.assert_refused_untouched('integration target has unrelated dirty or merge state: ' + name)
                repair()
        self.assertTrue(self.finish()['accepted'])

    def test_returning_to_an_earlier_head_over_ignored_data_is_refused(self):
        # A withdrawal resets the target to its HEAD before the preparation.
        self.keep_cache()
        self.force_commit(self.root, 'old.log', 'tracked\n')
        before = git(self.root, 'rev-parse', 'HEAD')
        git(self.root, 'rm', '-q', 'old.log')
        git(self.root, 'commit', '-m', 'drop old.log')
        (self.root / 'old.log').write_text('ignored since\n')
        self.assertEqual(service._ignored_collisions(self.root, before, merge=False),
                         [('old.log', 'ignored data at incoming path')])
        self.assertEqual(service._ignored_collisions(self.root, before), [], 'a merge of an ancestor brings nothing')

    def test_ignored_data_written_after_the_check_survives_a_fast_forward(self):
        self.keep_cache()
        self.begin_candidate()
        self.force_commit(self.topic, 'late.log', 'incoming\n')
        native = service._ignored_collisions

        def late(repo, incoming, **kwargs):
            found = native(repo, incoming, **kwargs)
            (Path(repo) / 'late.log').write_text('written later\n')
            return found

        with patch.object(service, '_ignored_collisions', side_effect=late):
            with self.assertRaisesRegex(LifecycleError, 'would be overwritten'):
                self.finish()
        self.assertEqual((self.root / 'late.log').read_text(), 'written later\n')

    def race(self, shape, merges):
        """Another process writes ignored data where a non-fast-forward brings
        ``shape``'s path, after every check and just before Git changes the
        checkout. ``merges`` names what Git brings it with: the candidate's
        merge, or the synchronization of a diverged default checkout."""
        incoming, names = late_data.SHAPES[shape]
        (self.root / '.git' / 'info' / 'exclude').write_text(late_data.IGNORE)
        self.begin_candidate()
        if merges == 'candidate':
            self.force_commit(self.topic, incoming, 'incoming\n')
            self.remote_commit('side', 'side\n')
        else:
            self.commit(self.root, 'local', 'local\n')
            self.force_commit(self.other, incoming, 'incoming\n')
            git(self.other, 'push', 'origin', 'HEAD:refs/heads/trunk')
        remote = git(self.remote, 'rev-parse', 'refs/heads/trunk')
        writing, fired = late_data.writing_before_checkout_change(self.root, incoming, names)
        with writing:
            if shape == 'case' and not late_data.folds_case():
                self.assertTrue(self.finish()['accepted'], 'distinct names do not collide')
                self.assertEqual(late_data.state(self.root, names)[names[0]][0], hashlib.sha256(
                    b'written by another process: case.log\n').hexdigest())
                return
            late = None
            with self.assertRaisesRegex(LifecycleError, late_data.REFUSAL):
                try:
                    self.finish()
                finally:
                    late = late_data.state(self.root, names)
        self.assertEqual(fired[0][:3], ('merge', '--ff-only', '--no-overwrite-ignore'),
                         'the other process wrote just before the only Git command that changed the checkout')
        self.assertNotIn(None, late.values())
        self.assertEqual(late_data.state(self.root, names), late)
        self.assertEqual(git(self.remote, 'rev-parse', 'refs/heads/trunk'), remote)
        self.assertEqual(git(self.root, 'status', '--porcelain'), '')
        self.assertNotIn('integrated', service.status(self.topic, 'candidate'))
        aside = self.base / 'aside'; aside.mkdir()
        late_data.move_aside(self.root, names, aside)
        result = self.finish()
        self.assertTrue(result['accepted'], 'the same finish resumes once the data is moved aside')
        self.assertEqual((self.root / incoming).read_text(), 'incoming\n')
        self.assertEqual(late_data.state(aside, names), late)
        merge = result['integration']['commit']
        self.assertEqual(git(self.root, 'rev-list', '--parents', '-1', merge).split()[2:],
                         [service.status(self.topic, 'candidate')['acceptance']['commit']])
        self.assertEqual(git(self.root, 'log', '-1', '--format=%B', merge), 'Merge workspace task candidate')

    for shape in late_data.SHAPES:
        for merges in ('candidate', 'synchronization'):
            locals()[f'test_ignored_{shape}_written_before_the_{merges}_merge_survives'] = (
                lambda self, shape=shape, merges=merges: self.race(shape, merges))
    del shape, merges

    def test_a_merge_is_signed_as_git_commit_would_sign_it(self):
        self.begin_candidate()
        self.remote_commit()
        remote = git(self.remote, 'rev-parse', 'refs/heads/trunk')
        git(self.root, 'config', 'commit.gpgSign', 'true')
        git(self.root, 'config', 'gpg.program', str(self.base / 'missing-signer'))
        with self.assertRaisesRegex(LifecycleError, 'failed to sign'):
            self.finish()
        self.assertEqual(git(self.remote, 'rev-parse', 'refs/heads/trunk'), remote)
        git(self.root, 'config', 'commit.gpgSign', 'false')
        self.assertTrue(self.finish()['accepted'])

    def test_a_merge_hook_leaves_the_merge_to_its_operator(self):
        self.begin_candidate()
        self.remote_commit()
        hook = Path(git(self.root, 'rev-parse', '--path-format=absolute', '--git-path', 'hooks/commit-msg'))
        hook.parent.mkdir(exist_ok=True)
        hook.write_text('#!/bin/sh\necho "Hooked: yes" >> "$1"\n')
        hook.chmod(0o755)
        with self.assertRaisesRegex(LifecycleError, 'merge hooks commit-msg; start it there'):
            self.finish()
        git(self.root, 'merge', '--no-ff', '--no-commit', self.source)
        merged = self.finish()['integration']['commit']
        self.assertIn('Hooked: yes', git(self.root, 'log', '-1', '--format=%B', merged))


if __name__ == '__main__':
    unittest.main()
