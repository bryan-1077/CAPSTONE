"""Portable package/resume inputs and shared interactive dispatch, offline."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

from .intake import collect_intake
from .nodes import FRONTEND
from .specification_inputs import inspect_input, snapshot_input, copy_resume_build


class SpecificationInputTests(unittest.TestCase):
    def test_real_package_snapshot_compiles_with_external_timing_library(self):
        source = FRONTEND / "microarch/specification/ddr4_fifo/package.yaml"
        info = inspect_input(source, target=200)
        self.assertEqual(len(info["files"]), 8)
        with tempfile.TemporaryDirectory() as tmp:
            copied, approval = snapshot_input(info, Path(tmp) / "input")
            self.assertIsNone(approval)
            result = subprocess.run([sys.executable, "-c",
                "import sys; from specification_lowering import compile_package; "
                "m=compile_package(sys.argv[1]); print(m['design_name'])", str(copied)],
                cwd=FRONTEND, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("ddr4_controller_top", result.stdout)

    def test_missing_document_and_clock_conflict_fail_before_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "request.yaml"
            source.write_text("controller_clock: {frequency_mhz: 180}\nrequirements: [queue]")
            with self.assertRaisesRegex(ValueError, "conflicts"):
                inspect_input(source, target=200)
            source.write_text("specification_version: 1\ndocuments: {shared: missing.yaml}")
            with self.assertRaises(FileNotFoundError):
                inspect_input(source)

    def test_resume_copy_preserves_source_and_rejects_patch_overwrite(self):
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "old"
            source.mkdir()
            architecture = {"modules": [{"spec": {"design_name": "example"}}]}
            (source / "architecture.yaml").write_text(yaml.safe_dump(architecture))
            (source / "microarch/gen_exp").mkdir(parents=True)
            spec = source / "microarch/gen_exp/example.yaml"
            spec.write_text("design_name: example")
            cache = source / ".llm_cache" / hashlib.sha256(spec.read_bytes()).hexdigest()
            cache.mkdir(parents=True)
            (cache / "example.sv").write_text("reviewed rtl")
            (source / "rtl_output").mkdir()
            rtl = source / "rtl_output/example.sv"
            rtl.write_text("reviewed rtl")
            copy_resume_build(source, root / "new", source / "architecture.yaml")
            self.assertEqual((root / "new/rtl_output/example.sv").read_text(), "reviewed rtl")
            rtl.write_text("intentional patch")
            with self.assertRaisesRegex(ValueError, "differs from reviewed cache"):
                copy_resume_build(source, root / "blocked", source / "architecture.yaml")
            self.assertFalse((root / "blocked").exists())
            self.assertEqual(rtl.read_text(), "intentional patch")

    def test_edit_and_opt_in_advisor_require_explicit_frequency(self):
        with tempfile.TemporaryDirectory() as tmp, patch("builtins.input", side_effect=[
                "queue", ".", "discuss", "180", "e", "revised queue", ".", "170", "y"]), patch(
                "shared.top_orchestrator.intake.ask_advisor", return_value="Consider 150 MHz") as advisor:
            path = collect_intake("specification", None, None, Path(tmp) / "intake.json")
            record = json.loads(path.read_text())
            self.assertEqual(record["original_text"], "revised queue")
            self.assertEqual(record["target_mhz"], 170)
            advisor.assert_called_once()

    def test_interactive_cli_enters_shared_intake_and_dispatches_selected_flow(self):
        from .__main__ import main
        with tempfile.TemporaryDirectory() as tmp, patch("sys.stdin.isatty", return_value=True), patch(
                "builtins.input", side_effect=["2", "queue", ".", "180", "y"]), patch(
                "sys.argv", ["orchestrator", "--no-verif", "--no-backend", "--run-dir", str(Path(tmp)/"run")]), patch(
                "shared.top_orchestrator.__main__.run") as execute:
            execute.return_value.status = "complete"
            self.assertEqual(main(), 0)
            self.assertEqual(execute.call_args.args[0].frontend_flow, "specification")
            self.assertEqual(execute.call_args.args[0].target_mhz, 180)

    def test_real_approved_resume_lint_and_handoff_with_network_disabled(self):
        import os
        import shutil
        if not shutil.which("verilator"):
            self.skipTest("Verilator required")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            guard = root / "guard"
            guard.mkdir()
            (guard / "sitecustomize.py").write_text(
                "import socket\ndef blocked(*a, **k): raise RuntimeError('Network disabled in offline acceptance')\n"
                "socket.socket.connect=blocked\nsocket.create_connection=blocked\n")
            script = root / "acceptance.py"
            script.write_text('''import hashlib, json, sys
from pathlib import Path
from generate_specification import propose_specification
from specification_lowering import compile_package, create_approval, emit_review
from architecture import lower_architecture
from test_generate_specification import register_fixture
root = Path(sys.argv[1])
_, request, replies = register_fixture()
replay = root / 'replay.json'
replay.write_text(json.dumps(replies))
package = propose_specification(request, output_dir=root/'source', response_path=replay)
manifest = compile_package(package)
architecture = root / 'review/architecture.yaml'
emit_review(manifest, architecture)
approval = root / 'approval.json'
approval.write_text(json.dumps(create_approval(manifest, 'offline-orchestrator-fixture')))
build = root / 'old-build'
specs = lower_architecture(manifest, build, specification_approval=approval)
name = manifest['modules'][0]['spec']['design_name']
cache = build / '.llm_cache' / hashlib.sha256(specs[0].read_bytes()).hexdigest()
cache.mkdir(parents=True)
(cache/(name+'.sv')).write_text(f"""module {name}(input logic clk, input logic rst_n,
  input logic in_valid, input logic [7:0] in_data,
  output logic out_valid, output logic [7:0] out_data);
  logic valid_q;
  assign out_valid = rst_n && valid_q;
  always_ff @(posedge clk) begin
    if (!rst_n) begin valid_q <= 1'b0; out_data <= '0; end
    else begin valid_q <= in_valid; if (in_valid) out_data <= in_data; end
  end
endmodule
""")
sys.path.insert(0, str(Path.cwd().parents[1]))
from shared.top_orchestrator.__main__ import main
sys.argv = ['orchestrator', '--frontend-flow', 'specification', '--input', str(architecture),
 '--spec-approval', str(approval), '--resume-build', str(build), '--target-mhz', '200',
 '--no-verif', '--no-backend', '--run-dir', str(root/'run')]
from unittest.mock import patch
with patch('shared.top_orchestrator.orchestrator.REPO_ROOT', root):
    assert main() == 0
result = json.loads((root/'run/frontend/frontend_result.json').read_text())
assert result['checks']['handoff_authentication'] == 'passed'
assert result['checks']['integration_lint'] == 'passed'
assert result['backend_ready'] is False
# Recheck a scoped external RTL patch using real lowering/lint/handoff, with
# networking disabled by this acceptance fixture's sitecustomize.
import shutil
from recheck_specification import recheck
from shared.top_orchestrator.specification_mailbox import publish_specification
native_build = root/'run/frontend/build'
native = json.loads((native_build/'verification_handoff.json').read_text())
candidate = root/'repair-candidate'
candidate.mkdir()
for row in native['inputs']:
    target = candidate/row['path']
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(native_build/row['path'], target)
patched = candidate/native['rtl_files'][0]
patched.write_text(patched.read_text()+'\\n// harmless offline recheck fixture\\n')
rechecked = root/'repair-build'
recheck(candidate, rechecked)
repaired_doc = json.loads((rechecked/'verification_handoff.json').read_text())
assert repaired_doc['identity']['rtl_revision'] != native['identity']['rtl_revision']
assert repaired_doc['identity']['package_fingerprint'] == native['identity']['package_fingerprint']
publication = publish_specification(rechecked, root/'repaired-publication', 200)
assert json.loads((publication/'snapshot.json').read_text())['backend_ready'] is False
stale = json.loads((candidate/'specification_approval.json').read_text())
stale['manifest_fingerprint'] = 'stale'
(candidate/'specification_approval.json').write_text(json.dumps(stale))
try: recheck(candidate, root/'stale-repair-build')
except ValueError: pass
else: raise AssertionError('recheck accepted stale approval')
# The prior partial build was not populated/modified by resume.
assert not (build/'verification_handoff.json').exists()
# Stale approvals are rejected by the real native validator before model calls.
from shared.top_orchestrator.specification_inputs import inspect_input
bad = json.loads(approval.read_text()); bad['manifest_fingerprint'] = 'tampered'
approval.write_text(json.dumps(bad))
try: inspect_input(architecture, approval, 200)
except ValueError: pass
else: raise AssertionError('stale approval accepted')
# Real handoff authentication rejects changed RTL.
from verification_handoff import validate_handoff
handoff = root/'run/frontend/build/verification_handoff.json'
doc = json.loads(handoff.read_text())
rtl = handoff.parent/doc['rtl_files'][0]
rtl.write_text(rtl.read_text()+'\\n// changed')
try: validate_handoff(handoff)
except ValueError: pass
else: raise AssertionError('tampered RTL accepted')
''')
            env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(guard), str(FRONTEND)]),
                       TAMUS_AI_CHAT_API_KEY="", CAPSTONE_TPM_LIMIT="0")
            result = subprocess.run([sys.executable, str(script), str(root)], cwd=FRONTEND,
                                    env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout[-8000:] + result.stderr)
