`timescale 1ns/1ps
// GENERATED VIA DUAL-LLM FLOW
module ddr4_request_queue #(
    parameter int DEPTH = 4,
    parameter int REQUEST_WIDTH_P = 51,
    parameter int REQ_ARRAY_WIDTH = REQUEST_WIDTH_P * DEPTH,
    parameter int REQ_VALID_WIDTH = DEPTH,
    parameter int SEL_IDX_WIDTH = (DEPTH <= 1) ? 1 : $clog2(DEPTH)
) (
    input  logic                       clk,
    input  logic                       deq_en,
    output logic                       enq_ready,
    input  logic [50:0]                enq_req,
    input  logic                       enq_valid,
    output logic [REQ_ARRAY_WIDTH-1:0] req_array,
    output logic [REQ_VALID_WIDTH-1:0] req_valid,
    input  logic                       rst_n,
    input  logic [SEL_IDX_WIDTH-1:0]   sel_idx
);

    localparam int REQUEST_WIDTH = 51;
    localparam int SEL_WIDTH = (DEPTH <= 1) ? 1 : $clog2(DEPTH);

    typedef struct packed {
        logic [1:0]  bank;
        logic [9:0]  row;
        logic [5:0]  col;
        logic        is_write;
        logic [31:0] wdata;
    } request_t;

    request_t enq_req_t;
    request_t req_mem [0:DEPTH-1];
    request_t next_req_mem [0:DEPTH-1];
    logic [DEPTH-1:0] next_req_valid;
    logic has_free_slot;
    logic [SEL_WIDTH-1:0] first_free_idx;
    logic [SEL_WIDTH-1:0] insert_idx;
    logic reuse_slot;
    logic [SEL_WIDTH-1:0] sel_idx_int;

    assign enq_req_t = enq_req;
    assign sel_idx_int = SEL_WIDTH'(sel_idx);

    always_comb begin
        has_free_slot = 1'b0;
        first_free_idx = SEL_WIDTH'(0);
        for (int i = 0; i < DEPTH; i++) begin
            if ((!has_free_slot) && (!req_valid[i])) begin
                has_free_slot = 1'b1;
                first_free_idx = SEL_WIDTH'(i);
            end
        end
    end

    always_comb begin
        enq_ready = has_free_slot || deq_en;
        if (has_free_slot) begin
            insert_idx = first_free_idx;
        end else begin
            insert_idx = sel_idx_int;
        end
        reuse_slot = deq_en && enq_valid && (insert_idx == sel_idx_int);
    end

    always_comb begin
        next_req_valid = req_valid[DEPTH-1:0];
        for (int j = 0; j < DEPTH; j++) begin
            next_req_mem[j] = req_mem[j];
        end

        if (deq_en && !reuse_slot) begin
            next_req_valid[sel_idx_int] = 1'b0;
        end

        if (enq_valid && enq_ready) begin
            next_req_mem[insert_idx] = enq_req_t;
            next_req_valid[insert_idx] = 1'b1;
        end
    end

    always_ff @(posedge clk) begin
        if (!rst_n) begin
            req_valid <= REQ_VALID_WIDTH'(0);
            for (int k = 0; k < DEPTH; k++) begin
                req_mem[k] <= REQUEST_WIDTH'(0);
            end
        end else begin
            req_valid <= REQ_VALID_WIDTH'(next_req_valid);
            for (int k = 0; k < DEPTH; k++) begin
                req_mem[k] <= next_req_mem[k];
            end
        end
    end

    always_comb begin
        req_array = REQ_ARRAY_WIDTH'(0);
        for (int m = 0; m < DEPTH; m++) begin
            req_array[(m*REQUEST_WIDTH) +: REQUEST_WIDTH] = req_mem[m];
        end
    end

endmodule