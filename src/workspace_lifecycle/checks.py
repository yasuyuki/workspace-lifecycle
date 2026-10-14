"""Required remote checks for integrating one exact accepted commit.

The repository owns which checks exist and may declare the ones every task
needs in a tracked file on its remote default branch. A task requires at least
that declared set. Only the latest GitHub check run of each required app and
name on the exact commit counts, and only ``completed`` with conclusion
``success`` passes.
"""
from __future__ import annotations

import json
import re
import subprocess

from .errors import LifecycleError
from .git import git, head, remote_default

DECLARATION = '.github/required-checks.json'

_GITHUB = re.compile(r'^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)'
                     r'([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$')


def parse(value) -> list[dict]:
    """A list of distinct {"app", "name"} check identities, possibly empty."""
    if not isinstance(value, list):
        raise LifecycleError('required checks must be a JSON array')
    checks = []
    for item in value:
        if (not isinstance(item, dict) or set(item) != {'app', 'name'}
                or not all(isinstance(item[k], str) and item[k].strip() for k in ('app', 'name'))):
            raise LifecycleError('each required check needs exactly a nonempty app and name')
        checks.append({'app': item['app'], 'name': item['name']})
    if len({(c['app'], c['name']) for c in checks}) != len(checks):
        raise LifecycleError('required checks must be distinct')
    return checks


def declared(repo, commit: str) -> dict:
    """The checks the repository declares at ``commit``; only an absent file declares none."""
    policy = {'path': DECLARATION, 'commit': commit}
    entry = git(repo, 'ls-tree', '--full-tree', commit, '--', DECLARATION)
    if not entry:
        return {**policy, 'blob': None, 'checks': []}
    mode, kind, blob = entry.partition('\t')[0].split()
    if kind != 'blob' or mode not in ('100644', '100755'):
        raise LifecycleError(DECLARATION + ' must be a regular file at ' + commit)
    try:
        return {**policy, 'blob': blob, 'checks': parse(json.loads(git(repo, 'cat-file', 'blob', blob)))}
    except (ValueError, LifecycleError) as exc:
        raise LifecycleError('invalid ' + DECLARATION + ' at ' + commit + ': ' + str(exc))


def current(repo, remote: str) -> dict:
    """The declaration on the remote default branch as fetched now, never the checkout."""
    default = remote_default(repo, remote)
    git(repo, 'fetch', remote, 'refs/heads/' + default)
    return declared(repo, head(repo, 'FETCH_HEAD'))


def require(policy: dict, selected: list[dict]) -> None:
    missing = [check for check in policy['checks'] if check not in selected]
    if missing:
        raise LifecycleError('required checks omit ' + json.dumps(missing) + ' declared by '
                             + policy['path'] + ' at ' + policy['commit']
                             + '; require them with update-required-checks and durable evidence')


def select(policy: dict, explicit: list[dict] | None) -> list[dict]:
    """The declared set when omitted; otherwise an explicit superset of it."""
    if explicit is None:
        return list(policy['checks'])
    require(policy, explicit)
    return explicit


def github_repository(url: str) -> str:
    match = _GITHUB.match(url or '')
    if not match:
        raise LifecycleError('required checks need a GitHub remote; push URL is ' + json.dumps(url))
    return match.group(1) + '/' + match.group(2)


def repository(repo, remote: str) -> str:
    """The GitHub repository the remote actually pushes to."""
    urls = (git(repo, 'remote', 'get-url', '--push', '--all', remote) or '').splitlines()
    if len(urls) != 1:
        raise LifecycleError('required checks need exactly one push URL for ' + remote)
    return github_repository(urls[0])


def _runs(repository: str, commit: str, name: str) -> list[dict]:
    argv = ['gh', 'api', '--hostname', 'github.com', '--paginate', '-X', 'GET',
            f'repos/{repository}/commits/{commit}/check-runs',
            '-f', 'check_name=' + name, '-f', 'filter=all', '-f', 'per_page=100',
            '--jq', '.check_runs[] | {id, name, head_sha, status, conclusion, app: .app.slug}']
    try:
        result = subprocess.run(argv, text=True, capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LifecycleError('required check status unavailable: ' + type(exc).__name__)
    if result.returncode:
        raise LifecycleError('required check status unavailable: ' + result.stderr.strip()[-500:])
    try:
        return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    except ValueError:
        raise LifecycleError('required check status unavailable: unreadable response')


def verify(repository: str, commit: str, required: list[dict]) -> dict:
    """Evidence that every required check succeeded on exactly ``commit``, or a refusal."""
    results, unmet = [], []
    for check in required:
        runs = [run for run in _runs(repository, commit, check['name'])
                if run.get('app') == check['app'] and run.get('name') == check['name']]
        if any(run.get('head_sha') != commit or not isinstance(run.get('id'), int) for run in runs):
            raise LifecycleError('required check response is not bound to the requested commit')
        latest = max(runs, key=lambda run: run['id'], default=None)
        state = ('missing' if latest is None else
                 latest.get('status') if latest.get('status') != 'completed' else
                 latest.get('conclusion') or 'unknown')
        entry = {**check, 'state': state, **({'run': latest['id']} if latest else {})}
        results.append(entry)
        if state != 'success':
            unmet.append(entry)
    if unmet:
        raise LifecycleError('required checks have not succeeded for ' + repository + '@' + commit
                             + ': ' + json.dumps(unmet, sort_keys=True))
    return {'repository': repository, 'commit': commit, 'checks': results}
