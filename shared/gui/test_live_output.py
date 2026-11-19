"""Reader control over live output during append and snapshot refreshes."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import Qt, QPoint, QPointF
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QAbstractSlider
from .run_reader import LOG_PREVIEW_LIMIT, SavedRun
from .run_review import RunReview
from .setup import SetupPanel


class LiveOutputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        with patch('shared.gui.registry.discover_worker', return_value=None), patch(
                'shared.gui.registry.read_record', return_value={}):
            self.panel = SetupPanel()
        self.panel.resize(1050, 900)
        self.panel.details_toggle.setChecked(True)
        self.panel.show()
        self.app.processEvents()
        self.output = self.panel.output
        self.text = 'Status: generating\n' + ''.join(f'Module output line {i}\n' for i in range(250))
        self.output.update_snapshot(self.text)
        self.app.processEvents()

    def tearDown(self):
        self.panel.close()
        self.panel.deleteLater()

    def test_scroll_pauses_refreshes_and_button_resumes_following(self):
        bar = self.output.verticalScrollBar()
        self.assertEqual(bar.value(), bar.maximum())
        self.assertTrue(self.panel.latest_output.isHidden())
        bar.triggerAction(QAbstractSlider.SliderAction.SliderPageStepSub)
        self.app.processEvents()
        self.assertFalse(self.output.following)
        self.assertFalse(self.panel.latest_output.isHidden())
        position = bar.value()
        self.output.update_snapshot(self.text + 'A newly generated module\n')
        self.app.processEvents()
        self.assertEqual(bar.value(), position)
        # The real poll replaces the summary as well as adding output.
        updated = self.text.replace('generating', 'finished!!') + 'More output\n'
        self.output.update_snapshot(updated)
        self.app.processEvents()
        self.assertEqual(bar.value(), position)
        self.assertFalse(self.output.following)
        self.panel.latest_output.click()
        self.assertTrue(self.output.following)
        self.assertEqual(bar.value(), bar.maximum())
        self.assertTrue(self.panel.latest_output.isHidden())
        self.output.update_snapshot(updated + 'Newest line\n')
        self.app.processEvents()
        self.assertEqual(bar.value(), bar.maximum())

    def test_keyboard_scroll_pauses_and_new_design_resets_following(self):
        self.output.setFocus()
        QTest.keyClick(self.output, Qt.Key.Key_Home, Qt.KeyboardModifier.ControlModifier)
        self.app.processEvents()
        self.assertFalse(self.output.following)
        self.output.update_snapshot(self.text + 'Newest line\n')
        self.assertEqual(self.output.verticalScrollBar().value(), 0)
        self.assertTrue(self.panel.reset_design())
        self.assertTrue(self.output.following)
        self.assertEqual(self.output.toPlainText(), '')
        self.assertTrue(self.panel.latest_output.isHidden())

    def test_opening_details_and_resizing_keeps_follow_mode(self):
        self.panel.details_toggle.setChecked(False)
        self.output.update_snapshot(self.text + 'Output while hidden\n')
        self.panel.details_toggle.setChecked(True)
        self.panel.resize(900, 700)
        self.app.processEvents()
        self.assertTrue(self.output.following)
        bar = self.output.verticalScrollBar()
        self.assertEqual(bar.value(), bar.maximum())

    def test_mouse_wheel_pauses_following(self):
        point = self.output.viewport().rect().center()
        event = QWheelEvent(QPointF(point), QPointF(self.output.viewport().mapToGlobal(point)),
                            QPoint(), QPoint(0, 120), Qt.MouseButton.NoButton,
                            Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.ScrollUpdate, False)
        QApplication.sendEvent(self.output.viewport(), event)
        self.app.processEvents()
        self.assertFalse(self.output.following)
        position = self.output.verticalScrollBar().value()
        self.output.update_snapshot(self.text + 'New output while reading\n')
        self.assertEqual(self.output.verticalScrollBar().value(), position)

    def test_section_older_than_preview_and_back_to_live_output(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'worker.log'
            rtl = '                          SWITCHING TO RTL GENERATION                           '
            verif = '                           SWITCHING TO VERIFICATION                            '
            content = f'{rtl}\n{self.text}\n{verif}\n' + self.text * 100
            self.assertGreater(len(content), LOG_PREVIEW_LIMIT)
            path.write_text(content, encoding='utf-8')
            self.output.update_log(path)
            self.assertNotIn(verif, self.output.toPlainText())
            button = self.output.section_button
            self.assertTrue(button.isVisible())
            menu = button.menu()
            menu.aboutToShow.emit()
            actions = menu.actions()
            self.assertEqual([action.text() for action in actions], ['RTL Generation', 'Verification'])
            actions[1].trigger()
            self.assertEqual(self.output.textCursor().block().text(), verif)
            self.assertFalse(self.output.following)
            self.assertFalse(self.panel.latest_output.isHidden())
            path.write_text(content + 'New output\n', encoding='utf-8')
            self.output.update_log(path)
            self.assertEqual(self.output.textCursor().block().text(), verif)
            self.panel.latest_output.click()
            self.assertTrue(self.output.following)
            self.assertTrue(self.output.toPlainText().endswith('New output\n'))
            bar = self.output.verticalScrollBar()
            self.assertEqual(bar.value(), bar.maximum())

    def test_saved_run_jumps_to_each_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            heading = '                           SWITCHING TO VERIFICATION                            '
            content = f'{heading}\nFirst attempt\n{self.text}{heading}\nSecond attempt\n{self.text}'
            (directory / 'worker.log').write_text(content, encoding='utf-8')
            review = RunReview()
            review.display(SavedRun(directory, {}))
            review.log_toggle.setChecked(True)
            review.resize(1050, 900)
            review.show()
            self.addCleanup(review.deleteLater)
            self.addCleanup(review.close)
            self.app.processEvents()
            button = review.log.section_button
            self.assertTrue(button.isVisible())
            menu = button.menu()
            menu.aboutToShow.emit()
            actions = menu.actions()
            self.assertEqual([action.text() for action in actions], ['Verification (1)', 'Verification (2)'])
            for action, label in zip(reversed(actions), ('Second attempt', 'First attempt')):
                action.trigger()
                block = review.log.textCursor().block()
                self.assertEqual(block.text(), heading)
                self.assertEqual(block.next().text(), label)
