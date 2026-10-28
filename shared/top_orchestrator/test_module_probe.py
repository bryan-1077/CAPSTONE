"""Real upload/runner/receipt path against local fake agents; no SSH or models."""
import json
from pathlib import Path
import shutil
import sys
import unittest
from dataclasses import replace
from unittest.mock import patch

from .contracts import RunConfig
from .module_probe import expected_source_catalog, verify_source_coverage
from .validation import validation_node, verify_backend_handoff
from .test_validation import LocalSSH
from . import test_specification_mailbox as fixtures

PIPELINE = '''import argparse, json
from pathlib import Path
import yaml
import requirement_verification as reader
parser = argparse.ArgumentParser()
parser.add_argument('rtl'); parser.add_argument('specs'); parser.add_argument('--jobs', type=int)
args = parser.parse_args()
Path(__file__).with_name('executed').write_text('fake agent only')
rtl, specs = Path(args.rtl), Path(args.specs)
root = rtl.parent
mode = Path(__file__).with_name('mode').read_text()
source = next(specs.glob('*.yaml'))
spec = yaml.safe_load(source.read_text())
catalog = reader.build_requirement_catalog(spec)
reader.attach_static_results(catalog, next(rtl.glob('*.sv')).read_text())
combined = reader.combine_requirement_results(catalog, {'overall_status':'PASS',
    'requirements':[{'id':row['id'], 'status':'PASS', 'evidence':'fake execution fixture'} for row in catalog]})
if mode == 'missing_sources':
    for row in combined['requirements']: row.pop('source_id', None)
if mode == 'incomplete':
    for row in combined['requirements']:
        if row.get('source_id'): row['status'] = 'NOT_TESTED'
if mode == 'mutate_input': source.write_text(source.read_text()+'\\n# modified')
if mode == 'mutate_consumer':
    path = Path(reader.__file__); path.write_text(path.read_text()+'\\n# modified')
def passed():
    return dict(overall_status='PASS', verification_complete=True, execution_completed=True,
        execution_succeeded=True, needs_review=False, requested_stages=['agent1','agent2','agent3'],
        stages={name:dict(status='PASS',exit_code=0) for name in ('agent1','agent2','agent3')})
summary = passed()
summary.update(input_directory=str(root),inputs=[str(rtl),str(specs)],run_id='module_fixture',kind='batch',
    designs=[dict(module_name=spec['design_name'],yaml_file=str(source),result=passed())])
out=root/'verification_reports/module_fixture';out.mkdir(parents=True)
(out/'summary.json').write_text(json.dumps(summary))
(out/'requirements.json').write_text(json.dumps(combined))
'''


class ModuleProbeTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.SpecificationMailboxTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.root = fixture.root
        self.mailbox = fixture.publish()
        self.server = self.root / 'server with spaces'
        self.server.mkdir()
        (self.server / 'pipeline.py').write_text(PIPELINE)
        (self.server / 'mode').write_text('pass')
        reader = Path(__file__).resolve().parents[2] / 'server/validation/requirement_verification.py'
        shutil.copyfile(reader, self.server / reader.name)
        config = self.root / 'remote.json'
        config.write_text(json.dumps(dict(host='fake', username='fake', project_dir=str(self.server),
            partition='fake',qos='fake',python=sys.executable,validation_script='pipeline.py',
            setup_commands=[], validation_mode='system')))
        self.config = RunConfig('validation',mailbox=self.mailbox,module_probe='unit',remote_config=config)
        LocalSSH.instances = []

    def execute(self, preflight=False, transport=LocalSSH):
        output = self.root / 'run'
        output.mkdir()
        with patch('shared.top_orchestrator.validation.SSHExecutor',transport):
            result = validation_node(replace(self.config,preflight_only=preflight),output)
        receipt = json.loads((output/'validation_result.json').read_text())
        return result, receipt

    def test_preflight_proves_reader_and_cli_without_agent_execution(self):
        result, receipt = self.execute(preflight=True)
        self.assertEqual(result.status,'passed',result.message)
        self.assertEqual(receipt['phase'],'consumer_preflight_complete')
        self.assertFalse(receipt['backend_ready'])
        self.assertEqual(receipt['system_verification'],'not_run')
        self.assertEqual(receipt['remote_result']['pipeline_returncode'],None)
        self.assertEqual(receipt['remote_result']['consumer_preflight']['status'],'passed')
        self.assertFalse((self.server/'executed').exists())

    def test_executed_module_receipt_requires_complete_source_coverage(self):
        result, receipt = self.execute()
        self.assertEqual(result.status,'passed',result.message)
        payload = receipt['remote_result']
        self.assertEqual(payload['validation_mode'],'modules')
        self.assertEqual(payload['source_coverage']['source_ids'],['R1','behavior.contract','system.verification.V1'])
        self.assertEqual(receipt['verification_scope'],'one_module_only')
        self.assertEqual(payload['system_verification'],'not_run')
        self.assertFalse(receipt['backend_ready'])

    def test_collection_rechecks_archive_and_keeps_system_pending(self):
        from .specification_validation import collect_modules
        result, _ = self.execute()
        self.assertEqual(result.status, 'passed', result.message)
        run = self.root / 'run'
        collection = collect_modules(self.mailbox, [run])
        self.assertEqual(collection['module_coverage'], 'complete', collection['rejected_receipts'])
        self.assertFalse(collection['backend_ready'])
        self.assertEqual(collection['system_verification']['status'], 'not_run')
        self.assertEqual(collection['modules'][-1]['status'], 'not_run')
        with (run / 'reports.zip').open('ab') as handle:
            handle.write(b'changed')
        rejected = collect_modules(self.mailbox, [run])
        self.assertEqual(rejected['module_coverage'], 'incomplete')
        self.assertIn('archive changed', rejected['rejected_receipts'][0]['reason'])

    def test_collection_rejects_preflight_and_duplicate_evidence(self):
        from .specification_validation import collect_modules
        self.execute(preflight=True)
        collection = collect_modules(self.mailbox, [self.root / 'run'])
        self.assertEqual(collection['module_coverage'], 'incomplete')
        self.assertEqual(collection['pending_modules'], ['unit'])

    def test_collection_rejects_stale_parent_and_duplicate_receipts(self):
        from .specification_validation import collect_modules
        self.execute()
        run = self.root / 'run'
        duplicate = collect_modules(self.mailbox, [run, run])
        self.assertEqual(duplicate['module_coverage'], 'incomplete')
        self.assertIn('Duplicate', duplicate['rejected_receipts'][0]['reason'])
        path = run / 'validation_result.json'
        receipt = json.loads(path.read_text())
        receipt['parent_rtl_revision'] = 'stale'
        path.write_text(json.dumps(receipt))
        stale = collect_modules(self.mailbox, [run])
        self.assertEqual(stale['pending_modules'], ['unit'])

    def test_batch_dispatch_records_real_fake_agent_acceptance(self):
        from .specification_validation import run_modules
        with patch('shared.top_orchestrator.validation.SSHExecutor', LocalSSH):
            collection = run_modules(self.config, self.root / 'batch')
        self.assertEqual(collection['module_coverage'], 'complete', collection['rejected_receipts'])
        self.assertFalse(collection['backend_ready'])
        with self.assertRaises(FileExistsError):
            run_modules(self.config, self.root / 'batch')

    def test_batch_stops_after_first_failure_without_retry(self):
        from copy import deepcopy
        from .specification_validation import run_modules
        from .specification_verification_plan import verification_plan
        (self.server / 'mode').write_text('incomplete')
        plan = verification_plan(self.mailbox)
        second = deepcopy(plan['modules'][0])
        second['module_name'] = 'second_module_must_not_run'
        plan['modules'].insert(1, second)
        with patch('shared.top_orchestrator.validation.SSHExecutor', LocalSSH), patch(
                'shared.top_orchestrator.specification_validation.verification_plan', return_value=plan):
            collection = run_modules(self.config, self.root / 'batch')
        self.assertEqual(collection['dispatch_status'], 'needs_attention')
        self.assertEqual(len(collection['dispatch']), 1)
        self.assertFalse((self.root / 'batch/module_001').exists())
        self.assertEqual(collection['module_coverage'], 'incomplete')
        self.assertFalse(collection['backend_ready'])

    def test_repair_plan_invalidates_old_receipts_and_requires_system_recheck(self):
        from copy import deepcopy
        from .specification_validation import repair_recheck_plan
        from .specification_verification_plan import verification_plan
        old = verification_plan(self.mailbox)
        new = deepcopy(old)
        new['snapshot_sha256'] = 'new-publication'
        new['rtl_revision'] = 'new-rtl-revision'
        new['modules'][0]['rtl_sha256'] = 'changed-rtl'
        with patch('shared.top_orchestrator.specification_validation.verification_plan',
                   side_effect=[old, new]):
            plan = repair_recheck_plan('old', 'new')
        self.assertEqual(plan['changed_modules'], ['unit'])
        self.assertEqual(plan['required_module_rechecks'], ['unit'])
        self.assertFalse(plan['old_receipts_accepted'])
        self.assertEqual(plan['structural_and_system_recheck'], 'required')
        with self.assertRaises(ValueError):
            repair_recheck_plan(self.mailbox, self.mailbox)
        with self.assertRaises(ValueError):
            verify_backend_handoff(self.root/'run/validation_result.json',self.mailbox)

    def test_old_reader_blocks_agents(self):
        path=self.server/'requirement_verification.py'
        path.write_text(path.read_text().replace('SPECIFICATION_CATALOG_VERSION = 1','SPECIFICATION_CATALOG_VERSION = 0'))
        result, receipt=self.execute()
        self.assertEqual(result.status,'needs_attention')
        self.assertIn('CATALOG_VERSION',result.message)
        self.assertFalse((self.server/'executed').exists())
        self.assertIsNone(receipt['remote_result']['pipeline_returncode'])

    def test_network_attempt_during_help_is_blocked(self):
        path=self.server/'pipeline.py'
        path.write_text("import socket,sys\nif '--help' in sys.argv: socket.create_connection(('example.invalid',443))\n"+path.read_text())
        result, _=self.execute(preflight=True)
        self.assertEqual(result.status,'needs_attention')
        self.assertIn('Network disabled',result.message)
        self.assertFalse((self.server/'executed').exists())

    def test_summary_pass_cannot_cover_missing_source_evidence(self):
        (self.server/'mode').write_text('missing_sources')
        result,_=self.execute()
        self.assertEqual(result.status,'needs_attention')
        self.assertIn('lack complete passed specification',result.message)

    def test_unexecuted_obligations_cannot_pass(self):
        (self.server/'mode').write_text('incomplete')
        result,_=self.execute()
        self.assertEqual(result.status,'needs_attention')
        self.assertIn('lack complete passed specification',result.message)

    def test_changed_consumer_invalidates_receipt(self):
        (self.server/'mode').write_text('mutate_consumer')
        result,_=self.execute()
        self.assertEqual(result.status,'needs_attention')
        self.assertIn('changed during invocation',result.message)

    def test_changed_input_invalidates_receipt(self):
        (self.server/'mode').write_text('mutate_input')
        result,_=self.execute()
        self.assertEqual(result.status,'needs_attention')
        self.assertIn('hashes do not match',result.message)

    def test_replayed_module_identity_is_rejected(self):
        class ReplaySSH(LocalSSH):
            def fetch_file(self,remote,local):
                result=super().fetch_file(remote,local)
                if Path(remote).name=='result.json' and result['ok']:
                    payload=json.loads(Path(local).read_text());payload['module_name']='other'
                    Path(local).write_text(json.dumps(payload))
                return result
        result,_=self.execute(transport=ReplaySSH)
        self.assertEqual(result.status,'needs_attention')
        self.assertIn('receipt differs',result.message)

    def test_plan_does_not_connect_or_write(self):
        from .__main__ import main
        argv=['orchestrator','validation','--mailbox',str(self.mailbox),'--module-probe','unit',
              '--preflight-only','--plan','--run-dir',str(self.root/'plan')]
        with patch('sys.argv',argv), patch('shared.top_orchestrator.__main__.run',side_effect=AssertionError('execution')):
            self.assertEqual(main(),0)
        self.assertFalse((self.root/'plan').exists())

    def test_static_or_empty_evidence_cannot_satisfy_source_coverage(self):
        import yaml
        spec=yaml.safe_load((self.mailbox/'specs/unit.yaml').read_text())
        expected=expected_source_catalog(spec)
        rows=[dict(source_id=key,status='PASS',method='simulation',evidence='executed fixture',**value)
              for key,value in expected.items()]
        reports=self.root/'reports';reports.mkdir()
        path=reports/'requirements.json'
        path.write_text(json.dumps({'requirements':rows}))
        self.assertEqual(verify_source_coverage(reports,expected)['status'],'passed')
        rows[0]['method']='static'
        path.write_text(json.dumps({'requirements':rows}))
        with self.assertRaises(ValueError): verify_source_coverage(reports,expected)
        rows[0].update(method='simulation',evidence='')
        path.write_text(json.dumps({'requirements':rows}))
        with self.assertRaises(ValueError): verify_source_coverage(reports,expected)
