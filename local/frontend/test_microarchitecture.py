"""Architecture compiler regressions; no network/LLM calls are required."""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml
import requests

from architecture import (ArchitectureError, ROOT, emit_wrapper, fingerprint,
                          library_context, lower_architecture, read_mapping,
                          validate_architecture, emit_verification_specs)
from generate_microarch import build_request, parse_response, propose, call_llm
from generate_microarch import build_prompt
from controller_baseline import controller_baseline
from design import generation_route, ROUTE_DUAL_LLM
from validator import validate_spec

EXAMPLE = ROOT / "microarch/examples/timing_proposal.json"
REQUEST = ROOT / "microarch/examples/timing_request.yaml"


class ArchitectureTests(unittest.TestCase):
    def test_architecture_request_uses_provider_default_temperature(self):
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps({'choices':[{'finish_reason':'stop',
                                                    'message':{'content':'{}'}}]}).encode()
        with patch.dict('os.environ', {'TAMUS_AI_CHAT_API_KEY':'secret-test-key'}):
            with patch('generate_microarch.requests.post', return_value=response) as post:
                self.assertEqual(call_llm('prompt', 'protected.gpt-5.6-terra'), '{}')
        payload = post.call_args.kwargs['json']
        self.assertEqual(payload['model'], 'protected.gpt-5.6-terra')
        self.assertNotIn('temperature', payload)

    def test_http_error_shows_status_and_provider_detail_without_credentials(self):
        response = requests.Response()
        response.status_code = 400
        response._content = json.dumps({'error':{'message':'Unsupported parameter temperature; Bearer secret-test-key',
                                                 'param':'temperature','code':'unsupported_parameter'}}).encode()
        with patch.dict('os.environ', {'TAMUS_AI_CHAT_API_KEY':'secret-test-key'}):
            with patch('generate_microarch.requests.post', return_value=response):
                with self.assertRaises(ArchitectureError) as caught:
                    call_llm('prompt', 'test-model')
        message = str(caught.exception)
        self.assertIn('HTTP 400', message)
        self.assertIn('Unsupported parameter temperature', message)
        self.assertIn('test-model', message)
        self.assertNotIn('secret-test-key', message)

    def test_http_non_json_error_is_bounded_and_truncation_remains_clear(self):
        response = requests.Response()
        response.status_code = 503
        response._content = ('Unavailable ' * 100).encode()
        with patch.dict('os.environ', {'TAMUS_AI_CHAT_API_KEY':'secret-test-key'}):
            with patch('generate_microarch.requests.post', return_value=response):
                with self.assertRaisesRegex(ArchitectureError, 'HTTP 503') as caught:
                    call_llm('prompt', 'test-model')
        self.assertLess(len(str(caught.exception)), 700)
        response.status_code = 200
        response._content = json.dumps({'choices':[{'finish_reason':'length','message':{'content':'partial'}}]}).encode()
        with patch.dict('os.environ', {'TAMUS_AI_CHAT_API_KEY':'secret-test-key'}):
            with patch('generate_microarch.requests.post', return_value=response):
                with self.assertRaisesRegex(ArchitectureError, 'exceeded token limit'):
                    call_llm('prompt', 'test-model')

    def test_flow_model_default_tracks_generator_and_explicit_override_wins(self):
        from run_flow import parse_args
        with patch('generate_microarch.DEFAULT_MODEL', 'test-model'):
            with patch.object(sys, 'argv', ['run_flow.py', str(REQUEST), '--microarch-only']):
                self.assertEqual(parse_args().microarch_model, 'test-model')
            with patch.object(sys, 'argv', ['run_flow.py', str(REQUEST), '--microarch-only',
                                           '--microarch-model', 'explicit-model']):
                self.assertEqual(parse_args().microarch_model, 'explicit-model')

    def setUp(self):
        self.manifest = json.loads(EXAMPLE.read_text())
        self.request = build_request(read_mapping(REQUEST))

    def assert_invalid(self, change, message):
        manifest = copy.deepcopy(self.manifest)
        change(manifest)
        with self.assertRaisesRegex(ArchitectureError, message):
            validate_architecture(manifest)

    def test_library_timing_and_lowering(self):
        modules = validate_architecture(self.manifest)
        self.assertEqual(modules['u_spacing']['spec']['behavior']['rtl_parameters']['TRRD_CYCLES'], 6)
        with tempfile.TemporaryDirectory() as tmp:
            specs = lower_architecture(self.manifest, tmp)
            self.assertEqual(len(specs), 1)
            self.assertIn('.act_pulse(act_pulse)', (Path(tmp) / 'rtl_output/timing_demo.sv').read_text())
            self.assertEqual(json.loads((Path(tmp) / 'verification_plan.json').read_text())['status'], 'planned_not_executed')

    def test_graph_rejections(self):
        changes = [
            (lambda m: m['connections'][0].update(to='missing.clk'), 'Unknown connection'),
            (lambda m: m['connections'][0].update(to='top.rst_n'), 'Wrong connection direction'),
            (lambda m: m['connections'].append(m['connections'][0]), 'Multiple drivers'),
            (lambda m: m['top_ports'][2].update(width=2), 'Width mismatch'),
            (lambda m: m['connections'].pop(0), 'Undriven'),
            (lambda m: m['modules'].append(copy.deepcopy(m['modules'][0])), 'Duplicate'),
            (lambda m: m['modules'][0]['reuse'].update(submodule='unknown'), 'unknown reusable'),
            (lambda m: m['requirements'][0].update(owners=['missing']), 'owners'),
            (lambda m: m['requirements'].append(m['requirements'][0]), 'Duplicate requirement'),
            (lambda m: m['top_ports'][0].update(name='module'), 'reserved HDL'),
            (lambda m: m['modules'][0].update(parameters={'X': 2}), 'Unsupported module fields'),
            (lambda m: m['connections'][0].update(expression='clk & ready'), 'unsupported fields'),
            (lambda m: m.update(integration_backend='legacy'), 'Unsupported architecture fields'),
            (lambda m: m['modules'][0]['reuse'].update(parameters={'X': 2}), 'unsupported fields'),
            (lambda m: m.update(assumptions='text'), 'list of strings'),
        ]
        for change, message in changes:
            with self.subTest(message=message):
                self.assert_invalid(change, message)

    def test_custom_module_reaches_existing_ir_and_datapath_route(self):
        spec = {'design_name':'custom_spacing', 'design_type':'datapath',
                'clock':{'name':'clk'}, 'reset':{'name':'rst_n','active_low':True,'synchronous':False},
                'ports':[{'name':'act_pulse','direction':'input','width':1},
                         {'name':'tRRD_block','direction':'output','width':1}],
                'behavior':{'description':'Count down six cycles on each accepted activation; reset clears count.'},
                'combinational_dependencies':{'tRRD_block':[]}}
        self.manifest['modules'][0].pop('reuse')
        self.manifest['modules'][0]['spec'] = spec
        with tempfile.TemporaryDirectory() as tmp:
            paths = lower_architecture(self.manifest, tmp)
            ir = validate_spec(str(paths[0]))
            self.assertEqual(generation_route(ir, ir.design_name), ROUTE_DUAL_LLM)
            self.assertIn('Count down', ir.metadata['behavior']['description'])
        self.assert_invalid(lambda m: m['modules'][0]['spec'].update(design_type='new_route'), 'supported generation')
        self.assert_invalid(lambda m: m['modules'][0]['spec'].update(design_name='ddr4_tRRD_simple_tRRD'), 'specialized timing')

    def test_reuse_queue_normalizes_explicit_clock_ports(self):
        manifest = copy.deepcopy(self.manifest)
        manifest['modules'] = [{'instance':'u_queue', 'responsibility':'Store requests.',
                                'reuse':{'feature':'request_queue','submodule':'request_queue'}}]
        # Derive only the interface fixture, not RTL: tests normalization against real library.
        lib = library_context()
        ports = lib['templates']['request_queue']['submodules'][0]['ports']
        manifest['top_ports'] = copy.deepcopy(ports)
        manifest['connections'] = [({'from':f'top.{p["name"]}', 'to':f'u_queue.{p["name"]}'} if p['direction']=='input'
                                    else {'from':f'u_queue.{p["name"]}', 'to':f'top.{p["name"]}'}) for p in ports]
        manifest['requirements'][0]['owners'] = ['u_queue']
        modules = validate_architecture(manifest)
        self.assertNotIn('clk', [p['name'] for p in modules['u_queue']['spec']['ports']])

    def test_fanout_shared_definition_and_reset_domains(self):
        m = self.manifest
        second = copy.deepcopy(m['modules'][0])
        second['instance'] = 'u_spacing2'
        m['modules'].append(second)
        m['top_ports'].append({'name':'block2','direction':'output','width':1})
        m['connections'].extend([{'from':f'top.{p}','to':f'u_spacing2.{p}'} for p in ['clk','rst_n','act_pulse']])
        m['connections'].append({'from':'u_spacing2.tRRD_block','to':'top.block2'})
        modules = validate_architecture(m)
        wrapper = emit_wrapper(m, modules)
        self.assertEqual(wrapper.count('.act_pulse(act_pulse)'), 2)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(len(lower_architecture(m, tmp)), 1)
        m['top_ports'].append({'name':'clk2','direction':'input','width':1})
        m['connections'][-4]['from'] = 'top.clk2'
        with self.assertRaisesRegex(ArchitectureError, 'one top-level clock/reset'):
            validate_architecture(m)

    def test_unused_output_requires_reason_and_emits_open_pin(self):
        m = self.manifest
        m['top_ports'].pop()
        m['connections'].pop()
        with self.assertRaisesRegex(ArchitectureError, 'explicit unused_outputs'):
            validate_architecture(m)
        m['unused_outputs'] = [{'endpoint':'u_spacing.tRRD_block', 'reason':'Spacing monitor not exported by this fixture.'}]
        modules = validate_architecture(m)
        self.assertIn('.tRRD_block()', emit_wrapper(m, modules))
        m['unused_outputs'][0]['reason'] = ''
        with self.assertRaisesRegex(ArchitectureError, 'nonempty text'):
            validate_architecture(m)

    def test_shared_reset_allows_different_assertion_styles_but_not_polarity(self):
        m = self.manifest
        spec = {'design_name':'sync_monitor', 'design_type':'datapath', 'clock':{'name':'clk'},
                'reset':{'name':'rst_n','active_low':True,'synchronous':True},
                'ports':[{'name':'pulse','direction':'input','width':1}, {'name':'seen','direction':'output','width':1}],
                'behavior':{'description':'Register a pulse; synchronous reset clears it.'},
                'combinational_dependencies':{'seen':[]}}
        m['modules'].append({'instance':'u_monitor', 'responsibility':'Observe pulses.', 'spec':spec})
        m['top_ports'].append({'name':'seen','direction':'output','width':1})
        m['connections'].extend([{'from':'top.clk','to':'u_monitor.clk'}, {'from':'top.rst_n','to':'u_monitor.rst_n'},
                                 {'from':'top.act_pulse','to':'u_monitor.pulse'}, {'from':'u_monitor.seen','to':'top.seen'}])
        validate_architecture(m)
        spec['reset']['active_low'] = False
        with self.assertRaisesRegex(ArchitectureError, 'consistent polarity'):
            validate_architecture(m)

    def test_combinational_feedback_fails_but_registered_feedback_is_valid(self):
        m = self.manifest
        def spec(name):
            return {'design_name':name,'design_type':'datapath', 'clock':{'name':'clk'},
                    'reset':{'name':'rst_n','active_low':True,'synchronous':False},
                    'ports':[{'name':'a','direction':'input','width':1}, {'name':'y','direction':'output','width':1}],
                    'behavior':{'description':'Pass through current input.'},
                    'combinational_dependencies':{'y':['a']}}
        m['modules'] = [{'instance':'u_a','responsibility':'First stage.', 'spec':spec('stage_a')},
                        {'instance':'u_b','responsibility':'Second stage.', 'spec':spec('stage_b')}]
        m['top_ports'] = [p for p in m['top_ports'] if p['name'] in {'clk','rst_n','tRRD_block'}]
        m['connections'] = [{'from':f'top.{p}','to':f'{i}.{p}'} for i in ('u_a','u_b') for p in ('clk','rst_n')]
        m['connections'].extend([{'from':'u_a.y','to':'u_b.a'}, {'from':'u_b.y','to':'u_a.a'},
                                 {'from':'u_b.y','to':'top.tRRD_block'}])
        m['requirements'][0]['owners'] = ['u_a','u_b']
        with self.assertRaisesRegex(ArchitectureError, 'combinational feedback cycle'):
            validate_architecture(m)
        m['modules'][1]['spec']['combinational_dependencies']['y'] = []
        validate_architecture(m)
        del m['modules'][1]['spec']['combinational_dependencies']
        with self.assertRaisesRegex(ArchitectureError, 'describe every output'):
            validate_architecture(m)

    def baseline_fixture(self):
        config = read_mapping(ROOT/'configs/user_input.yaml')
        request = build_request(config)
        baseline = request['baseline_contract']
        ports = copy.deepcopy(baseline['host_ports'])
        spec_ports = copy.deepcopy([p for p in ports if p['name'] not in {'clk','rst_n'}])
        spec = {'design_name':'baseline_fixture', 'design_type':'datapath', 'clock':{'name':'clk'},
                'reset':{'name':'rst_n','active_low':True,'synchronous':True}, 'ports':spec_ports,
                'behavior':{'description':'Contract validation fixture; no behavioral implementation is asserted.'},
                'combinational_dependencies':{p['name']:[] for p in ports if p['direction']=='output'}}
        m = {'architecture_version':1,'design_name':baseline['design_name'],'profile':baseline['profile'],
             'rationale':'Validate baseline contract shape without claiming behavior.', 'top_ports':ports,
             'modules':[{'instance':'u_baseline','roles':['command_engine'],'responsibility':'Own fixture ports.', 'spec':spec}],
             'requirements':[dict(r, owners=['u_baseline'], verification='Behavioral testing remains pending.') for r in request['requirements']],
             'connections':[({'from':'top.'+p['name'],'to':'u_baseline.'+p['name']} if p['direction']=='input' else
                             {'from':'u_baseline.'+p['name'],'to':'top.'+p['name']}) for p in ports]}
        # Independent role/interface fixtures exercise structure only; they do
        # not claim to implement a functional controller.
        for role in ('request_queue','scheduler','timing','memory'):
            name = 'u_' + role
            block = {'design_name':role+'_fixture','design_type':'datapath','clock':{'name':'clk'},
                     'reset':{'name':'rst_n','active_low':True,'synchronous':True},
                     'ports':[{'name':'control','direction':'input','width':1}, {'name':'status','direction':'output','width':1}],
                     'behavior':{'description':'Interface fixture only.'}, 'combinational_dependencies':{'status':[]}}
            m['modules'].append({'instance':name,'roles':[role], 'responsibility':'Own '+role+' fixture.', 'spec':block})
            spec['ports'].extend([{'name':role+'_control','direction':'output','width':1},
                                  {'name':role+'_status','direction':'input','width':1}])
            spec['combinational_dependencies'][role+'_control'] = []
            m['connections'].extend([{'from':'top.clk','to':name+'.clk'}, {'from':'top.rst_n','to':name+'.rst_n'},
                                     {'from':'u_baseline.'+role+'_control','to':name+'.control'},
                                     {'from':name+'.status','to':'u_baseline.'+role+'_status'}])
        for r in m['requirements']:
            r['owners'] = [block['instance'] for block in m['modules']]
        return request, m

    def test_baseline_attached_to_controller_requests_with_exact_interface_checks(self):
        request, m = self.baseline_fixture()
        ids = {r['id'] for r in request['requirements']}
        self.assertTrue({'BASE_HOST','BASE_DATA','BASE_TIMING','BASE_SEQUENCE','BASE_LEGALITY'} <= ids)
        prompt = build_prompt(request, library_context())
        self.assertIn('tRCD, tRAS and tRP', prompt)
        self.assertIn('Optional child outputs', prompt)
        m['baseline_contract'] = request['baseline_contract']
        validate_architecture(m)
        m['top_ports'][0]['width'] = 2
        # Structural width mismatch precedes the baseline check.
        with self.assertRaises(ArchitectureError):
            validate_architecture(m)
        m['top_ports'][0]['width'] = 1
        m['design_name'] = 'different_top'
        with self.assertRaisesRegex(ArchitectureError, 'existing top name'):
            validate_architecture(m)

    def test_baseline_proposal_repairs_host_interface_and_cannot_drop_contract(self):
        request, m = self.baseline_fixture()
        bad = copy.deepcopy(m)
        for p in bad['top_ports']:
            if p['name'] == 'txn_addr':
                p['name'] = 'req_addr'
        for c in bad['connections']:
            if c['from'] == 'top.txn_addr':
                c['from'] = 'top.req_addr'
        calls = []
        def caller(prompt, model):
            calls.append(prompt)
            return json.dumps(bad if len(calls)==1 else m)
        with tempfile.TemporaryDirectory() as tmp:
            result = propose(request, output=Path(tmp)/'proposal.yaml', caller=caller)
            self.assertEqual(len(calls), 2)
            self.assertIn('host port missing or changed', calls[1])
            self.assertEqual(result['baseline_contract'], request['baseline_contract'])
            del result['baseline_contract']
            with self.assertRaisesRegex(ArchitectureError, 'regenerate'):
                validate_architecture(result)

    def test_baseline_opt_out_and_bank_widths(self):
        config = read_mapping(ROOT/'configs/user_input.yaml')
        config['microarchitecture'] = {'baseline':'none'}
        self.assertNotIn('baseline_contract', build_request(config))
        del config['microarchitecture']
        for banks, width in [(1,None),(2,1),(4,2)]:
            config['topology']['banks'] = banks
            ports = {p['name']:p['width'] for p in controller_baseline(config)['host_ports']}
            self.assertEqual(ports.get('txn_bank'), width)

    def test_compatibility_preset_allows_fifo_and_configurable_waiting_capacity(self):
        config = read_mapping(ROOT/'configs/user_input.yaml')
        config['features']['scheduler'] = 'fifo'
        config['microarchitecture'] = {'baseline': 'current_controller'}
        for capacity in (1, 3, 4):
            config['microarchitecture']['waiting_capacity'] = capacity
            baseline = build_request(config)['baseline_contract']
            requirements = {r['id']: r['text'] for r in baseline['requirements']}
            self.assertEqual(baseline['id'], 'current_controller_v2')
            self.assertIn(f'{capacity} WAITING', requirements['BASE_QUEUE'])
            self.assertIn('PLUS one separately stored', requirements['BASE_QUEUE'])
            self.assertIn('scheduler policy fifo', requirements['BASE_POLICY'])
            self.assertNotIn('lowest-index', requirements['BASE_POLICY'])
            self.assertIn('tRCD, tRAS and tRP', requirements['BASE_TIMING'])
        del config['microarchitecture']['waiting_capacity']
        requirements = {r['id']: r['text'] for r in controller_baseline(config)['requirements']}
        self.assertIn('4 WAITING', requirements['BASE_QUEUE'])
        # The preset can also be selected by a generic request, without legacy features.
        del config['profile']
        del config['features']
        baseline = build_request(config)['baseline_contract']
        self.assertTrue(any('does not select one' in r['text'] for r in baseline['requirements']))
        config['microarchitecture'] = {'baseline': 'none'}
        self.assertNotIn('baseline_contract', build_request(config))

    def test_compatibility_capacity_cannot_be_invalid_or_silently_ignored(self):
        config = read_mapping(ROOT/'configs/user_input.yaml')
        for value in (0, -1, True, 1.5, '4'):
            config['microarchitecture'] = {'baseline': 'current_controller', 'waiting_capacity': value}
            with self.subTest(value=value), self.assertRaisesRegex(ArchitectureError, 'positive integer'):
                build_request(config)
        config['microarchitecture'] = {'baseline': 'none', 'waiting_capacity': 3}
        with self.assertRaisesRegex(ArchitectureError, 'requires the current_controller preset'):
            build_request(config)

    def test_compatibility_capacity_contract_cannot_be_tampered_with(self):
        request, manifest = self.baseline_fixture()
        manifest['baseline_contract'] = request['baseline_contract']
        manifest['baseline_contract']['config']['microarchitecture'] = {'waiting_capacity': 3}
        with self.assertRaisesRegex(ArchitectureError, 'edited controller baseline'):
            validate_architecture(manifest)

    def test_monolithic_controller_and_dummy_role_split_are_rejected(self):
        request, m = self.baseline_fixture()
        m['baseline_contract'] = request['baseline_contract']
        # An otherwise valid modular fixture cannot assign a second mandatory
        # role to the core, regardless of how many helper modules are present.
        m['modules'][0]['roles'].append('memory')
        with self.assertRaisesRegex(ArchitectureError, 'monolithic cores are prohibited'):
            validate_architecture(m)
        m['modules'][0]['roles'].remove('memory')
        m['modules'][-1]['roles'] = ['host_adapter']
        with self.assertRaisesRegex(ArchitectureError, 'separate modules for roles'):
            validate_architecture(m)

    def test_proposal_exports_resolved_module_yamls_before_any_rtl(self):
        request, m = self.baseline_fixture()
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)/'proposal.yaml'
            result = propose(request, output=output, caller=lambda *_: json.dumps(m))
            index = json.loads(output.with_suffix('.modules.json').read_text())
            self.assertEqual(len(index['modules']), 5)
            self.assertEqual(index['status'], 'specs_ready_verification_not_run')
            for entry in index['modules']:
                path = output.parent/entry['spec_path']
                spec = validate_spec(str(path))
                self.assertEqual(spec.design_name, entry['design_name'])
                self.assertIn('verification_requirements', spec.metadata)
                self.assertTrue(entry['roles'])
            self.assertEqual(len(list(Path(tmp).rglob('*.sv'))), 0)
            self.assertEqual(len(list(Path(tmp).rglob('*_ir.json'))), 0)
            self.assertEqual(result['design_kind'], 'memory_controller')
            cli = subprocess.run([sys.executable, str(ROOT/'run_flow.py'), str(output), '--microarch-only'],
                                 capture_output=True, text=True)
            self.assertEqual(cli.returncode, 0, cli.stdout + cli.stderr)
            self.assertIn('Module YAMLs for verification', cli.stdout)
            self.assertEqual(len(list(Path(tmp).rglob('*.sv'))), 0)
            self.assertEqual(len(list(Path(tmp).rglob('*_ir.json'))), 0)
            index_before = output.with_suffix('.modules.json').read_bytes()
            with self.assertRaises(ArchitectureError):
                propose(request, output=output, caller=lambda *_: '{}')
            self.assertEqual(output.with_suffix('.modules.json').read_bytes(), index_before)

    def test_tFAW_library_permission_cannot_gate_its_own_accepted_pulse(self):
        m = copy.deepcopy(self.manifest)
        gate = {'design_name':'activation_gate','design_type':'datapath','clock':{'name':'clk'},
                'reset':{'name':'rst_n','active_low':True,'synchronous':True},
                'ports':[{'name':'candidate','direction':'input','width':1},
                         {'name':'permit','direction':'input','width':1}, {'name':'accepted','direction':'output','width':1}],
                'behavior':{'description':'Accepted equals candidate and permit.'},
                'combinational_dependencies':{'accepted':['candidate','permit']}}
        m['modules'] = [{'instance':'u_gate','responsibility':'Issue accepted activations.', 'spec':gate},
                        {'instance':'u_tfaw','responsibility':'Track accepted activations.',
                         'reuse':{'feature':'tFAW_tracker','submodule':'tFAW_tracker','design_name':'ddr4_tFAW_tFAW_tracker'}}]
        m['top_ports'] = [p for p in m['top_ports'] if p['name'] in {'clk','rst_n','act_pulse'}]
        m['connections'] = [{'from':f'top.{p}','to':f'{i}.{p}'} for i in ('u_gate','u_tfaw') for p in ('clk','rst_n')]
        m['connections'].extend([{'from':'top.act_pulse','to':'u_gate.candidate'},
                                 {'from':'u_tfaw.tFAW_ok','to':'u_gate.permit'},
                                 {'from':'u_gate.accepted','to':'u_tfaw.act_pulse'}])
        m['unused_outputs'] = [{'endpoint':'u_tfaw.tFAW_block','reason':'Permission uses tFAW_ok.'},
                               {'endpoint':'u_tfaw.act_count','reason':'Debug count not exported.'}]
        m['requirements'][0]['owners'] = ['u_gate','u_tfaw']
        with self.assertRaisesRegex(ArchitectureError, 'combinational feedback cycle'):
            validate_architecture(m)

    def test_baseline_original_requirements_and_omission_checks(self):
        config = read_mapping(ROOT/'configs/user_input.yaml')
        config['requirements'] = ['Preserve the controller while changing its decomposition.']
        request = build_request(config)
        self.assertNotIn('requirements', request['baseline_contract']['config'])
        self.assertEqual(request['requirements'][0]['text'], config['requirements'][0])
        request, m = self.baseline_fixture()
        m['baseline_contract'] = request['baseline_contract']
        m['requirements'] = [r for r in m['requirements'] if r['id'] != 'BASE_DATA']
        with self.assertRaisesRegex(ArchitectureError, 'baseline requirement missing or rewritten: BASE_DATA'):
            validate_architecture(m)

    def test_proposal_repair_coverage_and_cache(self):
        invalid = copy.deepcopy(self.manifest)
        invalid['requirements'][0]['text'] = 'Dropped the original request'
        calls = []
        def caller(prompt, model):
            calls.append(prompt)
            return json.dumps(invalid if len(calls) == 1 else self.manifest)
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'proposal.yaml'
            result = propose(self.request, output=output, caller=caller)
            self.assertEqual(len(calls), 2)
            self.assertIn('missing or rewritten', calls[1])
            self.assertEqual(len(result['provenance']['validation_repairs']), 1)
            with patch('generate_microarch.call_llm', side_effect=AssertionError('network called')):
                self.assertEqual(propose(self.request, output=output, cache=True), result)
                changed = copy.deepcopy(self.request)
                changed['constraints']['banks'] = 16
                with self.assertRaisesRegex(ArchitectureError, 'stale'):
                    propose(changed, output=output, cache=True)
            old = output.read_bytes()
            with self.assertRaisesRegex(ArchitectureError, 'failed validation'):
                propose(self.request, output=output, caller=lambda *_: '{bad JSON')
            self.assertEqual(output.read_bytes(), old)
            with self.assertRaisesRegex(ArchitectureError, 'mutually exclusive'):
                propose(self.request, output=output, cache=True, response_path=EXAMPLE)

    def test_invalid_architecture_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'must_not_exist'
            self.manifest['connections'].clear()
            with self.assertRaises(ArchitectureError):
                lower_architecture(self.manifest, target)
            self.assertFalse(target.exists())

    @unittest.skipUnless(shutil.which('verilator'), 'Verilator required')
    def test_new_fsm_decomposition_builds_without_feature_registration(self):
        m = copy.deepcopy(self.manifest)
        spec = {'design_name':'experiment_control', 'design_type':'fsm',
                'clock':{'name':'clk'}, 'reset':{'name':'rst_n','active_low':True,'synchronous':True},
                'ports':[{'name':'start','direction':'input','width':1},
                         {'name':'busy','direction':'output','width':1}],
                'state_machine':{'states':['IDLE','BUSY'], 'reset_state':'IDLE',
                                 'transitions':[{'from':'IDLE','to':'BUSY','condition':'start == 1'},
                                                {'from':'BUSY','to':'IDLE','condition':'start == 0'}]},
                'output_overrides':{'busy':'(current_state == BUSY)'}}
        m['modules'] = [{'instance':'u_new','responsibility':'Own a new two-state control experiment.', 'spec':spec}]
        m['top_ports'] = [{'name':n,'direction':d,'width':1} for n,d in
                          [('clk','input'),('rst_n','input'),('start','input'),('busy','output')]]
        m['connections'] = [{'from':f'top.{p}','to':f'u_new.{p}'} for p in ['clk','rst_n','start']]
        m['connections'].append({'from':'u_new.busy','to':'top.busy'})
        m['requirements'][0]['owners'] = ['u_new']
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'manifest.yaml'
            path.write_text(yaml.safe_dump(m))
            build = Path(tmp) / 'build'
            result = subprocess.run([sys.executable, str(ROOT/'run_flow.py'), str(path),
                                     '--build-dir',str(build)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue((build/'rtl_output/experiment_control.sv').exists())
            self.assertTrue((build/'microarch/ir/experiment_control_ir.json').exists())

    def test_parse_response_and_unrestricted_requirements(self):
        self.assertEqual(parse_response('```json\n{"x":1}\n```'), {'x':1})
        for text in ('[]', '{bad'):
            with self.assertRaises(ArchitectureError):
                parse_response(text)
        request = build_request({'topology':{'banks':16}, 'requirements':['Explore per-bank execution.']})
        self.assertEqual(request['constraints']['topology']['banks'], 16)
        self.assertEqual(request['requirements'][0]['text'], 'Explore per-bank execution.')

    @unittest.skipUnless(shutil.which('verilator'), 'Verilator required')
    def test_flow_generation_cache_and_actual_timing_simulation(self):
        with tempfile.TemporaryDirectory() as tmp:
            output, build = Path(tmp) / 'proposal.yaml', Path(tmp) / 'build'
            command = [sys.executable, str(ROOT / 'run_flow.py'), str(REQUEST), '--microarch',
                       '--microarch-response', str(EXAMPLE), '--architecture-output', str(output), '--build-dir', str(build)]
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            status = json.loads((build / 'build_status.json').read_text())
            self.assertEqual(status['integration_lint'], 'passed')
            self.assertEqual(status['behavioral_verification'], 'not_run')
            cached = command[:]
            idx = cached.index('--microarch-response')
            del cached[idx:idx+2]
            cached.append('--cache')
            result = subprocess.run(cached, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            shell = subprocess.run(['bash',str(ROOT/'start_flow.sh'), '--input',str(REQUEST),
                                    '--microarch-response',str(EXAMPLE), '--architecture-output',str(output),
                                    '--microarch-only'], cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(shell.returncode, 0, shell.stdout + shell.stderr)
            self.assertEqual(shell.stdout.count('Proposal ready for review'), 1)
            self.assertNotIn('Describe the DDR4 controller', shell.stdout)
            rtl = build / 'rtl_output'
            tb = rtl / 'timing_tb.sv'
            tb.write_text('''`timescale 1ns/1ps
module timing_tb;
  logic clk=0, rst_n=1, act_pulse=0, blocked;
  always #5 clk=~clk;
  timing_demo dut(.clk(clk),.rst_n(rst_n),.act_pulse(act_pulse),.tRRD_block(blocked));
  initial begin
    #1; rst_n=0; #1; if (blocked !== 0) $fatal(1,"reset");
    @(negedge clk); rst_n=1; act_pulse=1;
    @(posedge clk); #1; if (!blocked) $fatal(1,"activation");
    @(negedge clk); act_pulse=0;
    repeat (4) begin @(posedge clk); #1; if (!blocked) $fatal(1,"early clear"); end
    @(posedge clk); #1; if (blocked) $fatal(1,"late clear");
    $display("TIMING SIM PASS"); $finish;
  end
endmodule
''')
            sim_dir = Path(tmp) / 'sim'
            compile_result = subprocess.run(['verilator','--binary','--timing','-Wno-fatal','--top-module','timing_tb',
                                             '--Mdir',str(sim_dir),'-f','filelist.f',str(tb)], cwd=rtl, capture_output=True, text=True)
            self.assertEqual(compile_result.returncode, 0, compile_result.stdout + compile_result.stderr)
            simulation = subprocess.run([str(sim_dir / 'Vtiming_tb')], capture_output=True, text=True)
            self.assertEqual(simulation.returncode, 0, simulation.stdout + simulation.stderr)
            self.assertIn('TIMING SIM PASS', simulation.stdout)


if __name__ == '__main__':
    unittest.main()
