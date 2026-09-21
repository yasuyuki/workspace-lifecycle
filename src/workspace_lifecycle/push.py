#!/usr/bin/env python3
"""Read-only policy check for a normal, single-branch git push.

``decide`` deliberately has no subprocess calls.  Its input is a JSON-like
mapping with these keys (all optional): ``current_branch``, ``candidates``,
``remotes``, ``upstream``, and ``repo``.  See ``collect_state`` for the exact
shape produced for a real repository.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import url2pathname


# Deliberately conservative: an unknown SPDX identifier is not evidence that a
# public repository is OSS.  This small verified initial set can be bypassed
# only by an explicit ``--oss yes`` declaration.
OSI_LICENSES = frozenset({"MIT", "Apache-2.0", "BSD-3-Clause", "ISC"})


def result(decision: str, reason: str, remote: str | None = None,
           destination: str | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {"decision": decision, "reason": reason,
                             "remote": remote, "destination": destination}
    if decision == "push":
        value["push_argv"] = ["git", "push", remote, "HEAD:refs/heads/" + destination]
    return value


def choose_remote(state: dict[str, Any]) -> str | None:
    candidates = state.get("candidates") or {}
    remotes = state.get("remotes") or {}
    for key in ("branch_push_remote", "remote_push_default", "branch_remote"):
        value = candidates.get(key)
        if value is not None:
            return value
    return next(iter(remotes)) if len(remotes) == 1 else None


def destination_for(state: dict[str, Any], remote: str, branch: str) -> str | None:
    upstream = state.get("upstream") or {}
    if upstream.get("remote") != remote:
        return branch
    merge = upstream.get("merge")
    if merge is None:
        return branch
    prefix = "refs/heads/"
    if not isinstance(merge, str) or not merge.startswith(prefix) or len(merge) == len(prefix):
        return None
    return merge[len(prefix):]


def valid_name(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and not value.startswith("-") and "\x00" not in value


def decide(state: dict[str, Any], user_intent: str = "auto", temporary: bool = False,
           oss: str = "auto", policy: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a deterministic ``push``, ``hold``, or ``ask`` policy decision.

    ``user_intent`` is ``auto``, ``push``, or ``hold``; ``oss`` is ``auto``,
    ``yes``, or ``no``. Invalid state and unknown evidence fail closed.
    """
    if user_intent not in {"auto", "push", "hold"} or oss not in {"auto", "yes", "no"}:
        return result("ask", "INVALID_INPUT")
    if user_intent == "hold":
        return result("hold", "USER_HOLD")
    if temporary and user_intent != "push":
        return result("hold", "TEMPORARY_COMMIT")
    allowed_repositories: set[tuple[str, str]] = set()
    if user_intent == "auto" and policy is not None:
        try:
            allowed_repositories = policy_repositories(policy)
        except (TypeError, ValueError):
            return result("ask", "INVALID_PUSH_POLICY")
    if state.get("collection_error"):
        return result("ask", "COLLECTION_ERROR")

    branch = state.get("current_branch")
    remote = choose_remote(state)
    if remote is None:
        return result("ask", "NO_PUSH_REMOTE")
    remotes = state.get("remotes") or {}
    if not valid_name(remote) or remote == "." or remote not in remotes:
        return result("ask", "INVALID_PUSH_REMOTE")
    info = remotes[remote] or {}
    push_urls = info.get("push_urls") or []
    fetch_urls = info.get("fetch_urls") or []
    if len(push_urls) > 1:
        return result("ask", "AMBIGUOUS_PUSH_URL", remote)
    effective_fetch = info.get("effective_fetch_urls", fetch_urls) or []
    effective_push = info.get("effective_push_urls", push_urls or fetch_urls) or []
    if len(effective_push) != 1 or len(effective_fetch) != 1:
        return result("ask", "AMBIGUOUS_PUSH_URL", remote)
    push_identity = repo_identity(effective_push[0])
    fetch_identity = repo_identity(effective_fetch[0])
    if push_identity is None or fetch_identity is None:
        return result("ask", "INVALID_REMOTE_URL", remote)
    if push_identity != fetch_identity:
        return result("ask", "PUSH_REPOSITORY_MISMATCH", remote)
    if not push_urls and len(fetch_urls) != 1:
        return result("ask", "AMBIGUOUS_FETCH_URL", remote)
    if info.get("push_refspecs"):
        return result("ask", "CUSTOM_PUSH_REFSPEC", remote)
    if info.get("mirror_invalid"):
        return result("ask", "INVALID_MIRROR_SETTING", remote)
    if info.get("mirror"):
        return result("ask", "MIRROR_REMOTE", remote)

    repo = state.get("repo") or {}
    if user_intent == "auto" and (repo.get("metadata_error") or repo.get("visibility") not in {"private", "public"}):
        return result("ask", "METADATA_UNAVAILABLE", remote)
    if user_intent == "auto" and repo.get("visibility") == "public":
        if oss == "no":
            return result("hold", "NON_OSS_PUBLIC_REPOSITORY", remote)
        if oss != "yes" and repo.get("license_spdx_id") not in OSI_LICENSES:
            return result("ask", "UNKNOWN_OSS_LICENSE", remote)
    if not valid_name(branch):
        return result("ask", "DETACHED_HEAD", remote)
    upstream = state.get("upstream") or {}
    merge_values = upstream.get("merge_values")
    if isinstance(merge_values, list) and len(merge_values) > 1:
        return result("ask", "INVALID_UPSTREAM", remote)
    destination = destination_for(state, remote, branch)
    if not valid_name(destination) or destination.startswith("refs/"):
        return result("ask", "INVALID_UPSTREAM", remote)
    if user_intent == "push":
        return result("push", "PUSH_ALLOWED", remote, destination)

    default_branch = repo.get("default_branch")
    if not valid_name(default_branch):
        return result("ask", "DEFAULT_BRANCH_UNAVAILABLE", remote, destination)
    if branch == default_branch or destination == default_branch:
        configured = policy or {}
        excluded = {repo_identity(url) for url in configured.get("default_branch_push_excluded_repositories", [])}
        if push_identity in excluded:
            return result("hold", "DEFAULT_BRANCH_PUSH_EXCLUDED", remote, destination)
        category = "private" if repo.get("visibility") == "private" else "oss"
        if push_identity in allowed_repositories or configured.get("default_branch_push_" + category, False):
            return result("push", "DEFAULT_BRANCH_PUSH_ALLOWED", remote, destination)
        return result("hold", "DEFAULT_BRANCH_PROTECTED", remote, destination)
    return result("push", "PUSH_ALLOWED", remote, destination)


def repo_identity(url: str) -> tuple[str, str] | None:
    """Normalize supported remote URLs without returning credentials."""
    if not isinstance(url, str) or not url or "\x00" in url:
        return None
    value = url.strip().removeprefix("git+")
    # Git accepts both native absolute paths and local file URLs. Preserve path
    # case and symlink spelling; do not infer identity for distinct filesystems.
    if os.path.isabs(value):
        return "local", os.path.normpath(value)
    if value.startswith("file://"):
        parsed = urlsplit(value)
        if parsed.netloc or parsed.query or parsed.fragment:
            return None
        path = url2pathname(parsed.path)
        if os.path.isabs(path):
            return "local", os.path.normpath(path)
        return None
    # SCP-like SSH syntax: user@host:owner/repo.git
    match = re.fullmatch(r"(?:[^@/:]+@)?([^/:]+):(.+)", value)
    if match and "://" not in value:
        host, path = match.groups()
    else:
        parsed = urlsplit(value)
        if parsed.query or parsed.fragment or (parsed.scheme == "file" and parsed.netloc):
            return None
        if parsed.scheme in {"http", "https", "ssh", "git"} and parsed.hostname:
            try:
                port = parsed.port
            except ValueError:
                return None
            defaults = {"http": 80, "https": 443, "ssh": 22, "git": 9418}
            host = parsed.hostname if port in {None, defaults[parsed.scheme]} else f"{parsed.hostname}:{port}"
            path = parsed.path
        elif parsed.scheme == "file":
            host, path = "file", parsed.path
        elif not parsed.scheme and value.startswith("/"):
            host, path = "file", value
        else:
            return None
    # Only GitHub establishes cross-transport repository identity here. Other
    # hosts may distinguish users, absolute paths, and a literal .git suffix.
    if host.lower() != "github.com":
        return "opaque", value
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if not host or not path:
        return None
    return host.lower(), path


def policy_repositories(policy: dict[str, Any]) -> set[tuple[str, str]]:
    """Validate an exact repository URL allowlist, never local paths or patterns."""
    key = "default_branch_push_repositories"
    excluded_key = "default_branch_push_excluded_repositories"
    flags = {"default_branch_push_private", "default_branch_push_oss"}
    if (not isinstance(policy, dict) or key not in policy or
            set(policy) - {key, excluded_key} - flags or not isinstance(policy[key], list) or
            not isinstance(policy.get(excluded_key, []), list) or
            any(type(policy.get(flag, False)) is not bool for flag in flags)):
        raise ValueError("invalid push policy")
    identities = set()
    for url in policy[key] + policy.get(excluded_key, []):
        if not isinstance(url, str) or not url or any(c.isspace() or c in "*?[]" for c in url):
            raise ValueError("invalid repository URL")
        value = url.removeprefix("git+")
        if "://" in value:
            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https", "ssh", "git"} or not parsed.hostname or not parsed.path.strip("/"):
                raise ValueError("invalid repository URL")
        elif not re.fullmatch(r"[^@/:]+@[^/:]+:.+", value):
            raise ValueError("invalid repository URL")
        identity = repo_identity(url)
        if identity is None:
            raise ValueError("invalid repository URL")
        if url in policy[key]:
            identities.add(identity)
    return identities


def load_policy(path: Path) -> dict[str, Any]:
    """Return invalid policy state on read/parse failure without exposing contents."""
    try:
        policy = json.loads(path.read_text(encoding="utf-8"))
        policy_repositories(policy)
        return policy
    except (OSError, UnicodeError, TypeError, ValueError):
        return {}


def run_git(repo: Path, *args: str, required: bool = True) -> str | None:
    completed = subprocess.run(["git", "-C", str(repo), *args], text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    if completed.returncode and required:
        raise RuntimeError("git command failed")
    return completed.stdout.rstrip("\n") if completed.returncode == 0 else None


def github_repo(url: str) -> tuple[str, str] | None:
    identity = repo_identity(url)
    if identity is None or identity[0] != "github.com":
        return None
    pieces = identity[1].split("/")
    return (pieces[0], pieces[1]) if len(pieces) == 2 and all(pieces) else None


def default_from_ls_remote(repo: Path, remote: str) -> str | None:
    completed = subprocess.run(["git", "-C", str(repo), "ls-remote", "--symref", remote, "HEAD"],
                               text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    if completed.returncode:
        return None
    match = re.search(r"^ref: refs/heads/([^\t\n]+)\tHEAD$", completed.stdout, re.MULTILINE)
    return match.group(1) if match else None


def remote_urls(repo: Path, name: str, push: bool = False) -> list[str]:
    args = ["remote", "get-url", "--all"]
    if push:
        args.append("--push")
    args.append(name)
    return (run_git(repo, *args, required=False) or "").splitlines()


def collect_state(repo: Path, need_metadata: bool = True) -> dict[str, Any]:
    """Collect only read-only git/GitHub data; errors become fail-closed state."""
    try:
        root = Path(run_git(repo, "rev-parse", "--show-toplevel") or "")
        branch = run_git(root, "symbolic-ref", "--quiet", "--short", "HEAD", required=False)
        names = (run_git(root, "remote") or "").splitlines()
        remotes: dict[str, Any] = {}
        for name in names:
            mirror_value = run_git(root, "config", "--get", f"remote.{name}.mirror", required=False)
            remotes[name] = {
                "fetch_urls": (run_git(root, "config", "--get-all", f"remote.{name}.url", required=False) or "").splitlines(),
                "push_urls": (run_git(root, "config", "--get-all", f"remote.{name}.pushurl", required=False) or "").splitlines(),
                "effective_fetch_urls": remote_urls(root, name),
                "effective_push_urls": remote_urls(root, name, push=True),
                "push_refspecs": (run_git(root, "config", "--get-all", f"remote.{name}.push", required=False) or "").splitlines(),
                "mirror": mirror_value == "true",
                "mirror_invalid": mirror_value not in {None, "true", "false"},
            }
        candidates = {"branch_push_remote": None, "remote_push_default": run_git(root, "config", "--get", "remote.pushDefault", required=False), "branch_remote": None}
        upstream = {"remote": None, "merge": None}
        if branch:
            candidates["branch_push_remote"] = run_git(root, "config", "--get", f"branch.{branch}.pushRemote", required=False)
            candidates["branch_remote"] = run_git(root, "config", "--get", f"branch.{branch}.remote", required=False)
            merges = (run_git(root, "config", "--get-all", f"branch.{branch}.merge", required=False) or "").splitlines()
            upstream = {"remote": candidates["branch_remote"], "merge": merges[0] if len(merges) == 1 else None,
                        "merge_values": merges}
        state: dict[str, Any] = {"current_branch": branch, "candidates": candidates, "remotes": remotes, "upstream": upstream,
                                 "repo": {"host": None, "visibility": None, "default_branch": None, "license_spdx_id": None, "metadata_error": False}}
        remote = choose_remote(state)
        if not need_metadata or remote not in remotes or not valid_name(remote):
            return state
        info = remotes[remote]
        urls = info.get("effective_push_urls") or info.get("effective_fetch_urls") or []
        if len(urls) != 1:
            return state
        target = github_repo(urls[0])
        if target is None:
            state["repo"]["host"] = "other"
            state["repo"]["default_branch"] = default_from_ls_remote(root, remote)
            return state
        state["repo"]["host"] = "github.com"
        completed = subprocess.run(["gh", "api", "--hostname", "github.com", f"repos/{target[0]}/{target[1]}"], text=True, encoding="utf-8",
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
        if completed.returncode:
            state["repo"]["metadata_error"] = True
        else:
            data = json.loads(completed.stdout)
            if not isinstance(data, dict):
                raise ValueError("invalid GitHub metadata")
            private = data.get("private")
            state["repo"]["visibility"] = "private" if private is True else "public" if private is False else None
            state["repo"]["default_branch"] = data.get("default_branch")
            license_data = data.get("license") or {}
            if not isinstance(license_data, dict):
                raise ValueError("invalid GitHub license metadata")
            state["repo"]["license_spdx_id"] = license_data.get("spdx_id")
        if not state["repo"]["default_branch"]:
            state["repo"]["default_branch"] = default_from_ls_remote(root, remote)
        return state
    except (OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError):
        return {"collection_error": True}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Decide whether to make a normal, single-branch push without changing Git state.",
        epilog=("Outputs JSON decision (push/hold/ask), reason, remote and destination. "
                "All decisions exit 0: inspect decision, not the exit status. "
                "Only push includes push_argv; execute it in the target repository. "
                "For hold report the reason; for ask resolve the missing information. "
                "Normal Git hooks and history protections still apply."))
    parser.add_argument("repo", nargs="?", default=".", help="target repository (default: current directory)")
    parser.add_argument("--user-intent", choices=("auto", "push", "hold"), default="auto",
                        help="explicit user push/hold instruction; default auto applies repository and branch policy")
    parser.add_argument("--temporary", action="store_true",
                        help="documented temporary-save intent, not inferred from a WIP subject; auto holds the push")
    parser.add_argument("--oss", choices=("auto", "yes", "no"), default="auto",
                        help="explicit OSS/non-OSS classification; auto uses hosting license metadata")
    parser.add_argument("--policy", type=Path,
                        help="environment-supplied default-branch push policy JSON; never omit a configured file")
    args = parser.parse_args()
    need_metadata = args.user_intent == "auto" and not args.temporary
    policy = load_policy(args.policy) if need_metadata and args.policy is not None else None
    output = decide(collect_state(Path(args.repo).resolve(), need_metadata), args.user_intent, args.temporary, args.oss, policy)
    if output["decision"] == "push":
        # Git owns reference/upstream facts. The lifecycle service independently
        # enforces task acceptance and exact merge identity before invoking push.
        legacy = Path(run_git(Path(args.repo), 'rev-parse', '--path-format=absolute',
                              '--git-common-dir') or '') / 'agent-branches/state.json'
        if legacy.exists():
            output = result('hold', 'LEGACY_CONSUMER: use its pinned preflight until explicit migration')
    print(json.dumps(output, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
