`timescale 1ns/1ps
// GENERATED VIA DUAL-LLM FLOW
module ddr4_request_queue #(
    parameter int DEPTH = 4,
    parameter int SEL_IDX_W = (DEPTH <= 1) ? 1 : $clog2(DEPTH)
) (
    input  logic                           clk,
    input  logic                           deq_en,
    output logic                           enq_ready,
    input  logic [50:0]                    enq_req,
    input  logic                           enq_valid,
    output logic [(DEPTH*51)-1:0]          req_array,
    output logic [DEPTH-1:0]               req_valid,
    input  logic                           rst_n,
    input  logic [SEL_IDX_W-1:0]           sel_idx
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
    request_t req_mem_n [0:DEPTH-1];

    logic [DEPTH-1:0] req_valid_int;
    logic [DEPTH-1:0] req_valid_n;
    logic             has_free_slot;
    logic [SEL_WIDTH-1:0] first_free_idx;
    logic [SEL_WIDTH-1:0] insert_idx;
    logic             reuse_slot;

    assign enq_req_t = enq_req;
    assign req_valid = req_valid_int;

    always_comb begin
        has_free_slot = 1'b0;
        first_free_idx = SEL_WIDTH'(0);
        for (int i = 0; i < DEPTH; i++) begin
            if ((!has_free_slot) && (!req_valid_int[i])) begin
                has_free_slot = 1'b1;
                first_free_idx = SEL_WIDTH'(i);
            end
        end

        if (has_free_slot) begin
            insert_idx = first_free_idx;
        end else begin
            insert_idx = sel_idx[SEL_WIDTH-1:0];
        end

        enq_ready = has_free_slot || deq_en;
        reuse_slot = deq_en && enq_valid && (insert_idx == sel_idx[SEL_WIDTH-1:0]);
    end

    always_comb begin
        for (int j = 0; j < DEPTH; j++) begin
            req_mem_n[j] = req_mem[j];
            req_valid_n[j] = req_valid_int[j];
        end

        if (deq_en && !reuse_slot) begin
            req_valid_n[sel_idx[SEL_WIDTH-1:0]] = 1'b0;
        end

        if (enq_valid && enq_ready) begin
            req_mem_n[insert_idx] = enq_req_t;
            req_valid_n[insert_idx] = 1'b1;
        end
    end

    always_ff @(posedge clk) begin
        if (!rst_n) begin
            for (int k = 0; k < DEPTH; k++) begin
                req_mem[k] <= request_t'(REQUEST_WIDTH'(0));
                req_valid_int[k] <= 1'b0;
            end
        end else begin
            for (int k = 0; k < DEPTH; k++) begin
                req_mem[k] <= req_mem_n[k];
                req_valid_int[k] <= req_valid_n[k];
            end
        end
    end

    always_comb begin
        req_array = ((DEPTH*REQUEST_WIDTH))'(0);
        for (int m = 0; m < DEPTH; m++) begin
            req_array[(m*REQUEST_WIDTH) +: REQUEST_WIDTH] = req_mem[m];
        end
    end

endmodule