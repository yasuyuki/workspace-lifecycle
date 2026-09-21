"""Explicit adoption of Git-owned work; never an importer of legacy receipts."""
from contextlib import ExitStack
import hashlib
import json
import os
import re
from pathlib import Path
import stat

from .errors import LifecycleError
from .git import common_dir, current_branch, git, head, remote_default, top, worktree_records
from .service import _index_protection, _lease, _no_links, _now, _snapshot
from .state import locked_state, save_state


def _identity(path):
    info = path.lstat()
    return [info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)]


def _operation_check(target):
    _index_protection(target)
    for name in ('MERGE_HEAD', 'REBASE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD',
                 'rebase-merge', 'rebase-apply', 'sequencer', 'BISECT_START', 'BISECT_LOG', 'index.lock'):
        path = Path(git(target, 'rev-parse', '--path-format=absolute', '--git-path', name))
        if path.exists() or path.is_symlink():
            raise LifecycleError('existing Git operation must be resolved before adoption: ' + name)


def _repository_boundary(path):
    """A nested repository remains owned by its own Git/lifecycle contract."""
    _no_links(path)
    marker = path / '.git'
    _no_links(marker)
    if top(path) != path:
        raise LifecycleError('directory baseline must be a distinct Git checkout')
    admin = Path(git(path, 'rev-parse', '--absolute-git-dir'))
    _no_links(admin)
    return {'worktree': _identity(path), 'marker': _identity(marker),
            'git_file': marker.read_text() if marker.is_file() else None,
            'git_dir': str(admin), 'git_dir_identity': _identity(admin),
            'common_dir': str(common_dir(path)), 'head': head(path)}


def _directories(target):
    """Preserve directory identities without taking ownership of nested repos."""
    result = {}
    def walk_error(error):
        raise error
    for current, dirs, _ in os.walk(target, followlinks=False, onerror=walk_error):
        for name in list(dirs):
            path = Path(current) / name
            if path == target / '.git':
                dirs.remove(name)  # Our own state/index/leases are Git metadata.
                continue
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                result[path.relative_to(target).as_posix()] = {'link': os.readlink(path)}
                continue
            if (getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)
                    or os.path.ismount(path)):
                raise LifecycleError('adoption refuses mounted or reparse directory content')
            marker = path / '.git'
            if marker.exists() or marker.is_symlink():
                result[path.relative_to(target).as_posix()] = _repository_boundary(path)
                dirs.remove(name)
            else:
                result[path.relative_to(target).as_posix()] = _identity(path)
    return result


def _capture(repo, target, branch, expected_head):
    _no_links(target)
    if os.path.ismount(target):
        raise LifecycleError('adoption refuses a mounted worktree')
    records = worktree_records(repo)
    primary = Path(records[0]['worktree']).resolve()
    # A filesystem may host both repositories (for example a mounted /work).
    # Below their shared anchor, a mount would redirect the requested checkout.
    try:
        anchor = Path(os.path.commonpath([primary, target]))
    except ValueError:
        # Native linked worktrees may live on another Windows volume.
        anchor = Path(target.anchor)
    for component in (target, *target.parents):
        if component == anchor:
            break
        if os.path.ismount(component):
            raise LifecycleError('adoption refuses mounted worktree path traversal')
    matches = [r for r in records if Path(r['worktree']).resolve() == target]
    if (len(matches) != 1
            or len([r for r in records if r.get('branch') == 'refs/heads/' + branch]) != 1
            or matches[0].get('branch') != 'refs/heads/' + branch
            or 'prunable' in matches[0]):
        raise LifecycleError('adoption requires exactly one existing worktree on the explicit branch')
    if (top(target) != target or common_dir(target) != common_dir(repo)
            or current_branch(target) != branch
            or head(target) != expected_head
            or head(repo, 'refs/heads/' + branch) != expected_head):
        raise LifecycleError('existing branch/worktree/expected-head identity mismatch')
    marker = target / '.git'
    _no_links(marker)
    if not (marker.is_dir() if target == primary else marker.is_file()):
        raise LifecycleError('adoption requires the native primary directory or linked Git file')
    admin = Path(git(target, 'rev-parse', '--absolute-git-dir'))
    _no_links(admin)
    _operation_check(target)
    dirty = _snapshot(target, optional_locks=False)
    directories = _directories(target)
    boundaries = {name: value for name, value in directories.items()
                  if isinstance(value, dict) and 'git_dir' in value}
    content = {}
    for name in sorted(dirty):
        boundary = next((root for root in boundaries
                         if name.rstrip('/') == root or name.startswith(root + '/')), None)
        if boundary is not None:
            content[name] = {'repository_boundary': boundary, 'identity': boundaries[boundary]}
            continue
        path = target / name
        _no_links(path.parent, target)
        for part in (path, *path.parents):
            if part == target:
                break
            if os.path.ismount(part):
                raise LifecycleError('adoption refuses mounted dirty content')
        try:
            before = path.lstat()
        except FileNotFoundError:
            content[name] = None
            continue
        if stat.S_ISLNK(before.st_mode):
            content[name] = {'link': os.readlink(path), 'mode': before.st_mode,
                             'identity': [before.st_dev, before.st_ino],
                             'mtime_ns': before.st_mtime_ns, 'ctime_ns': before.st_ctime_ns}
            continue
        if (not stat.S_ISREG(before.st_mode)
                or getattr(before, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)):
            raise LifecycleError('adoption requires regular dirty files: ' + name)
        digest = hashlib.sha256()
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(descriptor, 'rb') as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise LifecycleError('dirty content identity changed before reading')
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        after = path.lstat()
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise LifecycleError('dirty content changed while capturing adoption')
        content[name] = {'sha256': digest.hexdigest(), 'mode': before.st_mode,
                         'identity': [before.st_dev, before.st_ino],
                         'mtime_ns': before.st_mtime_ns, 'ctime_ns': before.st_ctime_ns}
    index_path = Path(git(target, 'rev-parse', '--path-format=absolute', '--git-path', 'index'))
    _no_links(index_path)
    index = index_path.read_bytes()
    snapshot = {'dirty': dirty, 'content': content,
                'directories': directories,
                'index_sha256': hashlib.sha256(index).hexdigest(),
                'index_identity': _identity(index_path),
                'worktree': _identity(target), 'git_dir': str(admin),
                'git_dir_identity': _identity(admin),
                'git_file': marker.read_text() if marker.is_file() else None,
                'git_file_identity': _identity(marker),
                'parents': [[str(p), _identity(p)] for p in target.parents],
                'record': matches[0],
                'head': expected_head, 'branch': branch}
    snapshot['digest'] = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
    return snapshot


def _bindings(repo, task, branch, retry):
    records = (git(repo, 'config', '--local', '--get-regexp',
                   r'^branch\..*\.workspaceTask$', optional=True) or '').splitlines()
    matches = []
    key = 'branch.' + branch + '.workspacetask'
    for row in records:
        name, _, value = row.partition(' ')
        if name == key or value == task:
            matches.append((name, value))
    if matches and (not retry or matches != [(key, task)]):
        raise LifecycleError('existing branch or task binding is already owned')


def adopt_existing(repo, *, task, request, remote, branch, worktree, expected_head,
                   evidence, validation, preflight, parent=None, dependencies=None,
                   hold_reason=None, next_action=None):
    repo = Path(repo).resolve()
    dependencies = dependencies or []
    if any(not isinstance(v, str) or not v.strip() or '\0' in v or '\n' in v
           for v in (task, request, remote, branch, worktree, expected_head, evidence)):
        raise LifecycleError('adoption requires explicit nonempty identity, request and evidence')
    if bool(hold_reason) != bool(next_action):
        raise LifecycleError('adoption hold requires both reason and next action')
    for argv in (validation, preflight):
        if not isinstance(argv, list) or not argv or not all(isinstance(v, str) and v for v in argv):
            raise LifecycleError('validation and preflight require nonempty argv arrays')
    if '{repo}' not in preflight:
        raise LifecycleError('preflight must explicitly select {repo}')
    target = Path(worktree)
    if not target.is_absolute():
        raise LifecycleError('existing worktree must be an explicit absolute path')
    _no_links(target)
    target = target.resolve()
    git(repo, 'check-ref-format', '--branch', branch)
    oid_length = 64 if git(repo, 'rev-parse', '--show-object-format') == 'sha256' else 40
    if not re.fullmatch('[0-9a-f]{' + str(oid_length) + '}', expected_head):
        raise LifecycleError('expected-head must be a full exact commit OID')
    desired = dict(task=task, request=request, remote=remote, branch=branch,
                   worktree=str(target), expected_head=expected_head, evidence=evidence,
                   parent=parent, dependencies=dependencies, validation=validation,
                   preflight=preflight, hold_reason=hold_reason, next_action=next_action)
    with ExitStack() as stack:
        for name in sorted(set([task] + dependencies + ([parent] if parent else []))):
            stack.enter_context(_lease(repo, name))
        with locked_state(repo, create=True) as (directory, state):
            if task in state['tasks'] or task in state.get('retired', {}):
                raise LifecycleError('task already exists; resume its existing checkout')
            intent = state['intents'].get(task)
            if intent and (intent.get('kind') != 'adopt-existing' or intent['desired'] != desired):
                raise LifecycleError('pending adoption must resume its exact requested identity')
            default = remote_default(repo, remote)
            upstream = git(repo, 'for-each-ref', '--format=%(upstream:remotename) %(upstream:remoteref)',
                           'refs/heads/' + branch)
            if branch == default or upstream == remote + ' refs/heads/' + default:
                raise LifecycleError('default branch is an integration destination, not an adoption task')
            if task in dependencies or task == parent or set(dependencies) - set(state['tasks']):
                raise LifecycleError('dependencies must refer to existing other tasks')
            integration = {'kind': 'remote-default', 'remote': remote}
            if parent:
                p = state['tasks'].get(parent)
                if not p or p.get('hold') or p.get('retire') or p['remote'] != remote:
                    raise LifecycleError('parent must be an existing unheld task with the same remote')
                integration = {'kind': 'task', 'task': parent}
            if intent and intent['integration'] != integration:
                raise LifecycleError('pending adoption integration contract changed')
            for other, pending in state['intents'].items():
                d = pending.get('desired', {})
                if other != task and (d.get('branch') == branch or d.get('worktree') == str(target)):
                    raise LifecycleError('another pending task owns this branch or worktree')
            _bindings(repo, task, branch, bool(intent))
            snapshot = _capture(repo, target, branch, expected_head)
            if not intent:
                intent = {'kind': 'adopt-existing', 'desired': desired, 'integration': integration,
                          'snapshot': snapshot, 'at': _now()}
                state['intents'][task] = intent
                save_state(directory, state)
            if snapshot != intent['snapshot'] or _capture(repo, target, branch, expected_head) != snapshot:
                raise LifecycleError('pending adoption snapshot changed; preserve the original intent')
            _bindings(repo, task, branch, True)
            git(repo, 'config', '--local', 'branch.' + branch + '.workspaceTask', task)
            if _capture(repo, target, branch, expected_head) != intent['snapshot']:
                raise LifecycleError('adoption changed after binding; preserve the original intent')
            _bindings(repo, task, branch, True)
            item = {'request': request, 'remote': remote, 'integration': integration,
                    'dependencies': dependencies, 'validation': validation, 'preflight': preflight,
                    'baseline_dirty': snapshot['dirty'], 'created_at': _now(),
                    'adoption': {'evidence': evidence, 'expected_head': expected_head,
                                 'snapshot_digest': snapshot['digest'],
                                 'protected_paths': sorted(name for name, value in snapshot['directories'].items()
                                                           if isinstance(value, dict) and 'git_dir' in value)}}
            if hold_reason:
                item['hold'] = {'reason': hold_reason, 'next_action': next_action, 'at': _now()}
            state['tasks'][task] = item
            state['intents'].pop(task)
            save_state(directory, state)
    return {'task': task, 'branch': branch, 'worktree': str(target),
            'head': expected_head, 'accepted': False}
