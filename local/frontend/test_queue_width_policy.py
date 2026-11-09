"""Regression for the queue port/localparam conflict and DEPTH=1 width bug."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import dual_llm_rtlGen as gen
from ir_to_llm_context import (
    ANSI_PORT_WIDTH_RULE, QUEUE_INDEX_WIDTH_RULES, build_llm_context,
)
from prompt_context import expand_context
from validator import validate_spec


ROOT = Path(__file__).resolve().parent
GUARDED_RANGE = '[((DEPTH <= 1) ? 1 : $clog2(DEPTH))-1:0]'


class QueueWidthPromptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with redirect_stdout(io.StringIO()):
            cls.design = validate_spec(str(
                ROOT / 'microarch/gen_exp/ddr4_request_queue_request_queue.yaml'))

    def test_real_context_has_compatible_port_and_internal_width_rules(self):
        context = build_llm_context(self.design)
        bundle = gen._design_context_bundle(self.design)
        self.assertEqual(expand_context(json.loads(bundle['ctx_str'])), context)
        for rule in (ANSI_PORT_WIDTH_RULE, *QUEUE_INDEX_WIDTH_RULES):
            self.assertIn(rule, context['implementation_constraints'])
            self.assertIn(rule, context['invariants'])
            self.assertIn(rule, bundle['ctx_str'])
        self.assertNotIn('Use SEL_WIDTH for sel_idx,', json.dumps(context))
        self.assertIn('DEPTH is the only configurable source', bundle['ctx_str'])
        self.assertNotIn('or use a parameter declared before the port list', bundle['ctx_str'])
        self.assertIn('DEPTH <= 1', context['request_payload']['required_localparams'][1])

    def test_every_actual_model_stage_receives_the_same_width_policy(self):
        rtl = 'module ddr4_request_queue; endmodule'
        good = json.dumps({'status': 'VALID', 'issues': [], 'corrected_rtl': rtl})
        review = json.dumps({'status': 'VALID', 'issues': []})
        with patch.object(gen, '_call_llm', side_effect=[good, review, review, good]) as call:
            gen._llm1_generate(self.design, '', [])
            gen._llm2_review(self.design, '', rtl, [])
            gen._llm2_review_compact(self.design, rtl)
            gen._llm1_merge(self.design, '', rtl, rtl, {'issues': []}, [])
        self.assertEqual(call.call_count, 4)
        for request in call.call_args_list:
            for rule in (ANSI_PORT_WIDTH_RULE, *QUEUE_INDEX_WIDTH_RULES):
                self.assertEqual(request.args[1].count(rule), 1)
            self.assertNotIn('use SEL_WIDTH for sel_idx,', request.args[1])

    def test_width_violation_is_repaired_and_reviewed_before_acceptance(self):
        repaired = (ROOT / 'test_fixtures/request_queue_width.sv').read_text()
        broken = repaired.replace(GUARDED_RANGE, '[$clog2(DEPTH)-1:0]')
        issue = {'type': 'SPEC_VIOLATION', 'location': 'sel_idx',
                 'description': 'sel_idx omits the required DEPTH=1 width guard.'}
        good = {'status': 'VALID', 'issues': [], 'corrected_rtl': repaired}
        with redirect_stdout(io.StringIO()), \
             patch.object(gen, 'MAX_ROUNDS', 2), \
             patch.object(gen, '_llm1_generate', return_value={**good, 'corrected_rtl': broken}), \
             patch.object(gen, '_llm2_review', side_effect=[
                 {'status': 'INVALID', 'issues': [issue], 'corrected_rtl': broken}, good,
             ]) as review, \
             patch.object(gen, '_llm1_merge', return_value=good) as merge, \
             patch.object(gen, '_write_reviewed_cache', side_effect=lambda name, rtl: rtl) as cache, \
             patch('requests.sessions.Session.request', side_effect=AssertionError('Network forbidden')):
            result = gen.run_dual_llm_rtlgen(self.design, '', lint_enabled=False)
        self.assertEqual(result, repaired)
        merge.assert_called_once()
        self.assertEqual(review.call_count, 2)
        self.assertEqual(review.call_args.args[2], repaired)
        cache.assert_called_once_with(self.design.design_name, repaired)


@unittest.skipUnless(shutil.which('verilator'), 'Verilator required')
class QueueWidthElaborationTests(unittest.TestCase):
    def run_width_probe(self, rtl, extra_parameters=''):
        """Check actual elaborated ports and body indices, not Python formulas."""
        probes = []
        for depth in (1, 2, 3, 4, 8):
            width = max(1, (depth - 1).bit_length())
            probes.append(f'''
    ddr4_request_queue #(.DEPTH({depth}){extra_parameters}) q{depth} (
        .clk(1'b0), .rst_n(1'b0), .enq_valid(1'b0), .enq_req(51'b0),
        .enq_ready(), .deq_en(1'b0), .sel_idx({width}'d0),
        .req_valid(), .req_array()
    );
    initial begin
        assert ($bits(q{depth}.sel_idx) == {width}) else $fatal(1, "sel_idx DEPTH={depth}");
        assert ($bits(q{depth}.first_free_idx) == {width}) else $fatal(1, "first_free_idx");
        assert ($bits(q{depth}.insert_idx) == {width}) else $fatal(1, "insert_idx");
    end
''')
        bench = 'module width_probe;\n' + '\n'.join(probes) + '\ninitial begin #1; $finish; end\nendmodule\n'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'queue.sv'
            source.write_text(rtl + '\n' + bench)
            build = subprocess.run([
                'verilator', '--binary', '--timing', '--assert', '-Wno-fatal',
                '--top-module', 'width_probe', '--Mdir', str(root / 'obj'), str(source),
            ], capture_output=True, text=True, timeout=120)
            self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
            # Avoid a core dump when the deliberately broken candidate aborts.
            import resource
            def no_core():
                resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            result = subprocess.run([str(root / 'obj/Vwidth_probe')],
                                    capture_output=True, text=True, timeout=10,
                                    preexec_fn=no_core)
            return result

    def test_inline_guarded_port_matches_internal_indices_at_all_depths(self):
        rtl = (ROOT / 'test_fixtures/request_queue_width.sv').read_text()
        result = self.run_width_probe(rtl)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_independent_parameter_override_can_break_port_and_internal_width_match(self):
        rtl = (ROOT / 'test_fixtures/request_queue_width.sv').read_text()
        rtl = rtl.replace('parameter int DEPTH = 4',
                          'parameter int DEPTH = 4,\n'
                          '    parameter int SEL_IDX_WIDTH = (DEPTH <= 1) ? 1 : $clog2(DEPTH)')
        rtl = rtl.replace(GUARDED_RANGE, '[SEL_IDX_WIDTH-1:0]')
        result = self.run_width_probe(rtl, ', .SEL_IDX_WIDTH(1)')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('sel_idx DEPTH=3', result.stdout + result.stderr)

    def test_original_unguarded_candidate_fails_the_depth_one_probe(self):
        rtl = (ROOT / 'test_fixtures/request_queue_width.sv').read_text()
        rtl = rtl.replace(GUARDED_RANGE, '[$clog2(DEPTH)-1:0]')
        result = self.run_width_probe(rtl)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('sel_idx DEPTH=1', result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
