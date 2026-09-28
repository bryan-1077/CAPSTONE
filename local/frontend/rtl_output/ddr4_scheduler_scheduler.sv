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

    request_t req_entries [0:DEPTH-1];
    logic [DEPTH-1:0] row_hit;
    logic             candidate_valid;
    logic [SEL_WIDTH-1:0] candidate_idx;
    logic             candidate_success;
    logic             candidate_blocked;
    logic             locked_valid;
    logic [SEL_WIDTH-1:0] locked_idx;
    logic             successful_issue;

    always_comb begin
        req_entries[0] = req_array[(0*REQUEST_WIDTH) +: REQUEST_WIDTH];
        req_entries[1] = req_array[(1*REQUEST_WIDTH) +: REQUEST_WIDTH];
        req_entries[2] = req_array[(2*REQUEST_WIDTH) +: REQUEST_WIDTH];
        req_entries[3] = req_array[(3*REQUEST_WIDTH) +: REQUEST_WIDTH];
    end

    always_comb begin
        row_hit = '0;
        for (int i = 0; i < DEPTH; i++) begin
            row_hit[i] = req_valid[i] && bank_active[req_entries[i].bank] && (bank_open_row[(req_entries[i].bank * 10) +: 10] == req_entries[i].row);
        end
    end

    always_comb begin
        candidate_valid = 1'b0;
        candidate_idx = SEL_WIDTH'(0);

        for (int j = 0; j < DEPTH; j++) begin
            if (!candidate_valid && row_hit[j]) begin
                candidate_valid = 1'b1;
                candidate_idx = SEL_WIDTH'(j);
            end
        end

        if (!candidate_valid) begin
            for (int k = 0; k < DEPTH; k++) begin
                if (!candidate_valid && req_valid[k]) begin
                    candidate_valid = 1'b1;
                    candidate_idx = SEL_WIDTH'(k);
                end
            end
        end
    end

    always_comb begin
        issue_valid = 1'b0;
        sel_idx = locked_idx;

        if (locked_valid) begin
            issue_valid = req_valid[locked_idx];
            sel_idx = locked_idx;
        end else begin
            issue_valid = candidate_valid;
            sel_idx = candidate_idx;
        end
    end

    always_comb begin
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