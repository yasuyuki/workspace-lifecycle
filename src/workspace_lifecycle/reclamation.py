"""Crash-resumable removal of an already released private directory tree.

The caller must hold the task lease, must have obtained cooperative release
from every owner/user, and exclusively owns the generated quarantine namespace.
Quarantine and hashing do not make arbitrary external writers safe; in
particular Linux has no handle-relative unlink primitive which can protect a
quarantine name from a noncooperating writer.  Ordinary original-path
replacements are outside the renamed private tree and are retained.  This
module fails closed on identities, links, mounts, hard links, unexpected names,
and unsupported operating systems.

``persist`` must durably save the supplied ``progress`` object (normally with a
state-generation CAS).  It is called before every rename/unlink/rmdir.  The
immutable retirement manifest is input only and is never rewritten.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat

from .errors import LifecycleError


_ROOT = "@root"
_PREFIX = ".workspace-lifecycle-delete-"


def _checkpoint(event: str, path: str) -> None:
    """Private test hook; production intentionally does nothing."""


def _digest_fd(fd: int) -> str:
    digest = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        block = os.read(fd, 1024 * 1024)
        if not block:
            return digest.hexdigest()
        digest.update(block)


def _identity(info: os.stat_result) -> list[int]:
    return [info.st_dev, info.st_ino]


def _manifest_id(manifest) -> str:
    encoded = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _rows(manifest) -> dict[str, tuple[str, str]]:
    result = {}
    if not isinstance(manifest, list):
        raise LifecycleError("reclaim manifest is not a list")
    for row in manifest:
        if not isinstance(row, list) or len(row) != 3 or row[1] not in ("file", "dir"):
            raise LifecycleError("reclaim manifest contains an invalid row")
        name, kind, digest = row
        path = PurePosixPath(name)
        if (not isinstance(name, str) or not name or path.is_absolute() or ".." in path.parts
                or path.as_posix() != name or name in result or "\0" in name):
            raise LifecycleError("reclaim manifest contains an unsafe path")
        if kind == "file" and (not isinstance(digest, str) or len(digest) != 64):
            raise LifecycleError("reclaim manifest contains an invalid digest")
        if kind == "dir" and digest != "":
            raise LifecycleError("reclaim directory manifest has a digest")
        result[name] = (kind, digest)
    return result


def _mount_id(fd: int) -> str | None:
    """Linux mount ID for an open object; None means the kernel view is absent."""
    try:
        for line in Path("/proc/self/fdinfo", str(fd)).read_text().splitlines():
            if line.startswith("mnt_id:"):
                return line.split(":", 1)[1].strip()
    except (FileNotFoundError, PermissionError, OSError):
        return None
    return None


def _mount_session() -> list:
    """Identify the Linux boot and mount namespace in which mount IDs apply."""
    try:
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        namespace = os.stat("/proc/self/ns/mnt", follow_symlinks=True)
    except (FileNotFoundError, PermissionError, OSError) as exc:
        raise LifecycleError("reclaim cannot identify Linux mount session") from exc
    return [boot, namespace.st_dev, namespace.st_ino]


def _open_dir(name, *, dir_fd=None) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    return os.open(name, flags, dir_fd=dir_fd)


def _open_file(name, *, dir_fd) -> int:
    return os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=dir_fd)


def _validate_root(fd: int, root_identity) -> tuple[os.stat_result, str | None]:
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode) or _identity(info) != list(root_identity):
        raise LifecycleError("reclaim root identity changed")
    return info, _mount_id(fd)


def _scan_linux(root: Path, manifest, root_identity) -> dict:
    expected = _rows(manifest)
    root_fd = _open_dir(root)
    try:
        root_info, root_mount = _validate_root(root_fd, root_identity)
        if root_mount is None:
            raise LifecycleError("reclaim cannot prove Linux mount identity")
        members = {}

        def walk(fd: int, prefix: str) -> None:
            actual = set(os.listdir(fd))
            wanted = {PurePosixPath(name).parts[len(PurePosixPath(prefix).parts)]
                      for name in expected
                      if (not prefix or name.startswith(prefix + "/"))
                      and len(PurePosixPath(name).parts) > len(PurePosixPath(prefix).parts)}
            if actual != wanted:
                raise LifecycleError("reclaim tree contains unknown or missing members")
            for name in sorted(actual):
                logical = name if not prefix else prefix + "/" + name
                kind_digest = expected.get(logical)
                has_children = any(item.startswith(logical + "/") for item in expected)
                if kind_digest is None and not has_children:
                    raise LifecycleError("reclaim tree contains an unknown member")
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    raise LifecycleError("reclaim refuses a link")
                if name == ".git" and logical != ".git":
                    raise LifecycleError("reclaim refuses a nested repository")
                if stat.S_ISDIR(info.st_mode):
                    if kind_digest != ("dir", ""):
                        raise LifecycleError("reclaim member kind changed")
                    child_fd = _open_dir(name, dir_fd=fd)
                    try:
                        child_mount = _mount_id(child_fd)
                        if root_mount is not None and child_mount != root_mount:
                            raise LifecycleError("reclaim refuses a mount crossing")
                        members[logical] = {"kind": "dir", "identity": _identity(os.fstat(child_fd)),
                                            "mount_id": child_mount}
                        walk(child_fd, logical)
                    finally:
                        os.close(child_fd)
                elif stat.S_ISREG(info.st_mode):
                    if kind_digest is None or kind_digest[0] != "file" or has_children:
                        raise LifecycleError("reclaim member kind changed")
                    child_fd = _open_file(name, dir_fd=fd)
                    try:
                        opened = os.fstat(child_fd)
                        if opened.st_nlink != 1:
                            raise LifecycleError("reclaim refuses a hard-linked file")
                        if _identity(opened) != _identity(info):
                            raise LifecycleError("reclaim member changed while opening")
                        child_mount = _mount_id(child_fd)
                        if root_mount is not None and child_mount != root_mount:
                            raise LifecycleError("reclaim refuses a mount crossing")
                        digest = _digest_fd(child_fd)
                        if digest != kind_digest[1]:
                            raise LifecycleError("reclaim file digest changed")
                        members[logical] = {"kind": "file", "identity": _identity(opened),
                                            "digest": digest, "mount_id": child_mount}
                    finally:
                        os.close(child_fd)
                else:
                    raise LifecycleError("reclaim refuses a special file")
        walk(root_fd, "")
        return {"manifest_id": _manifest_id(manifest), "root_identity": list(root_identity),
                "mount_session": _mount_session(), "root_mount_id": root_mount,
                "members": members}
    finally:
        os.close(root_fd)


def _win_api():
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                   wintypes.HANDLE)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.ReadFile.argtypes = (wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p)
    kernel.SetFilePointerEx.argtypes = (wintypes.HANDLE, ctypes.c_int64,
                                       ctypes.POINTER(ctypes.c_int64), wintypes.DWORD)
    kernel.GetFileInformationByHandle.argtypes = (wintypes.HANDLE, ctypes.c_void_p)
    kernel.GetFileInformationByHandleEx.argtypes = (wintypes.HANDLE, ctypes.c_int,
                                                    ctypes.c_void_p, wintypes.DWORD)
    kernel.SetFileInformationByHandle.argtypes = (wintypes.HANDLE, ctypes.c_int,
                                                  ctypes.c_void_p, wintypes.DWORD)
    return ctypes, wintypes, kernel


def _win_open(path: Path):
    ctypes, wintypes, kernel = _win_api()
    # READ_CONTROL is unnecessary.  DELETE plus GENERIC_READ, with only read
    # sharing, rejects an existing writer/deleter and prevents a new one while
    # this exact object is checked and marked for deletion.
    handle = kernel.CreateFileW(str(path), 0x80000000 | 0x00010000, 0x1, None, 3,
                                0x00200000 | 0x02000000, None)
    if handle == wintypes.HANDLE(-1).value:
        error = ctypes.get_last_error()
        if error == 32:
            raise LifecycleError("reclaim object is shared by another user")
        raise OSError(error, "CreateFileW failed", str(path))
    return handle


def _win_info(handle) -> dict:
    ctypes, wintypes, kernel = _win_api()
    class Info(ctypes.Structure):
        _fields_ = [("attributes", wintypes.DWORD), ("creation", wintypes.FILETIME),
                    ("access", wintypes.FILETIME), ("write", wintypes.FILETIME),
                    ("volume", wintypes.DWORD), ("size_high", wintypes.DWORD),
                    ("size_low", wintypes.DWORD), ("links", wintypes.DWORD),
                    ("index_high", wintypes.DWORD), ("index_low", wintypes.DWORD)]
    value = Info()
    if not kernel.GetFileInformationByHandle(handle, ctypes.byref(value)):
        raise ctypes.WinError(ctypes.get_last_error())
    class FileIdInfo(ctypes.Structure):
        _fields_ = [("volume", ctypes.c_uint64), ("file_id", ctypes.c_ubyte * 16)]
    file_id = FileIdInfo()
    if not kernel.GetFileInformationByHandleEx(handle, 18, ctypes.byref(file_id),
                                               ctypes.sizeof(file_id)):  # FileIdInfo
        raise ctypes.WinError(ctypes.get_last_error())
    if value.attributes & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT
        raise LifecycleError("reclaim refuses a link, junction, or mount reparse point")
    return {"identity": [int(file_id.volume), bytes(file_id.file_id).hex()],
            "kind": "dir" if value.attributes & 0x10 else "file",
            "links": int(value.links)}


def _win_hash(handle) -> str:
    ctypes, wintypes, kernel = _win_api()
    if not kernel.SetFilePointerEx(handle, 0, None, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    digest = hashlib.sha256()
    buffer = ctypes.create_string_buffer(1024 * 1024)
    while True:
        count = wintypes.DWORD()
        if not kernel.ReadFile(handle, buffer, len(buffer), ctypes.byref(count), None):
            raise ctypes.WinError(ctypes.get_last_error())
        if not count.value:
            return digest.hexdigest()
        digest.update(buffer.raw[:count.value])


def _win_close(handle) -> None:
    _win_api()[2].CloseHandle(handle)


def _win_delete(handle) -> None:
    ctypes, _wintypes, kernel = _win_api()
    # FILE_DISPOSITION_INFO contains BOOLEAN (one byte), not Win32 BOOL.
    value = ctypes.c_ubyte(1)
    if not kernel.SetFileInformationByHandle(handle, 4, ctypes.byref(value), ctypes.sizeof(value)):
        error = ctypes.get_last_error()
        if error == 32:
            raise LifecycleError("reclaim object is shared by another user")
        raise OSError(error, "SetFileInformationByHandle(FileDispositionInfo) failed")


def _scan_windows(root: Path, manifest, root_identity) -> dict:
    expected = _rows(manifest)
    root_handle = _win_open(root)
    try:
        root_native = _win_info(root_handle)
        if root_native["kind"] != "dir":
            raise LifecycleError("reclaim root is not a directory")
        # The service identity is Python's stable volume/file identity.  Keep it
        # in the public snapshot and additionally bind the native handle ID.
        if list(root_identity) != _identity(root.stat(follow_symlinks=False)):
            raise LifecycleError("reclaim root identity changed")
        members = {}
        seen = set()
        for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
            relative = Path(current).relative_to(root)
            for name in [*dirs, *files]:
                path = Path(current) / name
                logical = (relative / name).as_posix()
                if logical not in expected:
                    raise LifecycleError("reclaim tree contains an unknown member")
                handle = _win_open(path)
                try:
                    info = _win_info(handle)
                    kind, digest = expected[logical]
                    if info["identity"][0] != root_native["identity"][0]:
                        raise LifecycleError("reclaim refuses a volume crossing")
                    if info["kind"] != kind:
                        raise LifecycleError("reclaim member kind changed")
                    if kind == "file":
                        if info["links"] != 1:
                            raise LifecycleError("reclaim refuses a hard-linked file")
                        actual = _win_hash(handle)
                        if actual != digest:
                            raise LifecycleError("reclaim file digest changed")
                        info["digest"] = actual
                    members[logical] = info
                    seen.add(logical)
                finally:
                    _win_close(handle)
        if seen != set(expected):
            raise LifecycleError("reclaim tree contains unknown or missing members")
        return {"manifest_id": _manifest_id(manifest), "root_identity": list(root_identity),
                "root_native_identity": root_native["identity"], "members": members}
    finally:
        _win_close(root_handle)


def capture(root, manifest, root_identity) -> dict:
    """Capture identities which strengthen, but never modify, ``manifest``."""
    if os.name == "nt":
        return _scan_windows(Path(root), manifest, root_identity)
    if os.name == "posix" and Path("/proc/self/fdinfo").is_dir():
        return _scan_linux(Path(root), manifest, root_identity)
    raise LifecycleError("safe reclamation capture is unsupported on this platform")


def _tombstone(logical: str) -> str:
    return _PREFIX + hashlib.sha256(logical.encode("utf-8")).hexdigest()


def _root_tombstone(root: Path, root_identity) -> Path:
    token = f"{_ROOT}\0{root_identity[0]}\0{root_identity[1]}"
    return root.with_name(_tombstone(token))


def _fsync_dir(fd: int) -> None:
    os.fsync(fd)


def _validate_resuming_names(root: Path, members: dict) -> None:
    """Reject unknown names while permitting only journal-explained absence."""
    actual = set()
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        relative = Path(current).relative_to(root)
        for name in [*dirs, *files]:
            actual.add((relative / name).as_posix())
    allowed = set()
    for logical, member in members.items():
        parent = PurePosixPath(logical).parent
        tomb = PurePosixPath(member["tombstone"])
        tomb_logical = tomb.as_posix() if str(parent) == "." else (parent / tomb).as_posix()
        choices = actual & {logical, tomb_logical}
        if len(choices) > 1:
            raise LifecycleError("reclaim member and quarantine both exist")
        if not choices and not member.get("intent"):
            raise LifecycleError("reclaim member disappeared without intent")
        if member.get("status") == "removed" and choices:
            raise LifecycleError("reclaim removed member reappeared")
        allowed.update(choices)
    if actual != allowed:
        raise LifecycleError("reclaim tree contains an unknown member")


def _resolve_root(root: Path, root_identity, root_state: dict) -> tuple[Path, Path]:
    tomb = _root_tombstone(root, root_identity)
    source_exists = os.path.lexists(root)
    tomb_exists = os.path.lexists(tomb)
    if source_exists and tomb_exists:
        raise LifecycleError("reclaim root and quarantine both exist")
    if not source_exists and not tomb_exists:
        if root_state.get("intent"):
            return root, tomb
        raise LifecycleError("reclaim root disappeared without intent")
    selected = tomb if tomb_exists else root
    fd = _open_dir(selected)
    try:
        _validate_root(fd, root_identity)
    finally:
        os.close(fd)
    return root, tomb


def _windows_live_names(root: Path) -> set[str]:
    names = set()
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        relative = Path(current).relative_to(root)
        for name in [*dirs, *files]:
            names.add((relative / name).as_posix())
    return names


def _win_parent_guards(root: Path, logical: str, progress: dict) -> list:
    handles = []
    current = root
    try:
        for depth, component in enumerate(PurePosixPath(logical).parts[:-1], 1):
            current /= component
            handle = _win_open(current)
            handles.append(handle)
            captured = progress["members"].get("/".join(PurePosixPath(logical).parts[:depth]))
            info = _win_info(handle)
            if (not captured or info["kind"] != "dir"
                    or info["identity"] != captured["identity"]):
                raise LifecycleError("reclaim parent directory identity changed")
        return handles
    except BaseException:
        for handle in reversed(handles):
            _win_close(handle)
        raise


def _remove_tree_windows(root: Path, manifest, root_identity, progress: dict, persist) -> dict:
    wanted_id = _manifest_id(manifest)
    if not progress:
        progress.update(capture(root, manifest, root_identity))
    if (progress.get("manifest_id") == wanted_id
            and progress.get("root_identity") == list(root_identity)
            and "root" not in progress):
        progress["root"] = {"identity": list(root_identity),
                            "native_identity": progress.get("root_native_identity"),
                            "tombstone": "handle-bound"}
        for member in progress["members"].values():
            member["tombstone"] = "handle-bound"
        persist()
    if progress.get("manifest_id") != wanted_id or progress.get("root_identity") != list(root_identity):
        raise LifecycleError("reclaim progress does not match immutable manifest")
    expected = _rows(manifest)
    if set(progress.get("members", {})) != set(expected):
        raise LifecycleError("reclaim progress member set changed")
    root_state = progress.get("root")
    if not isinstance(root_state, dict) or root_state.get("native_identity") != progress.get("root_native_identity"):
        raise LifecycleError("reclaim root progress is invalid")
    if not os.path.lexists(root):
        if (not root_state.get("remove_intent")
                or any(member.get("status") != "removed"
                       for member in progress["members"].values())):
            raise LifecycleError("reclaim root disappeared before every member removal")
        root_state["status"] = "removed"; persist()
        return {"removed": True, "observed_removals": 0, "manifest_id": wanted_id}

    root_handle = _win_open(root)
    try:
        bound_root = _win_info(root_handle)
        if (bound_root["kind"] != "dir"
                or bound_root["identity"] != root_state["native_identity"]):
            raise LifecycleError("reclaim root handle identity changed")
        observed = _remove_tree_windows_bound(root, expected, progress, persist,
                                              root_state, root_handle)
    finally:
        _win_close(root_handle)
    observed += 1
    _checkpoint("after-root-remove", str(root))
    root_state["status"] = "removed"; persist()
    return {"removed": True, "observed_removals": observed, "manifest_id": wanted_id}


def _remove_tree_windows_bound(root, expected, progress, persist,
                               root_state, root_handle):
    live_names = _windows_live_names(root)
    unknown = live_names - set(expected)
    if unknown:
        raise LifecycleError("reclaim tree contains an unknown member")
    for missing in set(expected) - live_names:
        if not progress["members"][missing].get("intent"):
            raise LifecycleError("reclaim member disappeared without intent")

    observed = 0
    for logical in sorted(progress["members"], key=lambda p: (p.count("/"), p), reverse=True):
        member = progress["members"][logical]
        path = root.joinpath(*PurePosixPath(logical).parts)
        if not os.path.lexists(path):
            if not member.get("intent"):
                raise LifecycleError("reclaim member disappeared without intent")
            member["status"] = "removed"; persist()
            continue
        member["intent"] = True
        persist()
        _checkpoint("before-member-remove", logical)
        parents = _win_parent_guards(root, logical, progress)
        handle = None
        removed = False
        try:
            handle = _win_open(path)
            info = _win_info(handle)
            if info["identity"] != member["identity"] or info["kind"] != member["kind"]:
                raise LifecycleError("reclaim handle identity changed")
            if info["kind"] == "file":
                if info["links"] != 1 or _win_hash(handle) != member["digest"]:
                    raise LifecycleError("reclaim handle content changed")
            else:
                try:
                    next(path.iterdir())
                except StopIteration:
                    pass
                else:
                    raise LifecycleError("reclaim directory is not empty")
            member["status"] = "quarantined"
            persist()
            _win_delete(handle)
            removed = True
        finally:
            if handle is not None:
                _win_close(handle)
            for parent in reversed(parents):
                _win_close(parent)
        if removed:
            observed += 1
            _checkpoint("after-member-remove", logical)
            member["status"] = "removed"; persist()

    root_state["remove_intent"] = True
    persist()
    _checkpoint("before-root-remove", str(root))
    info = _win_info(root_handle)
    if info["identity"] != root_state["native_identity"] or info["kind"] != "dir":
        raise LifecycleError("reclaim root handle identity changed")
    try:
        next(root.iterdir())
    except StopIteration:
        pass
    else:
        raise LifecycleError("reclaim root is not empty")
    _win_delete(root_handle)
    return observed


def remove_tree(root, manifest, root_identity, progress: dict, persist) -> dict:
    """Remove a captured tree, resuming only operations with durable intent.

    The return value reports removals observed by this invocation.  A retry may
    confirm an intended member is already absent, but does not count that as a
    newly observed deletion.
    """
    root = Path(root)
    if os.name == "nt":
        return _remove_tree_windows(root, manifest, root_identity, progress, persist)
    if os.name != "posix" or not Path("/proc/self/fdinfo").is_dir():
        raise LifecycleError("safe reclamation removal is unsupported on this platform")
    wanted_id = _manifest_id(manifest)
    if not progress:
        snapshot = capture(root, manifest, root_identity)
        progress.update(snapshot)
    if (progress.get("manifest_id") == wanted_id
            and progress.get("root_identity") == list(root_identity)
            and "root" not in progress):
        # A retirement-time capture is the preferred starting progress.  Add
        # only operation journal fields; the captured identities stay fixed.
        progress["root"] = {"identity": list(root_identity),
                            "tombstone": _root_tombstone(root, root_identity).name}
        for logical, member in progress["members"].items():
            member["tombstone"] = _tombstone(logical)
        persist()
    if progress.get("manifest_id") != wanted_id or progress.get("root_identity") != list(root_identity):
        raise LifecycleError("reclaim progress does not match immutable manifest")
    if set(progress.get("members", {})) != set(_rows(manifest)):
        raise LifecycleError("reclaim progress member set changed")

    root_state = progress.get("root")
    if not isinstance(root_state, dict) or root_state.get("identity") != list(root_identity):
        raise LifecycleError("reclaim root progress is invalid")
    if root_state.get("tombstone") != _root_tombstone(root, root_identity).name:
        raise LifecycleError("reclaim root quarantine changed")
    original, staging = _resolve_root(root, root_identity, root_state)
    observed = 0
    if not os.path.lexists(original) and not os.path.lexists(staging):
        if (not root_state.get("remove_intent")
                or any(member.get("status") != "removed"
                       for member in progress["members"].values())):
            raise LifecycleError("reclaim root disappeared before every member removal")
        root_state["status"] = "removed"
        persist()
        return {"removed": True, "observed_removals": 0,
                "manifest_id": progress["manifest_id"]}
    if staging.exists() is False:
        root_state["intent"] = True
        persist()
        _checkpoint("before-root-rename", str(original))
        os.rename(original, staging)
        parent_fd = _open_dir(original.parent)
        try:
            _fsync_dir(parent_fd)
        finally:
            os.close(parent_fd)
        _checkpoint("after-root-rename", str(staging))
    root_state["status"] = "quarantined"
    persist()

    # Before the first effect, revalidate the full immutable capture.  On a
    # retry, a complete manifest is no longer expected; only journal-explained
    # source/quarantine/absence states are admitted.
    if not any(member.get("intent") for member in progress["members"].values()):
        live = _scan_linux(staging, manifest, root_identity)
        fields = {"kind", "identity", "digest"}
        if progress.get("mount_session") == live["mount_session"]:
            fields.add("mount_id")
        captured = {key: {k: v for k, v in value.items() if k in fields}
                    for key, value in progress["members"].items()}
        current = {key: {k: v for k, v in value.items() if k in fields}
                   for key, value in live["members"].items()}
        if live["manifest_id"] != progress["manifest_id"] or current != captured:
            raise LifecycleError("reclaim captured identities changed")
    else:
        _validate_resuming_names(staging, progress["members"])

    root_fd = _open_dir(staging)
    try:
        _root_info, current_mount = _validate_root(root_fd, root_identity)
        same_mount_session = progress.get("mount_session") == _mount_session()
        if (current_mount is None
                or same_mount_session and current_mount != progress.get("root_mount_id")):
            raise LifecycleError("reclaim root mount identity changed")
        for logical in sorted(progress["members"], key=lambda p: (p.count("/"), p), reverse=True):
            member = progress["members"][logical]
            if member.get("status") == "removed":
                continue
            parts = PurePosixPath(logical).parts
            parent_fd = os.dup(root_fd)
            try:
                for depth, component in enumerate(parts[:-1], 1):
                    next_fd = _open_dir(component, dir_fd=parent_fd)
                    component_logical = "/".join(parts[:depth])
                    captured_parent = progress["members"].get(component_logical)
                    opened_parent = os.fstat(next_fd)
                    if (not captured_parent or captured_parent["kind"] != "dir"
                            or _identity(opened_parent) != captured_parent["identity"]
                            or _mount_id(next_fd) != current_mount
                            or same_mount_session and captured_parent["mount_id"] != current_mount):
                        os.close(next_fd)
                        raise LifecycleError("reclaim parent directory identity changed")
                    os.close(parent_fd); parent_fd = next_fd
                name = parts[-1]; tomb = member["tombstone"]
                source = None
                try:
                    source = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                tomb_info = None
                try:
                    tomb_info = os.stat(tomb, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                if source is not None and tomb_info is not None:
                    raise LifecycleError("reclaim member and quarantine both exist")
                if source is None and tomb_info is None:
                    if not member.get("intent"):
                        raise LifecycleError("reclaim member disappeared without intent")
                    member["status"] = "removed"
                    persist()
                    continue
                if tomb_info is None:
                    member["intent"] = True
                    persist()
                    _checkpoint("before-member-rename", logical)
                    os.rename(name, tomb, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                    _fsync_dir(parent_fd)
                    tomb_info = os.stat(tomb, dir_fd=parent_fd, follow_symlinks=False)
                if _identity(tomb_info) != member["identity"]:
                    raise LifecycleError("reclaim quarantined member identity changed")
                member["status"] = "quarantined"
                persist()
                _checkpoint("before-member-remove", logical)
                if member["kind"] == "file":
                    fd = _open_file(tomb, dir_fd=parent_fd)
                    try:
                        info = os.fstat(fd)
                        if (_identity(info) != member["identity"] or info.st_nlink != 1
                                or _mount_id(fd) != current_mount
                                or same_mount_session and member["mount_id"] != current_mount
                                or _digest_fd(fd) != member["digest"]):
                            raise LifecycleError("reclaim quarantined file changed")
                    finally:
                        os.close(fd)
                    os.unlink(tomb, dir_fd=parent_fd)
                else:
                    fd = _open_dir(tomb, dir_fd=parent_fd)
                    try:
                        if (_identity(os.fstat(fd)) != member["identity"]
                                or _mount_id(fd) != current_mount
                                or same_mount_session and member["mount_id"] != current_mount
                                or os.listdir(fd)):
                            raise LifecycleError("reclaim quarantined directory changed")
                    finally:
                        os.close(fd)
                    os.rmdir(tomb, dir_fd=parent_fd)
                _fsync_dir(parent_fd)
                observed += 1
                _checkpoint("after-member-remove", logical)
                member["status"] = "removed"
                persist()
            finally:
                os.close(parent_fd)
        root_state["remove_intent"] = True
        persist()
        _checkpoint("before-root-remove", str(staging))
    finally:
        os.close(root_fd)
    parent_fd = _open_dir(staging.parent)
    try:
        current = os.stat(staging.name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISDIR(current.st_mode) or _identity(current) != list(root_identity):
            raise LifecycleError("reclaim root name no longer identifies captured directory")
        os.rmdir(staging.name, dir_fd=parent_fd)
        _fsync_dir(parent_fd)
    finally:
        os.close(parent_fd)
    observed += 1
    _checkpoint("after-root-remove", str(staging))
    root_state["status"] = "removed"
    persist()
    return {"removed": True, "observed_removals": observed,
            "manifest_id": progress["manifest_id"]}
