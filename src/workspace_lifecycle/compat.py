"""Read-only registration adapter for the pre-runtime-split inventory caller."""
from contextlib import contextmanager
import subprocess

from .errors import LifecycleError as BranchError
from .git import git, top, common_dir as common, head as oid, bound_task, current_branch, task_worktree
from .leases import guard
from .state import locked_state


def git_bytes(repo, *args, optional=False):
    result = subprocess.run(['git', '-C', str(repo), *args], capture_output=True)
    if result.returncode:
        if optional:
            return None
        raise BranchError(result.stderr.decode('utf-8', 'replace'))
    return result.stdout


@contextmanager
def registered_checkout(repo):
    task = bound_task(repo)
    with guard(repo, task):
        with locked_state(repo) as (_, state):
            if task not in state['tasks']:
                raise BranchError('unregistered task')
            if state['tasks'][task].get('retire'):
                raise BranchError('task has a retirement request')
            if task_worktree(repo, current_branch(repo)) != top(repo):
                raise BranchError('not the unique task worktree')
            yield {'task': task, 'tip': oid(repo), 'branch': current_branch(repo)}
