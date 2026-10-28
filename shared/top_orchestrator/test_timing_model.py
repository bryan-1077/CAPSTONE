"""Functional timing scope regressions; no live model calls."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

from .contracts import RunConfig
from .frequency import prepare_generation
from .nodes import FRONTEND
from .specification_frontend import run_specification
from .timing_model import apply_timing_model, FUNCTIONAL_TIMING


class TimingModelTests(unittest.TestCase):
    def test_existing_intake_gets_scope_without_changing_description_or_target(self):
        text = "I want 3200 speed, 4 banks, open policy, standard timing trackers"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            intake = root / "intake.json"
            intake.write_text(json.dumps({"schema_version": 1, "frontend_flow": "specification",
                "status": "confirmed_request", "original_text": text, "target_mhz": 200}))
            with patch("shared.top_orchestrator.specification_frontend.stream_command", return_value=1):
                result = run_specification(RunConfig("generate", intake=intake, target_mhz=200,
                    frontend_flow="specification"), root)
            request = yaml.safe_load((root / "frontend/input/request.yaml").read_text())
            self.assertEqual(request["requirements"], [text])
            self.assertEqual(request["controller_clock"]["frequency_mhz"], 200)
            self.assertEqual(request["timing_model"], FUNCTIONAL_TIMING)
            self.assertEqual(result.status, "failed")
            self.assertNotIn("frontend_result", result.artifacts)

    def test_explicit_scope_and_physical_requirements_are_not_overwritten(self):
        request = {"design_type": "memory_controller", "timing_model": {"id": "physical-ddr"},
                   "requirements": ["Provide real DDR4-3200 signaling and a PHY."]}
        self.assertEqual(apply_timing_model(request), request)
        with self.assertRaisesRegex(ValueError, "Standard supports only"):
            apply_timing_model(request, standard=True)
        modified = deepcopy(FUNCTIONAL_TIMING)
        modified["definition"] = "Rescale all timing to the controller frequency"
        with self.assertRaisesRegex(ValueError, "differs"):
            apply_timing_model({"timing_model": modified})
        for malformed in (None, "physical", {}, {"id": ""}):
            with self.subTest(malformed=malformed), self.assertRaisesRegex(ValueError, "nonempty id"):
                apply_timing_model({"timing_model": malformed})
        self.assertNotIn("timing_model", apply_timing_model({"design_type": "generic"}))

    def test_standard_original_yaml_is_unchanged_and_scope_is_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "legacy.yaml"
            text = "memory: {speed: 3200}\n# exact snapshot\n"
            source.write_text(text)
            run = root / "run"
            run.mkdir()
            config = prepare_generation(RunConfig("generate", input_yaml=source,
                skip_verification=True, no_backend=True), run)
            self.assertEqual(config.input_yaml.read_text(), text)
            self.assertEqual(source.read_text(), text)
            self.assertEqual(json.loads((run / "timing_model.json").read_text()), FUNCTIONAL_TIMING)

    def test_native_request_traceability_prompts_and_standard_counts(self):
        script = '''import json, sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd().parents[1]))
from shared.top_orchestrator.timing_model import apply_timing_model, FUNCTIONAL_TIMING
from specification_request import build_specification_request
from generate_specification import build_prompt, generation_context, validate_system, UnsupportedSpecification
from architecture import fingerprint
from expand_spec import build_substitution_map
from specification import read_document
from tpm_pacing import estimate_input
text = 'DDR4-3200, four banks; real physical DDR signaling is required.'
config = {'design_type': 'memory_controller', 'requirements': [text],
          'controller_clock': {'frequency_mhz': 200}}
request = build_specification_request(apply_timing_model(config))
assert request['requirements'][0]['text'] == text
assert request['constraints']['timing_model'] == FUNCTIONAL_TIMING
assert fingerprint(request) != fingerprint(build_specification_request(config))
assert any('timing_model/definition' in r['text'] and 'refresh intervals' in r['text'] for r in request['requirements'])
context = generation_context()
for stage in ('system', 'architecture'):
    prompt = build_prompt(stage, request, context)
    assert '110 ns' in prompt and 'rather than silently weakening it' in prompt
    assert estimate_input([{'role': 'user', 'content': prompt}]) + 24000 <= 80000
# Real unsupported responses remain terminal; the abstraction is not an override.
try: validate_system({'status': 'unsupported', 'unsupported': [{'requirement_ids': ['R1'],
    'reason': 'Physical interface requires additional domains', 'required_extension': 'PHY'}]}, request, context['timing_library'])
except UnsupportedSpecification: pass
else: raise AssertionError('unsupported response bypassed')
library = read_document('microarch/jedec/jedec_dictionary.yaml')
params = {'tRCD_cycles': 'timing_cycles.tRCD', 'tREFI_cycles': 'timing_cycles.tREFI'}
for profile in ('DDR4-2400', 'DDR4-3200'):
    resolved = build_substitution_map(library[profile], params)
    assert resolved['tRCD_cycles'] == library[profile]['timing_cycles']['tRCD']
    assert resolved['tREFI_cycles'] == library[profile]['timing_cycles']['tREFI']
assert library['DDR4-3200']['timing_cycles']['tRCD'] == 22
'''
        result = subprocess.run([sys.executable, "-c", script], cwd=FRONTEND,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
