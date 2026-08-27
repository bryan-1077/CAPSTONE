#!/usr/bin/env python3
"""Generate a deterministic self-checking SystemVerilog testbench."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import yaml


SCRIPT_DIR = Path(__file__).resolve().parent
RTL_OUTPUT_DIR = SCRIPT_DIR / "rtl_output"
IR_DIR = SCRIPT_DIR / "ir"
MANIFEST_PATH = RTL_OUTPUT_DIR / "manifest.json"
TOP_RTL_TEMPLATE = RTL_OUTPUT_DIR / "{top_module}.sv"
GENERATED_BANK_INPUT_PATH = SCRIPT_DIR / "inputs" / "generated" / "ddr4_bank.yaml"
USER_CONFIG_PATH = SCRIPT_DIR / "configs" / "user_input.yaml"
SUPPORTED_BANK_COUNTS = {1, 2, 4}
SUPPORTED_PAGE_POLICIES = {"open_page", "close_page"}
DEFAULT_PAGE_POLICY = "open_page"
MEMORY_ADDR_WIDTH = 4
MEMORY_DATA_WIDTH = 32
READ_RESPONSE_LATENCY_CYCLES = 1
LOCALITY_PATTERN_PAIRS = 16
TIMING_STRESS_OPS = 32
PATTERN_DETAIL_LOG_LIMIT = 6


def bank_select_width(bank_count: int) -> int:
    """Return the txn_bank width required to address the configured bank count."""
    if bank_count <= 1:
        return 0
    return (bank_count - 1).bit_length()


def bank_vector_literal(bank_count: int, asserted_indices: set[int]) -> str:
    """Return a packed bank bitmap literal with bit 0 mapped to bank 0."""
    bits = ["1" if bank_index in asserted_indices else "0" for bank_index in range(bank_count)]
    return f"{bank_count}'b{''.join(reversed(bits))}"


def cmd_type_literal(is_write: bool) -> str:
    """Return the expected bank cmd_type literal for one read/write transaction."""
    return "2'b10" if is_write else "2'b01"


def build_multibank_cmd_type_checks(
    bank_count: int,
    selected_expr: str,
    expected_expr: str,
    indent: str = "            ",
) -> list[str]:
    """Return per-bank cmd_type checks for one selected-bank issue attempt."""
    lines: list[str] = []
    for bank_index in range(bank_count):
        lines.append(f"{indent}if ({selected_expr} == {bank_index}) begin")
        lines.append(
            f'{indent}    `CHECK(dut.bank{bank_index}_cmd_type == {expected_expr}, '
            f'"Selected bank {bank_index} did not preserve the expected cmd_type")'
        )
        lines.append(f"{indent}end else begin")
        lines.append(
            f'{indent}    `CHECK(dut.bank{bank_index}_cmd_type == 2\'b00, '
            f'"Non-selected bank {bank_index} should not observe a command type")'
        )
        lines.append(f"{indent}end")
    return lines


def build_all_bank_cmd_type_zero_checks(
    bank_count: int,
    indent: str = "        ",
) -> list[str]:
    """Return checks proving that no bank observed a command type."""
    return [
        f'{indent}`CHECK(dut.bank{bank_index}_cmd_type == 2\'b00, '
        f'"Bank {bank_index} should not observe a command type")'
        for bank_index in range(bank_count)
    ]


@dataclass(frozen=True)
class DesignContext:
    """Summarize the generated design features visible to the testbench generator."""

    modules: frozenset[str]
    ports: frozenset[str]
    top_rtl_text: str
    bank_count: int
    scheduler_mode: str | None
    page_policy: str
    activate_fsm_instance: str | None
    refresh_instance: str | None
    tfaw_instance: str | None
    trrd_instance: str | None
    refresh_cycles: int | None
    tfaw_cycles: int | None
    tfaw_limit: int | None
    tRCD_cycles: int | None
    tRAS_cycles: int | None
    tRP_cycles: int | None

    def has_port(self, port_name: str) -> bool:
        return port_name in self.ports

    def has_identifier(self, name: str) -> bool:
        return bool(self.top_rtl_text) and re.search(rf"\b{re.escape(name)}\b", self.top_rtl_text) is not None

    @property
    def has_handshake(self) -> bool:
        if not self.top_rtl_text:
            return True
        return self.has_port("txn_valid") and self.has_port("cmd_ready")

    @property
    def has_scheduler(self) -> bool:
        return "ddr4_scheduler_scheduler" in self.modules or self.scheduler_mode is not None

    @property
    def is_close_page(self) -> bool:
        return self.page_policy == "close_page"

    @property
    def has_refresh_request(self) -> bool:
        return self.refresh_instance is not None and self.has_identifier("ref_req")

    @property
    def has_activate_fsm(self) -> bool:
        return self.activate_fsm_instance is not None

    @property
    def has_tfaw(self) -> bool:
        return (
            self.tfaw_instance is not None
            and self.has_identifier("issue_txn")
        )

    @property
    def has_trrd(self) -> bool:
        return (
            self.trrd_instance is not None
            and self.has_identifier("act_pulse")
        )

    @property
    def tfaw_limit_reachable(self) -> bool:
        """Return False only when the generated timing IR proves the tFAW limit is unreachable."""
        if self.tfaw_cycles is None or self.tfaw_limit is None:
            return True

        if self.bank_count < self.tfaw_limit:
            return False

        if self.tfaw_limit <= 1:
            return True

        timing_terms = (self.tRCD_cycles, self.tRAS_cycles, self.tRP_cycles)
        if any(value is None for value in timing_terms):
            return True

        min_activate_spacing = sum(value for value in timing_terms if value is not None)
        return ((self.tfaw_limit - 1) * min_activate_spacing) < self.tfaw_cycles


def load_manifest_modules() -> frozenset[str]:
    """Load the generated RTL manifest modules when available."""
    if not MANIFEST_PATH.is_file():
        return frozenset()

    try:
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return frozenset()

    modules = manifest.get("modules", [])
    if not isinstance(modules, list):
        return frozenset()
    return frozenset(str(module_name) for module_name in modules)


def parse_ports(top_rtl_text: str) -> frozenset[str]:
    """Collect declared top-level port names from the generated wrapper RTL."""
    ports: set[str] = set()
    port_pattern = re.compile(
        r"^\s*(?:input|output)\s+(?:logic\s+)?(?:\[[^]]+\]\s+)?([A-Za-z_]\w*)\s*(?:,|$)",
        re.MULTILINE,
    )
    for match in port_pattern.finditer(top_rtl_text):
        ports.add(match.group(1))
    return frozenset(ports)


def load_ir_json(filename: str) -> dict:
    """Load a generated IR JSON file and return an empty mapping on absence/parse failure."""
    path = IR_DIR / filename
    if not path.is_file():
        return {}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}

    return data if isinstance(data, dict) else {}


def load_yaml(path: Path) -> dict:
    """Load a YAML mapping and return an empty mapping on failure."""
    if not path.is_file():
        return {}

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return {}

    return data if isinstance(data, dict) else {}


def extract_tfaw_parameters() -> tuple[int | None, int | None]:
    """Extract the generated tFAW window and limit parameters from IR metadata."""
    ir_data = load_ir_json("ddr4_tFAW_tFAW_tracker_ir.json")
    behavior = ir_data.get("metadata", {}).get("behavior", {})
    rtl_parameters = behavior.get("rtl_parameters", {})

    tfaw_cycles = rtl_parameters.get("TFAW_CYCLES")
    tfaw_limit = rtl_parameters.get("TFAW_LIMIT")

    if not isinstance(tfaw_cycles, int):
        tfaw_cycles = None
    if not isinstance(tfaw_limit, int):
        tfaw_limit = None

    return tfaw_cycles, tfaw_limit


def extract_scheduler_mode() -> str | None:
    """Extract the generated scheduler mode from the scheduler IR metadata."""
    ir_data = load_ir_json("ddr4_scheduler_scheduler_ir.json")
    scheduler_mode = ir_data.get("metadata", {}).get("scheduler_mode")
    if scheduler_mode in {"simple", "round_robin"}:
        return str(scheduler_mode)
    return None


def extract_page_policy(top_rtl_text: str) -> str:
    """Extract the active page policy from generated metadata or config."""
    wrapper_match = re.search(
        r"^\s*//\s*Page policy\s*:\s*(open_page|close_page)\s*$",
        top_rtl_text,
        re.MULTILINE,
    )
    if wrapper_match is not None:
        return str(wrapper_match.group(1))

    generated_cfg = load_yaml(GENERATED_BANK_INPUT_PATH).get("controller_config", {})
    generated_policy = generated_cfg.get("page_policy")
    if generated_policy in SUPPORTED_PAGE_POLICIES:
        return str(generated_policy)

    user_policy = load_yaml(USER_CONFIG_PATH).get("features", {}).get("page_policy")
    if user_policy in SUPPORTED_PAGE_POLICIES:
        return str(user_policy)

    return DEFAULT_PAGE_POLICY


def extract_bank_count(top_rtl_text: str) -> int:
    """Extract the generated bank count from wrapper ports or generated config."""
    width_match = re.search(
        r"^\s*input\s+logic\s+\[(\d+):0\]\s+txn_bank\s*(?:,|$)",
        top_rtl_text,
        re.MULTILINE,
    )
    if width_match is not None:
        width = int(width_match.group(1)) + 1
        if width == 1:
            return 2
        if width == 2:
            return 4

    if re.search(r"^\s*input\s+logic\s+txn_bank\s*(?:,|$)", top_rtl_text, re.MULTILINE):
        return 2

    generated_cfg = load_yaml(GENERATED_BANK_INPUT_PATH).get("controller_config", {})
    topology_cfg = generated_cfg.get("topology")
    if isinstance(topology_cfg, dict):
        bank_count = topology_cfg.get("banks")
        if isinstance(bank_count, int) and not isinstance(bank_count, bool):
            if bank_count in SUPPORTED_BANK_COUNTS:
                return bank_count

    user_topology = load_yaml(USER_CONFIG_PATH).get("topology")
    if isinstance(user_topology, dict):
        bank_count = user_topology.get("banks")
        if isinstance(bank_count, int) and not isinstance(bank_count, bool):
            if bank_count in SUPPORTED_BANK_COUNTS:
                return bank_count

    return 1


def extract_fsm_threshold(filename: str) -> int | None:
    """Extract the first numeric >= threshold from a generated FSM IR file."""
    ir_data = load_ir_json(filename)
    transitions = ir_data.get("fsm", {}).get("transitions", [])
    if not isinstance(transitions, list):
        return None

    for transition in transitions:
        if not isinstance(transition, dict):
            continue
        condition = transition.get("condition", {})
        if not isinstance(condition, dict):
            continue
        if condition.get("op") != ">=":
            continue
        right = condition.get("right", {})
        if not isinstance(right, dict):
            continue
        value = right.get("value")
        if isinstance(value, int):
            return value

    return None


def compute_refresh_wait_cycles(refresh_cycles: int | None) -> int:
    """Return a deterministic observation window for refresh request checks."""
    if refresh_cycles is None:
        return 0
    return max(refresh_cycles + 32, 8200)


def find_instance_name(top_rtl_text: str, module_name: str) -> str | None:
    """Return the instantiated name for a known module type, if present."""
    match = re.search(
        rf"(?m)^\s*{re.escape(module_name)}\s+([A-Za-z_]\w*)\s*\(",
        top_rtl_text,
    )
    if match is None:
        return None
    return match.group(1)


def load_design_context(top_module: str) -> DesignContext:
    """Inspect the generated wrapper so testbench logic only references available features."""
    top_rtl_path = Path(str(TOP_RTL_TEMPLATE).format(top_module=top_module))
    top_rtl_text = top_rtl_path.read_text(encoding="utf-8") if top_rtl_path.is_file() else ""
    modules = load_manifest_modules()
    tfaw_cycles, tfaw_limit = extract_tfaw_parameters()

    return DesignContext(
        modules=modules,
        ports=parse_ports(top_rtl_text),
        top_rtl_text=top_rtl_text,
        bank_count=extract_bank_count(top_rtl_text),
        scheduler_mode=extract_scheduler_mode(),
        page_policy=extract_page_policy(top_rtl_text),
        activate_fsm_instance=find_instance_name(top_rtl_text, "ddr4_bank_activate_fsm"),
        refresh_instance=find_instance_name(top_rtl_text, "ddr4_refresh_refresh_controller"),
        tfaw_instance=find_instance_name(top_rtl_text, "ddr4_tFAW_tFAW_tracker"),
        trrd_instance=find_instance_name(top_rtl_text, "ddr4_tRRD_simple_tRRD"),
        refresh_cycles=extract_fsm_threshold("ddr4_refresh_refresh_controller_ir.json"),
        tfaw_cycles=tfaw_cycles,
        tfaw_limit=tfaw_limit,
        tRCD_cycles=extract_fsm_threshold("ddr4_bank_activate_fsm_ir.json"),
        tRAS_cycles=extract_fsm_threshold("ddr4_bank_tRAS_fsm_ir.json"),
        tRP_cycles=extract_fsm_threshold("ddr4_bank_precharge_fsm_ir.json"),
    )


def join_lines(lines: list[str]) -> str:
    """Join lines with newlines, preserving intentional blank lines."""
    return "\n".join(lines)


def build_phase1_row_buffer_testbench(top_module: str, design: DesignContext) -> str:
    """Return a Phase 1 row-buffer-aware deterministic testbench."""
    tb_module = f"tb_{top_module}"
    refresh_wait_cycles = compute_refresh_wait_cycles(design.refresh_cycles)
    txn_bank_width = max(bank_select_width(design.bank_count), 1)
    row_width = max(MEMORY_ADDR_WIDTH // 2, 1)
    col_width = MEMORY_ADDR_WIDTH - row_width

    bank_signal_decl = f"    logic [{txn_bank_width - 1}:0] txn_bank;" if design.bank_count > 1 else ""
    bank_dut_port = "        .txn_bank(txn_bank)," if design.bank_count > 1 else ""
    monitor_bank_expr = "txn_bank" if design.bank_count > 1 else "0"
    tfaw_timing_guard = "!dut.tRRD_block" if design.has_trrd else "1'b1"

    scheduler_decl_block = ""
    scheduler_task_block = ""
    scheduler_sequence_block = ""
    scheduler_monitor_block = ""
    scheduler_init_block = ""
    tfaw_state_decl = ""
    tfaw_init_block = ""
    tfaw_reset_block = ""
    tfaw_monitor_block = ""
    if design.has_scheduler:
        scheduler_decl_block = """
    /*verilator tracing_off*/
    logic sched_ref_req;
    logic sched_cmd_ready;
    logic sched_issue_ref;
    logic sched_issue_txn;
    logic [1:0] sched_sel_idx;
    logic sched_issue_valid;
    typedef struct packed {
        logic [1:0]  bank;
        logic [9:0]  row;
        logic [5:0]  col;
        logic        is_write;
        logic [31:0] wdata;
    } sched_request_t;
    sched_request_t sched_req_array [4];
    logic sched_req_valid [4];
    logic [3:0] sched_bank_active;
    logic [9:0] sched_bank_open_row [4];
    logic [203:0] sched_req_array_packed;
    logic [3:0] sched_req_valid_packed;
    logic [39:0] sched_bank_open_row_packed;

    assign sched_req_array_packed[50:0] = sched_req_array[0];
    assign sched_req_array_packed[101:51] = sched_req_array[1];
    assign sched_req_array_packed[152:102] = sched_req_array[2];
    assign sched_req_array_packed[203:153] = sched_req_array[3];
    assign sched_req_valid_packed = {sched_req_valid[3], sched_req_valid[2], sched_req_valid[1], sched_req_valid[0]};
    assign sched_bank_open_row_packed[9:0] = sched_bank_open_row[0];
    assign sched_bank_open_row_packed[19:10] = sched_bank_open_row[1];
    assign sched_bank_open_row_packed[29:20] = sched_bank_open_row[2];
    assign sched_bank_open_row_packed[39:30] = sched_bank_open_row[3];

    ddr4_scheduler_scheduler sched_policy_dut (
        .clk(clk),
        .rst_n(rst_n),
        .ref_req(sched_ref_req),
        .bank_active(sched_bank_active),
        .bank_open_row(sched_bank_open_row_packed),
        .req_array(sched_req_array_packed),
        .req_valid(sched_req_valid_packed),
        .cmd_ready(sched_cmd_ready),
        .timing_ok(1'b1),
        .sel_idx(sched_sel_idx),
        .issue_valid(sched_issue_valid),
        .issue_ref(sched_issue_ref),
        .issue_txn(sched_issue_txn)
    );
    /*verilator tracing_on*/
"""
        scheduler_task_block = """
    task automatic scheduler_idle_cycle;
        begin
            @(negedge clk);
            sched_ref_req = 1'b0;
            sched_req_valid[0] = 1'b0;
            sched_req_valid[1] = 1'b0;
            sched_req_valid[2] = 1'b0;
            sched_req_valid[3] = 1'b0;
            sched_req_array[0] = '0;
            sched_req_array[1] = '0;
            sched_req_array[2] = '0;
            sched_req_array[3] = '0;
            sched_bank_active = 4'b0000;
            sched_bank_open_row[0] = '0;
            sched_bank_open_row[1] = '0;
            sched_bank_open_row[2] = '0;
            sched_bank_open_row[3] = '0;
            sched_cmd_ready = 1'b1;
            @(posedge clk);
            #1;
            `CHECK((sched_issue_ref == 1'b0) && (sched_issue_txn == 1'b0),
                   "Scheduler should be idle when no requests are asserted")
        end
    endtask

    task automatic scheduler_expect(
        input logic ref_req_i,
        input logic txn_valid_i,
        input logic cmd_ready_i,
        input logic exp_issue_ref,
        input logic exp_issue_txn,
        input string phase_msg
    );
        begin
            @(negedge clk);
            sched_ref_req = ref_req_i;
            sched_req_valid[0] = txn_valid_i;
            sched_req_valid[1] = 1'b0;
            sched_req_valid[2] = 1'b0;
            sched_req_valid[3] = 1'b0;
            sched_req_array[0] = '0;
            sched_req_array[1] = '0;
            sched_req_array[2] = '0;
            sched_req_array[3] = '0;
            sched_bank_active = 4'b0000;
            sched_bank_open_row[0] = '0;
            sched_bank_open_row[1] = '0;
            sched_bank_open_row[2] = '0;
            sched_bank_open_row[3] = '0;
            sched_cmd_ready = cmd_ready_i;
            @(posedge clk);
            #1;
            `INFO(phase_msg)
            `CHECK(sched_issue_ref == exp_issue_ref,
                   "Unexpected scheduler issue_ref result")
            `CHECK(sched_issue_txn == exp_issue_txn,
                   "Unexpected scheduler issue_txn result")
        end
    endtask

    task automatic scheduler_row_hit_policy_checks;
        begin
            scheduler_idle_cycle();

            @(negedge clk);
            sched_ref_req = 1'b0;
            sched_cmd_ready = 1'b1;
            sched_req_array[0] = '0;
            sched_req_array[1] = '0;
            sched_req_array[0].bank = 2'd0;
            sched_req_array[0].row = 10'd3;
            sched_req_array[1].bank = 2'd1;
            sched_req_array[1].row = 10'd9;
            sched_req_valid[0] = 1'b1;
            sched_req_valid[1] = 1'b1;
            sched_req_valid[2] = 1'b0;
            sched_req_valid[3] = 1'b0;
            sched_bank_active = 4'b0010;
            sched_bank_open_row[0] = 10'd0;
            sched_bank_open_row[1] = 10'd9;
            sched_bank_open_row[2] = 10'd0;
            sched_bank_open_row[3] = 10'd0;
            @(posedge clk);
            #1;
            `INFO("Scheduler row-hit priority: req[1] beats non-hit req[0]")
            `CHECK(sched_sel_idx == 2'd1,
                   "Scheduler should select row-hit req[1]")
            `CHECK(sched_issue_txn == 1'b1,
                   "Scheduler should issue the selected row-hit transaction")

            scheduler_idle_cycle();

            @(negedge clk);
            sched_ref_req = 1'b0;
            sched_cmd_ready = 1'b1;
            sched_req_array[0] = '0;
            sched_req_array[1] = '0;
            sched_req_array[0].bank = 2'd0;
            sched_req_array[0].row = 10'd3;
            sched_req_array[1].bank = 2'd1;
            sched_req_array[1].row = 10'd9;
            sched_req_valid[0] = 1'b1;
            sched_req_valid[1] = 1'b1;
            sched_req_valid[2] = 1'b0;
            sched_req_valid[3] = 1'b0;
            sched_bank_active = 4'b0000;
            sched_bank_open_row[0] = 10'd0;
            sched_bank_open_row[1] = 10'd0;
            sched_bank_open_row[2] = 10'd0;
            sched_bank_open_row[3] = 10'd0;
            @(posedge clk);
            #1;
            `INFO("Scheduler fallback: no row hits selects first valid")
            `CHECK(sched_sel_idx == 2'd0,
                   "Scheduler should fall back to req[0] when no row hits exist")

            scheduler_idle_cycle();

            @(negedge clk);
            sched_ref_req = 1'b0;
            sched_cmd_ready = 1'b0;
            sched_req_array[0] = '0;
            sched_req_array[1] = '0;
            sched_req_array[0].bank = 2'd0;
            sched_req_array[0].row = 10'd3;
            sched_req_valid[0] = 1'b1;
            sched_req_valid[1] = 1'b0;
            sched_req_valid[2] = 1'b0;
            sched_req_valid[3] = 1'b0;
            sched_bank_active = 4'b0000;
            sched_bank_open_row[0] = 10'd0;
            sched_bank_open_row[1] = 10'd0;
            sched_bank_open_row[2] = 10'd0;
            sched_bank_open_row[3] = 10'd0;
            @(posedge clk);
            #1;
            `INFO("Scheduler lock behavior: req[0] selected while blocked")
            `CHECK(sched_sel_idx == 2'd0,
                   "Scheduler should select req[0] before lock")

            @(negedge clk);
            sched_req_array[1].bank = 2'd1;
            sched_req_array[1].row = 10'd9;
            sched_req_valid[1] = 1'b1;
            sched_bank_active = 4'b0010;
            sched_bank_open_row[1] = 10'd9;
            @(posedge clk);
            #1;
            `INFO("Scheduler lock behavior: later row hit does not reselect")
            `CHECK(sched_sel_idx == 2'd0,
                   "Scheduler should keep locked req[0] when req[1] becomes a row hit")

            @(negedge clk);
            sched_cmd_ready = 1'b1;
            sched_req_valid[1] = 1'b0;
            @(posedge clk);
            #1;
            scheduler_idle_cycle();
        end
    endtask
"""
        if design.scheduler_mode == "round_robin":
            scheduler_sequence_block = """
        `INFO("Scheduler policy checks: round_robin")
        scheduler_idle_cycle();
        scheduler_expect(1'b1, 1'b0, 1'b1, 1'b1, 1'b0,
                         "Scheduler case 1: only refresh issues refresh");
        scheduler_idle_cycle();
        scheduler_expect(1'b0, 1'b1, 1'b1, 1'b0, 1'b1,
                         "Scheduler case 2: only transaction issues transaction");
        scheduler_idle_cycle();
        scheduler_expect(1'b1, 1'b1, 1'b0, 1'b0, 1'b0,
                         "Scheduler case 3: contention with cmd_ready low issues nothing");
        scheduler_expect(1'b1, 1'b1, 1'b1, 1'b1, 1'b0,
                         "Scheduler case 4: first successful contention issues refresh");
        scheduler_idle_cycle();
        scheduler_expect(1'b0, 1'b1, 1'b1, 1'b0, 1'b1,
                         "Scheduler case 5: uncontested transaction still issues transaction");
        scheduler_idle_cycle();
        scheduler_expect(1'b1, 1'b1, 1'b1, 1'b0, 1'b1,
                         "Scheduler case 6: next contention issues transaction after turn flip");
        scheduler_idle_cycle();
        scheduler_expect(1'b1, 1'b1, 1'b1, 1'b1, 1'b0,
                         "Scheduler case 7: following contention flips back to refresh");
        scheduler_idle_cycle();
        scheduler_row_hit_policy_checks();
"""
        else:
            scheduler_sequence_block = """
        `INFO("Scheduler policy checks: simple")
        scheduler_idle_cycle();
        scheduler_expect(1'b1, 1'b0, 1'b1, 1'b1, 1'b0,
                         "Scheduler case 1: only refresh issues refresh");
        scheduler_idle_cycle();
        scheduler_expect(1'b0, 1'b1, 1'b1, 1'b0, 1'b1,
                         "Scheduler case 2: only transaction issues transaction");
        scheduler_idle_cycle();
        scheduler_expect(1'b1, 1'b1, 1'b0, 1'b0, 1'b0,
                         "Scheduler case 3: contention with cmd_ready low issues nothing");
        scheduler_expect(1'b1, 1'b1, 1'b1, 1'b1, 1'b0,
                         "Scheduler case 4: contention grants refresh");
        scheduler_idle_cycle();
        scheduler_expect(1'b0, 1'b1, 1'b1, 1'b0, 1'b1,
                         "Scheduler case 5: uncontested transaction still issues transaction");
        scheduler_idle_cycle();
        scheduler_expect(1'b1, 1'b1, 1'b1, 1'b1, 1'b0,
                         "Scheduler case 6: later contention still grants refresh");
        scheduler_idle_cycle();
        scheduler_row_hit_policy_checks();
"""
        scheduler_monitor_block = """
    always @(posedge clk) begin
        if (rst_n) begin
            `CHECK(!(sched_issue_ref && sched_issue_txn),
                   "Scheduler issued refresh and transaction simultaneously")
        end
    end
"""
        scheduler_init_block = """
        sched_ref_req = 1'b0;
        sched_cmd_ready = 1'b0;
        sched_req_valid[0] = 1'b0;
        sched_req_valid[1] = 1'b0;
        sched_req_valid[2] = 1'b0;
        sched_req_valid[3] = 1'b0;
        sched_req_array[0] = '0;
        sched_req_array[1] = '0;
        sched_req_array[2] = '0;
        sched_req_array[3] = '0;
        sched_bank_active = 4'b0000;
        sched_bank_open_row[0] = '0;
        sched_bank_open_row[1] = '0;
        sched_bank_open_row[2] = '0;
        sched_bank_open_row[3] = '0;
"""

    tRRD_check_block = ""
    tRRD_coverage_line = ""
    tRRD_monitor_block = ""
    tRRD_log_block = ""
    if design.has_trrd:
        tRRD_check_block = """
    always @(posedge clk) begin
        if (rst_n && dut.u_tRRD.tRRD_block && dut.act_pulse) begin
            `CHECK(0, "tRRD violation: act during block")
        end
    end
"""
        tRRD_coverage_line = '            `CHECK(saw_tRRD_block, "tRRD block never occurred")\n'
        tRRD_monitor_block = """
            if (!saw_tRRD_block && dut.tRRD_block) begin
                saw_tRRD_block <= 1'b1;
            end
"""
        tRRD_log_block = """
    always @(posedge clk) begin
        if (rst_n && txn_valid && dut.tRRD_block && !dut.is_row_hit && !dut.accept_txn &&
            detail_logging_enabled()) begin
            $display("[tRRD ][id=%0d][cycle=%0d] BLOCKED activation",
                     active_txn_id, cycle);
        end
    end
"""

    tFAW_check_block = ""
    if design.has_tfaw:
        tFAW_check_block = """
    always @(posedge clk) begin
        if (rst_n && dut.accepted_slow) begin
            `CHECK(dut.tfaw_can_accept_act,
                   "tFAW admission violation: slow activate accepted when tFAW window was full")
        end
    end
"""
        tfaw_state_decl = """
    bit saw_tFAW_block = 0;
    int observed_tfaw_admission_stall_cycles = 0;
    int observed_tfaw_hard_block_cycles = 0;
"""
        tfaw_init_block = """
        saw_tFAW_block = 1'b0;
        observed_tfaw_admission_stall_cycles = 0;
        observed_tfaw_hard_block_cycles = 0;
"""
        tfaw_reset_block = """
            saw_tFAW_block <= 1'b0;
            observed_tfaw_admission_stall_cycles <= 0;
            observed_tfaw_hard_block_cycles <= 0;
"""
        tfaw_monitor_block = f"""
            if (!saw_tFAW_block && dut.tFAW_block) begin
                saw_tFAW_block <= 1'b1;
            end
            if (txn_valid && !cmd_ready && !dut.service_pending_q &&
                ({tfaw_timing_guard}) && !dut.tfaw_can_accept_act) begin
                observed_tfaw_admission_stall_cycles <= observed_tfaw_admission_stall_cycles + 1;
            end
            if (txn_valid && !cmd_ready && !dut.service_pending_q && dut.tFAW_block) begin
                observed_tfaw_hard_block_cycles <= observed_tfaw_hard_block_cycles + 1;
            end
"""

    refresh_sequence_block = ""
    if design.has_refresh_request:
        refresh_sequence_block = f"""
        refresh_start_cycle = cycle;
        while ((dut.ref_req !== 1'b1) &&
               ((cycle - refresh_start_cycle) < REFRESH_WAIT_CYCLES)) begin
            @(posedge clk);
        end
        `CHECK(dut.ref_req == 1'b1,
               "Refresh request was not observed within the allotted window")
"""

    trrd_sequence_block = ""
    if design.has_trrd:
        if design.is_close_page:
            trrd_sequence_block = """
        wait(dut.tRRD_block === 1'b1);
        expect_access_ready(0, 1, 2, 1'b0,
                            "Close-page removes row-hit reuse, so same-row traffic also stalls during shared tRRD blocking");
        expect_access_ready(0, 2, 0, 1'b0,
                            "Different-row traffic also stalls during shared tRRD blocking");
"""
        else:
            trrd_sequence_block = """
        wait(dut.tRRD_block === 1'b1);
        expect_access_ready(0, 1, 2, 1'b1,
                            "Row hit remains ready while shared tRRD blocking is active");
        expect_access_ready(0, 2, 0, 1'b0,
                            "Row miss stalls while shared tRRD blocking is active");
"""

    bank_isolation_block = ""
    if design.bank_count > 1:
        if design.is_close_page:
            bank_isolation_block = """
        log_phase("BANK ISOLATION");
        `INFO("Bank isolation phase")
        issue_write_and_wait_complete(1, 3, 1, DATA_WIDTH'(32'h33330003),
                                      ROW_CLASS_CLOSED, SLOW_SERVICE_CYCLES, bank1_closed_latency,
                                      "Closed-bank WRITE on bank 1 still stores data independently");
        check_row_state(0, 1'b0, 0, "Bank 0 remains closed while bank 1 services its own access");
        check_row_state(1, 1'b0, 0, "Close-page clears bank 1 immediately after service");
        issue_read_and_wait_response(1, 3, 1, DATA_WIDTH'(32'h33330003),
                                     ROW_CLASS_CLOSED, SLOW_SERVICE_CYCLES, bank1_hit_latency,
                                     "Bank 1 returns its own stored data even without row reuse");
        issue_read_and_wait_response(0, 1, 1, DATA_WIDTH'(32'h11110001),
                                     ROW_CLASS_CLOSED, SLOW_SERVICE_CYCLES, bank0_hit_latency_after_isolation,
                                     "Bank 0 data remains isolated from bank 1 traffic under close-page");
"""
        else:
            bank_isolation_block = """
        log_phase("BANK ISOLATION");
        `INFO("Bank isolation phase")
        issue_write_and_wait_complete(1, 3, 1, DATA_WIDTH'(32'h33330003),
                                      ROW_CLASS_CLOSED, SLOW_SERVICE_CYCLES, bank1_closed_latency,
                                      "Closed-bank WRITE on bank 1 opens an independent row");
        check_row_state(0, 1'b1, 1, "Bank 0 row state is preserved while bank 1 opens a row");
        check_row_state(1, 1'b1, 3, "Bank 1 tracks its own open row");
        issue_read_and_wait_response(1, 3, 1, DATA_WIDTH'(32'h33330003),
                                     ROW_CLASS_HIT, HIT_SERVICE_CYCLES, bank1_hit_latency,
                                     "Bank 1 row hit returns its own stored data");
        issue_read_and_wait_response(0, 1, 1, DATA_WIDTH'(32'h11110001),
                                     ROW_CLASS_HIT, HIT_SERVICE_CYCLES, bank0_hit_latency_after_isolation,
                                     "Bank 0 data and row-open state remain intact after bank 1 traffic");
"""

    if design.is_close_page:
        pattern_sequence_block = """
        log_phase("ACCESS PATTERNS");
        `INFO("Pattern comparison phase")
        check_row_state(0, 1'b0, 0, "Pattern comparison begins with bank 0 closed under close-page");

        log_test_pattern("HIGH LOCALITY");
        pattern_accept_start = dut.cnt_accept;
        pattern_hit_start = dut.cnt_row_hit;
        pattern_nonhit_start = dut.cnt_row_miss + dut.cnt_row_closed;
        pattern_hit_latency_total_start = dut.cnt_latency_hit_total;
        pattern_nonhit_latency_total_start = dut.cnt_latency_nonhit_total;
        pattern_hit_count_start = dut.cnt_latency_hit_count;
        pattern_nonhit_count_start = dut.cnt_latency_nonhit_count;
        pattern_stall_busy_start = dut.cnt_stall_busy;
        pattern_stall_trrd_start = dut.cnt_stall_trrd;
        pattern_stall_refresh_start = dut.cnt_stall_refresh;
        pattern_stall_other_start = dut.cnt_stall_other;
        set_pattern_detail_logging("HIGH LOCALITY", PATTERN_DETAIL_LOG_LIMIT);
        for (pattern_iteration = 0; pattern_iteration < LOCALITY_PATTERN_PAIRS; pattern_iteration = pattern_iteration + 1) begin
            pattern_col = pattern_iteration % COL_COUNT;
            pattern_data = high_locality_data(pattern_iteration, pattern_col);
            high_locality_expected_data[pattern_col] = pattern_data;
            issue_write_and_wait_complete(0, 1, pattern_col, pattern_data,
                                          ROW_CLASS_CLOSED, SLOW_SERVICE_CYCLES, pattern_latency,
                                          "High locality: close-page keeps same-row traffic on the non-hit path");
            issue_read_and_wait_response(0, 1, pattern_col, high_locality_expected_data[pattern_col],
                                         ROW_CLASS_CLOSED, SLOW_SERVICE_CYCLES, pattern_latency,
                                         "High locality: close-page reopens row 1 instead of reusing it");
        end
        clear_pattern_detail_logging();
        report_pattern_summary("HIGH LOCALITY",
                               pattern_accept_start,
                               pattern_hit_start,
                               pattern_nonhit_start,
                               pattern_hit_latency_total_start,
                               pattern_nonhit_latency_total_start,
                               pattern_hit_count_start,
                               pattern_nonhit_count_start,
                               pattern_stall_busy_start,
                               pattern_stall_trrd_start,
                               pattern_stall_refresh_start,
                               pattern_stall_other_start,
                               "Close-page removed row reuse, reducing hit rate under HIGH LOCALITY.");
        $display("Pattern Explanation   : bank 0 stayed on row 1, but close-page cleared the row after every completed access.");
        check_row_state(0, 1'b0, 0, "High locality pattern leaves bank 0 closed under close-page");

        log_test_pattern("LOW LOCALITY");
        pattern_accept_start = dut.cnt_accept;
        pattern_hit_start = dut.cnt_row_hit;
        pattern_nonhit_start = dut.cnt_row_miss + dut.cnt_row_closed;
        pattern_hit_latency_total_start = dut.cnt_latency_hit_total;
        pattern_nonhit_latency_total_start = dut.cnt_latency_nonhit_total;
        pattern_hit_count_start = dut.cnt_latency_hit_count;
        pattern_nonhit_count_start = dut.cnt_latency_nonhit_count;
        pattern_stall_busy_start = dut.cnt_stall_busy;
        pattern_stall_trrd_start = dut.cnt_stall_trrd;
        pattern_stall_refresh_start = dut.cnt_stall_refresh;
        pattern_stall_other_start = dut.cnt_stall_other;
        set_pattern_detail_logging("LOW LOCALITY", PATTERN_DETAIL_LOG_LIMIT);
        for (pattern_iteration = 0; pattern_iteration < LOCALITY_PATTERN_PAIRS; pattern_iteration = pattern_iteration + 1) begin
            pattern_col = pattern_iteration % COL_COUNT;
            pattern_data = low_locality_data(pattern_iteration, pattern_col);
            issue_write_and_wait_complete(0, 2, pattern_col, pattern_data,
                                          ROW_CLASS_CLOSED, SLOW_SERVICE_CYCLES, pattern_latency,
                                          "Low locality: close-page still treats alternating-row traffic as closed-row access");
            issue_read_and_wait_response(0, 1, pattern_col, high_locality_expected_data[pattern_col],
                                         ROW_CLASS_CLOSED, SLOW_SERVICE_CYCLES, pattern_latency,
                                         "Low locality: switching back to row 1 still reopens from closed");
        end
        clear_pattern_detail_logging();
        report_pattern_summary("LOW LOCALITY",
                               pattern_accept_start,
                               pattern_hit_start,
                               pattern_nonhit_start,
                               pattern_hit_latency_total_start,
                               pattern_nonhit_latency_total_start,
                               pattern_hit_count_start,
                               pattern_nonhit_count_start,
                               pattern_stall_busy_start,
                               pattern_stall_trrd_start,
                               pattern_stall_refresh_start,
                               pattern_stall_other_start,
                               "Close-page keeps both locality patterns on the non-hit path, so LOW LOCALITY changes less than open-page.");
        $display("Pattern Explanation   : close-page cleared bank 0 after every access, so alternating rows looked similar to repeated-row traffic.");
        check_row_state(0, 1'b0, 0, "Low locality pattern also leaves bank 0 closed under close-page");
"""
    else:
        pattern_sequence_block = """
        log_phase("ACCESS PATTERNS");
        `INFO("Pattern comparison phase")
        check_row_state(0, 1'b1, 1, "Pattern comparison begins with bank 0 row 1 open");

        log_test_pattern("HIGH LOCALITY");
        pattern_accept_start = dut.cnt_accept;
        pattern_hit_start = dut.cnt_row_hit;
        pattern_nonhit_start = dut.cnt_row_miss + dut.cnt_row_closed;
        pattern_hit_latency_total_start = dut.cnt_latency_hit_total;
        pattern_nonhit_latency_total_start = dut.cnt_latency_nonhit_total;
        pattern_hit_count_start = dut.cnt_latency_hit_count;
        pattern_nonhit_count_start = dut.cnt_latency_nonhit_count;
        pattern_stall_busy_start = dut.cnt_stall_busy;
        pattern_stall_trrd_start = dut.cnt_stall_trrd;
        pattern_stall_refresh_start = dut.cnt_stall_refresh;
        pattern_stall_other_start = dut.cnt_stall_other;
        set_pattern_detail_logging("HIGH LOCALITY", PATTERN_DETAIL_LOG_LIMIT);
        for (pattern_iteration = 0; pattern_iteration < LOCALITY_PATTERN_PAIRS; pattern_iteration = pattern_iteration + 1) begin
            pattern_col = pattern_iteration % COL_COUNT;
            pattern_data = high_locality_data(pattern_iteration, pattern_col);
            high_locality_expected_data[pattern_col] = pattern_data;
            issue_write_and_wait_complete(0, 1, pattern_col, pattern_data,
                                          ROW_CLASS_HIT, HIT_SERVICE_CYCLES, pattern_latency,
                                          "High locality: bank 0 stays on row 1 while columns change");
            issue_read_and_wait_response(0, 1, pattern_col, high_locality_expected_data[pattern_col],
                                         ROW_CLASS_HIT, HIT_SERVICE_CYCLES, pattern_latency,
                                         "High locality: bank 0 reuses the same open row for readback");
        end
        clear_pattern_detail_logging();
        report_pattern_summary("HIGH LOCALITY",
                               pattern_accept_start,
                               pattern_hit_start,
                               pattern_nonhit_start,
                               pattern_hit_latency_total_start,
                               pattern_nonhit_latency_total_start,
                               pattern_hit_count_start,
                               pattern_nonhit_count_start,
                               pattern_stall_busy_start,
                               pattern_stall_trrd_start,
                               pattern_stall_refresh_start,
                               pattern_stall_other_start,
                               "Open-page preserved row reuse in HIGH LOCALITY.");
        $display("Pattern Explanation   : bank 0 stayed on row 1 while columns changed, so the open row matched every request.");
        check_row_state(0, 1'b1, 1, "High locality pattern keeps bank 0 row 1 open");

        log_test_pattern("LOW LOCALITY");
        pattern_accept_start = dut.cnt_accept;
        pattern_hit_start = dut.cnt_row_hit;
        pattern_nonhit_start = dut.cnt_row_miss + dut.cnt_row_closed;
        pattern_hit_latency_total_start = dut.cnt_latency_hit_total;
        pattern_nonhit_latency_total_start = dut.cnt_latency_nonhit_total;
        pattern_hit_count_start = dut.cnt_latency_hit_count;
        pattern_nonhit_count_start = dut.cnt_latency_nonhit_count;
        pattern_stall_busy_start = dut.cnt_stall_busy;
        pattern_stall_trrd_start = dut.cnt_stall_trrd;
        pattern_stall_refresh_start = dut.cnt_stall_refresh;
        pattern_stall_other_start = dut.cnt_stall_other;
        set_pattern_detail_logging("LOW LOCALITY", PATTERN_DETAIL_LOG_LIMIT);
        for (pattern_iteration = 0; pattern_iteration < LOCALITY_PATTERN_PAIRS; pattern_iteration = pattern_iteration + 1) begin
            pattern_col = pattern_iteration % COL_COUNT;
            pattern_data = low_locality_data(pattern_iteration, pattern_col);
            issue_write_and_wait_complete(0, 2, pattern_col, pattern_data,
                                          ROW_CLASS_MISS, SLOW_SERVICE_CYCLES, pattern_latency,
                                          "Low locality: alternating from row 1 to row 2 displaces the open row");
            issue_read_and_wait_response(0, 1, pattern_col, high_locality_expected_data[pattern_col],
                                         ROW_CLASS_MISS, SLOW_SERVICE_CYCLES, pattern_latency,
                                         "Low locality: alternating back to row 1 forces another non-hit");
        end
        clear_pattern_detail_logging();
        report_pattern_summary("LOW LOCALITY",
                               pattern_accept_start,
                               pattern_hit_start,
                               pattern_nonhit_start,
                               pattern_hit_latency_total_start,
                               pattern_nonhit_latency_total_start,
                               pattern_hit_count_start,
                               pattern_nonhit_count_start,
                               pattern_stall_busy_start,
                               pattern_stall_trrd_start,
                               pattern_stall_refresh_start,
                               pattern_stall_other_start,
                               "Open-page helps most when requests stay on one row; LOW LOCALITY kept displacing the open row.");
        $display("Pattern Explanation   : bank 0 alternated rows 1 and 2, so each new access displaced the prior open row.");
        check_row_state(0, 1'b1, 1, "Low locality pattern ends with bank 0 row 1 reopened");
"""

    if design.is_close_page:
        row_buffer_sequence_block = f"""
        log_phase("ROW BUFFER");
        `INFO("Row-buffer phase")
        issue_write_and_wait_complete(0, 1, 1, DATA_WIDTH'(32'h11110001),
                                      ROW_CLASS_CLOSED, SLOW_SERVICE_CYCLES, closed_latency,
                                      "First access to bank 0 opens row 1 only for the current service");
        check_row_state(0, 1'b0, 0, "Close-page clears bank 0 after the first access");
{trrd_sequence_block}        issue_read_and_wait_response(0, 1, 1, DATA_WIDTH'(32'h11110001),
                                     ROW_CLASS_CLOSED, SLOW_SERVICE_CYCLES, hit_latency,
                                     "Second access to bank 0 row 1 reopens the row instead of hitting");
        check_row_state(0, 1'b0, 0, "Repeated row access also leaves bank 0 closed");

        issue_write_and_wait_complete(0, 2, 0, DATA_WIDTH'(32'h22220002),
                                      ROW_CLASS_CLOSED, SLOW_SERVICE_CYCLES, miss_latency,
                                      "Accessing bank 0 row 2 remains a closed-row non-hit under close-page");
        check_row_state(0, 1'b0, 0, "Bank 0 closes again after the row-2 access");

        issue_read_and_wait_response(0, 2, 0, DATA_WIDTH'(32'h22220002),
                                     ROW_CLASS_CLOSED, SLOW_SERVICE_CYCLES, hit_latency_after_miss,
                                     "Reading bank 0 row 2 still reopens from the closed state");
        issue_read_and_wait_response(0, 1, 1, DATA_WIDTH'(32'h11110001),
                                     ROW_CLASS_CLOSED, SLOW_SERVICE_CYCLES, closed_readback_latency,
                                     "Returning to bank 0 row 1 preserves data but not row residency");
        check_row_state(0, 1'b0, 0, "Bank 0 remains closed after reading row 1 back");

        `CHECK(hit_latency == SLOW_SERVICE_CYCLES,
               "Close-page should remove the row-hit fast path for repeated-row traffic")
        `CHECK(hit_latency_after_miss == SLOW_SERVICE_CYCLES,
               "Close-page should keep post-switch reads on the slow path as well")
        `CHECK(closed_readback_latency == SLOW_SERVICE_CYCLES,
               "Close-page should preserve data correctness while keeping row reuse disabled")
"""
    else:
        row_buffer_sequence_block = f"""
        log_phase("ROW BUFFER");
        `INFO("Row-buffer phase")
        issue_write_and_wait_complete(0, 1, 1, DATA_WIDTH'(32'h11110001),
                                      ROW_CLASS_CLOSED, SLOW_SERVICE_CYCLES, closed_latency,
                                      "First access to bank 0 opens row 1 on the slow path");
        check_row_state(0, 1'b1, 1, "Bank 0 keeps row 1 open after the first access");
{trrd_sequence_block}        issue_read_and_wait_response(0, 1, 1, DATA_WIDTH'(32'h11110001),
                                     ROW_CLASS_HIT, HIT_SERVICE_CYCLES, hit_latency,
                                     "Second access to bank 0 row 1 is a row hit on the fast path");
        check_row_state(0, 1'b1, 1, "Row hit keeps bank 0 row 1 open");

        issue_write_and_wait_complete(0, 2, 0, DATA_WIDTH'(32'h22220002),
                                      ROW_CLASS_MISS, SLOW_SERVICE_CYCLES, miss_latency,
                                      "Accessing bank 0 row 2 causes a row miss and row replacement");
        check_row_state(0, 1'b1, 2, "Row miss updates bank 0 to keep row 2 open");

        issue_read_and_wait_response(0, 2, 0, DATA_WIDTH'(32'h22220002),
                                     ROW_CLASS_HIT, HIT_SERVICE_CYCLES, hit_latency_after_miss,
                                     "Reading bank 0 row 2 after replacement is a row hit");
        issue_read_and_wait_response(0, 1, 1, DATA_WIDTH'(32'h11110001),
                                     ROW_CLASS_MISS, SLOW_SERVICE_CYCLES, closed_readback_latency,
                                     "Returning to bank 0 row 1 preserves the original row-1 data");
        check_row_state(0, 1'b1, 1, "Bank 0 re-opens row 1 after reading it back");

        `CHECK(hit_latency < closed_latency,
               "Row-hit service should be faster than the first closed-row access")
        `CHECK(hit_latency < miss_latency,
               "Row-hit service should be faster than a row miss")
        `CHECK(hit_latency_after_miss == HIT_SERVICE_CYCLES,
               "Row-hit latency after a miss should remain on the fast path")
"""

    if design.has_tfaw:
        timing_tfaw_start_lines = """
        pattern_tfaw_admission_start = observed_tfaw_admission_stall_cycles;
        pattern_tfaw_hard_block_start = observed_tfaw_hard_block_cycles;
"""
    else:
        timing_tfaw_start_lines = """
        pattern_tfaw_admission_start = 0;
        pattern_tfaw_hard_block_start = 0;
"""

    if design.is_close_page:
        timing_stress_expected_class_expr = "ROW_CLASS_CLOSED"
        timing_stress_phase_msg = (
            "Timing stress: close-page keeps each ACT-heavy access on the closed-row path"
        )
    elif design.bank_count >= 4:
        timing_stress_expected_class_expr = (
            "(((pattern_bank >= 2) && ((pattern_iteration / BANK_COUNT) == 0)) ? "
            "ROW_CLASS_CLOSED : ROW_CLASS_MISS)"
        )
        timing_stress_phase_msg = (
            "Timing stress: alternating banks and rows keeps ACT pressure high under open-page"
        )
    else:
        timing_stress_expected_class_expr = "ROW_CLASS_MISS"
        timing_stress_phase_msg = (
            "Timing stress: alternating rows keeps forcing new ACTs under open-page"
        )

    if design.is_close_page:
        timing_stress_policy_observation = (
            "Close-page kept the timing-stress sequence on the non-hit path, so ACT pressure stayed consistently high."
        )
    else:
        timing_stress_policy_observation = (
            "Open-page still lost row reuse once the timing-stress pattern kept switching rows and banks."
        )

    timing_stress_block = f"""
        log_test_pattern("TIMING STRESS");
        pattern_accept_start = dut.cnt_accept;
        pattern_hit_start = dut.cnt_row_hit;
        pattern_nonhit_start = dut.cnt_row_miss + dut.cnt_row_closed;
        pattern_hit_latency_total_start = dut.cnt_latency_hit_total;
        pattern_nonhit_latency_total_start = dut.cnt_latency_nonhit_total;
        pattern_hit_count_start = dut.cnt_latency_hit_count;
        pattern_nonhit_count_start = dut.cnt_latency_nonhit_count;
        pattern_stall_busy_start = dut.cnt_stall_busy;
        pattern_stall_trrd_start = dut.cnt_stall_trrd;
        pattern_stall_refresh_start = dut.cnt_stall_refresh;
        pattern_stall_other_start = dut.cnt_stall_other;
{timing_tfaw_start_lines}        set_pattern_detail_logging("TIMING STRESS", PATTERN_DETAIL_LOG_LIMIT);
        for (pattern_iteration = 0; pattern_iteration < TIMING_STRESS_OPS; pattern_iteration = pattern_iteration + 1) begin
            pattern_bank = timing_stress_bank(pattern_iteration);
            pattern_row = timing_stress_row(pattern_bank, pattern_iteration);
            pattern_col = timing_stress_col(pattern_iteration);
            pattern_data = timing_stress_data(pattern_bank, pattern_iteration, pattern_row, pattern_col);
            issue_write_and_wait_complete(pattern_bank, pattern_row, pattern_col, pattern_data,
                                          {timing_stress_expected_class_expr}, SLOW_SERVICE_CYCLES, pattern_latency,
                                          "{timing_stress_phase_msg}");
        end
        clear_pattern_detail_logging();
        report_pattern_summary("TIMING STRESS",
                               pattern_accept_start,
                               pattern_hit_start,
                               pattern_nonhit_start,
                               pattern_hit_latency_total_start,
                               pattern_nonhit_latency_total_start,
                               pattern_hit_count_start,
                               pattern_nonhit_count_start,
                               pattern_stall_busy_start,
                               pattern_stall_trrd_start,
                               pattern_stall_refresh_start,
                               pattern_stall_other_start,
                               "{timing_stress_policy_observation}");
        report_timing_stress_summary(pattern_accept_start,
                                     pattern_stall_busy_start,
                                     pattern_stall_trrd_start,
                                     pattern_stall_refresh_start,
                                     pattern_stall_other_start,
                                     pattern_tfaw_admission_start,
                                     pattern_tfaw_hard_block_start);
"""

    if design.has_tfaw and design.tfaw_limit_reachable:
        timing_stress_summary_block = """
    task automatic report_timing_stress_summary(
        input int accept_start,
        input int stall_busy_start,
        input int stall_trrd_start,
        input int stall_refresh_start,
        input int stall_other_start,
        input int tfaw_admission_start,
        input int tfaw_hard_block_start
    );
        int accepted_delta;
        int stall_busy_delta;
        int stall_trrd_delta;
        int stall_refresh_delta;
        int stall_other_delta;
        int total_stall_delta;
        int tfaw_admission_delta;
        int tfaw_hard_block_delta;
        real stall_cycles_per_txn;
        begin
            accepted_delta = dut.cnt_accept - accept_start;
            stall_busy_delta = dut.cnt_stall_busy - stall_busy_start;
            stall_trrd_delta = dut.cnt_stall_trrd - stall_trrd_start;
            stall_refresh_delta = dut.cnt_stall_refresh - stall_refresh_start;
            stall_other_delta = dut.cnt_stall_other - stall_other_start;
            total_stall_delta = stall_busy_delta + stall_trrd_delta + stall_refresh_delta + stall_other_delta;
            tfaw_admission_delta = observed_tfaw_admission_stall_cycles - tfaw_admission_start;
            tfaw_hard_block_delta = observed_tfaw_hard_block_cycles - tfaw_hard_block_start;
            stall_cycles_per_txn = 0.0;

            if (accepted_delta != 0) begin
                stall_cycles_per_txn = $itor(total_stall_delta) / $itor(accepted_delta);
            end

            `CHECK(stall_trrd_delta != 0,
                   "Timing stress pattern should exercise tRRD throttling")

            $display("----- TIMING STRESS SUMMARY -----");
            $display("Accepted Transactions   : %0d", accepted_delta);
            $display("Busy Stall Cycles        : %0d", stall_busy_delta);
            $display("tRRD Stall Cycles        : %0d", stall_trrd_delta);
            $display("Refresh Stall Cycles     : %0d", stall_refresh_delta);
            $display("Other Stall Cycles       : %0d", stall_other_delta);
            $display("Stall / Accepted Txn     : %0.2f cycles", stall_cycles_per_txn);
            $display("tFAW Admission Stalls    : %0d", tfaw_admission_delta);
            $display("tFAW Hard-Block Cycles   : %0d", tfaw_hard_block_delta);
            if (tfaw_hard_block_delta != 0) begin
                $display("Timing Observation      : shared timing throttling showed both tRRD and hard tFAW blocking.");
            end else if (tfaw_admission_delta != 0) begin
                $display("Timing Observation      : tRRD throttled requests and conservative tFAW admission also delayed ACTs.");
            end else begin
                $display("Timing Observation      : tRRD throttled requests; reachable tFAW did not block in this run.");
            end
            $display("Pattern Explanation     : consecutive non-hit accesses across banks increased ACT pressure and exposed timing throttling.");
        end
    endtask
"""
    elif design.has_tfaw:
        timing_stress_summary_block = f"""
    task automatic report_timing_stress_summary(
        input int accept_start,
        input int stall_busy_start,
        input int stall_trrd_start,
        input int stall_refresh_start,
        input int stall_other_start,
        input int tfaw_admission_start,
        input int tfaw_hard_block_start
    );
        int accepted_delta;
        int stall_busy_delta;
        int stall_trrd_delta;
        int stall_refresh_delta;
        int stall_other_delta;
        int total_stall_delta;
        int tfaw_admission_delta;
        int tfaw_hard_block_delta;
        real stall_cycles_per_txn;
        begin
            accepted_delta = dut.cnt_accept - accept_start;
            stall_busy_delta = dut.cnt_stall_busy - stall_busy_start;
            stall_trrd_delta = dut.cnt_stall_trrd - stall_trrd_start;
            stall_refresh_delta = dut.cnt_stall_refresh - stall_refresh_start;
            stall_other_delta = dut.cnt_stall_other - stall_other_start;
            total_stall_delta = stall_busy_delta + stall_trrd_delta + stall_refresh_delta + stall_other_delta;
            tfaw_admission_delta = observed_tfaw_admission_stall_cycles - tfaw_admission_start;
            tfaw_hard_block_delta = observed_tfaw_hard_block_cycles - tfaw_hard_block_start;
            stall_cycles_per_txn = 0.0;

            if (accepted_delta != 0) begin
                stall_cycles_per_txn = $itor(total_stall_delta) / $itor(accepted_delta);
            end

            `CHECK(stall_trrd_delta != 0,
                   "Timing stress pattern should exercise tRRD throttling")

            $display("----- TIMING STRESS SUMMARY -----");
            $display("Accepted Transactions   : %0d", accepted_delta);
            $display("Busy Stall Cycles        : %0d", stall_busy_delta);
            $display("tRRD Stall Cycles        : %0d", stall_trrd_delta);
            $display("Refresh Stall Cycles     : %0d", stall_refresh_delta);
            $display("Other Stall Cycles       : %0d", stall_other_delta);
            $display("Stall / Accepted Txn     : %0.2f cycles", stall_cycles_per_txn);
            $display("tFAW Admission Stalls    : %0d", tfaw_admission_delta);
            $display("tFAW Hard-Block Cycles   : %0d", tfaw_hard_block_delta);
            $display("Timing Observation      : tRRD throttled requests; additional ACT pressure also showed up as tFAW admission stalls inside the 'other' bucket.");
            $display("tFAW Status             : enabled, but the hard block threshold is not realistically reachable in this config (window={design.tfaw_cycles}, limit={design.tfaw_limit}).");
            $display("Pattern Explanation     : consecutive non-hit accesses across banks increased ACT pressure and exposed timing throttling.");
        end
    endtask
"""
    else:
        timing_stress_summary_block = """
    task automatic report_timing_stress_summary(
        input int accept_start,
        input int stall_busy_start,
        input int stall_trrd_start,
        input int stall_refresh_start,
        input int stall_other_start,
        input int tfaw_admission_start,
        input int tfaw_hard_block_start
    );
        int accepted_delta;
        int stall_busy_delta;
        int stall_trrd_delta;
        int stall_refresh_delta;
        int stall_other_delta;
        int total_stall_delta;
        real stall_cycles_per_txn;
        begin
            accepted_delta = dut.cnt_accept - accept_start;
            stall_busy_delta = dut.cnt_stall_busy - stall_busy_start;
            stall_trrd_delta = dut.cnt_stall_trrd - stall_trrd_start;
            stall_refresh_delta = dut.cnt_stall_refresh - stall_refresh_start;
            stall_other_delta = dut.cnt_stall_other - stall_other_start;
            total_stall_delta = stall_busy_delta + stall_trrd_delta + stall_refresh_delta + stall_other_delta;
            stall_cycles_per_txn = 0.0;

            if (accepted_delta != 0) begin
                stall_cycles_per_txn = $itor(total_stall_delta) / $itor(accepted_delta);
            end

            `CHECK(stall_trrd_delta != 0,
                   "Timing stress pattern should exercise tRRD throttling")

            $display("----- TIMING STRESS SUMMARY -----");
            $display("Accepted Transactions   : %0d", accepted_delta);
            $display("Busy Stall Cycles        : %0d", stall_busy_delta);
            $display("tRRD Stall Cycles        : %0d", stall_trrd_delta);
            $display("Refresh Stall Cycles     : %0d", stall_refresh_delta);
            $display("Other Stall Cycles       : %0d", stall_other_delta);
            $display("Stall / Accepted Txn     : %0.2f cycles", stall_cycles_per_txn);
            $display("Timing Observation      : tRRD throttled requests in the ACT-heavy sequence.");
            $display("tFAW Status             : disabled in this configuration.");
            $display("Pattern Explanation     : consecutive non-hit accesses across banks increased ACT pressure and exposed timing throttling.");
        end
    endtask
"""

    page_policy_display = design.page_policy
    if design.is_close_page:
        coverage_goal_checks = f"""            `CHECK(saw_row_closed, "Row-closed access was never observed")
            `CHECK(!saw_row_hit, "Close-page should not produce row-hit reuse in this serialized demo")
            `CHECK(!saw_row_miss, "Close-page should classify the serialized non-hits as row-closed")
            `CHECK(saw_backpressure, "Controller backpressure was never observed")
{tRRD_coverage_line}"""
        performance_policy_observation = (
            "Close-page removed row reuse, so repeated-row traffic stayed on the non-hit path."
        )
    else:
        coverage_goal_checks = f"""            `CHECK(saw_row_closed, "Row-closed access was never observed")
            `CHECK(saw_row_hit, "Row-hit access was never observed")
            `CHECK(saw_row_miss, "Row-miss access was never observed")
            `CHECK(saw_backpressure, "Controller backpressure was never observed")
{tRRD_coverage_line}"""
        performance_policy_observation = (
            "Open-page preserved row reuse whenever the workload stayed on an already-open row."
        )

    return f"""`timescale 1ns/1ps

`define CHECK(cond, msg) \\
    if (!(cond)) begin \\
        $display("[ERROR] %s | time=%0t | %s", test_name, $time, msg); \\
        error_count++; \\
    end

`define INFO(msg) \\
    $display("[INFO] %s | time=%0t | %s", test_name, $time, msg);

module {tb_module};

    localparam int MAX_CYCLES = 20000;
    localparam time SIM_END_TIME = 200000ns;
    localparam int REFRESH_WAIT_CYCLES = {refresh_wait_cycles};
    localparam int BANK_COUNT = {design.bank_count};
    localparam int TXN_BANK_WIDTH = {txn_bank_width};
    localparam int ADDR_WIDTH = {MEMORY_ADDR_WIDTH};
    localparam int DATA_WIDTH = {MEMORY_DATA_WIDTH};
    localparam int ROW_WIDTH = {row_width};
    localparam int COL_WIDTH = {col_width};
    localparam int COL_COUNT = (1 << COL_WIDTH);
    localparam int LOCALITY_PATTERN_PAIRS = {LOCALITY_PATTERN_PAIRS};
    localparam int LOCALITY_PATTERN_OPS = (2 * LOCALITY_PATTERN_PAIRS);
    localparam int TIMING_STRESS_OPS = {TIMING_STRESS_OPS};
    localparam int PATTERN_DETAIL_LOG_LIMIT = {PATTERN_DETAIL_LOG_LIMIT};
    localparam int HIT_SERVICE_CYCLES = {READ_RESPONSE_LATENCY_CYCLES};
    localparam int SLOW_SERVICE_CYCLES = 3;
    localparam int ROW_CLASS_CLOSED = 0;
    localparam int ROW_CLASS_HIT = 1;
    localparam int ROW_CLASS_MISS = 2;
    localparam string PAGE_POLICY = "{page_policy_display}";

    int error_count = 0;
    int cycle = 0;
    int txn_id = 0;
    int active_txn_id = 0;
    int detailed_log_budget = -1;
    int pattern_iteration = 0;
    int pattern_bank = 0;
    int pattern_row = 0;
    int pattern_col = 0;
    string test_name = "{tb_module}";
    string detailed_log_context = "";
    bit results_reported = 0;
    bit detailed_log_notice_emitted = 0;
    bit traffic_started = 0;
    bit saw_backpressure = 0;
    bit saw_tRRD_block = 0;
    bit saw_row_closed = 0;
    bit saw_row_hit = 0;
    bit saw_row_miss = 0;
{tfaw_state_decl}
    logic [DATA_WIDTH-1:0] pattern_data;
    logic [DATA_WIDTH-1:0] high_locality_expected_data [0:COL_COUNT-1];

    logic clk;
    logic rst_n;
    logic txn_valid;
    logic txn_is_write;
    logic [ADDR_WIDTH-1:0] txn_addr;
    logic [DATA_WIDTH-1:0] txn_wdata;
{bank_signal_decl}
    logic cmd_ready;
    logic rsp_valid;
    logic [DATA_WIDTH-1:0] rsp_rdata;
{scheduler_decl_block}

    {top_module} dut (
        .clk(clk),
        .rst_n(rst_n),
        .txn_valid(txn_valid),
        .txn_is_write(txn_is_write),
        .txn_addr(txn_addr),
        .txn_wdata(txn_wdata),
{bank_dut_port}
        .cmd_ready(cmd_ready),
        .rsp_valid(rsp_valid),
        .rsp_rdata(rsp_rdata)
    );

    task automatic check_coverage_goals;
        begin
{coverage_goal_checks}        end
    endtask

    task automatic report_results;
        begin
            if (!results_reported) begin
                check_coverage_goals();
                results_reported = 1'b1;
                report_performance_summary();

                if (error_count == 0) begin
                    $display("=================================");
                    $display("=========== TEST PASS ===========");
                    $display("=================================");
                end else begin
                    $display("=================================");
                    $display("=========== TEST FAIL ===========");
                    $display("Errors: %0d", error_count);
                    $display("=================================");
                end
            end
        end
    endtask

    task automatic report_performance_summary;
        real row_hit_ratio;
        real non_hit_ratio;
        real avg_hit_latency;
        real avg_nonhit_latency;
        int stall_accounted_cycles;
        int bank_index;
        begin
            row_hit_ratio = 0.0;
            non_hit_ratio = 0.0;
            avg_hit_latency = 0.0;
            avg_nonhit_latency = 0.0;
            stall_accounted_cycles = dut.cnt_stall_busy + dut.cnt_stall_trrd
                                   + dut.cnt_stall_refresh + dut.cnt_stall_other;

            if (dut.cnt_accept != 0) begin
                row_hit_ratio = (100.0 * $itor(dut.cnt_row_hit)) / $itor(dut.cnt_accept);
                non_hit_ratio = (100.0 * $itor(dut.cnt_row_miss + dut.cnt_row_closed)) / $itor(dut.cnt_accept);
            end
            if (dut.cnt_latency_hit_count != 0) begin
                avg_hit_latency = $itor(dut.cnt_latency_hit_total) / $itor(dut.cnt_latency_hit_count);
            end
            if (dut.cnt_latency_nonhit_count != 0) begin
                avg_nonhit_latency = $itor(dut.cnt_latency_nonhit_total) / $itor(dut.cnt_latency_nonhit_count);
            end

            `CHECK(dut.cnt_stall == stall_accounted_cycles,
                   "Stall breakdown should explain the total stalled cycles")

            $display("===== PERFORMANCE SUMMARY =====");
            $display("Page Policy           : %s", PAGE_POLICY);
            $display("Accepted Transactions : %0d", dut.cnt_accept);
            $display("Row Hits              : %0d", dut.cnt_row_hit);
            $display("Row Misses            : %0d", dut.cnt_row_miss);
            $display("Row Closed            : %0d", dut.cnt_row_closed);
            $display("Non-Hits              : %0d", dut.cnt_row_miss + dut.cnt_row_closed);
            $display("Stall Cycles          : %0d", dut.cnt_stall);
            $display("Row Hit Ratio         : %0.2f%%", row_hit_ratio);
            $display("Non-Hit Ratio         : %0.2f%%", non_hit_ratio);
            $display("Policy Observation    : {performance_policy_observation}");
            $display("===== STALL BREAKDOWN =====");
            $display("Busy Stall Cycles     : %0d", dut.cnt_stall_busy);
            $display("tRRD Stall Cycles     : %0d", dut.cnt_stall_trrd);
            $display("Refresh Stall Cycles  : %0d", dut.cnt_stall_refresh);
            $display("Other Stall Cycles    : %0d", dut.cnt_stall_other);
            $display("===== PER-BANK SUMMARY =====");
            for (bank_index = 0; bank_index < BANK_COUNT; bank_index = bank_index + 1) begin
                $display("Bank %0d : accepted=%0d hit=%0d miss=%0d closed=%0d",
                         bank_index,
                         dut.cnt_accept_bank[bank_index],
                         dut.cnt_row_hit_bank[bank_index],
                         dut.cnt_row_miss_bank[bank_index],
                         dut.cnt_row_closed_bank[bank_index]);
            end
            $display("===== LATENCY BREAKDOWN =====");
            if (dut.cnt_latency_hit_count != 0) begin
                $display("Average Hit Latency     : %0.2f cycles", avg_hit_latency);
            end else begin
                $display("Average Hit Latency     : n/a");
            end
            if (dut.cnt_latency_nonhit_count != 0) begin
                $display("Average Non-Hit Latency : %0.2f cycles", avg_nonhit_latency);
            end else begin
                $display("Average Non-Hit Latency : n/a");
            end
        end
    endtask

    task automatic wait_cycles(input int count);
        int i;
        begin
            for (i = 0; i < count; i = i + 1) begin
                @(posedge clk);
            end
        end
    endtask

    task automatic log_phase(input string phase_name);
        begin
            $display("=== PHASE: %s ===", phase_name);
        end
    endtask

    task automatic log_test_pattern(input string pattern_name);
        begin
            $display("=== TEST PATTERN: %s ===", pattern_name);
        end
    endtask

    function automatic bit detail_logging_enabled;
        detail_logging_enabled = (detailed_log_budget != 0);
    endfunction

    task automatic set_pattern_detail_logging(
        input string pattern_name,
        input int detailed_accept_limit
    );
        begin
            detailed_log_context = pattern_name;
            detailed_log_budget = detailed_accept_limit;
            detailed_log_notice_emitted = 1'b0;
            $display("Pattern Detail Logging : first %0d accepted transactions", detailed_accept_limit);
        end
    endtask

    task automatic clear_pattern_detail_logging;
        begin
            detailed_log_context = "";
            detailed_log_budget = -1;
            detailed_log_notice_emitted = 1'b0;
        end
    endtask

    function automatic logic [DATA_WIDTH-1:0] high_locality_data(
        input int pair_index,
        input int col_sel
    );
        high_locality_data = DATA_WIDTH'(32'hAAAA1000 + (pair_index << 4) + col_sel);
    endfunction

    function automatic logic [DATA_WIDTH-1:0] low_locality_data(
        input int pair_index,
        input int col_sel
    );
        low_locality_data = DATA_WIDTH'(32'hBBBB2000 + (pair_index << 4) + col_sel);
    endfunction

    task automatic report_accepted_row_context(input int request_id);
        logic [ADDR_WIDTH-1:0] accepted_addr;
        begin
            accepted_addr = {{dut.accepted_requested_row_q, dut.accepted_requested_col_q}};
            if (dut.accepted_row_hit_q) begin
                $display("[WHY ][id=%0d][cycle=%0d] bank=%0d addr=%0d row=%0d col=%0d open_before=row%0d => ROW_HIT",
                         request_id, cycle, dut.accepted_bank_q, accepted_addr,
                         dut.accepted_requested_row_q, dut.accepted_requested_col_q,
                         dut.accepted_prev_open_row_q);
            end else if (dut.accepted_row_miss_q) begin
                $display("[WHY ][id=%0d][cycle=%0d] bank=%0d addr=%0d row=%0d col=%0d open_before=row%0d => ROW_MISS",
                         request_id, cycle, dut.accepted_bank_q, accepted_addr,
                         dut.accepted_requested_row_q, dut.accepted_requested_col_q,
                         dut.accepted_prev_open_row_q);
            end else if (dut.accepted_row_closed_q) begin
                $display("[WHY ][id=%0d][cycle=%0d] bank=%0d addr=%0d row=%0d col=%0d open_before=closed => ROW_CLOSED",
                         request_id, cycle, dut.accepted_bank_q, accepted_addr,
                         dut.accepted_requested_row_q, dut.accepted_requested_col_q);
            end
        end
    endtask

    task automatic report_pattern_summary(
        input string pattern_name,
        input int accept_start,
        input int hit_start,
        input int nonhit_start,
        input int hit_latency_total_start,
        input int nonhit_latency_total_start,
        input int hit_count_start,
        input int nonhit_count_start,
        input int stall_busy_start,
        input int stall_trrd_start,
        input int stall_refresh_start,
        input int stall_other_start,
        input string policy_observation
    );
        int accepted_delta;
        int hit_delta;
        int nonhit_delta;
        int hit_latency_total_delta;
        int nonhit_latency_total_delta;
        int hit_count_delta;
        int nonhit_count_delta;
        real hit_ratio;
        real nonhit_ratio;
        real avg_hit_latency;
        real avg_nonhit_latency;
        int stall_delta;
        real stall_cycles_per_txn;
        begin
            accepted_delta = dut.cnt_accept - accept_start;
            hit_delta = dut.cnt_row_hit - hit_start;
            nonhit_delta = (dut.cnt_row_miss + dut.cnt_row_closed) - nonhit_start;
            hit_latency_total_delta = dut.cnt_latency_hit_total - hit_latency_total_start;
            nonhit_latency_total_delta = dut.cnt_latency_nonhit_total - nonhit_latency_total_start;
            hit_count_delta = dut.cnt_latency_hit_count - hit_count_start;
            nonhit_count_delta = dut.cnt_latency_nonhit_count - nonhit_count_start;
            hit_ratio = 0.0;
            nonhit_ratio = 0.0;
            avg_hit_latency = 0.0;
            avg_nonhit_latency = 0.0;
            stall_delta = (dut.cnt_stall_busy - stall_busy_start)
                        + (dut.cnt_stall_trrd - stall_trrd_start)
                        + (dut.cnt_stall_refresh - stall_refresh_start)
                        + (dut.cnt_stall_other - stall_other_start);
            stall_cycles_per_txn = 0.0;

            if (accepted_delta != 0) begin
                hit_ratio = (100.0 * $itor(hit_delta)) / $itor(accepted_delta);
                nonhit_ratio = (100.0 * $itor(nonhit_delta)) / $itor(accepted_delta);
                stall_cycles_per_txn = $itor(stall_delta) / $itor(accepted_delta);
            end
            if (hit_count_delta != 0) begin
                avg_hit_latency = $itor(hit_latency_total_delta) / $itor(hit_count_delta);
            end
            if (nonhit_count_delta != 0) begin
                avg_nonhit_latency = $itor(nonhit_latency_total_delta) / $itor(nonhit_count_delta);
            end

            $display("----- PATTERN SUMMARY: %s -----", pattern_name);
            $display("Page Policy           : %s", PAGE_POLICY);
            $display("Accepted Transactions : %0d", accepted_delta);
            $display("Row Hits              : %0d", hit_delta);
            $display("Non-Hits              : %0d", nonhit_delta);
            $display("Stall Cycles          : %0d", stall_delta);
            $display("Row Hit Ratio         : %0.2f%%", hit_ratio);
            $display("Non-Hit Ratio         : %0.2f%%", nonhit_ratio);
            $display("Stall / Accepted Txn  : %0.2f cycles", stall_cycles_per_txn);
            if (hit_count_delta != 0) begin
                $display("Average Hit Latency     : %0.2f cycles", avg_hit_latency);
            end else begin
                $display("Average Hit Latency     : n/a");
            end
            if (nonhit_count_delta != 0) begin
                $display("Average Non-Hit Latency : %0.2f cycles", avg_nonhit_latency);
            end else begin
                $display("Average Non-Hit Latency : n/a");
            end
            $display("Policy Observation    : %s", policy_observation);
        end
    endtask
{timing_stress_summary_block}
{scheduler_task_block}
    function automatic [ADDR_WIDTH-1:0] make_addr(input int row_sel, input int col_sel);
        make_addr = ADDR_WIDTH'((row_sel << COL_WIDTH) | col_sel);
    endfunction

    function automatic int timing_stress_bank(input int stress_index);
        if (BANK_COUNT == 1) begin
            timing_stress_bank = 0;
        end else begin
            timing_stress_bank = stress_index % BANK_COUNT;
        end
    endfunction

    function automatic int timing_stress_row(input int bank_sel, input int stress_index);
        int visit_index;
        begin
            if (BANK_COUNT == 1) begin
                timing_stress_row = ((stress_index % 2) == 0) ? 2 : 1;
            end else begin
                visit_index = stress_index / BANK_COUNT;
                case (bank_sel)
                    0: timing_stress_row = ((visit_index % 2) == 0) ? 2 : 1;
                    1: timing_stress_row = ((visit_index % 2) == 0) ? 1 : 0;
                    default: timing_stress_row = ((visit_index % 2) == 0) ? 0 : 1;
                endcase
            end
        end
    endfunction

    function automatic int timing_stress_col(input int stress_index);
        timing_stress_col = stress_index % COL_COUNT;
    endfunction

    function automatic logic [DATA_WIDTH-1:0] timing_stress_data(
        input int bank_sel,
        input int stress_index,
        input int row_sel,
        input int col_sel
    );
        timing_stress_data = DATA_WIDTH'(32'hCC000000
                                       + (bank_sel << 20)
                                       + (stress_index << 6)
                                       + (row_sel << 2)
                                       + col_sel);
    endfunction

    task automatic check_row_state(
        input int bank_sel,
        input logic expected_valid,
        input int expected_row,
        input string phase_msg
    );
        begin
            #1;
            `INFO(phase_msg)
            `CHECK(dut.row_open_valid[bank_sel] == expected_valid,
                   "Unexpected row_open_valid state")
            if (expected_valid) begin
                `CHECK(dut.open_row[bank_sel] == ROW_WIDTH'(expected_row),
                       "Unexpected open_row value")
            end
        end
    endtask

    task automatic expect_access_ready(
        input int bank_sel,
        input int row_sel,
        input int col_sel,
        input logic expected_ready,
        input string phase_msg
    );
        begin
            @(negedge clk);
{("            txn_bank = bank_sel[TXN_BANK_WIDTH-1:0];\n" if design.bank_count > 1 else "")}            txn_addr = make_addr(row_sel, col_sel);
            txn_is_write = 1'b0;
            txn_wdata = '0;
            #1;
            `INFO(phase_msg)
            `CHECK(cmd_ready == expected_ready,
                   "Unexpected cmd_ready classification result")
        end
    endtask

    task automatic drive_request(
        input int bank_sel,
        input logic is_write,
        input int row_sel,
        input int col_sel,
        input logic [DATA_WIDTH-1:0] data,
        output int request_id
    );
        begin
            wait(rst_n === 1'b1);
            traffic_started = 1'b1;
            while (rsp_valid === 1'b1) begin
                @(posedge clk);
            end
            @(negedge clk);
            txn_id = txn_id + 1;
            request_id = txn_id;
            active_txn_id = request_id;
{("            txn_bank = bank_sel[TXN_BANK_WIDTH-1:0];\n" if design.bank_count > 1 else "")}            txn_is_write = is_write;
            txn_addr = make_addr(row_sel, col_sel);
            txn_wdata = data;
            txn_valid = 1'b1;
            if (detail_logging_enabled()) begin
                $display("[TXN ][id=%0d][cycle=%0d] %s bank=%0d addr=%0d data=0x%08h",
                         request_id, cycle, is_write ? "WRITE" : "READ",
                         bank_sel, txn_addr, data);
            end
        end
    endtask

    task automatic wait_for_accept_and_classify(
        input int request_id,
        input int bank_sel,
        input logic is_write,
        input int row_sel,
        input int col_sel,
        input logic [DATA_WIDTH-1:0] req_data,
        input int expected_row_class,
        output int accept_cycle
    );
        logic [BANK_COUNT-1:0] expected_cmd_valid;
        int wait_count;
        begin
            expected_cmd_valid = '0;
            wait_count = 0;
            accept_cycle = -1;

            while ((accept_cycle < 0) && (wait_count < MAX_CYCLES)) begin
                @(posedge clk);
                #1;
                if (dut.accept_txn) begin
                    accept_cycle = cycle;

                    case (expected_row_class)
                        ROW_CLASS_CLOSED: begin
                            `CHECK(dut.accepted_row_closed_q, "Expected accepted access to classify as row closed")
                            `CHECK(!dut.accepted_row_hit_q, "Row-closed access should not also be a row hit")
                            `CHECK(!dut.accepted_row_miss_q, "Row-closed access should not also be a row miss")
                            expected_cmd_valid[bank_sel] = 1'b1;
                        end
                        ROW_CLASS_HIT: begin
                            `CHECK(dut.accepted_row_hit_q, "Expected accepted access to classify as row hit")
                            `CHECK(!dut.accepted_row_closed_q, "Row-hit access should not also be row closed")
                            `CHECK(!dut.accepted_row_miss_q, "Row-hit access should not also be row miss")
                        end
                        ROW_CLASS_MISS: begin
                            `CHECK(dut.accepted_row_miss_q, "Expected accepted access to classify as row miss")
                            `CHECK(!dut.accepted_row_closed_q, "Row-miss access should not also be row closed")
                            `CHECK(!dut.accepted_row_hit_q, "Row-miss access should not also be row hit")
                            expected_cmd_valid[bank_sel] = 1'b1;
                        end
                        default: begin
                            `CHECK(0, "Unsupported expected_row_class")
                        end
                    endcase

                    `CHECK(dut.accepted_bank_cmd_valid_q == expected_cmd_valid,
                           "Unexpected bank_cmd_valid routing at acceptance")
                    `CHECK(dut.accepted_txn_cmd_type_q == (is_write ? 2'b10 : 2'b01),
                           "Unexpected txn_cmd_type mapping")
                    `CHECK(dut.accepted_bank_q == TXN_BANK_WIDTH'(bank_sel),
                           "Accepted bank visibility should match the driven bank")
                    `CHECK(dut.accepted_requested_row_q == ROW_WIDTH'(row_sel),
                           "Accepted row visibility should match the driven row")
                    `CHECK(dut.accepted_requested_col_q == COL_WIDTH'(col_sel),
                           "Accepted column visibility should match the driven column")
                    if (detail_logging_enabled()) begin
                        if (dut.accepted_row_closed_q) begin
                            $display("[ACPT][id=%0d][cycle=%0d] accepted (row_closed)",
                                     request_id, cycle);
                            $display("[ROW ][id=%0d][cycle=%0d] CLOSED -> OPEN bank=%0d row=%0d",
                                     request_id, cycle, bank_sel, row_sel);
                        end else if (dut.accepted_row_hit_q) begin
                            $display("[ACPT][id=%0d][cycle=%0d] accepted (row_hit)",
                                     request_id, cycle);
                            $display("[ROW ][id=%0d][cycle=%0d] HIT bank=%0d row=%0d",
                                     request_id, cycle, bank_sel, row_sel);
                        end else if (dut.accepted_row_miss_q) begin
                            $display("[ACPT][id=%0d][cycle=%0d] accepted (row_miss)",
                                     request_id, cycle);
                            $display("[ROW ][id=%0d][cycle=%0d] MISS bank=%0d old=%0d new=%0d",
                                     request_id, cycle, bank_sel, dut.accepted_prev_open_row_q, row_sel);
                        end
                        report_accepted_row_context(request_id);

                        if (is_write) begin
                            $display("[WRITE][id=%0d][cycle=%0d] bank=%0d addr=%0d <= 0x%08h",
                                     request_id, cycle, bank_sel, make_addr(row_sel, col_sel), req_data);
                        end else begin
                            $display("[READ ][id=%0d][cycle=%0d] bank=%0d addr=%0d",
                                     request_id, cycle, bank_sel, make_addr(row_sel, col_sel));
                        end
                    end

                    if (detailed_log_budget > 0) begin
                        detailed_log_budget = detailed_log_budget - 1;
                        if ((detailed_log_budget == 0) && !detailed_log_notice_emitted) begin
                            detailed_log_notice_emitted = 1'b1;
                            $display("[INFO] %s | time=%0t | Suppressing additional per-access logs for %s after %0d accepted transactions",
                                     test_name, $time, detailed_log_context, PATTERN_DETAIL_LOG_LIMIT);
                        end
                    end
                end else begin
                    wait_count = wait_count + 1;
                end
            end

            `CHECK(accept_cycle >= 0, "Transaction was never accepted")
            @(negedge clk);
            txn_valid = 1'b0;
            txn_is_write = 1'b0;
            txn_wdata = '0;
        end
    endtask

    task automatic wait_for_service_completion(
        output int completion_cycle
    );
        int wait_count;
        bit saw_pending;
        begin
            wait_count = 0;
            saw_pending = (dut.service_pending_q === 1'b1);
            completion_cycle = -1;

            while ((completion_cycle < 0) && (wait_count < MAX_CYCLES)) begin
                @(posedge clk);
                #1;
                if (dut.service_pending_q === 1'b1) begin
                    saw_pending = 1'b1;
                end else if (saw_pending) begin
                    completion_cycle = cycle;
                end
                wait_count = wait_count + 1;
            end

            `CHECK(saw_pending, "Service stage never became active after acceptance")
            `CHECK(completion_cycle >= 0, "Service stage never completed")
        end
    endtask

    task automatic issue_write_and_wait_complete(
        input int bank_sel,
        input int row_sel,
        input int col_sel,
        input logic [DATA_WIDTH-1:0] data,
        input int expected_row_class,
        input int expected_latency,
        output int observed_latency,
        input string phase_msg
    );
        int accept_cycle;
        int completion_cycle;
        int request_id;
        bit log_this_transaction;
        logic [ADDR_WIDTH-1:0] addr;
        begin
            addr = make_addr(row_sel, col_sel);
            log_this_transaction = detail_logging_enabled();
            drive_request(bank_sel, 1'b1, row_sel, col_sel, data, request_id);
            wait_for_accept_and_classify(request_id, bank_sel, 1'b1, row_sel, col_sel, data,
                                         expected_row_class, accept_cycle);
            wait_for_service_completion(completion_cycle);
            observed_latency = completion_cycle - accept_cycle;
            if (log_this_transaction) begin
                $display("[COMP ][id=%0d][cycle=%0d] latency=%0d",
                         request_id, cycle, observed_latency);
                `INFO(phase_msg)
            end
            `CHECK(observed_latency == expected_latency,
                   "Unexpected WRITE service latency")
            `CHECK(rsp_valid == 1'b0,
                   "WRITE should not raise rsp_valid")
            `CHECK(dut.bank_mem[bank_sel][addr] == data,
                   "WRITE did not update the expected bank/address storage")
        end
    endtask

    task automatic issue_read_and_wait_response(
        input int bank_sel,
        input int row_sel,
        input int col_sel,
        input logic [DATA_WIDTH-1:0] expected_data,
        input int expected_row_class,
        input int expected_latency,
        output int observed_latency,
        input string phase_msg
    );
        int accept_cycle;
        int completion_cycle;
        int request_id;
        bit log_this_transaction;
        begin
            log_this_transaction = detail_logging_enabled();
            drive_request(bank_sel, 1'b0, row_sel, col_sel, '0, request_id);
            wait_for_accept_and_classify(request_id, bank_sel, 1'b0, row_sel, col_sel, '0,
                                         expected_row_class, accept_cycle);
            wait_for_service_completion(completion_cycle);
            observed_latency = completion_cycle - accept_cycle;
            if (log_this_transaction) begin
                `INFO(phase_msg)
            end
            `CHECK(observed_latency == expected_latency,
                   "Unexpected READ service latency")
            `CHECK(rsp_valid == 1'b1,
                   "READ should raise rsp_valid when service completes")
            `CHECK(rsp_rdata == expected_data,
                   "Unexpected read data returned")
            if (log_this_transaction) begin
                $display("[RSP  ][id=%0d][cycle=%0d] data=0x%08h",
                         request_id, cycle, rsp_rdata);
                $display("[COMP ][id=%0d][cycle=%0d] latency=%0d",
                         request_id, cycle, observed_latency);
            end
            @(posedge clk);
            #1;
            `CHECK(rsp_valid == 1'b0,
                   "rsp_valid should pulse for one cycle per READ")
        end
    endtask

    initial clk = 1'b0;
    always #5 clk = ~clk;

    always @(posedge clk) begin
        if (!rst_n) begin
            cycle <= 0;
        end else begin
            cycle <= cycle + 1;

            if (cycle >= MAX_CYCLES && !results_reported) begin
                `CHECK(0, "Timeout reached")
                report_results();
                $finish;
            end
        end
    end

{tFAW_check_block}{tRRD_check_block}
    always @(posedge clk) begin
        if (!rst_n) begin
            saw_backpressure <= 1'b0;
            saw_tRRD_block <= 1'b0;
            saw_row_closed <= 1'b0;
            saw_row_hit <= 1'b0;
            saw_row_miss <= 1'b0;
{tfaw_reset_block}        end else begin
            if (!saw_backpressure && (txn_valid === 1'b1) && (cmd_ready === 1'b0)) begin
                saw_backpressure <= 1'b1;
            end
{tRRD_monitor_block}{tfaw_monitor_block}            if (dut.accept_txn && dut.is_row_closed) begin
                saw_row_closed <= 1'b1;
            end
            if (dut.accept_txn && dut.is_row_hit) begin
                saw_row_hit <= 1'b1;
            end
            if (dut.accept_txn && dut.is_row_miss) begin
                saw_row_miss <= 1'b1;
            end
        end
    end
{scheduler_monitor_block}
    always @(posedge clk) begin
        if (rst_n && txn_valid && !cmd_ready && detail_logging_enabled()) begin
            $display("[STALL][id=%0d][cycle=%0d] txn blocked (cmd_ready=0)",
                     active_txn_id, cycle);
        end
    end
{tRRD_log_block}

    initial begin
        int refresh_start_cycle;
        int bank_index;
        int closed_latency;
        int hit_latency;
        int miss_latency;
        int hit_latency_after_miss;
        int closed_readback_latency;
        int bank1_closed_latency;
        int bank1_hit_latency;
        int bank0_hit_latency_after_isolation;
        int pattern_latency;
        int pattern_accept_start;
        int pattern_hit_start;
        int pattern_nonhit_start;
        int pattern_hit_latency_total_start;
        int pattern_nonhit_latency_total_start;
        int pattern_hit_count_start;
        int pattern_nonhit_count_start;
        int pattern_stall_busy_start;
        int pattern_stall_trrd_start;
        int pattern_stall_refresh_start;
        int pattern_stall_other_start;
        int pattern_tfaw_admission_start;
        int pattern_tfaw_hard_block_start;

        txn_valid = 1'b0;
        txn_is_write = 1'b0;
        txn_addr = '0;
        txn_wdata = '0;
{("        txn_bank = '0;\n" if design.bank_count > 1 else "")}        rst_n = 1'b0;
        txn_id = 0;
        active_txn_id = 0;
        detailed_log_budget = -1;
        detailed_log_context = "";
        detailed_log_notice_emitted = 1'b0;
        traffic_started = 1'b0;
        saw_backpressure = 1'b0;
        saw_tRRD_block = 1'b0;
        saw_row_closed = 1'b0;
        saw_row_hit = 1'b0;
        saw_row_miss = 1'b0;
{tfaw_init_block}
{scheduler_init_block}
        for (bank_index = 0; bank_index < COL_COUNT; bank_index = bank_index + 1) begin
            high_locality_expected_data[bank_index] = '0;
        end

        $display("===== DDR4 MEMORY SYSTEM DEMO START =====");
        log_phase("RESET");
        `INFO("Reset phase")
        wait_cycles(4);
        rst_n = 1'b1;
        wait_cycles(2);
        `CHECK(cmd_ready == 1'b1,
               "cmd_ready should be high after reset release")

        for (bank_index = 0; bank_index < BANK_COUNT; bank_index = bank_index + 1) begin
            `CHECK(dut.row_open_valid[bank_index] == 1'b0,
                   "All banks should begin with rows closed")
        end
{scheduler_sequence_block}
{row_buffer_sequence_block}
{bank_isolation_block}
{pattern_sequence_block}
{timing_stress_block}
        log_phase("CORNER CASES");
        `INFO("Corner cases")
        wait(cmd_ready === 1'b0);
        `INFO("Observed backpressure while controller was busy")
{refresh_sequence_block}
        `INFO("=== TEST END ===");
        $display("===== TEST COMPLETE =====");
    end

    initial begin
        #SIM_END_TIME;
        report_results();
        $finish;
    end

    initial begin
        $dumpfile("{tb_module}.vcd");
        $dumpvars(0, {tb_module});
    end

endmodule
"""


def build_testbench(top_module: str, design: DesignContext) -> str:
    """Return a deterministic self-checking testbench for the requested top module."""
    return build_phase1_row_buffer_testbench(top_module, design)

    tb_module = f"tb_{top_module}"
    refresh_wait_cycles = compute_refresh_wait_cycles(design.refresh_cycles)
    txn_bank_width = max(bank_select_width(design.bank_count), 1)

    coverage_flag_lines: list[str] = []
    coverage_check_lines: list[str] = []
    coverage_monitor_blocks: list[str] = []
    immediate_check_blocks: list[str] = []
    helper_state_lines = ["    bit traffic_started = 0;"]
    scheduler_decl_block = ""
    scheduler_monitor_block = ""
    scheduler_sequence_block = ""
    scheduler_task_block = ""
    bank_signal_decl_block = ""
    bank_task_block = ""
    bank_sequence_block = ""
    memory_task_block = ""
    memory_sequence_block = ""

    dut_port_lines = [
        f"    {top_module} dut (",
        "        .clk(clk),",
        "        .rst_n(rst_n),",
        "        .txn_valid(txn_valid),",
        "        .txn_is_write(txn_is_write),",
        "        .txn_addr(txn_addr),",
        "        .txn_wdata(txn_wdata),",
    ]
    if design.bank_count > 1:
        bank_signal_decl_block = f"    logic [{txn_bank_width - 1}:0] txn_bank;\n"
        dut_port_lines.append("        .txn_bank(txn_bank),")
    dut_port_lines.extend(
        [
            "        .cmd_ready(cmd_ready),",
            "        .rsp_valid(rsp_valid),",
            "        .rsp_rdata(rsp_rdata)",
        ]
    )
    dut_port_lines.append("    );")
    dut_instantiation_block = "\n".join(dut_port_lines)

    if design.has_scheduler:
        scheduler_decl_block = """
    /*verilator tracing_off*/
    logic sched_ref_req;
    logic sched_cmd_ready;
    logic sched_issue_ref;
    logic sched_issue_txn;
    logic [1:0] sched_sel_idx;
    logic sched_issue_valid;
    typedef struct packed {
        logic [1:0]  bank;
        logic [9:0]  row;
        logic [5:0]  col;
        logic        is_write;
        logic [31:0] wdata;
    } sched_request_t;
    sched_request_t sched_req_array [4];
    logic sched_req_valid [4];
    logic [3:0] sched_bank_active;
    logic [9:0] sched_bank_open_row [4];
    logic [203:0] sched_req_array_packed;
    logic [3:0] sched_req_valid_packed;
    logic [39:0] sched_bank_open_row_packed;

    assign sched_req_array_packed[50:0] = sched_req_array[0];
    assign sched_req_array_packed[101:51] = sched_req_array[1];
    assign sched_req_array_packed[152:102] = sched_req_array[2];
    assign sched_req_array_packed[203:153] = sched_req_array[3];
    assign sched_req_valid_packed = {sched_req_valid[3], sched_req_valid[2], sched_req_valid[1], sched_req_valid[0]};
    assign sched_bank_open_row_packed[9:0] = sched_bank_open_row[0];
    assign sched_bank_open_row_packed[19:10] = sched_bank_open_row[1];
    assign sched_bank_open_row_packed[29:20] = sched_bank_open_row[2];
    assign sched_bank_open_row_packed[39:30] = sched_bank_open_row[3];

    ddr4_scheduler_scheduler sched_policy_dut (
        .clk(clk),
        .rst_n(rst_n),
        .ref_req(sched_ref_req),
        .bank_active(sched_bank_active),
        .bank_open_row(sched_bank_open_row_packed),
        .req_array(sched_req_array_packed),
        .req_valid(sched_req_valid_packed),
        .cmd_ready(sched_cmd_ready),
        .timing_ok(1'b1),
        .sel_idx(sched_sel_idx),
        .issue_valid(sched_issue_valid),
        .issue_ref(sched_issue_ref),
        .issue_txn(sched_issue_txn)
    );
    /*verilator tracing_on*/
"""

        scheduler_monitor_block = """
    always @(posedge clk) begin
        if (rst_n) begin
            `CHECK(!(sched_issue_ref && sched_issue_txn),
                   "Scheduler issued refresh and transaction simultaneously")
        end
    end
"""

        if design.scheduler_mode == "round_robin":
            scheduler_sequence_block = """
        `INFO("Scheduler policy checks: round_robin")
        scheduler_idle_cycle();
        scheduler_expect(1'b1, 1'b0, 1'b1, 1'b1, 1'b0,
                         "Scheduler case 1: only refresh issues refresh");
        scheduler_idle_cycle();
        scheduler_expect(1'b0, 1'b1, 1'b1, 1'b0, 1'b1,
                         "Scheduler case 2: only transaction issues transaction");
        scheduler_idle_cycle();
        scheduler_expect(1'b1, 1'b1, 1'b0, 1'b0, 1'b0,
                         "Scheduler case 3: contention with cmd_ready low issues nothing");
        scheduler_expect(1'b1, 1'b1, 1'b1, 1'b1, 1'b0,
                         "Scheduler case 4: first successful contention issues refresh");
        scheduler_idle_cycle();
        scheduler_expect(1'b0, 1'b1, 1'b1, 1'b0, 1'b1,
                         "Scheduler case 5: uncontested transaction still issues transaction");
        scheduler_idle_cycle();
        scheduler_expect(1'b1, 1'b1, 1'b1, 1'b0, 1'b1,
                         "Scheduler case 6: next contention issues transaction after turn flip");
        scheduler_idle_cycle();
        scheduler_expect(1'b1, 1'b1, 1'b1, 1'b1, 1'b0,
                         "Scheduler case 7: following contention flips back to refresh");
        scheduler_idle_cycle();
        scheduler_row_hit_policy_checks();
"""
        else:
            scheduler_sequence_block = """
        `INFO("Scheduler policy checks: simple")
        scheduler_idle_cycle();
        scheduler_expect(1'b1, 1'b0, 1'b1, 1'b1, 1'b0,
                         "Scheduler case 1: only refresh issues refresh");
        scheduler_idle_cycle();
        scheduler_expect(1'b0, 1'b1, 1'b1, 1'b0, 1'b1,
                         "Scheduler case 2: only transaction issues transaction");
        scheduler_idle_cycle();
        scheduler_expect(1'b1, 1'b1, 1'b0, 1'b0, 1'b0,
                         "Scheduler case 3: contention with cmd_ready low issues nothing");
        scheduler_expect(1'b1, 1'b1, 1'b1, 1'b1, 1'b0,
                         "Scheduler case 4: contention grants refresh");
        scheduler_idle_cycle();
        scheduler_expect(1'b0, 1'b1, 1'b1, 1'b0, 1'b1,
                         "Scheduler case 5: uncontested transaction still issues transaction");
        scheduler_idle_cycle();
        scheduler_expect(1'b1, 1'b1, 1'b1, 1'b1, 1'b0,
                         "Scheduler case 6: later contention still grants refresh");
        scheduler_idle_cycle();
        scheduler_row_hit_policy_checks();
"""

        scheduler_task_block = """
    task automatic scheduler_idle_cycle;
        begin
            @(negedge clk);
            sched_ref_req = 1'b0;
            sched_req_valid[0] = 1'b0;
            sched_req_valid[1] = 1'b0;
            sched_req_valid[2] = 1'b0;
            sched_req_valid[3] = 1'b0;
            sched_req_array[0] = '0;
            sched_req_array[1] = '0;
            sched_req_array[2] = '0;
            sched_req_array[3] = '0;
            sched_bank_active = 4'b0000;
            sched_bank_open_row[0] = '0;
            sched_bank_open_row[1] = '0;
            sched_bank_open_row[2] = '0;
            sched_bank_open_row[3] = '0;
            sched_cmd_ready = 1'b1;
            @(posedge clk);
            #1;
            `CHECK((sched_issue_ref == 1'b0) && (sched_issue_txn == 1'b0),
                   "Scheduler should be idle when no requests are asserted")
        end
    endtask

    task automatic scheduler_expect(
        input logic ref_req_i,
        input logic txn_valid_i,
        input logic cmd_ready_i,
        input logic exp_issue_ref,
        input logic exp_issue_txn,
        input string phase_msg
    );
        begin
            @(negedge clk);
            sched_ref_req = ref_req_i;
            sched_req_valid[0] = txn_valid_i;
            sched_req_valid[1] = 1'b0;
            sched_req_valid[2] = 1'b0;
            sched_req_valid[3] = 1'b0;
            sched_req_array[0] = '0;
            sched_req_array[1] = '0;
            sched_req_array[2] = '0;
            sched_req_array[3] = '0;
            sched_bank_active = 4'b0000;
            sched_bank_open_row[0] = '0;
            sched_bank_open_row[1] = '0;
            sched_bank_open_row[2] = '0;
            sched_bank_open_row[3] = '0;
            sched_cmd_ready = cmd_ready_i;
            @(posedge clk);
            #1;
            `INFO(phase_msg)
            `CHECK(sched_issue_ref == exp_issue_ref,
                   "Unexpected scheduler issue_ref result")
            `CHECK(sched_issue_txn == exp_issue_txn,
                   "Unexpected scheduler issue_txn result")
        end
    endtask

    task automatic scheduler_row_hit_policy_checks;
        begin
            scheduler_idle_cycle();

            @(negedge clk);
            sched_ref_req = 1'b0;
            sched_cmd_ready = 1'b1;
            sched_req_array[0] = '0;
            sched_req_array[1] = '0;
            sched_req_array[0].bank = 2'd0;
            sched_req_array[0].row = 10'd3;
            sched_req_array[1].bank = 2'd1;
            sched_req_array[1].row = 10'd9;
            sched_req_valid[0] = 1'b1;
            sched_req_valid[1] = 1'b1;
            sched_req_valid[2] = 1'b0;
            sched_req_valid[3] = 1'b0;
            sched_bank_active = 4'b0010;
            sched_bank_open_row[0] = 10'd0;
            sched_bank_open_row[1] = 10'd9;
            sched_bank_open_row[2] = 10'd0;
            sched_bank_open_row[3] = 10'd0;
            @(posedge clk);
            #1;
            `INFO("Scheduler row-hit priority: req[1] beats non-hit req[0]")
            `CHECK(sched_sel_idx == 2'd1,
                   "Scheduler should select row-hit req[1]")
            `CHECK(sched_issue_txn == 1'b1,
                   "Scheduler should issue the selected row-hit transaction")

            scheduler_idle_cycle();

            @(negedge clk);
            sched_ref_req = 1'b0;
            sched_cmd_ready = 1'b1;
            sched_req_array[0] = '0;
            sched_req_array[1] = '0;
            sched_req_array[0].bank = 2'd0;
            sched_req_array[0].row = 10'd3;
            sched_req_array[1].bank = 2'd1;
            sched_req_array[1].row = 10'd9;
            sched_req_valid[0] = 1'b1;
            sched_req_valid[1] = 1'b1;
            sched_req_valid[2] = 1'b0;
            sched_req_valid[3] = 1'b0;
            sched_bank_active = 4'b0000;
            sched_bank_open_row[0] = 10'd0;
            sched_bank_open_row[1] = 10'd0;
            sched_bank_open_row[2] = 10'd0;
            sched_bank_open_row[3] = 10'd0;
            @(posedge clk);
            #1;
            `INFO("Scheduler fallback: no row hits selects first valid")
            `CHECK(sched_sel_idx == 2'd0,
                   "Scheduler should fall back to req[0] when no row hits exist")

            scheduler_idle_cycle();

            @(negedge clk);
            sched_ref_req = 1'b0;
            sched_cmd_ready = 1'b0;
            sched_req_array[0] = '0;
            sched_req_array[1] = '0;
            sched_req_array[0].bank = 2'd0;
            sched_req_array[0].row = 10'd3;
            sched_req_valid[0] = 1'b1;
            sched_req_valid[1] = 1'b0;
            sched_req_valid[2] = 1'b0;
            sched_req_valid[3] = 1'b0;
            sched_bank_active = 4'b0000;
            sched_bank_open_row[0] = 10'd0;
            sched_bank_open_row[1] = 10'd0;
            sched_bank_open_row[2] = 10'd0;
            sched_bank_open_row[3] = 10'd0;
            @(posedge clk);
            #1;
            `INFO("Scheduler lock behavior: req[0] selected while blocked")
            `CHECK(sched_sel_idx == 2'd0,
                   "Scheduler should select req[0] before lock")

            @(negedge clk);
            sched_req_array[1].bank = 2'd1;
            sched_req_array[1].row = 10'd9;
            sched_req_valid[1] = 1'b1;
            sched_bank_active = 4'b0010;
            sched_bank_open_row[1] = 10'd9;
            @(posedge clk);
            #1;
            `INFO("Scheduler lock behavior: later row hit does not reselect")
            `CHECK(sched_sel_idx == 2'd0,
                   "Scheduler should keep locked req[0] when req[1] becomes a row hit")

            @(negedge clk);
            sched_cmd_ready = 1'b1;
            sched_req_valid[1] = 1'b0;
            @(posedge clk);
            #1;
            scheduler_idle_cycle();
        end
    endtask
"""

    if design.has_handshake:
        coverage_flag_lines.append("    bit saw_backpressure = 0;")
        coverage_monitor_blocks.append(
            """    always @(posedge clk) begin
        if (!rst_n) begin
            saw_backpressure <= 1'b0;
        end else if (!saw_backpressure &&
                     traffic_started &&
                     (cmd_ready === 1'b0)) begin
            saw_backpressure <= 1'b1;
        end
    end"""
        )

    if design.has_tfaw and design.tfaw_instance is not None:
        immediate_check_blocks.append(
            f"""    always @(posedge clk) begin
        if (rst_n && dut.{design.tfaw_instance}.tFAW_block) begin
            `CHECK(!dut.issue_txn,
                   "tFAW violation: issue_txn during block")
        end
    end"""
        )

        if design.tfaw_limit_reachable:
            coverage_flag_lines.append("    bit saw_tFAW_limit = 0;")
            coverage_check_lines.append('            `CHECK(saw_tFAW_limit, "tFAW limit was never reached")')
            coverage_monitor_blocks.append(
                f"""    always @(posedge clk) begin
        if (!rst_n) begin
            saw_tFAW_limit <= 1'b0;
        end else if (!saw_tFAW_limit &&
                     dut.{design.tfaw_instance}.tFAW_block) begin
            saw_tFAW_limit <= 1'b1;
        end
    end"""
            )

    if design.has_trrd and design.trrd_instance is not None:
        coverage_flag_lines.append("    bit saw_tRRD_block = 0;")
        coverage_check_lines.append('            `CHECK(saw_tRRD_block, "tRRD block never occurred")')
        coverage_monitor_blocks.append(
            f"""    always @(posedge clk) begin
        if (!rst_n) begin
            saw_tRRD_block <= 1'b0;
        end else if (!saw_tRRD_block &&
                     dut.{design.trrd_instance}.tRRD_block) begin
            saw_tRRD_block <= 1'b1;
        end
    end"""
        )
        immediate_check_blocks.append(
            f"""    always @(posedge clk) begin
        if (rst_n && dut.{design.trrd_instance}.tRRD_block && dut.act_pulse) begin
            `CHECK(0, "tRRD violation: act during block")
        end
    end"""
        )

    if design.has_activate_fsm and design.activate_fsm_instance is not None:
        immediate_check_blocks.append(
            f"""    always @(posedge clk) begin
        if (rst_n && dut.u_bank0.u_activate_fsm.tRCD_done) begin
            `CHECK(dut.u_bank0.u_activate_fsm.current_state == dut.u_bank0.u_activate_fsm.DONE,
                   "bank 0 tRCD_done asserted outside DONE state")
        end
    end"""
        )

    if design.bank_count > 1:
        routed_cmd_type_checks = build_multibank_cmd_type_checks(
            design.bank_count,
            "bank_sel",
            "expected_cmd_type",
        )
        blocked_cmd_type_checks = build_all_bank_cmd_type_zero_checks(design.bank_count, indent="        ")
        bank_task_lines = [
            "    task automatic expect_selected_ready(",
            "        input int bank_sel,",
            "        input logic expected_ready,",
            "        input string phase_msg",
            "    );",
            "        begin",
            "            @(negedge clk);",
            "            txn_bank = bank_sel[TXN_BANK_WIDTH-1:0];",
            "            #1;",
            "            `INFO(phase_msg)",
            '            `CHECK(cmd_ready == expected_ready, "Unexpected selected-bank cmd_ready value")',
            '            `CHECK(cmd_ready == dut.bank_cmd_ready[bank_sel], "cmd_ready should follow the selected bank readiness")',
            "        end",
            "    endtask",
            "",
            "    task automatic wait_for_bank_ready_pattern(",
            "        input logic [BANK_COUNT-1:0] expected_ready,",
            "        input string phase_msg",
            "    );",
            "        int wait_count;",
            "        begin",
            "            wait_count = 0;",
            "            while ((dut.bank_cmd_ready !== expected_ready) &&",
            "                   (wait_count < MAX_CYCLES)) begin",
            "                @(posedge clk);",
            "                wait_count = wait_count + 1;",
            "            end",
            "            #1;",
            "            `INFO(phase_msg)",
            '            `CHECK(dut.bank_cmd_ready == expected_ready, "Unexpected bank_cmd_ready state")',
            "        end",
            "    endtask",
            "",
            "    task automatic issue_transaction_to_bank(",
            "        input int bank_sel,",
            "        input logic is_write,",
            "        input logic [1:0] expected_cmd_type,",
            "        input logic [BANK_COUNT-1:0] expected_cmd_valid,",
            "        input string phase_msg",
            "    );",
            "        begin",
            "            wait(rst_n === 1'b1);",
            "            traffic_started = 1'b1;",
            "            txn_bank = bank_sel[TXN_BANK_WIDTH-1:0];",
            "            txn_is_write = is_write;",
            "            txn_addr = ADDR_WIDTH'(bank_sel);",
            "            txn_wdata = DATA_WIDTH'(32'hA5000000 + bank_sel);",
            "            wait(dut.act_allowed === 1'b1);",
            "            wait(cmd_ready === 1'b1);",
            "            @(negedge clk);",
            "            txn_bank = bank_sel[TXN_BANK_WIDTH-1:0];",
            "            txn_is_write = is_write;",
            "            txn_addr = ADDR_WIDTH'(bank_sel);",
            "            txn_wdata = DATA_WIDTH'(32'hA5000000 + bank_sel);",
            "            txn_valid = 1'b1;",
            "            @(posedge clk);",
            "            #1;",
            "            `INFO(phase_msg)",
            '            `CHECK(dut.bank_cmd_valid == expected_cmd_valid, "Unexpected bank routing result")',
            '            `CHECK(dut.txn_cmd_type == expected_cmd_type, "Unexpected txn_is_write to txn_cmd_type mapping")',
            *routed_cmd_type_checks,
            "            txn_valid = 1'b0;",
            "            txn_is_write = 1'b0;",
            "            txn_wdata = '0;",
            "        end",
            "    endtask",
        ]
        if design.has_trrd and design.trrd_instance is not None:
            bank_task_lines.extend(
                [
                    "",
                    "    task automatic wait_for_shared_trrd_block(input string phase_msg);",
                    "        int wait_count;",
                    "        begin",
                    "            wait_count = 0;",
                    "            while ((dut.tRRD_block !== 1'b1) && (wait_count < MAX_CYCLES)) begin",
                    "                @(posedge clk);",
                    "                wait_count = wait_count + 1;",
                    "            end",
                    "            #1;",
                    "            `INFO(phase_msg)",
                    '            `CHECK(dut.tRRD_block == 1\'b1, "Expected a shared tRRD block to become active")',
                    "        end",
                    "    endtask",
                ]
            )
        bank_task_block = "\n".join(bank_task_lines) + "\n"

        bank_sequence_lines = [
            f'        `INFO("{design.bank_count}-bank routing checks")',
            f'        `CHECK(dut.bank_cmd_ready == {bank_vector_literal(design.bank_count, set(range(design.bank_count)))},',
            '               "All banks should be ready after reset release")',
        ]
        for bank_index in range(design.bank_count):
            bank_sequence_lines.append(
                f'        expect_selected_ready({bank_index}, 1\'b1, "Selected bank {bank_index} reports ready after reset");'
            )
        bank_sequence_lines.append("")

        for bank_index in range(design.bank_count):
            if design.has_trrd and design.trrd_instance is not None and bank_index > 0:
                bank_sequence_lines.append("        wait(dut.tRRD_block === 1'b0);")

            busy_bank_set = set(range(design.bank_count)) - {bank_index}
            routing_bank_set = {bank_index}
            free_bank_index = (bank_index + 1) % design.bank_count
            is_write = (bank_index % 2) == 1
            txn_kind = "WRITE" if is_write else "READ"

            bank_sequence_lines.extend(
                [
                    f"        issue_transaction_to_bank({bank_index}, 1'b{1 if is_write else 0}, {cmd_type_literal(is_write)}, {bank_vector_literal(design.bank_count, routing_bank_set)}, \"{txn_kind} request routes only to bank {bank_index} and preserves {txn_kind} cmd_type\");",
                    f'        wait_for_bank_ready_pattern({bank_vector_literal(design.bank_count, busy_bank_set)}, "Bank {bank_index} busy while non-selected banks remain free");',
                    f'        expect_selected_ready({bank_index}, 1\'b0, "Selected bank {bank_index} stalls when bank {bank_index} is busy");',
                    f'        expect_selected_ready({free_bank_index}, 1\'b1, "Selected bank {free_bank_index} remains ready while bank {bank_index} is busy");',
                    "",
                ]
            )

        if design.has_trrd and design.trrd_instance is not None:
            last_bank_index = design.bank_count - 1
            bank_sequence_lines.extend(
                [
                    f'        wait_for_bank_ready_pattern({bank_vector_literal(design.bank_count, set(range(design.bank_count)))}, "All banks ready before shared tRRD gating check");',
                    '        issue_transaction_to_bank(0, 1\'b0, ' + cmd_type_literal(False) + ", " + bank_vector_literal(design.bank_count, {0}) + ', "Prime shared tRRD gate from bank 0 with a READ");',
                    '        wait_for_shared_trrd_block("Observed shared tRRD block after bank 0 activation");',
                    "        @(negedge clk);",
                    f"        txn_bank = TXN_BANK_WIDTH'({last_bank_index});",
                    "        txn_is_write = 1'b1;",
                    f"        txn_addr = ADDR_WIDTH'({last_bank_index});",
                    "        txn_wdata = DATA_WIDTH'(32'hDEADBEEF);",
                    "        txn_valid = 1'b1;",
                    "        @(posedge clk);",
                    "        #1;",
                    f'        `INFO("Shared tRRD gate blocks bank {last_bank_index} even though bank {last_bank_index} is locally ready")',
                    f"        `CHECK(cmd_ready == dut.bank_cmd_ready[{last_bank_index}],",
                    f'               "cmd_ready should still follow selected bank {last_bank_index} during shared tRRD blocking")',
                    "        `CHECK(cmd_ready == 1'b1,",
                    f'               "bank {last_bank_index} should remain locally ready while the shared tRRD gate blocks issue")',
                    "        `CHECK(dut.bank_cmd_valid == '0,",
                    '               "Shared tRRD gate should block all bank transaction issue attempts")',
                    '        `CHECK(dut.txn_cmd_type == 2\'b10, "Blocked WRITE should still map txn_is_write to WRITE txn_cmd_type")',
                    *blocked_cmd_type_checks,
                    "        txn_valid = 1'b0;",
                    "        txn_is_write = 1'b0;",
                    "        wait(dut.tRRD_block === 1'b0);",
                    f"        issue_transaction_to_bank({last_bank_index}, 1'b1, {cmd_type_literal(True)}, {bank_vector_literal(design.bank_count, {last_bank_index})}, \"Bank {last_bank_index} issues a WRITE once the shared tRRD gate clears\");",
                    "",
                ]
            )
        bank_sequence_block = "\n".join(bank_sequence_lines) + "\n"

        multibank_write_checks = build_multibank_cmd_type_checks(
            design.bank_count,
            "bank_sel",
            "2'b10",
        )
        multibank_read_checks = build_multibank_cmd_type_checks(
            design.bank_count,
            "bank_sel",
            "2'b01",
        )
        memory_task_block = "\n".join(
            [
                "    task automatic issue_write_store(",
                "        input int bank_sel,",
                "        input logic [ADDR_WIDTH-1:0] addr,",
                "        input logic [DATA_WIDTH-1:0] data,",
                "        input string phase_msg",
                "    );",
                "        logic [BANK_COUNT-1:0] expected_cmd_valid;",
                "        int wait_count;",
                "        bit accepted;",
                "        begin",
                "            wait(rst_n === 1'b1);",
                "            traffic_started = 1'b1;",
                "            expected_cmd_valid = '0;",
                "            expected_cmd_valid[bank_sel] = 1'b1;",
                "            wait_count = 0;",
                "            accepted = 1'b0;",
                "            while (rsp_valid === 1'b1) begin",
                "                @(posedge clk);",
                "            end",
                "            txn_bank = bank_sel[TXN_BANK_WIDTH-1:0];",
                "            txn_is_write = 1'b1;",
                "            txn_addr = addr;",
                "            txn_wdata = data;",
                "            wait(dut.act_allowed === 1'b1);",
                "            wait(cmd_ready === 1'b1);",
                "            @(negedge clk);",
                "            txn_bank = bank_sel[TXN_BANK_WIDTH-1:0];",
                "            txn_is_write = 1'b1;",
                "            txn_addr = addr;",
                "            txn_wdata = data;",
                "            txn_valid = 1'b1;",
                "            while (!accepted && (wait_count < MAX_CYCLES)) begin",
                "                @(posedge clk);",
                "                #1;",
                "                if (dut.bank_cmd_valid == expected_cmd_valid) begin",
                "                    accepted = 1'b1;",
                "                end else begin",
                "                    wait_count = wait_count + 1;",
                "                end",
                "            end",
                "            `INFO(phase_msg)",
                '            `CHECK(accepted, "WRITE was never accepted by the selected bank path")',
                '            `CHECK(dut.bank_cmd_valid == expected_cmd_valid, "WRITE should route only to the selected bank")',
                '            `CHECK(dut.txn_cmd_type == 2\'b10, "WRITE should map txn_is_write to WRITE txn_cmd_type")',
                *multibank_write_checks,
                '            `CHECK(rsp_valid == 1\'b0, "WRITE should not produce an immediate read response")',
                "            @(posedge clk);",
                "            #1;",
                '            `CHECK(dut.bank_mem[bank_sel][addr] == data, "WRITE did not update the selected bank/address storage")',
                "            txn_valid = 1'b0;",
                "            txn_is_write = 1'b0;",
                "            txn_wdata = '0;",
                "            @(posedge clk);",
                "            #1;",
                '            `CHECK(rsp_valid == 1\'b0, "WRITE should not raise rsp_valid")',
                "        end",
                "    endtask",
                "",
                "    task automatic issue_read_expect_data(",
                "        input int bank_sel,",
                "        input logic [ADDR_WIDTH-1:0] addr,",
                "        input logic [DATA_WIDTH-1:0] expected_data,",
                "        input string phase_msg",
                "    );",
                "        logic [BANK_COUNT-1:0] expected_cmd_valid;",
                "        int wait_count;",
                "        bit accepted;",
                "        begin",
                "            wait(rst_n === 1'b1);",
                "            traffic_started = 1'b1;",
                "            expected_cmd_valid = '0;",
                "            expected_cmd_valid[bank_sel] = 1'b1;",
                "            wait_count = 0;",
                "            accepted = 1'b0;",
                "            while (rsp_valid === 1'b1) begin",
                "                @(posedge clk);",
                "            end",
                "            txn_bank = bank_sel[TXN_BANK_WIDTH-1:0];",
                "            txn_is_write = 1'b0;",
                "            txn_addr = addr;",
                "            txn_wdata = '0;",
                "            wait(dut.act_allowed === 1'b1);",
                "            wait(cmd_ready === 1'b1);",
                "            @(negedge clk);",
                "            txn_bank = bank_sel[TXN_BANK_WIDTH-1:0];",
                "            txn_is_write = 1'b0;",
                "            txn_addr = addr;",
                "            txn_wdata = '0;",
                "            txn_valid = 1'b1;",
                "            while (!accepted && (wait_count < MAX_CYCLES)) begin",
                "                @(posedge clk);",
                "                #1;",
                "                if (dut.read_rsp_pending_q == 1'b1) begin",
                    "                    accepted = 1'b1;",
                "                end else begin",
                    "                    wait_count = wait_count + 1;",
                "                end",
                "            end",
                "            `INFO(phase_msg)",
                '            `CHECK(accepted, "READ was never accepted by the selected bank path")',
                '            `CHECK(dut.txn_cmd_type == 2\'b01, "READ should map txn_is_write to READ txn_cmd_type")',
                f'            `CHECK(rsp_valid == 1\'b0, "READ response should arrive {READ_RESPONSE_LATENCY_CYCLES} cycle after the accepted READ")',
                "            txn_valid = 1'b0;",
                "            @(posedge clk);",
                "            #1;",
                f'            `CHECK(rsp_valid == 1\'b1, "Expected read response {READ_RESPONSE_LATENCY_CYCLES} cycle after the accepted READ")',
                '            `CHECK(rsp_rdata == expected_data, "Unexpected read data returned for the selected bank/address")',
                "            @(posedge clk);",
                "            #1;",
                '            `CHECK(rsp_valid == 1\'b0, "rsp_valid should pulse for one cycle per READ response")',
                "        end",
                "    endtask",
            ]
        ) + "\n"

        same_addr = 3
        multibank_memory_lines = ['        `INFO("Memory-system phase")']
        for bank_index in range(design.bank_count):
            data_value = 0x10000000 * (bank_index + 1) + 0x11 * (bank_index + 1)
            multibank_memory_lines.append(
                f"        issue_write_store({bank_index}, ADDR_WIDTH'({same_addr}), DATA_WIDTH'(32'h{data_value:08X}), "
                f'"WRITE stores bank {bank_index} data at shared address {same_addr}");'
            )
        multibank_memory_lines.append("")
        for bank_index in range(design.bank_count):
            data_value = 0x10000000 * (bank_index + 1) + 0x11 * (bank_index + 1)
            multibank_memory_lines.append(
                f"        issue_read_expect_data({bank_index}, ADDR_WIDTH'({same_addr}), DATA_WIDTH'(32'h{data_value:08X}), "
                f'"READ returns bank {bank_index} data from shared address {same_addr}");'
            )
        memory_sequence_block = "\n".join(multibank_memory_lines) + "\n"

        issue_transaction_task_block = """
    task automatic issue_transaction(
        input logic is_write,
        input logic [1:0] expected_cmd_type,
        input string phase_msg
    );
        begin
            wait(rst_n === 1'b1);
            traffic_started = 1'b1;
            txn_bank = '0;
            txn_is_write = is_write;
            txn_addr = '0;
            txn_wdata = DATA_WIDTH'(32'hA5000001);
            wait(dut.act_allowed === 1'b1);
            wait(cmd_ready === 1'b1);
            `INFO(phase_msg)
            @(negedge clk);
            txn_bank = '0;
            txn_is_write = is_write;
            txn_addr = '0;
            txn_wdata = DATA_WIDTH'(32'hA5000001);
            txn_valid = 1'b1;
            @(posedge clk);
            #1;
            `CHECK(dut.bank_cmd_valid == {{(BANK_COUNT-1){1'b0}}, 1'b1},
                   "Expected the default stress transaction to route only to bank 0")
            `CHECK(dut.txn_cmd_type == expected_cmd_type,
                   "Unexpected txn_is_write to txn_cmd_type mapping")
            `CHECK(dut.bank0_cmd_type == expected_cmd_type,
                   "Bank 0 did not preserve the expected cmd_type")
            txn_valid = 1'b0;
            txn_is_write = 1'b0;
            txn_wdata = '0;
        end
    endtask
"""
    else:
        memory_task_block = """
    task automatic issue_write_store(
        input logic [ADDR_WIDTH-1:0] addr,
        input logic [DATA_WIDTH-1:0] data,
        input string phase_msg
    );
        int wait_count;
        bit accepted;
        begin
            wait(rst_n === 1'b1);
            traffic_started = 1'b1;
            wait_count = 0;
            accepted = 1'b0;
            while (rsp_valid === 1'b1) begin
                @(posedge clk);
            end
            txn_is_write = 1'b1;
            txn_addr = addr;
            txn_wdata = data;
            wait(dut.act_allowed === 1'b1);
            wait(cmd_ready === 1'b1);
            @(negedge clk);
            txn_is_write = 1'b1;
            txn_addr = addr;
            txn_wdata = data;
            txn_valid = 1'b1;
            while (!accepted && (wait_count < MAX_CYCLES)) begin
                @(posedge clk);
                #1;
                if (dut.bank_cmd_valid[0] == 1'b1) begin
                    accepted = 1'b1;
                end else begin
                    wait_count = wait_count + 1;
                end
            end
            `INFO(phase_msg)
            `CHECK(accepted,
                   "WRITE was never accepted by the single-bank path")
            `CHECK(dut.bank_cmd_valid[0] == 1'b1,
                   "WRITE should issue to bank 0 in the single-bank wrapper")
            `CHECK(dut.txn_cmd_type == 2'b10,
                   "WRITE should map txn_is_write to WRITE txn_cmd_type")
            `CHECK(dut.bank0_cmd_type == 2'b10,
                   "Single-bank WRITE did not preserve the expected cmd_type")
            `CHECK(rsp_valid == 1'b0,
                   "WRITE should not produce an immediate read response")
            @(posedge clk);
            #1;
            `CHECK(dut.bank_mem[0][addr] == data,
                   "WRITE did not update the single-bank storage")
            txn_valid = 1'b0;
            txn_is_write = 1'b0;
            txn_wdata = '0;
            @(posedge clk);
            #1;
            `CHECK(rsp_valid == 1'b0,
                   "WRITE should not raise rsp_valid")
        end
    endtask

    task automatic issue_read_expect_data(
        input logic [ADDR_WIDTH-1:0] addr,
        input logic [DATA_WIDTH-1:0] expected_data,
        input string phase_msg
    );
        int wait_count;
        bit accepted;
        begin
            wait(rst_n === 1'b1);
            traffic_started = 1'b1;
            wait_count = 0;
            accepted = 1'b0;
            while (rsp_valid === 1'b1) begin
                @(posedge clk);
            end
            txn_is_write = 1'b0;
            txn_addr = addr;
            txn_wdata = '0;
            wait(dut.act_allowed === 1'b1);
            wait(cmd_ready === 1'b1);
            @(negedge clk);
            txn_is_write = 1'b0;
            txn_addr = addr;
            txn_wdata = '0;
            txn_valid = 1'b1;
            while (!accepted && (wait_count < MAX_CYCLES)) begin
                @(posedge clk);
                #1;
                if (dut.read_rsp_pending_q == 1'b1) begin
                    accepted = 1'b1;
                end else begin
                    wait_count = wait_count + 1;
                end
            end
            `INFO(phase_msg)
            `CHECK(accepted,
                   "READ was never accepted by the single-bank path")
            `CHECK(dut.txn_cmd_type == 2'b01,
                   "READ should map txn_is_write to READ txn_cmd_type")
            `CHECK(rsp_valid == 1'b0,
                   "READ response should arrive one cycle after the accepted READ")
            txn_valid = 1'b0;
            @(posedge clk);
            #1;
            `CHECK(rsp_valid == 1'b1,
                   "Expected read response one cycle after the accepted READ")
            `CHECK(rsp_rdata == expected_data,
                   "Unexpected read data returned from the single-bank storage model")
            @(posedge clk);
            #1;
            `CHECK(rsp_valid == 1'b0,
                   "rsp_valid should pulse for one cycle per READ response")
        end
    endtask
"""

        issue_transaction_task_block = """
    task automatic issue_transaction(
        input logic is_write,
        input logic [1:0] expected_cmd_type,
        input string phase_msg
    );
        begin
            wait(rst_n === 1'b1);
            traffic_started = 1'b1;
            txn_is_write = is_write;
            txn_addr = '0;
            txn_wdata = DATA_WIDTH'(32'hA5000001);
            wait(dut.act_allowed === 1'b1);
            wait(cmd_ready === 1'b1);
            `INFO(phase_msg)
            @(negedge clk);
            txn_is_write = is_write;
            txn_addr = '0;
            txn_wdata = DATA_WIDTH'(32'hA5000001);
            txn_valid = 1'b1;
            @(posedge clk);
            #1;
            `CHECK(dut.bank_cmd_valid[0] == 1'b1,
                   "Expected the single-bank wrapper to issue one bank command")
            `CHECK(dut.txn_cmd_type == expected_cmd_type,
                   "Unexpected txn_is_write to txn_cmd_type mapping")
            `CHECK(dut.bank0_cmd_type == expected_cmd_type,
                   "Single-bank path did not preserve the expected cmd_type")
            txn_valid = 1'b0;
            txn_is_write = 1'b0;
            txn_wdata = '0;
        end
    endtask
"""
        memory_sequence_block = "\n".join(
            [
                '        `INFO("Memory-system phase")',
                '        issue_write_store(ADDR_WIDTH\'(2), DATA_WIDTH\'(32\'hCAFE0001), "WRITE stores single-bank data at address 2");',
                '        issue_read_expect_data(ADDR_WIDTH\'(2), DATA_WIDTH\'(32\'hCAFE0001), "READ returns single-bank data from address 2");',
                '        issue_write_store(ADDR_WIDTH\'(7), DATA_WIDTH\'(32\'h5A5A00F0), "WRITE updates a second single-bank address");',
                '        issue_read_expect_data(ADDR_WIDTH\'(7), DATA_WIDTH\'(32\'h5A5A00F0), "READ returns the updated second single-bank value");',
            ]
        ) + "\n"

    coverage_state_block = ""
    state_lines = [*helper_state_lines, *coverage_flag_lines]
    if state_lines:
        coverage_state_block = join_lines(["", *state_lines])

    coverage_check_block = "        begin\n        end"
    if coverage_check_lines:
        coverage_check_block = join_lines(["        begin", *coverage_check_lines, "        end"])

    immediate_check_block = ""
    if immediate_check_blocks:
        immediate_check_block = "\n\n" + "\n\n".join(immediate_check_blocks)

    coverage_monitor_block = ""
    if coverage_monitor_blocks:
        coverage_monitor_block = "\n\n" + "\n\n".join(coverage_monitor_blocks)
    if scheduler_monitor_block:
        coverage_monitor_block += "\n\n" + scheduler_monitor_block.strip("\n")

    stress_phase_lines = "\n".join(
        f"        issue_transaction(1'b{1 if (index % 2) else 0}, {cmd_type_literal((index % 2) == 1)}, "
        f'"Case {index + 2}: {"WRITE" if (index % 2) else "READ"} back-to-back request");'
        for index in range(8)
    )

    if design.bank_count > 1:
        stimulus_phase_block = '\n'.join(
            [
                '        `INFO("Stimulus phase")',
                '        `INFO("Multi-bank READ/WRITE traffic was exercised in the directed bank-routing phase")',
                "        wait_cycles(4);",
                "",
                memory_sequence_block.rstrip(),
                "",
                '        `INFO("Stress phase")',
                '        `INFO("Multi-bank stress coverage comes from the directed bank-routing and shared-gating checks")',
            ]
        )
    else:
        stimulus_phase_block = '\n'.join(
            [
                '        `INFO("Stimulus phase")',
                '        issue_transaction(1\'b0, 2\'b01, "Case 1a: single READ transaction");',
                "        wait_cycles(2);",
                '        issue_transaction(1\'b1, 2\'b10, "Case 1b: single WRITE transaction");',
                "        wait_cycles(4);",
                "",
                memory_sequence_block.rstrip(),
                "",
                '        `INFO("Stress phase")',
                '        `INFO("Directed single-bank READ/WRITE checks cover the control-path distinction for this simplified flow")',
            ]
        )

    startup_init_lines = [
        "        txn_valid = 1'b0;",
        "        txn_is_write = 1'b0;",
        "        txn_addr = '0;",
        "        txn_wdata = '0;",
        "        rst_n = 1'b0;",
    ]
    if design.bank_count > 1:
        startup_init_lines.insert(1, "        txn_bank = '0;")
    if design.has_scheduler:
        startup_init_lines.extend(
            [
                "        sched_ref_req = 1'b0;",
                "        sched_cmd_ready = 1'b0;",
                "        sched_req_valid[0] = 1'b0;",
                "        sched_req_valid[1] = 1'b0;",
                "        sched_req_valid[2] = 1'b0;",
                "        sched_req_valid[3] = 1'b0;",
                "        sched_req_array[0] = '0;",
                "        sched_req_array[1] = '0;",
                "        sched_req_array[2] = '0;",
                "        sched_req_array[3] = '0;",
                "        sched_bank_active = 4'b0000;",
                "        sched_bank_open_row[0] = '0;",
                "        sched_bank_open_row[1] = '0;",
                "        sched_bank_open_row[2] = '0;",
                "        sched_bank_open_row[3] = '0;",
            ]
        )
    if design.has_handshake:
        startup_init_lines.append("        traffic_started = 1'b0;")
        startup_init_lines.append("        saw_backpressure = 1'b0;")
    if design.has_tfaw and design.tfaw_limit_reachable:
        startup_init_lines.append("        saw_tFAW_limit = 1'b0;")
    if design.has_trrd:
        startup_init_lines.append("        saw_tRRD_block = 1'b0;")

    corner_case_lines: list[str] = []
    if design.has_handshake:
        corner_case_lines.extend(
            [
                '        `INFO("Corner cases")',
                "        wait(cmd_ready === 1'b0);",
                '        `INFO("Observed backpressure while controller was busy")',
                "",
            ]
        )

    refresh_check_lines = ""
    if design.has_refresh_request:
        refresh_check_lines = """        refresh_start_cycle = cycle;
        while ((dut.ref_req !== 1'b1) &&
               ((cycle - refresh_start_cycle) < REFRESH_WAIT_CYCLES)) begin
            @(posedge clk);
        end
        `CHECK(dut.ref_req == 1'b1,
               "Refresh request was not observed within the allotted window")

"""

    refresh_decl_line = "        int refresh_start_cycle;\n" if design.has_refresh_request else ""

    return f"""`timescale 1ns/1ps

`define CHECK(cond, msg) \\
    if (!(cond)) begin \\
        $display("[ERROR] %s | time=%0t | %s", test_name, $time, msg); \\
        error_count++; \\
    end

`define INFO(msg) \\
    $display("[INFO] %s | time=%0t | %s", test_name, $time, msg);

module {tb_module};

    localparam int MAX_CYCLES = 20000;
    localparam time SIM_END_TIME = 200000ns;
    localparam int REFRESH_WAIT_CYCLES = {refresh_wait_cycles};
    localparam int BANK_COUNT = {design.bank_count};
    localparam int TXN_BANK_WIDTH = {txn_bank_width};
    localparam int ADDR_WIDTH = {MEMORY_ADDR_WIDTH};
    localparam int DATA_WIDTH = {MEMORY_DATA_WIDTH};
    localparam int READ_RESPONSE_LATENCY = {READ_RESPONSE_LATENCY_CYCLES};

    // ---------------------------------------------------------
    // Global Testbench State
    // ---------------------------------------------------------
    int error_count = 0;
    int cycle = 0;
    string test_name = "{tb_module}";
    bit results_reported = 0;{coverage_state_block}

    // ---------------------------------------------------------
    // Signals
    // ---------------------------------------------------------
    logic clk;
    logic rst_n;
    logic txn_valid;
    logic txn_is_write;
    logic [ADDR_WIDTH-1:0] txn_addr;
    logic [DATA_WIDTH-1:0] txn_wdata;
{bank_signal_decl_block}    logic cmd_ready;
    logic rsp_valid;
    logic [DATA_WIDTH-1:0] rsp_rdata;
{scheduler_decl_block}

    // ---------------------------------------------------------
    // DUT
    // ---------------------------------------------------------
{dut_instantiation_block}

    // ---------------------------------------------------------
    // Common Tasks
    // ---------------------------------------------------------
    task automatic check_coverage_goals;
{coverage_check_block}
    endtask

    task automatic report_results;
        begin
            if (!results_reported) begin
                check_coverage_goals();
                results_reported = 1'b1;

                if (error_count == 0) begin
                    $display("=================================");
                    $display("=========== TEST PASS ===========");
                    $display("=================================");
                end else begin
                    $display("=================================");
                    $display("=========== TEST FAIL ===========");
                    $display("Errors: %0d", error_count);
                    $display("=================================");
                end
            end
        end
    endtask

    task automatic wait_cycles(input int count);
        int i;
        begin
            for (i = 0; i < count; i = i + 1) begin
                @(posedge clk);
            end
        end
    endtask
{scheduler_task_block}
{bank_task_block}
{issue_transaction_task_block}
{memory_task_block}

    // ---------------------------------------------------------
    // Clock Generation (100MHz)
    // ---------------------------------------------------------
    initial clk = 1'b0;
    always #5 clk = ~clk;

    // ---------------------------------------------------------
    // Cycle Counter and Timeout Protection
    // ---------------------------------------------------------
    always @(posedge clk) begin
        if (!rst_n) begin
            cycle <= 0;
        end else begin
            cycle <= cycle + 1;

            if (cycle >= MAX_CYCLES && !results_reported) begin
                `CHECK(0, "Timeout reached")
                report_results();
                $finish;
            end
        end
    end

    // ---------------------------------------------------------
    // Immediate Assertions and Design Checks
    // ---------------------------------------------------------
    always @(posedge clk) begin
        if (rst_n) begin
            assert (!(txn_valid && !cmd_ready))
            else begin
                $error("[ASSERT FAIL] txn_valid while cmd_ready=0 at %0t", $time);
                error_count++;
            end

            `CHECK(!(txn_valid && !cmd_ready),
                   "Handshake violation: valid while not ready")
        end
    end{immediate_check_block}{coverage_monitor_block}

    // ---------------------------------------------------------
    // Structured Test Phases
    // ---------------------------------------------------------
    initial begin
{refresh_decl_line}{join_lines(startup_init_lines)}

        `INFO("=== TEST START ===");

        `INFO("Reset phase")
        wait_cycles(4);
        rst_n = 1'b1;
        wait_cycles(2);
        `CHECK(cmd_ready == 1'b1,
               "cmd_ready should be high after reset release")

{scheduler_sequence_block}
{bank_sequence_block}

{stimulus_phase_block}

{join_lines(corner_case_lines)}{refresh_check_lines}        `INFO("=== TEST END ===");
    end

    // ---------------------------------------------------------
    // Final PASS/FAIL Block
    // ---------------------------------------------------------
    initial begin
        #SIM_END_TIME;
        report_results();
        $finish;
    end

    // ---------------------------------------------------------
    // Waveform Dump
    // ---------------------------------------------------------
    initial begin
        $dumpfile("{tb_module}.vcd");
        $dumpvars(0, {tb_module});
    end

endmodule
"""


def generate_testbench(top_module: str = "ddr4_controller_top") -> Path:
    """Create tb/ and write the generated SystemVerilog testbench."""
    tb_dir = SCRIPT_DIR / "tb"
    tb_dir.mkdir(parents=True, exist_ok=True)

    output_path = tb_dir / f"tb_{top_module}.sv"
    design = load_design_context(top_module)
    output_path.write_text(build_testbench(top_module, design), encoding="utf-8")
    print(f"[TB] Generated: {output_path.relative_to(SCRIPT_DIR).as_posix()}")
    return output_path


if __name__ == "__main__":
    generate_testbench()
