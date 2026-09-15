`timescale 1ns/1ps
// GENERATED VIA DUAL-LLM FLOW
module ddr4_scheduler_scheduler (
    input  logic         clk,
    input  logic         rst_n,
    input  logic [3:0]   bank_active,
    input  logic [39:0]  bank_open_row,
    input  logic         cmd_ready,
    output logic         issue_ref,
    output logic         issue_txn,
    output logic         issue_valid,
    input  logic         ref_req,
    input  logic [203:0] req_array,
    input  logic [3:0]   req_valid,
    output logic [1:0]   sel_idx,
    input  logic         timing_ok
);

    parameter int DEPTH = 4;

    localparam int REQUEST_WIDTH = 51;
    localparam int SEL_WIDTH = (DEPTH <= 1) ? 1 : $clog2(DEPTH);
    localparam logic [SEL_WIDTH-1:0] SEL_ZERO = SEL_WIDTH'(0);

    typedef struct packed {
        logic [1:0]  bank;
        logic [9:0]  row;
        logic [5:0]  col;
        logic        is_write;
        logic [31:0] wdata;
    } request_t;

    request_t req_unpacked [0:DEPTH-1];
    logic [9:0] bank_open_row_arr [0:3];
    logic [DEPTH-1:0] row_hit;

    logic [SEL_WIDTH-1:0] locked_idx;
    logic                 locked_valid;

    logic [SEL_WIDTH-1:0] candidate_idx;
    logic                 candidate_valid;
    logic                 candidate_success;
    logic                 candidate_blocked;
    logic                 successful_issue;

    always_comb begin
        for (int i = 0; i < DEPTH; i++) begin
            req_unpacked[i] = req_array[(i*REQUEST_WIDTH) +: REQUEST_WIDTH];
        end
    end

    always_comb begin
        for (int j = 0; j < 4; j++) begin
            bank_open_row_arr[j] = bank_open_row[(j*10) +: 10];
        end
    end

    always_comb begin
        for (int k = 0; k < DEPTH; k++) begin
            row_hit[k] = req_valid[k] && bank_active[req_unpacked[k].bank] && (bank_open_row_arr[req_unpacked[k].bank] == req_unpacked[k].row);
        end
    end

    always_comb begin
        candidate_idx = SEL_ZERO;
        candidate_valid = 1'b0;

        for (int m = 0; m < DEPTH; m++) begin
            if (!candidate_valid && row_hit[m]) begin
                candidate_idx = SEL_WIDTH'(m);
                candidate_valid = 1'b1;
            end
        end

        if (!candidate_valid) begin
            for (int n = 0; n < DEPTH; n++) begin
                if (!candidate_valid && req_valid[n]) begin
                    candidate_idx = SEL_WIDTH'(n);
                    candidate_valid = 1'b1;
                end
            end
        end
    end

    always_comb begin
        sel_idx = locked_valid ? locked_idx[1:0] : candidate_idx[1:0];
        issue_valid = locked_valid ? req_valid[locked_idx] : candidate_valid;
        issue_ref = ref_req && cmd_ready && timing_ok;
        issue_txn = issue_valid && cmd_ready && timing_ok && !ref_req && !issue_ref;
        successful_issue = issue_valid && cmd_ready && timing_ok && !ref_req && !issue_ref;
        candidate_success = candidate_valid && cmd_ready && timing_ok && !ref_req;
        candidate_blocked = candidate_valid && !ref_req && !candidate_success;
    end

    always_ff @(posedge clk) begin
        if (!rst_n) begin
            locked_idx <= SEL_ZERO;
            locked_valid <= 1'b0;
        end else begin
            if (locked_valid) begin
                if (successful_issue) begin
                    locked_valid <= 1'b0;
                    locked_idx <= locked_idx;
                end else begin
                    locked_valid <= locked_valid;
                    locked_idx <= locked_idx;
                end
            end else begin
                if (candidate_blocked) begin
                    locked_valid <= 1'b1;
                    locked_idx <= candidate_idx;
                end else begin
                    locked_valid <= 1'b0;
                    locked_idx <= locked_idx;
                end
            end
        end
    end

endmodule