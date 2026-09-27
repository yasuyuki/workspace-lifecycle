import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from workspace_lifecycle.errors import LifecycleError
from workspace_lifecycle import reclamation


@unittest.skipUnless(os.name == "posix" and Path("/proc/self/fdinfo").is_dir(),
                     "Linux descriptor-bound reclamation")
class ReclamationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="reclamation ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "payload"
        self.root.mkdir()
        (self.root / "one").write_bytes(b"one")
        (self.root / "dir").mkdir()
        (self.root / "dir" / "two").write_bytes(b"two")
        self.manifest = [
            ["dir", "dir", ""],
            ["dir/two", "file", hashlib.sha256(b"two").hexdigest()],
            ["one", "file", hashlib.sha256(b"one").hexdigest()],
        ]
        info = self.root.stat()
        self.identity = [info.st_dev, info.st_ino]

    @staticmethod
    def persist(progress, durable):
        durable.clear()
        durable.update(json.loads(json.dumps(progress)))

    def test_capture_is_separate_and_rejects_unknown_and_hardlink(self):
        original = json.loads(json.dumps(self.manifest))
        snapshot = reclamation.capture(self.root, self.manifest, self.identity)
        self.assertEqual(self.manifest, original)
        self.assertEqual(set(snapshot["members"]), {"one", "dir", "dir/two"})
        (self.root / "unknown").write_text("hold")
        with self.assertRaisesRegex(LifecycleError, "unknown"):
            reclamation.capture(self.root, self.manifest, self.identity)
        (self.root / "unknown").unlink()
        os.link(self.root / "one", self.root / "alias")
        self.manifest.append(["alias", "file", hashlib.sha256(b"one").hexdigest()])
        with self.assertRaisesRegex(LifecycleError, "hard-linked"):
            reclamation.capture(self.root, self.manifest, self.identity)

    def test_capture_rejects_nested_git_even_when_manifested(self):
        nested = self.root / "dir" / ".git"
        nested.mkdir()
        self.manifest.append(["dir/.git", "dir", ""])
        with self.assertRaisesRegex(LifecycleError, "nested repository"):
            reclamation.capture(self.root, self.manifest, self.identity)

    def test_remove_persists_intents_and_removes_exact_tree(self):
        progress = {}; durable = {}; saves = []
        def persist():
            self.persist(progress, durable); saves.append(json.loads(json.dumps(progress)))
        result = reclamation.remove_tree(self.root, self.manifest, self.identity, progress, persist)
        self.assertFalse(self.root.exists())
        self.assertTrue(result["removed"])
        self.assertEqual(result["observed_removals"], 4)
        for name, member in durable["members"].items():
            self.assertTrue(member["intent"], name)
            self.assertEqual(member["status"], "removed")
        self.assertTrue(durable["root"]["remove_intent"])

    def test_crash_after_member_rename_resumes_from_durable_intent(self):
        progress = {}; durable = {}; crashed = {"done": False}
        def persist(): self.persist(progress, durable)
        def checkpoint(event, path):
            if event == "after-root-rename" and not crashed["done"]:
                crashed["done"] = True
                raise RuntimeError("killed")
        with patch.object(reclamation, "_checkpoint", side_effect=checkpoint):
            with self.assertRaisesRegex(RuntimeError, "killed"):
                reclamation.remove_tree(self.root, self.manifest, self.identity, progress, persist)
        resumed = json.loads(json.dumps(durable))
        result = reclamation.remove_tree(self.root, self.manifest, self.identity, resumed,
                                         lambda: self.persist(resumed, durable))
        self.assertTrue(result["removed"])
        self.assertFalse(self.root.exists())

    def test_crash_before_member_remove_resumes_quarantined_name(self):
        progress = {}; durable = {}; crashed = {"done": False}
        def persist(): self.persist(progress, durable)
        def checkpoint(event, path):
            if event == "before-member-remove" and not crashed["done"]:
                crashed["done"] = True
                raise RuntimeError("killed")
        with patch.object(reclamation, "_checkpoint", side_effect=checkpoint):
            with self.assertRaisesRegex(RuntimeError, "killed"):
                reclamation.remove_tree(self.root, self.manifest, self.identity, progress, persist)
        resumed = json.loads(json.dumps(durable))
        result = reclamation.remove_tree(self.root, self.manifest, self.identity, resumed,
                                         lambda: self.persist(resumed, durable))
        self.assertTrue(result["removed"])

    def _retry_across_mount_session(self, stop_at):
        progress = {}; durable = {}; crashed = {"done": False}
        def persist(): self.persist(progress, durable)
        def checkpoint(event, path):
            if event == stop_at and not crashed["done"]:
                crashed["done"] = True
                raise RuntimeError("restart")
        mount_id = reclamation._mount_id
        with patch.object(reclamation, "_checkpoint", side_effect=checkpoint), \
                patch.object(reclamation, "_mount_session", return_value=["previous-boot", 1, 2]), \
                patch.object(reclamation, "_mount_id", side_effect=lambda fd: "old-" + mount_id(fd)):
            with self.assertRaises(RuntimeError):
                reclamation.remove_tree(self.root, self.manifest, self.identity, progress, persist)
        resumed = json.loads(json.dumps(durable))
        original = json.loads(json.dumps(resumed))
        result = reclamation.remove_tree(self.root, self.manifest, self.identity, resumed,
                                         lambda: self.persist(resumed, durable))
        self.assertTrue(result["removed"])
        self.assertEqual(resumed['mount_session'], original['mount_session'])
        self.assertEqual(resumed['root_mount_id'], original['root_mount_id'])

    def test_retry_does_not_compare_raw_mount_ids_across_sessions(self):
        self._retry_across_mount_session('before-member-remove')

    def test_new_mount_session_before_any_member_intent_keeps_original_capture(self):
        self._retry_across_mount_session('after-root-rename')

    def test_replacement_after_capture_is_held(self):
        progress = {}; durable = {}
        calls = {"n": 0}
        def persist():
            self.persist(progress, durable)
            calls["n"] += 1
            # First persistence contains the exact capture, before root intent.
            if calls["n"] == 1:
                old = self.root / "one.old"
                (self.root / "one").rename(old)
                (self.root / "one").write_bytes(b"one")
                old.unlink()
        with self.assertRaisesRegex(LifecycleError, "identities changed"):
            reclamation.remove_tree(self.root, self.manifest, self.identity, progress, persist)
        self.assertTrue(any(Path(self.temporary.name).glob(".workspace-lifecycle-delete-*")))

    def test_original_path_replacement_at_last_checkpoint_is_held(self):
        progress = {}; durable = {}; replaced = {"done": False}
        def persist(): self.persist(progress, durable)
        def checkpoint(event, logical):
            if event != "before-member-remove" or replaced["done"]:
                return
            self.root.mkdir()
            (self.root / "replacement").write_text("unknown")
            replaced["done"] = True
        with patch.object(reclamation, "_checkpoint", side_effect=checkpoint):
            result = reclamation.remove_tree(self.root, self.manifest, self.identity, progress, persist)
        self.assertTrue(result["removed"])
        self.assertTrue(replaced["done"])
        self.assertEqual((self.root / "replacement").read_text(), "unknown")

    def test_missing_without_intent_is_refused(self):
        progress = reclamation.capture(self.root, self.manifest, self.identity)
        progress["root"] = {"identity": self.identity,
                            "tombstone": reclamation._root_tombstone(self.root, self.identity).name}
        for logical, member in progress["members"].items():
            member["tombstone"] = reclamation._tombstone(logical)
        (self.root / "one").unlink()
        with self.assertRaisesRegex(LifecycleError, "unknown or missing"):
            reclamation.remove_tree(self.root, self.manifest, self.identity, progress, lambda: None)

    def test_killed_process_after_root_rename_resumes(self):
        state = Path(self.temporary.name) / "progress.json"
        gate = Path(self.temporary.name) / "renamed"
        manifest = Path(self.temporary.name) / "manifest.json"
        manifest.write_text(json.dumps(self.manifest))
        code = r'''
import json, os, sys, time
from pathlib import Path
from workspace_lifecycle import reclamation
root, manifest_path, state_path, gate, dev, ino = sys.argv[1:]
manifest = json.loads(Path(manifest_path).read_text())
progress = json.loads(Path(state_path).read_text()) if Path(state_path).exists() else {}
def persist():
    temporary = Path(state_path + '.new')
    with temporary.open('w') as stream:
        json.dump(progress, stream); stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, state_path)
def checkpoint(event, path):
    if event == 'after-root-rename':
        Path(gate).write_text('ready')
        while True: time.sleep(.05)
reclamation._checkpoint = checkpoint
reclamation.remove_tree(Path(root), manifest, [int(dev), int(ino)], progress, persist)
'''
        child = subprocess.Popen([sys.executable, "-c", code, str(self.root), str(manifest),
                                  str(state), str(gate), *(str(value) for value in self.identity)],
                                 env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")})
        self.addCleanup(lambda: child.poll() is None and child.kill())
        deadline = time.monotonic() + 10
        while not gate.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(gate.exists(), "child did not reach post-rename checkpoint")
        child.kill(); child.wait(timeout=10)
        self.assertNotEqual(child.returncode, 0)
        progress = json.loads(state.read_text())
        durable = json.loads(state.read_text())
        result = reclamation.remove_tree(self.root, self.manifest, self.identity, progress,
                                         lambda: self.persist(progress, durable))
        self.assertTrue(result["removed"])
        self.assertFalse(self.root.exists())


@unittest.skipUnless(os.name == "nt", "native Windows handle reclamation")
class WindowsReclamationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="reclamation ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "payload"
        self.root.mkdir()
        (self.root / "one").write_bytes(b"one")
        (self.root / "dir").mkdir()
        (self.root / "dir" / "two").write_bytes(b"two")
        self.manifest = [["dir", "dir", ""],
                         ["dir/two", "file", hashlib.sha256(b"two").hexdigest()],
                         ["one", "file", hashlib.sha256(b"one").hexdigest()]]
        info = self.root.stat()
        self.identity = [info.st_dev, info.st_ino]

    @staticmethod
    def _persist(progress, durable):
        durable.clear(); durable.update(json.loads(json.dumps(progress)))

    def test_same_handle_removal(self):
        progress = reclamation.capture(self.root, self.manifest, self.identity)
        durable = json.loads(json.dumps(progress))
        result = reclamation.remove_tree(
            self.root, self.manifest, self.identity, progress,
            lambda: self._persist(progress, durable))
        self.assertTrue(result["removed"])
        self.assertFalse(self.root.exists())

    def test_incompatible_share_holds_bytes(self):
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                       ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                       wintypes.HANDLE)
        kernel.CreateFileW.restype = wintypes.HANDLE
        blocker = kernel.CreateFileW(str(self.root / "one"), 0x80000000, 0x1, None, 3, 0, None)
        self.assertNotEqual(blocker, wintypes.HANDLE(-1).value)
        try:
            with self.assertRaisesRegex(LifecycleError, "shared"):
                reclamation.capture(self.root, self.manifest, self.identity)
            self.assertEqual((self.root / "one").read_bytes(), b"one")
        finally:
            kernel.CloseHandle(blocker)


if __name__ == "__main__":
    unittest.main()
