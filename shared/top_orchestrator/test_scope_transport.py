"""Exercise real upload/runner/download with a synthetic scoped consumer."""
from dataclasses import replace
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from .contracts import RunConfig
from .test_validation import LocalSSH
from . import test_verification_scope as fixtures
from .validation import validation_node

PIPELINE = '''import argparse, hashlib, json, socket, sys
from pathlib import Path
mode = Path(__file__).with_name('mode').read_text()
if '--scope-capabilities' in sys.argv:
    if mode == 'legacy': sys.exit(2)
    if mode == 'network': socket.create_connection(('example.invalid',443), timeout=1)
    if mode == 'discovery_mutation':
        p=Path(__file__); p.write_text(p.read_text()+'\\n# changed')
    if mode == 'discovery_plan_mutation':
        p=Path.cwd().parent/'execution_plan.json'; p.write_text(p.read_text()+'\\n')
    print(json.dumps(dict(schema='capstone.module_execution_capabilities',schema_version=1,
          capabilities=['capstone.module_execution_plan.v1'], request_flag='--execution-request',
          results_flag='--execution-results',consumer_files=[Path(__file__).name,'requirement_verification.py'])))
    sys.exit(0)
parser=argparse.ArgumentParser()
parser.add_argument('rtl'); parser.add_argument('specs'); parser.add_argument('--jobs',type=int)
parser.add_argument('--execution-request',required=True); parser.add_argument('--execution-results',required=True)
args=parser.parse_args()
Path(__file__).with_name('executed').write_text('synthetic agent execution only')
request_path=Path(args.execution_request)
request=json.loads(request_path.read_text())
plan_path=request_path.parent/request['plan_file']
plan=json.loads(plan_path.read_text())
rtl,specs=Path(args.rtl),Path(args.specs)
reports=Path(args.execution_results).parent
(reports/'this_run').mkdir(parents=True)
artifact=reports/'this_run/simulation.log'
artifact.write_text('Synthetic evidence fixture; no real simulation or models.')
evidence=[dict(path='this_run/simulation.log',sha256=hashlib.sha256(artifact.read_bytes()).hexdigest())]
body=dict(schema='capstone.module_execution_results',schema_version=1,identity=plan['identity'],
          plan_sha256=plan['plan_sha256'],consumer_capability=plan['consumer_capability'],
          scope='module_checks_only',backend_ready=False,
          executed_tests=[dict(id='fake-test',method='simulation',status='passed',execution_completed=True,
                               execution_succeeded=True,evidence=evidence)],
          checks=[dict(id=row['id'],method=row['method'],status='passed',
                       test_ids=['fake-test'] if row['method']=='simulation' else [],evidence=evidence)
                  for row in plan['checks']],
          source_results={key:dict(status='unverified' if row['scope']=='system' else 'passed',check_ids=row['checks'])
                          for key,row in plan['assignments'].items()})
if mode=='deferred_pass': body['source_results']['R1']['status']='passed'
if mode=='wrong_identity': body['identity']=dict(body['identity'],rtl_revision='stale')
if mode=='missing_check': body['checks'].pop()
if mode=='bad_method': body['checks'][0]['method']='static'
if mode=='no_execution': body['executed_tests'][0]['execution_completed']=False
if mode=='artifact_mutation': artifact.write_text('changed after hashing')
if mode=='input_mutation':
    p=next(specs.glob('*.yaml')); p.write_text(p.read_text()+'\\n# changed')
if mode=='plan_mutation': plan_path.write_text(plan_path.read_text()+'\\n')
if mode=='consumer_mutation':
    p=Path(__file__).with_name('requirement_verification.py'); p.write_text(p.read_text()+'\\n# changed')
Path(args.execution_results).write_text(json.dumps(body))
def passed():
    return dict(overall_status='PASS',verification_complete=True,execution_completed=True,
                execution_succeeded=True,needs_review=False,requested_stages=['agent1','agent2','agent3'],
                stages={name:dict(status='PASS',exit_code=0) for name in ['agent1','agent2','agent3']})
summary=passed()
summary.update(input_directory=str(rtl.parent),inputs=[str(rtl),str(specs)],kind='batch',
               designs=[dict(module_name=plan['identity']['module_name'],
                             yaml_file=str(next(specs.glob('*.yaml'))),result=passed())])
(reports/'this_run/summary.json').write_text(json.dumps(summary))
if mode=='exit_nonzero': sys.exit(1)
'''


class ScopeTransportTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.VerificationScopeTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.root, self.mailbox, self.plan = fixture.root, fixture.mailbox, fixture.plan
        self.plan_path = self.root / 'selected_plan.json'
        self.plan_path.write_text(json.dumps(self.plan))
        self.server = self.root / 'server project'
        self.server.mkdir()
        (self.server / 'pipeline.py').write_text(PIPELINE)
        (self.server / 'requirement_verification.py').write_text('# declared synthetic consumer dependency\n')
        (self.server / 'mode').write_text('pass')
        remote = self.root / 'remote.json'
        remote.write_text(json.dumps(dict(host='fake',username='fake',project_dir=str(self.server),
             partition='fake',qos='fake',python=sys.executable,validation_script='pipeline.py',setup_commands=[])))
        self.config = RunConfig('validation',mailbox=self.mailbox,module_probe='unit',scope_plan=self.plan_path,
                                frontend_flow='specification',remote_config=remote)
        self.count = 0
        LocalSSH.instances = []

    def execute(self, mode='pass', preflight=False, transport=LocalSSH):
        (self.server / 'mode').write_text(mode)
        output = self.root / ('run_%03d' % self.count)
        self.count += 1
        output.mkdir()
        with patch('shared.top_orchestrator.validation.SSHExecutor',transport):
            result = validation_node(replace(self.config,preflight_only=preflight),output)
        return result,json.loads((output/'validation_result.json').read_text()),output

    def test_real_transport_preserves_plan_and_pending_system_dispositions(self):
        result, receipt, output = self.execute()
        self.assertEqual(result.status,'passed',result.message)
        self.assertEqual(receipt['verification_scope'],'module_checks_only')
        accepted=receipt['scoped_acceptance']
        self.assertIn('R1',accepted['deferred_system_sources'])
        self.assertEqual(accepted['remote_invocation_authentication'],'checked_by_validation_adapter')
        self.assertFalse(accepted['backend_ready'])
        self.assertEqual((output/'execution_plan.json').read_bytes(),self.plan_path.read_bytes())
        self.assertIn('--execution-request',receipt['remote_result']['command'])
        self.assertNotIn('source_coverage',receipt['remote_result'])

    def test_preflight_never_runs_agents(self):
        result,receipt,_=self.execute(preflight=True)
        self.assertEqual(result.status,'passed',result.message)
        self.assertEqual(receipt['phase'],'consumer_preflight_complete')
        self.assertIsNone(receipt['remote_result']['pipeline_returncode'])
        self.assertFalse((self.server/'executed').exists())

    def test_unsupported_network_or_changed_capability_stops_before_agents(self):
        for mode in ('legacy','network','discovery_mutation','discovery_plan_mutation'):
            (self.server/'pipeline.py').write_text(PIPELINE)
            result,receipt,_=self.execute(mode)
            with self.subTest(mode=mode):
                self.assertEqual(result.status,'needs_attention',result.message)
                self.assertFalse((self.server/'executed').exists())
                self.assertIsNone(receipt['remote_result']['pipeline_returncode'])

    def test_body_evidence_and_revision_failures_are_not_accepted(self):
        for mode in ('deferred_pass','wrong_identity','missing_check','bad_method','no_execution','artifact_mutation','exit_nonzero'):
            result,receipt,_=self.execute(mode)
            with self.subTest(mode=mode):
                self.assertNotEqual(result.status,'passed',result.message)
                self.assertNotEqual(receipt.get('scoped_acceptance',{}).get('remote_invocation_authentication'),
                                    'checked_by_validation_adapter')

    def test_changed_inputs_plan_and_consumer_are_rejected(self):
        for mode in ('input_mutation','plan_mutation','consumer_mutation'):
            (self.server/'requirement_verification.py').write_text('# declared synthetic dependency\n')
            result,receipt,_=self.execute(mode)
            with self.subTest(mode=mode):
                self.assertNotEqual(result.status,'passed',result.message)
                expected = 'hashes' if mode == 'input_mutation' else 'changed'
                self.assertIn(expected,receipt['message'].lower())

    def test_modified_plan_upload_rejected_before_agents(self):
        class ChangedUpload(LocalSSH):
            def upload_file(self,local,remote,exclusive=False):
                super().upload_file(local,remote,exclusive)
                if Path(remote).name=='execution_plan.json':
                    path=Path(remote); path.write_text(path.read_text()+'\n')
        result,_,_=self.execute(transport=ChangedUpload)
        self.assertNotEqual(result.status,'passed')
        self.assertFalse((self.server/'executed').exists())

    def test_dry_run_validates_plan_without_remote_or_outputs(self):
        from .__main__ import main
        output=self.root/'preview'
        argv=['orchestrator','validation','--mailbox',str(self.mailbox),'--module-probe','unit',
              '--scope-plan',str(self.plan_path),'--preflight-only','--plan','--run-dir',str(output)]
        with patch('sys.argv',argv),patch('sys.stdout',io.StringIO()) as text,patch(
                'getpass.getpass',side_effect=AssertionError('prompt')),patch(
                'shared.top_orchestrator.remote.RemoteConfig.load',side_effect=AssertionError('remote setup')):
            self.assertEqual(main(),0)
        self.assertIn('No legacy fallback',text.getvalue())
        self.assertFalse(output.exists())

    def test_result_embedded_in_receipt_must_match_archive(self):
        class ChangedReceipt(LocalSSH):
            def fetch_file(self,remote,local):
                result=super().fetch_file(remote,local)
                if result.get('ok') and Path(remote).name=='result.json':
                    payload=json.loads(Path(local).read_text())
                    payload['scoped_result']['source_results']['R1']['status']='passed'
                    Path(local).write_text(json.dumps(payload))
                return result
        result,receipt,_=self.execute(transport=ChangedReceipt)
        self.assertNotEqual(result.status,'passed')
        self.assertIn('body differs',receipt['message'])

    def test_scoped_plan_cannot_start_legacy_batch(self):
        from .specification_validation import run_modules
        with patch('shared.top_orchestrator.validation.SSHExecutor',side_effect=AssertionError('SSH')):
            with self.assertRaisesRegex(ValueError,'per-module plans'):
                run_modules(self.config,self.root/'batch')
        self.assertFalse((self.root/'batch').exists())

    def test_scoped_failure_cannot_lose_plan_evidence_in_legacy_repair(self):
        from .specification_repair import prepare_workspace
        _,_,output=self.execute('bad_method')
        with self.assertRaisesRegex(ValueError,'Scoped failure repair mapping'):
            prepare_workspace(self.mailbox,output,self.root/'repair')
        self.assertFalse((self.root/'repair').exists())
