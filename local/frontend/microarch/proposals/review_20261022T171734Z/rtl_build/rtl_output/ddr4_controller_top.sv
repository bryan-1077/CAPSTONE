`timescale 1ns/1ps
// Structural integration from validated architecture manifest.
module ddr4_controller_top(
    input logic clk,
    input logic rst_n,
    input logic txn_valid,
    input logic txn_is_write,
    input logic [3:0] txn_addr,
    input logic [31:0] txn_wdata,
    input logic [1:0] txn_bank,
    output logic txn_ready,
    output logic cmd_ready,
    output logic rsp_valid,
    output logic [31:0] rsp_rdata
);
    logic [50:0] arch_net_5;
    logic arch_net_6;
    logic arch_net_7;
    logic arch_net_8;
    logic [50:0] arch_net_9;
    logic arch_net_10;
    logic arch_net_11;
    logic [50:0] arch_net_12;
    logic arch_net_13;
    logic arch_net_14;
    logic [52:0] arch_net_15;
    logic arch_net_16;
    logic arch_net_17;
    logic [52:0] arch_net_18;
    logic arch_net_19;
    logic arch_net_20;
    logic [3:0] arch_net_21;
    logic [39:0] arch_net_22;
    logic arch_net_23;
    logic [52:0] arch_net_24;
    logic arch_net_25;
    logic arch_net_26;
    logic arch_net_27;
    logic [31:0] arch_net_28;
    logic arch_net_29;
    assign txn_ready = arch_net_29;
    logic arch_net_30;
    assign cmd_ready = arch_net_30;
    logic arch_net_31;
    assign rsp_valid = arch_net_31;
    logic [31:0] arch_net_32;
    assign rsp_rdata = arch_net_32;
    ddr4_controller_top_spec_host_protocol host_protocol (
        .txn_valid(txn_valid),
        .txn_is_write(txn_is_write),
        .txn_addr(txn_addr),
        .txn_wdata(txn_wdata),
        .txn_bank(txn_bank),
        .queue_ready(arch_net_7),
        .probe_ready(arch_net_20),
        .memory_rsp_valid(arch_net_27),
        .memory_rsp_data(arch_net_28),
        .mapped_request(arch_net_5),
        .enqueue_valid(arch_net_6),
        .txn_ready(arch_net_29),
        .cmd_ready(arch_net_30),
        .rsp_valid(arch_net_31),
        .rsp_rdata(arch_net_32),
        .clk(clk),
        .rst_n(rst_n)
    );
    ddr4_controller_top_spec_waiting_queue waiting_queue (
        .enqueue_request(arch_net_5),
        .enqueue_valid(arch_net_6),
        .head_take(arch_net_10),
        .enqueue_ready(arch_net_7),
        .head_valid(arch_net_8),
        .head_request(arch_net_9),
        .clk(clk),
        .rst_n(rst_n)
    );
    ddr4_controller_top_spec_fifo_scheduler fifo_scheduler (
        .head_valid(arch_net_8),
        .head_request(arch_net_9),
        .dispatch_ready(arch_net_13),
        .dispatch_valid(arch_net_11),
        .dispatch_request(arch_net_12),
        .head_take(arch_net_10),
        .clk(clk),
        .rst_n(rst_n)
    );
    ddr4_controller_top_spec_command_engine command_engine (
        .dispatch_valid(arch_net_11),
        .dispatch_request(arch_net_12),
        .candidate_ok(arch_net_16),
        .probe_ok(arch_net_19),
        .open_valid(arch_net_21),
        .open_rows(arch_net_22),
        .refresh_pending(arch_net_25),
        .refresh_busy(arch_net_26),
        .incoming_request(arch_net_5),
        .dispatch_ready(arch_net_13),
        .candidate_valid(arch_net_14),
        .candidate_command(arch_net_15),
        .commit_valid(arch_net_23),
        .commit_command(arch_net_24),
        .probe_valid(arch_net_17),
        .probe_command(arch_net_18),
        .probe_ready(arch_net_20),
        .clk(clk),
        .rst_n(rst_n)
    );
    ddr4_controller_top_spec_bank_state bank_state (
        .commit_valid(arch_net_23),
        .commit_command(arch_net_24),
        .open_valid(arch_net_21),
        .open_rows(arch_net_22),
        .clk(clk),
        .rst_n(rst_n)
    );
    ddr4_controller_top_spec_timing_enforcement timing_enforcement (
        .candidate_valid(arch_net_14),
        .candidate_command(arch_net_15),
        .probe_valid(arch_net_17),
        .probe_command(arch_net_18),
        .open_valid(arch_net_21),
        .open_rows(arch_net_22),
        .commit_valid(arch_net_23),
        .commit_command(arch_net_24),
        .candidate_ok(arch_net_16),
        .probe_ok(arch_net_19),
        .clk(clk),
        .rst_n(rst_n)
    );
    ddr4_controller_top_spec_memory_responses memory_responses (
        .commit_valid(arch_net_23),
        .commit_command(arch_net_24),
        .rsp_valid(arch_net_27),
        .rsp_data(arch_net_28),
        .clk(clk),
        .rst_n(rst_n)
    );
    ddr4_controller_top_spec_refresh_control refresh_control (
        .commit_valid(arch_net_23),
        .commit_command(arch_net_24),
        .refresh_pending(arch_net_25),
        .refresh_busy(arch_net_26),
        .clk(clk),
        .rst_n(rst_n)
    );
endmodule
