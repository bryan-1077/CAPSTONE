`timescale 1ns/1ps
// GENERATED VIA DUAL-LLM FLOW
module ddr4_scheduler_scheduler #(
    parameter int DEPTH     = 4,
    parameter int NUM_BANKS = 4,
    parameter int ROW_WIDTH = 10,
    localparam int SEL_WIDTH = (DEPTH <= 1) ? 1 : $clog2(DEPTH)
) (
    input  logic                 clk,
    input  logic                 rst_n,
    input  logic [3:0]           bank_active,
    input  logic [39:0]          bank_open_row,
    input  logic                 cmd_ready,
    input  logic                 ref_req,
    input  logic [203:0]         req_array,
    input  logic [3:0]           req_valid,
    input  logic                 timing_ok,
    output logic                 issue_ref,
    output logic                 issue_txn,
    output logic                 issue_valid,
    output logic [SEL_WIDTH-1:0] sel_idx
);

    // ------------------------------------------------------------------
    // Local parameters
    // ------------------------------------------------------------------
    localparam int REQUEST_WIDTH = 51;

    // ------------------------------------------------------------------
    // Request payload type (bank[2], row[10], col[6], is_write[1], wdata[32])
    // ------------------------------------------------------------------
    typedef struct packed {
        logic [1:0]  bank;
        logic [9:0]  row;
        logic [5:0]  col;
        logic        is_write;
        logic [31:0] wdata;
    } request_t;

    request_t                 req_unpacked [DEPTH];
    logic [DEPTH-1:0]         row_hit;

    logic                     hit_valid;
    logic [SEL_WIDTH-1:0]     hit_idx;
    logic                     any_valid;
    logic [SEL_WIDTH-1:0]     first_valid_idx;

    logic                     candidate_valid;
    logic [SEL_WIDTH-1:0]     candidate_idx;
    logic                     candidate_success;
    logic                     candidate_blocked;

    logic                     locked;
    logic [SEL_WIDTH-1:0]     locked_idx;

    logic                     successful_issue;

    // ------------------------------------------------------------------
    // Unpack packed request vector into request_t entries
    // ------------------------------------------------------------------
    always_comb begin
        for (int i = 0; i < DEPTH; i++) begin
            req_unpacked[i] = request_t'(req_array[(i*REQUEST_WIDTH) +: REQUEST_WIDTH]);
        end
    end

    // ------------------------------------------------------------------
    // Row-hit computation
    // ------------------------------------------------------------------
    always_comb begin
        for (int j = 0; j < DEPTH; j++) begin
            row_hit[j] = req_valid[j]
                      && bank_active[req_unpacked[j].bank]
                      && (bank_open_row[(int'(req_unpacked[j].bank) * ROW_WIDTH) +: 10]
                          == req_unpacked[j].row);
        end
    end

    // ------------------------------------------------------------------
    // Lowest-index row-hit selection
    // ------------------------------------------------------------------
    always_comb begin
        hit_valid = 1'b0;
        hit_idx   = {SEL_WIDTH{1'b0}};
        for (int k = 0; k < DEPTH; k++) begin
            if (!hit_valid && row_hit[k]) begin
                hit_valid = 1'b1;
                hit_idx   = SEL_WIDTH'(k);
            end
        end
    end

    // ------------------------------------------------------------------
    // Lowest-index valid fallback selection
    // ------------------------------------------------------------------
    always_comb begin
        any_valid       = 1'b0;
        first_valid_idx = {SEL_WIDTH{1'b0}};
        for (int m = 0; m < DEPTH; m++) begin
            if (!any_valid && req_valid[m]) begin
                any_valid       = 1'b1;
                first_valid_idx = SEL_WIDTH'(m);
            end
        end
    end

    // ------------------------------------------------------------------
    // Candidate (unlocked) selection
    // ------------------------------------------------------------------
    always_comb begin
        candidate_valid = hit_valid || any_valid;
        candidate_idx   = hit_valid ? hit_idx : first_valid_idx;
    end

    always_comb begin
        candidate_success = candidate_valid && cmd_ready && timing_ok && !ref_req;
        candidate_blocked = candidate_valid && !ref_req && !candidate_success;
    end

    // ------------------------------------------------------------------
    // Selected index / issue outputs
    // ------------------------------------------------------------------
    always_comb begin
        sel_idx     = locked ? locked_idx : candidate_idx;
        issue_valid = locked ? req_valid[locked_idx] : candidate_valid;
    end

    always_comb begin
        issue_ref        = ref_req && cmd_ready && timing_ok;
        issue_txn        = issue_valid && cmd_ready && timing_ok && !ref_req && !issue_ref;
        successful_issue = issue_valid && cmd_ready && timing_ok && !ref_req && !issue_ref;
    end

    // ------------------------------------------------------------------
    // Lock state (selection holding)
    // ------------------------------------------------------------------
    always_ff @(posedge clk) begin
        if (!rst_n) begin
            locked     <= 1'b0;
            locked_idx <= {SEL_WIDTH{1'b0}};
        end else begin
            if (successful_issue) begin
                locked     <= 1'b0;
                locked_idx <= {SEL_WIDTH{1'b0}};
            end else if (!locked && candidate_blocked) begin
                locked     <= 1'b1;
                locked_idx <= candidate_idx;
            end else begin
                locked     <= locked;
                locked_idx <= locked_idx;
            end
        end
    end

endmodule