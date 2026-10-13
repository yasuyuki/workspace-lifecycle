"""Required remote checks for integrating one exact accepted commit.

The repository owns which checks exist; a task names the ones integration
needs. Only the latest GitHub check run of each required app and name on the
exact commit counts, and only ``completed`` with conclusion ``success`` passes.
"""
from __future__ import annotations

import json
import re
import subprocess

from .errors import LifecycleError
from .git import git

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


def github_repository(url: str) -> str:
    match = _GITHUB.match(url or '')
    if not match:
        raise LifecycleError('required checks need a GitHub remote; push URL is ' + json.dumps(url))
    return match.group(1) + '/' + match.group(2)


def repository(repo, remote: str) -> str:
    """The GitHub repository the remote actually pushes to."""
    return github_repository(git(repo, 'remote', 'get-url', '--push', remote))


def _runs(repository: str, commit: str, name: str) -> list[dict]:
    argv = ['gh', 'api', '--paginate', '-X', 'GET',
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
