"""The verification helper detects a wrong launch and never reports a pass without one."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "skills/verify-workspace-lifecycle/scripts/verify.py"


class VerificationSkillTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.evidence = self.base / "evidence"

    def verify(self, repo, *extra):
        env = {k: v for k, v in os.environ.items()
               if k != "PYTHONPATH" and not k.startswith("WORKSPACE_LIFECYCLE_")}
        proc = subprocess.run([sys.executable, str(HELPER), "--repo", str(repo),
                               "--evidence-root", str(self.evidence), *extra],
                              capture_output=True, text=True, encoding="utf-8", env=env)
        path = Path(json.loads(proc.stdout)["result"])
        return proc.returncode, json.loads(path.read_text(encoding="utf-8")), path.parent

    def assertCleaned(self, result, evidence):
        self.assertTrue(result["cleanup"]["ok"])
        self.assertFalse(Path(result["cleanup"]["scratch"]).exists())
        self.assertTrue(result["evidence_retained"])
        for item in result["artifacts"]:
            self.assertTrue((evidence / item).exists(), item)

    def test_checkout_passes_and_keeps_evidence(self):
        code, result, evidence = self.verify(ROOT)
        self.assertEqual((code, result["status"]), (0, "pass"), result.get("error"))
        self.assertTrue(all(item["ok"] for item in result["assertions"]))
        self.assertIn("session-first.json", result["artifacts"])
        self.assertCleaned(result, evidence)

    def test_missing_notice_with_expected_exit_fails(self):
        copy = self.base / "faulty"
        shutil.copytree(ROOT, copy, ignore=shutil.ignore_patterns(".git", "build", "dist", "*.egg-info", "__pycache__"))
        cli = copy / "src/workspace_lifecycle/cli.py"
        text = cli.read_text(encoding="utf-8")
        notice = "        _notify([{'lifecycle': 'unmanaged', 'repo': str(repo), 'reason': reason,\n"
        self.assertIn(notice, text)
        cli.write_text(text.replace(notice, "        ([{'lifecycle': 'unmanaged', 'repo': str(repo), 'reason': reason,\n"),
                       encoding="utf-8")
        code, result, evidence = self.verify(copy)
        self.assertEqual((code, result["status"], result["target"]["revision"]), (1, "fail", "unknown"))
        failed = [item for item in result["assertions"] if not item["ok"]]
        self.assertEqual([item["name"] for item in failed], ["unbound notice"])
        self.assertEqual(result["commands"][-1]["exit_code"], 41)
        self.assertCleaned(result, evidence)

    def test_wrong_checkout_blocks_and_doctor_only_is_not_run(self):
        code, result, _ = self.verify(self.base)
        self.assertEqual((code, result["status"], result["feature_status"]), (2, "blocked", "not-run"))
        self.assertEqual(result["commands"], [])
        code, result, _ = self.verify(ROOT, "--doctor-only")
        self.assertEqual((code, result["status"], result["feature_status"]), (3, "not-run", "not-run"))
        self.assertIsNone(result["cleanup"]["scratch"])


if __name__ == "__main__":
    unittest.main()
