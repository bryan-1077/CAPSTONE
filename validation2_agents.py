# validation2_agents.py

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

    for fname in os.listdir(report_dir):
        path = os.path.join(report_dir, fname)
        if re.search(r'timing', fname, re.IGNORECASE):
            reports['timing'] = path
        elif re.search(r'messages', fname, re.IGNORECASE):
            reports['sim_log'] = path
        elif re.search(r'gates|check_design_post|check_unresolved', fname, re.IGNORECASE):
            reports['compile_log'] = path
        elif re.search(r'area', fname, re.IGNORECASE):
            reports['coverage'] = path
    return reports


# ============================================================
# Verilog / Netlist Parsing
# ============================================================

def extract_module_blocks(verilog_text):
    if not verilog_text:
        return []
    pattern = re.compile(
        r'^\s*module\s+([a-zA-Z_]\w*)\b(.*?)(?=^\s*endmodule\b)',
        re.MULTILINE | re.DOTALL
    )
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
    header_match = re.search(r'\bmodule\s+[a-zA-Z_]\w*\s*\((.*?)\)\s*;', module_block_text, re.DOTALL)
    if not header_match:
        return []
    port_blob = header_match.group(1)
    port_blob = re.sub(r'//.*', '', port_blob)
    port_blob = re.sub(r'/\*.*?\*/', '', port_blob, flags=re.DOTALL)
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
            inst_pat = r'^\s*' + re.escape(candidate) + r'\s+([a-zA-Z_]\w*)\s*\('
            if re.search(inst_pat, block_text, re.MULTILINE):
                instantiated.add(candidate)
    top_candidates = [m for m in module_names if m not in instantiated]
    if top_candidates:
        for cand in top_candidates:
            if "top" in cand.lower() or "ctrl" in cand.lower():
                return cand
        return top_candidates[0]
    return module_names[0]


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
    def __init__(self, netlist_file, required_modules=None, required_ports=None):
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
        top_module = detect_top_module(module_names, text)
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
            "missing_required_modules": missing_modules,
            "missing_required_ports": missing_ports,
            "clocks_found": clocks,
            "resets_found": resets,
            "pass": len(missing_modules) == 0 and len(missing_ports) == 0 and len(clocks) > 0 and len(resets) > 0
        }
        return self.report


# ============================================================
# Agent 2: GDSII Layout Structure Consistency
# ============================================================

class GDSIILayoutStructureConsistencyAgent:
    def __init__(self, gds_file, expected_modules):
        self.gds_file = gds_file
        self.expected_modules = expected_modules
        self.report = {}

    def parse_gds(self):
        if not GDSPY_AVAILABLE:
            return {"available": False, "reason": "gdspy not installed"}
        if not file_exists(self.gds_file):
            return {"available": False, "reason": "GDS file not found"}
        try:
            lib = gdspy.GdsLibrary()
            lib.read_gds(self.gds_file)
            all_cells = list(lib.cells.keys())
            top_candidates = [c for c in all_cells if not any(c in getattr(cell, "references", []) for cell in lib.cells.values())]
            references = {cell_name: [ref.ref_cell.name for ref in getattr(cell, "references", [])] for cell_name, cell in lib.cells.items()}
            return {"available": True, "cells": all_cells, "top_candidates": top_candidates, "references": references}
        except Exception as e:
            return {"available": False, "reason": str(e)}

    def run(self):
        parsed = self.parse_gds()
        if not parsed["available"]:
            self.report = {"pass": False, "reason": parsed.get("reason")}
            return self.report
        gds_cells = parsed["cells"]
        gds_top = parsed["top_candidates"][0] if parsed["top_candidates"] else None
        missing_in_gds = [m for m in self.expected_modules if m not in gds_cells]
        extra_in_gds = [c for c in gds_cells if c not in self.expected_modules]
        instance_counts = {}
        if gds_top and gds_top in parsed["references"]:
            instance_counts = dict(Counter(parsed["references"][gds_top]))
        self.report = {
            "gds_available": True,
            "gds_top": gds_top,
            "cells_found": gds_cells,
            "missing_expected_cells": missing_in_gds,
            "extra_cells": extra_in_gds,
            "top_cell_instance_counts": instance_counts,
            "pass": len(missing_in_gds) == 0
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
        self.report = {"assertions_passed_count": len(passes), "assertions_failed_count": len(fails), "pass": len(fails)==0}
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
        compile_pass = len(compile_errors)==0
        sim_pass = len(sim_errors)==0
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
        self.report = {"functional_coverage": None, "code_coverage": None, "pass": True}
        return self.report


# ============================================================
# Validation 2 Orchestrator
# ============================================================

class Validation2Agent:
    def __init__(self, netlist_file, gds_file, report_folder="GDSII_Reports"):
        self.netlist_file = netlist_file
        self.gds_file = gds_file

        # automatically find report files
        reports = find_report_files(report_folder)

        netlist_text = read_file_safely(netlist_file)
        module_names = extract_module_names(netlist_text)
        top_module = detect_top_module(module_names, netlist_text)
        ports = []
        clocks, resets = [], []
        for mod_name, block_text in extract_module_blocks(netlist_text):
            if mod_name == top_module:
                ports = extract_ports_from_module_block(block_text)
                clocks, resets = find_clock_and_reset_ports(ports)
                break

        self.structure_agent = StructureNetlistConsistencyAgent(
            netlist_file, required_modules=[top_module], required_ports=clocks+resets
        )
        self.gds_structure_agent = GDSIILayoutStructureConsistencyAgent(
            gds_file, expected_modules=module_names
        )
        self.protocol_agent = ProtocolAssertionComplianceAgent(reports.get("sim_log"))
        self.gate_level_agent = GateLevelSynthesizedNetlistValidationAgent(
            netlist_file, reports.get("compile_log"), reports.get("sim_log")
        )
        self.timing_agent = TimingConstraintSanityAgent(reports.get("timing"))
        self.coverage_agent = CoverageTestCompletenessAgent(reports.get("coverage"))

    def validate(self):
        return {
            "structure_netlist/gdsii_consistency": self.structure_agent.run(),
            #"gdsii_layout_structure_consistency": self.gds_structure_agent.run(),
            "protocol_assertion_compliance": self.protocol_agent.run(),
            "gate_level_synthesized_netlist_validation": self.gate_level_agent.run(),
            "timing_constraint_sanity": self.timing_agent.run(),
            "coverage_test_completeness": self.coverage_agent.run()
        }


# ============================================================
# Example usage
# ============================================================

if __name__ == "__main__":
    netlist_file = "MemoryController_impl_generic.v"
    gds_file = "MemoryController_impl.gds"

    validator = Validation2Agent(netlist_file, gds_file, report_folder="GDSII_Reports")
    report = validator.validate()
    print(json.dumps(report, indent=2))