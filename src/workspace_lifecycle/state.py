from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile

from .errors import LifecycleError
from .git import common_dir


def state_dir(repo) -> Path:
    return common_dir(repo) / "workspace-lifecycle"


def _atomic(path: Path, value: dict) -> None:
    fd, temporary = tempfile.mkstemp(prefix="state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            fd2 = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try: os.fsync(fd2)
            finally: os.close(fd2)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


@contextmanager
def locked_state(repo, create: bool = False):
    directory = state_dir(repo)
    legacy = common_dir(repo) / "agent-branches" / "state.json"
    if legacy.exists():
        raise LifecycleError("legacy agent-branches registry exists; migration is not authorized")
    if create: directory.mkdir(parents=True, exist_ok=True)
    if not directory.is_dir():
        raise LifecycleError("workspace lifecycle is not initialized")
    lock = directory / "lock"
    with lock.open("a+b") as stream:
        if os.name == "nt":
            import msvcrt
            if not stream.tell(): stream.write(b"0"); stream.flush()
            stream.seek(0)
            try: msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            except OSError as exc: raise LifecycleError("lifecycle state is busy; retry") from exc
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            path = directory / "state.json"
            state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"version": 1, "tasks": {}, "intents": {}}
            if state.get("version") != 1 or not isinstance(state.get("tasks"), dict):
                raise LifecycleError("unsupported workspace lifecycle state")
            yield directory, state
        finally:
            if os.name == "nt":
                stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else: fcntl.flock(stream, fcntl.LOCK_UN)


def save_state(directory: Path, state: dict) -> None:
    _atomic(directory / "state.json", state)
