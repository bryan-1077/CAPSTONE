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
    localparam int DEPTH         = 4;
    localparam int NUM_BANKS     = 4;
    localparam int REQUEST_WIDTH = 51;
    localparam int ROW_WIDTH     = 10;
    localparam int SEL_WIDTH     = (DEPTH <= 1) ? 1 : $clog2(DEPTH);

    // ------------------------------------------------------------------
    // Request payload type (51 bits)
    // ------------------------------------------------------------------
    typedef struct packed {
        logic [1:0]  bank;
        logic [9:0]  row;
        logic [5:0]  col;
        logic        is_write;
        logic [31:0] wdata;
    } request_t;

    request_t                req_unpacked [DEPTH];
    logic [DEPTH-1:0]        row_hit;

    logic                    hit_found;
    logic [SEL_WIDTH-1:0]    hit_idx;
    logic                    val_found;
    logic [SEL_WIDTH-1:0]    val_idx;

    logic                    candidate_valid;
    logic [SEL_WIDTH-1:0]    candidate_idx;
    logic                    candidate_success;
    logic                    candidate_blocked;

    logic                    successful_issue;

    // Lock state
    logic                    locked;
    logic [SEL_WIDTH-1:0]    locked_idx;

    // ------------------------------------------------------------------
    // Unpack packed vector port into request_t entries
    // ------------------------------------------------------------------
    always_comb begin
        for (int i = 0; i < DEPTH; i++) begin
            req_unpacked[i] = req_array[(i*REQUEST_WIDTH) +: REQUEST_WIDTH];
        end
    end

    // ------------------------------------------------------------------
    // Row-hit computation
    // ------------------------------------------------------------------
    always_comb begin
        for (int j = 0; j < DEPTH; j++) begin
            row_hit[j] = req_valid[j]
                      && bank_active[int'(req_unpacked[j].bank)]
                      && (bank_open_row[(int'(req_unpacked[j].bank)*ROW_WIDTH) +: ROW_WIDTH]
                          == req_unpacked[j].row);
        end
    end

    // ------------------------------------------------------------------
    // Ascending-order scan: lowest row-hit index, lowest valid index
    // ------------------------------------------------------------------
    always_comb begin
        hit_found = 1'b0;
        hit_idx   = {SEL_WIDTH{1'b0}};
        val_found = 1'b0;
        val_idx   = {SEL_WIDTH{1'b0}};
        for (int k = 0; k < DEPTH; k++) begin
            if (!hit_found && row_hit[k]) begin
                hit_found = 1'b1;
                hit_idx   = SEL_WIDTH'(k);
            end
            if (!val_found && req_valid[k]) begin
                val_found = 1'b1;
                val_idx   = SEL_WIDTH'(k);
            end
        end
    end

    // ------------------------------------------------------------------
    // Candidate selection (row-hit first, else first valid)
    // ------------------------------------------------------------------
    always_comb begin
        candidate_valid = val_found;
        candidate_idx   = hit_found ? hit_idx : val_idx;
    end

    // ------------------------------------------------------------------
    // Selected index and issue outputs
    // ------------------------------------------------------------------
    always_comb begin
        if (locked) begin
            sel_idx     = locked_idx;
            issue_valid = req_valid[locked_idx];
        end else begin
            sel_idx     = candidate_idx;
            issue_valid = candidate_valid;
        end
    end

    always_comb begin
        issue_ref        = ref_req && cmd_ready && timing_ok;
        issue_txn        = issue_valid && cmd_ready && timing_ok && !ref_req && !issue_ref;
        successful_issue = issue_valid && cmd_ready && timing_ok && !ref_req && !issue_ref;

        candidate_success = candidate_valid && cmd_ready && timing_ok && !ref_req;
        candidate_blocked = candidate_valid && !ref_req && !candidate_success;
    end

    // ------------------------------------------------------------------
    // Lock state: acquire only on blocked candidate, clear only on
    // successful transaction issue
    // ------------------------------------------------------------------
    always_ff @(posedge clk) begin
        if (!rst_n) begin
            locked     <= 1'b0;
            locked_idx <= {SEL_WIDTH{1'b0}};
        end else begin
            if (locked) begin
                if (successful_issue) begin
                    locked     <= 1'b0;
                    locked_idx <= locked_idx;
                end else begin
                    locked     <= 1'b1;
                    locked_idx <= locked_idx;
                end
            end else begin
                if (candidate_blocked) begin
                    locked     <= 1'b1;
                    locked_idx <= candidate_idx;
                end else begin
                    locked     <= 1'b0;
                    locked_idx <= locked_idx;
                end
            end
        end
    end

endmodule