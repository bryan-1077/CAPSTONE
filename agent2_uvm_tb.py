#!/usr/bin/env python3
"""
agent2_uvm_tb.py
================
Generic Agent 2: UVM testbench generator for multi-file RTL.

Features:
- Reads validation_result.json from Agent 1
- Supports multiple RTL files
- Uses top_module_name / top_module_file / dut_spec from Agent 1
- Generates generic UVM TB:
    - tb_auto/dut_pkg.sv
    - tb_auto/dut_if.sv
    - tb_auto/dut_tb_top.sv
- Compiles all RTL files + TB files using VCS
- Iterates with compile-error feedback
- Strips any accidental `timescale directives from generated files
- Compiles with global VCS timescale: -timescale=1ns/1ps
"""

import os
import re
import sys
import json
import shutil
import subprocess
import requests
from dotenv import load_dotenv

TB_ROOT = "tb_auto"
BUILD_DIR = "build_auto"
SIMV_NAME = "simv_auto"
MAX_ITERS = 5
VALIDATION_RESULT_FILE = "validation_result.json"
VCS_UVM_FLAGS = ["-ntb_opts", "uvm"]
ENABLE_DEBUG = True

COLORS = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "green": "\033[92m",
    "yellow": "\033[93m",
    "red": "\033[91m",
    "cyan": "\033[96m",
    "magenta": "\033[95m",
    "grey": "\033[90m",
}

def c(color, text):
    return f"{COLORS.get(color, '')}{text}{COLORS['reset']}"

def banner(title, color="magenta"):
    w = 58
    line = "═" * w
    print(f"\n{c(color, line)}")
    print(c("bold", f"  {title}"))
    print(f"{c(color, line)}\n")

def log(agent, msg, level="info"):
    color_map = {
        "info": "cyan",
        "ok": "green",
        "warn": "yellow",
        "error": "red",
        "muted": "grey",
    }
    print(f"{c(color_map.get(level, 'cyan'), f'[{agent}]')} {msg}")

def section(title):
    print(f"\n{c('grey', '─' * 50)}")
    print(c("bold", title))
    print(c('grey', '─' * 50))


# ─────────────────────────────────────────────
# LLM
# ─────────────────────────────────────────────
def call_llm(system_prompt, user_prompt, agent="LLM"):
    load_dotenv()
    api_key = os.getenv("TAMU_API_KEY")
    base_url = os.getenv("TAMU_BASE_URL", "").strip()
    model = os.getenv("TAMU_MODEL")

    if not all([api_key, base_url, model]):
        raise RuntimeError("Missing TAMU_API_KEY / TAMU_BASE_URL / TAMU_MODEL in .env")

    headers = {
        "Authorization": "Bearer {}".format(api_key),
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 1,
        "max_tokens": 5000,
    }

    resp = requests.post(base_url, headers=headers, data=json.dumps(payload), timeout=180)
    if resp.status_code != 200:
        raise RuntimeError("LLM HTTP {}: {}".format(resp.status_code, resp.text[:400]))

    chunks = []
    for line in resp.text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        chunk = line[5:].strip()
        if chunk in ("[DONE]", ""):
            continue
        try:
            data = json.loads(chunk)
            delta = data["choices"][0]["delta"]
            if "content" in delta:
                chunks.append(delta["content"])
        except Exception:
            continue

    return "".join(chunks).strip()


# ─────────────────────────────────────────────
# FILE BLOCKS
# ─────────────────────────────────────────────
FILE_BLOCK_RE = re.compile(
    r"===\s*filename:\s*(.*?)\s*===\s*\n(.*?)(?=\n===\s*filename:|\Z)",
    re.DOTALL,
)

def parse_file_blocks(text):
    return [(f.strip(), code.strip() + "\n") for f, code in FILE_BLOCK_RE.findall(text)]

def strip_timescale_directives(code):
    return re.sub(r'^\s*`timescale[^\n]*\n', '', code, flags=re.MULTILINE)

def write_files(files):
    for fname, code in files:
        code = strip_timescale_directives(code)
        d = os.path.dirname(fname)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(fname, "w") as fh:
            fh.write(code)
        log("FILE_WRITER", "Wrote: {}".format(fname), "muted")


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────
def load_validation_result():
    if not os.path.exists(VALIDATION_RESULT_FILE):
        return None
    with open(VALIDATION_RESULT_FILE, "r") as f:
        return json.load(f)

def gather_sv_files(root):
    sv = []
    for r, _, files in os.walk(root):
        for fn in sorted(files):
            if fn.endswith(".sv") or fn.endswith(".v"):
                sv.append(os.path.join(r, fn))
    return sv

def gather_incdirs(root):
    incdirs = set()
    for r, _, _ in os.walk(root):
        incdirs.add(r)
    return sorted(incdirs)

def which(tool):
    return shutil.which(tool)

COMPILE_ORDER = [
    "dut_pkg.sv",
    "dut_if.sv",
    "dut_tb_top.sv",
    "tb_top.sv",
]

def ordered_tb_files(root):
    all_files = gather_sv_files(root)
    basename_to_path = {os.path.basename(p): p for p in all_files}

    ordered = []
    top_files = []

    for name in COMPILE_ORDER:
        if name in basename_to_path:
            path = basename_to_path[name]
            if "top" in name:
                top_files.append(path)
            else:
                ordered.append(path)

    known = set(COMPILE_ORDER)
    for path in all_files:
        bn = os.path.basename(path)
        if bn not in known:
            if "top" in bn:
                top_files.append(path)
            else:
                ordered.append(path)

    return ordered + top_files

def ensure_build_dirs():
    log_dir = os.path.join(BUILD_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)
    return log_dir

def read_file(path):
    with open(path, "r", errors="ignore") as f:
        return f.read()

def read_tb_files_for_context(max_chars=12000):
    parts = []
    total = 0
    for path in sorted(gather_sv_files(TB_ROOT)):
        code = read_file(path)
        chunk = "\n// ===== {} =====\n{}".format(path, code)
        parts.append(chunk)
        total += len(chunk)
        if total > max_chars:
            parts.append("\n// ... truncated ...")
            break
    return "\n".join(parts)


# ─────────────────────────────────────────────
# VCS
# ─────────────────────────────────────────────
def extract_vcs_errors(compile_out):
    lines = compile_out.splitlines()
    capturing = False
    blocks = []
    current = []
    blank_run = 0

    for ln in lines:
        is_error = bool(re.match(r"^\s*Error-\[", ln))
        if is_error:
            if current:
                blocks.append("\n".join(current))
            current = [ln]
            capturing = True
            blank_run = 0
        elif capturing:
            if ln.strip() == "":
                blank_run += 1
                if blank_run >= 2:
                    capturing = False
                    blocks.append("\n".join(current))
                    current = []
                    blank_run = 0
            else:
                blank_run = 0
                current.append(ln)

    if current:
        blocks.append("\n".join(current))

    result_lines = []
    for b in blocks:
        result_lines.extend(b.splitlines())
        result_lines.append("")
        if len(result_lines) > 120:
            break

    return "\n".join(result_lines[:120]).strip()

def vcs_compile(rtl_files, iter_idx):
    log_dir = ensure_build_dirs()
    compile_log = os.path.join(log_dir, "compile_iter_{}.log".format(iter_idx))
    filelist = os.path.join(BUILD_DIR, "tb_filelist.f")
    simv_path = os.path.join(BUILD_DIR, SIMV_NAME)

    if not which("vcs"):
        msg = "ERROR: VCS executable not found in PATH."
        with open(compile_log, "w") as fh:
            fh.write(msg + "\n")
        return False, compile_log, msg

    tb_files = ordered_tb_files(TB_ROOT)
    with open(filelist, "w") as fh:
        for tf in tb_files:
            fh.write(tf + "\n")

    inc_flags = ["+incdir+{}".format(d) for d in gather_incdirs(TB_ROOT)]
    debug_flags = ["-debug_access+r+w-memcbk", "-debug_region+cell"] if ENABLE_DEBUG else []

    cmd = (
        ["vcs", "-sverilog", "-full64", "-timescale=1ns/1ps"]
        + VCS_UVM_FLAGS
        + debug_flags
        + inc_flags
        + rtl_files
        + ["-f", filelist, "-o", simv_path]
    )

    log("VCS", "Compiling {} RTL file(s) + TB...".format(len(rtl_files)), "info")
    p = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
    )
    out, _ = p.communicate()

    with open(compile_log, "w") as fh:
        fh.write(out)

    return p.returncode == 0, compile_log, out

def vcs_run(iter_idx):
    log_dir = ensure_build_dirs()
    sim_log = os.path.join(log_dir, "sim_iter_{}.log".format(iter_idx))
    simv_path = os.path.join(BUILD_DIR, SIMV_NAME)

    if not os.path.exists(simv_path):
        return False, sim_log, ""

    p = subprocess.Popen(
        [simv_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
    )
    out, _ = p.communicate()

    with open(sim_log, "w") as fh:
        fh.write(out)

    return p.returncode == 0, sim_log, out


# ─────────────────────────────────────────────
# REPORT
# ─────────────────────────────────────────────
def extract_scoreboard_block(sim_text):
    idxs = [m.start() for m in re.finditer(r"SCOREBOARD\s+REPORT", sim_text)]
    if not idxs:
        return None
    tail = sim_text[idxs[-1]:]
    lines = tail.splitlines()
    window, sep_count = [], 0
    for ln in lines[:80]:
        window.append(ln)
        if re.match(r"^\s*=+\s*$", ln):
            sep_count += 1
            if sep_count >= 2:
                break
    return "\n".join(window).strip() or None

def count_uvm_issues(sim_text):
    n_err = len(re.findall(r"^UVM_ERROR\s+\S+\(", sim_text, re.MULTILINE))
    n_fat = len(re.findall(r"^UVM_FATAL\s+\S+\(", sim_text, re.MULTILINE))
    return n_err, n_fat

def print_scoreboard(sim_text, iter_idx):
    n_err, n_fat = count_uvm_issues(sim_text)
    sb = extract_scoreboard_block(sim_text)
    passed = (n_err == 0 and n_fat == 0)

    print()
    print(c("cyan", "═" * 58))
    print(c("bold", "  SCOREBOARD REPORT — Iteration {}".format(iter_idx + 1)))
    print(c("cyan", "═" * 58))
    print("  Status    : {}".format(c('green', 'PASS') if passed else c('red', 'FAIL')))
    print("  UVM_ERROR : {}".format(n_err))
    print("  UVM_FATAL : {}".format(n_fat))
    if sb:
        print(c("grey", "\n  ── Scoreboard Output ──"))
        for ln in sb.splitlines()[:30]:
            print("  {}".format(ln))
    else:
        print("  {}".format(c('yellow', 'No scoreboard block found.')))
    print(c("cyan", "═" * 58))
    return passed


# ─────────────────────────────────────────────
# PROMPTS
# ─────────────────────────────────────────────
UVM_SYSTEM = """\
You are an expert UVM verification engineer targeting Synopsys VCS.

Output ONLY file blocks in this exact format:
=== filename: tb_auto/<name>.sv ===
<complete SystemVerilog source>

Rules:
- No markdown fences
- No prose
- No explanations
- Output only complete file blocks
"""

def format_dut_spec_for_prompt(dut_spec):
    return json.dumps(dut_spec, indent=2)

def build_port_summary(dut_spec):
    lines = []
    for p in dut_spec["ports"]:
        rng = " {}".format(p["range"]) if p["range"] else ""
        lines.append("- {}{} {}".format(p["dir"], rng, p["name"]))
    return "\n".join(lines)

def detect_single_clock_name(dut_spec):
    if len(dut_spec["clock_ports"]) == 1:
        return dut_spec["clock_ports"][0]
    return None

def detect_single_reset(dut_spec):
    if len(dut_spec["reset_ports"]) == 1:
        return dut_spec["reset_ports"][0]
    return None

def prompt_generate(rtl_code, dut_spec, rtl_files):
    module_name = dut_spec["module_name"]
    port_summary = build_port_summary(dut_spec)
    spec_json = format_dut_spec_for_prompt(dut_spec)
    clk_name = detect_single_clock_name(dut_spec)
    rst_info = detect_single_reset(dut_spec)

    clk_hint = clk_name if clk_name else "none_or_multiple"
    rst_hint = json.dumps(rst_info, indent=2) if rst_info else "none_or_multiple"

    return """Generate a complete, generic, working UVM testbench for the TOP DUT only.

TOP DUT RTL:
{rtl_code}

TOP DUT SPEC:
{spec_json}

FULL RTL FILE LIST FOR CONTEXT:
{rtl_files}

PORT SUMMARY:
{port_summary}

CRITICAL REQUIREMENTS:

1. Output exactly 3 files:
   - tb_auto/dut_pkg.sv
   - tb_auto/dut_if.sv
   - tb_auto/dut_tb_top.sv

2. FILE: dut_pkg.sv
   - Must be `package dut_pkg;`
   - Must include:
       import uvm_pkg::*;
       `include "uvm_macros.svh"
   - Must contain ALL UVM classes inside the package:
       dut_seq_item
       dut_sequence
       dut_driver
       dut_monitor
       dut_sequencer
       dut_agent
       dut_scoreboard
       dut_env
       dut_test
   - End with `endpackage`

3. FILE: dut_if.sv
   - Plain SystemVerilog interface
   - Include ALL TOP DUT ports using the exact DUT port names and widths from the spec
   - Do not rename DUT ports

4. FILE: dut_tb_top.sv
   - Plain module
   - Must import:
       import uvm_pkg::*;
       import dut_pkg::*;
   - Instantiate the interface
   - Instantiate DUT module `{module_name}` by exact name
   - Connect DUT ports to interface signals by exact port names
   - If exactly one clock exists (`{clk_hint}`), generate a simple clock
   - Put the virtual interface into uvm_config_db
   - Call run_test("dut_test")

5. Transaction modeling
   - Build dut_seq_item fields from TOP DUT ports:
     * stimulus fields from input ports excluding clocks/resets
     * observed fields for outputs

6. Reset handling
   - If there is exactly one reset, use this hint:
       {rst_hint}
   - Respect reset polarity/style if inferable
   - If unknown, keep reset handling conservative

7. Scoreboard
   - Do NOT hardcode counter behavior
   - Implement generic smoke/sanity checking
   - It is acceptable to check:
       * transactions are observed
       * outputs are not permanently X/Z after reset/settling
       * monitor and driver activity exists
   - In report_phase, print:
       `uvm_info("SB", "=== SCOREBOARD REPORT ===", UVM_NONE)
       `uvm_info("SB", $sformatf("PASS=%0d FAIL=%0d", pass_cnt, fail_cnt), UVM_NONE)

8. Sequence
   - Create a valid sequence that exercises controllable inputs
   - If no controllable inputs exist, still run a smoke sequence

9. VCS/UVM correctness
   - Ensure package/import ordering is valid
   - Use virtual interface properly
   - Keep all classes inside dut_pkg.sv

10. IMPORTANT
   - This is a generic DUT, not necessarily a counter
   - Use only the provided DUT/spec
   - Output only the 3 file blocks

11. TIMESCALE RULE
   - Do NOT include `timescale in any generated file
   - Do not place `timescale in dut_pkg.sv
   - Do not place `timescale in dut_if.sv
   - Do not place `timescale in dut_tb_top.sv
""".format(
        rtl_code=rtl_code,
        spec_json=spec_json,
        rtl_files=json.dumps(rtl_files, indent=2),
        port_summary=port_summary,
        module_name=module_name,
        clk_hint=clk_hint,
        rst_hint=rst_hint
    )

def prompt_fix(rtl_code, dut_spec, rtl_files, error_block, tb_source):
    spec_json = format_dut_spec_for_prompt(dut_spec)

    return """The generated generic UVM testbench FAILED in VCS. Fix every compile/runtime issue.

DO NOT MODIFY THE DUT RTL.

TOP DUT RTL:
{rtl_code}

TOP DUT SPEC:
{spec_json}

FULL RTL FILE LIST:
{rtl_files}

VCS ERROR OUTPUT:
{error_block}

CURRENT GENERATED TESTBENCH SOURCE:
{tb_source}

REQUIRED STRUCTURE:
- tb_auto/dut_pkg.sv
- tb_auto/dut_if.sv
- tb_auto/dut_tb_top.sv

FIX RULES:
1. Keep all UVM classes inside dut_pkg.sv
2. Keep dut_if.sv as a plain interface
3. Keep dut_tb_top.sv as a plain module
4. Preserve exact DUT module name and exact DUT port names from the spec
5. Do not rename DUT ports
6. Fix package/import/order/type/VCS issues exactly as required
7. Do not introduce counter-specific assumptions
8. Output complete corrected versions of ALL 3 files
9. Output only file blocks
10. Remove every `timescale directive from all generated files

Output only the corrected 3 file blocks.
""".format(
        rtl_code=rtl_code,
        spec_json=spec_json,
        rtl_files=json.dumps(rtl_files, indent=2),
        error_block=error_block,
        tb_source=tb_source
    )


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def run_uvm_agent(rtl_files, top_module_file, dut_spec):
    banner("AGENT 2 — UVM TESTBENCH", "magenta")

    os.makedirs(TB_ROOT, exist_ok=True)
    ensure_build_dirs()

    rtl_code = read_file(top_module_file)

    log("UVM_TB_AGENT", "Top module: {}".format(dut_spec["module_name"]), "ok")
    log("UVM_TB_AGENT", "Top file: {}".format(top_module_file), "ok")
    log("UVM_TB_AGENT", "RTL files: {}".format(len(rtl_files)), "info")

    last_compile_out = ""

    for i in range(MAX_ITERS):
        section("Iteration {} / {}".format(i + 1, MAX_ITERS))

        if i == 0:
            if os.path.exists(TB_ROOT):
                shutil.rmtree(TB_ROOT)
            os.makedirs(TB_ROOT)
            prompt = prompt_generate(rtl_code, dut_spec, rtl_files)
            log("UVM_TB_AGENT", "Generating generic UVM testbench...", "info")
        else:
            error_block = extract_vcs_errors(last_compile_out)
            if not error_block:
                error_block = "(no parseable VCS error block found)"
            tb_source = read_tb_files_for_context()
            prompt = prompt_fix(rtl_code, dut_spec, rtl_files, error_block, tb_source)
            log("UVM_TB_AGENT", "Fixing testbench using VCS errors...", "warn")

        raw = call_llm(UVM_SYSTEM, prompt, "UVM_TB_AGENT")
        files = parse_file_blocks(raw)

        if not files:
            log("UVM_TB_AGENT", "LLM returned no file blocks.", "error")
            continue

        write_files(files)

        ok_compile, compile_log, compile_out = vcs_compile(rtl_files, i)
        last_compile_out = compile_out
        log("VCS", "Compile {} -> {}".format("PASS" if ok_compile else "FAIL", compile_log), "ok" if ok_compile else "warn")

        if not ok_compile:
            err_summary = extract_vcs_errors(compile_out)
            if err_summary:
                print(c("yellow", "\n── VCS Errors ──"))
                for ln in err_summary.splitlines()[:30]:
                    print(ln)
            continue

        ok_run, sim_log, sim_out = vcs_run(i)
        log("VCS", "Simulation {} -> {}".format("PASS" if ok_run else "FAIL", sim_log), "ok" if ok_run else "warn")

        passed = print_scoreboard(sim_out, i)

        if ok_run and passed:
            print("\n{}\n".format(c('green', '[UVM_TB_AGENT] PASS — UVM TB generated and simulated successfully.')))
            return True

    print("\n{}\n".format(c('red', '[UVM_TB_AGENT] FAIL after {} iterations.'.format(MAX_ITERS))))
    return False

def main():
    val = load_validation_result()

    if len(sys.argv) > 1:
        rtl_files = [os.path.abspath(x) for x in sys.argv[1:]]
        if not val:
            print(c("red", "validation_result.json is required for generic Agent 2."))
            sys.exit(1)
    else:
        if not val:
            print(c("red", "Missing {}. Run Agent 1 first.".format(VALIDATION_RESULT_FILE)))
            sys.exit(1)
        rtl_files = val.get("rtl_files", [])

    if not val:
        print(c("red", "Missing validation_result.json."))
        sys.exit(1)

    if not val.get("is_valid"):
        print(c("red", "[BLOCKED] Agent 1 marked RTL invalid."))
        print(c("yellow", "Summary: {}".format(val.get('summary', ''))))
        for issue in val.get("issues", []):
            print("  - {}".format(issue))
        sys.exit(1)

    top_module_file = val.get("top_module_file")
    dut_spec = val.get("dut_spec")

    if not rtl_files:
        print(c("red", "No rtl_files found in validation_result.json"))
        sys.exit(1)
    if not top_module_file:
        print(c("red", "No top_module_file found in validation_result.json"))
        sys.exit(1)
    if not dut_spec:
        print(c("red", "No dut_spec found in validation_result.json"))
        sys.exit(1)

    ok = run_uvm_agent(rtl_files, top_module_file, dut_spec)
    sys.exit(0 if ok else 1)

if __name__ == "__main__":
    main()