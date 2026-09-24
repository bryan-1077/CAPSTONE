#!/usr/bin/env python3
"""
rtl_generation_agent.py
-----------------------
RTL Generation Agent - Frontend Team
Generates SystemVerilog RTL from specifications and validates syntax.

FSM States:
  GENERATE_VERILOG  -> LLM writes synthesizable SystemVerilog
  WRITE_VERILOG     -> Save RTL to disk
  CHECK_RTL         -> Optional syntax validation (can skip if not working)
  DONE              -> Package RTL for handoff to backend team

Usage:
  python3 rtl_generation_agent.py
"""

import os
import re
import subprocess
import json
from datetime import datetime

# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────

WORK_DIR = "./rtl_output"           # All generated RTL files land here

# Keep these ONLY for syntax check (optional)
SYNOPSYS_SEARCH_PATH = ". /opt/coe/synopsys/syn/V-2023.12-SP1/libraries/syn"
TARGET_LIBRARY       = "/opt/coe/synopsys/syn/V-2023.12-SP1/libraries/syn/lsi_10k.db"
DC_SHELL_CMD  = "dc_shell"          # Only for syntax check

MAX_RTL_RETRIES = 3                 # How many times to fix RTL on syntax errors

LLM_MODEL = "protected.gpt-4o"
TAMU_API_URL = "https://chat-api.tamu.ai/openai/chat/completions"

# Feature flags
ENABLE_SYNTAX_CHECK = False         # Set to True when syntax checker is working

# ──────────────────────────────────────────────
# LLM helper
# ──────────────────────────────────────────────

def call_llm(prompt: str, max_tokens: int = 1000, temperature: float = 0.0) -> str:
    """Call TAMU AI Chat API. Requires TAMUS_AI_CHAT_API_KEY env var."""
    import requests

    api_key = os.environ.get("TAMUS_AI_CHAT_API_KEY")
    if not api_key:
        return "Error: TAMUS_AI_CHAT_API_KEY not set"

    try:
        response = requests.post(
            TAMU_API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type":  "application/json",
            },
            json={
                "model":       LLM_MODEL,
                "messages":    [{"role": "user", "content": prompt}],
                "max_tokens":  max_tokens,
                "temperature": temperature,
                "stream":      False,
            },
            timeout=30,
        )

        if response.status_code != 200:
            return f"Error: HTTP {response.status_code} — {response.text[:200]}"

        return response.json()["choices"][0]["message"]["content"]

    except Exception as e:
        return f"Error: {str(e)}"


# ──────────────────────────────────────────────
# Utility helpers
# ──────────────────────────────────────────────

def strip_markdown_fences(text: str, lang: str = "") -> str:
    """Remove markdown fences, language tags, and stray leading words."""
    if not text:
        return ""

    patterns = [
        rf"```{re.escape(lang)}\s*\n(.*?)\n```",
        r"```\s*\n(.*?)\n```",
        r"^\"\"\"\s*(.*?)\s*\"\"\"$",
        r"^'(.*?)'$",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.DOTALL | re.IGNORECASE)
        if m:
            text = m.group(1)
            break

    text = text.strip()
    text = re.sub(
        r"^(?:systemverilog|system\s*verilog|verilog|sv|tcl)\s*[\r\n]+",
        "",
        text,
        flags=re.IGNORECASE
    )
    text = text.strip()
    text = re.sub(r"^`+|`+$", "", text).strip()

    return text


def write_file(path: str, content: str):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        f.write(content)
    print(f"  [write] {path}")


def log(msg: str):
    print(f"\n{'='*60}\n{msg}\n{'='*60}")


# ──────────────────────────────────────────────
# State implementations
# ──────────────────────────────────────────────

def state_generate_verilog(task_description: str, previous_rtl: str = "", fix_notes: str = "") -> str:
    """Ask the LLM to produce synthesizable SystemVerilog."""
    fix_section = ""
    if fix_notes:
        fix_section = f"""
A previous version of this RTL had issues. Here are the problems to fix:
{fix_notes}

Previous RTL (needs fixing):
{previous_rtl}
"""

    prompt = f"""You are an expert ASIC RTL designer. Write synthesizable SystemVerilog for the following design.

DESIGN REQUIREMENT:
{task_description}

RULES — your output must follow these exactly:
- Use SystemVerilog syntax (logic type, always_ff, always_comb)
- Use only synthesizable constructs
- No inferred latches — every signal in always_comb must be assigned in all branches
- No combinational loops
- Use `logic` type for all signals (this is SystemVerilog, not Verilog-2001)
- Sequential blocks: use `always_ff @(posedge clk or negedge rst_n)`
- Combinational blocks: use `always_comb`
- Active-low reset should use negedge in always_ff sensitivity list
- Add a short header comment block: module name, ports, date
- Output ONLY the SystemVerilog code — no explanation, no markdown fences, no language tags
{fix_section}
"""
    raw = call_llm(prompt, max_tokens=2000, temperature=0.2)
    return strip_markdown_fences(raw, lang="verilog")


def state_check_rtl_syntax(module_name: str, sv_filename: str, work_dir: str) -> dict:
    """
    Quick syntax check using Design Compiler - no synthesis.
    Returns dict with 'passed' (bool) and 'errors' (list of strings).
    
    NOTE: This is optional and can be disabled via ENABLE_SYNTAX_CHECK flag.
    """
    check_tcl = f"""# check_rtl.tcl - Syntax check only

# Set library paths (needed for link command)
set search_path "{{{SYNOPSYS_SEARCH_PATH}}}"
set target_library "{TARGET_LIBRARY}"
set link_library "* $target_library"

# Set SystemVerilog standard
set hdlin_sverilog_std 2017

# Try to read the SystemVerilog file
if {{ [catch {{read_sverilog {sv_filename}}} result] }} {{
    puts "ERROR: Failed to read SystemVerilog file"
    puts $result
    exit 1
}}

# Try to set current design
if {{ [catch {{current_design {module_name}}} result] }} {{
    puts "ERROR: Failed to set current design"
    puts $result
    exit 1
}}

# Try to link (this catches most syntax and elaboration errors)
if {{ [link] == 0 }} {{
    puts "ERROR: Design failed to link"
    exit 1
}}

puts "RTL_CHECK_PASSED"
exit 0
"""
    
    check_tcl_path = os.path.join(work_dir, "check_rtl.tcl")
    write_file(check_tcl_path, check_tcl)
    
    cmd = f"{DC_SHELL_CMD} -f check_rtl.tcl > check_rtl.log 2>&1"
    print(f"  [check_rtl] Running syntax check...")
    
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=60,
            cwd=work_dir,
        )
        
        combined_log = (result.stdout or "") + (result.stderr or "")
        passed = "RTL_CHECK_PASSED" in combined_log
        
        dc_errors = re.findall(r"^Error:.*$", combined_log, re.MULTILINE)
        
        return {
            "passed": passed,
            "errors": dc_errors,
            "log": combined_log,
        }
        
    except subprocess.TimeoutExpired:
        return {
            "passed": False,
            "errors": ["Syntax check timed out"],
            "log": "Timeout",
        }
    except Exception as e:
        return {
            "passed": False,
            "errors": [str(e)],
            "log": str(e),
        }


# ──────────────────────────────────────────────
# Main agent loop
# ──────────────────────────────────────────────

def run_rtl_agent(
    task_description: str,
    module_name: str,
    work_dir: str = WORK_DIR,
):
    """
    RTL Generation loop: Generate → Validate Syntax → Output
    """
    os.makedirs(work_dir, exist_ok=True)

    sv_filename = f"{module_name}.sv"
    sv_path     = os.path.join(work_dir, sv_filename)

    rtl_code   = ""
    retry      = 0
    fix_notes  = ""
    passed     = False

    log(f"RTL Generation Agent Starting\nModule: {module_name}  |  Work dir: {work_dir}")

    while retry <= MAX_RTL_RETRIES:

        # ── State: GENERATE_VERILOG ────────────────────────────────────────
        log(f"State: GENERATE_VERILOG  (attempt {retry + 1}/{MAX_RTL_RETRIES + 1})")
        rtl_code = state_generate_verilog(task_description, rtl_code, fix_notes)
        print(f"  Generated {len(rtl_code.splitlines())} lines of SystemVerilog")
        print(f"  First 200 chars:\n{rtl_code[:200]}")

        # ── State: WRITE_VERILOG ───────────────────────────────────────────
        log("State: WRITE_VERILOG")
        write_file(sv_path, rtl_code)
        
        # ── State: CHECK_RTL (optional) ────────────────────────────────────
        if ENABLE_SYNTAX_CHECK:
            log("State: CHECK_RTL (syntax validation)")
            syntax_check = state_check_rtl_syntax(module_name, sv_filename, work_dir)
            
            if syntax_check["passed"]:
                print("  ✓ RTL syntax check PASSED")
                passed = True
                break
            else:
                print("  ✗ RTL syntax check FAILED")
                print(f"  Errors:\n    " + "\n    ".join(syntax_check["errors"][:5]))
                
                retry += 1
                if retry <= MAX_RTL_RETRIES:
                    log(f"State: FIX_RTL (syntax errors, retry {retry}/{MAX_RTL_RETRIES})")
                    fix_notes = f"""The RTL has syntax errors:

Errors:
{chr(10).join(syntax_check['errors'])}

Please fix these syntax errors in the SystemVerilog code.
"""
                    print(f"\n  Fix notes:\n{fix_notes}")
                    continue
                else:
                    log("Max retries reached — escalating to user")
                    break
        else:
            # Syntax check disabled - just accept the RTL
            print("  ⚠ Syntax check DISABLED (set ENABLE_SYNTAX_CHECK=True to enable)")
            passed = True
            break

    # ── State: DONE ────────────────────────────────────────────────────────
    log("State: DONE")
    _print_final_report(module_name, work_dir, passed, retry)

    return {
        "passed":       passed,
        "rtl_path":     sv_path,
        "retries_used": retry,
    }


def _print_final_report(module_name, work_dir, passed, retries):
    """Print a clean final summary."""
    status = "✓ PASSED" if passed else "✗ FAILED"

    print(f"""
┌─────────────────────────────────────────────────────────┐
│  RTL Generation Complete — {module_name:<31}
├─────────────────────────────────────────────────────────┤
│  Status       : {status:<42}
│  RTL retries  : {retries:<42}
│  Syntax check : {"ENABLED" if ENABLE_SYNTAX_CHECK else "DISABLED":<42}
├─────────────────────────────────────────────────────────┤
│  Output files (in {work_dir}):
│    RTL file   : {module_name}.sv
│    Check log  : check_rtl.log (if syntax check enabled)
├─────────────────────────────────────────────────────────┤
│  Next Steps:
│    1. Review the generated RTL: {module_name}.sv
│    2. Hand off to backend team for synthesis
│    3. Backend will create TCL scripts and run dc_shell
└─────────────────────────────────────────────────────────┘
""")

    if not passed:
        print("  ⚠ ACTION REQUIRED: RTL generation or validation failed.")
        print("     Check the RTL file and fix any issues manually if needed.")


# ──────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────

if __name__ == "__main__":

    TASK = """

    """

    MODULE_NAME = "counter"

    result = run_rtl_agent(
        task_description=TASK,
        module_name=MODULE_NAME,
        work_dir="./rtl_output",
    )

    print("\nResult dict:", json.dumps(result, indent=2))