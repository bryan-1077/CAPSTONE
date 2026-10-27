"""Executable profile regressions, with independent traces and fault injection."""
import copy
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from simulation.controller import CheckFailure, ControllerOracle, scenarios
from simulate_handoff import check_trace, read_trace, run, testbench


def shared():
    def record(fields):
        return {'kind': 'record', 'fields': [{'name': n, 'width': w, 'lsb': l, 'msb': l + w - 1}
                                            for n, w, l in fields]}
    return {'parameters': {'BANK_COUNT': 1, 'WAITING_CAPACITY': 1, 'ACTIVE_CAPACITY': 1,
                'DATA_WIDTH': 32, 'HOST_ADDR_WIDTH': 4, 'MEMORY_WORDS_PER_BANK': 16,
                'READ_RESPONSE_LATENCY': 1, 'TFAW_ACT_LIMIT': 4, 'PAGE_POLICY': 'open_page'},
        'types': {'row': {'width': 10},
            'bank_rows': {'kind': 'array', 'count': 1, 'element_width': 10, 'packed_order': 'ascending_index_from_lsb'},
            'bank_valid': {'kind': 'array', 'count': 1, 'element_width': 1, 'packed_order': 'ascending_index_from_lsb'},
            'opcode': {'encodings': {'NOP': 0, 'ACT': 1, 'PRE': 2,
                                                               'READ': 3, 'WRITE': 4, 'REF': 5}},
            'request': record([('bank', 2, 49), ('row', 10, 39), ('column', 6, 33),
                               ('is_write', 1, 32), ('wdata', 32, 0)]),
            'command': record([('opcode', 3, 50), ('bank', 2, 48), ('row', 10, 38),
                               ('column', 6, 32), ('wdata', 32, 0)])},
        'clock_reset': {'clock': 'clk', 'reset': 'rst_n', 'edge': 'rising', 'active_level': 0,
                        'assertion_style': 'synchronous'},
        'timing_profile': {'unit': 'cycles', 'values': {'tRCD': 3, 'tRAS': 4, 'tRP': 2,
                                             'tRRD': 1, 'tFAW': 4, 'tREFI': 50, 'tRFC': 3}}}


def transaction_trace():
    """Hand-written timing: accept/dispatch, ACT, held WRITE, READ, response.

    Fixed values deliberately do not consult the oracle to generate expectations.
    """
    trace = []
    for c in range(14):
        s = dict.fromkeys(('valid', 'cmd_ready', 'write', 'bank', 'addr', 'wdata', 'rsp_valid',
                          'rsp_data', 'admission', 'dispatch', 'commit', 'incoming_request',
                          'dispatch_request', 'commit_command', 'candidate_valid', 'candidate_command',
                          'candidate_ok', 'probe_valid', 'probe_ok', 'open_valid', 'open_rows', 'pending', 'busy'), 0)
        s.update(cycle=c, rst_n=int(c > 0), ready=int(c > 0))
        if c in (1, 2, 8, 9, 12, 13):
            s.update(probe_valid=1, probe_ok=1, cmd_ready=1)
        if c >= 5:
            s['open_valid'] = 1
        if c in (1, 8):
            s.update(valid=1, admission=1)
        if c == 1:
            s.update(write=1, wdata=0x1234, incoming_request=(1 << 32) | 0x1234)
        if c in (2, 9):
            s['dispatch'] = 1
        if c == 2:
            s['dispatch_request'] = (1 << 32) | 0x1234
        if c in (4, 6, 7, 11):
            s['candidate_valid'] = 1
            s['candidate_command'] = ((1 if c == 4 else 3 if c == 11 else 4) << 50) | (0x1234 if c in (6, 7) else 0)
            s['commit_command'] = s['candidate_command']
            s['candidate_ok'] = int(c != 6)
            s['commit'] = int(c != 6)
        if c == 12:
            s.update(rsp_valid=1, rsp_data=0x1234)
        trace.append(s)
    return trace


def fixture_handoff():
    from simulate_handoff import HOST
    ports = [{'name': n, 'direction': d, 'width': w} for n, (d, w) in HOST.items()]
    internal = {'incoming_request': 51, 'dispatch_request': 51, 'commit_command': 53,
                'dispatch_valid': 1, 'commit_valid': 1, 'candidate_valid': 1,
                'candidate_command': 53, 'candidate_ok': 1, 'probe_valid': 1,
                'probe_ok': 1, 'open_valid': 1, 'open_rows': 10, 'refresh_pending': 1, 'refresh_busy': 1}
    def bound(name, width=1):
        top = name in HOST
        return {'endpoint': ('top.' if top else 'core.') + name,
                'dut_path_suffix': ('' if top else 'core.') + name,
                'width': width, 'lsb': 0, 'msb': width - 1}
    def event(when, payload, signal, width):
        return {'status': 'bound', 'when': when, 'payload': {payload: bound(signal, width)}}
    events = {
        'admission': event({'all': [dict(bound('txn_valid'), equals=1), dict(bound('txn_ready'), equals=1)]},
                           'request', 'incoming_request', 51),
        'dispatch': event(dict(bound('dispatch_valid'), equals=1), 'request', 'dispatch_request', 51),
        'command_commit': event(dict(bound('commit_valid'), equals=1), 'command', 'commit_command', 53),
        'response_delivery': event(dict(bound('rsp_valid'), equals=1), 'data', 'rsp_rdata', 32)}
    return {'phase': 'rtl', 'shared': shared(), 'design': {'top_module': 'fixture_top', 'top_ports': ports,
        'modules': [{'instance': 'core', 'ports': [{'name': n, 'direction': 'output', 'width': w}
                                                 for n, w in internal.items()]}]},
        'verification_intent': {'bindings': {'sampling': {'phase': 'pre_edge', 'reset_gates_events': True},
                          'events': events, 'observations': {n: bound(n, w) for n, w in internal.items()}}}}


class ControllerSimulationTests(unittest.TestCase):
    def replay(self, trace, config=None):
        oracle = ControllerOracle(config or shared())
        for row in trace:
            oracle.step(row)
        oracle.finish(trace[-1]['cycle'])
        return oracle

    def test_independent_write_read_trace_passes_with_stalled_candidate(self):
        result = self.replay(transaction_trace())
        self.assertEqual(result.counts['responses'], 1)
        self.assertEqual(result.counts['retirements'], 2)
        self.assertEqual(result.counts['blocked_candidate_edges'], 1)

    def test_corruption_reordering_early_command_and_reset_fail_at_first_bad_edge(self):
        for cycle, values, check in [
            (2, {'dispatch_request': 0}, 'fifo_order'),
            (6, {'candidate_ok': 1, 'commit': 1}, 'tRCD'),
            (7, {'candidate_command': 0, 'commit_command': 0}, 'candidate_hold'),
            (12, {'rsp_data': 0xbad}, 'read_data'),
            (12, {'rsp_valid': 0}, 'response_latency'),
            (3, {'open_valid': 1}, 'row_state'),
            (0, {'commit': 1}, 'reset_gating'),
            (0, {'commit_command': 7}, 'reset_payload')]:
            trace = transaction_trace()
            trace[cycle].update(values)
            with self.subTest(check=check), self.assertRaises(CheckFailure) as error:
                self.replay(trace)
            self.assertEqual(error.exception.check, check)
            self.assertEqual(error.exception.cycle, cycle)

    def test_deadline_is_predicted_independently_and_early_pending_fails(self):
        trace = transaction_trace()[:3]
        trace[2].update(pending=1)
        with self.assertRaisesRegex(CheckFailure, 'refresh_deadline'):
            self.replay(trace)

    def test_incomplete_accepted_work_cannot_pass(self):
        with self.assertRaisesRegex(CheckFailure, 'bounded_completion'):
            self.replay(transaction_trace()[:3])

    def test_parser_checks_contiguous_complete_nonempty_trace(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'trace'
            for text in ('', 'cycle rst_n\n', 'cycle rst_n\n0 0\n2 1\n', 'cycle rst_n\n0\n', 'cycle rst_n\n0 x\n'):
                path.write_text(text)
                with self.subTest(text=text), self.assertRaises(ValueError):
                    list(read_trace(path))

    def test_passing_trace_reports_checks_without_claiming_all_requirements(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'trace'
            rows = transaction_trace()
            fields = ['cycle', *[k for k in rows[0] if k != 'cycle']]
            path.write_text(' '.join(fields) + '\n' + ''.join(
                ' '.join(f'{row[k]:x}' for k in fields) + '\n' for row in rows))
            result = check_trace(path, shared(), 'fifo_memory')
            self.assertEqual(result['status'], 'passed')
            self.assertGreater(result['counters']['check:read_data'], 0)
            rows[12]['rsp_data'] = 0
            path.write_text(' '.join(fields) + '\n' + ''.join(
                ' '.join(f'{row[k]:x}' for k in fields) + '\n' for row in rows))
            self.assertEqual(check_trace(path, shared(), 'fifo_memory')['failure']['check'], 'read_data')

    def test_vectors_are_seeded_and_do_not_mutate_configuration(self):
        config = shared()
        original = copy.deepcopy(config)
        self.assertEqual(scenarios(config, 4), scenarios(config, 4))
        self.assertNotEqual(scenarios(config, 4)['queue_pressure'], scenarios(config, 5)['queue_pressure'])
        self.assertEqual(config, original)

    def test_timing_refresh_and_cleanup_faults_are_rejected(self):
        config = shared()
        for check in ('tRP', 'tRAS', 'tRRD', 'tFAW', 'tRFC', 'refresh_drain', 'page_policy'):
            oracle = ControllerOracle(copy.deepcopy(config))
            oracle.active = {'bank': 0, 'row': 1, 'column': 0, 'is_write': 1, 'wdata': 0}
            cmd = {'opcode': 1, 'bank': 0, 'row': 1, 'column': 0, 'wdata': 0}
            if check == 'tRP':
                oracle.pres[0] = 9
            elif check == 'tRAS':
                oracle.rows[0], oracle.acts[0] = 0, 9
                cmd['opcode'] = 2
            elif check == 'tRRD':
                oracle.t['tRRD'] = 2
                oracle.act_history = [9]
            elif check == 'tFAW':
                oracle.t['tFAW'] = 5
                oracle.act_history = [6, 7, 8, 9]
            elif check == 'tRFC':
                oracle.last_ref = 9
            elif check == 'refresh_drain':
                oracle.pending = True
                cmd['opcode'] = 5
            else:
                oracle.active, oracle.cleanup, oracle.rows[0] = None, 1, 0
                cmd['opcode'] = 2
            with self.subTest(check=check), self.assertRaises(CheckFailure) as error:
                oracle.command(cmd, 10)
            self.assertEqual(error.exception.check, check)

    def test_source_requirements_are_not_automatically_promoted_to_passed(self):
        # A report carries every source key, even when no known oracle mapping
        # exists. A mocked compiler failure exercises reporting without any API.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            h = fixture_handoff()
            h.update(identity={'rtl_revision': 'fixture'}, handoff_fingerprint='fixture',
                     inputs=[{'role': 'architecture', 'path': 'architecture.json'}], rtl_files=[])
            h['verification_intent'].update(requirements={'system.rules.UNKNOWN_RULE': {}},
                                           obligations={'system.verification.UNKNOWN_CHECK': {}})
            import json
            (root / 'handoff.json').write_text(json.dumps(h))
            (root / 'architecture.json').write_text(json.dumps({'provenance': {'specification': {
                'snapshot': {'package': {}}}}}))
            with patch('simulate_handoff.validate_handoff'), patch('simulate_handoff.shutil.which', return_value=None):
                report = run(root / 'handoff.json', root / 'out')
            self.assertEqual(report['status'], 'tool_error')
            for group in report['source_results'].values():
                self.assertEqual(next(iter(group.values()))['status'], 'unverified')

    def test_unsupported_profiles_unbound_and_private_observations_fail(self):
        candidates = []
        h = fixture_handoff(); h['shared']['parameters']['READ_RESPONSE_LATENCY'] = 2; candidates.append(h)
        h = fixture_handoff(); h['phase'] = 'review'; candidates.append(h)
        h = fixture_handoff(); h['shared']['types']['bank_rows']['packed_order'] = 'msb_first'; candidates.append(h)
        h = fixture_handoff(); h['shared']['types']['opcode']['encodings']['NOP'] = 7; candidates.append(h)
        h = fixture_handoff(); h['verification_intent']['bindings']['events']['command_commit']['status'] = 'unbound'; candidates.append(h)
        h = fixture_handoff(); h['verification_intent']['bindings']['observations']['open_valid']['dut_path_suffix'] = 'core.private_state'; candidates.append(h)
        h = fixture_handoff(); h['design']['top_module'] = 'bad; injected'; candidates.append(h)
        for h in candidates:
            with self.subTest(h=h), self.assertRaises(ValueError):
                testbench(h)

    def test_tampered_handoff_stops_before_compiler_with_failure_report(self):
        from specification import SpecificationError
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'run'
            with patch('simulate_handoff.validate_handoff', side_effect=SpecificationError('hash changed')), \
                 patch('simulate_handoff.subprocess.run') as command:
                result = run(Path(directory) / 'handoff.json', output)
            command.assert_not_called()
            self.assertEqual(result['status'], 'invalid_input')
            self.assertIn('hash changed', result['error'])
            self.assertTrue((output / 'simulation_report.json').is_file())
            with self.assertRaises(FileExistsError):
                run(Path(directory) / 'handoff.json', output)

    @unittest.skipUnless(shutil.which('verilator'), 'Verilator unavailable')
    def test_actual_verilator_driver_holds_payload_and_samples_before_edge(self):
        h = fixture_handoff()
        from simulate_handoff import HOST
        declarations = ',\n'.join(f'{direction} logic ' + (f'[{width-1}:0] ' if width > 1 else '') + name
                                   for name, (direction, width) in HOST.items())
        internal = h['design']['modules'][0]['ports']
        observables = ',\n'.join('output logic ' + (f'[{p["width"]-1}:0] ' if p['width'] > 1 else '')
                                 + p['name'] for p in internal)
        # Intentionally simple DUT: backpressure for two edges after reset.
        rtl = f'''module observations({observables});
            {''.join('assign ' + p['name'] + " = '0;" for p in internal)}
            endmodule
            module fixture_top({declarations});
            observations core();
            integer elapsed;
            always_ff @(posedge clk) if (!rst_n) elapsed <= 0; else elapsed <= elapsed + 1;
            assign txn_ready = rst_n && elapsed >= 2;
            assign cmd_ready = 0; assign rsp_valid = 0; assign rsp_rdata = 0;
            endmodule'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'dut.sv').write_text(rtl)
            (root / 'tb.sv').write_text(testbench(h))
            compile_result = subprocess.run(['verilator', '--binary', '--timing', '-Wno-fatal',
                '--top-module', 'capstone_sim_tb', '--Mdir', str(root / 'obj'),
                str(root / 'dut.sv'), str(root / 'tb.sv')], capture_output=True, text=True, timeout=120)
            self.assertEqual(compile_result.returncode, 0, compile_result.stdout + compile_result.stderr)
            (root / 'vectors').write_text('0 2 0 0 0\n2 1 0 7 12345678\n1 2 0 0 0\n')
            result = subprocess.run([str(root / 'obj/Vcapstone_sim_tb'), '+VECTORS=' + str(root / 'vectors'),
                '+TRACE=' + str(root / 'trace'), '+MAX_CYCLES=20'], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            rows = list(read_trace(root / 'trace'))
            requests = [r for r in rows if r['rst_n'] and r['valid']]
            self.assertEqual([r['admission'] for r in requests], [0, 0, 1])
            self.assertEqual([(r['addr'], r['wdata'], r['write']) for r in requests], [(7, 0x12345678, 1)] * 3)


if __name__ == '__main__':
    unittest.main()
