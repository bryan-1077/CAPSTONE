#!/usr/bin/env python3
"""Generate the top-level DDR4 controller wrapper RTL."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

from check_interfaces import check_interfaces


MODULE_NAME = "ddr4_controller_top"
BANK_TOP_MODULE_NAME = "ddr4_bank_top"
SCRIPT_DIR = Path(__file__).resolve().parent
RTL_OUTPUT_DIR = SCRIPT_DIR / "rtl_output"
DEFAULT_OUTPUT = RTL_OUTPUT_DIR / f"{MODULE_NAME}.sv"
SCHEDULER_IR_PATH = SCRIPT_DIR / "ir" / "ddr4_scheduler_scheduler_ir.json"
GENERATED_BANK_INPUT_PATH = SCRIPT_DIR / "inputs" / "generated" / "ddr4_bank.yaml"
GENERATED_SCHEDULER_INPUT_PATH = SCRIPT_DIR / "inputs" / "generated" / "ddr4_scheduler.yaml"
USER_CONFIG_PATH = SCRIPT_DIR / "configs" / "user_input.yaml"
SUPPORTED_BANK_COUNTS = {1, 2, 4}
SUPPORTED_PAGE_POLICIES = {"open_page", "close_page"}
DEFAULT_PAGE_POLICY = "open_page"
MEMORY_ADDR_WIDTH = 4
MEMORY_DATA_WIDTH = 32
READ_RESPONSE_LATENCY_CYCLES = 1

CORE_MODULES = [
    "ddr4_request_queue",
    "ddr4_scheduler_scheduler",
    "ddr4_bank_bank_sequencer",
    "ddr4_bank_activate_fsm",
    "ddr4_bank_tRAS_fsm",
    "ddr4_bank_precharge_fsm",
]
OPTIONAL_MODULES = {
    "refresh": "ddr4_refresh_refresh_controller",
    "tfaw": "ddr4_tFAW_tFAW_tracker",
    "trrd": "ddr4_tRRD_simple_tRRD",
}


def module_rtl_path(module_name: str) -> Path:
    """Return the expected RTL path for a generated module."""
    return RTL_OUTPUT_DIR / f"{module_name}.sv"


def load_yaml(path: Path) -> dict:
    """Load a YAML mapping or return an empty mapping if unavailable."""
    if not path.is_file():
        return {}

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return {}

    return data if isinstance(data, dict) else {}


def load_bank_count() -> int:
    """Return the configured bank count, defaulting to the current single-bank topology."""
    generated_cfg = load_yaml(GENERATED_BANK_INPUT_PATH).get("controller_config", {})
    topology_cfg = generated_cfg.get("topology")
    if isinstance(topology_cfg, dict):
        bank_count = topology_cfg.get("banks")
        if isinstance(bank_count, int) and not isinstance(bank_count, bool):
            if bank_count in SUPPORTED_BANK_COUNTS:
                return bank_count

    user_cfg = load_yaml(USER_CONFIG_PATH).get("topology")
    if isinstance(user_cfg, dict):
        bank_count = user_cfg.get("banks")
        if isinstance(bank_count, int) and not isinstance(bank_count, bool):
            if bank_count in SUPPORTED_BANK_COUNTS:
                return bank_count

    return 1


def load_scheduler_mode() -> str:
    """Return the generated scheduler mode recorded in config or scheduler IR."""
    scheduler_input = load_yaml(GENERATED_SCHEDULER_INPUT_PATH).get("controller_config", {})
    feature_list = scheduler_input.get("features")
    if isinstance(feature_list, list):
        if "scheduler_round_robin" in feature_list:
            return "round_robin"
        if "scheduler" in feature_list:
            return "simple"

    user_scheduler = load_yaml(USER_CONFIG_PATH).get("features", {}).get("scheduler")
    if user_scheduler in {"simple", "round_robin"}:
        return str(user_scheduler)

    if not SCHEDULER_IR_PATH.is_file():
        return "simple"

    try:
        ir_data = json.loads(SCHEDULER_IR_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return "simple"

    scheduler_mode = ir_data.get("metadata", {}).get("scheduler_mode")
    if scheduler_mode in {"simple", "round_robin"}:
        return str(scheduler_mode)
    return "simple"


def load_page_policy() -> str:
    """Return the configured page policy, defaulting to open_page."""
    generated_cfg = load_yaml(GENERATED_BANK_INPUT_PATH).get("controller_config", {})
    generated_policy = generated_cfg.get("page_policy")
    if generated_policy in SUPPORTED_PAGE_POLICIES:
        return str(generated_policy)

    user_policy = load_yaml(USER_CONFIG_PATH).get("features", {}).get("page_policy")
    if user_policy in SUPPORTED_PAGE_POLICIES:
        return str(user_policy)

    return DEFAULT_PAGE_POLICY


def bank_select_width(bank_count: int) -> int:
    """Return the txn_bank width required to address the configured bank count."""
    if bank_count <= 1:
        return 0
    return (bank_count - 1).bit_length()


def bank_select_range(bank_count: int) -> str:
    """Return the optional packed range used for txn_bank declarations."""
    width = bank_select_width(bank_count)
    if width <= 1:
        return ""
    return f"[{width - 1}:0] "


def bank_select_literal(bank_count: int, bank_index: int) -> str:
    """Return a width-aware SystemVerilog literal for one bank index."""
    width = max(bank_select_width(bank_count), 1)
    return f"{width}'d{bank_index}"


def typed_cmd_literal(is_write: bool) -> str:
    """Return the wrapper-level bank command encoding for read/write transactions."""
    return "2'b10" if is_write else "2'b01"


def discover_available_modules() -> set[str]:
    """Return currently generated module names available for wrapper assembly."""
    module_names = set(CORE_MODULES)
    module_names.update(OPTIONAL_MODULES.values())
    return {
        module_name
        for module_name in module_names
        if module_rtl_path(module_name).is_file()
    }


def ensure_required_modules(available_modules: set[str]) -> None:
    """Raise a clear error if any core module is missing."""
    missing = [module_name for module_name in CORE_MODULES if module_name not in available_modules]
    if missing:
        missing_list = ", ".join(sorted(missing))
        raise RuntimeError(f"missing required generated modules: {missing_list}")


def build_io_descriptions(bank_count: int) -> list[tuple[str, str, str]]:
    """Return the generated top-level IO descriptions."""
    io_descriptions = [
        ("clk", "input", "Controller clock."),
        ("rst_n", "input", "Active-low synchronous reset."),
        ("txn_valid", "input", "Requests scheduler issue a transaction when timing allows."),
        ("txn_is_write", "input", "Transaction type selector: 0=READ, 1=WRITE."),
        ("txn_addr", "input", f"Address for the minimal banked storage model ({MEMORY_ADDR_WIDTH} bits)."),
        ("txn_wdata", "input", f"Write data for accepted WRITE transactions ({MEMORY_DATA_WIDTH} bits)."),
    ]
    if bank_count > 1:
        bank_select_desc = (
            "Selects the target bank for the single incoming transaction stream."
            if bank_count == 2
            else f"Selects one of {bank_count} banks for the single incoming transaction stream."
        )
        io_descriptions.append(
            ("txn_bank", "input", bank_select_desc),
        )
    io_descriptions.append(
        ("cmd_ready", "output", "Indicates whether the currently selected bank can accept a transaction."),
    )
    io_descriptions.append(
        (
            "rsp_valid",
            "output",
            f"One-cycle pulse indicating read response data is valid {READ_RESPONSE_LATENCY_CYCLES} cycle after an accepted READ.",
        ),
    )
    io_descriptions.append(
        ("rsp_rdata", "output", f"Read response data returned from the selected banked storage ({MEMORY_DATA_WIDTH} bits)."),
    )
    return io_descriptions


def build_known_simplifications(bank_count: int, page_policy: str) -> list[str]:
    """Return the known-simplification notes aligned with the generated wrapper."""
    notes = [
        "READ and WRITE share the same simplified bank sequencing structure; cmd_type preserves direction semantics without changing the bank FSM structure.",
        f"Accepted READ transactions return data with a fixed {READ_RESPONSE_LATENCY_CYCLES}-cycle response latency.",
        "WRITE transactions update the minimal banked storage model and do not produce a response payload.",
        f"Page policy is modeled at the wrapper level: {page_policy} controls whether a serviced row remains open after completion.",
        "Refresh remains a simplified top-level event source and is not modeled as a detailed per-bank flow.",
        "tFAW and tRRD remain controller-global gates even when multiple banks are instantiated.",
    ]
    if bank_count == 1:
        notes.insert(
            0,
            "The wrapper preserves the current single-bank control semantics while adding minimal address, write-data, and read-response ports.",
        )
    else:
        notes.insert(
            0,
            "The wrapper exposes one transaction stream with explicit bank selection and does not perform bank reordering or auto-selection.",
        )
    return notes


def build_feature_summary(
    has_refresh: bool,
    has_tfaw: bool,
    has_trrd: bool,
    scheduler_mode: str,
    bank_count: int,
    page_policy: str,
) -> list[str]:
    """Return a concise feature summary aligned with the assembled wrapper."""
    summary = [
        f"Instantiates {bank_count} reusable {BANK_TOP_MODULE_NAME} integration block(s) to package the per-bank FSM chain.",
        "Keeps scheduler arbitration focused on refresh-versus-transaction selection; bank choice comes from the external transaction bank select.",
        "Maps txn_is_write into bank-local cmd_type values so READ and WRITE remain visible through the control path.",
        "Implements a small banked storage model in the controller wrapper so accepted WRITEs store data and accepted READs return stored data.",
        (
            "Keeps the serviced row open after completion so later accesses can reuse it."
            if page_policy == "open_page"
            else "Closes the serviced row after completion so later accesses must reopen it."
        ),
    ]
    if bank_count > 1:
        summary.append(
            "Routes the single transaction stream only to the selected bank and mirrors cmd_ready from that selected bank."
        )
    else:
        summary.append("Presents the single transaction stream directly to the sole bank integration block.")

    if has_refresh:
        if scheduler_mode == "round_robin":
            summary.append(
                "Instantiates the refresh controller and resolves refresh-versus-transaction contention with round-robin arbitration."
            )
        else:
            summary.append(
                "Instantiates the refresh controller and gives refresh requests fixed priority in the scheduler path."
            )
    else:
        summary.append("Ties the scheduler refresh request input low when refresh support is disabled.")

    if has_tfaw or has_trrd:
        gating_terms = []
        if has_tfaw:
            gating_terms.append("tFAW")
        if has_trrd:
            gating_terms.append("tRRD")
        summary.append(
            "Gates transaction issue with shared controller-level {} activation-spacing checks.".format(
                " and ".join(gating_terms)
            )
        )
    else:
        summary.append("Passes transaction issue straight through when activation-spacing trackers are disabled.")

    summary.append(
        f"Returns read data on rsp_rdata with a fixed {READ_RESPONSE_LATENCY_CYCLES}-cycle rsp_valid pulse and no write response."
    )

    return summary


def build_bank_top_rtl() -> str:
    """Return the reusable per-bank integration module RTL."""
    return """`timescale 1ns/1ps
// ============================================================
// Auto-generated by generate_wrapper.py -- DO NOT EDIT
// Design : ddr4_bank_top
// Purpose: Package the per-bank FSM chain behind one reusable module
// ============================================================

module ddr4_bank_top (
    input  logic       clk,
    input  logic       rst_n,
    input  logic       cmd_valid,
    input  logic [1:0] cmd_type,
    output logic       bank_idle,
    output logic       bank_active,
    output logic       cmd_ready,
    output logic       activating
);

    logic tRCD_start;
    logic tRAS_start;
    logic tRP_start;
    logic tRCD_done;
    logic tRAS_done;
    logic tRP_done;
    logic tRCD_ack;
    logic tRAS_ack;
    logic tRP_ack;

    assign activating = tRCD_start;

    ddr4_bank_bank_sequencer u_bank_sequencer (
        .clk(clk),
        .rst_n(rst_n),
        .cmd_valid(cmd_valid),
        .cmd_type(cmd_type),
        .tRCD_done(tRCD_done),
        .tRAS_done(tRAS_done),
        .tRP_done(tRP_done),
        .bank_idle(bank_idle),
        .bank_active(bank_active),
        .cmd_ready(cmd_ready),
        .tRCD_start(tRCD_start),
        .tRAS_start(tRAS_start),
        .tRP_start(tRP_start),
        .tRCD_ack(tRCD_ack),
        .tRAS_ack(tRAS_ack),
        .tRP_ack(tRP_ack)
    );

    ddr4_bank_activate_fsm u_activate_fsm (
        .clk(clk),
        .rst_n(rst_n),
        .start(tRCD_start),
        .ack(tRCD_ack),
        .tRCD_done(tRCD_done)
    );

    ddr4_bank_tRAS_fsm u_tRAS_fsm (
        .clk(clk),
        .rst_n(rst_n),
        .start(tRAS_start),
        .ack(tRAS_ack),
        .tRAS_done(tRAS_done)
    );

    ddr4_bank_precharge_fsm u_precharge_fsm (
        .clk(clk),
        .rst_n(rst_n),
        .start(tRP_start),
        .ack(tRP_ack),
        .tRP_done(tRP_done)
    );

endmodule  // ddr4_bank_top
"""


def build_phase1_row_buffer_wrapper_rtl(available_modules: set[str], bank_count: int) -> str:
    """Return a row-buffer-aware Phase 1 controller wrapper."""
    has_refresh = OPTIONAL_MODULES["refresh"] in available_modules
    has_tfaw = OPTIONAL_MODULES["tfaw"] in available_modules
    has_trrd = OPTIONAL_MODULES["trrd"] in available_modules
    scheduler_mode = load_scheduler_mode()
    page_policy = load_page_policy()
    keep_rows_open = page_policy == "open_page"

    if bank_count > 1 and scheduler_mode != "simple":
        raise RuntimeError(
            "Unsupported configuration: multi-bank controller generation currently requires the simple scheduler"
        )

    bank_sel_width = max(bank_select_width(bank_count), 1)
    txn_bank_range = bank_select_range(bank_count)
    bank_vector_range = f"[{bank_count - 1}:0]"
    row_width = max(MEMORY_ADDR_WIDTH // 2, 1)
    col_width = MEMORY_ADDR_WIDTH - row_width
    decoded_bank_expr = "txn_bank" if bank_count > 1 else "2'd0"
    bank_select_signal = "selected_bank[BANK_SEL_WIDTH-1:0]" if bank_count > 1 else f"{bank_sel_width}'d0"
    scheduler_ref_req = "ref_req" if has_refresh else "1'b0"
    scheduler_timing_expr = "ref_req ? slow_path_allowed : scheduler_timing_ok" if has_refresh else "scheduler_timing_ok"
    if scheduler_mode == "simple":
        txn_sched_grant = "~ref_req" if has_refresh else "1'b1"
    else:
        txn_sched_grant = "issue_txn"

    lines = [
        "`timescale 1ns/1ps",
        "// ============================================================",
        "// Auto-generated by generate_wrapper.py -- DO NOT EDIT",
        "// Design : ddr4_controller_top",
        f"// Scheduler policy : {scheduler_mode}",
        f"// Page policy      : {page_policy}",
        f"// Bank count       : {bank_count}",
        "// ============================================================",
        "",
        "module ddr4_controller_top (",
        "    input  logic clk,",
        "    input  logic rst_n,",
        "    input  logic txn_valid,",
        "    input  logic txn_is_write,",
        f"    input  logic [{MEMORY_ADDR_WIDTH - 1}:0] txn_addr,",
        f"    input  logic [{MEMORY_DATA_WIDTH - 1}:0] txn_wdata,",
    ]
    if bank_count > 1:
        lines.append(f"    input  logic {txn_bank_range}txn_bank,")
    lines.extend(
        [
            "    output logic cmd_ready,",
            "    output logic rsp_valid,",
            f"    output logic [{MEMORY_DATA_WIDTH - 1}:0] rsp_rdata",
            ");",
            "",
            f"    localparam int BANK_COUNT = {bank_count};",
            f"    localparam int ADDR_WIDTH = {MEMORY_ADDR_WIDTH};",
            f"    localparam int DATA_WIDTH = {MEMORY_DATA_WIDTH};",
            f"    localparam int BANK_SEL_WIDTH = {bank_sel_width};",
            "    localparam int MEM_DEPTH = 1 << ADDR_WIDTH;",
            f"    localparam int ROW_WIDTH = {row_width};",
            f"    localparam int COL_WIDTH = {col_width};",
            f"    localparam int HIT_SERVICE_CYCLES = {READ_RESPONSE_LATENCY_CYCLES};",
            "    localparam int SLOW_SERVICE_CYCLES = 3;",
            "    localparam int SERVICE_COUNTER_WIDTH = 2;",
            "    localparam int REQ_QUEUE_DEPTH = 4;",
            "    localparam int REQ_SEL_WIDTH = $clog2(REQ_QUEUE_DEPTH);",
            "    localparam int SCHED_BANK_COUNT = 4;",
            f"    localparam bit KEEP_ROWS_OPEN = 1'b{1 if keep_rows_open else 0};",
        ]
    )
    if has_tfaw:
        lines.append("    localparam logic [2:0] TFAW_ACT_LIMIT_MINUS_ONE = 3'd3;")
    lines.extend(
        [
            "",
            "    logic issue_ref;",
            "    logic issue_txn;",
            "    logic issue_valid;",
            "    logic act_allowed;",
            "    logic slow_path_allowed;",
            "    logic act_pulse;",
            "    logic accept_txn_fire;",
            "    logic accept_txn;",
            "    logic accepted_hit;",
            "    logic accepted_slow;",
            "    logic accepted_read;",
            "    logic accepted_write;",
            "    logic accept_txn_q;",
            "    logic accepted_row_closed_q;",
            "    logic accepted_row_hit_q;",
            "    logic accepted_row_miss_q;",
            "    logic [BANK_SEL_WIDTH-1:0] accepted_bank_q;",
            "    logic [1:0] accepted_txn_cmd_type_q;",
            f"    logic {bank_vector_range} accepted_bank_cmd_valid_q;",
            "    logic accepted_open_row_valid_q;",
            "    logic [ROW_WIDTH-1:0] accepted_prev_open_row_q;",
            "    logic [ROW_WIDTH-1:0] accepted_requested_row_q;",
            "    logic [COL_WIDTH-1:0] accepted_requested_col_q;",
            "    logic txn_sched_grant;",
            "    logic controller_ready;",
            "    logic downstream_cmd_ready;",
            "    logic scheduler_timing_ok;",
            "    logic service_done;",
            "    logic service_pending_q;",
            "    logic service_is_write_q;",
            "    logic service_update_open_row_q;",
            "    logic service_prev_row_valid_q;",
            "    logic [1:0] txn_cmd_type;",
            "    logic [BANK_SEL_WIDTH-1:0] service_bank_q;",
            "    logic [ADDR_WIDTH-1:0] service_addr_q;",
            "    logic [DATA_WIDTH-1:0] service_wdata_q;",
            "    logic [ROW_WIDTH-1:0] requested_row;",
            "    logic [COL_WIDTH-1:0] requested_col;",
            "    logic selected_row_open_valid;",
            "    logic [ROW_WIDTH-1:0] selected_open_row;",
            "    logic incoming_row_open_valid;",
            "    logic [ROW_WIDTH-1:0] incoming_open_row;",
            "    logic incoming_row_hit;",
            "    logic incoming_timing_ok;",
            "    logic [ROW_WIDTH-1:0] service_row_q;",
            "    logic [ROW_WIDTH-1:0] service_prev_row_q;",
            "    logic [SERVICE_COUNTER_WIDTH-1:0] service_cycles_left_q;",
            "    typedef struct packed {",
            "        logic [1:0]  bank;",
            "        logic [9:0]  row;",
            "        logic [5:0]  col;",
            "        logic        is_write;",
            "        logic [31:0] wdata;",
            "    } request_t;",
            "    request_t decoded_req;",
            "    request_t selected_req;",
            "    request_t req_array [REQ_QUEUE_DEPTH];",
            "    logic req_valid [REQ_QUEUE_DEPTH];",
            "    logic [50:0] decoded_req_packed;",
            "    logic [(REQ_QUEUE_DEPTH*51)-1:0] req_array_packed;",
            "    logic [REQ_QUEUE_DEPTH-1:0] req_valid_packed;",
            "    logic [REQ_SEL_WIDTH-1:0] sel_idx;",
            "    logic deq_en;",
            "    logic enq_ready;",
            "    logic enqueue_req_valid;",
            "    logic enqueue_fire;",
            "    logic txn_enqueued_q;",
            "    logic [17:0] decoded_addr;",
            "    logic [1:0] selected_bank;",
            "    logic [1:0] incoming_bank;",
            "    logic [ADDR_WIDTH-1:0] selected_addr;",
            "    logic rsp_valid_q;",
            "    logic [DATA_WIDTH-1:0] rsp_rdata_q;",
            "    logic [31:0] cnt_accept;",
            "    logic [31:0] cnt_row_hit;",
            "    logic [31:0] cnt_row_miss;",
            "    logic [31:0] cnt_row_closed;",
            "    logic [31:0] cnt_stall;",
            "    logic [31:0] cnt_stall_busy;",
            "    logic [31:0] cnt_stall_trrd;",
            "    logic [31:0] cnt_stall_refresh;",
            "    logic [31:0] cnt_stall_other;",
            "    logic [31:0] cnt_latency_hit_total;",
            "    logic [31:0] cnt_latency_nonhit_total;",
            "    logic [31:0] cnt_latency_hit_count;",
            "    logic [31:0] cnt_latency_nonhit_count;",
            "    logic [31:0] cnt_accept_bank [0:BANK_COUNT-1];",
            "    logic [31:0] cnt_row_hit_bank [0:BANK_COUNT-1];",
            "    logic [31:0] cnt_row_miss_bank [0:BANK_COUNT-1];",
            "    logic [31:0] cnt_row_closed_bank [0:BANK_COUNT-1];",
            "    logic is_row_closed;",
            "    logic is_row_hit;",
            "    logic is_row_miss;",
            "    logic row_replacement_event;",
            f"    logic {bank_vector_range} row_open_valid;",
            "    logic [ROW_WIDTH-1:0] open_row [0:BANK_COUNT-1];",
            "    logic [SCHED_BANK_COUNT-1:0] scheduler_bank_active;",
            "    logic [9:0] scheduler_bank_open_row [0:SCHED_BANK_COUNT-1];",
            "    logic [(SCHED_BANK_COUNT*10)-1:0] scheduler_bank_open_row_packed;",
            f"    logic {bank_vector_range} bank_cmd_valid;",
            f"    logic {bank_vector_range} bank_cmd_ready;",
            f"    logic {bank_vector_range} bank_bank_idle;",
            f"    logic {bank_vector_range} bank_bank_active;",
            f"    logic {bank_vector_range} bank_activating;",
            "    logic [DATA_WIDTH-1:0] bank_mem [0:BANK_COUNT-1][0:MEM_DEPTH-1];",
            "    integer bank_mem_bank;",
            "    integer bank_mem_addr;",
        ]
    )

    for bank_index in range(bank_count):
        lines.append(f"    logic [1:0] bank{bank_index}_cmd_type;")

    if has_refresh:
        lines.extend(
            [
                "    logic ref_req;",
                "    logic ref_ack;",
            ]
        )
    if has_tfaw:
        lines.extend(
            [
                "    logic [2:0] tFAW_act_count;",
                "    logic tFAW_block;",
                "    logic tFAW_ok;",
                "    logic tfaw_can_accept_act;",
            ]
        )
    if has_trrd:
        lines.append("    logic tRRD_block;")

    lines.extend(
        [
            "",
            "    assign decoded_addr = 18'(txn_addr);",
            f"    assign decoded_req.bank = 2'({decoded_bank_expr});",
            "    assign decoded_req.row = 10'(txn_addr[ADDR_WIDTH-1 -: ROW_WIDTH]);",
            "    assign decoded_req.col = 6'(txn_addr[COL_WIDTH-1:0]);",
            "    assign decoded_req.is_write = txn_is_write;",
            "    assign decoded_req.wdata = txn_wdata;",
            "    assign decoded_req_packed = decoded_req;",
            "    assign selected_req = req_array[sel_idx];",
            "    assign selected_bank = (BANK_COUNT == 1) ? 2'd0 :",
            "                           (BANK_COUNT == 2) ? {1'b0, selected_req.bank[0]} :",
            "                           selected_req.bank;",
            "    assign incoming_bank = (BANK_COUNT == 1) ? 2'd0 :",
            "                           (BANK_COUNT == 2) ? {1'b0, decoded_req.bank[0]} :",
            "                           decoded_req.bank;",
            "    assign selected_addr = ADDR_WIDTH'({selected_req.row[ROW_WIDTH-1:0], selected_req.col[COL_WIDTH-1:0]});",
            "    assign requested_row = selected_req.row[ROW_WIDTH-1:0];",
            "    assign requested_col = selected_req.col[COL_WIDTH-1:0];",
            "    assign txn_cmd_type = selected_req.is_write ? 2'b10 : 2'b01;",
        ]
    )
    if has_tfaw:
        lines.append("    assign tfaw_can_accept_act = (tFAW_act_count < TFAW_ACT_LIMIT_MINUS_ONE);")

    slow_path_terms: list[str] = []
    if has_tfaw:
        slow_path_terms.append("tfaw_can_accept_act")
    if has_trrd:
        slow_path_terms.append("~tRRD_block")
    slow_path_allow_expr = " & ".join(slow_path_terms) if slow_path_terms else "1'b1"
    stall_trrd_condition = "tRRD_block && !is_row_hit" if has_trrd else "1'b0"
    stall_refresh_condition = "!txn_sched_grant" if has_refresh else "1'b0"
    lines.append(f"    assign slow_path_allowed = {slow_path_allow_expr};")
    lines.extend(
        [
            "    assign act_allowed = slow_path_allowed;",
            "    assign is_row_closed = ~selected_row_open_valid;",
            "    assign is_row_hit = selected_row_open_valid && (requested_row == selected_open_row);",
            "    assign is_row_miss = selected_row_open_valid && (requested_row != selected_open_row);",
            "    assign incoming_row_hit = incoming_row_open_valid && (decoded_req.row[ROW_WIDTH-1:0] == incoming_open_row);",
            "    assign downstream_cmd_ready = ~service_pending_q;",
            "    assign scheduler_timing_ok = is_row_hit || slow_path_allowed;",
            "    assign incoming_timing_ok = incoming_row_hit || slow_path_allowed;",
            "    assign controller_ready = downstream_cmd_ready && scheduler_timing_ok;",
            "    assign cmd_ready = downstream_cmd_ready && incoming_timing_ok;",
            f"    assign txn_sched_grant = {txn_sched_grant};",
            "    assign accept_txn_fire = issue_valid && controller_ready && txn_sched_grant;",
            "    assign deq_en = accept_txn_fire;",
            "    assign enqueue_req_valid = txn_valid && !txn_enqueued_q;",
            "    assign enqueue_fire = enqueue_req_valid && enq_ready;",
            "    assign accept_txn = accept_txn_q;",
            "    assign accepted_hit = accept_txn_fire && is_row_hit;",
            "    assign accepted_slow = accept_txn_fire && ~is_row_hit;",
            "    assign accepted_read = accept_txn_fire && ~selected_req.is_write;",
            "    assign accepted_write = accept_txn_fire && selected_req.is_write;",
            "    assign service_done = service_pending_q && (service_cycles_left_q == SERVICE_COUNTER_WIDTH'(0));",
            "    assign row_replacement_event = service_done && service_update_open_row_q && service_prev_row_valid_q;",
            "    assign rsp_valid = rsp_valid_q;",
            "    assign rsp_rdata = rsp_rdata_q;",
        ]
    )

    if bank_count > 1:
        lines.extend(
            [
                "",
                "    always_comb begin",
                "        selected_row_open_valid = 1'b0;",
                "        selected_open_row = '0;",
                "        unique case (selected_bank)",
            ]
        )
        for bank_index in range(bank_count):
            lines.extend(
                [
                    f"            {bank_select_literal(bank_count, bank_index)}: begin",
                    f"                selected_row_open_valid = row_open_valid[{bank_index}];",
                    f"                selected_open_row = open_row[{bank_index}];",
                    "            end",
                ]
            )
        lines.extend(
            [
                "            default: begin",
                "                selected_row_open_valid = 1'b0;",
                "                selected_open_row = '0;",
                "            end",
                "        endcase",
                "    end",
                "",
                "    always_comb begin",
                "        incoming_row_open_valid = 1'b0;",
                "        incoming_open_row = '0;",
                "        unique case (incoming_bank)",
            ]
        )
        for bank_index in range(bank_count):
            lines.extend(
                [
                    f"            {bank_select_literal(bank_count, bank_index)}: begin",
                    f"                incoming_row_open_valid = row_open_valid[{bank_index}];",
                    f"                incoming_open_row = open_row[{bank_index}];",
                    "            end",
                ]
            )
        lines.extend(
            [
                "            default: begin",
                "                incoming_row_open_valid = 1'b0;",
                "                incoming_open_row = '0;",
                "            end",
                "        endcase",
                "    end",
            ]
        )
    else:
        lines.extend(
            [
                "    assign selected_row_open_valid = row_open_valid[0];",
                "    assign selected_open_row = open_row[0];",
                "    assign incoming_row_open_valid = row_open_valid[0];",
                "    assign incoming_open_row = open_row[0];",
            ]
        )

    for bank_index in range(4):
        if bank_index < bank_count:
            lines.extend(
                [
                    f"    assign scheduler_bank_active[{bank_index}] = row_open_valid[{bank_index}];",
                    f"    assign scheduler_bank_open_row[{bank_index}] = 10'(open_row[{bank_index}]);",
                    f"    assign scheduler_bank_open_row_packed[{bank_index}*10 +: 10] = scheduler_bank_open_row[{bank_index}];",
                ]
            )
        else:
            lines.extend(
                [
                    f"    assign scheduler_bank_active[{bank_index}] = 1'b0;",
                    f"    assign scheduler_bank_open_row[{bank_index}] = '0;",
                    f"    assign scheduler_bank_open_row_packed[{bank_index}*10 +: 10] = scheduler_bank_open_row[{bank_index}];",
                ]
            )

        lines.extend(
            [
                f"    assign req_array[{bank_index}] = request_t'(req_array_packed[{bank_index}*51 +: 51]);",
                f"    assign req_valid[{bank_index}] = req_valid_packed[{bank_index}];",
            ]
        )

    for bank_index in range(bank_count):
        bank_select_term = (
            f"({bank_select_signal} == {bank_select_literal(bank_count, bank_index)})"
            if bank_count > 1
            else "1'b1"
        )
        lines.extend(
            [
                f"    assign bank_cmd_valid[{bank_index}] = accepted_slow && {bank_select_term};",
                f"    assign bank{bank_index}_cmd_type = bank_cmd_valid[{bank_index}] ? txn_cmd_type : 2'b00;",
            ]
        )

    lines.extend(
        [
            "    assign act_pulse = |bank_cmd_valid;",
        ]
    )
    if has_refresh:
        lines.append("    assign ref_ack = issue_ref;")

    lines.extend(
        [
            "",
            "    always_ff @(posedge clk) begin",
            "        if (!rst_n) begin",
            "            accept_txn_q <= 1'b0;",
            "            txn_enqueued_q <= 1'b0;",
            "            accepted_row_closed_q <= 1'b0;",
            "            accepted_row_hit_q <= 1'b0;",
            "            accepted_row_miss_q <= 1'b0;",
            "            accepted_bank_q <= '0;",
            "            accepted_txn_cmd_type_q <= 2'b00;",
            "            accepted_bank_cmd_valid_q <= '0;",
            "            accepted_open_row_valid_q <= 1'b0;",
            "            accepted_prev_open_row_q <= '0;",
            "            accepted_requested_row_q <= '0;",
            "            accepted_requested_col_q <= '0;",
            "            rsp_valid_q <= 1'b0;",
            "            rsp_rdata_q <= '0;",
            "            cnt_accept <= '0;",
            "            cnt_row_hit <= '0;",
            "            cnt_row_miss <= '0;",
            "            cnt_row_closed <= '0;",
            "            cnt_stall <= '0;",
            "            cnt_stall_busy <= '0;",
            "            cnt_stall_trrd <= '0;",
            "            cnt_stall_refresh <= '0;",
            "            cnt_stall_other <= '0;",
            "            cnt_latency_hit_total <= '0;",
            "            cnt_latency_nonhit_total <= '0;",
            "            cnt_latency_hit_count <= '0;",
            "            cnt_latency_nonhit_count <= '0;",
            "            service_pending_q <= 1'b0;",
            "            service_is_write_q <= 1'b0;",
            "            service_update_open_row_q <= 1'b0;",
            "            service_prev_row_valid_q <= 1'b0;",
            "            service_bank_q <= '0;",
            "            service_addr_q <= '0;",
            "            service_wdata_q <= '0;",
            "            service_row_q <= '0;",
            "            service_prev_row_q <= '0;",
            "            service_cycles_left_q <= '0;",
            "            row_open_valid <= '0;",
            "            for (bank_mem_bank = 0; bank_mem_bank < BANK_COUNT; bank_mem_bank = bank_mem_bank + 1) begin",
            "                cnt_accept_bank[bank_mem_bank] <= '0;",
            "                cnt_row_hit_bank[bank_mem_bank] <= '0;",
            "                cnt_row_miss_bank[bank_mem_bank] <= '0;",
            "                cnt_row_closed_bank[bank_mem_bank] <= '0;",
            "                open_row[bank_mem_bank] <= '0;",
            "                for (bank_mem_addr = 0; bank_mem_addr < MEM_DEPTH; bank_mem_addr = bank_mem_addr + 1) begin",
            "                    bank_mem[bank_mem_bank][bank_mem_addr] <= '0;",
            "                end",
            "            end",
            "        end else begin",
            "            rsp_valid_q <= 1'b0;",
            "            if (!txn_valid) begin",
            "                txn_enqueued_q <= 1'b0;",
            "            end else if (enqueue_fire) begin",
            "                txn_enqueued_q <= 1'b1;",
            "            end",
            "            if (txn_valid && !cmd_ready) begin",
            "                cnt_stall <= cnt_stall + 32'd1;",
            "                if (service_pending_q) begin",
            "                    cnt_stall_busy <= cnt_stall_busy + 32'd1;",
            f"                end else if ({stall_trrd_condition}) begin",
            "                    cnt_stall_trrd <= cnt_stall_trrd + 32'd1;",
            f"                end else if ({stall_refresh_condition}) begin",
            "                    cnt_stall_refresh <= cnt_stall_refresh + 32'd1;",
            "                end else begin",
            "                    cnt_stall_other <= cnt_stall_other + 32'd1;",
            "                end",
            "            end",
            "            if (service_pending_q) begin",
            "                if (service_cycles_left_q != SERVICE_COUNTER_WIDTH'(0)) begin",
            "                    service_cycles_left_q <= service_cycles_left_q - SERVICE_COUNTER_WIDTH'(1);",
            "                end else begin",
            "                    service_pending_q <= 1'b0;",
            "                    if (service_update_open_row_q) begin",
            "                        cnt_latency_nonhit_total <= cnt_latency_nonhit_total + SLOW_SERVICE_CYCLES;",
            "                        cnt_latency_nonhit_count <= cnt_latency_nonhit_count + 32'd1;",
            "                    end else begin",
            "                        cnt_latency_hit_total <= cnt_latency_hit_total + HIT_SERVICE_CYCLES;",
            "                        cnt_latency_hit_count <= cnt_latency_hit_count + 32'd1;",
            "                    end",
            "                    if (KEEP_ROWS_OPEN && service_update_open_row_q) begin",
            "                        row_open_valid[service_bank_q] <= 1'b1;",
            "                        open_row[service_bank_q] <= service_row_q;",
            "                    end else if (!KEEP_ROWS_OPEN) begin",
            "                        row_open_valid[service_bank_q] <= 1'b0;",
            "                        open_row[service_bank_q] <= '0;",
            "                    end",
            "                    if (service_is_write_q) begin",
            "                        bank_mem[service_bank_q][service_addr_q] <= service_wdata_q;",
            "                    end else begin",
            "                        rsp_valid_q <= 1'b1;",
            "                        rsp_rdata_q <= bank_mem[service_bank_q][service_addr_q];",
            "                    end",
            "                end",
            "            end",
            "            accept_txn_q <= 1'b0;",
            "            if (!service_pending_q && accept_txn_fire) begin",
                "                accept_txn_q <= 1'b1;",
                "                accepted_row_closed_q <= is_row_closed;",
                "                accepted_row_hit_q <= is_row_hit;",
                "                accepted_row_miss_q <= is_row_miss;",
                f"                accepted_bank_q <= {bank_select_signal};",
                "                accepted_txn_cmd_type_q <= txn_cmd_type;",
                "                accepted_bank_cmd_valid_q <= '0;",
                "                accepted_open_row_valid_q <= selected_row_open_valid;",
                "                accepted_prev_open_row_q <= selected_open_row;",
                "                accepted_requested_row_q <= requested_row;",
                "                accepted_requested_col_q <= requested_col;",
                "                cnt_accept <= cnt_accept + 32'd1;",
                f"                cnt_accept_bank[{bank_select_signal}] <= cnt_accept_bank[{bank_select_signal}] + 32'd1;",
                "                if (is_row_hit) begin",
                "                    cnt_row_hit <= cnt_row_hit + 32'd1;",
                f"                    cnt_row_hit_bank[{bank_select_signal}] <= cnt_row_hit_bank[{bank_select_signal}] + 32'd1;",
                "                end else if (is_row_miss) begin",
                "                    cnt_row_miss <= cnt_row_miss + 32'd1;",
                f"                    cnt_row_miss_bank[{bank_select_signal}] <= cnt_row_miss_bank[{bank_select_signal}] + 32'd1;",
                "                end else if (is_row_closed) begin",
                "                    cnt_row_closed <= cnt_row_closed + 32'd1;",
                f"                    cnt_row_closed_bank[{bank_select_signal}] <= cnt_row_closed_bank[{bank_select_signal}] + 32'd1;",
                "                end",
                "                if (!is_row_hit) begin",
                    f"                    accepted_bank_cmd_valid_q[{bank_select_signal}] <= 1'b1;",
                "                end",
            "                service_pending_q <= 1'b1;",
            "                service_is_write_q <= selected_req.is_write;",
            "                service_update_open_row_q <= accepted_slow;",
            "                service_prev_row_valid_q <= selected_row_open_valid;",
            f"                service_bank_q <= {bank_select_signal};",
            "                service_addr_q <= selected_addr;",
            "                service_wdata_q <= selected_req.wdata;",
            "                service_row_q <= requested_row;",
            "                service_prev_row_q <= selected_open_row;",
            "                if (accepted_hit) begin",
            "                    service_cycles_left_q <= SERVICE_COUNTER_WIDTH'(HIT_SERVICE_CYCLES - 1);",
            "                end else begin",
            "                    service_cycles_left_q <= SERVICE_COUNTER_WIDTH'(SLOW_SERVICE_CYCLES - 1);",
            "                end",
            "            end",
            "        end",
            "    end",
            "",
            "    ddr4_request_queue #(.DEPTH(REQ_QUEUE_DEPTH)) u_request_queue (",
            "        .clk(clk),",
            "        .rst_n(rst_n),",
            "        .enq_valid(enqueue_req_valid),",
            "        .enq_req(decoded_req_packed),",
            "        .enq_ready(enq_ready),",
            "        .req_array(req_array_packed),",
            "        .req_valid(req_valid_packed),",
            "        .sel_idx(sel_idx),",
            "        .deq_en(deq_en)",
            "    );",
            "",
            "    ddr4_scheduler_scheduler u_scheduler (",
            "        .clk(clk),",
            "        .rst_n(rst_n),",
            f"        .ref_req({scheduler_ref_req}),",
            "        .bank_active(scheduler_bank_active),",
            "        .bank_open_row(scheduler_bank_open_row_packed),",
            "        .req_array(req_array_packed),",
            "        .req_valid(req_valid_packed),",
            "        .cmd_ready(downstream_cmd_ready),",
            f"        .timing_ok({scheduler_timing_expr}),",
            "        .sel_idx(sel_idx),",
            "        .issue_valid(issue_valid),",
            "        .issue_ref(issue_ref),",
            "        .issue_txn(issue_txn)",
            "    );",
        ]
    )

    for bank_index in range(bank_count):
        lines.extend(
            [
                "",
                f"    ddr4_bank_top u_bank{bank_index} (",
                "        .clk(clk),",
                "        .rst_n(rst_n),",
                f"        .cmd_valid(bank_cmd_valid[{bank_index}]),",
                f"        .cmd_type(bank{bank_index}_cmd_type),",
                f"        .bank_idle(bank_bank_idle[{bank_index}]),",
                f"        .bank_active(bank_bank_active[{bank_index}]),",
                f"        .cmd_ready(bank_cmd_ready[{bank_index}]),",
                f"        .activating(bank_activating[{bank_index}])",
                "    );",
            ]
        )

    if has_refresh:
        lines.extend(
            [
                "",
                "    ddr4_refresh_refresh_controller u_refresh_controller (",
                "        .clk(clk),",
                "        .rst_n(rst_n),",
                "        .ref_ack(ref_ack),",
                "        .ref_req(ref_req)",
                "    );",
            ]
        )

    if has_tfaw:
        lines.extend(
            [
                "",
                "    ddr4_tFAW_tFAW_tracker u_tFAW_tracker (",
                "        .clk(clk),",
                "        .rst_n(rst_n),",
                "        .act_pulse(act_pulse),",
                "        .act_count(tFAW_act_count),",
                "        .tFAW_block(tFAW_block),",
                "        .tFAW_ok(tFAW_ok)",
                "    );",
            ]
        )

    if has_trrd:
        lines.extend(
            [
                "",
                "    ddr4_tRRD_simple_tRRD u_tRRD (",
                "        .clk(clk),",
                "        .rst_n(rst_n),",
                "        .act_pulse(act_pulse),",
                "        .tRRD_block(tRRD_block)",
                "    );",
            ]
        )

    lines.extend(["", "endmodule  // ddr4_controller_top", ""])
    return "\n".join(lines)


def build_wrapper_rtl(available_modules: set[str], bank_count: int) -> str:
    """Return the wrapper RTL assembled from the currently generated module set."""
    return build_phase1_row_buffer_wrapper_rtl(available_modules, bank_count)

    has_refresh = OPTIONAL_MODULES["refresh"] in available_modules
    has_tfaw = OPTIONAL_MODULES["tfaw"] in available_modules
    has_trrd = OPTIONAL_MODULES["trrd"] in available_modules
    scheduler_mode = load_scheduler_mode()
    scheduler_ref_req = "ref_req" if has_refresh else "1'b0"
    bank_vector_range = f"[{bank_count - 1}:0]"
    txn_bank_range = bank_select_range(bank_count)

    if bank_count > 1 and scheduler_mode != "simple":
        raise RuntimeError(
            "Unsupported configuration: multi-bank controller generation currently requires the simple scheduler"
        )

    lines = [
        "`timescale 1ns/1ps",
        "// ============================================================",
        "// Auto-generated by generate_wrapper.py -- DO NOT EDIT",
        "// Design : ddr4_controller_top",
        f"// Scheduler policy : {scheduler_mode}",
        f"// Bank count       : {bank_count}",
        "// ============================================================",
        "",
        "module ddr4_controller_top (",
        "    input  logic clk,",
        "    input  logic rst_n,",
        "    input  logic txn_valid,",
        "    input  logic txn_is_write,",
        f"    input  logic [{MEMORY_ADDR_WIDTH - 1}:0] txn_addr,",
        f"    input  logic [{MEMORY_DATA_WIDTH - 1}:0] txn_wdata,",
    ]
    if bank_count > 1:
        lines.append(f"    input  logic {txn_bank_range}txn_bank,")
    lines.extend(
        [
            "    output logic cmd_ready,",
            "    output logic rsp_valid,",
            f"    output logic [{MEMORY_DATA_WIDTH - 1}:0] rsp_rdata",
            ");",
            "",
            f"    localparam int BANK_COUNT = {bank_count};",
            f"    localparam int ADDR_WIDTH = {MEMORY_ADDR_WIDTH};",
            f"    localparam int DATA_WIDTH = {MEMORY_DATA_WIDTH};",
            "    localparam int MEM_DEPTH = 1 << ADDR_WIDTH;",
            "",
            "    logic issue_ref;",
            "    logic issue_txn;",
            "    logic act_allowed;",
            "    logic act_pulse;",
            "    logic selected_bank_cmd_valid;",
            "    logic accepted_read;",
            "    logic accepted_write;",
            "    logic [1:0] txn_cmd_type;",
            "    logic selected_bank_cmd_ready;",
            "    logic [DATA_WIDTH-1:0] selected_bank_rdata;",
            "    logic read_rsp_pending_q;",
            "    logic [DATA_WIDTH-1:0] read_rsp_data_q;",
            "    logic rsp_valid_q;",
            "    logic [DATA_WIDTH-1:0] rsp_rdata_q;",
            f"    logic {bank_vector_range} bank_cmd_valid;",
            f"    logic {bank_vector_range} bank_cmd_ready;",
            f"    logic {bank_vector_range} bank_bank_idle;",
            f"    logic {bank_vector_range} bank_bank_active;",
            f"    logic {bank_vector_range} bank_activating;",
            f"    logic {bank_vector_range} bank_prev_activating;",
            f"    logic {bank_vector_range} bank_act_pulse;",
            "    logic [DATA_WIDTH-1:0] bank_mem [0:BANK_COUNT-1][0:MEM_DEPTH-1];",
            "    integer bank_mem_bank;",
            "    integer bank_mem_addr;",
        ]
    )
    for bank_index in range(bank_count):
        lines.append(f"    logic [1:0] bank{bank_index}_cmd_type;")

    if has_refresh:
        lines.extend(
            [
                "    logic ref_req;",
                "    logic ref_ack;",
            ]
        )
    if has_tfaw:
        lines.append("    logic tFAW_ok;")
    if has_trrd:
        lines.append("    logic tRRD_block;")

    tfaw_allow_expr = "tFAW_ok" if has_tfaw else "1'b1"
    trrd_block_expr = "tRRD_block" if has_trrd else "1'b0"
    lines.extend(
        [
            "",
            f"    assign act_allowed = {tfaw_allow_expr} & ~{trrd_block_expr};",
            f"    assign txn_cmd_type = txn_is_write ? {typed_cmd_literal(True)} : {typed_cmd_literal(False)};",
        ]
    )

    if bank_count > 1:
        lines.extend(
            [
                "",
                "    always_comb begin",
                "        selected_bank_cmd_ready = 1'b0;",
                "        selected_bank_cmd_valid = 1'b0;",
                "        selected_bank_rdata = '0;",
                "        unique case (txn_bank)",
            ]
        )
        for bank_index in range(bank_count):
            lines.append(
                f"            {bank_select_literal(bank_count, bank_index)}: begin"
            )
            lines.append(
                f"                selected_bank_cmd_ready = bank_cmd_ready[{bank_index}];"
            )
            lines.append(
                f"                selected_bank_cmd_valid = bank_cmd_valid[{bank_index}];"
            )
            lines.append(
                f"                selected_bank_rdata = bank_mem[{bank_index}][txn_addr];"
            )
            lines.append("            end")
        lines.extend(
            [
                "            default: begin",
                "                selected_bank_cmd_ready = 1'b0;",
                "                selected_bank_cmd_valid = 1'b0;",
                "                selected_bank_rdata = '0;",
                "            end",
                "        endcase",
                "    end",
            ]
        )
    else:
        lines.append("    assign selected_bank_cmd_ready = bank_cmd_ready[0];")
        lines.append("    assign selected_bank_cmd_valid = bank_cmd_valid[0];")
        lines.append("    assign selected_bank_rdata = bank_mem[0][txn_addr];")

    for bank_index in range(bank_count):
        if bank_count > 1:
            lines.append(
                f"    assign bank_cmd_valid[{bank_index}] = issue_txn & act_allowed & (txn_bank == {bank_select_literal(bank_count, bank_index)});"
            )
        else:
            lines.append(f"    assign bank_cmd_valid[{bank_index}] = issue_txn & act_allowed;")
        lines.append(
            f"    assign bank{bank_index}_cmd_type = bank_cmd_valid[{bank_index}] ? txn_cmd_type : 2'b00;"
        )
        lines.append(
            f"    assign bank_act_pulse[{bank_index}] = bank_activating[{bank_index}] & ~bank_prev_activating[{bank_index}];"
        )

    lines.extend(
        [
            "    assign cmd_ready = selected_bank_cmd_ready;",
            "    assign accepted_read = selected_bank_cmd_valid & ~txn_is_write;",
            "    assign accepted_write = selected_bank_cmd_valid & txn_is_write;",
            "    assign rsp_valid = rsp_valid_q;",
            "    assign rsp_rdata = rsp_rdata_q;",
            "    assign act_pulse = |bank_act_pulse;",
        ]
    )

    if has_refresh:
        lines.append("    assign ref_ack = issue_ref;")

    lines.extend(
        [
            "",
            "    always_ff @(posedge clk) begin",
            "        if (!rst_n) begin",
            "            bank_prev_activating <= '0;",
            "            read_rsp_pending_q <= 1'b0;",
            "            read_rsp_data_q <= '0;",
            "            rsp_valid_q <= 1'b0;",
            "            rsp_rdata_q <= '0;",
            "            for (bank_mem_bank = 0; bank_mem_bank < BANK_COUNT; bank_mem_bank = bank_mem_bank + 1) begin",
            "                for (bank_mem_addr = 0; bank_mem_addr < MEM_DEPTH; bank_mem_addr = bank_mem_addr + 1) begin",
            "                    bank_mem[bank_mem_bank][bank_mem_addr] <= '0;",
            "                end",
            "            end",
            "        end else begin",
            "            bank_prev_activating <= bank_activating;",
            "            rsp_valid_q <= read_rsp_pending_q;",
            "            if (read_rsp_pending_q) begin",
            "                rsp_rdata_q <= read_rsp_data_q;",
            "            end",
            "            read_rsp_pending_q <= accepted_read;",
            "            if (accepted_read) begin",
            "                read_rsp_data_q <= selected_bank_rdata;",
            "            end",
        ]
    )
    for bank_index in range(bank_count):
        lines.extend(
            [
                f"            if (bank_cmd_valid[{bank_index}] && txn_is_write) begin",
                f"                bank_mem[{bank_index}][txn_addr] <= txn_wdata;",
                "            end",
            ]
        )
    lines.extend(
        [
            "        end",
            "    end",
            "",
            "    ddr4_scheduler_scheduler u_scheduler (",
            "        .clk(clk),",
            "        .rst_n(rst_n),",
            f"        .ref_req({scheduler_ref_req}),",
            "        .cmd_ready(cmd_ready),",
            "        .issue_ref(issue_ref),",
            "        .issue_txn(issue_txn)",
            "    );",
        ]
    )
    for bank_index in range(bank_count):
        lines.extend(
            [
                "",
                f"    ddr4_bank_top u_bank{bank_index} (",
                "        .clk(clk),",
                "        .rst_n(rst_n),",
                f"        .cmd_valid(bank_cmd_valid[{bank_index}]),",
                f"        .cmd_type(bank{bank_index}_cmd_type),",
                f"        .bank_idle(bank_bank_idle[{bank_index}]),",
                f"        .bank_active(bank_bank_active[{bank_index}]),",
                f"        .cmd_ready(bank_cmd_ready[{bank_index}]),",
                f"        .activating(bank_activating[{bank_index}])",
                "    );",
            ]
        )

    if has_refresh:
        lines.extend(
            [
                "",
                "    ddr4_refresh_refresh_controller u_refresh_controller (",
                "        .clk(clk),",
                "        .rst_n(rst_n),",
                "        .ref_ack(ref_ack),",
                "        .ref_req(ref_req)",
                "    );",
            ]
        )

    if has_tfaw:
        lines.extend(
            [
                "",
                "    ddr4_tFAW_tFAW_tracker u_tFAW_tracker (",
                "        .clk(clk),",
                "        .rst_n(rst_n),",
                "        .act_pulse(act_pulse),",
                "        .act_count(),",
                "        .tFAW_block(),",
                "        .tFAW_ok(tFAW_ok)",
                "    );",
            ]
        )

    if has_trrd:
        lines.extend(
            [
                "",
                "    ddr4_tRRD_simple_tRRD u_tRRD (",
                "        .clk(clk),",
                "        .rst_n(rst_n),",
                "        .act_pulse(act_pulse),",
                "        .tRRD_block(tRRD_block)",
                "    );",
            ]
        )

    lines.extend(["", "endmodule  // ddr4_controller_top", ""])
    return "\n".join(lines)


def write_module(path: Path, rtl: str) -> None:
    """Write one generated SystemVerilog module to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rtl, encoding="utf-8")


def resolve_rtl_output_dir(output_path: Path) -> Path:
    """Infer the rtl_output package directory from the generated wrapper path."""
    for parent in (output_path.parent, *output_path.parents):
        if parent.name == "rtl_output":
            return parent
    return output_path.parent


def bank_top_output_path(output_path: Path) -> Path:
    """Return the generated bank_top path colocated with the wrapper package."""
    rtl_output_dir = resolve_rtl_output_dir(output_path)
    return rtl_output_dir / f"{BANK_TOP_MODULE_NAME}.sv"


def discover_rtl_files(rtl_output_dir: Path) -> list[Path]:
    """Return all generated SystemVerilog files relative to rtl_output."""
    return sorted(
        path.relative_to(rtl_output_dir)
        for path in rtl_output_dir.rglob("*.sv")
        if path.is_file()
    )


def write_filelist(rtl_output_dir: Path, rtl_files: list[Path]) -> Path:
    """Write a relative-path filelist for the generated RTL package."""
    filelist_path = rtl_output_dir / "filelist.f"
    contents = "\n".join(path.as_posix() for path in rtl_files)
    if contents:
        contents += "\n"
    filelist_path.write_text(contents, encoding="utf-8")
    return filelist_path


def write_manifest(rtl_output_dir: Path, rtl_files: list[Path], filelist_path: Path) -> Path:
    """Write a compact JSON manifest for the generated RTL package."""
    manifest_path = rtl_output_dir / "manifest.json"
    manifest = {
        "design_name": MODULE_NAME,
        "top_module": MODULE_NAME,
        "modules": sorted({path.stem for path in rtl_files}),
        "filelist": filelist_path.relative_to(rtl_output_dir).as_posix(),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest_path


def build_readme(
    rtl_files: list[Path],
    filelist_path: Path,
    has_refresh: bool,
    has_tfaw: bool,
    has_trrd: bool,
    scheduler_mode: str,
    bank_count: int,
    page_policy: str,
) -> str:
    """Build a concise handoff README for the generated RTL package."""
    file_count = len(rtl_files)
    module_count = len({path.stem for path in rtl_files})
    io_lines = "\n".join(
        f"- `{name}` ({direction}): {description}"
        for name, direction, description in build_io_descriptions(bank_count)
    )
    feature_lines = "\n".join(
        f"- {item}"
        for item in build_feature_summary(
            has_refresh,
            has_tfaw,
            has_trrd,
            scheduler_mode,
            bank_count,
            page_policy,
        )
    )
    simplification_lines = "\n".join(
        f"- {item}" for item in build_known_simplifications(bank_count, page_policy)
    )
    module_lines = "\n".join(f"- `{path.stem}`" for path in rtl_files)

    return f"""# DDR4 Controller RTL Handoff

## Design Description
`ddr4_controller_top` is a generated top-level DDR4 controller wrapper that combines the selected scheduler policy, {bank_count} bank integration path(s), and any enabled optional timing modules into one integration point.

Generated page policy: `{page_policy}`.

## Compile Instructions
Compile the package from the `rtl_output/` directory using the generated filelist:

```sh
iverilog -g2012 -s {MODULE_NAME} -f {filelist_path.name}
```

## Top Module
- `{MODULE_NAME}`

## IO Description
{io_lines}

## Feature Summary
{feature_lines}

## Generated Modules
- {module_count} modules across {file_count} SystemVerilog files
{module_lines}

## Known Simplifications
{simplification_lines}
"""


def write_readme(
    rtl_output_dir: Path,
    rtl_files: list[Path],
    filelist_path: Path,
    has_refresh: bool,
    has_tfaw: bool,
    has_trrd: bool,
    scheduler_mode: str,
    bank_count: int,
    page_policy: str,
) -> Path:
    """Write the handoff README for the generated RTL package."""
    readme_path = rtl_output_dir / "README.md"
    readme_path.write_text(
        build_readme(
            rtl_files,
            filelist_path,
            has_refresh,
            has_tfaw,
            has_trrd,
            scheduler_mode,
            bank_count,
            page_policy,
        ),
        encoding="utf-8",
    )
    return readme_path


def write_handoff_package(
    output_path: Path,
    has_refresh: bool,
    has_tfaw: bool,
    has_trrd: bool,
    scheduler_mode: str,
    bank_count: int,
    page_policy: str,
) -> dict[str, Path]:
    """Write filelist, manifest, and README for the generated RTL package."""
    rtl_output_dir = resolve_rtl_output_dir(output_path)
    rtl_files = discover_rtl_files(rtl_output_dir)
    filelist_path = write_filelist(rtl_output_dir, rtl_files)
    manifest_path = write_manifest(rtl_output_dir, rtl_files, filelist_path)
    readme_path = write_readme(
        rtl_output_dir,
        rtl_files,
        filelist_path,
        has_refresh,
        has_tfaw,
        has_trrd,
        scheduler_mode,
        bank_count,
        page_policy,
    )
    return {
        "rtl_output_dir": rtl_output_dir,
        "filelist": filelist_path,
        "manifest": manifest_path,
        "readme": readme_path,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate the DDR4 controller top-level wrapper."
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Output SystemVerilog path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Print the generated RTL to stdout after writing the file.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_path = args.output.expanduser().resolve()
    bank_count = load_bank_count()

    available_modules = discover_available_modules()
    try:
        ensure_required_modules(available_modules)
    except RuntimeError as exc:
        print(f"[generate_wrapper] FAIL: {exc}")
        return 1

    validation_modules = [*CORE_MODULES]
    for module_name in OPTIONAL_MODULES.values():
        if module_name in available_modules:
            validation_modules.append(module_name)

    print("[check_interfaces] Running interface validation...")
    try:
        check_interfaces(validation_modules, root_dir=SCRIPT_DIR)
        print("[check_interfaces] PASS")
    except Exception as exc:
        print(f"[check_interfaces] FAIL: {exc}")
        return 1

    try:
        rtl = build_wrapper_rtl(available_modules, bank_count)
    except RuntimeError as exc:
        print(f"[generate_wrapper] FAIL: {exc}")
        return 1

    bank_top_path = bank_top_output_path(output_path)
    write_module(bank_top_path, build_bank_top_rtl())
    write_module(output_path, rtl)

    has_refresh = OPTIONAL_MODULES["refresh"] in available_modules
    has_tfaw = OPTIONAL_MODULES["tfaw"] in available_modules
    has_trrd = OPTIONAL_MODULES["trrd"] in available_modules
    scheduler_mode = load_scheduler_mode()
    page_policy = load_page_policy()
    handoff_paths = write_handoff_package(
        output_path,
        has_refresh,
        has_tfaw,
        has_trrd,
        scheduler_mode,
        bank_count,
        page_policy,
    )

    print(f"[INFO] Wrote bank_top RTL to {bank_top_path}")
    print(f"[INFO] Wrote wrapper RTL to {output_path}")
    print(f"[INFO] Wrote filelist to {handoff_paths['filelist']}")
    print(f"[INFO] Wrote manifest to {handoff_paths['manifest']}")
    print(f"[INFO] Wrote README to {handoff_paths['readme']}")
    if args.stdout:
        print()
        print(rtl, end="")
    print("[generate_wrapper] Wrapper generation complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
