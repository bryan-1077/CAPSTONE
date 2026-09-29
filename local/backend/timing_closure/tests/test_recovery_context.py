import tempfile
import unittest
from pathlib import Path

from timing_closure.recovery_context import build_recovery_evidence, REPORTS


class ReportSSH:
    def __init__(self, missing=None):
        self.missing = missing
        self.fetched = []

    def fetch_file(self, remote, local):
        self.fetched.append(remote)
        if self.missing and remote.endswith(self.missing):
            return {"ok": False, "error": "not found"}
        Path(local).write_text(remote + "\n" + "setup hold optimization result\n" * 2000)
        return {"ok": True}


class RecoveryContextTests(unittest.TestCase):
    def bundle(self, ssh, attempts=()):
        with tempfile.TemporaryDirectory() as staging:
            return build_recovery_evidence(
                ssh, "/project", staging,
                {"attempt": 2, "outdir": "best_build"}, list(attempts))

    def test_reports_are_attributed_and_bounded(self):
        ssh = ReportSSH()
        bundle = self.bundle(ssh)
        self.assertEqual(len(ssh.fetched), 5)
        for kind, report in bundle["best_build"]["reports"].items():
            self.assertEqual(report["source_build"], "best_build")
            self.assertEqual(report["status"], "included")
            self.assertTrue(report["truncated"])
            self.assertLessEqual(len(report["content"]), REPORTS[kind][1])
            self.assertIn("/project/best_build/", report["content"])

    def test_missing_hold_is_explicit(self):
        bundle = self.bundle(ReportSSH("hold_postroute.rpt"))
        hold = bundle["best_build"]["reports"]["hold"]
        self.assertEqual(hold["status"], "missing")
        self.assertEqual(hold["error"], "not found")
        self.assertNotIn("content", hold)

    def test_only_two_recent_rejected_builds_and_their_results(self):
        attempts = [{"attempt": i, "outdir": f"rejected_{i}", "kind": "ai_resize",
                     "status": "hold_violated", "hold_wns_ns": -0.042,
                     "ai_plan": {"actions": []}} for i in range(3, 7)]
        bundle = self.bundle(ReportSSH(), attempts)
        self.assertEqual(bundle["omitted_rejected_builds"], 2)
        candidates = bundle["rejected_candidates"]
        self.assertEqual([x["source_build"] for x in candidates], ["rejected_5", "rejected_6"])
        self.assertEqual(candidates[0]["result"]["hold_wns_ns"], -0.042)
        self.assertEqual(candidates[0]["reports"]["hold"]["source_build"], "rejected_5")
