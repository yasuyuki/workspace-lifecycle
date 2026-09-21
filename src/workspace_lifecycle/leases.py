"""Explicit process-use leases, separate from Git's worktree administrative locks.

A crashed supervisor leaves a receipt. Absence of a PID or Windows job name is
not proof that all external users released the checkout.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import uuid


def _paths(repo, task):
    proc = subprocess.run(['git', '-C', str(repo), 'rev-parse', '--path-format=absolute',
                           '--git-common-dir'], capture_output=True, text=True, check=True)
    common = Path(proc.stdout.strip())
    if (common / 'agent-branches/state.json').exists():
        raise ValueError('legacy registry: explicit consumer migration is required')
    root = common / 'workspace-lifecycle' / 'leases'
    root.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha256(task.encode('utf-8')).hexdigest()
    return root / (name + '.lock'), root / (name + '.json')


@contextmanager
def _lock(path, blocking=False):
    with path.open('a+b') as stream:
        if os.name == 'nt':
            import msvcrt
            if not path.stat().st_size:
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise ValueError('task is in use by another lifecycle operation') from exc
        else:
            import fcntl
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError as exc:
                raise ValueError('task is in use by another lifecycle operation') from exc
        try:
            yield
        finally:
            if os.name == 'nt':
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def _save(path, value):
    # Windows readers otherwise deny the atomic destination replacement. Keep
    # receipt I/O separate from the use lease held for the entire process tree.
    with _lock(path.with_suffix('.io.lock'), blocking=True):
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
    with _lock(path.with_suffix('.io.lock'), blocking=True):
        try:
            with path.open(encoding='utf-8') as stream:
                return json.load(stream)
        except FileNotFoundError:
            return None


def _remove(path):
    with _lock(path.with_suffix('.io.lock'), blocking=True):
        path.unlink()


@contextmanager
def guard(repo, task, allow_use=False):
    """Serialize mutations, admitting only the owning supervised session when allowed."""
    lock, receipt = _paths(repo, task)
    with _lock(lock.with_suffix('.mutation.lock')):
        record = _read(receipt)
        own_use = (allow_use and record is not None
                   and os.environ.get('WORKSPACE_LIFECYCLE_USE') == record['token']
                   and _identity(record['owner_pid']) == record['owner_identity'])
        if own_use:
            yield
        else:
            with _lock(lock):
                if receipt.exists():
                    raise ValueError('unreleased task use; inspect lease and explicitly recover: ' + task)
                yield


def status(repo, task):
    lock, receipt = _paths(repo, task)
    record = _read(receipt)
    try:
        with _lock(lock):
            busy = False
    except ValueError:
        busy = True
    return {'busy': busy, 'receipt': record}


def is_in_use(repo, task):
    value = status(repo, task)
    return value['busy'] or value['receipt'] is not None


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
    except FileNotFoundError:
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
        except FileNotFoundError:
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
        except FileNotFoundError:
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


def run(repo, task, argv, cwd, before_spawn=None, child_env=None):
    """Dedicated supervisor entry, preserving signals and holding descendants.

    External detached users must still release explicitly before retirement.
    No kill-on-close job, process reap or implicit termination is used.
    """
    lock, receipt = _paths(repo, task)
    with _lock(lock):
        if receipt.exists():
            raise ValueError('unreleased task use; inspect lease before restarting')
        if before_spawn is not None:
            before_spawn()
        record = {'token': uuid.uuid4().hex, 'owner_pid': os.getpid(),
                  'owner_identity': _identity(os.getpid()), 'task': task}
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
                # Python 3.10 supports preexec_fn rather than process_group.
                _linux_subreaper()
                options['preexec_fn'] = os.setpgrp
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
    lock, receipt = _paths(repo, task)
    if not isinstance(evidence, str) or not evidence.strip():
        raise ValueError('external user release evidence is required')
    with _lock(lock):
        record = _read(receipt)
        if record is None:
            raise ValueError('lease receipt no longer exists')
        if record['token'] != token:
            raise ValueError('lease identity changed')
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
