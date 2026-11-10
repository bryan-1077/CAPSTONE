import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from .run_reader import load_run
from .physical_details import backend_report, physical_steps, measured_results
from .window import RunWindow


class LayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_large_backend_report_displays_steps_and_measured_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'state.json').write_text('{}')
            backend = root / '002_backend'
            backend.mkdir()
            report = backend / 'backend_result.json'
            payload = {'history': ['x' * 700000], 'stage_status': {'gdsii': 'success'},
                       'timing_closure_best_attempt': {'wns_ns': -0.02, 'outdir': 'selected-build'}}
            report.write_text(json.dumps(payload))
            run = load_run(root)
            _, result = backend_report(run)
            self.assertNotIn('history', result)
            self.assertEqual(physical_steps(run)[-1][2], 'Passed')
            self.assertIn(('Setup WNS', '-0.02 ns'), measured_results(result))
            self.assertIn(('Measured Build', 'selected-build'), measured_results(result))
            payload['stage_status']['gdsii'] = 'failed'
            report.write_text(json.dumps(payload))
            self.assertEqual(physical_steps(run)[-1][2], 'Failed')
            with report.open('wb') as handle:
                handle.truncate(17 * 1024 * 1024)
            self.assertIn('16 MiB', backend_report(run)[1]['_read_error'])

    def test_layout_and_files_preview_and_copy_selected_run(self):
        with tempfile.TemporaryDirectory() as tmp, patch(
                'shared.gui.registry.discover_worker', return_value=None):
            root = Path(tmp) / 'gui_test'
            folder = root / '002_backend/backend_reports/layout_images'
            folder.mkdir(parents=True)
            (root / 'state.json').write_text(json.dumps({'status': 'complete', 'results': []}))
            (root / '002_backend/backend_result.json').write_text(json.dumps({
                'stage_status': {'gdsii': 'success'}}))
            image = QImage(200, 100, QImage.Format.Format_RGB32)
            image.fill(0x123456)
            path = folder / 'routing.png'
            self.assertTrue(image.save(str(path)))
            window = RunWindow(Path(tmp), preferences_path=Path(tmp) / 'preferences.json')
            try:
                window.display_run(load_run(root))
                window.copy_run_button.click()
                self.assertEqual(self.app.clipboard().text(), root.name)
                window.review.open_stage('backend')
                panel = window.review.details.layout_images
                self.assertEqual(panel.selected_title, 'Routing')
                self.assertIs(window.review.details.pages.currentWidget(), panel)
                window.review.details.pages.setCurrentWidget(window.review.details.physical)
                window.review.open_stage('backend')
                self.assertIs(window.review.details.pages.currentWidget(), window.review.details.physical)
                self.assertEqual(len(panel.comparisons[0].scene.items()), 1)
                window.show_evidence(path)
                self.assertIs(window.preview_stack.currentWidget(), window.image_preview)
                self.assertEqual(len(window.image_preview.scene.items()), 1)
                window.image_preview.zoom(1.25)
                self.assertFalse(window.image_preview.fitting)
                window.image_preview.actual_size()
                self.assertEqual(window.image_preview.view.transform().m11(), 1)
                window.show_evidence(root / 'state.json')
                self.assertIs(window.preview_stack.currentWidget(), window.text)
                self.assertEqual(len(window.image_preview.scene.items()), 0)
                path.write_bytes(b'not a png')
                window.show_evidence(path)
                self.assertEqual(len(window.image_preview.scene.items()), 0)
                self.assertIn('unreadable', window.image_preview.message.text())
                window.open_design()
                self.assertTrue(window.copy_run_button.isHidden())
            finally:
                window.close()

    def test_saved_run_download_is_background_and_opens_images_without_saving_password(self):
        with tempfile.TemporaryDirectory() as tmp, patch(
                'shared.gui.registry.discover_worker', return_value=None):
            root = Path(tmp) / 'gui_download'
            backend = root / '002_backend'
            backend.mkdir(parents=True)
            (root / 'state.json').write_text('{"status":"complete"}')
            (backend / 'backend_result.json').write_text(json.dumps({
                'stage_status': {'gdsii': 'success'}, 'outputs': {'gdsii_remote_dir': '/selected/build'},
                'ssh_host': 'offline', 'ssh_user': 'test'}))
            fixture = Path(tmp) / 'fixture.png'
            image = QImage(200, 100, QImage.Format.Format_RGB32)
            image.fill(0x123456)
            image.save(str(fixture))
            png = fixture.read_bytes()
            window = RunWindow(Path(tmp), preferences_path=Path(tmp) / 'ui.json')
            try:
                window.display_run(load_run(root))
                window.review.open_stage('backend')
                panel = window.review.details.layout_images
                self.assertIs(window.review.details.pages.currentWidget(), window.review.details.physical)
                def collect(state, config, folder, *, stages):
                    self.assertEqual(stages, ('routing',))
                    self.assertEqual(config['password'], 'offline-test-password')
                    folder.mkdir(parents=True)
                    (folder / 'routing.png').write_bytes(png)
                    receipt = {'status': 'partial', 'downloaded_stages': ['routing'], 'images': [
                        {'stage': 'routing', 'status': 'available', 'file': 'routing.png'}]}
                    (folder / 'collection.json').write_text(json.dumps(receipt))
                    return receipt
                with patch('shared.gui.layout_images.QInputDialog.getText',
                           return_value=('offline-test-password', True)), patch(
                        'shared.remote.layout_images.collect_layout_images', side_effect=collect):
                    panel.cards['Routing'].download.click()
                    job = panel._downloads[root]
                    deadline = time.monotonic() + 3
                    while panel._downloads and time.monotonic() < deadline:
                        QTest.qWait(10)
                    self.assertFalse(panel._downloads)
                    self.assertEqual(job.config, {})
                self.assertIs(window.review.details.pages.currentWidget(), panel)
                self.assertEqual(panel.selected_title, 'Routing')
                self.assertTrue(panel.cards['Routing'].download.isEnabled())
                self.assertEqual(panel.cards['Routing'].status_text.text(), 'Routing downloaded.')
                self.assertEqual(panel.cards['Floorplan'].status_text.text(), '')
                self.assertNotIn('offline-test-password', (backend / 'backend_reports/layout_images/collection.json').read_text())
            finally:
                window.close()
