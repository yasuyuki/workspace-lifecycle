from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import os
from pathlib import Path
import subprocess

from .errors import LifecycleError


def git(repo: Path | str, *args: str, optional: bool = False, env=None) -> str | None:
    process = subprocess.run(["git", "-C", str(repo), *args], text=True, encoding="utf-8",
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, env={**(os.environ if env is None else env), "GIT_LITERAL_PATHSPECS": "1"})
    if process.returncode:
        if optional:
            return None
        raise LifecycleError(process.stderr.strip() or "git failed: " + " ".join(args))
    return process.stdout.strip()


_common_dirs: ContextVar[dict[str, Path] | None] = ContextVar("common_dirs", default=None)


@contextmanager
def operation():
    """Scope of one lifecycle operation, which resolves each checkout's common directory once.

    The state and lease locks live in the common directory, so the operation
    stays bound to the repository it started in.
    """
    if _common_dirs.get() is not None:
        yield
        return
    token = _common_dirs.set({})
    try:
        yield
    finally:
        _common_dirs.reset(token)


def common_dir(repo: Path | str) -> Path:
    known = _common_dirs.get()
    key = os.path.abspath(repo)
    if known is not None and key in known:
        return known[key]
    value = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
    if known is not None:
        known[key] = value
    return value


def top(repo: Path | str) -> Path:
    return Path(git(repo, "rev-parse", "--show-toplevel")).resolve()


def current_branch(repo: Path | str) -> str:
    name = git(repo, "symbolic-ref", "--quiet", "--short", "HEAD", optional=True)
    if not name:
        raise LifecycleError("detached HEAD cannot be a lifecycle workspace")
    return name


def head(repo: Path | str, ref: str = "HEAD") -> str:
    value = git(repo, "rev-parse", "--verify", ref + "^{commit}", optional=True)
    if not value:
        raise LifecycleError("missing commit: " + ref)
    return value


def remote_default(repo: Path | str, remote: str) -> str:
    remotes = (git(repo, "remote") or "").splitlines()
    if remote.startswith("-") or remote not in remotes:
        raise LifecycleError("choose an existing remote explicitly")
    output = git(repo, "ls-remote", "--symref", remote, "HEAD") or ""
    refs = [row.split()[1] for row in output.splitlines() if row.startswith("ref: refs/heads/")]
    if len(refs) != 1:
        raise LifecycleError("remote default branch could not be verified")
    return refs[0].removeprefix("refs/heads/")


def worktree_records(repo: Path | str) -> list[dict[str, str]]:
    output = subprocess.run(["git", "-C", str(repo), "worktree", "list", "--porcelain", "-z"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout
    result = []
    for block in output.split(b"\0\0"):
        item = {}
        for field in block.split(b"\0"):
            if field:
                key, _, value = os.fsdecode(field).partition(" ")
                item[key] = value
        if "worktree" in item:
            item["worktree"] = str(Path(item["worktree"]).absolute())
            result.append(item)
    return result


def checkout_state(repo: Path | str) -> tuple[Path, Path, str, str]:
    """Toplevel, Git dir, HEAD commit and HEAD's full ref name ("HEAD" when detached), read in one call."""
    rows = git(repo, "rev-parse", "--show-toplevel", "--absolute-git-dir", "HEAD^{commit}",
               "--symbolic-full-name", "HEAD").splitlines()
    if len(rows) != 4:
        raise LifecycleError("checkout state could not be read")
    return Path(rows[0]).resolve(), Path(rows[1]).resolve(), rows[2], rows[3]


def task_config_key(branch: str) -> str:
    return "branch." + branch + ".workspaceTask"


def task_bindings(repo: Path | str) -> list[tuple[str, str]]:
    """Every (branch, task) binding in the local configuration, in file order."""
    records = (git(repo, "config", "--local", "--get-regexp", r"^branch\..*\.workspaceTask$", optional=True) or "").splitlines()
    bindings = []
    for record in records:
        key, _, value = record.partition(" ")
        if key.startswith("branch.") and key.endswith(".workspacetask"):
            bindings.append((key[len("branch."):-len(".workspacetask")], value))
    return bindings


def bound_task(repo: Path | str, branch: str | None = None, bindings: list[tuple[str, str]] | None = None) -> str:
    branch = branch or current_branch(repo)
    if bindings is None:
        task = git(repo, "config", "--local", "--get", task_config_key(branch), optional=True)
    else:
        task = ([value for name, value in bindings if name == branch] or [None])[-1]
    if not task:
        raise LifecycleError("current branch has no workspace lifecycle task binding")
    return task


def branch_for_task(repo: Path | str, task: str, bindings: list[tuple[str, str]] | None = None) -> str:
    matches = [branch for branch, value in (task_bindings(repo) if bindings is None else bindings) if value == task]
    if len(matches) != 1:
        raise LifecycleError("task does not have exactly one Git branch binding: " + task)
    return matches[0]


def task_worktree(repo: Path | str, branch: str, records: list[dict[str, str]] | None = None) -> Path:
    needle = "refs/heads/" + branch
    matches = [Path(row["worktree"]).resolve() for row in (worktree_records(repo) if records is None else records)
               if row.get("branch") == needle]
    if len(matches) != 1:
        raise LifecycleError("branch does not have exactly one live Git worktree: " + branch)
    return matches[0]
