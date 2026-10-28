import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from probes import run_innovus_GDSII_universal as runner


class PDKRootTests(unittest.TestCase):
    def test_shared_root_precedes_account_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shared = root / 'shared/sky130A'
            (shared / 'libs.ref').mkdir(parents=True)
            (root / '.volare/sky130A/libs.ref').mkdir(parents=True)
            with patch.object(runner, 'SHARED_SKY130_ROOT', str(shared)), patch.object(Path, 'home', return_value=root):
                self.assertEqual(runner.choose_sky130_root(), shared.resolve())

    def test_explicit_root_still_takes_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shared = root / 'shared/sky130A'
            (shared / 'libs.ref').mkdir(parents=True)
            explicit = root / 'explicit/sky130A'
            explicit.mkdir(parents=True)
            with patch.object(runner, 'SHARED_SKY130_ROOT', str(shared)):
                self.assertEqual(runner.choose_sky130_root(str(explicit)), explicit.resolve())

    def test_missing_shared_root_keeps_account_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            account = root / '.volare/sky130A'
            (account / 'libs.ref').mkdir(parents=True)
            with patch.object(runner, 'SHARED_SKY130_ROOT', str(root / 'missing')), patch.object(Path, 'home', return_value=root):
                self.assertEqual(runner.choose_sky130_root(), account.resolve())


if __name__ == '__main__':
    unittest.main()
