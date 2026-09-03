#!/usr/bin/env python3
"""
agent3_testvectors.py
=====================
Generic Agent 3: Test Vector Generator

Role in pipeline:
- Reads validation_result.json from Agent 1
- Uses DUT spec and top module metadata
- Uses LLM only to generate a JSON vector plan
- Python deterministically generates a plain SystemVerilog vector testbench
- Compiles all RTL files + vector TB with VCS
- Runs simulation and prints a vector test summary
"""

import os
import re
import sys
import json
import shutil
import subprocess
import requests
from dotenv import load_dotenv

VALIDATION_RESULT_FILE = "validation_result.json"
TB_ROOT = "tv_auto"
BUILD_DIR = "build_auto_tv"
SIMV_NAME = "simv_tv"
MAX_LLM_RETRIES = 3
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
    return "{}{}{}".format(COLORS.get(color, ""), text, COLORS["reset"])

def banner(title, color="yellow"):
    w = 58
    line = "═" * w
    print("\n{}".format(c(color, line)))
    print(c("bold", "  {}".format(title)))
    print("{}".format(c(color, line)))
    print()

def log(agent, msg, level="info"):
    color_map = {
        "info": "cyan",
        "ok": "green",
        "warn": "yellow",
        "error": "red",
        "muted": "grey",
    }
    print("{} {}".format(c(color_map.get(level, "cyan"), "[{}]".format(agent)), msg))

def section(title):
    print("\n{}".format(c("grey", "─" * 50)))
    print(c("bold", title))
    print(c("grey", "─" * 50))


# ─────────────────────────────────────────────
# BASIC HELPERS
# ─────────────────────────────────────────────
def load_validation_result():
    if not os.path.exists(VALIDATION_RESULT_FILE):
        return None
    with open(VALIDATION_RESULT_FILE, "r") as f:
        return json.load(f)

def read_file(path):
    with open(path, "r", errors="ignore") as f:
        return f.read()

def collect_rtl_files(paths):
    rtl_files = []
    for path in paths:
        if not os.path.exists(path):
            raise FileNotFoundError("Path not found: {}".format(path))
        if os.path.isfile(path):
            if path.endswith((".sv", ".v")):
                rtl_files.append(os.path.abspath(path))
        elif os.path.isdir(path):
            for root, _, files in os.walk(path):
                for fn in files:
                    if fn.endswith((".sv", ".v")):
                        rtl_files.append(os.path.abspath(os.path.join(root, fn)))
    rtl_files = sorted(set(rtl_files))
    if not rtl_files:
        raise RuntimeError("No RTL files (.sv/.v) found.")
    return rtl_files

def ensure_build_dirs():
    os.makedirs(TB_ROOT, exist_ok=True)
    os.makedirs(BUILD_DIR, exist_ok=True)
    os.makedirs(os.path.join(BUILD_DIR, "logs"), exist_ok=True)

def strip_timescale_directives(code):
    return re.sub(r'^\s*`timescale[^\n]*\n', '', code, flags=re.MULTILINE)

def write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    text = strip_timescale_directives(text)
    with open(path, "w") as f:
        f.write(text)

def sanitize_identifier(s):
    s = re.sub(r"[^A-Za-z0-9_]", "_", s)
    if not s:
        s = "unnamed"
    if s[0].isdigit():
        s = "_" + s
    return s

def which(tool):
    return shutil.which(tool)


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
        "max_tokens": 4000,
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
        if chunk in ("", "[DONE]"):
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
# DUT SPEC HELPERS
# ─────────────────────────────────────────────
def get_single_clock(dut_spec):
    clocks = dut_spec.get("clock_ports", [])
    return clocks[0] if len(clocks) == 1 else None

def get_single_reset(dut_spec):
    resets = dut_spec.get("reset_ports", [])
    return resets[0] if len(resets) == 1 else None

def port_width(port):
    if port.get("width") is None:
        return 1
    return int(port["width"])

def sv_decl_type(port):
    w = port_width(port)
    if w == 1:
        return "logic"
    return "logic [{}:0]".format(w - 1)

def max_unsigned(width):
    if width <= 0:
        return 0
    return (1 << min(width, 30)) - 1

def clamp_to_width(value, width):
    if width <= 0:
        return 0
    mask = (1 << min(width, 63)) - 1
    return int(value) & mask

def sv_literal(value, width):
    value = clamp_to_width(value, width)
    if width == 1:
        return "1'b{}".format(value & 1)
    return "{}'d{}".format(width, value)

def choose_mid_value(width):
    if width == 1:
        return 1
    m = max(1, (1 << min(width, 16)) // 2)
    return m - 1

def default_input_value(width, mode="mid"):
    if mode == "zero":
        return 0
    if mode == "one":
        return 1
    if mode == "max":
        return max_unsigned(width)
    if mode == "toggle":
        return 1 if width == 1 else choose_mid_value(width)
    return choose_mid_value(width)

def summarize_ports(dut_spec):
    lines = []
    for p in dut_spec["ports"]:
        rng = " {}".format(p["range"]) if p.get("range") else ""
        lines.append("- {}{} {}".format(p["dir"], rng, p["name"]))
    return "\n".join(lines)


# ─────────────────────────────────────────────
# VECTOR PLAN GENERATION
# ─────────────────────────────────────────────
PLAN_SYSTEM = """You are a hardware verification engineer.

Return ONLY valid JSON.
No markdown.
No prose outside JSON.

Required schema:
{
  "design_summary": "one short sentence",
  "assumptions": ["short assumption 1", "short assumption 2"],
  "directed_tests": [
    {
      "name": "smoke_1",
      "description": "what it checks",
      "steps": [
        {
          "drive": {"sig_a": 0, "sig_b": 3},
          "hold_cycles": 2
        }
      ]
    }
  ],
  "random_tests": [
    {
      "name": "rand_1",
      "description": "what it checks",
      "num_transactions": 25
    }
  ]
}

Rules:
- Only include DUT controllable input ports in each drive object
- Do NOT include clock ports in drive
- Do NOT include reset in drive; reset is handled separately
- Use integer values only
- Generate 4 to 8 directed_tests
- Generate 2 to 4 random_tests
- Keep plans generic and conservative
"""

def default_plan(dut_spec):
    controllable = dut_spec.get("input_ports", [])
    directed = []
    random_tests = []

    if controllable:
        zero_drive = dict((p["name"], 0) for p in controllable)
        max_drive = dict((p["name"], max_unsigned(port_width(p))) for p in controllable)
        mix_drive = {}
        tog_drive = {}
        for i, p in enumerate(controllable):
            w = port_width(p)
            mix_drive[p["name"]] = default_input_value(w, "mid")
            tog_drive[p["name"]] = (i + 1) & max_unsigned(w)

        directed = [
            {
                "name": "all_zero_smoke",
                "description": "Drive all controllable inputs to zero.",
                "steps": [{"drive": zero_drive, "hold_cycles": 3}]
            },
            {
                "name": "all_max_smoke",
                "description": "Drive all controllable inputs to max values.",
                "steps": [{"drive": max_drive, "hold_cycles": 3}]
            },
            {
                "name": "mixed_values",
                "description": "Drive representative mid-range values.",
                "steps": [{"drive": mix_drive, "hold_cycles": 4}]
            },
            {
                "name": "toggle_pattern",
                "description": "Drive a simple changing pattern.",
                "steps": [
                    {"drive": zero_drive, "hold_cycles": 2},
                    {"drive": tog_drive, "hold_cycles": 2},
                    {"drive": max_drive, "hold_cycles": 2}
                ]
            },
        ]
        random_tests = [
            {
                "name": "random_smoke",
                "description": "Randomly exercises controllable inputs.",
                "num_transactions": 25
            },
            {
                "name": "random_longer",
                "description": "Longer randomized pass for broader coverage.",
                "num_transactions": 50
            },
        ]
    else:
        directed = [
            {
                "name": "clock_reset_smoke",
                "description": "Observe DUT under reset release and free-running clock.",
                "steps": [{"drive": {}, "hold_cycles": 8}]
            },
            {
                "name": "long_observation",
                "description": "Observe longer post-reset behavior.",
                "steps": [{"drive": {}, "hold_cycles": 16}]
            },
        ]
        random_tests = [
            {
                "name": "observation_only",
                "description": "No controllable inputs; observation-only randomized phase.",
                "num_transactions": 20
            }
        ]

    return {
        "design_summary": "Generic vector plan for {}.".format(dut_spec["module_name"]),
        "assumptions": [
            "Only smoke-level generic checking is applied.",
            "Reset is handled separately by the Python-generated testbench."
        ],
        "directed_tests": directed,
        "random_tests": random_tests,
    }

def get_vector_plan(rtl_code, dut_spec):
    agent = "TV_PLAN_AGENT"
    controllable_names = [p["name"] for p in dut_spec.get("input_ports", [])]

    prompt = """Create a generic vector plan for this DUT.

DUT RTL:
{rtl_code}

DUT SPEC:
{dut_spec}

PORT SUMMARY:
{port_summary}

Controllable input ports:
{controllable_names}

Remember:
- drive only controllable inputs
- do not drive clocks
- do not drive reset
- be conservative and generic
""".format(
        rtl_code=rtl_code,
        dut_spec=json.dumps(dut_spec, indent=2),
        port_summary=summarize_ports(dut_spec),
        controllable_names=json.dumps(controllable_names, indent=2)
    )

    for attempt in range(MAX_LLM_RETRIES):
        try:
            raw = call_llm(PLAN_SYSTEM, prompt, agent)
            clean = re.sub(r"```[a-zA-Z0-9_]*", "", raw).replace("```", "").strip()
            m = re.search(r"\{[\s\S]*\}", clean)
            obj = json.loads(m.group(0) if m else clean)
            if "directed_tests" in obj and "random_tests" in obj:
                return obj
        except Exception as e:
            log(agent, "Plan parse failed on attempt {}: {}".format(attempt + 1, e), "warn")

    log(agent, "Falling back to default generated plan.", "warn")
    return default_plan(dut_spec)


# ─────────────────────────────────────────────
# VECTOR TB GENERATION
# ─────────────────────────────────────────────
def build_signal_decls(dut_spec):
    lines = []
    for p in dut_spec["ports"]:
        lines.append("  {} {};".format(sv_decl_type(p), p["name"]))
    return "\n".join(lines)

def build_dut_instantiation(dut_spec):
    conns = [".{}({})".format(p["name"], p["name"]) for p in dut_spec["ports"]]
    joined = ",\n    ".join(conns)
    return """  {module_name} dut (
    {joined}
  );""".format(module_name=dut_spec["module_name"], joined=joined)

def build_clock_gen(dut_spec):
    clk = get_single_clock(dut_spec)
    if not clk:
        return "  // No single clock inferred; no automatic clock generation added."
    return """  initial {clk} = 1'b0;
  always #5 {clk} = ~{clk};""".format(clk=clk)

def build_reset_task(dut_spec):
    rst = get_single_reset(dut_spec)
    clk = get_single_clock(dut_spec)

    if not rst:
        return """  task automatic apply_reset();
    begin
      // No reset inferred; skipping reset sequence.
    end
  endtask"""

    rst_name = rst["name"]
    active = rst.get("active", "unknown")
    style = rst.get("style", "unknown")
    active_val = "1'b0" if active == "low" else "1'b1"
    inactive_val = "1'b1" if active == "low" else "1'b0"

    if clk and style == "async":
        return """  task automatic apply_reset();
    begin
      {rst_name} = {inactive_val};
      #1;
      {rst_name} = {active_val};
      #7;
      {rst_name} = {inactive_val};
      repeat (2) @(posedge {clk});
    end
  endtask""".format(rst_name=rst_name, active_val=active_val, inactive_val=inactive_val, clk=clk)

    if clk:
        return """  task automatic apply_reset();
    begin
      {rst_name} = {active_val};
      repeat (2) @(posedge {clk});
      {rst_name} = {inactive_val};
      repeat (2) @(posedge {clk});
    end
  endtask""".format(rst_name=rst_name, active_val=active_val, inactive_val=inactive_val, clk=clk)

    return """  task automatic apply_reset();
    begin
      {rst_name} = {active_val};
      #10;
      {rst_name} = {inactive_val};
      #10;
    end
  endtask""".format(rst_name=rst_name, active_val=active_val, inactive_val=inactive_val)

def build_init_block(dut_spec):
    clk = get_single_clock(dut_spec)
    rst = get_single_reset(dut_spec)
    lines = ["  task automatic init_inputs();", "    begin"]

    for p in dut_spec["ports"]:
        if p["dir"] != "input":
            continue
        if clk and p["name"] == clk:
            continue
        if rst and p["name"] == rst["name"]:
            inactive_val = "1'b1" if rst.get("active") == "low" else "1'b0"
            lines.append("      {} = {};".format(p["name"], inactive_val))
        else:
            lines.append("      {} = {};".format(p["name"], sv_literal(0, port_width(p))))

    lines += ["    end", "  endtask"]
    return "\n".join(lines)

def build_output_check_tasks(dut_spec):
    out_ports = dut_spec.get("output_ports", [])
    if not out_ports:
        return """  task automatic check_outputs_not_unknown(input [1023:0] context);
    begin
      pass_cnt = pass_cnt + 1;
    end
  endtask"""

    lines = [
        "  task automatic check_outputs_not_unknown(input [1023:0] context);",
        "    begin",
    ]
    for p in out_ports:
        name = p["name"]
        lines.append("      if ((^{}) === 1'bx) begin".format(name))
        lines.append("        fail_cnt = fail_cnt + 1;")
        lines.append('        $display("[TV][FAIL] %0s : output {} is X/Z at time %0t", context, $time);'.format(name))
        lines.append("      end else begin")
        lines.append("        pass_cnt = pass_cnt + 1;")
        lines.append("      end")
    lines += ["    end", "  endtask"]
    return "\n".join(lines)

def build_sample_outputs_task(dut_spec):
    out_ports = dut_spec.get("output_ports", [])
    if not out_ports:
        return """  task automatic sample_outputs(input [1023:0] label);
    begin
      $display("[TV][OBSERVE] %0s at t=%0t", label, $time);
    end
  endtask"""

    fmt_parts = ["{}=%0d".format(p["name"]) for p in out_ports]
    arg_parts = [p["name"] for p in out_ports]
    fmt = " ".join(fmt_parts)
    args = ", ".join(arg_parts)
    return """  task automatic sample_outputs(input [1023:0] label);
    begin
      $display("[TV][OBSERVE] %0s t=%0t {fmt}", label, $time, {args});
    end
  endtask""".format(fmt=fmt, args=args)

def build_wait_cycles_snippet(dut_spec, count_expr):
    clk = get_single_clock(dut_spec)
    if clk:
        return "repeat ({}) @(posedge {});".format(count_expr, clk)
    return "#{};".format(int(count_expr) * 10)

def build_directed_sequence_block(plan, dut_spec):
    controllable = dict((p["name"], p) for p in dut_spec.get("input_ports", []))
    lines = []
    test_idx = 0

    for test in plan.get("directed_tests", []):
        tname = sanitize_identifier(test.get("name", "directed_{}".format(test_idx)))
        desc = test.get("description", "")
        lines.append('    $display("\\n[TV] START DIRECTED TEST: {} -- {}");'.format(tname, desc))

        for step_idx, step in enumerate(test.get("steps", [])):
            drive = step.get("drive", {})
            hold_cycles = int(step.get("hold_cycles", 1))
            hold_cycles = max(1, hold_cycles)

            for sig, val in drive.items():
                if sig not in controllable:
                    continue
                width = port_width(controllable[sig])
                lines.append("    {} = {};".format(sig, sv_literal(val, width)))

            lines.append('    sample_outputs("before_{}_{}");'.format(tname, step_idx))
            lines.append("    {}".format(build_wait_cycles_snippet(dut_spec, hold_cycles)))
            lines.append('    check_outputs_not_unknown("after_{}_{}");'.format(tname, step_idx))
            lines.append('    sample_outputs("after_{}_{}");'.format(tname, step_idx))

        test_idx += 1

    return "\n".join(lines)

def build_random_sequence_block(plan, dut_spec):
    controllable = dut_spec.get("input_ports", [])
    lines = []

    if not controllable:
        for i, test in enumerate(plan.get("random_tests", [])):
            num_txn = max(1, int(test.get("num_transactions", 10)))
            name = sanitize_identifier(test.get("name", "random_{}".format(i)))
            lines.append('    $display("\\n[TV] START RANDOM TEST: {} (observation-only)");'.format(name))
            lines.append("    {}".format(build_wait_cycles_snippet(dut_spec, num_txn)))
            lines.append('    check_outputs_not_unknown("random_{}_observe");'.format(name))
            lines.append('    sample_outputs("random_{}_observe");'.format(name))
        return "\n".join(lines)

    for i, test in enumerate(plan.get("random_tests", [])):
        num_txn = max(1, int(test.get("num_transactions", 10)))
        name = sanitize_identifier(test.get("name", "random_{}".format(i)))

        lines.append('    $display("\\n[TV] START RANDOM TEST: {} ({} transactions)");'.format(name, num_txn))
        lines.append("    for (int txn_{i} = 0; txn_{i} < {n}; txn_{i}++) begin".format(i=i, n=num_txn))
        for p in controllable:
            w = port_width(p)
            maxv = max_unsigned(w)
            if maxv <= 1:
                lines.append("      {} = $urandom_range(0, 1);".format(p["name"]))
            else:
                lines.append("      {} = $urandom_range(0, {});".format(p["name"], maxv))
        lines.append('      sample_outputs("random_{}_before");'.format(name))
        lines.append("      {}".format(build_wait_cycles_snippet(dut_spec, 1)))
        lines.append('      check_outputs_not_unknown("random_{}_after");'.format(name))
        lines.append('      sample_outputs("random_{}_after");'.format(name))
        lines.append("    end")

    return "\n".join(lines)

def build_vector_tb(dut_spec, plan):
    decls = build_signal_decls(dut_spec)
    inst = build_dut_instantiation(dut_spec)
    clk_gen = build_clock_gen(dut_spec)
    reset_task = build_reset_task(dut_spec)
    init_task = build_init_block(dut_spec)
    out_check_task = build_output_check_tasks(dut_spec)
    sample_task = build_sample_outputs_task(dut_spec)
    directed_block = build_directed_sequence_block(plan, dut_spec)
    random_block = build_random_sequence_block(plan, dut_spec)

    assumptions = "\n".join(["// - {}".format(a) for a in plan.get("assumptions", [])])

    return """module tv_tb_top;

{decls}

  integer pass_cnt = 0;
  integer fail_cnt = 0;

{inst}

{clk_gen}

{init_task}

{reset_task}

{out_check_task}

{sample_task}

  initial begin
    $display("==============================================");
    $display("VECTOR TEST START : {module_name}");
    $display("Design summary    : {design_summary}");
    $display("==============================================");
{assumptions}
    init_inputs();
    apply_reset();

{directed_block}

{random_block}

    $display("\\n==============================================");
    $display("VECTOR TEST SUMMARY");
    $display("PASS=%0d FAIL=%0d", pass_cnt, fail_cnt);
    $display("==============================================");

    if (fail_cnt > 0) begin
      $fatal(1, "Vector testing failed.");
    end else begin
      $finish;
    end
  end

endmodule
""".format(
        decls=decls,
        inst=inst,
        clk_gen=clk_gen,
        init_task=init_task,
        reset_task=reset_task,
        out_check_task=out_check_task,
        sample_task=sample_task,
        module_name=dut_spec["module_name"],
        design_summary=plan.get("design_summary", ""),
        assumptions=assumptions if assumptions else "// no assumptions",
        directed_block=directed_block if directed_block else "    // No directed tests generated.",
        random_block=random_block if random_block else "    // No random tests generated."
    )

def save_plan(plan):
    path = os.path.join(BUILD_DIR, "vector_plan.json")
    write_text(path, json.dumps(plan, indent=2))
    return path


# ─────────────────────────────────────────────
# VCS
# ─────────────────────────────────────────────
def vcs_compile(rtl_files):
    compile_log = os.path.join(BUILD_DIR, "logs", "compile_tv.log")
    simv_path = os.path.join(BUILD_DIR, SIMV_NAME)
    tb_path = os.path.join(TB_ROOT, "tv_tb_top.sv")

    if not which("vcs"):
        msg = "ERROR: VCS executable not found in PATH."
        write_text(compile_log, msg + "\n")
        return False, compile_log, msg

    debug_flags = ["-debug_access+r+w-memcbk", "-debug_region+cell"] if ENABLE_DEBUG else []
    cmd = ["vcs", "-sverilog", "-full64"] + debug_flags + rtl_files + [tb_path, "-o", simv_path]

    log("VCS", "Compiling {} RTL file(s) + vector TB...".format(len(rtl_files)), "info")
    p = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
    )
    out, _ = p.communicate()

    write_text(compile_log, out)
    return p.returncode == 0, compile_log, out

def vcs_run():
    sim_log = os.path.join(BUILD_DIR, "logs", "sim_tv.log")
    simv_path = os.path.join(BUILD_DIR, SIMV_NAME)

    if not os.path.exists(simv_path):
        return False, sim_log, ""

    log("VCS", "Running vector simulation...", "info")
    p = subprocess.Popen(
        [simv_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
    )
    out, _ = p.communicate()

    write_text(sim_log, out)
    return p.returncode == 0, sim_log, out

def extract_error_lines(text, max_lines=40):
    lines = []
    for ln in text.splitlines():
        low = ln.lower()
        if "Error-" in ln or "error" in low or "fatal" in low:
            lines.append(ln)
        if len(lines) >= max_lines:
            break
    return "\n".join(lines).strip()

def extract_vector_summary(sim_out):
    m = re.search(r"PASS=(\d+)\s+FAIL=(\d+)", sim_out)
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2))


# ─────────────────────────────────────────────
# MAIN FLOW
# ─────────────────────────────────────────────
def resolve_inputs():
    val = load_validation_result()

    if len(sys.argv) > 1:
        rtl_files = collect_rtl_files(sys.argv[1:])
        if not val:
            raise RuntimeError("validation_result.json is required so Agent 3 can use dut_spec/top module metadata.")
    else:
        if not val:
            raise RuntimeError("Missing {}. Run Agent 1 first.".format(VALIDATION_RESULT_FILE))
        rtl_files = val.get("rtl_files", [])

    if not val:
        raise RuntimeError("Missing {}.".format(VALIDATION_RESULT_FILE))
    if not val.get("is_valid"):
        raise RuntimeError("Agent 1 marked RTL invalid: {}".format(val.get("summary", "")))
    if not rtl_files:
        raise RuntimeError("No rtl_files found.")
    if not val.get("top_module_file"):
        raise RuntimeError("No top_module_file found in validation_result.json")
    if not val.get("dut_spec"):
        raise RuntimeError("No dut_spec found in validation_result.json")

    return rtl_files, val["top_module_file"], val["dut_spec"]

def print_plan_summary(plan):
    section("VECTOR PLAN")
    print("  {} {}".format(c("bold", "Design:"), plan.get("design_summary", "?")))
    print("  {} {}".format(c("bold", "Directed tests:"), len(plan.get("directed_tests", []))))
    for t in plan.get("directed_tests", [])[:8]:
        print("    {} {} — {}".format(c("cyan", "▸"), t.get("name", "?"), t.get("description", "")))
    print("  {} {}".format(c("bold", "Random tests:"), len(plan.get("random_tests", []))))
    for t in plan.get("random_tests", [])[:8]:
        print("    {} {} — {}".format(c("yellow", "▸"), t.get("name", "?"), t.get("description", "")))

def main():
    banner("AGENT 3 — TEST VECTORS", "yellow")

    try:
        rtl_files, top_module_file, dut_spec = resolve_inputs()
    except Exception as e:
        log("TV_AGENT", str(e), "error")
        sys.exit(1)

    ensure_build_dirs()

    rtl_code = read_file(top_module_file)
    log("TV_AGENT", "Top module: {}".format(dut_spec["module_name"]), "ok")
    log("TV_AGENT", "Top file: {}".format(top_module_file), "ok")
    log("TV_AGENT", "RTL files: {}".format(len(rtl_files)), "info")

    plan = get_vector_plan(rtl_code, dut_spec)
    plan_path = save_plan(plan)
    print_plan_summary(plan)
    log("TV_AGENT", "Saved vector plan -> {}".format(plan_path), "muted")

    tb_code = build_vector_tb(dut_spec, plan)
    tb_path = os.path.join(TB_ROOT, "tv_tb_top.sv")
    write_text(tb_path, tb_code)
    log("TV_AGENT", "Wrote vector TB -> {}".format(tb_path), "ok")

    ok_compile, compile_log, compile_out = vcs_compile(rtl_files)
    log("VCS", "Compile {} -> {}".format("PASS" if ok_compile else "FAIL", compile_log), "ok" if ok_compile else "warn")

    if not ok_compile:
        errs = extract_error_lines(compile_out)
        if errs:
            print(c("yellow", "\n── Compile Errors ──"))
            print(errs)
        sys.exit(1)

    ok_run, sim_log, sim_out = vcs_run()
    log("VCS", "Simulation {} -> {}".format("PASS" if ok_run else "FAIL", sim_log), "ok" if ok_run else "warn")

    pass_cnt, fail_cnt = extract_vector_summary(sim_out)

    section("VECTOR RESULTS")
    if pass_cnt is not None:
        print("  PASS = {}".format(c("green", str(pass_cnt))))
        print("  FAIL = {}".format(c("red" if fail_cnt else "green", str(fail_cnt))))
    else:
        print("  {}".format(c("yellow", "Could not parse PASS/FAIL summary from sim output.")))

    if not ok_run:
        errs = extract_error_lines(sim_out)
        if errs:
            print(c("yellow", "\n── Simulation Errors ──"))
            print(errs)
        sys.exit(1)

    if fail_cnt and fail_cnt > 0:
        print("\n{}\n".format(c("red", "[TV_AGENT] FAIL — Vector testing found issues.")))
        sys.exit(1)

    print("\n{}\n".format(c("green", "[TV_AGENT] PASS — Vector testing completed successfully.")))
    sys.exit(0)

if __name__ == "__main__":
    main()