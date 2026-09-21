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

    localparam int DEPTH = 4;
    localparam int REQUEST_WIDTH = 51;
    localparam int SEL_WIDTH = (DEPTH <= 1) ? 1 : $clog2(DEPTH);

    typedef struct packed {
        logic [1:0]  bank;
        logic [9:0]  row;
        logic [5:0]  col;
        logic        is_write;
        logic [31:0] wdata;
    } request_t;

    request_t req_array_unpacked [0:DEPTH-1];
    logic [DEPTH-1:0] row_hit;
    logic [SEL_WIDTH-1:0] candidate_idx;
    logic candidate_valid;
    logic candidate_success;
    logic candidate_blocked;
    logic locked_valid;
    logic [SEL_WIDTH-1:0] locked_idx;
    logic successful_issue;

    always_comb begin
        for (int i = 0; i < DEPTH; i++) begin
            req_array_unpacked[i] = req_array[(i*REQUEST_WIDTH) +: REQUEST_WIDTH];
        end
    end

    always_comb begin
        for (int j = 0; j < DEPTH; j++) begin
            row_hit[j] = req_valid[j] &&
                         bank_active[req_array_unpacked[j].bank] &&
                         (bank_open_row[(req_array_unpacked[j].bank * 10) +: 10] == req_array_unpacked[j].row);
        end
    end

    always_comb begin
        candidate_valid = 1'b0;
        candidate_idx = SEL_WIDTH'(0);

        for (int k = 0; k < DEPTH; k++) begin
            if (!candidate_valid && row_hit[k]) begin
                candidate_valid = 1'b1;
                candidate_idx = SEL_WIDTH'(k);
            end
        end

        if (!candidate_valid) begin
            for (int m = 0; m < DEPTH; m++) begin
                if (!candidate_valid && req_valid[m]) begin
                    candidate_valid = 1'b1;
                    candidate_idx = SEL_WIDTH'(m);
                end
            end
        end
    end

    always_comb begin
        if (locked_valid) begin
            issue_valid = req_valid[locked_idx];
            sel_idx = locked_idx;
        end else begin
            issue_valid = candidate_valid;
            sel_idx = candidate_idx;
        end

        issue_ref = ref_req && cmd_ready && timing_ok;
        issue_txn = issue_valid && cmd_ready && timing_ok && !ref_req && !issue_ref;
        successful_issue = issue_valid && cmd_ready && timing_ok && !ref_req && !issue_ref;
        candidate_success = candidate_valid && cmd_ready && timing_ok && !ref_req;
        candidate_blocked = candidate_valid && !ref_req && !candidate_success;
    end

    always_ff @(posedge clk) begin
        if (!rst_n) begin
            locked_valid <= 1'b0;
            locked_idx <= SEL_WIDTH'(0);
        end else begin
            if (locked_valid) begin
                if (successful_issue) begin
                    locked_valid <= 1'b0;
                end
                locked_idx <= locked_idx;
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