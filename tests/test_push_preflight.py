"""Offline policy fixtures and real-Git collection tests."""
import copy
import contextlib
import io
import importlib.util
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
from workspace_lifecycle import push as preflight

BASE = {
    "current_branch": "feature",
    "candidates": {"branch_push_remote": None, "remote_push_default": None, "branch_remote": None},
    "remotes": {"origin": {"fetch_urls": ["https://github.com/example/project.git"],
                           "push_urls": [], "push_refspecs": [], "mirror": False}},
    "upstream": {"remote": None, "merge": None},
    "repo": {"host": "github", "visibility": "private", "default_branch": "main",
             "license_spdx_id": None, "metadata_error": False},
}


class PolicyTests(unittest.TestCase):
    def test_default_branch_allowlist(self):
        state = copy.deepcopy(BASE)
        state["current_branch"] = "main"
        policy = {"default_branch_push_repositories": ["git@github.com:example/project.git"]}
        for configured in (None, {"default_branch_push_repositories": []},
                           {"default_branch_push_repositories": ["https://github.com/other/project"]}):
            self.assertEqual(preflight.decide(state, policy=configured)["reason"], "DEFAULT_BRANCH_PROTECTED")
        allowed = preflight.decide(state, policy=policy)
        self.assertEqual(allowed["reason"], "DEFAULT_BRANCH_PUSH_ALLOWED")
        self.assertEqual(allowed["push_argv"], ["git", "push", "origin", "HEAD:refs/heads/main"])
        state["current_branch"] = "feature"
        self.assertEqual(preflight.decide(state, policy=policy)["reason"], "PUSH_ALLOWED")
        state["upstream"] = {"remote": "origin", "merge": "refs/heads/main"}
        self.assertEqual(preflight.decide(state, policy=policy)["destination"], "main")
        state["current_branch"] = "trunk"
        state["repo"]["default_branch"] = "trunk"
        state["upstream"] = {}
        self.assertEqual(preflight.decide(state, policy=policy)["reason"], "DEFAULT_BRANCH_PUSH_ALLOWED")
        state["remotes"]["fork"] = {"fetch_urls": ["https://github.com/fork/project.git"]}
        state["candidates"]["branch_push_remote"] = "fork"
        self.assertEqual(preflight.decide(state, policy=policy)["reason"], "DEFAULT_BRANCH_PROTECTED")

    def test_allowlist_preserves_existing_restrictions(self):
        policy = {"default_branch_push_repositories": ["https://github.com/example/project"]}
        fixtures = json.loads((ROOT / "tests/fixtures/push_preflight.json").read_text())
        for fixture in fixtures:
            if fixture["reason"] == "DEFAULT_BRANCH_PROTECTED":
                continue
            with self.subTest(fixture=fixture["name"]):
                state = copy.deepcopy(BASE)
                for key, value in fixture.get("state", {}).items():
                    if isinstance(value, dict):
                        state[key].update(value)
                    else:
                        state[key] = value
                actual = preflight.decide(state, policy=policy, **fixture.get("inputs", {}))
                self.assertEqual(actual["reason"], fixture["reason"])
                self.assertEqual(actual["decision"], fixture["decision"])

    def test_allowlist_uses_existing_repository_identity(self):
        state = copy.deepcopy(BASE)
        state["current_branch"] = "main"
        state["remotes"]["origin"]["fetch_urls"] = ["https://example.com/team/repo"]
        for url, reason in (
            ("git+https://example.com/team/repo", "DEFAULT_BRANCH_PUSH_ALLOWED"),
            ("ssh://git@example.com/team/repo", "DEFAULT_BRANCH_PROTECTED"),
            ("https://example.com/team/repo.git", "DEFAULT_BRANCH_PROTECTED"),
        ):
            with self.subTest(url=url):
                policy = {"default_branch_push_repositories": [url]}
                self.assertEqual(preflight.decide(state, policy=policy)["reason"], reason)

    def test_category_grants_and_exclusions(self):
        state = copy.deepcopy(BASE)
        state["current_branch"] = "main"
        policy = {"default_branch_push_repositories": [],
                  "default_branch_push_private": True, "default_branch_push_oss": True}
        self.assertEqual(preflight.decide(state, policy=policy)["reason"], "DEFAULT_BRANCH_PUSH_ALLOWED")
        state["repo"].update(visibility="public", license_spdx_id="MIT")
        self.assertEqual(preflight.decide(state, policy=policy)["reason"], "DEFAULT_BRANCH_PUSH_ALLOWED")
        policy["default_branch_push_oss"] = False
        self.assertEqual(preflight.decide(state, policy=policy)["reason"], "DEFAULT_BRANCH_PROTECTED")
        policy["default_branch_push_oss"] = True
        state["repo"]["license_spdx_id"] = None
        self.assertEqual(preflight.decide(state, policy=policy)["reason"], "UNKNOWN_OSS_LICENSE")
        self.assertEqual(preflight.decide(state, policy=policy, oss="yes")["reason"], "DEFAULT_BRANCH_PUSH_ALLOWED")
        self.assertEqual(preflight.decide(state, policy=policy, oss="no")["reason"], "NON_OSS_PUBLIC_REPOSITORY")
        state["repo"]["visibility"] = "private"
        policy["default_branch_push_private"] = False
        self.assertEqual(preflight.decide(state, policy=policy)["reason"], "DEFAULT_BRANCH_PROTECTED")
        policy["default_branch_push_private"] = True
        policy["default_branch_push_repositories"] = ["https://github.com/example/project"]
        policy["default_branch_push_excluded_repositories"] = ["git@github.com:example/project.git"]
        self.assertEqual(preflight.decide(state, policy=policy)["reason"], "DEFAULT_BRANCH_PUSH_EXCLUDED")
        self.assertEqual(preflight.decide(state, policy=policy, user_intent="push")["reason"], "PUSH_ALLOWED")
        state["current_branch"] = "feature"
        self.assertEqual(preflight.decide(state, policy=policy)["reason"], "PUSH_ALLOWED")
        state["upstream"] = {"remote": "origin", "merge": "refs/heads/main"}
        self.assertEqual(preflight.decide(state, policy=policy)["reason"], "DEFAULT_BRANCH_PUSH_EXCLUDED")
        policy["default_branch_push_excluded_repositories"] = ["https://github.com/other/project"]
        self.assertEqual(preflight.decide(state, policy=policy)["reason"], "DEFAULT_BRANCH_PUSH_ALLOWED")

    def test_category_grants_preserve_existing_restrictions(self):
        policy = {"default_branch_push_repositories": [],
                  "default_branch_push_private": True, "default_branch_push_oss": True}
        fixtures = json.loads((ROOT / "tests/fixtures/push_preflight.json").read_text())
        for fixture in fixtures:
            if fixture["reason"] == "DEFAULT_BRANCH_PROTECTED":
                continue
            with self.subTest(fixture=fixture["name"]):
                state = copy.deepcopy(BASE)
                for key, value in fixture.get("state", {}).items():
                    if isinstance(value, dict):
                        state[key].update(value)
                    else:
                        state[key] = value
                actual = preflight.decide(state, policy=policy, **fixture.get("inputs", {}))
                self.assertEqual(actual["reason"], fixture["reason"])
                self.assertEqual(actual["decision"], fixture["decision"])

    def test_invalid_policy_and_explicit_intent(self):
        invalid = [[], {}, {"default_branch_push_repositories": None},
                   {"default_branch_push_repositories": [], "extra": True}]
        invalid += [{"default_branch_push_repositories": [url]} for url in
                    (None, "origin", "/tmp/repo", "file:///tmp/repo", "../repo", "C:/repo",
                     "https://github.com/example/*", "https://github.com/example/pro?ject",
                     "https://[broken", "https://github.com", "git@github.com:example/[repo]",
                     "https://github.com/example/repo#fragment")]
        invalid += [{"default_branch_push_repositories": [], key: value} for key, value in
                    (("default_branch_push_private", "true"), ("default_branch_push_oss", 1),
                     ("default_branch_push_oss", None), ("default_branch_push_excluded_repositories", "origin"),
                     ("default_branch_push_excluded_repositories", ["*"]))]
        for policy in invalid:
            with self.subTest(policy=policy):
                self.assertEqual(preflight.decide(BASE, policy=policy)["reason"], "INVALID_PUSH_POLICY")
                self.assertEqual(preflight.decide(BASE, policy=policy, user_intent="hold")["reason"], "USER_HOLD")
                self.assertEqual(preflight.decide(BASE, policy=policy, temporary=True)["reason"], "TEMPORARY_COMMIT")
                self.assertEqual(preflight.decide(BASE, policy=policy, user_intent="push")["reason"], "PUSH_ALLOWED")

    def test_cli_policy_loading(self):
        state = copy.deepcopy(BASE)
        state["current_branch"] = "main"
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(["git", "init", directory], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            path = Path(directory) / "policy.json"
            def invoke(*args):
                output = io.StringIO()
                with patch.object(sys, "argv", ["push_preflight.py", directory, "--policy", str(path), *args]), \
                     patch.object(preflight, "collect_state", return_value=state), contextlib.redirect_stdout(output):
                    code = preflight.main()
                decided = json.loads(output.getvalue())
                self.assertEqual(code, {"push": 0, "hold": 10, "ask": 11}[decided["decision"]], decided)
                return decided
            self.assertEqual(invoke()["reason"], "INVALID_PUSH_POLICY")
            for contents in (b"{", b"null", b"\xff", b'{"default_branch_push_repositories": [false]}'):
                path.write_bytes(contents)
                self.assertEqual(invoke()["reason"], "INVALID_PUSH_POLICY")
            path.write_text('{"default_branch_push_repositories": []}', encoding="utf-8")
            self.assertEqual(invoke()["reason"], "DEFAULT_BRANCH_PROTECTED")
            path.write_text('{"default_branch_push_repositories": ["https://github.com/example/project"]}', encoding="utf-8")
            self.assertEqual(invoke()["reason"], "DEFAULT_BRANCH_PUSH_ALLOWED")
            with patch.object(Path, "read_text", side_effect=PermissionError):
                self.assertEqual(invoke()["reason"], "INVALID_PUSH_POLICY")
                self.assertEqual(invoke("--user-intent", "push")["reason"], "PUSH_ALLOWED")

    def test_distinct_remote_endpoints(self):
        for fetch, push in [
            ("https://github.com/example/project.git", "ssh://git@github.com:2222/example/project.git"),
            ("file:///srv/Project.git", "file:///srv/project.git"),
            ("file:///srv/project.git", "file://other/srv/project.git"),
            ("file:///srv/project.git", "file:///srv/project"),
            ("https://github.com/a/b.git?route=one", "https://github.com/a/b.git?route=two"),
            ("ssh://one@host/project.git", "ssh://two@host/project.git"),
        ]:
            with self.subTest(push=push):
                state = copy.deepcopy(BASE)
                state["remotes"]["origin"]["fetch_urls"] = [fetch]
                state["remotes"]["origin"]["push_urls"] = [push]
                self.assertEqual(preflight.decide(state, user_intent="push")["decision"], "ask")

    def test_real_git_remote_rewrites(self):
        with tempfile.TemporaryDirectory() as directory:
            def git(*args):
                return subprocess.run(["git", "-C", directory, *args], check=True,
                                      text=True, capture_output=True).stdout
            git("init", "-q")
            git("symbolic-ref", "HEAD", "refs/heads/feature")
            git("remote", "add", "origin", "https://github.com/example/project.git")
            native_run = subprocess.run
            def read_only_run(argv, **kwargs):
                if argv[0] == "gh":
                    return subprocess.CompletedProcess(argv, 0, json.dumps({
                        "private": True, "default_branch": "main", "license": None,
                    }), "")
                self.assertNotIn("push", argv[1:])
                return native_run(argv, **kwargs)
            with patch.object(preflight.subprocess, "run", side_effect=read_only_run):
                self.assertEqual(preflight.decide(preflight.collect_state(Path(directory)))["decision"], "push")
            git("config", "url.https://github.com/other/.pushInsteadOf", "https://github.com/example/")
            with patch.object(preflight.subprocess, "run", side_effect=read_only_run):
                result = preflight.decide(preflight.collect_state(Path(directory)))
            self.assertEqual(result["decision"], "ask", result)

    def test_collected_settings_match_git_config_lookups(self):
        with tempfile.TemporaryDirectory() as directory:
            def git(*args, check=True):
                return subprocess.run(["git", "-C", directory, *args], check=check,
                                      text=True, capture_output=True).stdout
            git("init", "-q")
            git("symbolic-ref", "HEAD", "refs/heads/Topic.v2")
            included = Path(directory) / "included.config"
            included.write_text('[remote "origin"]\n\tpushurl = https://example.invalid/included.git\n')
            config = Path(git("rev-parse", "--git-path", "config").strip())
            if not config.is_absolute():
                config = Path(directory) / config
            with config.open("a") as stream:
                stream.write('[remote "origin"]\n\turl = https://example.invalid/a.git\n\turl = https://example.invalid/b.git\n'
                             '\tMirror\n\tpush = +refs/heads/*:refs/heads/*\n'
                             '[remote "Origin"]\n\turl = https://example.invalid/upper.git\n\tmirror = false\n'
                             '[remote "empty"]\n\turl =\n'
                             f'[include]\n\tpath = {included.as_posix()}\n'
                             '[remote]\n\tPushDefault = Origin\n'
                             '[branch "Topic.v2"]\n\tremote = origin\n\tmerge = refs/heads/one\n\tmerge = refs/heads/two\n'
                             '\tPushRemote = empty\n')
            state = preflight.collect_state(Path(directory), need_metadata=False)
            def get(key):
                found = subprocess.run(["git", "-C", directory, "config", "--get", key], text=True, capture_output=True)
                return found.stdout.rstrip("\n") if found.returncode == 0 else None
            get_all = lambda key: git("config", "--get-all", key, check=False).rstrip("\n").splitlines()
            self.assertEqual(sorted(state["remotes"]), ["Origin", "empty", "origin"])
            for name, remote in state["remotes"].items():
                self.assertEqual(remote["fetch_urls"], get_all(f"remote.{name}.url"))
                self.assertEqual(remote["push_urls"], get_all(f"remote.{name}.pushurl"))
                self.assertEqual(remote["push_refspecs"], get_all(f"remote.{name}.push"))
                mirror = get(f"remote.{name}.mirror")
                self.assertEqual((remote["mirror"], remote["mirror_invalid"]),
                                 (mirror == "true", mirror not in {None, "true", "false"}))
            self.assertEqual(state["remotes"]["origin"]["push_urls"], ["https://example.invalid/included.git"])
            self.assertTrue(state["remotes"]["origin"]["mirror_invalid"])
            self.assertEqual(state["candidates"], {"branch_push_remote": "empty", "remote_push_default": "Origin",
                                                   "branch_remote": "origin"})
            self.assertEqual(state["upstream"], {"remote": "origin", "merge": None,
                                                 "merge_values": ["refs/heads/one", "refs/heads/two"]})

    def test_github_metadata_utf8_under_cp932_locale(self):
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(["git", "init", "-q", directory], check=True)
            subprocess.run(["git", "-C", directory, "remote", "add", "origin",
                            "https://github.com/example/project.git"], check=True)
            native_run = subprocess.run
            metadata = json.dumps({"private": True, "default_branch": "main",
                                   "description": "日本語の説明", "license": None},
                                  ensure_ascii=False).encode("utf-8")

            def read_only_run(argv, **kwargs):
                if argv[0] == "gh":
                    argv = [sys.executable, "-c",
                            f"import sys; sys.stdout.buffer.write({metadata!r})"]
                # Simulate a non-UTF-8 default without depending on private
                # subprocess helpers, which differ across supported Pythons.
                if kwargs.get("text") and kwargs.get("encoding") is None:
                    kwargs["encoding"] = "cp932"
                return native_run(argv, **kwargs)

            with patch.object(preflight.subprocess, "run", side_effect=read_only_run):
                state = preflight.collect_state(Path(directory))
                self.assertNotIn("collection_error", state)
                self.assertEqual(state["repo"]["visibility"], "private")
                self.assertEqual(state["repo"]["default_branch"], "main")
                metadata = b"\xff"
                self.assertEqual(preflight.collect_state(Path(directory)), {"collection_error": True})

    def test_policy_fixtures(self):
        fixtures = json.loads((ROOT / "tests/fixtures/push_preflight.json").read_text())
        for fixture in fixtures:
            with self.subTest(fixture=fixture["name"]):
                state = copy.deepcopy(BASE)
                for key, value in fixture.get("state", {}).items():
                    if isinstance(value, dict):
                        state[key].update(value)
                    else:
                        state[key] = value
                before = copy.deepcopy(state)
                result = preflight.decide(state, **fixture.get("inputs", {}))
                self.assertEqual(result["decision"], fixture["decision"], result)
                self.assertEqual(result["reason"], fixture["reason"])
                self.assertEqual(state, before)
                if result["decision"] == "push":
                    self.assertEqual(result["remote"], fixture.get("remote", "origin"))
                    self.assertEqual(result["destination"], fixture.get("destination", "feature"))
                    self.assertEqual(result["push_argv"], ["git", "push", result["remote"],
                                    "HEAD:refs/heads/" + result["destination"]])
                else:
                    self.assertFalse(result.get("push_argv"))

    def test_cli_local_repository_no_remote(self):
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(["git", "init", "-q", directory], check=True)
            subprocess.run(["git", "-C", directory, "symbolic-ref", "HEAD", "refs/heads/feature"], check=True)
            result = subprocess.run([sys.executable, "-m", "workspace_lifecycle.push", directory],
                                    text=True, capture_output=True)
            self.assertEqual((result.returncode, json.loads(result.stdout)["decision"]), (11, "ask"))
            held = subprocess.run([sys.executable, "-m", "workspace_lifecycle.push", directory,
                                   "--user-intent", "hold"], text=True, capture_output=True)
            self.assertEqual((held.returncode, json.loads(held.stdout)["decision"]), (10, "hold"))

    def test_shell_and_list_pushes_only_after_push_decision(self):
        with tempfile.TemporaryDirectory() as directory:
            work, bare = Path(directory, "work"), Path(directory, "remote.git")
            def git(*args):
                return subprocess.run(["git", *args], check=True, text=True, capture_output=True).stdout
            git("init", "-q", "--bare", str(bare))
            git("init", "-q", str(work))
            git("-C", str(work), "symbolic-ref", "HEAD", "refs/heads/feature")
            git("-C", str(work), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                "commit", "-q", "--allow-empty", "-m", "work")
            git("-C", str(work), "remote", "add", "origin", bare.as_uri())
            # shell=True is cmd.exe on Windows and /bin/sh elsewhere.
            quote = subprocess.list2cmdline if sys.platform == "win32" else shlex.join
            decide = [sys.executable, "-m", "workspace_lifecycle.push", str(work)]
            push = quote(["git", "-C", str(work), "push", "-q", "origin", "HEAD:refs/heads/feature"])
            for args, decision, code in [((), "ask", 11), (("--user-intent", "hold"), "hold", 10),
                                         (("--temporary",), "hold", 10), (("--user-intent", "push"), "push", 0)]:
                with self.subTest(decision=decision, args=args):
                    shell = subprocess.run(quote([*decide, *args]) + " && " + push, shell=True,
                                           text=True, capture_output=True, cwd=directory)
                    self.assertEqual((shell.returncode, json.loads(shell.stdout)["decision"]), (code, decision),
                                     shell.stderr)
                    pushed = git("--git-dir", str(bare), "for-each-ref", "--format=%(refname)")
                    self.assertEqual(pushed.split(), ["refs/heads/feature"] if decision == "push" else [])


if __name__ == "__main__":
    unittest.main()
