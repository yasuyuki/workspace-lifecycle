"""Explicit foreign retirement boundaries, never retirement certification."""
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import stat

from .errors import LifecycleError
from .git import git, worktree_records


def _text(value):
    return isinstance(value, str) and bool(value.strip()) and not any(c in value for c in '\0\n\r')


def _identity(value):
    return (isinstance(value, list) and len(value) == 2
            and all(type(n) is int and n >= 0 for n in value))


def contracts(value, task):
    if value is None:
        return []
    if not isinstance(value, list):
        raise LifecycleError('preserved data must be an explicit boundary array')
    result = []
    for entry in value:
        if (not isinstance(entry, dict)
                or set(entry) != {'path', 'owner', 'receipt_ref', 'evidence', 'identity',
                                 'admin_archive', 'unresolved'}
                or not all(_text(entry.get(key)) for key in ('path', 'owner', 'receipt_ref', 'evidence'))
                or entry['owner'] == task or not _identity(entry['identity'])
                or not isinstance(entry['unresolved'], list)
                or not all(_text(v) for v in entry['unresolved'])):
            raise LifecycleError('preserved data needs foreign owner, receipt/evidence references, current identities and unresolved conditions')
        path = Path(entry['path'])
        if (path.is_absolute() or path.drive or path.as_posix() != entry['path'] or not path.parts
                or any(p.casefold() in ('.', '..', '.git') for p in path.parts)):
            raise LifecycleError('preserved data path must be canonical relative and outside Git metadata')
        admin = entry['admin_archive']
        if (not isinstance(admin, dict) or set(admin) != {'path', 'identity'}
                or not _text(admin['path']) or not Path(admin['path']).is_absolute()
                or not _identity(admin['identity'])):
            raise LifecycleError('preserved data needs an absolute archived admin and its current identity')
        result.append(entry)
    paths = [Path(e['path']) for e in result]
    if any(a == b or a in b.parents or b in a.parents
           for i, a in enumerate(paths) for b in paths[i + 1:]):
        raise LifecycleError('preserved data boundaries overlap')
    return deepcopy(result)


def _anchor(path, expected=None, *, directory=True, anchor=None):
    from .service import _no_links
    _no_links(path)
    for part in (path, *path.parents):
        if part == anchor:
            break
        if os.path.ismount(part) and part != Path(part.anchor):
            raise LifecycleError('preserved data refuses mount traversal')
    info = path.lstat()
    identity = [info.st_dev, info.st_ino]
    if expected is not None and identity != expected:
        raise LifecycleError('preserved data current identity mismatch')
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise LifecycleError('preserved data anchor has the wrong file type')
    return identity


def capture(workspace, entries, records=None):
    """Read only named roots and the native Git pointer; never enter payload/admin."""
    if not entries:
        return {}
    workspace = Path(workspace).resolve()
    records = worktree_records(workspace) if records is None else records
    result = {}
    for entry in entries:
        payload = workspace / entry['path']
        admin = Path(entry['admin_archive']['path'])
        try:
            anchor = Path(os.path.commonpath([workspace, admin]))
        except ValueError:
            anchor = None  # Distinct Windows volume roots are valid native anchors.
        _anchor(payload, entry['identity'], anchor=anchor or Path(payload.anchor))
        _anchor(admin, entry['admin_archive']['identity'], anchor=anchor or Path(admin.anchor))
        if payload == admin or payload in admin.parents or admin in payload.parents:
            raise LifecycleError('preserved payload and archived admin overlap')
        roots = [payload, admin]
        for record in records:
            active = Path(record['worktree']).resolve()
            if any(active == root or root in active.parents or (active != workspace and active in root.parents) for root in roots):
                raise LifecycleError('preserved data overlaps an active Git worktree')
        marker = payload / '.git'
        marker_identity = _anchor(marker, directory=False, anchor=anchor or Path(marker.anchor))
        if marker.stat().st_size > 16384:
            raise LifecycleError('preserved payload requires a native retired Git file')
        raw = marker.read_bytes()
        try:
            pointer = raw.decode('utf-8').strip()
        except UnicodeError as exc:
            raise LifecycleError('preserved payload Git pointer is invalid') from exc
        if not pointer.startswith('gitdir: ') or '\n' in pointer or '\r' in pointer:
            raise LifecycleError('preserved payload requires a native retired Git file')
        original = Path(pointer[8:])
        if not original.is_absolute():
            raise LifecycleError('preserved payload requires an absolute former admin path')
        from .service import _no_links
        _no_links(original)
        try:
            original_anchor = Path(os.path.commonpath([workspace, original]))
        except ValueError:
            original_anchor = Path(original.anchor)
        for part in (original, *original.parents):
            if part == original_anchor:
                break
            if os.path.ismount(part) and part != Path(part.anchor):
                raise LifecycleError('preserved data refuses former admin mount traversal')
        if os.path.lexists(original):
            raise LifecycleError('preserved data still has an active Git admin')
        # No tracked source or Gitlink may be hidden behind this contract.
        protected = [root.relative_to(workspace).as_posix() for root in roots
                     if workspace in root.parents and '.git' not in root.relative_to(workspace).parts]
        for name in protected:
            if git(workspace, 'ls-files', '-z', '--', name):
                raise LifecycleError('preserved data overlaps tracked source')
        anchors = {}
        for root in roots:
            stop = anchor or Path(root.anchor)
            for parent in root.parents:
                anchors[str(parent)] = _anchor(parent, anchor=stop)
                if parent == stop:
                    break
        result[entry['path']] = {'payload': entry['identity'],
            'marker_identity': marker_identity, 'marker_sha256': hashlib.sha256(raw).hexdigest(),
            'admin_archive': entry['admin_archive'], 'parents': anchors,
            'protected_paths': protected}
        _anchor(payload, entry['identity'], anchor=anchor or Path(payload.anchor))
        _anchor(admin, entry['admin_archive']['identity'], anchor=anchor or Path(admin.anchor))
        if _anchor(marker, directory=False, anchor=anchor or Path(marker.anchor)) != marker_identity or marker.read_bytes() != raw:
            raise LifecycleError('preserved data Git marker changed while capturing')
    return result


def check(workspace, item):
    adoption = item.get('adoption', {})
    entries = adoption.get('preserved_data', [])
    if entries and capture(workspace, entries) != adoption['preserved_snapshot']:
        raise LifecycleError('preserved data boundary changed; keep the original ownership contract')


def overlap(name, boundaries):
    path = Path(name)
    return any(path == Path(root) or Path(root) in path.parents or path in Path(root).parents
               for root in boundaries)
