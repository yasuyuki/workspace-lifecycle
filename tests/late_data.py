"""Ignored data another process writes after every lifecycle check, just
before Git changes the default checkout."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

from workspace_lifecycle import service

IGNORE = '*.log\n'
# Git's refusal of untracked data in the way of what it writes.
REFUSAL = 'would (be overwritten|be removed|lose untracked files)'

# Each shape: the tracked path Git brings into the checkout, and the ignored
# files the other process writes where that path, or one of its parents, goes.
SHAPES = {
    'file': ('late.log', ('late.log',)),
    'directory': ('dir.log', ('dir.log/inner.txt',)),
    'nested': ('nest/a/b.log', ('nest/a/b.log',)),
    'parent': ('top.log/child.txt', ('top.log',)),
    'case': ('Case.log', ('case.log',)),
}

WRITE = """
import json, os, sys
from pathlib import Path
root = Path(sys.argv[1])
for name in json.loads(sys.argv[2]):
    (root / name).parent.mkdir(parents=True, exist_ok=True)
    (root / name).write_bytes(b'written by another process: ' + name.encode() + b'\\n')
    os.chmod(root / name, 0o640)
"""


def folds_case():
    with tempfile.TemporaryDirectory() as directory:
        (Path(directory) / 'probe').write_text('')
        return (Path(directory) / 'PROBE').exists()


def state(root, names):
    """Path, SHA-256 and mode of each late file, or None once it is gone."""
    def one(path):
        if not path.is_file():
            return None
        return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mode
    return {name: one(Path(root) / name) for name in names}


def brings(root, revision, path):
    def has(commit):
        return subprocess.run(['git', '-C', str(root), 'cat-file', '-e', commit + ':' + path],
                              capture_output=True).returncode == 0
    return has(revision) and not has('HEAD')


def writing_before_checkout_change(root, path, names):
    """Patch the lifecycle's Git calls so that, right before the first merge,
    checkout or reset of ``root`` that would bring ``path``, another process
    writes ``names``; the returned list records that call."""
    native = service.git
    fired = []

    def call(repo, *args, **kwargs):
        # resolve() expands a Windows 8.3 temporary path to the worktree's long one.
        if (not fired and Path(repo).resolve() == Path(root).resolve() and args[:1] in (('merge',), ('checkout',), ('reset',))
                and brings(root, args[-1], path)):
            fired.append(args)
            subprocess.run([sys.executable, '-c', WRITE, str(root), json.dumps(list(names))], check=True)
        return native(repo, *args, **kwargs)

    return patch.object(service, 'git', side_effect=call), fired


def move_aside(root, names, destination):
    for name in names:
        top = name.split('/')[0]
        if (Path(root) / top).exists():
            (Path(root) / top).rename(Path(destination) / top)
