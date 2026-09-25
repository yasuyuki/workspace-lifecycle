from __future__ import annotations

from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import uuid

from .errors import LifecycleError
from .git import bound_task, branch_for_task, current_branch, git, head, remote_default, task_worktree, top, worktree_records
from .state import locked_state, save_state


def _now(): return datetime.now(timezone.utc).isoformat()
def _sha(path: Path): return hashlib.sha256(path.read_bytes()).hexdigest()


@contextmanager
def _lease(repo, task, allow_use=False):
    # This is intentionally mandatory: a missing lease module means a partial
    # installation must not mutate a workspace.
    try:
        from .leases import guard
    except ImportError as exc:
        raise LifecycleError("workspace lifecycle lease support is unavailable") from exc
    try:
        with guard(repo, task, allow_use=allow_use):
            yield
    except (ValueError, OSError) as exc:
        raise LifecycleError(str(exc)) from exc


def _snapshot(repo: Path, *, optional_locks=True) -> dict[str, str]:
    environment = None if optional_locks else {**os.environ, 'GIT_OPTIONAL_LOCKS': '0'}
    process = subprocess.run(['git', '-C', str(repo), 'status', '--porcelain=v1', '-z',
                              '--untracked-files=all', '--ignored=no', '--no-renames'],
                             capture_output=True, check=True, env=environment)
    result = {os.fsdecode(row[3:]): os.fsdecode(row[:2]) for row in process.stdout.split(b'\0') if row}
    ignored = subprocess.run(['git', '-C', str(repo), 'ls-files', '--others', '--ignored',
                              '--exclude-standard', '-z'], capture_output=True, check=True)
    result.update({os.fsdecode(row): '!!' for row in ignored.stdout.split(b'\0') if row})
    return result


def _task(state, task):
    value = state['tasks'].get(task)
    if not value:
        raise LifecycleError('unknown task: ' + task)
    return value


def _no_links(path, stop=None):
    # Windows canonicalization expands 8.3 paths and normalizes casing. That is
    # not a link. Check the actual filesystem attributes on every existing part.
    for component in (path, *path.parents):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400):
            raise LifecycleError('workspace path cannot traverse a link or reparse point')
        if component == stop:
            break


def begin(repo, *, task: str, request: str, remote: str, branch: str, worktree: str,
          parent: str | None = None, dependencies: list[str] | None = None,
          validation: list[str] | None = None, preflight: list[str] | None = None) -> dict:
    repo = Path(repo).resolve(); dependencies = dependencies or []
    if not task or not request or not branch or not worktree or '\n' in task or '\0' in task:
        raise LifecycleError('task, request, branch, and worktree are required')
    for value in (validation, preflight):
        if not isinstance(value, list) or not value or not all(isinstance(x, str) and x for x in value):
            raise LifecycleError('validation and preflight require nonempty argv arrays')
    if '{repo}' not in preflight:
        raise LifecycleError('preflight must explicitly select {repo}')
    git(repo, 'check-ref-format', '--branch', branch)
    target = Path(worktree).absolute()
    _no_links(target)
    target = target.resolve()
    if target == top(repo) or top(repo) in target.parents:
        raise LifecycleError('new worktree must be outside the integration checkout')
    desired = {'branch': branch, 'worktree': str(target), 'request': request, 'remote': remote,
               'parent': parent, 'dependencies': dependencies, 'validation': validation, 'preflight': preflight}
    with ExitStack() as stack:
        stack.enter_context(_lease(repo, task))
        if parent:
            stack.enter_context(_lease(repo, parent))
        with locked_state(repo, create=True) as (directory, state):
            if task in state['tasks'] or task in state.get('retired', {}):
                raise LifecycleError('task already exists; resume its existing checkout')
            intent = state['intents'].get(task)
            if intent and (intent.get('kind') != 'begin' or intent.get('desired') != desired):
                raise LifecycleError('pending begin must resume its exact requested identity')
            if not intent:
                if target.exists() or git(repo, 'show-ref', '--verify', '--quiet', 'refs/heads/' + branch, optional=True) is not None:
                    raise LifecycleError('new lifecycle worktree and branch must not already exist')
                default = remote_default(repo, remote)
                if branch == default:
                    raise LifecycleError('default branch is an integration destination, not a new task')
                if set(dependencies) - set(state['tasks']) or task in dependencies:
                    raise LifecycleError('dependencies must refer to existing other tasks')
                if parent:
                    parent_task = _task(state, parent)
                    if parent_task.get('hold') or parent_task.get('retire'):
                        raise LifecycleError('parent is held or retiring')
                    if parent_task['remote'] != remote:
                        raise LifecycleError('parent and child must use the same remote contract')
                    base = head(repo, 'refs/heads/' + branch_for_task(repo, parent))
                    integration = {'kind': 'task', 'task': parent}
                else:
                    git(repo, 'fetch', remote, 'refs/heads/' + default)
                    base = head(repo, 'FETCH_HEAD')
                    integration = {'kind': 'remote-default', 'remote': remote}
                # This is a transient operation intent, not a live Git-state mirror.
                intent = {'kind': 'begin', 'desired': desired, 'base': base,
                          'integration': integration, 'at': _now()}
                state['intents'][task] = intent; save_state(directory, state)
            base = intent['base']
            records = [r for r in worktree_records(repo) if Path(r['worktree']).resolve() == target]
            existing = git(repo, 'show-ref', '--verify', '--hash', 'refs/heads/' + branch, optional=True)
            if records:
                if (len(records) != 1 or records[0].get('branch') != 'refs/heads/' + branch
                        or head(target) != base or _snapshot(target)):
                    raise LifecycleError('interrupted begin checkout changed; preserve it for review')
            elif existing:
                if existing != base or target.exists():
                    raise LifecycleError('interrupted begin branch or destination changed')
                git(repo, 'worktree', 'add', str(target), branch)
            else:
                if target.exists():
                    raise LifecycleError('interrupted begin destination appeared without Git identity')
                git(repo, 'worktree', 'add', '-b', branch, str(target), base)
            key = 'branch.' + branch + '.workspaceTask'
            binding = git(repo, 'config', '--local', '--get', key, optional=True)
            if binding and binding != task:
                raise LifecycleError('branch binding belongs to another task')
            git(repo, 'config', '--local', key, task)
            state['tasks'][task] = {'request': request, 'remote': remote,
                'integration': intent['integration'], 'dependencies': dependencies,
                'validation': validation, 'preflight': preflight,
                'baseline_dirty': _snapshot(target), 'created_at': _now()}
            state['intents'].pop(task); save_state(directory, state)
    return {'task': task, 'branch': branch, 'base': base, 'worktree': str(target)}


def status(repo, task: str | None = None) -> dict:
    repo = Path(repo).resolve()
    with locked_state(repo) as (_, state):
        if task is None:
            try: task = bound_task(repo)
            except LifecycleError:
                return {"tasks": [{"task": name, "completion": "exception" if item.get("exception") else ("accepted" if item.get("acceptance") else "active"), "retire_pending": bool(item.get("retire"))} for name, item in state["tasks"].items()]}
        data = _task(state, task).copy()
        branch = branch_for_task(repo, task)
        try:
            live = task_worktree(repo, branch)
            data["live"] = {"branch": branch, "head": head(live), "worktree": str(live), "dirty": _snapshot(live)}
        except LifecycleError as exc: data["live_error"] = str(exc)
        data["task"] = task
        data["completion"] = "exception" if data.get("exception") else ("accepted" if data.get("acceptance") else "active")
        if task in state["intents"]: data["intent"] = state["intents"][task]
        return data


def update_preflight(repo, *, task: str, expected_preflight: list[str],
                     preflight: list[str], evidence: str) -> dict:
    """Change one current task's push policy command under an exact argv CAS."""
    for name, argv in (("expected preflight", expected_preflight), ("preflight", preflight)):
        if not isinstance(argv, list) or not argv or not all(isinstance(x, str) and x for x in argv):
            raise LifecycleError(name + ' requires a nonempty string argv array')
    if '{repo}' not in preflight:
        raise LifecycleError('preflight must explicitly select {repo}')
    if not isinstance(evidence, str) or not evidence.strip():
        raise LifecycleError('preflight update requires durable evidence')
    with _lease(repo, task):
        with locked_state(repo) as (directory, state):
            item = _task(state, task)
            if task in state.get('intents', {}) or item.get('retire'):
                raise LifecycleError('pending task operation prevents preflight update')
            if item.get('preflight') != expected_preflight:
                raise LifecycleError('preflight CAS mismatch')
            item['preflight'] = list(preflight)
            item['preflight_update'] = {'from': list(expected_preflight), 'to': list(preflight),
                                        'evidence': evidence, 'at': _now()}
            save_state(directory, state)
    return {'task': task, 'preflight': list(preflight)}


def hold(repo, task: str, reason: str, next_action: str) -> dict:
    if not reason or not next_action: raise LifecycleError("hold requires reason and next action")
    with _lease(repo, task, allow_use=True):
        with locked_state(repo) as (directory, state):
            _task(state, task)["hold"] = {"reason": reason, "next_action": next_action, "at": _now()}
            save_state(directory, state)
    return {"task": task, "held": True}


def release_hold(repo, task, evidence):
    if not isinstance(evidence, str) or not evidence.strip():
        raise LifecycleError('explicit hold resolution evidence is required')
    with _lease(repo, task, allow_use=True):
        with locked_state(repo) as (directory, state):
            item = _task(state, task)
            if not item.get('hold'):
                raise LifecycleError('task has no hold to resolve')
            item.pop('hold')
            item['hold_resolution'] = evidence
            save_state(directory, state)
    return {'task': task, 'held': False}


def _validate(repo: Path, argv: list[str]) -> dict:
    if not argv: return {"argv": [], "returncode": 0}
    result = subprocess.run(argv, cwd=repo, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    evidence = {"argv": argv, "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
    if result.returncode: raise LifecycleError("configured validation failed: " + json.dumps(evidence, ensure_ascii=True))
    return evidence


def _push(repo: Path, preflight: list[str], expected_branch: str, expected_remote=None) -> dict:
    argv = [str(repo) if value == "{repo}" else value for value in preflight]
    if "{repo}" not in preflight: raise LifecycleError("preflight argv must contain literal {repo}")
    result = subprocess.run(argv, cwd=repo, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode: raise LifecycleError("push preflight failed")
    try: decision = json.loads(result.stdout)
    except json.JSONDecodeError as exc: raise LifecycleError("push preflight did not emit JSON") from exc
    if decision.get("decision") != "push": raise LifecycleError("push blocked: " + str(decision.get("reason", "unknown")))
    push_argv = decision.get("push_argv")
    if not isinstance(push_argv, list) or len(push_argv) != 4 or push_argv[:2] != ["git", "push"] or not isinstance(push_argv[2], str) or push_argv[3] != "HEAD:refs/heads/" + expected_branch:
        raise LifecycleError("preflight push argv is not an exact normal branch push")
    if push_argv[2].startswith('-') or (expected_remote is not None and push_argv[2] != expected_remote):
        raise LifecycleError('preflight selected a different remote than the registered integration contract')
    subprocess.run(push_argv, cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
    remote_tip = git(repo, "ls-remote", push_argv[2], "refs/heads/" + expected_branch) or ""
    if not remote_tip.startswith(head(repo) + "\t"): raise LifecycleError("push did not verify remote branch tip")
    return {"decision": decision["decision"], "reason": decision.get("reason"), "argv": push_argv}


def _plan(path: Path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LifecycleError("invalid finish plan") from exc
    if not isinstance(value, dict) or set(value) - {"commit", "restore", "archive", "exception"}:
        raise LifecycleError("finish plan has unknown fields")
    for key in ("commit", "restore", "archive"):
        if key in value and not isinstance(value[key], list):
            raise LifecycleError("plan " + key + " must be a list")
    return value


def _file_path(repo, name):
    relative = Path(name)
    if relative.is_absolute() or not relative.parts or any(part.casefold() in ("..", ".git") for part in relative.parts):
        raise LifecycleError("path must name a workspace file outside Git metadata")
    candidate = repo / relative
    _no_links(candidate, repo)
    if repo not in candidate.resolve().parents:
        raise LifecycleError("plan path escapes workspace")
    for parent in candidate.parents:
        if parent == repo:
            break
        marker = parent / '.git'
        if marker.exists() or marker.is_symlink():
            raise LifecycleError('plan path belongs to a nested Git checkout')
    return candidate


def _checked_paths(repo, entries, *, need, owner, completed=None):
    paths = []
    for item in entries:
        if (not isinstance(item, dict) or not isinstance(item.get("path"), str)
                or item.get("classification") != need or item.get("owner") != owner
                or not isinstance(item.get("evidence"), str) or not item['evidence'].strip()):
            raise LifecycleError("each plan entry needs exact path, classification, task owner and ownership evidence")
        name = item['path']
        candidate = _file_path(repo, name)
        if completed and name in completed:
            paths.append(name)
            continue
        if need == 'source' and item.get('deleted') is True:
            previous = subprocess.run(['git', '-C', str(repo), 'cat-file', '--filters', 'HEAD:' + name], capture_output=True)
            # After a recorded commit, the exact before-image remains in its parent.
            if previous.returncode:
                previous = subprocess.run(['git', '-C', str(repo), 'cat-file', '--filters', 'HEAD^:' + name], capture_output=True)
            if (candidate.exists() or candidate.is_symlink() or previous.returncode
                    or hashlib.sha256(previous.stdout).hexdigest() != item.get('before_sha256')):
                raise LifecycleError('deleted source does not match its reviewed before-image')
        elif not candidate.is_file() or item.get('sha256') != _sha(candidate):
            raise LifecycleError("plan file disappeared or content hash changed: " + name)
        if need == 'source':
            if item.get('safe_to_commit') is not True:
                raise LifecycleError('source requires explicit safe-to-commit attestation')
            if git(repo, 'check-ignore', '--quiet', '--', name, optional=True) is not None:
                raise LifecycleError('ignored data cannot be committed by finish')
        paths.append(name)
    return paths


def _exception(plan, paths):
    exception = plan.get('exception')
    alternatives = exception.get('alternatives') if isinstance(exception, dict) else None
    categories = {x.get('category') for x in alternatives if isinstance(x, dict)} if isinstance(alternatives, list) else set()
    if (not isinstance(exception, dict) or exception.get('reviewed_all_alternatives') is not True
            or not isinstance(alternatives, list) or len(alternatives) != 4
            or categories != {'commit', 'restore', 'archive', 'owner-resolution'}
            or not exception.get('remaining_owner') or not exception.get('next_action')
            or any(not x.get('irreversible_harm') or not x.get('evidence') for x in alternatives)):
        raise LifecycleError('unresolved dirty is not a completion exception: review all applicable alternatives and preserve evidence/owner/next action')
    return {**exception, 'paths': sorted(paths), 'at': _now()}


def _index_paths(repo):
    result = subprocess.run(['git', '-C', str(repo), 'diff', '--cached', '--name-only', '-z'], capture_output=True, check=True)
    return {os.fsdecode(x) for x in result.stdout.split(b'\0') if x}


def _index_protection(repo):
    result = subprocess.run(['git', '-C', str(repo), 'ls-files', '-v', '-z'], capture_output=True, check=True)
    if any(row[:1].islower() or row[:1] == b'S' for row in result.stdout.split(b'\0') if row):
        raise LifecycleError('index assume-unchanged/skip-worktree flags require explicit owner resolution')
    if git(repo, 'ls-files', '--unmerged'):
        raise LifecycleError('unmerged index must be resolved in its existing worktree')


def _sync_file(path):
    with path.open('r+b') as stream:
        os.fsync(stream.fileno())
    if os.name != 'nt':
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _resolve_files(repo, plan, intent, directory, state):
    for entry in plan.get('archive', []):
        name = entry['path']; source = _file_path(repo, name)
        approval = entry.get('approval_evidence')
        store = Path(entry.get('store', ''))
        if not approval or not store.is_absolute() or not store.is_dir():
            raise LifecycleError('archive needs an explicit existing authorized absolute store and approval evidence')
        _no_links(store)
        store = store.resolve()
        if store == repo or repo in store.parents or git(store, 'rev-parse', '--show-toplevel', optional=True):
            raise LifecycleError('private archive store cannot be inside a Git worktree')
        filename = entry.get('name', source.name)
        if not isinstance(filename, str) or Path(filename).name != filename or filename in ('.', '..'):
            raise LifecycleError('archive destination name must be a filename')
        destination = store / filename
        action = intent['actions'].get(name)
        if action:
            if action['destination'] != str(destination) or action['sha256'] != entry['sha256']:
                raise LifecycleError('archive recovery identity differs')
        else:
            if destination.exists() or destination.is_symlink():
                raise LifecycleError('archive destination exists without this task preservation receipt')
            action = {'kind': 'archive', 'destination': str(destination),
                      'sha256': entry['sha256'], 'phase': 'copying'}
            intent['actions'][name] = action
            save_state(directory, state)
        if destination.is_symlink():
            raise LifecycleError('archive destination became a link')
        if action['phase'] == 'copying':
            if not source.is_file() or _sha(source) != entry['sha256']:
                raise LifecycleError('private source changed during preservation')
            if destination.exists():
                partial = destination.read_bytes()
                if not source.read_bytes().startswith(partial):
                    raise LifecycleError('partial archive was changed outside this operation')
                with destination.open('ab') as output, source.open('rb') as incoming:
                    incoming.seek(len(partial)); shutil.copyfileobj(incoming, output)
                    output.flush(); os.fsync(output.fileno())
            else:
                fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'wb') as output, source.open('rb') as incoming:
                    shutil.copyfileobj(incoming, output)
                    output.flush(); os.fsync(output.fileno())
        elif not destination.is_file() or _sha(destination) != entry['sha256']:
            raise LifecycleError('preserved archive changed; keep remaining workspace data')
        if action['phase'] == 'copying':
            _sync_file(destination)
        if _sha(destination) != entry['sha256']:
            raise LifecycleError('archive readback differs')
        action = intent['actions'][name]
        action['phase'] = 'preserved'; save_state(directory, state)
        if source.exists():
            if source.is_symlink() or _sha(source) != entry['sha256']:
                raise LifecycleError('private source changed after archive; preserve both')
            shutil.copystat(source, destination)
            source.unlink()
        action['phase'] = 'resolved'; save_state(directory, state)
    for entry in plan.get('restore', []):
        name = entry['path']; path = _file_path(repo, name)
        proof = entry.get('regeneration')
        if not isinstance(proof, dict) or not proof.get('evidence'):
            raise LifecycleError('restore needs verified regeneration evidence')
        action = intent['actions'].get(name)
        if action and action['phase'] == 'resolved':
            continue
        tracked = git(repo, 'ls-files', '--error-unmatch', '--', name, optional=True) is not None
        if action and not path.exists() and not tracked:
            action['phase'] = 'resolved'; save_state(directory, state)
            continue
        original = subprocess.run(['git', '-C', str(repo), 'cat-file', '--filters', 'HEAD:' + name], capture_output=True)
        restored = tracked and original.returncode == 0 and path.is_file() and path.read_bytes() == original.stdout
        if not restored and (not path.is_file() or _sha(path) != entry['sha256']):
            raise LifecycleError('generated output changed before restore')
        intent['actions'][name] = {'kind': 'restore', 'phase': 'restoring'}
        save_state(directory, state)
        if tracked:
            git(repo, 'restore', '--source=HEAD', '--staged', '--worktree', '--', name)
        elif path.exists():
            path.unlink()  # exact owned reproducible file, never a recursive clean
        intent['actions'][name]['phase'] = 'resolved'; save_state(directory, state)



def finish(repo, *, task: str, plan_path: str, result_ref: str, message: str = 'workspace lifecycle completion', users_released: bool = False, revision_evidence: str | None = None) -> dict:
    repo = Path(repo).resolve(); plan = _plan(Path(plan_path)); task_id = task
    if not isinstance(result_ref, str) or not result_ref.strip():
        raise LifecycleError('durable result reference is required')
    if result_ref.startswith(str(repo)):
        raise LifecycleError('result must be preserved outside the retiring workspace')
    plan_digest = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
    with _lease(repo, task_id, allow_use=True):
        with locked_state(repo) as (directory, state):
            task = _task(state, task_id); branch = branch_for_task(repo, task_id)
            if top(repo) != task_worktree(repo, branch) or current_branch(repo) != branch or bound_task(repo) != task_id:
                raise LifecycleError('finish must run in the bound task worktree')
            if task.get('hold'):
                raise LifecycleError('task is held: ' + json.dumps(task['hold']))
            _index_protection(repo)
            intent = state['intents'].get(task_id)
            if intent and intent['kind'] != 'finish':
                # A preserved integration operation belongs to _integrate; do
                # not replace it or recommit the accepted source on resume.
                accepted = task.get('acceptance')
                if not accepted or accepted['commit'] != head(repo):
                    raise LifecycleError('pending operation does not match accepted source')
                commit = accepted['commit']
            else:
                if intent and (intent['plan_digest'] != plan_digest or intent['result_ref'] != result_ref):
                    if not revision_evidence or intent['result_ref'] != result_ref:
                        raise LifecycleError('unfinished finish requires the same reviewed plan and result, or explicit plan revision evidence')
                    if head(repo) not in (intent['initial_head'], intent.get('committed')):
                        raise LifecycleError('resolve interrupted commit identity before revising its plan')
                    if any(action['phase'] != 'resolved' for action in intent['actions'].values()):
                        raise LifecycleError('finish pending preservation actions before revising the plan')
                    for action in intent['actions'].values():
                        if action['kind'] == 'archive':
                            saved = Path(action['destination'])
                            if not saved.is_file() or saved.is_symlink() or _sha(saved) != action['sha256']:
                                raise LifecycleError('previous preservation evidence changed')
                    task.setdefault('finish_receipts', []).append({**intent, 'revision_evidence': revision_evidence})
                    intent = None
                if not intent:
                    intent = {'kind': 'finish', 'initial_head': head(repo), 'plan_digest': plan_digest,
                              'result_ref': result_ref, 'actions': {}, 'at': _now()}
                    state['intents'][task_id] = intent
                    save_state(directory, state)
                if head(repo) not in {intent['initial_head'], intent.get('committed')}:
                    # Crash after commit but before receipt: prove the exact
                    # planned tree and sole parent from native immutable objects.
                    expected_tree = intent.get('commit_tree')
                    parents = (git(repo, 'rev-list', '--parents', '-n', '1', 'HEAD') or '').split()
                    if not expected_tree or git(repo, 'rev-parse', 'HEAD^{tree}') != expected_tree or parents[1:] != [intent['initial_head']]:
                        raise LifecycleError('HEAD changed outside this finish intent')
                    intent['committed'] = head(repo); save_state(directory, state)
                resolved = {name for name, action in intent['actions'].items() if action['phase'] in ('resolved', 'preserved', 'restoring')}
                commit_paths = _checked_paths(repo, plan.get('commit', []), need='source', owner=task_id)
                restore_paths = _checked_paths(repo, plan.get('restore', []), need='reproducible', owner=task_id, completed=resolved)
                archive_paths = _checked_paths(repo, plan.get('archive', []), need='private', owner=task_id, completed=resolved)
                all_paths = commit_paths + restore_paths + archive_paths
                if len(all_paths) != len(set(all_paths)):
                    raise LifecycleError('plan categories overlap')
                protected = task.get('adoption', {}).get('protected_paths', [])
                if any(path == boundary or path.startswith(boundary + '/')
                       for path in all_paths for boundary in protected):
                    raise LifecycleError('plan path belongs to a preserved nested Git checkout')
                if any(path == baseline or (baseline.endswith('/') and path.startswith(baseline))
                       for path in all_paths for baseline in task['baseline_dirty']):
                    raise LifecycleError('preexisting dirty is not owned by this task')
                if _index_paths(repo) - set(commit_paths + restore_paths):
                    raise LifecycleError('index contains changes outside the owned finish plan')
                dirty = _snapshot(repo)
                uncovered = set(dirty) - set(all_paths)
                # Safe owned actions run first. Remaining dirt is never waived
                # merely because it existed before this session.
                precommit = _validate(repo, task['validation'])
                _resolve_files(repo, plan, intent, directory, state)
                _checked_paths(repo, plan.get('commit', []), need='source', owner=task_id)
                if _index_paths(repo) - set(commit_paths):
                    raise LifecycleError('validation or cleanup staged unrelated changes')
                if commit_paths and not intent.get('committed'):
                    git(repo, 'add', '--', *commit_paths)
                    if _index_paths(repo) - set(commit_paths):
                        raise LifecycleError('index changed before task commit')
                    if _index_paths(repo):
                        intent['commit_tree'] = git(repo, 'write-tree'); save_state(directory, state)
                        git(repo, 'commit', '-m', message)
                        intent['committed'] = head(repo); save_state(directory, state)
                evidence = _validate(repo, task['validation'])
                final_dirty = _snapshot(repo)
                if final_dirty:
                    exception = _exception(plan, final_dirty)
                    task['exception'] = exception
                    state['intents'].pop(task_id, None); save_state(directory, state)
                    return {'task': task_id, 'completion': 'exception', 'dirty': sorted(final_dirty)}
                commit = head(repo)
                pushed = _push(repo, task['preflight'], branch, task['remote'])
                task.pop('exception', None)
                task['acceptance'] = {'commit': commit, 'result_ref': result_ref,
                                      'validation': {'precommit': precommit, 'postcommit': evidence},
                                      'push': pushed, 'accepted_at': _now()}
                state['intents'].pop(task_id, None); save_state(directory, state)
    integration = _integrate(repo, task_id, result_ref)
    result = {'task': task_id, 'accepted': True, 'commit': commit, 'result_ref': result_ref, 'integration': integration}
    if users_released:
        with locked_state(repo) as (_, state):
            remote = _task(state, task_id)['remote']
        control = task_worktree(repo, remote_default(repo, remote))
        retire(control, task=task_id, result_ref=result_ref, users_released=True, request_only=True)
        try:
            result['retirement'] = retire(control, task=task_id, result_ref=result_ref)
        except (LifecycleError, ValueError, OSError) as exc:
            result['retirement'] = {'pending': True, 'reason': str(exc)}
    return result


def _remote_tip(repo, remote, branch):
    rows = (git(repo, 'ls-remote', remote, 'refs/heads/' + branch) or '').splitlines()
    if len(rows) != 1 or rows[0].split()[1:] != ['refs/heads/' + branch]:
        raise LifecycleError('remote integration branch could not be verified')
    return rows[0].split()[0]


def _integrate(repo: Path, task_id: str, result_ref: str) -> dict:
    """Resume only the declared, accepted edge, then follow its parent to default."""
    with locked_state(repo) as (_, state):
        item = _task(state, task_id)
        direction = item['integration']
        target_guard = direction['task'] if direction['kind'] == 'task' else '@default'
    with ExitStack() as stack:
        for identity in sorted({task_id, target_guard}):
            stack.enter_context(_lease(repo, identity, allow_use=identity == task_id))
        with locked_state(repo) as (directory, state):
            item = _task(state, task_id)
            if item.get('hold') or not item.get('acceptance'):
                raise LifecycleError('source is held or not accepted: ' + task_id)
            acceptance = item['acceptance']; source_commit = acceptance['commit']
            source_branch = branch_for_task(repo, task_id)
            if head(repo, 'refs/heads/' + source_branch) != source_commit:
                raise LifecycleError('source HEAD differs from the exact accepted identity')
            for dependency in item['dependencies']:
                required = _task(state, dependency)
                if required.get('hold') or not required.get('acceptance'):
                    raise LifecycleError('dependency is held or not accepted: ' + dependency)
                if head(repo, 'refs/heads/' + branch_for_task(repo, dependency)) != required['acceptance']['commit']:
                    raise LifecycleError('dependency changed after acceptance: ' + dependency)
            parent = None
            if direction['kind'] == 'task':
                parent = _task(state, direction['task'])
                if parent.get('hold') or not parent.get('acceptance'):
                    raise LifecycleError('integration parent is held or not accepted: ' + direction['task'])
                target_branch = branch_for_task(repo, direction['task'])
            else:
                target_branch = remote_default(repo, item['remote'])
            target_worktree = task_worktree(repo, target_branch)
            if current_branch(target_worktree) != target_branch:
                raise LifecycleError('integration checkout branch changed')
            record = item.get('integrated')
            if record and record['source'] == source_commit:
                if git(repo, 'merge-base', '--is-ancestor', record['commit'], head(target_worktree), optional=True) is None:
                    raise LifecycleError('recorded integration is no longer in its target')
                merged = record['commit']
            else:
                intent = state['intents'].get(task_id)
                if intent and (intent['kind'] != 'merge' or intent['source'] != source_commit
                               or intent['target'] != target_branch):
                    raise LifecycleError('pending integration identity differs; preserve the existing operation')
                if not intent:
                    if _snapshot(target_worktree) or git(target_worktree, 'rev-parse', '--verify', 'MERGE_HEAD', optional=True):
                        raise LifecycleError('integration target has unrelated dirty or merge state')
                    target_head = head(target_worktree)
                    if parent and target_head != parent['acceptance']['commit']:
                        raise LifecycleError('parent HEAD differs from its accepted identity')
                    if target_head != _remote_tip(repo, item['remote'], target_branch):
                        raise LifecycleError('integration target differs from its actual remote tip; resolve in this same target')
                    intent = {'kind': 'merge', 'source': source_commit, 'target': target_branch,
                              'target_head': target_head, 'result_ref': result_ref, 'at': _now()}
                    state['intents'][task_id] = intent; save_state(directory, state)
                current = head(target_worktree)
                merge_head = git(target_worktree, 'rev-parse', '--verify', 'MERGE_HEAD', optional=True)
                if current != intent['target_head']:
                    parents = (git(target_worktree, 'rev-list', '--parents', '-n', '1', 'HEAD') or '').split()[1:]
                    if merge_head or parents != [intent['target_head'], source_commit]:
                        raise LifecycleError('target moved outside the recorded normal merge')
                    intent['merged'] = current; save_state(directory, state)
                elif not merge_head:
                    if _snapshot(target_worktree):
                        raise LifecycleError('pending target has unrelated dirty content')
                    if git(repo, 'merge-base', '--is-ancestor', source_commit, current, optional=True) is not None:
                        intent['merged'] = current
                    else:
                        git(target_worktree, 'merge', '--no-ff', '--no-commit', source_commit)
                        merge_head = git(target_worktree, 'rev-parse', '--verify', 'MERGE_HEAD', optional=True)
                if not intent.get('merged'):
                    if merge_head != source_commit:
                        raise LifecycleError('native merge identity differs from the requested source')
                    _index_protection(target_worktree)
                # Both the child and its parent contract must hold for the new
                # parent identity before it can progress to the next edge.
                evidence = _validate(target_worktree, item['validation'])
                parent_evidence = _validate(target_worktree, parent['validation']) if parent else None
                dirty = _snapshot(target_worktree)
                if any(code in ('??', '!!') or code[1] != ' ' for code in dirty.values()):
                    raise LifecycleError('merge validation left unstaged or private data; resolve in this target')
                if not intent.get('merged'):
                    intent['merge_tree'] = git(target_worktree, 'write-tree'); save_state(directory, state)
                    git(target_worktree, 'commit', '-m', 'Merge workspace task ' + task_id)
                    intent['merged'] = head(target_worktree); save_state(directory, state)
                if _snapshot(target_worktree):
                    raise LifecycleError('integration commit left dirty content')
                merged = intent['merged']
                pushed = _push(target_worktree, item['preflight'], target_branch, item['remote'])
                record = {'destination': target_branch, 'commit': merged, 'source': source_commit,
                          'validation': evidence, 'push': pushed, 'result_ref': result_ref, 'at': _now()}
                item['integrated'] = record
                if parent:
                    parent['acceptance'] = {**parent['acceptance'], 'commit': merged,
                        'previous_accepted': parent['acceptance']['commit'],
                        'child_integration': {'task': task_id, 'source': source_commit},
                        'validation': parent_evidence, 'push': pushed, 'accepted_at': _now()}
                state['intents'].pop(task_id, None); save_state(directory, state)
    result = {'destination': target_branch, 'commit': merged}
    if direction['kind'] == 'task':
        result['parent'] = _integrate(repo, direction['task'], result_ref)
    return result


def _retirement_contents(workspace):
    """Validate preexisting content before the no-delete retirement boundary."""
    _index_protection(workspace)
    snapshot = _snapshot(workspace)
    if any(code != '!!' for code in snapshot.values()) or git(workspace, 'submodule', 'status', '--recursive'):
        raise LifecycleError('retire refuses dirty, untracked, ignored, or submodule data')
    process = subprocess.run(['git', '-C', str(workspace), 'ls-files', '-z'], capture_output=True, check=True)
    tracked = {os.fsdecode(row) for row in process.stdout.split(b'\0') if row}
    directories = {str(parent).replace(os.sep, '/') for name in tracked for parent in Path(name).parents if str(parent) != '.'}
    empty_directories = set()
    def walk_error(error):
        raise error
    for current, dirs, files in os.walk(workspace, topdown=True, followlinks=False, onerror=walk_error):
        relative = Path(current).relative_to(workspace)
        for name in [*dirs, *files]:
            path = Path(current) / name; logical = (relative / name).as_posix()
            _no_links(path, workspace)
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400):
                raise LifecycleError('retire refuses links or reparse points')
            if logical == '.git':
                if not stat.S_ISREG(info.st_mode):
                    raise LifecycleError('only linked worktrees can retire')
                continue
            if os.path.ismount(path):
                raise LifecycleError('retire refuses linked or mounted filesystem content')
            if stat.S_ISDIR(info.st_mode):
                if logical not in directories:
                    empty_directories.add(logical)
            elif logical not in tracked or not stat.S_ISREG(info.st_mode):
                raise LifecycleError('retire refuses nontracked or special filesystem content')
    # An ignored Git entry is admissible only when this filesystem walk proved
    # it is an unowned directory tree containing no files or protected objects.
    if any(name.rstrip('/') not in empty_directories for name in snapshot):
        raise LifecycleError('retire refuses ignored filesystem content')


def _retirement_identity(path):
    _no_links(path)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or os.path.ismount(path):
        raise LifecycleError('retirement directory identity changed')
    return [info.st_dev, info.st_ino]


def _retirement_admin(workspace, common):
    admin = Path(git(workspace, 'rev-parse', '--path-format=absolute', '--git-dir')).resolve()
    admin_root = Path(git(workspace, 'rev-parse', '--path-format=absolute', '--git-path', 'worktrees')).resolve()
    if admin.parent != admin_root or admin_root.parent != common or admin == common:
        raise LifecycleError('target is not an exact linked worktree admin entry')
    if Path(git(workspace, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve() != common:
        raise LifecycleError('retirement Git common directory changed')
    return admin


def _retirement_pointer(payload, admin_original, admin_at):
    for path in (payload / '.git', admin_at / 'gitdir'):
        _no_links(path)
        if not stat.S_ISREG(path.lstat().st_mode):
            raise LifecycleError('retirement Git pointer is not a regular file')
    pointer = (payload / '.git').read_text(encoding='utf-8').strip()
    if not pointer.startswith('gitdir: '):
        raise LifecycleError('retirement payload Git pointer changed')
    value = Path(pointer[len('gitdir: '):])
    if not value.is_absolute():
        value = payload / value
    if value.resolve() != admin_original:
        raise LifecycleError('retirement payload Git pointer changed')
    gitdir = (admin_at / 'gitdir').read_text(encoding='utf-8').strip()
    value = Path(gitdir)
    if not value.is_absolute():
        value = admin_original / value
    if value.resolve() != (payload / '.git').resolve():
        raise LifecycleError('retirement admin Git pointer changed')


def _retirement_record(records, path):
    return [row for row in records if Path(row['worktree']) == path]


def retire(repo, *, task: str, result_ref: str, users_released: bool = False, request_only: bool = False) -> dict:
    """End one active worktree while retaining its payload and Git admin entry."""
    repo = Path(repo).resolve()
    with _lease(repo, task, allow_use=request_only):
        with locked_state(repo) as (directory, state):
            if task in state.get('retired', {}):
                return {'task': task, 'retired': True, 'receipt': state['retired'][task]}
            item = _task(state, task); request = item.get('retire')
            acceptance = item.get('acceptance')
            if item.get('hold') or not acceptance or acceptance.get('result_ref') != result_ref:
                raise LifecycleError('retire requires exact accepted result and no hold')
            if not request and not users_released:
                raise LifecycleError('retire requires explicit external-user release')
            for other_id, other in state['tasks'].items():
                if other_id != task and (task in other['dependencies'] or other['integration'].get('task') == task):
                    raise LifecycleError('another task still depends on this identity: ' + other_id)
            expected = acceptance['commit']
            integrated = item.get('integrated')
            if not integrated or integrated['source'] != expected:
                raise LifecycleError('retire requires exact accepted integration')
            if not request:
                default = remote_default(repo, item['remote'])
                git(repo, 'fetch', item['remote'], 'refs/heads/' + default)
                if git(repo, 'merge-base', '--is-ancestor', expected, 'FETCH_HEAD', optional=True) is None:
                    raise LifecycleError('accepted source has not reached the actual remote default')
            branch = request['branch'] if request else branch_for_task(repo, task)
            if head(repo, 'refs/heads/' + branch) != expected:
                raise LifecycleError('retire expected accepted HEAD; branch changed')
            workspace = Path(request['path']) if request else task_worktree(repo, branch)
            if workspace == Path(worktree_records(repo)[0]['worktree']).resolve():
                raise LifecycleError('primary checkout cannot retire')
            if not request_only and (repo == workspace or workspace in repo.parents or Path.cwd() == workspace or workspace in Path.cwd().parents):
                raise LifecycleError('retire must run outside its target worktree')
            for dependency in (Path(__file__).resolve(), Path(os.sys.executable).resolve()):
                if workspace == dependency or workspace in dependency.parents:
                    raise LifecycleError('retire target supplies this lifecycle runtime')
            if not request:
                identity = _retirement_identity(workspace)
                common = directory.parent
                admin = _retirement_admin(workspace, common)
                admin_identity = _retirement_identity(admin)
                _retirement_pointer(workspace, admin, admin)
                token = uuid.uuid4().hex
                recovery_root = workspace.parent / '.workspace-lifecycle-recovery'
                if os.path.lexists(recovery_root):
                    _no_links(recovery_root)
                    if _retirement_identity(recovery_root)[0] != identity[0]:
                        raise LifecycleError('recovery payload root is on another filesystem')
                else:
                    recovery_root.mkdir()
                recovery = recovery_root / token
                if os.path.lexists(recovery):
                    raise LifecycleError('recovery payload destination already exists')
                admin_root = directory / 'recovery-admin'
                if os.path.lexists(admin_root):
                    _no_links(admin_root)
                else:
                    admin_root.mkdir()
                admin_slot = admin_root / token
                admin_slot.mkdir()
                if _retirement_identity(admin_slot)[0] != admin_identity[0]:
                    raise LifecycleError('recovery admin is on another filesystem')
                request = {'branch': branch, 'path': str(workspace), 'identity': identity,
                           'recovery_path': str(recovery), 'recovery_identity': identity,
                           'recovery_root': str(recovery_root), 'recovery_root_identity': _retirement_identity(recovery_root),
                           'admin_original_path': str(admin), 'admin_identity': admin_identity,
                           'admin_archive_path': str(admin_slot / 'admin'), 'admin_slot_identity': _retirement_identity(admin_slot),
                           'expected_head': expected, 'result_ref': result_ref,
                           'users_released': True, 'phase': 'requested', 'at': _now()}
                item['retire'] = request; save_state(directory, state)
            if request_only:
                return {'task': task, 'pending': True}
            recovery = Path(request['recovery_path'])
            admin = Path(request['admin_original_path'])
            archive = Path(request['admin_archive_path'])
            if (request['expected_head'] != expected or request['result_ref'] != result_ref
                    or not request['users_released']):
                raise LifecycleError('retirement request identity changed')
            if _retirement_identity(Path(request['recovery_root'])) != request['recovery_root_identity']:
                raise LifecycleError('recovery payload root identity changed')
            if _retirement_identity(archive.parent) != request['admin_slot_identity']:
                raise LifecycleError('recovery admin slot identity changed')
            if os.path.lexists(recovery):
                if _retirement_identity(recovery) != request['recovery_identity']:
                    raise LifecycleError('recovery payload identity changed')
                if os.path.lexists(admin) and os.path.lexists(archive):
                    raise LifecycleError('both active and archived admin entries exist')
            else:
                if request['phase'] not in ('requested', 'moving') or os.path.lexists(archive):
                    raise LifecycleError('recovery payload disappeared before retirement completed')
                if _retirement_identity(workspace) != request['identity']:
                    raise LifecycleError('retirement filesystem identity changed')
                records = worktree_records(repo)
                found = _retirement_record(records, workspace)
                if len(found) != 1 or found[0].get('branch') != 'refs/heads/' + branch or head(workspace) != expected:
                    raise LifecycleError('retirement Git identity changed')
                if _retirement_admin(workspace, directory.parent) != admin or _retirement_identity(admin) != request['admin_identity']:
                    raise LifecycleError('retirement admin identity changed')
                _retirement_pointer(workspace, admin, admin)
                if request['phase'] == 'requested':
                    _retirement_contents(workspace)
                    request['phase'] = 'moving'; save_state(directory, state)
                git(repo, 'worktree', 'move', str(workspace), str(recovery))
            if _retirement_identity(recovery) != request['recovery_identity']:
                raise LifecycleError('recovery payload identity changed')
            if os.path.lexists(admin):
                if _retirement_identity(admin) != request['admin_identity']:
                    raise LifecycleError('retirement admin identity changed')
                found = _retirement_record(worktree_records(repo), recovery)
                if len(found) != 1 or found[0].get('branch') != 'refs/heads/' + branch or head(recovery) != expected:
                    raise LifecycleError('recovery Git identity changed')
                _retirement_pointer(recovery, admin, admin)
                if request['phase'] != 'recovery-held':
                    request['phase'] = 'recovery-held'; save_state(directory, state)
                if os.path.lexists(archive):
                    raise LifecycleError('admin archive destination already exists')
                request['phase'] = 'archiving-admin'; save_state(directory, state)
                admin.rename(archive)
            elif not os.path.lexists(archive):
                raise LifecycleError('retirement admin entry disappeared without archive')
            if _retirement_identity(recovery) != request['recovery_identity'] or _retirement_identity(archive) != request['admin_identity']:
                raise LifecycleError('retirement archive identity changed')
            _retirement_pointer(recovery, admin, archive)
            records = worktree_records(repo)
            if _retirement_record(records, recovery) or _retirement_record(records, workspace):
                raise LifecycleError('target remains registered as an active Git worktree')
            if head(repo, 'refs/heads/' + branch) != expected:
                raise LifecycleError('retired branch was not preserved')
            binding = git(repo, 'config', '--local', '--get', 'branch.' + branch + '.workspaceTask', optional=True)
            if binding not in (None, task):
                raise LifecycleError('retired branch binding changed')
            if binding:
                git(repo, 'config', '--local', '--unset', 'branch.' + branch + '.workspaceTask')
            state.setdefault('retired', {})[task] = {'commit': expected, 'result_ref': result_ref,
                'branch': branch, 'remote': item['remote'], 'phase': 'retired',
                'users_released': request['users_released'],
                'removed_from_active_at': _now(), 'recovery_path': str(recovery),
                'original_identity': request['identity'], 'recovery_identity': request['recovery_identity'],
                'admin_original_path': str(admin), 'admin_archive_path': str(archive),
                'admin_identity': request['admin_identity'],
                'original_path': str(workspace), 'recovery_held': True,
                'content_manifest': _tree_manifest(recovery),
                'admin_manifest': _tree_manifest(archive)}
            del state['tasks'][task]
            state['intents'].pop(task, None); save_state(directory, state)
    return {'task': task, 'retired': True, 'branch': branch, 'receipt': state['retired'][task]}


def _tree_manifest(root: Path):
    """Hash one ordinary directory tree. Refuse links, mounts, and nested Git."""
    root = Path(root)
    _no_links(root)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or os.path.ismount(root):
        raise LifecycleError('reclaim target is not an ordinary directory')
    rows = []

    def walk_error(error):
        raise LifecycleError('reclaim cannot read ' + str(error.filename)) from error

    for current, dirs, files in os.walk(root, topdown=True, followlinks=False, onerror=walk_error):
        relative = Path(current).relative_to(root)
        for name in list(dirs):
            path = Path(current) / name
            logical = (relative / name).as_posix()
            _no_links(path, root)
            child = path.lstat()
            if os.path.ismount(path) or not stat.S_ISDIR(child.st_mode):
                raise LifecycleError('reclaim refuses a mount or non-directory')
            if name == '.git':
                raise LifecycleError('reclaim refuses a nested repository')
            rows.append([logical, 'dir', ''])
        for name in files:
            path = Path(current) / name
            logical = (relative / name).as_posix()
            _no_links(path, root)
            child = path.lstat()
            if logical != '.git' and name == '.git':
                raise LifecycleError('reclaim refuses a nested repository')
            if not stat.S_ISREG(child.st_mode):
                raise LifecycleError('reclaim refuses a special file')
            rows.append([logical, 'file', _sha(path)])
    rows.sort()
    return rows


def _remove_matching_tree(root: Path, manifest):
    live = _tree_manifest(root)
    if live != manifest:
        raise LifecycleError('reclaim content changed; holding payload')
    files = [row for row in manifest if row[1] == 'file']
    dirs = [row for row in manifest if row[1] == 'dir']
    for logical, _kind, digest in files:
        path = root / logical
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or _sha(path) != digest:
            raise LifecycleError('reclaim content changed during disposal; holding remainder')
        path.unlink()
    for logical, _kind, _digest in sorted(dirs, key=lambda row: row[0].count('/'), reverse=True):
        path = root / logical
        if path.is_symlink() or os.path.ismount(path):
            raise LifecycleError('reclaim refuses a changed directory')
        path.rmdir()
    if root.is_symlink() or os.path.ismount(root):
        raise LifecycleError('reclaim refuses a changed directory')
    root.rmdir()


def reclaim(repo, *, task: str, result_ref: str, preservation_evidence: str) -> dict:
    """Delete one retired payload only after its recorded manifest still matches."""
    repo = Path(repo).resolve()
    if not preservation_evidence or '\n' in preservation_evidence or '\0' in preservation_evidence:
        raise LifecycleError('reclaim requires durable preservation evidence')
    with _lease(repo, task):
        with locked_state(repo) as (directory, state):
            if task in state.get('tasks', {}):
                raise LifecycleError('reclaim refuses an active or in-flight retirement')
            receipt = state.get('retired', {}).get(task)
            if not receipt:
                raise LifecycleError('reclaim requires a retired receipt')
            if receipt.get('result_ref') != result_ref:
                raise LifecycleError('reclaim result reference does not match the retired receipt')
            if receipt.get('reclaim_phase') == 'reclaimed':
                return {'task': task, 'reclaimed': True, 'receipt': receipt}
            if not receipt.get('content_manifest') or not receipt.get('remote'):
                raise LifecycleError('reclaim holds a receipt without a retirement content manifest')
            stored = receipt.get('preservation_evidence')
            if stored and stored != preservation_evidence:
                raise LifecycleError('reclaim preservation evidence changed')
            if not stored:
                receipt['preservation_evidence'] = preservation_evidence
                save_state(directory, state)
            default = remote_default(repo, receipt['remote'])
            git(repo, 'fetch', receipt['remote'], 'refs/heads/' + default)
            if git(repo, 'merge-base', '--is-ancestor', receipt['commit'], 'FETCH_HEAD', optional=True) is None:
                raise LifecycleError('retired commit is no longer on the remote default')
            if not receipt.get('admin_manifest'):
                raise LifecycleError('reclaim holds a receipt without a retirement content manifest')
            phase = receipt.get('reclaim_phase')
            recovery = Path(receipt['recovery_path'])
            archive = Path(receipt['admin_archive_path'])
            if phase in (None, 'authorized'):
                staging = Path(receipt['reclaim_staging']) if receipt.get('reclaim_staging') else recovery.parent / (recovery.name + '.reclaim')
                if phase is None:
                    if not recovery.exists():
                        raise LifecycleError('recovery payload disappeared before reclaim')
                    if staging.exists():
                        raise LifecycleError('reclaim staging path already exists')
                    if _retirement_identity(recovery) != receipt['recovery_identity']:
                        raise LifecycleError('recovery payload identity changed')
                    if not archive.exists() or _retirement_identity(archive) != receipt['admin_identity']:
                        raise LifecycleError('recovery admin identity changed')
                    if _tree_manifest(recovery) != receipt['content_manifest']:
                        raise LifecycleError('recovery content changed after retirement; holding payload')
                    if _tree_manifest(archive) != receipt['admin_manifest']:
                        raise LifecycleError('recovery admin content changed after retirement; holding payload')
                    receipt['reclaim_staging'] = str(staging)
                    receipt['reclaim_phase'] = 'authorized'
                    save_state(directory, state)
                    recovery.rename(staging)
                elif recovery.exists() and not staging.exists():
                    if _retirement_identity(recovery) != receipt['recovery_identity']:
                        raise LifecycleError('recovery payload identity changed')
                    if not archive.exists() or _retirement_identity(archive) != receipt['admin_identity']:
                        raise LifecycleError('recovery admin identity changed')
                    if _tree_manifest(recovery) != receipt['content_manifest']:
                        raise LifecycleError('recovery content changed after retirement; holding payload')
                    if _tree_manifest(archive) != receipt['admin_manifest']:
                        raise LifecycleError('recovery admin content changed after retirement; holding payload')
                    recovery.rename(staging)
                elif recovery.exists() or not staging.exists():
                    raise LifecycleError('reclaim staging does not match the authorized receipt')
                if _retirement_identity(staging) != receipt['recovery_identity']:
                    raise LifecycleError('reclaim staging identity changed')
                if _tree_manifest(staging) != receipt['content_manifest']:
                    if recovery.exists():
                        raise LifecycleError('recovery content changed after rename; holding payload')
                    staging.rename(recovery)
                    raise LifecycleError('recovery content changed after rename; holding payload')
                receipt['reclaim_phase'] = 'renamed'
                save_state(directory, state)
                phase = 'renamed'
            if phase == 'renamed':
                staging = Path(receipt['reclaim_staging'])
                if _retirement_identity(staging) != receipt['recovery_identity']:
                    raise LifecycleError('reclaim staging identity changed')
                _remove_matching_tree(staging, receipt['content_manifest'])
                receipt['reclaim_phase'] = 'payload-removed'
                save_state(directory, state)
                phase = 'payload-removed'
            if phase == 'payload-removed':
                if not archive.exists() or _retirement_identity(archive) != receipt['admin_identity']:
                    raise LifecycleError('recovery admin identity changed')
                if _tree_manifest(archive) != receipt['admin_manifest']:
                    raise LifecycleError('recovery admin content changed after retirement; holding payload')
                receipt['reclaim_phase'] = 'admin-removing'
                save_state(directory, state)
                _remove_matching_tree(archive, receipt['admin_manifest'])
                phase = 'admin-removing'
            if phase == 'admin-removing':
                if archive.exists():
                    if _retirement_identity(archive) != receipt['admin_identity']:
                        raise LifecycleError('recovery admin identity changed')
                    if _tree_manifest(archive) != receipt['admin_manifest']:
                        raise LifecycleError('recovery admin content changed after retirement; holding payload')
                    _remove_matching_tree(archive, receipt['admin_manifest'])
                receipt['reclaim_phase'] = 'admin-removed'
                save_state(directory, state)
                phase = 'admin-removed'
            if phase == 'admin-removed':
                receipt['phase'] = 'reclaimed'
                receipt['recovery_held'] = False
                receipt['reclaim_phase'] = 'reclaimed'
                receipt['reclaimed_at'] = _now()
                save_state(directory, state)
            return {'task': task, 'reclaimed': True, 'receipt': receipt}


def reclaim_pending(repo) -> dict:
    """Resume reclaim that already has preservation evidence. Never authorize a new one."""
    repo = Path(repo).resolve()
    with locked_state(repo) as (_, state):
        pending = [(name, item['result_ref'], item['preservation_evidence'])
                   for name, item in state.get('retired', {}).items()
                   if item.get('preservation_evidence') and item.get('reclaim_phase') not in (None, 'reclaimed')]
    results = []
    for name, reference, evidence in pending:
        try:
            results.append(reclaim(repo, task=name, result_ref=reference, preservation_evidence=evidence))
        except (LifecycleError, ValueError, OSError, subprocess.CalledProcessError) as exc:
            results.append({'task': name, 'reclaimed': False, 'error': str(exc)})
    return {'pending': results}


def retire_pending(repo) -> dict:
    """Retry only durable explicit retirement requests, never scan for cleanup."""
    repo = Path(repo).resolve()
    with locked_state(repo) as (_, state):
        pending = [(name, item['retire']['result_ref']) for name, item in state['tasks'].items() if item.get('retire')]
    results = []
    for name, reference in pending:
        try:
            results.append(retire(repo, task=name, result_ref=reference))
        except (LifecycleError, ValueError, OSError, subprocess.CalledProcessError) as exc:
            results.append({'task': name, 'retired': False, 'error': str(exc)})
    return {'pending': results}


def before_run(repo, task: str) -> None:
    with locked_state(repo) as (_, state):
        item = _task(state, task)
        if item.get('hold'):
            raise LifecycleError('task is held; explicit hold resolution is required')
        if item.get('retire'):
            raise LifecycleError('task has a retirement request; replay it before starting new use')
        if bound_task(repo) != task or task_worktree(repo, branch_for_task(repo, task)) != top(repo):
            raise LifecycleError('run must use the exact bound task checkout')
