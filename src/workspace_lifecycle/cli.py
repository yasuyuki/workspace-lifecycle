from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import os
import subprocess

from . import __version__

from .errors import LifecycleError
from . import service
from .git import bound_task, common_dir, remote_default, task_worktree, top
from .state import locked_state


def _json_list(value: str):
    result = json.loads(value)
    if not isinstance(result, list) or not all(isinstance(x, str) for x in result):
        raise argparse.ArgumentTypeError("must be a JSON string array")
    return result


def parser():
    root = argparse.ArgumentParser(prog="workspace-lifecycle")
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--repo", default=".")
    commands = root.add_subparsers(dest="command", required=True)
    begin = commands.add_parser("begin")
    for name in ("task", "request", "remote", "branch", "worktree"): begin.add_argument("--" + name, required=True)
    begin.add_argument("--parent"); begin.add_argument("--dependency", action="append", default=[])
    begin.add_argument("--validation-json", type=_json_list, default=[])
    begin.add_argument("--preflight-json", type=_json_list, required=True)
    status = commands.add_parser("status"); status.add_argument("--task")
    hold = commands.add_parser("hold"); hold.add_argument("--task", required=True); hold.add_argument("--reason", required=True); hold.add_argument("--next-action", required=True)
    resume = commands.add_parser('release-hold', help='resolve an explicit hold using a decision reference')
    resume.add_argument('--task', required=True); resume.add_argument('--evidence', required=True)
    finish = commands.add_parser("finish", description="Resolve owned dirty, validate, save, push and normally integrate one task.",
        epilog='Plan: commit/restore/archive arrays of path, owner, evidence, classification and sha256; source requires safe_to_commit=true. Restore requires regeneration.evidence; private archive requires store and approval_evidence. Exception requires reviewed_all_alternatives=true and commit/restore/archive/owner-resolution evidence, irreversible_harm, remaining_owner and next_action.'); finish.add_argument("--task", required=True); finish.add_argument("--plan", required=True); finish.add_argument("--result-ref", required=True); finish.add_argument("--message", default="workspace lifecycle completion"); finish.add_argument("--users-released", action="store_true"); finish.add_argument("--revise-plan-evidence", help="explicit review of a revised finish plan after all pending preservation actions complete")
    retire = commands.add_parser("retire"); retire.add_argument("--task"); retire.add_argument("--result-ref"); retire.add_argument("--pending", action="store_true"); retire.add_argument("--users-released", action="store_true"); retire.add_argument("--request", action="store_true")
    run = commands.add_parser("run"); run.add_argument("--task", required=True); run.add_argument("--cwd", default="."); run.add_argument("argv", nargs=argparse.REMAINDER)
    resolve = commands.add_parser("resolve-run", help="Run unmanaged work directly or supervise an already managed task.")
    resolve.add_argument("--cwd", required=True, help="effective workspace directory selected by the caller")
    resolve.add_argument("--launch-cwd", required=True, help="invocation directory used to resolve native relative arguments")
    resolve.add_argument("argv", nargs=argparse.REMAINDER)
    lease_status = commands.add_parser("lease-status"); lease_status.add_argument("--task", required=True)
    release = commands.add_parser("lease-release"); release.add_argument("--task", required=True); release.add_argument("--token", required=True); release.add_argument("--evidence", required=True)
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "begin": result = service.begin(args.repo, task=args.task, request=args.request, remote=args.remote, branch=args.branch, worktree=args.worktree, parent=args.parent, dependencies=args.dependency, validation=args.validation_json, preflight=args.preflight_json)
        elif args.command == "status": result = service.status(args.repo, args.task)
        elif args.command == "hold": result = service.hold(args.repo, args.task, args.reason, args.next_action)
        elif args.command == "release-hold": result = service.release_hold(args.repo, args.task, args.evidence)
        elif args.command == "finish": result = service.finish(args.repo, task=args.task, plan_path=args.plan, result_ref=args.result_ref, message=args.message, users_released=args.users_released, revision_evidence=args.revise_plan_evidence)
        elif args.command == "retire":
            if args.pending:
                if args.task or args.result_ref: raise LifecycleError("retire --pending takes no task or result reference")
                result = service.retire_pending(args.repo)
            elif args.task and args.result_ref: result = service.retire(args.repo, task=args.task, result_ref=args.result_ref, users_released=args.users_released, request_only=args.request)
            else: raise LifecycleError("retire requires --task and --result-ref")
        elif args.command in {"run", "resolve-run"}:
            argv = args.argv[1:] if args.argv[:1] == ['--'] else args.argv
            if not argv:
                raise LifecycleError(args.command + ' requires an argv after --')
            if args.command == "resolve-run":
                return _resolve_run(args.cwd, args.launch_cwd, argv)
            from .leases import run
            repo = Path(args.repo).resolve()
            cwd = Path(args.cwd).resolve()
            if cwd != repo and repo not in cwd.parents:
                raise LifecycleError('run cwd must be inside the selected task worktree')
            service.retire_pending(repo)
            code = run(repo, args.task, argv, cwd,
                       before_spawn=lambda: service.before_run(repo, args.task))
            # Native output and status pass through; management JSON belongs to
            # status/finish, not the program's stdout stream.
            with locked_state(repo) as (_, state):
                remote = state['tasks'][args.task]['remote']
            control = task_worktree(repo, remote_default(repo, remote))
            os.chdir(control)
            recovered = service.retire_pending(control)
            if any(not entry.get('retired') for entry in recovered['pending']):
                print(json.dumps(recovered), file=sys.stderr)
                if code == 0:
                    return 1
            return code if code >= 0 else 128 - code
        elif args.command == "lease-status":
            from .leases import status
            result = {"task": args.task, "lease": status(args.repo, args.task)}
        else:
            from .leases import release
            release(args.repo, args.task, args.token, args.evidence); result = {"task": args.task, "released": True}
    except (LifecycleError, ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr); return 2
    print(json.dumps({"ok": True, **result}, sort_keys=True)); return 0


def _native_code(code):
    return code if code >= 0 else 128 - code


def _plain_run(argv, cwd, env=None):
    environment = os.environ if env is None else env
    if os.name == 'posix':
        os.chdir(cwd)
        os.execvpe(argv[0], argv, environment)
    import signal
    signal.signal(signal.SIGBREAK, lambda *_: None)
    child = subprocess.Popen(argv, cwd=cwd, env=environment)
    while True:
        try:
            return _native_code(child.wait())
        except KeyboardInterrupt:
            # Console control events reach the child directly. Keep the thin
            # parent alive until the native command reports its own status.
            continue


def _git_marker(path):
    for candidate in (path, *path.parents):
        marker = candidate / '.git'
        if marker.exists() or marker.is_symlink():
            return marker
    return None


def _resolve_run(effective, launch, argv):
    """Resolve lifecycle ownership without exposing its state schema to callers."""
    effective = Path(effective).resolve()
    launch = Path(launch).resolve()
    if not effective.is_dir() or not launch.is_dir():
        raise LifecycleError('resolve-run directories must exist')
    probe = subprocess.run(['git', '-C', str(effective), 'rev-parse', '--show-toplevel'],
                           text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if probe.returncode:
        if _git_marker(effective) is not None:
            raise LifecycleError('effective cwd has an invalid Git workspace')
        return _plain_run(argv, launch)
    repo = Path(probe.stdout.strip()).resolve()
    if effective != repo and repo not in effective.parents:
        raise LifecycleError('effective cwd is outside its resolved Git worktree')
    common = common_dir(repo)
    if (common / 'agent-branches' / 'state.json').exists():
        raise LifecycleError('legacy agent-branches registry exists; migration is not authorized')
    lifecycle = common / 'workspace-lifecycle'
    if not lifecycle.exists():
        return _plain_run(argv, launch)
    if not lifecycle.is_dir():
        raise LifecycleError('workspace lifecycle state is not a directory')
    task = bound_task(repo)
    finish_argv = [sys.executable, '-m', 'workspace_lifecycle', '--repo', str(repo),
                   'finish', '--task', task]
    context = json.dumps({'version': 1, 'repo': str(repo), 'task': task,
                          'finish_argv': finish_argv},
                         sort_keys=True, separators=(',', ':'))
    from .leases import run
    service.retire_pending(repo)
    code = run(repo, task, argv, launch,
               before_spawn=lambda: service.before_run(repo, task),
               child_env={'WORKSPACE_LIFECYCLE_CONTEXT': context,
                          'WORKSPACE_LIFECYCLE_REPO': str(repo),
                          'WORKSPACE_LIFECYCLE_TASK': task})
    with locked_state(repo) as (_, state):
        remote = state['tasks'][task]['remote']
    control = task_worktree(repo, remote_default(repo, remote))
    os.chdir(control)
    recovered = service.retire_pending(control)
    if any(not entry.get('retired') for entry in recovered['pending']):
        print(json.dumps(recovered), file=sys.stderr)
        return 1 if code == 0 else _native_code(code)
    return _native_code(code)


if __name__ == "__main__": raise SystemExit(main())
