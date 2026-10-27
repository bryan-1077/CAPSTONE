"""Offline campaign/planner context and independent isolated timing checks."""
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import yaml
from architecture import fingerprint
from campaigns.timing import cases
from generate_specification import generation_context, build_prompt, propose_specification
from planning_library import planning_catalog, planning_index
from prepare_timing_campaign import prepare
from simulation import timing_guard as timing
from simulation.controller import CheckFailure
from specification import SpecificationError
from specification_request import build_specification_request
from test_generate_specification import register_fixture
from test_single_request_simulation import fixture


def guard_fixture():
    h=fixture()
    h['design']['modules']=[{'module_name':'fixture_timing','instance':'guard','roles':['timing'],
        'clock':{'name':'clk'},'reset':{'name':'rst_n','active_low':True,'synchronous':True},
        'ports':[{'name':n,'direction':d,'width':w} for n,(d,w) in timing.PORTS.items()]}]
    return h


def row(c,*,reset=False,cv=0,cmd=0,kv=0,commit=0,banks=0,rows=0,ok=0):
    return {'cycle':c,'rst_n':int(not reset),'candidate_valid':cv,'candidate_command':cmd,
            'commit_valid':kv,'commit_command':commit,'open_valid':banks,'open_rows':rows,'candidate_ok':ok}


class TimingCampaignTests(unittest.TestCase):
    def test_catalog_is_compact_optional_and_does_not_claim_verification(self):
        catalog=planning_catalog()
        self.assertIn('tFAW_tracker',catalog['entries'])
        self.assertIn('host_protocol',catalog['accepted_contract_roles'])
        self.assertTrue(any('Project simplification' in note for note in catalog['timing_source_notes']))
        self.assertLess(len(json.dumps(catalog)),20000)
        for item in catalog['entries'].values():
            self.assertEqual(item['verification_status'],'not_established_by_catalog')
            for block in item['submodules']:
                self.assertNotIn('state_machine',block)
                self.assertNotIn('behavior',block)
        proof=copy.deepcopy(catalog);digest=proof.pop('catalog_fingerprint')
        self.assertEqual(fingerprint(proof),digest)

    def test_catalog_changes_are_in_specification_cache_identity(self):
        context=generation_context()
        self.assertEqual(context['local_library_catalog'],planning_index(planning_catalog()))
        changed=planning_catalog()
        changed['entries']['tRRD']['description']+=' changed interface evidence'
        changed['catalog_fingerprint']=fingerprint(changed)
        with patch('planning_library.planning_catalog',return_value=changed):
            self.assertNotEqual(fingerprint(generation_context()),fingerprint(context))
        for stage in ('system','architecture'):
            prompt=build_prompt(stage,{'requirements':[]},context)
            self.assertIn('local_library_catalog',prompt)
            self.assertIn('optional_unverified_features',prompt)
        self.assertNotIn('axi_adapter',planning_catalog()['accepted_contract_roles'])

    def test_catalog_template_and_dependency_changes_invalidate_fingerprint(self):
        from architecture import library_context
        original=planning_catalog()
        for field in ('templates','combinational_dependencies'):
            lib=library_context()
            if field=='templates':lib[field]['tRRD']['description']+=' revised'
            else:lib[field]['tRRD']['tRRD_block']=['act_pulse']
            with self.subTest(field=field),patch('planning_library.library_context',return_value=lib):
                self.assertNotEqual(planning_catalog()['catalog_fingerprint'],original['catalog_fingerprint'])

    def test_campaign_preparation_is_offline_fresh_and_never_reports_design_pass(self):
        with tempfile.TemporaryDirectory() as directory,patch('requests.post',side_effect=AssertionError('No LLM')):
            root=Path(directory)/'campaign'
            path=prepare(root);manifest=json.loads(path.read_text())
            self.assertEqual(manifest['status'],'prepared_not_generated')
            self.assertEqual(len(manifest['cases']),7)
            self.assertEqual(manifest['planning_context_fingerprint'],fingerprint(json.loads((root/'planning_context.json').read_text())))
            self.assertEqual(sum(c['checker_status']=='executable' for c in manifest['cases']),2)
            for case in manifest['cases']:
                self.assertEqual(case['behavioral_status'],'not_run')
                config=yaml.safe_load((root/case['request']).read_text())
                self.assertEqual(fingerprint(build_specification_request(config)),case['request_fingerprint'])
            before=path.read_bytes()
            with self.assertRaises(FileExistsError):prepare(root)
            self.assertEqual(path.read_bytes(),before)

    def test_campaign_profiles_and_width_edges_have_explicit_acceptance(self):
        definitions=cases()
        self.assertEqual([c['config']['parameters']['T_CYCLES'] for c in definitions if c['family']=='reloadable_timer'],[1,2,31,32,33])
        for case in definitions[:2]:
            self.assertEqual(case['required_checks'],list(timing.CHECKS))
            self.assertIn('Only commit_valid ACT',case['config']['requirements'][0])
            self.assertIn('No host, queue',case['config']['requirements'][0])
        self.assertEqual([c['expected_cycles']['tRRD'] for c in definitions[:2]],[4,6])
        self.assertTrue(all(c['checker_profile'] is None for c in definitions[2:]))

    def test_new_planning_context_rejects_stale_generation_cache_without_paid_calls(self):
        _,request,replay=register_fixture()
        with tempfile.TemporaryDirectory() as directory,redirect_stdout(io.StringIO()), \
             patch('requests.post',side_effect=AssertionError('No LLM')):
            output=Path(directory)/'source'
            replies=iter([json.dumps(replay['system']),json.dumps(replay['architecture'])])
            propose_specification(request,output_dir=output,caller=lambda *args:next(replies))
            self.assertEqual(json.loads((output/'library_catalog.json').read_text()),planning_catalog())
            catalog=planning_catalog();catalog['policy']['reuse']+=' changed';catalog['catalog_fingerprint']=fingerprint(catalog)
            with patch('planning_library.planning_catalog',return_value=catalog):
                with self.assertRaisesRegex(SpecificationError,'stale'):
                    propose_specification(request,output_dir=output,cache=True)

    def test_timing_profile_validates_module_role_ports_reset_and_stubs(self):
        original=guard_fixture();self.assertEqual(timing.profile_check(original)['module_name'],'fixture_timing')
        mutations=[lambda h:h['design']['modules'][0].update(roles=[]),
                   lambda h:h['design']['modules'][0]['ports'].pop(),
                   lambda h:h['design']['modules'][0]['reset'].update(synchronous=False),
                   lambda h:h['shared']['parameters'].update(BANK_COUNT=2),
                   lambda h:h['shared']['timing_profile']['values'].update(tRFC=2),
                   lambda h:h['shared']['types']['bank_rows'].update(packed_order='other')]
        for mutate in mutations:
            h=copy.deepcopy(original);mutate(h)
            with self.subTest(mutate=mutate),self.assertRaises(ValueError):timing.profile_check(h)
        tb=timing.testbench(original)
        self.assertIn('fixture_timing dut',tb)
        self.assertIn('logic [91:0] data_arg',tb)
        self.assertNotIn('dut.guard.',tb)

    def test_tRRD_permission_uses_pre_edge_history_and_exact_boundary(self):
        oracle=timing.TimingOracle(guard_fixture()['shared'])
        oracle.step(row(0,reset=True))
        # Same-edge ACT probe sees old history, and the commit affects later edges.
        oracle.step(row(1,cv=1,cmd=timing.command(1),ok=1,kv=1,commit=timing.command(1)))
        oracle.step(row(2,cv=1,cmd=timing.command(1,1),ok=0))
        oracle.step(row(3,cv=1,cmd=timing.command(1,1),ok=1))
        oracle.step(row(4,cv=1,cmd=timing.command(1,1),ok=1))
        self.assertEqual(oracle.counts['tRRD:T-1'],1)
        self.assertEqual(oracle.counts['tRRD:T'],1)
        self.assertEqual(oracle.counts['tRRD:T+1'],1)

    def test_four_act_window_excludes_expiring_event_and_accepts_new_commit(self):
        shared=guard_fixture()['shared'];shared['timing_profile']['values']['tRRD']=1
        oracle=timing.TimingOracle(shared)
        oracle.step(row(0,reset=True))
        for c in range(1,5):oracle.step(row(c,kv=1,commit=timing.command(1,c-1)))
        for c in range(5,9):oracle.step(row(c,cv=1,cmd=timing.command(1),ok=0))
        oracle.step(row(9,cv=1,cmd=timing.command(1),ok=1,kv=1,commit=timing.command(1)))
        oracle.step(row(10,cv=1,cmd=timing.command(1,1),ok=1))
        self.assertGreater(oracle.counts['tFAW:blocked_only'],0)
        self.assertEqual(oracle.counts['tFAW:expiry_with_commit'],1)

    def test_banked_histories_and_reset_do_not_create_delays(self):
        oracle=timing.TimingOracle(guard_fixture()['shared'])
        oracle.step(row(0,reset=True));oracle.step(row(1,kv=1,commit=timing.command(1,0,3)))
        oracle.step(row(2,cv=1,cmd=timing.command(3,1,4),banks=3,rows=0x43,ok=1))
        oracle.step(row(3,cv=1,cmd=timing.command(3,0,3),banks=1,rows=3,ok=0))
        oracle.step(row(4,cv=1,cmd=timing.command(4,0,3),banks=1,rows=3,ok=1))
        oracle.step(row(5,cv=1,cmd=timing.command(2),banks=1,rows=3,ok=0))
        oracle.step(row(6,cv=1,cmd=timing.command(2),banks=1,rows=3,ok=1))
        oracle.step(row(7,reset=True,kv=1,commit=timing.command(1)))
        oracle.step(row(8,cv=1,cmd=timing.command(1),ok=1))
        self.assertEqual(oracle.history,[])

    def test_mutated_early_late_reset_and_window_permissions_fail(self):
        for c,ok in ((2,1),(3,0)):
            oracle=timing.TimingOracle(guard_fixture()['shared'])
            oracle.step(row(0,reset=True));oracle.step(row(1,kv=1,commit=timing.command(1)))
            with self.subTest(cycle=c),self.assertRaises(CheckFailure):
                oracle.step(row(c,cv=1,cmd=timing.command(1,1),ok=ok))
        oracle=timing.TimingOracle(guard_fixture()['shared'])
        with self.assertRaises(CheckFailure):oracle.step(row(0,reset=True,cv=1,ok=1))
        shared=guard_fixture()['shared'];shared['timing_profile']['values']['tRRD']=1
        oracle=timing.TimingOracle(shared)
        for c in range(4):oracle.step(row(c,kv=1,commit=timing.command(1,c)))
        with self.assertRaises(CheckFailure):oracle.step(row(4,cv=1,cmd=timing.command(1),ok=1))

    def test_only_valid_act_commits_create_history(self):
        oracle=timing.TimingOracle(guard_fixture()['shared'])
        for c in range(12):oracle.step(row(c,cv=1,cmd=timing.command(1),ok=1,commit=timing.command(1)))
        for c,op in enumerate((0,2,3,4,7),12):oracle.step(row(c,kv=1,commit=timing.command(op)))
        self.assertEqual(oracle.history,[])
        oracle.step(row(17,cv=1,cmd=timing.command(1),ok=1))

    def test_seeded_scenarios_are_reproducible_and_not_oracle_derived(self):
        shared=guard_fixture()['shared'];original=copy.deepcopy(shared)
        self.assertEqual(timing.scenarios(shared,2),timing.scenarios(shared,2))
        self.assertNotEqual(timing.scenarios(shared,2)['seeded_history'],timing.scenarios(shared,3)['seeded_history'])
        self.assertEqual(shared,original)
        for name,vectors in timing.scenarios(shared,1).items():
            self.assertTrue(vectors,name)
            self.assertTrue(all(amount>0 for _,amount,*_ in vectors))

    def test_trace_requires_actual_boundary_coverage_and_rejects_empty_input(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'trace'
            rows=[row(0,reset=True),row(1,cv=1,cmd=timing.command(1),ok=1)]
            keys=list(rows[0])
            path.write_text(' '.join(keys)+'\n'+''.join(' '.join(f'{r[k]:x}' for k in keys)+'\n' for r in rows))
            self.assertEqual(timing.check_trace(path,guard_fixture()['shared'],'reset_and_invalid')['status'],'failed')
            result=timing.check_trace(path,guard_fixture()['shared'],'tRRD_global')
            self.assertEqual(result['failure']['check'],'scenario_coverage')
            path.write_text('')
            with self.assertRaises(ValueError):timing.check_trace(path,guard_fixture()['shared'],'tRRD_global')


if __name__=='__main__':unittest.main()
