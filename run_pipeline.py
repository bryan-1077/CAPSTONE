#!/usr/bin/env python3
"""
run_pipeline.py
===============
Chains all three agents in sequence.

Examples:
  python3 run_pipeline.py rtl/basicCounter.sv
  python3 run_pipeline.py rtl/
  python3 run_pipeline.py rtl/top.sv rtl/submodule.sv
"""

import sys
import os
import json
import subprocess

CYAN   = "\033[96m"
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

VALIDATION_RESULT_FILE = "validation_result.json"


def header(title, color=CYAN):
    w = 58
    print(f"\n{color}{'='*w}{RESET}")
    print(f"{BOLD}  {title}{RESET}")
    print(f"{color}{'='*w}{RESET}")


def collect_rtl_files(paths):
    rtl_files = []

    for path in paths:
        if not os.path.exists(path):
            print(f"{RED}[ERROR] Path not found: {path}{RESET}")
            sys.exit(1)

        if os.path.isfile(path):
            if path.endswith((".sv", ".v")):
                rtl_files.append(path)

        elif os.path.isdir(path):
            for root, _, files in os.walk(path):
                for f in files:
                    if f.endswith((".sv", ".v")):
                        rtl_files.append(os.path.join(root, f))

    rtl_files = sorted(set(rtl_files))

    if not rtl_files:
        print(f"{RED}[ERROR] No RTL files (.sv/.v) found.{RESET}")
        sys.exit(1)

    return rtl_files


def load_validation_result():
    if not os.path.exists(VALIDATION_RESULT_FILE):
        return None
    try:
        with open(VALIDATION_RESULT_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return None


def print_agent1_failure_details():
    validation_result = load_validation_result()

    print(f"\n{RED}[PIPELINE] RTL failed validation.{RESET}")

    if not validation_result:
        print(f"           ➜ Could not read {VALIDATION_RESULT_FILE}.{RESET}")
        print(f"           ➜ Return to RTL generation and fix the RTL.{RESET}\n")
        return

    yaml_sane = validation_result.get("yaml_sane")
    spec_consistent = validation_result.get("spec_consistent")
    rtl_sane = validation_result.get("rtl_sane")
    compile_passed = validation_result.get("compile_passed")
    summary = validation_result.get("summary", "")
    issues = validation_result.get("issues", [])
    compile_output = validation_result.get("compile_output_snippet", "")

    if yaml_sane is False:
        print(f"           ➜ Failure category: YAML sanity")
        print(f"           ➜ The YAML spec itself appears invalid or contradictory.")
    elif spec_consistent is False:
        print(f"           ➜ Failure category: Spec ↔ RTL consistency")
        print(f"           ➜ The RTL does not match the YAML spec.")
    elif rtl_sane is False:
        print(f"           ➜ Failure category: RTL intrinsic sanity")
        print(f"           ➜ The RTL appears structurally/logically invalid on its own.")
    elif compile_passed is False:
        print(f"           ➜ Failure category: Compile sanity")
        print(f"           ➜ The RTL failed VCS/compile validation.")
    else:
        print(f"           ➜ Failure category: General validation failure")

    if summary:
        print(f"           ➜ Summary: {summary}")

    if issues:
        print(f"           ➜ Issues:")
        for issue in issues[:8]:
            if "\n" in issue:
                first = issue.split("\n")[0]
                print(f"              - {first}")
            else:
                print(f"              - {issue}")

    if compile_passed is False and compile_output:
        print(f"           ➜ Compile output snippet:")
        for ln in compile_output.splitlines()[:12]:
            print(f"              {ln}")

    print(f"           ➜ Return to RTL generation and fix the failing stage.{RESET}\n")


def main():
    if len(sys.argv) < 2:
        print(f"Usage: python3 {sys.argv[0]} <rtl_file_or_dir> [more_files_or_dirs...]")
        sys.exit(1)

    rtl_files = collect_rtl_files(sys.argv[1:])

    print(f"{CYAN}[PIPELINE] RTL inputs:{RESET}")
    for f in rtl_files:
        print(f"  - {f}")

    # ── Agent 1 ─────────────────────────────────────────
    header("AGENT 1 — RTL VALIDATOR")
    r1 = subprocess.run(["python3", "agent1_rtl_validator.py"] + rtl_files)
    if r1.returncode != 0:
        print_agent1_failure_details()
        sys.exit(1)

    # ── Agent 2 ─────────────────────────────────────────
    header("AGENT 2 — UVM TESTBENCH")
    r2 = subprocess.run(["python3", "agent2_uvm_tb.py"] + rtl_files)
    if r2.returncode != 0:
        print(f"\n{RED}[PIPELINE] UVM TB generation/simulation failed.{RESET}\n")
        sys.exit(1)

    # ── Agent 3 ─────────────────────────────────────────
    header("AGENT 3 — TEST VECTOR GENERATOR", YELLOW)
    r3 = subprocess.run(["python3", "agent3_testvectors.py"] + rtl_files)
    if r3.returncode != 0:
        print(f"\n{RED}[PIPELINE] Test vector simulation failed.{RESET}\n")
        sys.exit(1)

    print(f"\n{GREEN}{BOLD}[PIPELINE] Complete — RTL validated, TB verified, vectors passed.{RESET}\n")


if __name__ == "__main__":
    main()