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

    // ------------------------------------------------------------------
    // Declarations
    // ------------------------------------------------------------------
    request_t                 req_unpacked [DEPTH];
    logic [DEPTH-1:0]         row_hit;

    logic [SEL_WIDTH-1:0]     cand_idx;
    logic                     cand_valid;

    logic                     candidate_success;
    logic                     candidate_blocked;
    logic                     successful_issue;

    logic                     locked;
    logic [SEL_WIDTH-1:0]     locked_idx;

    // ------------------------------------------------------------------
    // Unpack packed vector port into request_t entries
    // ------------------------------------------------------------------
    always_comb begin
        for (int u = 0; u < DEPTH; u++) begin
            req_unpacked[u] = request_t'(req_array[(u*REQUEST_WIDTH) +: REQUEST_WIDTH]);
        end
    end

    // ------------------------------------------------------------------
    // Row-hit computation
    // ------------------------------------------------------------------
    always_comb begin
        for (int h = 0; h < DEPTH; h++) begin
            row_hit[h] = req_valid[h]
                         && bank_active[req_unpacked[h].bank]
                         && (bank_open_row[(req_unpacked[h].bank)*ROW_WIDTH +: ROW_WIDTH]
                             == req_unpacked[h].row);
        end
    end

    // ------------------------------------------------------------------
    // Candidate selection: lowest-index row hit, else lowest-index valid
    // ------------------------------------------------------------------
    always_comb begin
        cand_idx   = {SEL_WIDTH{1'b0}};
        cand_valid = 1'b0;

        for (int r = 0; r < DEPTH; r++) begin
            if (!cand_valid && row_hit[r]) begin
                cand_valid = 1'b1;
                cand_idx   = SEL_WIDTH'(r);
            end
        end

        if (!cand_valid) begin
            for (int v = 0; v < DEPTH; v++) begin
                if (!cand_valid && req_valid[v]) begin
                    cand_valid = 1'b1;
                    cand_idx   = SEL_WIDTH'(v);
                end
            end
        end
    end

    // ------------------------------------------------------------------
    // Selected index / issue qualification
    // ------------------------------------------------------------------
    always_comb begin
        if (locked) begin
            sel_idx = {{(2-SEL_WIDTH){1'b0}}, locked_idx};
        end else begin
            sel_idx = {{(2-SEL_WIDTH){1'b0}}, cand_idx};
        end
    end

    always_comb begin
        if (locked) begin
            issue_valid = req_valid[locked_idx];
        end else begin
            issue_valid = cand_valid;
        end
    end

    always_comb begin
        issue_ref        = ref_req && cmd_ready && timing_ok;
        issue_txn        = issue_valid && cmd_ready && timing_ok && !ref_req && !issue_ref;
        successful_issue = issue_valid && cmd_ready && timing_ok && !ref_req && !issue_ref;
    end

    // ------------------------------------------------------------------
    // Lock acquisition qualification (unlocked candidate)
    // ------------------------------------------------------------------
    always_comb begin
        candidate_success = cand_valid && cmd_ready && timing_ok && !ref_req;
        candidate_blocked = cand_valid && !ref_req && !candidate_success;
    end

    // ------------------------------------------------------------------
    // Lock state
    // ------------------------------------------------------------------
    always_ff @(posedge clk) begin
        if (!rst_n) begin
            locked     <= 1'b0;
            locked_idx <= {SEL_WIDTH{1'b0}};
        end else begin
            if (locked) begin
                if (successful_issue) begin
                    locked     <= 1'b0;
                    locked_idx <= {SEL_WIDTH{1'b0}};
                end else begin
                    locked     <= locked;
                    locked_idx <= locked_idx;
                end
            end else if (candidate_blocked) begin
                locked     <= 1'b1;
                locked_idx <= cand_idx;
            end else begin
                locked     <= 1'b0;
                locked_idx <= {SEL_WIDTH{1'b0}};
            end
        end
    end

endmodule