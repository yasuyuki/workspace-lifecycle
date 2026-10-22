"""Explicit process-use leases, separate from Git's worktree administrative locks.

Several supervised uses of one task may run at once. Each holds a shared use
lock and its own receipt; mutations need the exclusive lock and no receipt
other than the caller's own. A crashed supervisor leaves a receipt. Absence of
a PID or Windows job name is not proof that all external users released the
checkout.
"""
from contextlib import ExitStack, contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import time
import uuid


class UseConflict(ValueError):
    """A refusal that names how the caller can resolve it."""

    def __init__(self, message, recovery):
        super().__init__(message)
        self.recovery = recovery


def _paths(repo, task):
    proc = subprocess.run(['git', '-C', str(repo), 'rev-parse', '--path-format=absolute',
                           '--git-common-dir'], capture_output=True, text=True, encoding="utf-8", check=True)
    common = Path(proc.stdout.strip())
    if (common / 'agent-branches/state.json').exists():
        raise ValueError('legacy registry: explicit consumer migration is required')
    root = common / 'workspace-lifecycle' / 'leases'
    root.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha256(task.encode('utf-8')).hexdigest()
    return root / (name + '.lock'), root / (name + '.json')


BUSY = 'task is in use by another lifecycle operation'


def _windows_range(stream, flags):
    import ctypes
    from ctypes import wintypes
    import msvcrt
    class Overlapped(ctypes.Structure):
        _fields_ = [('internal', ctypes.c_size_t), ('internal_high', ctypes.c_size_t),
                    ('offset', wintypes.DWORD), ('offset_high', wintypes.DWORD),
                    ('event', wintypes.HANDLE)]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    pointer = ctypes.POINTER(Overlapped)
    kernel.LockFileEx.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.DWORD, wintypes.DWORD, pointer)
    kernel.UnlockFileEx.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                    wintypes.DWORD, pointer)
    kernel.LockFileEx.restype = kernel.UnlockFileEx.restype = wintypes.BOOL
    handle = wintypes.HANDLE(msvcrt.get_osfhandle(stream.fileno()))
    overlapped = Overlapped()
    # Byte 0, as msvcrt.locking in earlier releases, so both exclude each other.
    if flags is None:
        done = kernel.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlapped))
    else:
        done = kernel.LockFileEx(handle, flags, 0, 1, 0, ctypes.byref(overlapped))
    if not done:
        raise OSError(ctypes.get_last_error(), 'byte-range lock failed')


@contextmanager
def _lock(path, blocking=False, shared=False, message=BUSY, recovery=None):
    with path.open('a+b') as stream:
        if os.name == 'nt':
            if not path.stat().st_size:
                stream.write(b'0')
                stream.flush()
            try:
                _windows_range(stream, (0 if shared else 2) | (0 if blocking else 1))
            except OSError as exc:
                raise UseConflict(message, recovery) from exc
        else:
            import fcntl
            try:
                fcntl.flock(stream, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
                            | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError as exc:
                raise UseConflict(message, recovery) from exc
        try:
            yield
        finally:
            if os.name == 'nt':
                _windows_range(stream, None)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def _io_lock(path):
    return path.parent / (path.name.split('.', 1)[0] + '.io.lock')


def _receipts(lock):
    """Every use receipt of one task, including the single receipt of 0.4 releases."""
    prefix = lock.name[:-len('.lock')]
    pattern = re.compile(re.escape(prefix) + r'(\.[0-9a-f]{32})?\.json')
    return sorted(path for path in lock.parent.iterdir() if pattern.fullmatch(path.name))


def _uses(lock):
    uses = []
    for path in _receipts(lock):
        record = _read(path)
        if record is not None:
            uses.append(record)
    return uses


def _save(path, value):
    # Windows readers otherwise deny the atomic destination replacement. Keep
    # receipt I/O separate from the use lease held for the entire process tree.
    with _lock(_io_lock(path), blocking=True):
        fd, temporary = tempfile.mkstemp(dir=path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(value, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def _read(path):
    with _lock(_io_lock(path), blocking=True):
        try:
            with path.open(encoding='utf-8') as stream:
                return json.load(stream)
        except FileNotFoundError:
            return None


def _remove(path):
    with _lock(_io_lock(path), blocking=True):
        path.unlink()


def _mutation(lock):
    """Hold the mutation lock; registering a use takes the same short gate first."""
    stack = ExitStack()
    with _lock(lock.with_suffix('.register.lock'), blocking=True):
        stack.enter_context(_lock(lock.with_suffix('.mutation.lock'),
                                  recovery='retry after the other lifecycle operation completes'))
    return stack


def _users_recovery(task):
    return ('wait for the other sessions to exit; `lease-status --task %s` lists them. '
            'After confirming a listed supervisor and its users are gone, run '
            '`lease-release --task %s --token TOKEN --evidence REF`' % (task, task))


@contextmanager
def guard(repo, task, allow_use=False):
    """Serialize mutations, admitting only a sole owning supervised session when allowed."""
    lock, _ = _paths(repo, task)
    with _mutation(lock):
        uses = _uses(lock)
        if allow_use and len(uses) == 1 and _own_use(uses[0], task):
            yield
            return
        with _lock(lock, message='task is in use by another session: ' + task,
                   recovery=_users_recovery(task)):
            if uses:
                raise UseConflict('unreleased task use; inspect lease and explicitly recover: ' + task,
                                  _users_recovery(task))
            yield


def _alive(record):
    try:
        return _identity(record['owner_pid']) == record['owner_identity']
    except (KeyError, TypeError, ValueError):
        return True  # Uncertainty is not proof of release.


def status(repo, task):
    lock, _ = _paths(repo, task)
    uses = _uses(lock)
    return {'busy': any(_alive(record) for record in uses), 'uses': uses}


def is_in_use(repo, task):
    return bool(status(repo, task)['uses'])


def _identity(pid):
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
        kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            if ctypes.get_last_error() == 87:
                return None
            raise ValueError('cannot inspect lease process')
        try:
            exit_code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                raise ValueError('cannot inspect process exit state')
            if exit_code.value != 259:  # STILL_ACTIVE
                return None
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                raise ValueError('cannot inspect process creation identity')
            return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
        finally:
            kernel.CloseHandle(handle)
    try:
        fields = Path('/proc', str(pid), 'stat').read_text().rsplit(')', 1)[1].split()
        if fields[0] == 'Z':
            return None
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip() + ':' + fields[19]
    except (FileNotFoundError, ProcessLookupError):
        # A process that exits between opening and reading its stat raises ESRCH.
        if Path('/proc').is_dir():
            return None
        raise ValueError('process creation identity is unavailable on this OS')


def _group_active(pgid):
    # Linux zombie members cannot use a checkout and can outlive their reaper.
    # Inspect every member; errors are uncertainty, not proof of release.
    for directory in Path('/proc').iterdir():
        if not directory.name.isdecimal():
            continue
        try:
            fields = (directory / 'stat').read_text().rsplit(')', 1)[1].split()
        except (FileNotFoundError, ProcessLookupError):
            continue
        if int(fields[2]) == pgid and fields[0] != 'Z':
            return True
    return False


def _linux_subreaper():
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0):  # PR_SET_CHILD_SUBREAPER, Linux native
        raise ValueError('cannot establish Linux descendant supervision')


def _linux_descendants():
    # This dedicated supervisor is a subreaper: double-fork / setsid children
    # are reparented here, so changing process groups does not release the lease.
    parents = {}
    live = set()
    for directory in Path('/proc').iterdir():
        if not directory.name.isdecimal():
            continue
        try:
            fields = (directory / 'stat').read_text().rsplit(')', 1)[1].split()
        except (FileNotFoundError, ProcessLookupError):
            continue
        pid = int(directory.name)
        parents[pid] = int(fields[1])
        if fields[0] != 'Z':
            live.add(pid)
    descendants = {os.getpid()}
    while True:
        expanded = descendants | {pid for pid, parent in parents.items() if parent in descendants}
        if expanded == descendants:
            break
        descendants = expanded
    # Reap exited adopted children; never terminate a live process.
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            break
        if not pid:
            break
    return bool((descendants - {os.getpid()}) & live)


def _windows_active(name):
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenJobObjectW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    kernel.OpenJobObjectW.restype = wintypes.HANDLE
    kernel.QueryInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int,
                                                ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p)
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.OpenJobObjectW(4, False, name)
    if not handle:
        if ctypes.get_last_error() == 2:
            return None  # Lost last handle is uncertainty, not completion.
        raise ValueError('cannot inspect Windows lease job')
    class Accounting(ctypes.Structure):
        _fields_ = [('times', ctypes.c_int64 * 4), ('faults', wintypes.DWORD),
                    ('total', wintypes.DWORD), ('active', wintypes.DWORD), ('terminated', wintypes.DWORD)]
    try:
        info = Accounting()
        if not kernel.QueryInformationJobObject(handle, 1, ctypes.byref(info), ctypes.sizeof(info), None):
            raise ValueError('cannot inspect Windows lease descendants')
        return info.active != 0
    finally:
        kernel.CloseHandle(handle)


def _windows_job(token):
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel.QueryInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int,
                                                ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p)
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    name = 'Local\\workspace-lifecycle-' + token
    handle = kernel.CreateJobObjectW(None, name)
    if not handle or ctypes.get_last_error() == 183:
        raise ValueError('cannot create unique use job')
    # Run is a dedicated CLI process. Joining before spawn closes the attach race;
    # nested jobs work on supported Windows, and all children inherit membership.
    if not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess()):
        kernel.CloseHandle(handle)
        raise ValueError('cannot establish Windows process-tree lease')
    class Accounting(ctypes.Structure):
        _fields_ = [('times', ctypes.c_int64 * 4), ('faults', wintypes.DWORD),
                    ('total', wintypes.DWORD), ('active', wintypes.DWORD), ('terminated', wintypes.DWORD)]
    def active():
        info = Accounting()
        if not kernel.QueryInformationJobObject(handle, 1, ctypes.byref(info), ctypes.sizeof(info), None):
            raise ValueError('cannot inspect Windows process-tree lease')
        return info.active > 1
    return name, active, lambda: kernel.CloseHandle(handle)


def _windows_job_member(name):
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenJobObjectW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    kernel.OpenJobObjectW.restype = wintypes.HANDLE
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.IsProcessInJob.argtypes = (wintypes.HANDLE, wintypes.HANDLE,
                                     ctypes.POINTER(wintypes.BOOL))
    kernel.IsProcessInJob.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.OpenJobObjectW(4, False, name)  # JOB_OBJECT_QUERY
    if not handle:
        return False  # A missing or inaccessible Job cannot prove membership.
    try:
        member = wintypes.BOOL()
        if not kernel.IsProcessInJob(kernel.GetCurrentProcess(), handle,
                                     ctypes.byref(member)):
            raise ValueError('cannot inspect Windows use job membership')
        return bool(member.value)
    finally:
        kernel.CloseHandle(handle)


def _own_use(record, task):
    if not isinstance(record, dict) or record.get('task') != task:
        return False
    token = record.get('token')
    owner = record.get('owner_identity')
    owner_pid = record.get('owner_pid')
    if (not isinstance(token, str) or not token
            or os.environ.get('WORKSPACE_LIFECYCLE_USE') != token
            or not isinstance(owner, str) or not owner
            or not isinstance(owner_pid, int) or owner_pid <= 0
            or _identity(owner_pid) != owner):
        return False
    if os.name == 'nt':
        name = 'Local\\workspace-lifecycle-' + token
        return record.get('windows_job') == name and _windows_job_member(name)
    return True


def _joined_run(argv, cwd, token, child_env):
    # The caller is already in the owner's Job. Its child inherits that Job;
    # only the outer supervisor owns the receipt and waits for all descendants.
    env = {**os.environ, **(child_env or {}), 'WORKSPACE_LIFECYCLE_USE': token}
    child = subprocess.Popen(argv, cwd=str(cwd), env=env)
    while True:
        try:
            return child.wait()
        except KeyboardInterrupt:
            # Console input also reaches the child. Keep the joiner alive until
            # that child reports its own exit status.
            continue


def run(repo, task, argv, cwd, before_spawn=None, child_env=None):
    """Dedicated supervisor entry, preserving signals and holding descendants.

    External detached users must still release explicitly before retirement.
    No kill-on-close job, process reap or implicit termination is used.
    """
    lock, _ = _paths(repo, task)
    if os.name == 'nt':
        for record in _uses(lock):
            if _own_use(record, task):
                if before_spawn is not None:
                    before_spawn()
                return _joined_run(argv, cwd, record['token'], child_env)
    with ExitStack() as use:
        with _lock(lock.with_suffix('.register.lock'), blocking=True):
            with _lock(lock.with_suffix('.mutation.lock'),
                       message='task is being changed by another lifecycle operation: ' + task,
                       recovery='retry after that operation, such as finish or retire, completes'):
                pass
            use.enter_context(_lock(lock, shared=True,
                                    message='task is in use by an older workspace-lifecycle supervisor: ' + task,
                                    recovery='wait for that session to exit, or start every session of this task with this version'))
            if before_spawn is not None:
                before_spawn()
            record = {'token': uuid.uuid4().hex, 'owner_pid': os.getpid(),
                      'owner_identity': _identity(os.getpid()), 'task': task}
            receipt = lock.with_name(lock.name[:-len('.lock')] + '.' + record['token'] + '.json')
            _save(receipt, record)  # crash between intent and spawn stays unresolved
        child = None
        previous = {}
        close_job = None
        terminal = None
        foreground = None
        complete = False
        try:
            options = {'cwd': str(cwd), 'env': {**os.environ, **(child_env or {}),
                                                'WORKSPACE_LIFECYCLE_USE': record['token']}}
            if os.name == 'nt':
                job, descendants, close_job = _windows_job(record['token'])
                record['windows_job'] = job
                _save(receipt, record)
                options['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP
                def forward_console(_number, _frame):
                    if child is not None and child.poll() is None:
                        try:
                            child.send_signal(signal.CTRL_BREAK_EVENT)
                        except OSError:
                            if child.poll() is None:
                                raise
                for number in (signal.SIGINT, signal.SIGBREAK):
                    previous[number] = signal.signal(number, forward_console)
            else:
                _linux_subreaper()
                options['process_group'] = 0
                def forward(number, _frame):
                    if child is not None:
                        try:
                            os.killpg(child.pid, number)
                        except ProcessLookupError:
                            pass
                for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT):
                    previous[number] = signal.signal(number, forward)
                previous[signal.SIGTTOU] = signal.signal(signal.SIGTTOU, signal.SIG_IGN)
                try:
                    terminal = os.open('/dev/tty', os.O_RDWR)
                    foreground = os.tcgetpgrp(terminal)
                except OSError:
                    if terminal is not None:
                        os.close(terminal)
                    terminal = None
            child = subprocess.Popen(argv, **options)
            record.update(child_pid=child.pid, child_identity=_identity(child.pid))
            if os.name != 'nt':
                record['process_group'] = child.pid
            _save(receipt, record)
            if terminal is not None:
                os.tcsetpgrp(terminal, child.pid)
                os.killpg(child.pid, signal.SIGCONT)
            while True:
                try:
                    # Windows SIGBREAK does not interrupt an infinite process
                    # wait. Return to Python at the existing descendant-check
                    # cadence so its handler can forward the console event.
                    code = child.wait(timeout=0.1 if os.name == 'nt' else None)
                    break
                except subprocess.TimeoutExpired:
                    continue
                except KeyboardInterrupt:
                    if os.name == 'nt':
                        forward_console(signal.SIGINT, None)
            while descendants() if os.name == 'nt' else _linux_descendants():
                time.sleep(0.1)
            complete = True
            return code
        finally:
            if terminal is not None:
                try:
                    os.tcsetpgrp(terminal, foreground)
                finally:
                    os.close(terminal)
            for number, handler in previous.items():
                signal.signal(number, handler)
            if complete:
                _remove(receipt)
            if close_job is not None:
                close_job()


def release(repo, task, token, evidence):
    """Recover exactly one dead-owner lease after explicit external-user review."""
    lock, _ = _paths(repo, task)
    if not isinstance(evidence, str) or not evidence.strip():
        raise ValueError('external user release evidence is required')
    # Other live uses keep the shared lock; owner identities, not the lock,
    # prove that this receipt's users are gone.
    with _mutation(lock):
        matches = [(path, record) for path in _receipts(lock)
                   for record in [_read(path)] if record is not None and record.get('token') == token]
        if not matches:
            raise ValueError('lease receipt no longer exists')
        receipt, record = matches[0]
        if _identity(record['owner_pid']) == record['owner_identity']:
            raise ValueError('lease supervisor is still alive')
        if record.get('child_identity') is not None and _identity(record['child_pid']) == record['child_identity']:
            raise ValueError('lease child is still alive')
        if record.get('process_group') and _group_active(record['process_group']):
            raise ValueError('lease process group is still alive')
        if record.get('windows_job') and _windows_active(record['windows_job']):
            raise ValueError('lease Windows job still has active descendants')
        # A lost Windows job name / pre-attachment crash requires the caller's
        # explicit evidence. Never infer release from inability to open a job.
        record['release_evidence'] = evidence
        _save(receipt.with_suffix('.released.json'), record)
        _remove(receipt)
