// GENERATED VIA DUAL-LLM FLOW
module ddr4_scheduler_scheduler #(parameter int DEPTH = 4) (
    input  logic clk,
    input  logic rst_n,
    input  logic [3:0] bank_active,
    input  logic [39:0] bank_open_row,
    input  logic cmd_ready,
    output logic issue_ref,
    output logic issue_txn,
    output logic issue_valid,
    input  logic ref_req,
    input  logic [DEPTH*51-1:0] req_array,
    input  logic [DEPTH-1:0] req_valid,
    output logic [((DEPTH <= 1) ? 1 : $clog2(DEPTH))-1:0] sel_idx,
    input  logic timing_ok
);
    // Localparams
    localparam int REQUEST_WIDTH = 51;
    localparam int SEL_WIDTH = (DEPTH <= 1) ? 1 : $clog2(DEPTH);

    // Request structure definition
    typedef struct packed {
        logic [1:0] bank;
        logic [9:0] row;
        logic [5:0] col;
        logic is_write;
        logic [31:0] wdata;
    } request_t;

    // Internal unpacked requests
    request_t req_unpack   [DEPTH-1:0];

    // Unpack req_array
    always_comb begin
        for (int ui = 0; ui < DEPTH; ui++) begin
            req_unpack[ui] = request_t'(req_array[ui*REQUEST_WIDTH +: REQUEST_WIDTH]);
        end
    end

    // Compute row_hit per entry
    logic [DEPTH-1:0] row_hit;
    always_comb begin
        for (int ri = 0; ri < DEPTH; ri++) begin
            logic [1:0] bank_idx;
            logic [9:0] open_row;
            bank_idx = req_unpack[ri].bank;
            open_row = bank_open_row[bank_idx*10 +: 10];
            row_hit[ri] = req_valid[ri] && bank_active[bank_idx] && (open_row == req_unpack[ri].row);
        end
    end

    // Selection candidate
    logic candidate_valid;
    logic [SEL_WIDTH-1:0] candidate_idx;

    always_comb begin
        candidate_valid = 1'b0;
        candidate_idx = '0;
        // Search for the lowest row-hit
        for (int si = 0; si < DEPTH; si++) begin
            if (row_hit[si]) begin
                candidate_valid = 1'b1;
                candidate_idx = SEL_WIDTH'(si);
                break;
            end
        end
        // Fallback to lowest valid if no row-hit
        if (!candidate_valid) begin
            for (int fi = 0; fi < DEPTH; fi++) begin
                if (req_valid[fi]) begin
                    candidate_valid = 1'b1;
                    candidate_idx = SEL_WIDTH'(fi);
                    break;
                end
            end
        end
    end

    // State: locked index/register
    logic locked;
    logic [SEL_WIDTH-1:0] locked_idx;

    // Track lock and sel_idx according to blocking policy
    always_ff @(posedge clk) begin
        if (!rst_n) begin
            locked <= 1'b0;
            locked_idx <= '0;
        end else begin
            // Successful transaction issue (unlocked after pulse)
            if (locked && (issue_valid && cmd_ready && timing_ok && !ref_req && !issue_ref)) begin
                locked <= 1'b0;
            end
            // Lock acquisition (only when not refresh, only blocked candidate)
            else if (!locked && candidate_valid && !ref_req) begin
                // Candidate can acquire lock ONLY if it is blocked (i.e. not immediately issuing)
                if (!(cmd_ready && timing_ok)) begin
                    locked <= 1'b1;
                    locked_idx <= candidate_idx;
                end
            end
        end
    end

    // Output selection
    always_comb begin
        if (locked) begin
            sel_idx = locked_idx;
        end else begin
            sel_idx = candidate_valid ? candidate_idx : '0;
        end
    end

    // Issue valid generation
    always_comb begin
        if (locked) begin
            issue_valid = req_valid[locked_idx];
        end else begin
            issue_valid = candidate_valid ? req_valid[candidate_idx] : 1'b0;
        end
    end

    // Issue reference (refresh)
    assign issue_ref = ref_req && cmd_ready && timing_ok;

    // Issue transaction: only one successful transaction per pulse, mutual exclusion with issue_ref
    assign issue_txn = issue_valid && cmd_ready && timing_ok && !ref_req && !issue_ref;

endmodule