import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from .specification import select_specification


class EditorTests(unittest.TestCase):
    def test_typed_selections_and_preserved_fields(self):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PySide6.QtWidgets import QApplication
        from .spec_editor import SpecificationEditor
        app = QApplication.instance() or QApplication([])
        editor = SpecificationEditor()
        candidate = dict(design_name='controller', profile={'type': 'basic'},
                         memory={'protocol': 'DDR4', 'speed': '3200'},
                         topology={'banks': 4}, features={'refresh': True, 'scheduler': 'simple'},
                         extra={'list': [1, 2]})
        editor.load(candidate)
        self.assertEqual(editor.configuration(), candidate)
        self.assertNotIn(('design_name',), editor.fields)
        self.assertNotIn(('profile', 'type'), editor.fields)
        editor.fields[('topology', 'banks')][0].setCurrentIndex(0)
        editor.fields[('features', 'refresh')][0].setChecked(False)
        result = editor.configuration()
        self.assertEqual(result['topology']['banks'], 1)
        self.assertIs(result['features']['refresh'], False)
        self.assertEqual(result['extra'], candidate['extra'])
        self.assertEqual(result['design_name'], candidate['design_name'])
        self.assertEqual(result['profile'], candidate['profile'])
        self.assertIsInstance(result['memory']['speed'], str)
        editor.close()

    def test_edited_approval_validated_and_saved(self):
        validated = []
        api = SimpleNamespace(MAX_CLARIFICATION_WARNINGS=3, MAX_CONFIRMATION_REJECTIONS=2,
            DEFAULT_LLM_MODEL='test', FatalConfigTextError=type('Fatal', (Exception,), {}),
            ConfigTextError=type('Recoverable', (Exception,), {}),
            extract_config_with_llm=lambda text, model: {},
            normalize_result=lambda value: ({'design_name': 'original'}, [], 'none', None),
            validate_user_config=lambda config: validated.append(config))
        client = SimpleNamespace(request=lambda kind, context: dict(action='approve',
            digest=context['digest'], configuration={'design_name': 'edited'}))
        with tempfile.TemporaryDirectory() as tmp:
            path = select_specification('design', Path(tmp), client, api)
            self.assertIn('edited', path.read_text())
            self.assertEqual(validated, [{'design_name': 'edited'}])
