`timescale 1ns/1ps
// GENERATED VIA DUAL-LLM FLOW
module ddr4_scheduler_scheduler (
    input  logic         clk,
    input  logic         rst_n,
    input  logic [3:0]   bank_active,
    input  logic [39:0]  bank_open_row,
    input  logic         cmd_ready,
    input  logic         ref_req,
    input  logic [203:0] req_array,
    input  logic [3:0]   req_valid,
    input  logic         timing_ok,
    output logic         issue_ref,
    output logic         issue_txn,
    output logic         issue_valid,
    output logic [1:0]   sel_idx
);

    // ------------------------------------------------------------------
    // Local parameters
    // ------------------------------------------------------------------
    localparam int REQUEST_WIDTH = 51;
    localparam int DEPTH         = 4;
    localparam int NUM_BANKS     = 4;
    localparam int ROW_WIDTH     = 10;
    localparam int SEL_WIDTH     = (DEPTH <= 1) ? 1 : $clog2(DEPTH);

    // ------------------------------------------------------------------
    // Request payload type (51 bits, no valid field)
    // ------------------------------------------------------------------
    typedef struct packed {
        logic [1:0]  bank;
        logic [9:0]  row;
        logic [5:0]  col;
        logic        is_write;
        logic [31:0] wdata;
    } request_t;

    // ------------------------------------------------------------------
    // Internal signals
    // ------------------------------------------------------------------
    request_t                 req_q [DEPTH];
    logic [DEPTH-1:0]         row_hit;

    logic [SEL_WIDTH-1:0]     candidate_idx;
    logic                     candidate_valid;
    logic                     candidate_success;
    logic                     candidate_blocked;
    logic                     successful_issue;

    logic                     locked_q;
    logic [SEL_WIDTH-1:0]     locked_idx_q;

    // ------------------------------------------------------------------
    // Unpack packed vector port into request_t entries
    // ------------------------------------------------------------------
    always_comb begin : unpack_proc
        for (int i = 0; i < DEPTH; i++) begin
            req_q[i] = req_array[(i * REQUEST_WIDTH) +: REQUEST_WIDTH];
        end
    end

    // ------------------------------------------------------------------
    // Row-hit computation
    // ------------------------------------------------------------------
    always_comb begin : row_hit_proc
        for (int j = 0; j < DEPTH; j++) begin
            row_hit[j] = req_valid[j]
                         && bank_active[req_q[j].bank]
                         && (bank_open_row[(int'(req_q[j].bank) * ROW_WIDTH) +: ROW_WIDTH]
                             == req_q[j].row);
        end
    end

    // ------------------------------------------------------------------
    // Selection: locked index holds, else row-hit first then first valid
    // ------------------------------------------------------------------
    always_comb begin : select_proc
        logic hit_found;
        logic val_found;

        hit_found       = 1'b0;
        val_found       = 1'b0;
        candidate_idx   = '0;
        candidate_valid = 1'b0;

        if (locked_q) begin
            candidate_idx   = locked_idx_q;
            candidate_valid = req_valid[locked_idx_q];
        end
        else begin
            for (int k = 0; k < DEPTH; k++) begin
                if (!hit_found && row_hit[k]) begin
                    hit_found     = 1'b1;
                    candidate_idx = SEL_WIDTH'(k);
                end
            end
            for (int m = 0; m < DEPTH; m++) begin
                if (!val_found && req_valid[m]) begin
                    val_found = 1'b1;
                    if (!hit_found) begin
                        candidate_idx = SEL_WIDTH'(m);
                    end
                end
            end
            candidate_valid = hit_found || val_found;
        end
    end

    // ------------------------------------------------------------------
    // Arbitration / issue outputs
    // ------------------------------------------------------------------
    always_comb begin : issue_proc
        issue_valid       = candidate_valid;
        sel_idx           = candidate_idx;
        issue_ref         = ref_req && cmd_ready && timing_ok;
        issue_txn         = candidate_valid && cmd_ready && timing_ok && !ref_req && !issue_ref;
        successful_issue  = candidate_valid && cmd_ready && timing_ok && !ref_req && !issue_ref;
        candidate_success = candidate_valid && cmd_ready && timing_ok && !ref_req;
        candidate_blocked = candidate_valid && !ref_req && !candidate_success;
    end

    // ------------------------------------------------------------------
    // Lock state: acquire only on blocked candidate, clear only on
    // successful transaction issue
    // ------------------------------------------------------------------
    always_ff @(posedge clk) begin
        if (!rst_n) begin
            locked_q     <= 1'b0;
            locked_idx_q <= '0;
        end
        else begin
            if (locked_q) begin
                if (successful_issue) begin
                    locked_q     <= 1'b0;
                    locked_idx_q <= '0;
                end
            end
            else begin
                if (candidate_blocked) begin
                    locked_q     <= 1'b1;
                    locked_idx_q <= candidate_idx;
                end
            end
        end
    end

endmodule