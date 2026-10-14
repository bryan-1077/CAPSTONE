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

    // ---------------------------------------------------------------------
    // Local parameters
    // ---------------------------------------------------------------------
    localparam int DEPTH         = 4;
    localparam int NUM_BANKS     = 4;
    localparam int BANK_WIDTH    = 2;
    localparam int ROW_WIDTH     = 10;
    localparam int COL_WIDTH     = 6;
    localparam int WDATA_WIDTH   = 32;
    localparam int REQUEST_WIDTH = 51;
    localparam int SEL_WIDTH     = (DEPTH <= 1) ? 1 : $clog2(DEPTH);

    // ---------------------------------------------------------------------
    // Request payload type (51 bits)
    // ---------------------------------------------------------------------
    typedef struct packed {
        logic [BANK_WIDTH-1:0]  bank;
        logic [ROW_WIDTH-1:0]   row;
        logic [COL_WIDTH-1:0]   col;
        logic                   is_write;
        logic [WDATA_WIDTH-1:0] wdata;
    } request_t;

    // ---------------------------------------------------------------------
    // Internal signals
    // ---------------------------------------------------------------------
    request_t             req_unpacked [DEPTH];

    logic [DEPTH-1:0]     row_hit;

    logic                 row_hit_any;
    logic [SEL_WIDTH-1:0] row_hit_idx;
    logic                 valid_any;
    logic [SEL_WIDTH-1:0] valid_idx;

    logic [SEL_WIDTH-1:0] cand_idx;
    logic                 candidate_valid;
    logic                 candidate_success;
    logic                 candidate_blocked;
    logic                 successful_issue;

    logic                 locked;
    logic [SEL_WIDTH-1:0] locked_idx;

    // ---------------------------------------------------------------------
    // Unpack packed request vector into request_t entries
    // ---------------------------------------------------------------------
    always_comb begin
        for (int unpack_i = 0; unpack_i < DEPTH; unpack_i++) begin
            req_unpacked[unpack_i] = req_array[unpack_i*REQUEST_WIDTH +: REQUEST_WIDTH];
        end
    end

    // ---------------------------------------------------------------------
    // Row-hit computation
    // ---------------------------------------------------------------------
    always_comb begin
        row_hit = {DEPTH{1'b0}};
        for (int hit_i = 0; hit_i < DEPTH; hit_i++) begin
            int bank_sel;
            bank_sel = int'(req_unpacked[hit_i].bank);
            row_hit[hit_i] = req_valid[hit_i]
                             && bank_active[bank_sel]
                             && (bank_open_row[bank_sel*ROW_WIDTH +: ROW_WIDTH]
                                 == req_unpacked[hit_i].row);
        end
    end

    // ---------------------------------------------------------------------
    // Lowest-index row-hit selection (ascending scan)
    // ---------------------------------------------------------------------
    always_comb begin
        row_hit_any = 1'b0;
        row_hit_idx = {SEL_WIDTH{1'b0}};
        for (int rh_i = 0; rh_i < DEPTH; rh_i++) begin
            if (row_hit[rh_i] && !row_hit_any) begin
                row_hit_any = 1'b1;
                row_hit_idx = SEL_WIDTH'(rh_i);
            end
        end
    end

    // ---------------------------------------------------------------------
    // Lowest-index valid selection (ascending scan, fallback)
    // ---------------------------------------------------------------------
    always_comb begin
        valid_any = 1'b0;
        valid_idx = {SEL_WIDTH{1'b0}};
        for (int vv_i = 0; vv_i < DEPTH; vv_i++) begin
            if (req_valid[vv_i] && !valid_any) begin
                valid_any = 1'b1;
                valid_idx = SEL_WIDTH'(vv_i);
            end
        end
    end

    // ---------------------------------------------------------------------
    // Candidate selection (locked index wins while locked)
    // ---------------------------------------------------------------------
    always_comb begin
        if (locked) begin
            cand_idx        = locked_idx;
            candidate_valid = req_valid[locked_idx];
        end
        else if (row_hit_any) begin
            cand_idx        = row_hit_idx;
            candidate_valid = 1'b1;
        end
        else if (valid_any) begin
            cand_idx        = valid_idx;
            candidate_valid = 1'b1;
        end
        else begin
            cand_idx        = {SEL_WIDTH{1'b0}};
            candidate_valid = 1'b0;
        end
    end

    // ---------------------------------------------------------------------
    // Outputs and arbitration
    // ---------------------------------------------------------------------
    always_comb begin
        sel_idx     = 2'(cand_idx);
        issue_valid = candidate_valid;

        issue_ref   = ref_req && cmd_ready && timing_ok;
        issue_txn   = candidate_valid && cmd_ready && timing_ok && !ref_req
                      && !(ref_req && cmd_ready && timing_ok);

        successful_issue  = issue_txn;
        candidate_success = candidate_valid && cmd_ready && timing_ok && !ref_req;
        candidate_blocked = candidate_valid && !ref_req && !candidate_success;
    end

    // ---------------------------------------------------------------------
    // Lock state
    // ---------------------------------------------------------------------
    always_ff @(posedge clk) begin
        if (!rst_n) begin
            locked     <= 1'b0;
            locked_idx <= {SEL_WIDTH{1'b0}};
        end
        else begin
            if (locked) begin
                if (successful_issue) begin
                    locked     <= 1'b0;
                    locked_idx <= {SEL_WIDTH{1'b0}};
                end
            end
            else if (candidate_blocked) begin
                locked     <= 1'b1;
                locked_idx <= cand_idx;
            end
        end
    end

endmodule