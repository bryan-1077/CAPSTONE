import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import debug_agent as agent


class BareHunkTests(unittest.TestCase):
    def normalize(self, root, text):
        with patch.object(agent, "SCRIPT_DIR", root), patch.object(agent, "RTL_OUTPUT_DIR", root / "rtl_output"):
            return agent.normalize_patch_text(text)

    def test_multiple_hunks_with_line_offset_pass_git_check(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "rtl_output").mkdir()
            source = root / "rtl_output/test.sv"
            original = "a\nb\nc\nd\ne\nf\ng\nh\ni\n"
            source.write_text(original)
            text = "--- a/rtl_output/test.sv\n+++ b/rtl_output/test.sv\n@@\n a\n-b\n+B\n+extra\n c\n@@\n g\n-h\n+H\n i\n"
            normalized, warnings = self.normalize(root, text)
            self.assertIn("@@ -1,3 +1,4 @@", normalized)
            self.assertIn("@@ -7,3 +8,3 @@", normalized)
            checked = subprocess.run(["git", "apply", "--check", "-"], input=normalized,
                                     text=True, capture_output=True, cwd=root)
            self.assertEqual(checked.returncode, 0, checked.stderr)
            self.assertEqual(source.read_text(), original)
            self.assertTrue(any("unique exact" in warning for warning in warnings))
            self.assertEqual(self.normalize(root, normalized)[0], normalized)

    def test_unsafe_or_unmatched_context_is_not_reconstructed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "rtl_output").mkdir()
            (root / "rtl_output/test.sv").write_text("repeat\nrepeat\n")
            for name, body in (("rtl_output/test.sv", "-repeat\n+new\n"),
                               ("rtl_output/test.sv", "-missing\n+new\n"),
                               ("rtl_output/test.sv", "+insertion\n"),
                               ("../outside.sv", "-old\n+new\n")):
                with self.subTest(name=name, body=body):
                    text = f"--- a/{name}\n+++ b/{name}\n@@\n{body}"
                    normalized, warnings = self.normalize(root, text)
                    self.assertIn("\n@@\n", normalized)
                    self.assertTrue(any("Cannot reconstruct" in warning for warning in warnings))


if __name__ == "__main__":
    unittest.main()
