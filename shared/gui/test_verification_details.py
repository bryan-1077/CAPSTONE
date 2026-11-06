import json
import os
from pathlib import Path
import tempfile
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication
from .run_reader import SavedRun
from .verification_details import verification_agents
from .stage_details import StageDetails


class VerificationDetailsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_live_log_reports_active_and_completed_agents_without_guessing_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = root / 'validation.log'
            log.write_text('[REPORTS] /remote/agent1/report.json\n'
                           'AGENT 1 SYSTEM RESULT : PASS\n'
                           '[REPORTS] /remote/agent2/report.json\n')
            run = SavedRun(root, dict(current_stage='validation', status='running'))
            agents = verification_agents(run, alive=True)
            self.assertEqual([a[2] for a in agents], ['PASS', 'RUNNING', 'NOT_REPORTED'])
            self.assertEqual(verification_agents(run)[1][2], 'UNCONFIRMED')
            with log.open('a') as handle:
                handle.write('[REPORTS] /remote/agent3/report.json\n')
            self.assertEqual([a[2] for a in verification_agents(run, alive=True)],
                             ['PASS', 'UNCONFIRMED', 'RUNNING'])

    def test_retrieved_report_preserves_gaps_and_user_expansion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / 'reports/rtl/verification_reports/run/final_system_report.json'
            report.parent.mkdir(parents=True)
            payload = dict(mode='system', top_module='controller', agents={
                'agent1': 'PASS', 'agent2': 'PASS', 'agent3': 'PASS_WITH_GAPS'},
                vectors={'passed': 29, 'failed': 0}, coverage_limitations=[
                    {'timing_rule': 'tRAS', 'reason': 'Boundary not observed'}])
            report.write_text(json.dumps(payload))
            run = SavedRun(root, dict(status='complete', current_stage=None))
            view = StageDetails()
            try:
                view.display(run, 'validation')
                self.assertIs(view.pages.currentWidget(), view.agents)
                self.assertEqual(view.agents.topLevelItemCount(), 3)
                agent3 = view.agents.topLevelItem(2)
                self.assertEqual(agent3.text(1), 'Passed with gaps')
                self.assertTrue(any('Boundary not observed' in agent3.child(i).text(1)
                                    for i in range(agent3.childCount())))
                agent3.setExpanded(True)
                payload['vectors']['passed'] = 30
                report.write_text(json.dumps(payload))
                view.display(run, 'validation')
                self.assertTrue(view.agents.topLevelItem(2).isExpanded())
                view.pages.setCurrentWidget(view.evidence)
                view.display(run, 'validation')
                self.assertIs(view.pages.currentWidget(), view.evidence)
                view.display(run, 'backend')
                self.assertFalse(view.pages.isTabVisible(2))
            finally:
                view.close()

    def test_retry_does_not_reuse_old_reports_and_missing_data_stays_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root / '001_validation/reports/rtl/verification_reports/run/final_system_report.json'
            old.parent.mkdir(parents=True)
            old.write_text('{"agents":{"agent1":"PASS"}}')
            (root / '003_validation').mkdir()
            run = SavedRun(root, dict(status='running', current_stage='validation', results=[
                dict(stage='validation', status='passed', artifacts={'log': str(root / '001_validation/validation.log')})]))
            self.assertEqual([a[2] for a in verification_agents(run, alive=True)], ['NOT_REPORTED'] * 3)
            report = root / '003_validation/reports/rtl/verification_reports/run/final_system_report.json'
            report.parent.mkdir(parents=True)
            report.write_text('{')
            self.assertEqual(verification_agents(run)[0][2], 'NOT_REPORTED')
