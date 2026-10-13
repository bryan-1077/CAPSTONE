`timescale 1ns/1ps
// GENERATED VIA DUAL-LLM FLOW
module ddr4_scheduler_scheduler #(
    parameter  int DEPTH     = 4,
    parameter  int NUM_BANKS = 4,
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
    localparam int BANK_WIDTH    = 2;
    localparam int ROW_WIDTH     = 10;
    localparam int COL_WIDTH     = 6;
    localparam int DATA_WIDTH    = 32;

    // ------------------------------------------------------------------
    // Request payload type (51 bits, no valid field)
    // ------------------------------------------------------------------
    typedef struct packed {
        logic [BANK_WIDTH-1:0] bank;
        logic [ROW_WIDTH-1:0]  row;
        logic [COL_WIDTH-1:0]  col;
        logic                  is_write;
        logic [DATA_WIDTH-1:0] wdata;
    } request_t;

    // ------------------------------------------------------------------
    // Declarations
    // ------------------------------------------------------------------
    request_t                req_unpacked [DEPTH];
    logic [DEPTH-1:0]        row_hit;

    logic                    candidate_valid;
    logic [SEL_WIDTH-1:0]    candidate_idx;
    logic                    candidate_success;
    logic                    candidate_blocked;

    logic                    locked;
    logic [SEL_WIDTH-1:0]    locked_idx;

    logic                    successful_issue;

    // ------------------------------------------------------------------
    // Unpack packed request vector into request_t entries
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
                      && (bank_open_row[(ROW_WIDTH*int'(req_unpacked[j].bank)) +: ROW_WIDTH]
                          == req_unpacked[j].row);
        end
    end

    // ------------------------------------------------------------------
    // Candidate selection: lowest-index row hit, else lowest-index valid
    // ------------------------------------------------------------------
    always_comb begin
        candidate_valid = 1'b0;
        candidate_idx   = {SEL_WIDTH{1'b0}};

        for (int k = 0; k < DEPTH; k++) begin
            if (!candidate_valid && row_hit[k]) begin
                candidate_valid = 1'b1;
                candidate_idx   = SEL_WIDTH'(k);
            end
        end

        if (!candidate_valid) begin
            for (int m = 0; m < DEPTH; m++) begin
                if (!candidate_valid && req_valid[m]) begin
                    candidate_valid = 1'b1;
                    candidate_idx   = SEL_WIDTH'(m);
                end
            end
        end
    end

    // ------------------------------------------------------------------
    // Outputs and arbitration
    // ------------------------------------------------------------------
    always_comb begin
        sel_idx     = locked ? locked_idx                  : candidate_idx;
        issue_valid = locked ? req_valid[int'(locked_idx)] : candidate_valid;

        issue_ref   = ref_req && cmd_ready && timing_ok;
        issue_txn   = issue_valid && cmd_ready && timing_ok && !ref_req && !issue_ref;
    end

    always_comb begin
        successful_issue  = issue_valid && cmd_ready && timing_ok && !ref_req && !issue_ref;
        candidate_success = candidate_valid && cmd_ready && timing_ok && !ref_req;
        candidate_blocked = candidate_valid && !ref_req && !candidate_success;
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
            end else begin
                if (candidate_blocked) begin
                    locked     <= 1'b1;
                    locked_idx <= candidate_idx;
                end else begin
                    locked     <= 1'b0;
                    locked_idx <= {SEL_WIDTH{1'b0}};
                end
            end
        end
    end

endmodule