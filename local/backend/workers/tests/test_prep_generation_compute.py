import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from workers.workers import _build_prepare_command


class PrepGenerationComputeTests(unittest.TestCase):
    def run_generation(self, root, *, shared, reject=False):
        project = root / "project with spaces"
        project.mkdir()
        script = project / "prepare fixture.py"
        script.write_text('''
import argparse, json, os
from pathlib import Path
parser = argparse.ArgumentParser()
parser.add_argument('--input')
parser.add_argument('--outdir')
parser.add_argument('--top')
args = parser.parse_args()
output = Path(args.outdir)
output.mkdir()
(output / 'observed.json').write_text(json.dumps({
    'arguments': vars(args), 'cwd': os.getcwd(),
    'compute': os.environ.get('CAPSTONE_TEST_COMPUTE'),
    'iteration': os.environ.get('EDA_RTL_PREP_ITERATION'),
    'errors': json.loads(os.environ['EDA_RTL_PREP_REVIEW_ERRORS_JSON']),
    'rules': os.environ.get('EDA_RTL_PREP_RULES_PATH'),
}))
''')
        output = project / "prepared output"
        output.mkdir()
        (output / "old.txt").write_text("existing output")
        scheduler_record = root / "scheduler.json"
        bin_dir = root / "bin"
        bin_dir.mkdir()
        scheduler = bin_dir / "srun"
        scheduler.write_text(f"#!{sys.executable}\n" + '''
import json, os, subprocess, sys
from pathlib import Path
arguments = sys.argv[1:]
Path(os.environ['CAPSTONE_TEST_SCHEDULER_RECORD']).write_text(json.dumps({
    'arguments': arguments,
    'output_exists_before_dispatch': Path(os.environ['CAPSTONE_TEST_OLD_OUTPUT']).exists(),
}))
if os.environ.get('CAPSTONE_TEST_REJECT'):
    raise SystemExit(42)
shell = arguments.index('/bin/bash')
# Model the compute process without sourcing the workstation's real shell setup.
payload = arguments[shell + 2].replace('source ~/.bashrc', ':', 1)
environment = dict(os.environ, CAPSTONE_TEST_COMPUTE='allocated')
raise SystemExit(subprocess.run(['/bin/bash', '-c', payload], env=environment).returncode)
''')
        scheduler.chmod(0o755)
        feedback = "Missing 'helper'\n$(touch injected)"
        state = {
            "remote_project_root": str(project),
            "remote_prepare_dir": "prepared output",
            "remote_prepare_input": "mailbox input",
            "remote_prepare_script": script.name,
            "remote_python_cmd": sys.executable,
            "mailbox_source": "/local/mailbox",
            "top_module": "custom_top",
            "prep_iteration": 2,
            "prep_review_errors": [feedback],
            "universal_rules_path": "/local/rules with spaces.md",
        }
        if shared:
            state.update(shared_allocation_id="777", shared_allocation_cpus=6)
        environment = {
            **os.environ,
            "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
            "CAPSTONE_TEST_SCHEDULER_RECORD": str(scheduler_record),
            "CAPSTONE_TEST_OLD_OUTPUT": str(output / "old.txt"),
        }
        if reject:
            environment["CAPSTONE_TEST_REJECT"] = "1"
        completed = subprocess.run(
            ["/bin/bash", "-c", _build_prepare_command(state)],
            cwd=root, env=environment, capture_output=True, text=True, timeout=10,
        )
        return completed, json.loads(scheduler_record.read_text()), output, state

    def test_generation_executes_inside_compute_dispatch_with_quoted_inputs(self):
        for shared in (True, False):
            with self.subTest(shared=shared), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                completed, dispatch, output, state = self.run_generation(root, shared=shared)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertTrue(dispatch["output_exists_before_dispatch"])
                self.assertFalse((output / "old.txt").exists())
                observed = json.loads((output / "observed.json").read_text())
                self.assertEqual(observed["compute"], "allocated")
                self.assertEqual(observed["cwd"], state["remote_project_root"])
                self.assertEqual(observed["arguments"], {
                    "input": "mailbox input", "outdir": "prepared output", "top": "custom_top",
                })
                self.assertEqual(observed["iteration"], "2")
                self.assertEqual(observed["errors"], state["prep_review_errors"])
                self.assertEqual(observed["rules"], state["universal_rules_path"])
                self.assertFalse((Path(state["remote_project_root"]) / "injected").exists())
                self.assertIn("[PREP] Generation running on", completed.stdout)
                if shared:
                    self.assertIn("--jobid=777", dispatch["arguments"])
                    self.assertIn("--cpus-per-task=6", dispatch["arguments"])
                    self.assertFalse(any(argument.startswith("--partition=") for argument in dispatch["arguments"]))
                else:
                    self.assertIn("--partition=adademic", dispatch["arguments"])

    def test_failed_compute_dispatch_preserves_previous_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            completed, dispatch, output, _ = self.run_generation(Path(temporary), shared=True, reject=True)
            self.assertEqual(completed.returncode, 42)
            self.assertTrue(dispatch["output_exists_before_dispatch"])
            self.assertEqual((output / "old.txt").read_text(), "existing output")
            self.assertFalse((output / "observed.json").exists())


if __name__ == "__main__":
    unittest.main()
