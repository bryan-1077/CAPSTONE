# GDSII_Validation_Agent.py
"""Backend netlist/GDSII structural inspection; not an LVS or DRC engine."""
import argparse
from run_pipeline import AgentReport, utc_now

import os
import re
import json
from collections import Counter

# Optional dependency for GDSII parsing
try:
    import gdspy
    GDSPY_AVAILABLE = True
except ImportError:
    GDSPY_AVAILABLE = False


# ============================================================
# Utility Functions
# ============================================================

def read_file_safely(path):
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "r", errors="ignore") as f:
            return f.read()
    except Exception:
        return None


def file_exists(path):
    return path is not None and os.path.isfile(path)


def find_report_files(report_dir):
    """Automatically find report files in GDSII_Reports folder"""
    reports = {}
    if not os.path.isdir(report_dir):
        return reports

    for fname in sorted(os.listdir(report_dir)):
        path = os.path.join(report_dir, fname)
        if re.search(r'timing', fname, re.IGNORECASE):
            reports['timing'] = path
        elif re.search(r'messages', fname, re.IGNORECASE):
            reports['sim_log'] = path
        elif re.search(r'gates|check_design_post|check_unresolved', fname, re.IGNORECASE):
            reports['compile_log'] = path
        elif re.search(r'coverage', fname, re.IGNORECASE):
            reports['coverage'] = path
    return reports


# ============================================================
# Verilog / Netlist Parsing
# ============================================================

def extract_module_blocks(verilog_text):
    if not verilog_text:
        return []
    pattern = re.compile(
        r'\bmodule\s+([a-zA-Z_$][\w$]*|\\[^\s]+)\s*(.*?)(?=\bendmodule\b)',
        re.MULTILINE | re.DOTALL
    )
    verilog_text = re.sub(r'//[^\n]*|/\*.*?\*/', '', verilog_text, flags=re.DOTALL)
    modules = []
    for match in pattern.finditer(verilog_text):
        module_name = match.group(1)
        block_text = match.group(0)
        modules.append((module_name, block_text))
    return modules


def extract_module_names(verilog_text):
    return [name for name, _ in extract_module_blocks(verilog_text)]


def extract_ports_from_module_block(module_block_text):
    if not module_block_text:
        return []
    clean = re.sub(r'//[^\n]*|/\*.*?\*/', '', module_block_text, flags=re.DOTALL)
    header = re.match(r'\s*module\s+(?:[a-zA-Z_$][\w$]*|\\[^\s]+)\s*', clean)
    if not header:
        return []
    tail = clean[header.end():].lstrip()
    def take_group(text):
        if not text.startswith('('):
            return '', text
        depth = 0
        for i, char in enumerate(text):
            if char == '(':
                depth += 1
            elif char == ')':
                depth -= 1
                if depth == 0:
                    return text[1:i], text[i+1:].lstrip()
        return '', ''
    if tail.startswith('#'):
        _, tail = take_group(tail[1:].lstrip())
    port_blob, _ = take_group(tail)
    raw_ports = [p.strip() for p in port_blob.split(",")]
    ports = []
    for item in raw_ports:
        item = re.sub(r'\b(input|output|inout|wire|reg|logic|signed|unsigned)\b', '', item)
        item = re.sub(r'\[[^\]]+\]', '', item)
        item = item.strip()
        if not item:
            continue
        tokens = item.split()
        port_name = tokens[-1]
        ports.append(port_name)
    return list(dict.fromkeys(ports))


def detect_top_module(module_names, verilog_text):
    if not module_names or not verilog_text:
        return module_names[0] if module_names else None
    instantiated = set()
    for mod_name, block_text in extract_module_blocks(verilog_text):
        for candidate in module_names:
            if candidate == mod_name:
                continue
            inst_pat = r'(?<![\w$])' + re.escape(candidate) + r'\s+(?:#\s*\(.*?\)\s*)?(?:[a-zA-Z_$][\w$]*|\\[^\s]+)\s*\(' 
            if re.search(inst_pat, block_text, re.DOTALL):
                instantiated.add(candidate)
    top_candidates = [m for m in module_names if m not in instantiated]
    return top_candidates[0] if len(top_candidates) == 1 else None


def find_clock_and_reset_ports(port_names):
    clocks = []
    resets = []
    for p in port_names:
        lp = p.lower()
        if "clk" in lp or "clock" in lp:
            clocks.append(p)
        if "rst" in lp or "reset" in lp:
            resets.append(p)
    return list(dict.fromkeys(clocks)), list(dict.fromkeys(resets))


# ============================================================
# Agent 1: Structure / Netlist Consistency
# ============================================================

class StructureNetlistConsistencyAgent:
    def __init__(self, netlist_file, required_modules=None, required_ports=None, top_module=None):
        self.top_module = top_module
        self.netlist_file = netlist_file
        self.required_modules = required_modules or []
        self.required_ports = required_ports or []
        self.report = {}

    def run(self):
        text = read_file_safely(self.netlist_file)
        if not text:
            self.report = {"pass": False, "reason": "Netlist not found"}
            return self.report
        module_names = extract_module_names(text)
        top_module = self.top_module or detect_top_module(module_names, text)
        ports = []
        clocks, resets = [], []
        for mod_name, block_text in extract_module_blocks(text):
            if mod_name == top_module:
                ports = extract_ports_from_module_block(block_text)
                clocks, resets = find_clock_and_reset_ports(ports)
                break
        missing_modules = [m for m in self.required_modules if m not in module_names]
        missing_ports = [p for p in self.required_ports if p not in ports]
        self.report = {
            "top_module": top_module,
            "modules_found": module_names,
            "ports_found": ports,
            "reason": ("Top module missing or ambiguous; supply --top" if top_module not in module_names
                       else "Duplicate module declarations" if len(module_names) != len(set(module_names))
                       else "Required modules or ports missing" if missing_modules or missing_ports else ""),
            "missing_required_modules": missing_modules,
            "missing_required_ports": missing_ports,
            "clocks_found": clocks,
            "resets_found": resets,
            "pass": len(missing_modules) == 0 and len(missing_ports) == 0 and top_module in module_names and len(module_names) == len(set(module_names))
        }
        return self.report


# ============================================================
# Agent 2: GDSII Layout Structure Consistency
# ============================================================

class GDSIILayoutStructureConsistencyAgent:
    def __init__(self, gds_file, expected_modules=None, top_cell=None, gds_libraries=None):
        self.gds_libraries = gds_libraries or []
        self.top_cell = top_cell
        self.gds_file = gds_file
        self.expected_modules = expected_modules or []
        self.report = {}

    def parse_gds(self):
        if not GDSPY_AVAILABLE:
            return {"available": False, "reason": "gdspy not installed"}
        if not file_exists(self.gds_file):
            return {"available": False, "reason": "GDS file not found"}
        try:
            lib = gdspy.GdsLibrary()
            lib.read_gds(self.gds_file)
            for path in self.gds_libraries:
                lib.read_gds(path)
            for cell in lib.cells.values():
                for ref in cell.references:
                    if isinstance(ref.ref_cell, str) and ref.ref_cell in lib.cells:
                        ref.ref_cell = lib.cells[ref.ref_cell]
            all_cells = list(lib.cells.keys())
            top_candidates = [cell.name for cell in lib.top_level()]
            references = {cell_name: [getattr(ref.ref_cell, "name", ref.ref_cell) for ref in getattr(cell, "references", [])] for cell_name, cell in lib.cells.items()}
            return {"available": True, "cells": all_cells, "top_candidates": top_candidates, "references": references}
        except Exception as e:
            return {"available": False, "reason": str(e)}

    def run(self):
        parsed = self.parse_gds()
        if not parsed["available"]:
            self.report = {"pass": False, "reason": parsed.get("reason")}
            return self.report
        gds_cells = parsed["cells"]
        gds_top = self.top_cell or (parsed["top_candidates"][0] if len(parsed["top_candidates"]) == 1 else None)
        unresolved = sorted({ref for refs in parsed["references"].values() for ref in refs if ref not in gds_cells})
        missing_in_gds = [m for m in self.expected_modules if m not in gds_cells]
        extra_in_gds = [c for c in gds_cells if c not in self.expected_modules]
        instance_counts = {}
        if gds_top and gds_top in parsed["references"]:
            instance_counts = dict(Counter(parsed["references"][gds_top]))
        self.report = {
            "gds_available": True,
            "reason": ("Layout top missing or ambiguous; supply --gds-top if renamed" if gds_top not in parsed["top_candidates"]
                       else "Unresolved layout references" if unresolved else "Required cells missing" if missing_in_gds else ""),
            "gds_top": gds_top,
            "top_candidates": parsed["top_candidates"],
            "unresolved_references": unresolved,
            "cells_found": gds_cells,
            "missing_expected_cells": missing_in_gds,
            "extra_cells": extra_in_gds,
            "top_cell_instance_counts": instance_counts,
            "pass": len(missing_in_gds) == 0 and gds_top in parsed["top_candidates"] and not unresolved
        }
        return self.report


# ============================================================
# Agent 3: Protocol / Assertion Compliance
# ============================================================

class ProtocolAssertionComplianceAgent:
    def __init__(self, sim_log_file=None):
        self.sim_log_file = sim_log_file
        self.report = {}

    def run(self):
        text = read_file_safely(self.sim_log_file)
        if not text:
            self.report = {"pass": False, "reason": "Simulation log missing"}
            return self.report
        fail_patterns = ["ASSERTION.*FAIL","Assertion.*failed","protocol violation","JEDEC.*violation"]
        fails = [line.strip() for line in text.splitlines() if any(re.search(p, line, re.IGNORECASE) for p in fail_patterns)]
        passes = [line.strip() for line in text.splitlines() if re.search(r'ASSERTION.*PASS|Assertion.*passed', line, re.IGNORECASE)]
        self.report = {"assertions_passed_count": len(passes), "assertions_failed_count": len(fails), "pass": len(fails)==0 and len(passes)>0}
        return self.report


# ============================================================
# Agent 4: Gate-Level / Synthesized Netlist Validation
# ============================================================

class GateLevelSynthesizedNetlistValidationAgent:
    def __init__(self, netlist_file, compile_log_file=None, sim_log_file=None):
        self.netlist_file = netlist_file
        self.compile_log_file = compile_log_file
        self.sim_log_file = sim_log_file
        self.report = {}

    def run(self):
        netlist_exists = file_exists(self.netlist_file)
        compile_text = read_file_safely(self.compile_log_file)
        sim_text = read_file_safely(self.sim_log_file)
        compile_errors = [l for l in (compile_text or "").splitlines() if "error" in l.lower()]
        compile_warnings = [l for l in (compile_text or "").splitlines() if "warning" in l.lower()]
        sim_errors = [l for l in (sim_text or "").splitlines() if "error" in l.lower()]
        sim_warnings = [l for l in (sim_text or "").splitlines() if "warning" in l.lower()]
        compile_pass = bool(compile_text) and len(compile_errors)==0
        sim_pass = bool(sim_text) and len(sim_errors)==0
        self.report = {"netlist_file_found": netlist_exists, "compile_pass": compile_pass, "simulation_pass": sim_pass,
                       "compile_warnings": len(compile_warnings), "compile_errors": len(compile_errors),
                       "sim_warnings": len(sim_warnings), "sim_errors": len(sim_errors),
                       "pass": netlist_exists and compile_pass and sim_pass}
        return self.report


# ============================================================
# Agent 5: Timing Constraint Sanity
# ============================================================

class TimingConstraintSanityAgent:
    def __init__(self, timing_report_file=None):
        self.timing_report_file = timing_report_file
        self.report = {}

    def run(self):
        text = read_file_safely(self.timing_report_file)
        if not text:
            self.report = {"pass": False, "reason": "Timing report missing"}
            return self.report
        clocks = [l for l in text.splitlines() if "clock" in l.lower()]
        unconstrained_paths = sum(1 for l in text.splitlines() if "unconstrained" in l.lower())
        self.report = {"clock_reference_count": len(clocks), "unconstrained_paths": unconstrained_paths,
                       "pass": len(clocks)>0 and unconstrained_paths==0}
        return self.report


# ============================================================
# Agent 6: Coverage / Test Completeness
# ============================================================

class CoverageTestCompletenessAgent:
    def __init__(self, coverage_report_file=None):
        self.coverage_report_file = coverage_report_file
        self.report = {}

    def run(self):
        text = read_file_safely(self.coverage_report_file)
        self.report = {"functional_coverage": None, "code_coverage": None, "pass": False, "reason": "Coverage report parsing is not implemented"}
        return self.report


# ============================================================
# Validation 2 Orchestrator
# ============================================================

def inspect_backend_report(path, expected_top=None):
    """Conservative text adapters; preserve line evidence and never infer signoff."""
    result = {'source': os.path.abspath(path), 'status': 'NOT_TESTED',
              'findings': [], 'reason': '', 'design_association': 'UNCONFIRMED'}
    try:
        with open(path, 'rb') as handle:
            raw = handle.read(10 * 1024 * 1024 + 1)
        if len(raw) > 10 * 1024 * 1024 or b'\x00' in raw:
            result['reason'] = 'Report preserved; binary or larger than the 10 MiB text-analysis limit.'
            return result
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        result['reason'] = 'Report preserved; unsupported text encoding or binary format.'
        return result
    except OSError as exc:
        result.update(status='ERROR', reason=str(exc))
        return result
    modules = re.findall(r'^\s*Module:\s*(\S+)', text, re.M)
    if expected_top and modules:
        if any(name != expected_top for name in modules):
            result.update(status='ERROR', reason='Report module does not match the supplied netlist top: ' + ', '.join(modules),
                          design_association='MISMATCH')
            return result
        result['design_association'] = 'TOP_NAME_MATCH_ONLY'
    def finding(line, kind, value, failed, evidence):
        result['findings'].append({'line': line, 'check': kind, 'value': value,
                                   'status': 'FAIL' if failed else 'PASS', 'evidence': evidence.strip()})
    number = r'[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?'
    for line_number, line in enumerate(text.splitlines(), 1):
        # Genus design-check summary counts; no filename assumptions.
        count = re.match(r'^\s*(Unresolved References|Undriven .*?|Multidriven .*?)\s+(\d+)\s*$', line, re.I)
        if count:
            finding(line_number, count[1].strip(), int(count[2]), int(count[2]) > 0, line)
        slack = re.search(r'\bslack\s*(?:\((?:MET|VIOLATED)\))?\s*[:=]*\s*(' + number + r')\b', line, re.I)
        if slack:
            finding(line_number, 'reported_slack_in_report_units', float(slack[1]), float(slack[1]) < 0, line)
        path_check = re.search(r'^\s*Path\s+\d+:\s*(MET|VIOLATED)\b', line, re.I)
        if path_check:
            finding(line_number, 'reported_timing_path', path_check[1].upper(), path_check[1].upper() == 'VIOLATED', line)
        violations = re.match(r'^\s*(?:Total\s+)?(DRC violations|DRC errors|Unconstrained paths)\s*[:=]\s*(\d+)\s*$', line, re.I)
        if violations:
            finding(line_number, violations[1], int(violations[2]), int(violations[2]) > 0, line)
        lvs = re.match(r'^\s*LVS\s*(?::|=)\s*(PASS|FAIL)\s*$', line, re.I)
        if lvs:
            finding(line_number, 'backend_reported_LVS_status', lvs[1].upper(), lvs[1].upper() == 'FAIL', line)
        severity = re.match(r'^\s*\|[^|]+\|\s*(Error|Fatal)\s*\|\s*(\d+)\|', line, re.I)
        if severity:
            finding(line_number, 'backend_' + severity[1].lower() + '_count', int(severity[2]), int(severity[2]) > 0, line)
        elif re.match(r'^\s*(?:ERROR|FATAL)(?:\s*[:\[]|\s+-)', line):
            finding(line_number, 'backend_error', line.strip(), True, line)
    if any(f['status'] == 'FAIL' for f in result['findings']):
        result['status'] = 'FAIL'
        result['reason'] = 'Backend report contains failed checks; inspect the recorded evidence.'
    elif result['findings']:
        result['status'] = 'PASS_WITH_GAPS'
        result['reason'] = 'Recognized entries passed; other content and report completeness were not verified.'
    else:
        result['reason'] = 'Report preserved; no supported check results found. Review or add a format-specific parser.'
    return result


class GDSIIValidationAgent:
    """One explicit backend artifact pair per call; isolated reports for each run."""
    def __init__(self, netlist_file, gds_file, top_module=None, gds_top=None,
                 required_modules=None, required_ports=None, required_cells=None, gds_libraries=None, reports=None, report_dirs=None):
        self.reports = reports or []
        self.report_dirs = report_dirs or []
        self.gds_libraries = [os.path.abspath(p) for p in gds_libraries or []]
        self.netlist_file = os.path.abspath(netlist_file)
        self.gds_file = os.path.abspath(gds_file)
        self.top_module = top_module
        self.gds_top = gds_top
        self.required_modules = required_modules or []
        self.required_ports = required_ports or []
        self.required_cells = required_cells or []

    def collect_backend_reports(self):
        paths = [os.path.abspath(p) for p in self.reports]
        for folder in self.report_dirs:
            folder = os.path.abspath(folder)
            if not os.path.isdir(folder):
                paths.append(folder)  # Emit an explicit input error in the report.
                continue
            for root, dirs, files in os.walk(folder):
                dirs[:] = sorted(d for d in dirs if d not in ('verification_reports', '.git'))
                paths.extend(os.path.join(root, name) for name in sorted(files))
        excluded = {os.path.realpath(p) for p in [self.netlist_file, self.gds_file] + self.gds_libraries}
        return list(dict.fromkeys(p for p in paths if os.path.realpath(p) not in excluded))

    def validate(self):
        structure = StructureNetlistConsistencyAgent(
            self.netlist_file, self.required_modules, self.required_ports, self.top_module).run()
        # By default require a matching top. Backend renaming is explicit via gds_top.
        layout = GDSIILayoutStructureConsistencyAgent(
            self.gds_file, self.required_cells, self.gds_top or structure.get('top_module'), self.gds_libraries).run()
        checks = {'netlist_structure': structure, 'gdsii_layout_structure': layout}
        if not all(os.path.isfile(p) for p in (self.netlist_file, self.gds_file)):
            status = 'ERROR'
        elif not layout.get('gds_available'):
            status = 'ERROR'
        elif not all(row.get('pass') for row in checks.values()):
            status = 'FAIL'
        else:
            status = 'PASS_WITH_GAPS'
        backend_reports = [inspect_backend_report(p, structure.get('top_module'))
                           for p in self.collect_backend_reports()]
        if any(r['status'] == 'ERROR' for r in backend_reports):
            status = 'ERROR'
        elif status != 'ERROR' and any(r['status'] == 'FAIL' for r in backend_reports):
            status = 'FAIL'
        return {'schema_version': 1, 'agent': 'GDSII_Validation_Agent', 'status': status,
                'backend_reports': backend_reports,
                'needs_review': True,
                'failed_backend_reports': [r for r in backend_reports if r['status'] in ('FAIL', 'ERROR')],
                'unparsed_backend_reports': [r for r in backend_reports if r['status'] == 'NOT_TESTED'],
                'netlist_file': self.netlist_file, 'gds_file': self.gds_file,
                'gds_libraries': self.gds_libraries, 'checks': checks,
                'structural_checks_passed': all(row.get('pass') for row in checks.values()),
                'coverage_limitations': [
                    'Netlist inspection uses a lightweight parser; it does not compile or simulate the design.',
                    'Layout-versus-schematic (LVS) connectivity and design-rule checks (DRC) were not run.',
                    'Backend report findings are imported evidence, not rerun checks; matching design names do not establish run provenance.',
                    'Unsupported report content, timing completeness, protocol compliance and coverage require review.'],
                'allow_signoff': False}

    def run(self):
        reporter = AgentReport('gdsii_validation', inputs=[os.path.dirname(self.gds_file)])
        reporter.step('structural_validation')
        result = self.validate()
        for index, entry in enumerate(result['backend_reports'], 1):
            path = entry['source']
            name = 'backend_reports/{:04d}_{}'.format(index, os.path.basename(path))
            if os.path.isfile(path):
                try:
                    reporter.artifact(name, source=path)
                    entry['artifact'] = reporter.data['artifacts'][name]
                except OSError as exc:
                    entry.update(status='ERROR', reason='Could not preserve report: ' + str(exc))
                    result['status'] = 'ERROR'
        result['failed_backend_reports'] = [r for r in result['backend_reports'] if r['status'] in ('FAIL', 'ERROR')]
        reporter.artifact('gdsii_validation_report.json', data=result)
        reporter.update(status=result['status'], verification_status=result['status'],
                        checks=result['checks'], backend_reports=result['backend_reports'],
                        failed_backend_reports=result['failed_backend_reports'],
                        unparsed_backend_reports=result['unparsed_backend_reports'], coverage_limitations=result['coverage_limitations'],
                        exit_code=0 if result['status'] == 'PASS_WITH_GAPS' else 1,
                        finished_at=utc_now(), failure_stage=None if result['status'] == 'PASS_WITH_GAPS' else 'validation')
        self.report_path = os.path.join(reporter.directory, 'gdsii_validation_report.json')
        return result


# Compatibility for Python callers while using the new module filename.
Validation2Agent = GDSIIValidationAgent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--netlist', required=True, dest='netlist_file')
    parser.add_argument('--gds', required=True, dest='gds_file')
    parser.add_argument('--report', action='append', dest='reports', help='Backend report file; repeat for any number of reports')
    parser.add_argument('--reports-dir', action='append', dest='report_dirs', help='Recursively ingest a backend report directory; repeat as needed')
    parser.add_argument('--top', dest='top_module', help='Netlist top; required if inference is ambiguous')
    parser.add_argument('--gds-library', action='append', dest='gds_libraries', help='Additional standard-cell or macro GDS library; repeat as needed')
    parser.add_argument('--gds-top', help='Layout top if renamed by the backend')
    parser.add_argument('--require-module', action='append', dest='required_modules')
    parser.add_argument('--require-port', action='append', dest='required_ports')
    parser.add_argument('--require-cell', action='append', dest='required_cells')
    validator = GDSIIValidationAgent(**vars(parser.parse_args()))
    result = validator.run()
    print(json.dumps(result, indent=2))
    print('Report: ' + validator.report_path)
    return 0 if result['status'] == 'PASS_WITH_GAPS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
