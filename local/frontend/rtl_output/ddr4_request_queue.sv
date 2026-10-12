`timescale 1ns/1ps
// GENERATED VIA DUAL-LLM FLOW
module ddr4_request_queue #(
    parameter int DEPTH = 4
) (
    clk,
    rst_n,
    enq_valid,
    enq_req,
    deq_en,
    sel_idx,
    enq_ready,
    req_array,
    req_valid
);

    localparam int REQUEST_WIDTH = 51;
    localparam int SEL_WIDTH     = (DEPTH <= 1) ? 1 : $clog2(DEPTH);

    // Non-ANSI port declarations so SEL_WIDTH (localparam) can size sel_idx
    input  logic                             clk;
    input  logic                             rst_n;
    input  logic                             enq_valid;
    input  logic [REQUEST_WIDTH-1:0]         enq_req;
    input  logic                             deq_en;
    input  logic [SEL_WIDTH-1:0]             sel_idx;
    output logic                             enq_ready;
    output logic [DEPTH*REQUEST_WIDTH-1:0]   req_array;
    output logic [DEPTH-1:0]                 req_valid;

    typedef struct packed {
        logic [1:0]  bank;
        logic [9:0]  row;
        logic [5:0]  col;
        logic        is_write;
        logic [31:0] wdata;
    } request_t;

    request_t             enq_req_unpacked;
    request_t             entries [DEPTH];
    logic                 any_free;
    logic [SEL_WIDTH-1:0] first_free_idx;
    logic [SEL_WIDTH-1:0] insert_idx;
    logic                 reuse_slot;
    logic                 do_enq;

    // Unpack the packed request vector port into request_t (widths match)
    always_comb begin
        enq_req_unpacked = enq_req;
    end

    // Lowest-index free slot detection
    always_comb begin
        any_free       = 1'b0;
        first_free_idx = '0;
        for (int i = DEPTH - 1; i >= 0; i--) begin
            if (!req_valid[i]) begin
                any_free       = 1'b1;
                first_free_idx = SEL_WIDTH'(i);
            end
        end
    end

    // Insertion index, readiness and explicit same-cycle reuse detection
    always_comb begin
        insert_idx = any_free ? first_free_idx : sel_idx;
        enq_ready  = any_free || deq_en;
        reuse_slot = deq_en && enq_valid && (insert_idx == sel_idx);
        do_enq     = enq_valid && enq_ready;
    end

    // Pack stored entries onto the output vector
    always_comb begin
        for (int k = 0; k < DEPTH; k++) begin
            req_array[k*REQUEST_WIDTH +: REQUEST_WIDTH] = entries[k];
        end
    end

    // Sequential storage: synchronous active-low reset
    always_ff @(posedge clk) begin
        if (!rst_n) begin
            req_valid <= '0;
            for (int j = 0; j < DEPTH; j++) begin
                entries[j] <= '0;
            end
        end else begin
            // Dequeue clear is suppressed when the same slot is reused this cycle
            if (deq_en && !reuse_slot) begin
                req_valid[sel_idx] <= 1'b0;
            end
            // Enqueue wins on a same-index collision: slot stays valid with new payload
            if (do_enq) begin
                entries[insert_idx]   <= enq_req_unpacked;
                req_valid[insert_idx] <= 1'b1;
            end
        end
    end

endmodule