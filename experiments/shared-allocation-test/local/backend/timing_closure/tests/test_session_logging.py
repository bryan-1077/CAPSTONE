import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from session_log_utils import collect_legacy_logs, run_with_session_logging


BACKEND = Path(__file__).resolve().parents[2]


class SessionLoggingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.logs = self.root / "logs"
        self.logs.mkdir()
        self.parser = self.root / "parse_full_session_log.py"
        shutil.copyfile(BACKEND / "parse_full_session_log.py", self.parser)

    def run_session(self, callback):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return run_with_session_logging(
                callback,
                log_path=self.root / "full_session.log",
                parser_script_path=self.parser,
                parser_outdir=self.logs,
            )

    def test_collection_preserves_current_logs_and_legacy_sidecars(self):
        (self.logs / "full_session.log").write_text("current")
        (self.root / "full_session.log").write_text("old")
        (self.root / "full_session.log.state.json").write_text("{}")
        staging = self.root / ".timing_closure_remote" / "run"
        staging.mkdir(parents=True)
        (staging / "session.log").write_text("downloaded")
        (self.root / "app.py").write_text("# source")
        first = collect_legacy_logs(self.root)
        self.assertEqual((self.logs / "full_session.log").read_text(), "current")
        archived = next(path for path in first if path.name == "full_session.log")
        self.assertEqual(archived.read_text(), "old")
        self.assertEqual(Path(str(archived) + ".state.json").read_text(), "{}")
        self.assertEqual((archived.parent / ".timing_closure_remote" / "run" / "session.log").read_text(), "downloaded")
        self.assertEqual(collect_legacy_logs(self.root), [])
        (self.root / "full_session.log").write_text("another run")
        second = collect_legacy_logs(self.root)
        self.assertNotEqual(first[0].parent, second[0].parent)
        self.assertEqual(archived.read_text(), "old")
        self.assertTrue((self.root / "app.py").exists())

    def test_success_writes_transcript_state_and_parsed_logs_inside_logs(self):
        def flow():
            print("flow output")
            (self.root / "helper.log").write_text("helper output")
            return {"history": [], "current_stage": "done"}
        result = self.run_session(flow)
        self.assertEqual(result["current_stage"], "done")
        self.assertIn("flow output", (self.logs / "full_session.log").read_text())
        self.assertEqual(json.loads((self.logs / "full_session.log.state.json").read_text()), result)
        self.assertTrue((self.logs / "prep_logs" / "stdout.log").exists())
        self.assertFalse((self.root / "full_session.log").exists())
        self.assertFalse((self.root / "helper.log").exists())
        self.assertEqual(len(list((self.logs / "legacy").rglob("helper.log"))), 1)

    def test_failed_and_interrupted_runs_still_parse_and_collect_logs(self):
        for error in (RuntimeError("failed"), KeyboardInterrupt(), SystemExit(7)):
            with self.subTest(error=type(error).__name__):
                def flow():
                    print("before failure")
                    (self.root / "helper.log").write_text("failure details")
                    raise error
                expected = KeyboardInterrupt if isinstance(error, KeyboardInterrupt) else SystemExit
                with self.assertRaises(expected) as caught:
                    self.run_session(flow)
                if isinstance(error, SystemExit):
                    self.assertEqual(caught.exception.code, 7)
                self.assertIn("before failure", (self.logs / "full_session.log").read_text())
                self.assertTrue((self.logs / "prep_logs" / "stdout.log").exists())
                self.assertFalse((self.root / "helper.log").exists())


if __name__ == "__main__":
    unittest.main()
