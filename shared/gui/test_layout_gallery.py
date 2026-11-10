import os
from pathlib import Path
import tempfile
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel
from .layout_gallery import LayoutGallery


class GalleryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.images = []
        for index, name in enumerate(('Floorplan', 'Placement', 'Routing')):
            path = Path(self.tmp.name) / (name.lower() + '.png')
            image = QImage(240, 180, QImage.Format.Format_RGB32)
            image.fill(0x112233 + index * 10000)
            image.save(str(path))
            self.images.append((name, path, {'source_build': '/build/' + name.lower()}))
        self.gallery = LayoutGallery()
        self.gallery.resize(850, 600)
        self.gallery.set_images(self.images, reset=True)
        self.gallery.show()
        self.app.processEvents()

    def tearDown(self):
        if self.gallery.viewer:
            self.gallery.viewer.close()
        self.gallery.close()
        self.gallery.deleteLater()
        self.app.processEvents()

    def test_all_images_remain_visible_and_details_are_collapsed(self):
        gallery = self.gallery
        self.assertEqual(gallery.selected_title, 'Routing')
        self.assertEqual(len(gallery.comparisons), 3)
        self.assertFalse(hasattr(gallery, 'compare'))
        self.assertFalse(hasattr(gallery, 'thumbnail_strip'))
        self.assertFalse(hasattr(gallery, 'controls'))
        card = gallery.cards['Floorplan']
        self.assertIsNone(card.info_window)
        card.info.click()
        details = '\n'.join(label.text() for label in card.info_window.findChildren(QLabel))
        self.assertIn('/build/floorplan', details)
        self.assertNotIn('/build/routing', details)
        self.assertTrue(card.info.isChecked())
        card.info_window.close()
        self.assertFalse(card.info.isChecked())

    def test_compare_and_expanded_view_navigation(self):
        gallery = self.gallery
        self.assertEqual(len(gallery.comparisons), 3)
        gallery.comparisons[1].view.clicked.emit()
        self.app.processEvents()
        viewer = gallery.viewer
        self.assertEqual(viewer.index, 1)
        viewer.next.click()
        self.assertEqual(viewer.index, 2)
        self.assertFalse(viewer.next.isEnabled())
        viewer.previous.click()
        self.assertEqual(viewer.index, 1)
        viewer.preview.zoom(1.25)
        self.assertFalse(viewer.preview.fitting)
        QTest.keyClick(viewer, Qt.Key.Key_Escape)
        self.app.processEvents()
        self.assertIsNone(gallery.viewer)
        gallery.cards['Placement'].open_button.click()
        self.app.processEvents()
        self.assertIsNotNone(gallery.viewer)
        self.assertEqual(gallery.viewer.index, 1)
        self.assertEqual(len(gallery.comparisons), 3)

    def test_partial_empty_and_replaced_images_clear_old_comparisons(self):
        gallery = self.gallery
        gallery.set_images(self.images[:2])
        self.assertEqual(len(gallery.comparisons), 2)
        gallery.set_images([])
        self.assertEqual(gallery.selected_title, '')
        self.assertEqual(len(gallery.cards), 3)
        self.assertTrue(all(not card.open_button.isEnabled() for card in gallery.cards.values()))
        gallery.set_images(self.images[:1], reset=True)
        self.assertEqual(gallery.selected_title, 'Floorplan')
        self.assertEqual(len(gallery.comparisons), 1)
        self.assertFalse(gallery.comparison.isHidden())

    def test_open_window_arrow_opens_the_matching_stage(self):
        gallery = self.gallery
        arrow = gallery.cards['Floorplan'].open_button
        self.assertEqual(arrow.accessibleName(), 'Open Floorplan')
        arrow.click()
        self.app.processEvents()
        self.assertEqual(gallery.viewer.index, 0)
        self.assertEqual(len(gallery.comparisons), 3)

    def test_unreadable_image_message_stays_inside_gallery_and_clears(self):
        preview = self.gallery.comparisons[0]
        bad = Path(self.tmp.name) / 'broken.png'
        bad.write_bytes(b'broken')
        self.assertFalse(preview.show_image(bad))
        self.assertIs(preview.message.parentWidget(), preview)
        self.assertFalse(preview.message.isHidden())
        self.assertTrue(preview.show_image(self.images[0][1]))
        self.assertTrue(preview.message.isHidden())
