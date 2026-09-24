`timescale 1ns/1ps

`define CHECK(cond, msg) \
    if (!(cond)) begin \
        $display("[ERROR] %s | time=%0t | %s", test_name, $time, msg); \
        error_count++; \
    end

`define INFO(msg) \
    $display("[INFO] %s | time=%0t | %s", test_name, $time, msg);

module tb_ddr4_controller_top;

    localparam int MAX_CYCLES = 20000;
    localparam time SIM_END_TIME = 200000ns;
    localparam int REFRESH_WAIT_CYCLES = 12512;
    localparam int BANK_COUNT = 4;
    localparam int TXN_BANK_WIDTH = 2;
    localparam int ADDR_WIDTH = 4;
    localparam int DATA_WIDTH = 32;
    localparam int ROW_WIDTH = 2;
    localparam int COL_WIDTH = 2;
    localparam int COL_COUNT = (1 << COL_WIDTH);
    localparam int LOCALITY_PATTERN_PAIRS = 16;
    localparam int LOCALITY_PATTERN_OPS = (2 * LOCALITY_PATTERN_PAIRS);
    localparam int TIMING_STRESS_OPS = 32;
    localparam int PATTERN_DETAIL_LOG_LIMIT = 6;
    localparam int HIT_SERVICE_CYCLES = 1;
    localparam int SLOW_SERVICE_CYCLES = 3;
    localparam int ROW_CLASS_CLOSED = 0;
    localparam int ROW_CLASS_HIT = 1;
    localparam int ROW_CLASS_MISS = 2;
    localparam string PAGE_POLICY = "open_page";

    int error_count = 0;
    int cycle = 0;
    int txn_id = 0;
    int active_txn_id = 0;
    int detailed_log_budget = -1;
    int pattern_iteration = 0;
    int pattern_bank = 0;
    int pattern_row = 0;
    int pattern_col = 0;
    string test_name = "tb_ddr4_controller_top";
    string detailed_log_context = "";
    bit results_reported = 0;
    bit detailed_log_notice_emitted = 0;
    bit traffic_started = 0;
    bit saw_backpressure = 0;
    bit saw_tRRD_block = 0;
    bit saw_row_closed = 0;
    bit saw_row_hit = 0;
    bit saw_row_miss = 0;

    bit saw_tFAW_block = 0;
    int observed_tfaw_admission_stall_cycles = 0;
    int observed_tfaw_hard_block_cycles = 0;

    logic [DATA_WIDTH-1:0] pattern_data;
    logic [DATA_WIDTH-1:0] high_locality_expected_data [0:COL_COUNT-1];

    logic clk;
    logic rst_n;
    logic txn_valid;
    logic txn_is_write;
    logic [ADDR_WIDTH-1:0] txn_addr;
    logic [DATA_WIDTH-1:0] txn_wdata;
    logic [1:0] txn_bank;
    logic cmd_ready;
    logic rsp_valid;
    logic [DATA_WIDTH-1:0] rsp_rdata;

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


    ddr4_controller_top dut (
        .clk(clk),
        .rst_n(rst_n),
        .txn_valid(txn_valid),
        .txn_is_write(txn_is_write),
        .txn_addr(txn_addr),
        .txn_wdata(txn_wdata),
        .txn_bank(txn_bank),
        .cmd_ready(cmd_ready),
        .rsp_valid(rsp_valid),
        .rsp_rdata(rsp_rdata)
    );

    task automatic check_coverage_goals;
        begin
            `CHECK(saw_row_closed, "Row-closed access was never observed")
            `CHECK(saw_row_hit, "Row-hit access was never observed")
            `CHECK(saw_row_miss, "Row-miss access was never observed")
            `CHECK(saw_backpressure, "Controller backpressure was never observed")
            `CHECK(saw_tRRD_block, "tRRD block never occurred")
        end
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
            $display("Policy Observation    : Open-page preserved row reuse whenever the workload stayed on an already-open row.");
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
            accepted_addr = {dut.accepted_requested_row_q, dut.accepted_requested_col_q};
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
            $display("tFAW Status             : enabled, but the hard block threshold is not realistically reachable in this config (window=40, limit=4).");
            $display("Pattern Explanation     : consecutive non-hit accesses across banks increased ACT pressure and exposed timing throttling.");
        end
    endtask


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
            txn_bank = bank_sel[TXN_BANK_WIDTH-1:0];
            txn_addr = make_addr(row_sel, col_sel);
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
            txn_bank = bank_sel[TXN_BANK_WIDTH-1:0];
            txn_is_write = is_write;
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


    always @(posedge clk) begin
        if (rst_n && dut.accepted_slow) begin
            `CHECK(dut.tfaw_can_accept_act,
                   "tFAW admission violation: slow activate accepted when tFAW window was full")
        end
    end

    always @(posedge clk) begin
        if (rst_n && dut.u_tRRD.tRRD_block && dut.act_pulse) begin
            `CHECK(0, "tRRD violation: act during block")
        end
    end

    always @(posedge clk) begin
        if (!rst_n) begin
            saw_backpressure <= 1'b0;
            saw_tRRD_block <= 1'b0;
            saw_row_closed <= 1'b0;
            saw_row_hit <= 1'b0;
            saw_row_miss <= 1'b0;

            saw_tFAW_block <= 1'b0;
            observed_tfaw_admission_stall_cycles <= 0;
            observed_tfaw_hard_block_cycles <= 0;
        end else begin
            if (!saw_backpressure && (txn_valid === 1'b1) && (cmd_ready === 1'b0)) begin
                saw_backpressure <= 1'b1;
            end

            if (!saw_tRRD_block && dut.tRRD_block) begin
                saw_tRRD_block <= 1'b1;
            end

            if (!saw_tFAW_block && dut.tFAW_block) begin
                saw_tFAW_block <= 1'b1;
            end
            if (txn_valid && !cmd_ready && !dut.service_pending_q &&
                (!dut.tRRD_block) && !dut.tfaw_can_accept_act) begin
                observed_tfaw_admission_stall_cycles <= observed_tfaw_admission_stall_cycles + 1;
            end
            if (txn_valid && !cmd_ready && !dut.service_pending_q && dut.tFAW_block) begin
                observed_tfaw_hard_block_cycles <= observed_tfaw_hard_block_cycles + 1;
            end
            if (dut.accept_txn && dut.is_row_closed) begin
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

    always @(posedge clk) begin
        if (rst_n) begin
            `CHECK(!(sched_issue_ref && sched_issue_txn),
                   "Scheduler issued refresh and transaction simultaneously")
        end
    end

    always @(posedge clk) begin
        if (rst_n && txn_valid && !cmd_ready && detail_logging_enabled()) begin
            $display("[STALL][id=%0d][cycle=%0d] txn blocked (cmd_ready=0)",
                     active_txn_id, cycle);
        end
    end

    always @(posedge clk) begin
        if (rst_n && txn_valid && dut.tRRD_block && !dut.is_row_hit && !dut.accept_txn &&
            detail_logging_enabled()) begin
            $display("[tRRD ][id=%0d][cycle=%0d] BLOCKED activation",
                     active_txn_id, cycle);
        end
    end


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
        txn_bank = '0;
        rst_n = 1'b0;
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

        saw_tFAW_block = 1'b0;
        observed_tfaw_admission_stall_cycles = 0;
        observed_tfaw_hard_block_cycles = 0;


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


        log_phase("ROW BUFFER");
        `INFO("Row-buffer phase")
        issue_write_and_wait_complete(0, 1, 1, DATA_WIDTH'(32'h11110001),
                                      ROW_CLASS_CLOSED, SLOW_SERVICE_CYCLES, closed_latency,
                                      "First access to bank 0 opens row 1 on the slow path");
        check_row_state(0, 1'b1, 1, "Bank 0 keeps row 1 open after the first access");

        wait(dut.tRRD_block === 1'b1);
        expect_access_ready(0, 1, 2, 1'b1,
                            "Row hit remains ready while shared tRRD blocking is active");
        expect_access_ready(0, 2, 0, 1'b0,
                            "Row miss stalls while shared tRRD blocking is active");
        issue_read_and_wait_response(0, 1, 1, DATA_WIDTH'(32'h11110001),
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

        pattern_tfaw_admission_start = observed_tfaw_admission_stall_cycles;
        pattern_tfaw_hard_block_start = observed_tfaw_hard_block_cycles;
        set_pattern_detail_logging("TIMING STRESS", PATTERN_DETAIL_LOG_LIMIT);
        for (pattern_iteration = 0; pattern_iteration < TIMING_STRESS_OPS; pattern_iteration = pattern_iteration + 1) begin
            pattern_bank = timing_stress_bank(pattern_iteration);
            pattern_row = timing_stress_row(pattern_bank, pattern_iteration);
            pattern_col = timing_stress_col(pattern_iteration);
            pattern_data = timing_stress_data(pattern_bank, pattern_iteration, pattern_row, pattern_col);
            issue_write_and_wait_complete(pattern_bank, pattern_row, pattern_col, pattern_data,
                                          (((pattern_bank >= 2) && ((pattern_iteration / BANK_COUNT) == 0)) ? ROW_CLASS_CLOSED : ROW_CLASS_MISS), SLOW_SERVICE_CYCLES, pattern_latency,
                                          "Timing stress: alternating banks and rows keeps ACT pressure high under open-page");
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
                               "Open-page still lost row reuse once the timing-stress pattern kept switching rows and banks.");
        report_timing_stress_summary(pattern_accept_start,
                                     pattern_stall_busy_start,
                                     pattern_stall_trrd_start,
                                     pattern_stall_refresh_start,
                                     pattern_stall_other_start,
                                     pattern_tfaw_admission_start,
                                     pattern_tfaw_hard_block_start);

        log_phase("CORNER CASES");
        `INFO("Corner cases")
        wait(cmd_ready === 1'b0);
        `INFO("Observed backpressure while controller was busy")

        refresh_start_cycle = cycle;
        while ((dut.ref_req !== 1'b1) &&
               ((cycle - refresh_start_cycle) < REFRESH_WAIT_CYCLES)) begin
            @(posedge clk);
        end
        `CHECK(dut.ref_req == 1'b1,
               "Refresh request was not observed within the allotted window")

        `INFO("=== TEST END ===");
        $display("===== TEST COMPLETE =====");
    end

    initial begin
        #SIM_END_TIME;
        report_results();
        $finish;
    end

    initial begin
        $dumpfile("tb_ddr4_controller_top.vcd");
        $dumpvars(0, tb_ddr4_controller_top);
    end

endmodule
