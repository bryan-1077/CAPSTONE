#!/usr/bin/env python3
"""
agent3_testvectors.py
=====================
Generic Agent 3: Test Vector Generator

Role in pipeline:
- Reads validation_result.json from Agent 1
- Uses DUT spec and top module metadata
- Generates or accepts directed JSON transaction vectors
- Reuses the verified Agent 2 UVM environment and immutable checker logic
- Compiles the sequence extension with VCS and reports per-vector evidence
"""

import os
import re
import sys
import json
import shutil
import subprocess
import requests
from dotenv import load_dotenv
from run_pipeline import run_reported_agent

VALIDATION_RESULT_FILE = os.environ.get('RTL_VALIDATION_RESULT_FILE', 'validation_result.json')
ACTIVE_REPORT = None
LAST_PLAN_RESPONSE = {}
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
            for root, directories, files in os.walk(path):
                directories[:] = [d for d in directories if d != 'verification_reports']
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
    global LAST_PLAN_RESPONSE
    LAST_PLAN_RESPONSE = {}
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
        "max_tokens": int(os.environ.get("AGENT3_MAX_TOKENS", "8192")),
    }

    resp = requests.post(base_url, headers=headers, data=json.dumps(payload), timeout=180)
    if resp.status_code != 200:
        raise RuntimeError("LLM HTTP {}: {}".format(resp.status_code, resp.text[:400]))

    try:
        choice = resp.json()['choices'][0]
        LAST_PLAN_RESPONSE = {'finish_reason': choice.get('finish_reason'), 'max_tokens': payload['max_tokens']}
        message = choice['message']['content']
        if isinstance(message, str):
            return message.strip()
    except (ValueError, KeyError, TypeError, IndexError):
        pass
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
            choice = data["choices"][0]
            if choice.get("finish_reason"):
                LAST_PLAN_RESPONSE = {"finish_reason": choice["finish_reason"], "max_tokens": payload["max_tokens"]}
            delta = choice.get("delta", {})
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
                if ACTIVE_REPORT is not None:
                    ACTIVE_REPORT.update(plan_source='llm', plan_attempts=attempt + 1)
                return obj
        except Exception as e:
            log(agent, "Plan parse failed on attempt {}: {}".format(attempt + 1, e), "warn")
            if ACTIVE_REPORT is not None:
                errors = ACTIVE_REPORT.data.get('plan_errors', []) + [str(e)]
                ACTIVE_REPORT.update(plan_errors=errors)

    log(agent, "Falling back to default generated plan.", "warn")
    if ACTIVE_REPORT is not None:
        ACTIVE_REPORT.update(plan_source='fallback', plan_attempts=MAX_LLM_RETRIES)
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
        lines.append('    test_start_pass = pass_cnt; test_start_fail = fail_cnt;')
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

        lines.append('    $display("AGENT3_TEST|directed|{}|{}|%0d|%0d", pass_cnt-test_start_pass, fail_cnt-test_start_fail);'.format(test_idx, tname))
        test_idx += 1

    return "\n".join(lines)

def build_random_sequence_block(plan, dut_spec):
    controllable = dut_spec.get("input_ports", [])
    lines = []

    if not controllable:
        for i, test in enumerate(plan.get("random_tests", [])):
            num_txn = max(1, int(test.get("num_transactions", 10)))
            name = sanitize_identifier(test.get("name", "random_{}".format(i)))
            lines.append('    test_start_pass = pass_cnt; test_start_fail = fail_cnt;')
            lines.append('    $display("\\n[TV] START RANDOM TEST: {} (observation-only)");'.format(name))
            lines.append("    {}".format(build_wait_cycles_snippet(dut_spec, num_txn)))
            lines.append('    check_outputs_not_unknown("random_{}_observe");'.format(name))
            lines.append('    sample_outputs("random_{}_observe");'.format(name))
            lines.append('    $display("AGENT3_TEST|random|{}|{}|%0d|%0d", pass_cnt-test_start_pass, fail_cnt-test_start_fail);'.format(i, name))
        return "\n".join(lines)

    for i, test in enumerate(plan.get("random_tests", [])):
        num_txn = max(1, int(test.get("num_transactions", 10)))
        name = sanitize_identifier(test.get("name", "random_{}".format(i)))
        lines.append('    test_start_pass = pass_cnt; test_start_fail = fail_cnt;')

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
        lines.append('    $display("AGENT3_TEST|random|{}|{}|%0d|%0d", pass_cnt-test_start_pass, fail_cnt-test_start_fail);'.format(i, name))

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
  integer test_start_pass = 0;
  integer test_start_fail = 0;

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
    include_flags = ['+incdir+' + directory for directory in sorted({os.path.dirname(os.path.abspath(p)) for p in rtl_files})]
    cmd = ["vcs", "-sverilog", "-full64", "-top", "tv_tb_top"] + debug_flags + include_flags + rtl_files + [tb_path, "-o", simv_path]

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


def build_vector_report(plan, dut_spec, sim_out, simulation_ok):
    passed, failed = extract_vector_summary(sim_out)
    observed = {}
    for match in re.finditer(r'AGENT3_TEST\|(directed|random)\|(\d+)\|([^|\r\n]+)\|(\d+)\|(\d+)', sim_out):
        kind, index, name, checks_passed, checks_failed = match.groups()
        key = (kind, int(index))
        previous = observed.get(key, (name, 0, 0))
        observed[key] = (name, max(previous[1], int(checks_passed)), max(previous[2], int(checks_failed)))
    tests = []
    outputs = [p['name'] for p in dut_spec.get('output_ports', [])]
    for kind in ('directed', 'random'):
        for index, test in enumerate(plan.get(kind + '_tests', [])):
            name = sanitize_identifier(test.get('name', '{}_{}'.format(kind, index)))
            actual_name, count_passed, count_failed = observed.get((kind, index), (None, 0, 0))
            if count_failed:
                status = 'FAIL'
            elif actual_name == name and count_passed > 0 and outputs:
                status = 'PASS'
            else:
                status = 'NOT_TESTED'
            tests.append({'id': '{}-{}'.format(kind, index + 1), 'name': name,
                          'kind': kind, 'description': test.get('description', ''),
                          'status': status, 'passed_checks': count_passed, 'failed_checks': count_failed,
                          'expected_behavior': 'Sampled DUT outputs contain no X/Z bits.',
                          'observed_outputs': outputs,
                          'evidence': ('Completed vector check marker was recorded.' if status == 'PASS' else
                                       'Vector checks failed.' if status == 'FAIL' else
                                       'No completed checks of real DUT outputs were recorded.')})
    errors = [line for line in sim_out.splitlines() if '[TV][FAIL]' in line or '$fatal' in line or
              re.match(r'^\s*(?:Fatal|Error)', line)]
    if simulation_ok is False or (failed or 0) > 0 or errors or any(t['status'] == 'FAIL' for t in tests):
        status = 'FAIL'
    elif passed is None or passed <= 0 or not outputs or not tests:
        status = 'NOT_TESTED'
    else:
        # Agent 3 currently checks knownness, not YAML-predicted values/timing.
        status = 'PASS_WITH_GAPS'
    return {'verification_status': status, 'tests': tests,
            'failed_tests': [t for t in tests if t['status'] == 'FAIL'],
            'untested_tests': [t for t in tests if t['status'] == 'NOT_TESTED'],
            'checks': {'passed': passed, 'failed': failed},
            'simulation_completed': simulation_ok is True,
            'simulation_errors': errors[:40], 'verification_scope': 'output_knownness_only',
            'uvm_environment_reused': False, 'jedec_timing_verified': False,
            'coverage_limitations': [
                'Vector results check outputs for X/Z; they do not compare functional results against the YAML.',
                'No timing assertions were executed by Agent 3; these results do not establish JEDEC compliance.']}


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

# Directed vectors execute inside the frozen Agent 2 environment. The legacy
# standalone builders above remain import-compatible but are not the pipeline path.
def load_uvm_handoff(path, rtl_files, dut_spec, spec_path):
    import agent2_uvm_tb as uvm
    import yaml
    manifest = json.loads(read_file(path))
    if manifest.get('dut_top_module') != dut_spec['module_name']:
        raise ValueError('Agent 2 UVM manifest belongs to another DUT. Run Agent 2 for these inputs.')
    if manifest.get('overall_status') not in ('PASS', 'PASS_WITH_GAPS'):
        raise ValueError('Agent 2 must complete without verification failures before vector extension.')
    expected = {os.path.realpath(p) for p in rtl_files}
    if expected != {os.path.realpath(r['path']) for r in manifest['rtl_files']}:
        raise ValueError('Agent 2 manifest RTL file set differs from current inputs.')
    for row in manifest['rtl_files'] + manifest['testbench_files']:
        if uvm.file_sha256(row['path']) != row['sha256']:
            raise ValueError('Agent 2 artifact changed since verification: ' + row['path'])
    spec = yaml.safe_load(read_file(spec_path))
    if spec != yaml.safe_load(read_file(manifest['approved_yaml'])):
        raise ValueError('YAML changed since Agent 2 verification; rerun Agent 2.')
    files = [(os.path.basename(r['path']), read_file(r['path'])) for r in manifest['testbench_files']]
    if set(n for n, _ in files) != set(uvm.COMPILE_ORDER):
        raise ValueError('Agent 2 handoff must include the complete three-file UVM environment.')
    if manifest.get('requirement_report'):
        manifest['baseline_requirement_report'] = json.loads(read_file(manifest['requirement_report']))
    return manifest, files, spec


def vector_transaction_contract(files):
    import agent2_uvm_tb as uvm
    sources = dict(files)
    code = uvm._sv_without_comments(sources['dut_pkg.sv'])
    sequence = re.search(r'\bclass\s+(dut_sequence)\s+extends\s+uvm_sequence\s*#\s*\(\s*(\w+)\s*\)', code)
    driver = re.search(r'\bclass\s+dut_driver\b.*?\bendclass\b', code, re.S)
    if not sequence or not driver:
        raise ValueError('Saved UVM environment needs dut_sequence and dut_driver with an explicit transaction type.')
    item = sequence.group(2)
    declarations = re.findall(r'\b' + re.escape(item) + r'\s+(\w+)\s*;', driver.group())
    bindings = {}
    for variable in declarations:
        for assignment in re.finditer(r'\b(\w+(?:\.\w+){1,2})\s*(?:<=|=(?!=))\s*([^;]+);', driver.group()):
            for field in re.findall(r'\b' + re.escape(variable) + r'\.(\w+)\b', assignment.group(2)):
                bindings.setdefault(field, []).append(assignment.group().strip())
    if not bindings:
        raise ValueError('Cannot identify transaction fields consumed by the saved UVM driver.')
    parent = 'dut_sequence'
    overrides = re.findall(r'dut_sequence::type_id::set_type_override\(\s*(\w+)::get_type\(\)', sources['dut_tb_top.sv'])
    if overrides:
        parent = overrides[-1]
    widths = {}
    item_class = re.search(r'\bclass\s+' + re.escape(item) + r'\b.*?\bendclass\b', code, re.S)
    if item_class:
        for field in bindings:
            decl = re.search(r'\b(logic|bit|int|integer|byte|shortint|longint)\s+(?:unsigned\s+|signed\s+)?'
                             r'(?:\[\s*(\d+)\s*:\s*(\d+)\s*\]\s*)?' + re.escape(field) + r'\s*[;=,]', item_class.group())
            if decl:
                widths[field] = (abs(int(decl.group(2)) - int(decl.group(3))) + 1 if decl.group(2) else
                                 {'logic':1, 'bit':1, 'int':32, 'integer':32, 'byte':8, 'shortint':16, 'longint':64}[decl.group(1)])
    return {'item_type': item, 'sequence_parent': parent, 'fields': bindings, 'field_widths': widths}


def validate_directed_plan(plan, contract, requirements):
    if not isinstance(plan, dict) or not isinstance(plan.get('directed_tests'), list) or not plan['directed_tests']:
        raise ValueError('A nonempty directed_tests list is required.')
    if plan.get('random_tests'):
        raise ValueError('This Agent 3 path accepts directed vectors, not random tests.')
    ids = {r['id'] for r in requirements if r.get('verification_method') == 'simulation'}
    names = set()
    total = 0
    for test in plan['directed_tests']:
        if not isinstance(test, dict):
            raise ValueError('Each directed test must be an object.')
        name = test.get('name', '')
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z_]\w*', name) or name in names:
            raise ValueError('Vector names must be unique identifiers.')
        names.add(name)
        targets = test.get('requirement_ids')
        if not isinstance(targets, list) or not targets or any(not isinstance(r, str) or r not in ids for r in targets):
            raise ValueError('Each vector must target existing simulation requirement IDs.')
        if not isinstance(test.get('description'), str) or not test['description'].strip():
            raise ValueError('Each vector needs a description of the scenario being exercised.')
        if not isinstance(test.get('steps'), list) or not test['steps']:
            raise ValueError('Each vector needs steps.')
        for step in test['steps']:
            if not isinstance(step, dict) or not isinstance(step.get('fields'), dict):
                raise ValueError('Each step needs a fields object using the UVM transaction contract.')
            if set(step['fields']) != set(contract['fields']):
                raise ValueError('Every step must specify exactly these transaction fields: ' + ', '.join(sorted(contract['fields'])))
            for field, value in step['fields'].items():
                if type(value) is not int or value < 0 or value.bit_length() > 65536:
                    raise ValueError('Transaction values must be nonnegative integers of at most 65536 bits.')
                width = contract.get('field_widths', {}).get(field)
                if width is not None and value.bit_length() > width:
                    raise ValueError('Test {} field {} value {} exceeds {} bits (maximum {}).'.format(
                        name, field, value, width, (1 << width) - 1))
            cycles = step.get('hold_cycles', 1)
            if type(cycles) is not int or not 1 <= cycles <= 100000:
                raise ValueError('hold_cycles must be an integer from 1 to 100000.')
            total += cycles
    if total > 1000000:
        raise ValueError('Plan exceeds one million transactions; split it into smaller runs.')
    return plan


DIRECTED_UVM_SYSTEM = '''Return only JSON describing directed UVM test vectors.
Schema: {"directed_tests":[{"name":"identifier","description":"scenario and boundary",
"requirement_ids":["REQ-001"],"steps":[{"fields":{"transaction_field":0},"hold_cycles":1}]}]}.
Use exactly the supplied transaction fields in EVERY step, with nonnegative integer values.
These are sequence-item fields, not necessarily DUT port names. Read driver assignments
for reset polarity, encoding and widths. Never invent fields or use random expressions.
Each hold cycle sends one transaction; repeated commands remain asserted for every cycle.
Tests replace the baseline stimulus and run in listed order after driver startup reset. Establish known state with
legal reset items where supported, and end each test with sufficient idle/drain items.
Target YAML requirements with normal, simultaneous, reset and boundary scenarios. For
specified timing constraints use before/at/after-boundary cases with legal prerequisites.
Do not infer missing timing numbers from RTL or claim unspecified JEDEC compliance.
The existing UVM scoreboard supplies expected behavior; you cannot change it.
Include no random_tests. Use enough directed cases to cover the requested requirements.
'''


def get_directed_uvm_plan(files, manifest, spec, contract, supplied_path=None):
    requirements = manifest['requirements']
    if supplied_path:
        return validate_directed_plan(json.loads(read_file(supplied_path)), contract, requirements)
    # Bound each response independently of the total number of requirements.
    # Every runtime requirement is assigned a group; static checks stay in Agent 1/2.
    runtime = [r for r in requirements if r.get('verification_method') == 'simulation']
    if not runtime:
        raise ValueError('No runtime requirements are available for directed vector generation.')
    result = {'directed_tests': []}
    context = json.dumps({'yaml': spec, 'transaction_contract': contract}, indent=2)
    context += '\nSaved UVM source (read-only):\n' + '\n'.join(n + '\n' + s for n, s in files)
    for group_index, offset in enumerate(range(0, len(runtime), 3)):
        group = runtime[offset:offset + 3]
        target_ids = {r['id'] for r in group}
        log('TV_PLAN_AGENT', 'Generating directed vector group {}/{} for {}'.format(
            group_index + 1, (len(runtime) + 2) // 3, ', '.join(sorted(target_ids))))
        prompt = context + '\nTARGET REQUIREMENTS FOR THIS RESPONSE ONLY:\n' + json.dumps(group, indent=2)
        prompt += ("\nReturn one or two compact directed_tests covering these target IDs. "
                   "Use at most 24 steps per test; use hold_cycles for repeated cycles. "
                   "Do not repeat the entire requirement catalog. Omit reasoning/prose. "
                   "Honor field_widths; never clamp or wrap oversized values. "
                   "Establish the required starting state; prior tests may have left any legal state.\n")
        prior = ''
        for attempt in range(MAX_LLM_RETRIES):
            try:
                LAST_PLAN_RESPONSE.clear()
                raw = call_llm(DIRECTED_UVM_SYSTEM, prompt + prior, 'TV_PLAN_AGENT')
                if ACTIVE_REPORT:
                    ACTIVE_REPORT.artifact('vector_plan_group_{}_response_{}.txt'.format(group_index, attempt), text=raw)
                if LAST_PLAN_RESPONSE.get('finish_reason') in ('length', 'max_tokens'):
                    raise ValueError('Model response truncated at its token limit; return a smaller compact group plan.')
                clean = re.sub(r'<think>.*?</think>', '', raw, flags=re.S).strip()
                clean = re.sub(r'^```(?:json)?\s*|\s*```$', '', clean)
                partial = validate_directed_plan(json.loads(clean), contract, requirements)
                covered = {rid for test in partial['directed_tests'] for rid in test['requirement_ids']}
                if not target_ids <= covered:
                    raise ValueError('Plan omitted target IDs: ' + ', '.join(sorted(target_ids - covered)))
                for test in partial['directed_tests']:
                    test = dict(test)
                    test['name'] = 'group_{}_{}'.format(group_index + 1, test['name'])
                    result['directed_tests'].append(test)
                if ACTIVE_REPORT:
                    ACTIVE_REPORT.artifact('partial_vector_plan.json', data=result)
                    ACTIVE_REPORT.update(plan_groups_completed=group_index + 1, plan_groups_total=(len(runtime) + 2) // 3)
                break
            except (ValueError, TypeError) as exc:
                detail = 'Group {} attempt {}: {}'.format(group_index + 1, attempt + 1, exc)
                log('TV_PLAN_AGENT', detail, 'warn')
                if ACTIVE_REPORT:
                    ACTIVE_REPORT.update(plan_errors=ACTIVE_REPORT.data.get('plan_errors', []) + [detail])
                    ACTIVE_REPORT.artifact('partial_vector_plan.json', data=result)
                prior = '\nPrevious response rejected: {}. Correct this group only.\n'.format(exc)
        else:
            raise ValueError('Directed vector generation failed for {} after {} attempts. Last error: {}'.format(
                ', '.join(sorted(target_ids)), MAX_LLM_RETRIES, prior.strip()))
    return validate_directed_plan(result, contract, requirements)


def build_uvm_vector_sources(files, plan, contract):
    import agent2_uvm_tb as uvm
    sources = dict(files)
    package = sources['dut_pkg.sv']
    # Add observational logging around the Python-owned checker wrapper only.
    blocks = list(re.finditer(r'// AGENT2_EVIDENCE_BEGIN.*?// AGENT2_EVIDENCE_END[^\n]*', package, re.S))
    if not blocks:
        raise ValueError('Saved UVM environment lacks canonical requirement evidence; regenerate with Agent 2.')
    for block in reversed(blocks):
        text = block.group()
        signatures, _ = uvm._requirement_check_signatures(uvm._sv_without_comments(text))
        signature = next((s for s in signatures if len(s['parameters']) == 5), None)
        if not signature:
            raise ValueError('Agent 3 needs the five-argument canonical requirement checker.')
        names = [n for n, _ in signature['parameters']]
        def role(options):
            selected = [n for n in names if n in options]
            if len(selected) != 1:
                raise ValueError('Unsupported canonical evidence argument names.')
            return selected[0]
        rid = role(('id', 'req_id', 'requirement_id', 'rid'))
        expected = role(('expected', 'expected_str', 'exp_str', 'expected_value'))
        observed = role(('observed', 'observed_str', 'obs_str', 'actual_str', 'observed_value'))
        scenario = role(('scenario', 'scenario_str', 'scenario_description'))
        condition = names[signature['condition_index']]
        observation = '''if (agent3_vector_index >= 0)
        $display("AGENT3_OBS|%0d|%0d|%s|%0d|%0t|%s|%s|%s", agent3_vector_index,
            agent3_step_index, RID, (CONDITION === 1'b1), $time,
            _agent2_evidence_field(SCENARIO), _agent2_evidence_field(EXPECTED),
            _agent2_evidence_field(OBSERVED));
    '''
        for key, value in (('RID', rid), ('CONDITION', condition), ('SCENARIO', scenario), ('EXPECTED', expected), ('OBSERVED', observed)):
            observation = observation.replace(key, value)
        call = text.rfind('    _agent2_generated_check_requirement(')
        if call < 0:
            raise ValueError('Canonical requirement wrapper call missing.')
        text = text[:call] + observation + text[call:]
        package = package[:block.start()] + text + package[block.end():]
    package = re.sub(r'(\bpackage\s+dut_pkg\s*;)', r'\1\nint agent3_vector_index = -1;\nint agent3_step_index = -1;\nint agent3_pending_vector = -1;\nint agent3_pending_step = -1;', package, count=1)
    # Tag observations only after the driver's drive event, never during startup
    # or while the first item is merely waiting for the next drive edge.
    driver = re.search(r'\bclass\s+dut_driver\b.*?\bendclass\b', uvm._sv_without_comments(package), re.S)
    gets = list(re.finditer(r'\bseq_item_port\.get_next_item\([^;]*\)\s*;', driver.group())) if driver else []
    if len(gets) != 1:
        raise ValueError('Directed vector attribution requires one get_next_item site in dut_driver.')
    remainder = driver.group()[gets[0].end():]
    wait = re.search(r'@\s*(?:\([^;]+\)|[\w.]+)\s*;', remainder)
    done = re.search(r'\bseq_item_port\.item_done\s*\(', remainder)
    if not wait or not done or wait.end() > done.start():
        raise ValueError('Directed vectors require a driver clock event before item_done.')
    offset = driver.start() + gets[0].end() + wait.end()
    package = package[:offset] + ('\nagent3_vector_index = agent3_pending_vector;\n'
                                  'agent3_step_index = agent3_pending_step;\n') + package[offset:]
    lines = ['class agent3_directed_sequence extends {};'.format(contract['sequence_parent']),
             '`uvm_object_utils(agent3_directed_sequence)',
             'function new(string name="agent3_directed_sequence"); super.new(name); endfunction',
             'task body();', '{} req;'.format(contract['item_type']),
             'agent3_vector_index = -1;']
    for index, test in enumerate(plan['directed_tests']):
        lines += ['agent3_vector_index = -1;', 'agent3_pending_vector = {};'.format(index)]
        for number, step in enumerate(test['steps']):
            lines += ['agent3_pending_step = {};'.format(number), 'repeat ({}) begin'.format(step.get('hold_cycles', 1)),
                      'req = {}::type_id::create("agent3_item");'.format(contract['item_type']), 'start_item(req);',
                      '$display("AGENT3_BEGIN|{}");'.format(index)]
            for field, value in sorted(step['fields'].items()):
                bits = max(1, value.bit_length())
                lines += ['if ($bits(req.{}) < {}) `uvm_fatal("AGENT3_WIDTH", "Vector value exceeds {} width")'.format(field, bits, field),
                          "req.{} = {}'h{:x};".format(field, bits, value)]
            lines += ['finish_item(req);', 'uvm_wait_for_nba_region();', 'end']
        lines += ['$display("AGENT3_END|{}");'.format(index), 'agent3_vector_index = -1;', 'agent3_pending_vector = -1;']
    lines += ['agent3_step_index = -1;', 'endtask', 'endclass']
    package = package.replace('endpackage', '\n'.join(lines) + '\nendpackage')
    top = sources['dut_tb_top.sv']
    starts = list(re.finditer(r'\brun_test\s*\(', uvm._sv_without_comments(top)))
    if len(starts) != 1:
        raise ValueError('Saved top must contain exactly one run_test call.')
    offset = starts[0].start()
    top = top[:offset] + 'dut_sequence::type_id::set_type_override(agent3_directed_sequence::get_type());\n' + top[offset:]
    sources['dut_pkg.sv'], sources['dut_tb_top.sv'] = package, top
    return [(name, sources[name]) for name in uvm.COMPILE_ORDER]


def combine_uvm_requirement_reports(original, directed):
    """Retain baseline coverage; neither stage can erase the other's failures."""
    if not original:
        return directed
    merged = dict(directed)
    previous = {r['id']: r for r in original.get('requirements', [])}
    rows = []
    for current in directed['requirements']:
        before = previous.get(current['id'], {})
        row = dict(current)
        states = (before.get('status'), current['status'])
        row['status'] = 'FAIL' if 'FAIL' in states else 'PASS' if 'PASS' in states else 'NOT_TESTED'
        row['checks'] = before.get('checks', 0) + current.get('checks', 0)
        row['failures'] = before.get('failures', 0) + current.get('failures', 0)
        row['stage_results'] = {'agent2': before, 'agent3': current}
        row['comparison_evidence'] = before.get('comparison_evidence', []) + current.get('comparison_evidence', [])
        if row['status'] != 'NOT_TESTED':
            row['gap_kind'] = ''
            row['evidence_gap'] = ''
        if current['status'] == 'NOT_TESTED' and before.get('status') == 'PASS':
            row['evidence'] = before.get('evidence', '')
        rows.append(row)
    merged['requirements'] = rows
    merged['passed_requirements'] = sum(r['status'] == 'PASS' for r in rows)
    merged['failed_requirements'] = sum(r['status'] == 'FAIL' for r in rows)
    merged['not_tested_requirements'] = sum(r['status'] == 'NOT_TESTED' for r in rows)
    for method, prefix in (('simulation', 'simulation'), ('static', 'structural')):
        # Static rows can use different method labels; simulation is explicit.
        subset = [r for r in rows if (r.get('verification_method') == 'simulation') == (method == 'simulation')]
        merged[prefix + '_passed_requirements'] = sum(r['status'] == 'PASS' for r in subset)
        merged[prefix + '_gaps'] = sum(r['status'] == 'NOT_TESTED' for r in subset)
    merged['tested_requirements'] = sum(r.get('verification_method') == 'simulation' and r['checks'] > 0 for r in rows)
    for field in ('scoreboard_passed_checks', 'scoreboard_failed_checks', 'uvm_errors', 'uvm_fatals'):
        merged[field] = (original.get(field) or 0) + (directed.get(field) or 0)
    merged['overall_status'] = ('FAIL' if merged['failed_requirements'] or 'FAIL' in
                                (original.get('overall_status'), directed.get('overall_status')) else
                                'PASS_WITH_GAPS' if merged['not_tested_requirements'] else 'PASS')
    return merged


def build_directed_uvm_report(plan, manifest, sim_out, simulation_ok):
    import agent2_uvm_tb as uvm
    catalog = {r['id']: r for r in manifest['requirements']}
    begun = set(int(x) for x in re.findall(r'^AGENT3_BEGIN\|(\d+)$', sim_out, re.M))
    ended = set(int(x) for x in re.findall(r'^AGENT3_END\|(\d+)$', sim_out, re.M))
    vector_errors = {}
    active = None
    for line in sim_out.splitlines():
        begin = re.match(r'^AGENT3_BEGIN\|(\d+)$', line)
        end = re.match(r'^AGENT3_END\|(\d+)$', line)
        if begin:
            active = int(begin.group(1))
        elif end:
            active = None
        elif active is not None and re.match(r'^UVM_(?:ERROR|FATAL)\s+(?!:)', line):
            vector_errors.setdefault(active, []).append(line)
    evidence = {}
    pattern = r'^AGENT3_OBS\|(\d+)\|(\d+)\|([^|]+)\|([01])\|([^|]+)\|([^|]*)\|([^|]*)\|([^\r\n]*)$'
    for m in re.finditer(pattern, sim_out, re.M):
        index, step, rid, passed, time, scenario, expected, observed = m.groups()
        evidence.setdefault(int(index), []).append({'requirement_id': rid, 'step': int(step),
            'passed': passed == '1', 'time': time, 'scenario': scenario,
            'expected': expected, 'observed': observed})
    tests = []
    for index, test in enumerate(plan['directed_tests']):
        samples = evidence.get(index, [])
        missing = [rid for rid in test['requirement_ids'] if not any(e['requirement_id'] == rid for e in samples)]
        complete = index in begun and index in ended
        failures = sum(not e['passed'] for e in samples)
        status = 'FAIL' if failures or vector_errors.get(index) else 'PASS' if complete and not missing else 'NOT_TESTED'
        tests.append({'name': test['name'], 'kind': 'directed', 'description': test['description'],
            'requirement_ids': test['requirement_ids'], 'steps': test['steps'], 'status': status,
            'expected_behavior': {rid: catalog[rid]['text'] for rid in test['requirement_ids']},
            'verification_method': 'Saved Agent 2 UVM driver, monitor and scoreboard; actual checker observations during this vector.',
            'passed_checks': len(samples) - failures, 'failed_checks': failures,
            'untested_requirements': missing, 'completed': complete, 'evidence': samples,
            'uvm_messages': vector_errors.get(index, [])})
    errors, fatals = uvm.count_uvm_issues(sim_out)
    baseline = uvm.build_requirement_report(manifest['requirements'], sim_out, simulation_ok=simulation_ok)
    combined = combine_uvm_requirement_reports(manifest.get('baseline_requirement_report'), baseline)
    status = ('FAIL' if not simulation_ok or errors or fatals or combined['failed_requirements'] or
              any(not t['completed'] for t in tests) or
              any(t['status'] == 'FAIL' for t in tests) else
              'PASS_WITH_GAPS' if combined['not_tested_requirements'] or any(t['status'] != 'PASS' for t in tests) else 'PASS')
    return {'verification_status': status, 'uvm_environment_reused': True,
            'verification_scope': 'directed_vectors_with_saved_uvm_checkers',
            'simulation_timescale': manifest.get('timescale'),
            'tests': tests, 'failed_tests': [t for t in tests if t['status'] == 'FAIL'],
            'untested_tests': [t for t in tests if t['status'] == 'NOT_TESTED'],
            'uvm_errors': errors, 'uvm_fatals': fatals, 'simulation_completed': simulation_ok,
            'requirement_results': baseline, 'combined_requirement_results': combined, 'jedec_timing_verified': False,
            'coverage_limitations': [r['id'] + ': ' + r.get('evidence', 'No executed verification evidence.')
                                     for r in combined['requirements'] if r['status'] == 'NOT_TESTED'] +
                [t['name'] + ': target checks did not execute: ' + ', '.join(t['untested_requirements'])
                 for t in tests if t['untested_requirements']],
            'verification_notes': ['Only implemented UVM checks establish results. A requirement ID or vector description alone does not prove timing coverage.',
                'Full JEDEC compliance is not established; timing results are limited to the specified configuration and existing executable timing checks.']}


def run_directed_uvm(report, rtl_files, dut_spec):
    manifest_path = os.path.abspath(os.environ.get('AGENT3_UVM_MANIFEST', 'build_auto/uvm_environment.json'))
    validation = load_validation_result()
    report.step('uvm_handoff_validation')
    manifest, files, spec = load_uvm_handoff(manifest_path, rtl_files, dut_spec, validation['spec_file'])
    contract = vector_transaction_contract(files)
    report.artifact('uvm_handoff.json', data=manifest)
    report.artifact('approved_spec.yaml', source=manifest['approved_yaml'])
    report.update(uvm_environment_reused=True, uvm_manifest=manifest_path, transaction_contract=contract)
    report.step('directed_vector_plan')
    report.update(plan_source='supplied_file' if os.environ.get('AGENT3_VECTOR_PLAN_FILE') else 'llm')
    plan = get_directed_uvm_plan(files, manifest, spec, contract, os.environ.get('AGENT3_VECTOR_PLAN_FILE'))
    report.artifact('vector_plan.json', data=plan)
    save_plan(plan)
    report.step('uvm_vector_extension')
    extended = build_uvm_vector_sources(files, plan, contract)
    for name, code in extended:
        write_text(os.path.join(TB_ROOT, name), code)
        report.artifact('uvm_sources/' + name, text=code)
    paths = [os.path.abspath(os.path.join(TB_ROOT, n)) for n in ('dut_if.sv', 'dut_pkg.sv', 'dut_tb_top.sv')]
    binary = os.path.abspath(os.path.join(BUILD_DIR, SIMV_NAME))
    include_dirs = {os.path.dirname(os.path.abspath(p)) for p in rtl_files + paths}
    include_dirs.update(os.path.dirname(r['path']) for r in manifest['testbench_files'])
    command = ['vcs', '-full64', '-sverilog'] + manifest.get('uvm_flags', ['-ntb_opts', 'uvm']) + ['-timescale=' + manifest['timescale'],
               '-top', manifest['testbench_top'], '-Mdir=' + os.path.abspath(os.path.join(BUILD_DIR, 'csrc'))]
    command += ['+incdir+' + p for p in sorted(include_dirs)] + list(rtl_files) + paths + ['-o', binary]
    report.step('compile')
    compiled = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              universal_newlines=True, timeout=300)
    report.artifact('compile_tv.log', text=compiled.stdout)
    report.update(compile_passed=compiled.returncode == 0)
    if compiled.returncode:
        raise RuntimeError('Directed UVM compilation failed; see compile_tv.log.')
    report.step('simulation')
    try:
        simulated = subprocess.run([binary, '-no_save'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   universal_newlines=True, timeout=300)
        output, completed = simulated.stdout, simulated.returncode == 0
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ''
        if isinstance(output, bytes):
            output = output.decode('utf-8', errors='replace')
        completed = False
    report.artifact('sim_tv.log', text=output)
    result = build_directed_uvm_report(plan, manifest, output, completed)
    report.requirements(result['combined_requirement_results'])
    report.update(**result)
    report.artifact('directed_vector_results.json', data=result)
    report.artifact('combined_requirement_verification.json', data=result['combined_requirement_results'])
    print('Agent 3 directed UVM results: ' + result['verification_status'])
    for test in result['tests']:
        print('  {}: {} ({} passed checks, {} failed checks)'.format(
            test['name'], test['status'], test['passed_checks'], test['failed_checks']))
    return result['verification_status'] != 'FAIL' and all(t['completed'] for t in result['tests'])


def _main(report):
    global ACTIVE_REPORT
    ACTIVE_REPORT = report
    banner("AGENT 3 — TEST VECTORS", "yellow")

    try:
        rtl_files, top_module_file, dut_spec = resolve_inputs()
    except Exception as e:
        log("TV_AGENT", str(e), "error")
        sys.exit(1)

    ensure_build_dirs()

    rtl_code = read_file(top_module_file)
    report.update(module_name=dut_spec['module_name'], rtl_files=rtl_files,
                  rtl_top_file=os.path.abspath(top_module_file))
    log("TV_AGENT", "Top module: {}".format(dut_spec["module_name"]), "ok")
    log("TV_AGENT", "Top file: {}".format(top_module_file), "ok")
    log("TV_AGENT", "RTL files: {}".format(len(rtl_files)), "info")

    try:
        success = run_directed_uvm(report, rtl_files, dut_spec)
    except Exception as exc:
        report.update(verification_status='FAIL', failure_detail=str(exc))
        log('TV_AGENT', str(exc), 'error')
        sys.exit(1)
    sys.exit(0 if success else 1)


def main():
    # Explicit file inputs support offline/frontend plans without model calls.
    for option, variable in (('--vector-plan', 'AGENT3_VECTOR_PLAN_FILE'),
                             ('--uvm-manifest', 'AGENT3_UVM_MANIFEST')):
        if option in sys.argv:
            index = sys.argv.index(option)
            if index + 1 >= len(sys.argv):
                raise SystemExit(option + ' requires a path')
            os.environ[variable] = os.path.abspath(sys.argv[index + 1])
            del sys.argv[index:index + 2]
    inputs = sys.argv[1:]
    if not inputs:
        try:
            validation = load_validation_result() or {}
        except (OSError, ValueError):
            validation = {}
        inputs = validation.get('rtl_files') or [os.getcwd()]
    try:
        run_reported_agent('agent3', _main, inputs)
    finally:
        global ACTIVE_REPORT
        ACTIVE_REPORT = None


if __name__ == "__main__":
    main()
