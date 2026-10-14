import json
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from services.prep_top import tcl_selects_top
from workers.workers import _build_prepare_review_command
from agents.openai_prep_edit import _extract_detected_macros


TOP = "ddr4_controller_top"
GENERATED_TCL = '''
set TOP ""
if {[file exists "./top_module.txt"]} {
    set fh [open "./top_module.txt" r]
    set TOP [string trim [read $fh]]
    close $fh
}
if {[info exists ::env(TOP)] && [string trim $::env(TOP)] ne ""} {
    set TOP [string trim $::env(TOP)]
}
set FILELIST [file normalize "./filelist_genus.f"]
read_hdl -sv -f $FILELIST
elaborate $TOP
'''

FILE_BACKED_GETS_TCL = '''
set top_file [file normalize "./top_module.txt"]
if {[info exists ::env(TOP)] && [string trim $::env(TOP)] ne ""} {
    set TOP [string trim $::env(TOP)]
} elseif {[file exists $top_file]} {
    set fh [open $top_file r]
    set TOP [string trim [gets $fh]]
    close $fh
} else {
    puts "ERROR: top_module.txt not found and env(TOP) not set"
    exit 2
}
set FILELIST [file normalize "./filelist_genus.f"]
read_hdl -sv -f $FILELIST
elaborate $TOP
'''

FALLBACK_TCL = ('set DEFAULT_TOP "' + TOP + '"\n' + FILE_BACKED_GETS_TCL).replace(
    'elaborate $TOP', 'if {$TOP eq ""} {\n    set TOP $DEFAULT_TOP\n}\nelaborate $TOP')


class PrepTopTests(unittest.TestCase):
    def test_prep_editor_ignores_none_macro_marker(self):
        for body, expected in (('  - none\n', []), ('  - none\n  - FEATURE\n', ['FEATURE'])):
            with self.subTest(body=body):
                payload = {'context': {'key_files': {'README_prepared.txt':
                    'Preprocessor macros detected in source:\n' + body}}}
                self.assertEqual(_extract_detected_macros(payload), expected)

    def test_generated_file_selection(self):
        for command in ('read', 'gets'):
            with self.subTest(command=command):
                self.assertTrue(tcl_selects_top(GENERATED_TCL.replace('[read $fh]', f'[{command} $fh]'), TOP))

    def test_triage_file_selection(self):
        self.assertTrue(tcl_selects_top(FILE_BACKED_GETS_TCL, TOP))

    def test_verified_literal_alias_and_fallback(self):
        for script in (FALLBACK_TCL, f'set DEFAULT_TOP "{TOP}"\nset TOP $DEFAULT_TOP\nelaborate $TOP',
                       f'set DEFAULT_TOP "{TOP}"\nset TOP ${{DEFAULT_TOP}}\nelaborate $TOP'):
            with self.subTest(script=script):
                self.assertTrue(tcl_selects_top(script, TOP))

    def test_wrong_unknown_or_reassigned_alias_rejected(self):
        for script in (FALLBACK_TCL.replace(f'set DEFAULT_TOP "{TOP}"', 'set DEFAULT_TOP wrong_top'),
                       'set TOP $UNKNOWN\nelaborate $TOP',
                       f'set DEFAULT_TOP "{TOP}"\nset DEFAULT_TOP wrong_top\nset TOP $DEFAULT_TOP\nelaborate $TOP',
                       f'# set DEFAULT_TOP "{TOP}"\nset TOP $DEFAULT_TOP\nelaborate $TOP'):
            with self.subTest(script=script):
                self.assertFalse(tcl_selects_top(script, TOP))

    def test_invalid_gets_selection(self):
        for script in (
            FILE_BACKED_GETS_TCL.replace('[gets $fh]', '[gets $other]'),
            FILE_BACKED_GETS_TCL.replace('./top_module.txt', './wrong_top.txt'),
            FILE_BACKED_GETS_TCL.replace('elaborate $TOP', 'elaborate wrong_top'),
            FILE_BACKED_GETS_TCL.replace('elaborate $TOP', 'set TOP wrong_top\nelaborate $TOP'),
            FILE_BACKED_GETS_TCL.replace('elaborate $TOP', '# elaborate $TOP'),
        ):
            with self.subTest(script=script):
                self.assertFalse(tcl_selects_top(script, TOP))

    def test_normalized_file_variable(self):
        script = GENERATED_TCL.replace('set TOP ""', 'set TOP_FILE [file normalize "./top_module.txt"]\nset TOP ""')
        script = script.replace('[open "./top_module.txt" r]', '[open $TOP_FILE r]')
        self.assertTrue(tcl_selects_top(script, TOP))

    def test_literal_selection(self):
        for script in (f'elaborate {TOP}', f'set TOP "{TOP}"\nelaborate $TOP'):
            with self.subTest(script=script):
                self.assertTrue(tcl_selects_top(script, TOP))

    def test_invalid_selection(self):
        for script in (
            f'# {TOP}\nelaborate wrong_top',
            f'puts "{TOP}"\nelaborate wrong_top',
            GENERATED_TCL.replace('elaborate $TOP', 'elaborate wrong_top'),
            GENERATED_TCL.replace('elaborate $TOP', 'set TOP wrong_top\nelaborate $TOP'),
            GENERATED_TCL.replace('[read $fh]', '[read $other]'),
            GENERATED_TCL.replace('./top_module.txt', './wrong_top.txt'),
            GENERATED_TCL.replace('elaborate $TOP', '# elaborate $TOP'),
            'set TOP [string trim $::env(TOP)]\nelaborate $TOP',
        ):
            with self.subTest(script=script):
                self.assertFalse(tcl_selects_top(script, TOP))

    def test_remote_review_command(self):
        # Execute the actual generated remote Python locally, without SSH,
        # Genus, or model calls; all temporary files remain in local/backend.
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp:
            root = Path(tmp).resolve()
            (root / 'rtl').mkdir()
            (root / 'constraints').mkdir()
            (root / 'rtl/top.sv').write_text(f'module {TOP}(input clk, output y); assign y = clk; endmodule\n')
            (root / 'filelist_genus.f').write_text('./rtl/top.sv\n')
            (root / 'top_module.txt').write_text(TOP + '\n')
            (root / 'README_prepared.txt').write_text('Top module: ' + TOP)
            (root / 'constraints/mc_genus.sdc').write_text('create_clock -name clk -period 4 [get_ports clk]\n')
            rules = Path(__file__).resolve().parents[2] / 'templates/universal genus prep instruction.md'
            state = {'universal_rules_path': str(rules), 'top_module': TOP}
            for script, expected in ((GENERATED_TCL, True),
                                     (FILE_BACKED_GETS_TCL, True),
                                     (FALLBACK_TCL, True),
                                     (FILE_BACKED_GETS_TCL.replace('./top_module.txt', './wrong_top.txt'), False),
                                     (FILE_BACKED_GETS_TCL.replace('elaborate $TOP', 'elaborate wrong_top'), False),
                                     (GENERATED_TCL.replace('elaborate $TOP', 'elaborate wrong_top'), False)):
                with self.subTest(expected=expected):
                    (root / 'run_genus_mc.tcl').write_text(script)
                    command = shlex.split(_build_prepare_review_command(state, str(root)))
                    result = subprocess.run([sys.executable, '-B', '-c', command[-1]], capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    payload = json.loads(result.stdout)
                    self.assertEqual(payload['pass'], expected, payload['errors'])
            (root / 'run_genus_mc.tcl').write_text(GENERATED_TCL)
            for macros, expected in (('none', True), ('None', True), ('FEATURE', False)):
                with self.subTest(macros=macros):
                    (root / 'README_prepared.txt').write_text('Top module: ' + TOP +
                        '\n\nPreprocessor macros detected in source:\n  - ' + macros + '\n')
                    command = shlex.split(_build_prepare_review_command(state, str(root)))
                    result = subprocess.run([sys.executable, '-B', '-c', command[-1]], capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(json.loads(result.stdout)['pass'], expected)
            (root / 'README_prepared.txt').write_text('Top module: ' + TOP)
            for value in ('', None):
                with self.subTest(top_metadata=value):
                    top = root / 'top_module.txt'
                    if value is None:
                        top.unlink()
                    else:
                        top.write_text(value)
                    command = shlex.split(_build_prepare_review_command(state, str(root)))
                    result = subprocess.run([sys.executable, '-B', '-c', command[-1]], capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertFalse(json.loads(result.stdout)['pass'])


if __name__ == '__main__':
    unittest.main()
