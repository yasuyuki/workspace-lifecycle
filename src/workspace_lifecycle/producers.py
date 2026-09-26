"""Task-scoped owner receipt registration and idempotent completion callbacks."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess

from .errors import LifecycleError
from .git import bound_task, branch_for_task, common_dir, current_branch, git, head, task_worktree, top
from .state import locked_state, save_state


def _key(owner: str, generation: str) -> str:
    return hashlib.sha256((owner + "\0" + generation).encode()).hexdigest()


def task_receipt_dir(repo, task: str) -> Path:
    """External, task-private root for owner durability evidence."""
    return common_dir(repo) / 'workspace-lifecycle' / 'owner-receipts' / hashlib.sha256(task.encode()).hexdigest()


def _absolute(value, name: str, *, reject_links: bool = True) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise LifecycleError(name + ' must be an absolute path')
    if reject_links:
        from .service import _no_links
        _no_links(path)
    # An executable's invocation path selects its environment (for example a
    # venv Python symlink). Outputs and receipts still require canonical paths.
    return path.resolve(strict=False) if reject_links else path


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _overlap(left: Path, right: Path) -> bool:
    return _inside(left, right) or _inside(right, left)


def _validate_bound(repo: Path, task: str, item: dict) -> Path:
    branch = branch_for_task(repo, task)
    workspace = task_worktree(repo, branch)
    if top(repo) != workspace or current_branch(repo) != branch or bound_task(repo) != task:
        raise LifecycleError('owner receipt registration requires the bound task worktree')
    if item.get('acceptance') or item.get('hold') or item.get('retire') or item.get('completion_release'):
        raise LifecycleError('owner receipt registration requires an active unheld task')
    return workspace


def _validate_output(repo: Path, workspace: Path, item: dict, value: str) -> str:
    output = _absolute(value, 'owner output')
    if os.path.lexists(output):
        raise LifecycleError('owner output must be absent before generation')
    if output == workspace or output == top(repo):
        raise LifecycleError('owner output cannot be a repository root')
    if not _inside(output, workspace):
        return str(output)
    relative = output.relative_to(workspace).as_posix()
    if relative == '.git' or relative.startswith('.git/'):
        raise LifecycleError('owner output cannot overlap Git administration')
    if git(workspace, 'ls-files', '--error-unmatch', '--', relative, optional=True) is not None:
        raise LifecycleError('owner output cannot overlap tracked source')
    for name in item.get('baseline_dirty', {}):
        if relative == name or relative.startswith(name.rstrip('/') + '/') or name.rstrip('/').startswith(relative + '/'):
            raise LifecycleError('owner output cannot overlap baseline data')
    for name in item.get('adoption', {}).get('protected_paths', []):
        if relative == name or relative.startswith(name.rstrip('/') + '/') or name.rstrip('/').startswith(relative + '/'):
            raise LifecycleError('owner output cannot overlap a nested repository')
    return str(output)


def register(repo, task: str, owner: str, generation: str, receipt: str,
             outputs: list[str], completion: list[str]) -> dict:
    """Durably bind one owner generation before it creates any output."""
    from .service import _lease

    repo = Path(repo).resolve()
    if not all(isinstance(value, str) and value for value in (task, owner, generation)):
        raise LifecycleError('owner, task, and generation are required')
    if not isinstance(outputs, list) or not outputs or not all(isinstance(value, str) for value in outputs):
        raise LifecycleError('owner outputs require a nonempty string list')
    if not isinstance(completion, list) or not completion or not all(isinstance(value, str) and value for value in completion):
        raise LifecycleError('owner completion requires a nonempty string argv')
    with _lease(repo, task, allow_use=True):
        with locked_state(repo) as (directory, state):
            item = state['tasks'].get(task)
            if not item:
                raise LifecycleError('unknown task: ' + task)
            workspace = _validate_bound(repo, task, item)
            receipt_path = _absolute(receipt, 'owner receipt')
            receipt_root = task_receipt_dir(repo, task)
            if not _inside(receipt_path, receipt_root) or receipt_path == receipt_root:
                raise LifecycleError('owner receipt must be under the task receipt directory')
            executable = _absolute(completion[0], 'owner completion executable', reject_links=False)
            desired = {'owner': owner, 'generation': generation, 'receipt': str(receipt_path),
                       'outputs': [str(_absolute(value, 'owner output')) for value in outputs],
                       'completion': [str(executable), *completion[1:]], 'phase': 'registered'}
            if len(desired['outputs']) != len(set(desired['outputs'])):
                raise LifecycleError('owner outputs must be distinct')
            if any(_overlap(Path(left), Path(right))
                   for index, left in enumerate(desired['outputs'])
                   for right in desired['outputs'][index + 1:]):
                raise LifecycleError('owner outputs cannot overlap')
            key = _key(owner, generation)
            records = item.setdefault('owner_receipts', {})
            existing = records.get(key)
            if existing:
                immutable = ('owner', 'generation', 'receipt', 'outputs', 'completion')
                if {name: existing.get(name) for name in immutable} != {name: desired[name] for name in immutable}:
                    raise LifecycleError('owner generation is already registered with a different receipt')
                return deepcopy(existing)
            if not executable.is_file():
                raise LifecycleError('owner completion executable must exist')
            desired['outputs'] = [_validate_output(repo, workspace, item, value) for value in outputs]
            for other in records.values():
                for output in desired['outputs']:
                    if any(_overlap(Path(output), Path(claimed)) for claimed in other.get('outputs', [])):
                        raise LifecycleError('owner output is already claimed by another generation')
            try:
                receipt_path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise LifecycleError('owner receipt directory cannot be created before generation') from exc
            if not receipt_path.parent.is_dir():
                raise LifecycleError('owner receipt directory is not usable before generation')
            records[key] = desired
            save_state(directory, state)
            return deepcopy(desired)


def owned_dirty_paths(repo, task_record: dict) -> set[str]:
    """Exact registered output paths that finish may treat as owner-cleanable."""
    workspace = Path(repo).resolve()
    result = set()
    for record in task_record.get('owner_receipts', {}).values():
        for output in record.get('outputs', []):
            path = Path(output)
            if _inside(path, workspace):
                result.add(path.relative_to(workspace).as_posix())
    return result


def _run(record: dict, result_ref: str, workspace: Path, task: str) -> None:
    from .cli import _managed_context
    environment = {**os.environ, **_managed_context(workspace, task)}
    context = json.loads(environment['WORKSPACE_LIFECYCLE_CONTEXT'])
    context['owner_completion'] = True
    environment['WORKSPACE_LIFECYCLE_CONTEXT'] = json.dumps(context)
    environment.pop('WORKSPACE_LIFECYCLE_USE', None)
    argv = [part.replace('{result_ref}', result_ref) for part in record['completion']]
    try:
        process = subprocess.run(argv, cwd=workspace, text=True, encoding='utf-8',
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 env=environment)
    except OSError as exc:
        raise LifecycleError('owner completion could not start') from exc
    if process.returncode:
        raise LifecycleError(process.stderr.strip() or 'owner completion failed')
    try:
        reply = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise LifecycleError('owner completion did not emit JSON') from exc
    receipt = Path(record['receipt'])
    if (not isinstance(reply, dict) or reply.get('reclaimed') is not True
            or reply.get('generation') != record['generation']
            or reply.get('receipt') != str(receipt)):
        raise LifecycleError('owner completion did not confirm the exact generation')
    try:
        durable = json.loads(receipt.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise LifecycleError('owner receipt is not durably readable') from exc
    if (not isinstance(durable, dict) or durable.get('generation') != record['generation']
            or durable.get('owner') != record['owner'] or durable.get('output') not in record['outputs']
            or durable.get('receipt') != str(receipt) or durable.get('state') != 'reclaimed'
            or durable.get('hold') is not False or durable.get('accepted_proof') != result_ref
            or durable.get('released_proof') != result_ref or not isinstance(durable.get('sha256'), str)
            or not durable['sha256'] or not durable.get('source_revision')
            or not isinstance(durable.get('inputs'), list) or not isinstance(durable.get('identity'), dict)):
        raise LifecycleError('owner receipt does not confirm the exact generation')
    if any(os.path.lexists(output) for output in record['outputs']):
        raise LifecycleError('owner completion claimed reclaim while registered output remains')


def retry(repo, task: str) -> dict:
    """Complete only pre-registered owner generations after task acceptance."""
    from .service import _lease

    repo = Path(repo).resolve()
    results = []
    with _lease(repo, task, allow_use=True):
        with locked_state(repo) as (_, state):
            item = deepcopy(state['tasks'].get(task))
        if not item:
            raise LifecycleError('unknown task: ' + task)
        release = item.get('owner_release')
        acceptance = item.get('acceptance')
        if not item.get('owner_receipts'):
            return {'task': task, 'results': []}
        if item.get('hold') or not release or not acceptance or acceptance.get('commit') != release.get('commit'):
            raise LifecycleError('owner receipt completion requires the exact accepted release')
        if acceptance.get('result_ref') != release.get('result_ref'):
            raise LifecycleError('owner receipt release result does not match acceptance')
        branch = branch_for_task(repo, task)
        workspace = task_worktree(repo, branch)
        if current_branch(workspace) != branch or bound_task(workspace) != task:
            raise LifecycleError('owner receipt completion requires the bound task worktree')
        if head(workspace) != acceptance['commit']:
            raise LifecycleError('owner receipt completion refuses a changed accepted branch')
        for key, record in item['owner_receipts'].items():
            if record.get('phase') == 'completed':
                results.append({'generation': record['generation'], 'reclaimed': True})
                continue
            try:
                _run(record, release['result_ref'], workspace, task)
            except LifecycleError as exc:
                with locked_state(repo) as (directory, latest):
                    current = latest['tasks'].get(task, {}).get('owner_receipts', {}).get(key)
                    if current != record:
                        raise LifecycleError('owner receipt changed concurrently')
                    current['failure'] = str(exc)
                    save_state(directory, latest)
                results.append({'generation': record['generation'], 'reclaimed': False, 'error': str(exc)})
                continue
            with locked_state(repo) as (directory, latest):
                current = latest['tasks'].get(task, {}).get('owner_receipts', {}).get(key)
                if current != record:
                    raise LifecycleError('owner receipt changed concurrently')
                current['phase'] = 'completed'
                current.pop('failure', None)
                save_state(directory, latest)
            results.append({'generation': record['generation'], 'reclaimed': True})
    return {'task': task, 'results': results}
