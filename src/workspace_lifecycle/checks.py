"""Required remote checks for integrating one exact accepted commit.

The repository owns which checks exist and may declare the ones every task
needs in a tracked file on its remote default branch. A task requires at least
that declared set. Only the latest GitHub check run of each required app and
name on the exact commit counts, and only ``completed`` with conclusion
``success`` passes. Checks on a branch's commit count only runs pushed to that
branch, since the same commit may also carry runs from another branch. Any other completed conclusion is a failure; a missing or
unfinished check is pending.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time

from .errors import LifecycleError
from .git import git, head, remote_default

DECLARATION = '.github/required-checks.json'

# GitHub Actions ends a job after 360 minutes unless the workflow sets
# timeout-minutes, and no declaring workflow sets it.
DEFAULT_WAIT_SECONDS = 360 * 60
PENDING = frozenset({'missing', 'queued', 'in_progress', 'waiting', 'requested', 'pending'})
_POLL_SECONDS = 30

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


def source(repo, commit: str, policy: dict, selected: list[dict]) -> dict | None:
    """The declaration ``commit`` changes relative to the trusted ``policy``, if any.

    One it inherits unchanged from its merge base with ``policy`` does not land.
    Its checks join ``selected`` as ``added``; a removal or weakening adds nothing,
    so ``selected`` keeps every check the task already requires.
    """
    base = git(repo, 'merge-base', commit, policy['commit'], optional=True)
    revisions = [commit, *([base] if base else [])]
    looked_up = subprocess.run(['git', '-C', str(repo), 'cat-file', '--batch-check=%(objectname)'],
                               input=''.join(revision + ':' + DECLARATION + '\n' for revision in revisions),
                               text=True, encoding='utf-8', capture_output=True)
    rows = looked_up.stdout.splitlines()
    if looked_up.returncode or len(rows) != len(revisions):
        raise LifecycleError('could not read ' + DECLARATION + ' at ' + commit)
    # A path absent at a revision prints "<revision>:<path> missing".
    entry, *inherited = [None if ' ' in row else row for row in rows]
    if entry in {policy['blob'], *inherited}:
        return None
    proposed = declared(repo, commit)
    return {**proposed, 'base': {'commit': policy['commit'], 'blob': policy['blob']},
            'added': [check for check in proposed['checks'] if check not in selected]}


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
            '--jq', '.check_runs[] | {id, name, head_sha, status, conclusion, html_url, app: .app.slug, suite: .check_suite.id}']
    return _api(argv)


def _suites(repository: str, commit: str) -> dict[int, str | None]:
    """The branch each check suite on ``commit`` was created for."""
    argv = ['gh', 'api', '--hostname', 'github.com', '--paginate', '-X', 'GET',
            f'repos/{repository}/commits/{commit}/check-suites', '-f', 'per_page=100',
            '--jq', '.check_suites[] | {id, head_branch, head_sha}']
    suites = _api(argv)
    if any(suite.get('head_sha') != commit or not isinstance(suite.get('id'), int) for suite in suites):
        raise LifecycleError('required check response is not bound to the requested commit')
    return {suite['id']: suite.get('head_branch') for suite in suites}


def _api(argv: list[str]) -> list[dict]:
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


def _states(repository: str, commit: str, required: list[dict], branch: str | None = None) -> list[dict]:
    results = []; suites = _suites(repository, commit) if branch else None
    for check in required:
        runs = [run for run in _runs(repository, commit, check['name'])
                if run.get('app') == check['app'] and run.get('name') == check['name']]
        if any(run.get('head_sha') != commit or not isinstance(run.get('id'), int) for run in runs):
            raise LifecycleError('required check response is not bound to the requested commit')
        if branch:
            runs = [run for run in runs if suites.get(run.get('suite')) == branch]
        latest = max(runs, key=lambda run: run['id'], default=None)
        state = ('missing' if latest is None else
                 latest.get('status') if latest.get('status') != 'completed' else
                 latest.get('conclusion') or 'unknown')
        url = latest.get('html_url') if latest else None
        results.append({**check, 'state': state, **({'run': latest['id']} if latest else {}),
                        **({'url': url} if isinstance(url, str) and url else {})})
    return results


def verify(repository: str, commit: str, required: list[dict]) -> dict:
    """Evidence that every required check succeeded on exactly ``commit``, or a refusal."""
    results = _states(repository, commit, required)
    unmet = [entry for entry in results if entry['state'] != 'success']
    if unmet:
        raise LifecycleError('required checks have not succeeded for ' + repository + '@' + commit
                             + ': ' + json.dumps(unmet, sort_keys=True))
    return {'repository': repository, 'commit': commit, 'checks': results}


def watch(repository: str, commit: str, required: list[dict], seconds: float,
          branch: str | None = None) -> tuple[str, list[dict]]:
    """Poll the checks on ``commit``, from runs on ``branch`` when given:
    ``success`` once every one succeeded, ``failed`` at the first failed one,
    or ``pending`` when ``seconds`` run out."""
    deadline = time.monotonic() + seconds; reported = None
    while True:
        results = _states(repository, commit, required, branch)
        if any(entry['state'] not in PENDING | {'success'} for entry in results):
            return 'failed', results
        unmet = [entry for entry in results if entry['state'] != 'success']
        if not unmet:
            return 'success', results
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return 'pending', results
        states = [(entry['name'], entry['state']) for entry in unmet]
        if states != reported:
            print('waiting for required checks on ' + commit + ': ' + json.dumps(dict(states)),
                  file=sys.stderr, flush=True)
            reported = states
        time.sleep(min(_POLL_SECONDS, remaining))


def wait(repository: str, commit: str, required: list[dict], seconds: float) -> None:
    """Return once every required check succeeded on ``commit``; refuse at the
    first failed one, or with the unfinished ones when ``seconds`` run out."""
    outcome, results = watch(repository, commit, required, seconds)
    if outcome == 'failed':
        failed = [entry for entry in results if entry['state'] not in PENDING | {'success'}]
        raise LifecycleError('required checks failed for ' + repository + '@' + commit + ': '
                             + json.dumps(failed, sort_keys=True),
                             'the accepted commit is kept; rerun the failed check or commit a fix, then run finish again')
    if outcome == 'pending':
        unmet = [entry for entry in results if entry['state'] != 'success']
        raise LifecycleError('required checks did not finish within ' + str(seconds) + ' seconds for '
                             + repository + '@' + commit + ': ' + json.dumps(unmet, sort_keys=True),
                             'the accepted commit is kept; run the same finish again to keep waiting')
