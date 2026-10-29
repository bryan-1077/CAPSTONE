#!/usr/bin/env python3
"""Shared width-safety helpers for generated SystemVerilog."""

from __future__ import annotations

import re


def _extract_int_parameter(rtl: str, parameter_name: str, default: int) -> int:
    """Return an emitted integer parameter default when present, else fallback."""
    match = re.search(
        rf"\bparameter\s+int\s+{re.escape(parameter_name)}\s*=\s*(\d+)\s*;",
        rtl,
    )
    if match is None:
        return default
    return int(match.group(1))


def _emit_width_safe_trrd(module_name: str, trrd_cycles: int) -> str:
    return f"""module {module_name} (
    input logic clk,
    input logic rst_n,
    input logic act_pulse,
    output logic tRRD_block
);

    parameter int TRRD_CYCLES = {trrd_cycles};
    localparam int COUNTER_WIDTH = $clog2(TRRD_CYCLES + 1);

    logic [COUNTER_WIDTH-1:0] counter;

    localparam logic [COUNTER_WIDTH-1:0] TRRD_CYCLES_L = COUNTER_WIDTH'(TRRD_CYCLES);
    localparam logic [COUNTER_WIDTH-1:0] COUNTER_ZERO_L = COUNTER_WIDTH'(0);
    localparam logic [COUNTER_WIDTH-1:0] COUNTER_ONE_L = COUNTER_WIDTH'(1);

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            counter <= COUNTER_ZERO_L;
        end else if (act_pulse) begin
            counter <= TRRD_CYCLES_L - COUNTER_ONE_L;
        end else if (counter > COUNTER_ZERO_L) begin
            counter <= counter - COUNTER_ONE_L;
        end else begin
            counter <= COUNTER_ZERO_L;
        end
    end

    always_comb begin
        tRRD_block = (counter > COUNTER_ZERO_L);
    end

endmodule
"""


def _emit_width_safe_tfaw(module_name: str, tfaw_cycles: int, tfaw_limit: int) -> str:
    return f"""module {module_name}(
  input logic clk,
  input logic rst_n,
  input logic act_pulse,
  output logic [2:0] act_count,
  output logic tFAW_block,
  output logic tFAW_ok
);

  parameter int TFAW_CYCLES = {tfaw_cycles};
  parameter int TFAW_LIMIT = {tfaw_limit};

  typedef logic [TFAW_CYCLES-1:0] shift_reg_t;
  localparam int NEXT_COUNT_WIDTH = $clog2(TFAW_CYCLES + 1);
  logic [NEXT_COUNT_WIDTH-1:0] next_count;
  shift_reg_t act_window;
  shift_reg_t next_window;

  localparam int ACT_COUNT_WIDTH = $bits(act_count);
  localparam logic [NEXT_COUNT_WIDTH-1:0] TFAW_LIMIT_L = NEXT_COUNT_WIDTH'(TFAW_LIMIT);
  localparam logic [ACT_COUNT_WIDTH-1:0] TFAW_LIMIT_ACT_COUNT_L = ACT_COUNT_WIDTH'(TFAW_LIMIT);
  localparam logic [ACT_COUNT_WIDTH-1:0] ACT_COUNT_ZERO_L = ACT_COUNT_WIDTH'(0);

  // Combinational logic to calculate next_window and next_count
  always_comb begin
    next_window = {{act_window[TFAW_CYCLES-2:0], act_pulse}};
    next_count = NEXT_COUNT_WIDTH'($countones(next_window));
    tFAW_block = (next_count >= TFAW_LIMIT_L);
    tFAW_ok = !tFAW_block;
  end

  // Sequential logic for act_window and act_count
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      act_window <= '0;
      act_count <= ACT_COUNT_ZERO_L;
    end else begin
      act_window <= next_window;
      act_count <= (next_count > TFAW_LIMIT_L)
          ? TFAW_LIMIT_ACT_COUNT_L
          : next_count[ACT_COUNT_WIDTH-1:0];
    end
  end

endmodule
"""


def _enforce_scheduler_refresh_priority(rtl: str) -> str:
    """Make queue scheduler refresh arbitration explicit and mutually exclusive."""
    if "module ddr4_scheduler_scheduler" not in rtl:
        return rtl

    rtl = re.sub(
        r"successful_issue\s*=\s*issue_valid\s*&&\s*cmd_ready\s*&&\s*timing_ok\s*&&\s*!issue_ref\s*;",
        "successful_issue = issue_valid && cmd_ready && timing_ok && !ref_req && !issue_ref;",
        rtl,
        count=1,
    )

    pattern = re.compile(
        r"(?P<indent>[ \t]*)successful_issue\s*=\s*issue_valid\s*&&\s*cmd_ready\s*&&\s*timing_ok\s*;\s*\n"
        r"(?P=indent)issue_txn\s*=\s*successful_issue\s*;\s*\n"
        r"(?P=indent)issue_ref\s*=\s*ref_req\s*&&\s*cmd_ready\s*&&\s*timing_ok\s*;",
        re.MULTILINE,
    )

    def replace(match: re.Match[str]) -> str:
        indent = match.group("indent")
        return "\n".join([
            f"{indent}issue_ref = ref_req && cmd_ready && timing_ok;",
            f"{indent}successful_issue = issue_valid && cmd_ready && timing_ok && !ref_req && !issue_ref;",
            f"{indent}issue_txn = successful_issue;",
        ])

    hardened, replacements = pattern.subn(replace, rtl, count=1)
    if replacements:
        rtl = hardened

    else:
        pattern = re.compile(
            r"(?P<indent>[ \t]*)issue_ref\s*=\s*ref_req\s*&&\s*cmd_ready\s*&&\s*timing_ok\s*;\s*\n"
            r"(?P=indent)successful_issue\s*=\s*issue_valid\s*&&\s*cmd_ready\s*&&\s*timing_ok\s*;\s*\n"
            r"(?P=indent)issue_txn\s*=\s*successful_issue\s*;",
            re.MULTILINE,
        )
        hardened, replacements = pattern.subn(replace, rtl, count=1)
        if replacements:
            rtl = hardened

    candidate_blocked_terms = [
        ("candidate_valid", "candidate_valid && !ref_req && !(cmd_ready && timing_ok)"),
        ("candidate_found", "candidate_found && !ref_req && !(cmd_ready && timing_ok)"),
    ]
    for candidate_signal, blocked_expr in candidate_blocked_terms:
        rtl = re.sub(
            rf"\bif\s*\(\s*{candidate_signal}\s*\)\s*begin",
            f"if ({blocked_expr}) begin",
            rtl,
            count=1,
        )
        rtl = re.sub(
            rf"\bif\s*\(\s*{candidate_signal}\s*&&\s*!ref_req\s*\)\s*begin",
            f"if ({blocked_expr}) begin",
            rtl,
            count=1,
        )

    rtl = re.sub(
        r"\bif\s*\(\s*!\s*ref_req\s*&&\s*\(\s*found_row_hit\s*\|\|\s*found_valid\s*\)\s*\)\s*begin",
        "if (!ref_req && (found_row_hit || found_valid) && !(cmd_ready && timing_ok)) begin",
        rtl,
        count=1,
    )

    return rtl


def enforce_width_safety(rtl: str, module_name: str) -> str:
    """Return width-safe RTL for known generated modules."""
    if module_name == "ddr4_tRRD_simple_tRRD":
        trrd_cycles = _extract_int_parameter(rtl, "TRRD_CYCLES", 4)
        return _emit_width_safe_trrd(module_name, trrd_cycles)
    if module_name == "ddr4_tFAW_tFAW_tracker":
        tfaw_cycles = _extract_int_parameter(rtl, "TFAW_CYCLES", 32)
        tfaw_limit = _extract_int_parameter(rtl, "TFAW_LIMIT", 4)
        return _emit_width_safe_tfaw(module_name, tfaw_cycles, tfaw_limit)
    if module_name == "ddr4_scheduler_scheduler":
        return _enforce_scheduler_refresh_priority(rtl)
    return rtl
