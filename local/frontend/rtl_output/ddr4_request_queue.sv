`timescale 1ns/1ps
// GENERATED VIA DUAL-LLM FLOW
module ddr4_request_queue #(
    parameter int DEPTH     = 4,
    parameter int SEL_WIDTH = (DEPTH <= 1) ? 1 : $clog2(DEPTH)
) (
    input  logic                  clk,
    input  logic                  rst_n,
    input  logic                  enq_valid,
    input  logic [50:0]           enq_req,
    input  logic                  deq_en,
    input  logic [SEL_WIDTH-1:0]  sel_idx,
    output logic                  enq_ready,
    output logic [DEPTH*51-1:0]   req_array,
    output logic [DEPTH-1:0]      req_valid
);

    // ------------------------------------------------------------------
    // Local parameters
    // ------------------------------------------------------------------
    localparam int REQUEST_WIDTH = 51;

    // ------------------------------------------------------------------
    // Request payload type (no valid field: req_valid is the sole source)
    // ------------------------------------------------------------------
    typedef struct packed {
        logic [1:0]  bank;
        logic [9:0]  row;
        logic [5:0]  col;
        logic        is_write;
        logic [31:0] wdata;
    } request_t;

    // ------------------------------------------------------------------
    // Storage
    // ------------------------------------------------------------------
    request_t                  entries [DEPTH];

    // ------------------------------------------------------------------
    // Internal combinational helpers
    // ------------------------------------------------------------------
    request_t                  enq_req_s;
    logic [SEL_WIDTH-1:0]      sel_idx_w;
    logic [SEL_WIDTH-1:0]      first_free_idx;
    logic [SEL_WIDTH-1:0]      insert_idx;
    logic                      any_free;
    logic                      enq_fire;
    logic                      reuse_slot;

    // Unpack packed vector port into request_t (widths match: 51 bits)
    always_comb begin
        enq_req_s = enq_req;
    end

    // Selected dequeue index in internal index width (widths match)
    always_comb begin
        sel_idx_w = sel_idx;
    end

    // Lowest-index free slot (descending scan gives lowest index priority)
    always_comb begin
        any_free       = 1'b0;
        first_free_idx = '0;
        for (int i = DEPTH-1; i >= 0; i--) begin
            if (!req_valid[i]) begin
                any_free       = 1'b1;
                first_free_idx = SEL_WIDTH'(i);
            end
        end
    end

    // Insert index: lowest free slot, or sel_idx when full and deq_en asserted
    always_comb begin
        insert_idx = any_free ? first_free_idx : sel_idx_w;
    end

    // Ready when a slot is free, or when deq_en frees sel_idx in this cycle
    always_comb begin
        enq_ready = any_free || deq_en;
    end

    always_comb begin
        enq_fire = enq_valid && enq_ready;
    end

    // Explicit same-cycle enqueue/dequeue collision detection
    always_comb begin
        reuse_slot = deq_en && enq_valid && (insert_idx == sel_idx_w);
    end

    // ------------------------------------------------------------------
    // Pack stored entries into packed vector output
    // ------------------------------------------------------------------
    always_comb begin
        req_array = '0;
        for (int k = 0; k < DEPTH; k++) begin
            req_array[k*REQUEST_WIDTH +: REQUEST_WIDTH] = entries[k];
        end
    end

    // ------------------------------------------------------------------
    // Sequential state: entry payloads and validity
    // ------------------------------------------------------------------
    always_ff @(posedge clk) begin
        if (!rst_n) begin
            req_valid <= '0;
            for (int r = 0; r < DEPTH; r++) begin
                entries[r] <= '0;
            end
        end
        else begin
            // Dequeue clear is suppressed when the same slot is re-enqueued
            if (deq_en && !reuse_slot) begin
                req_valid[sel_idx_w] <= 1'b0;
            end

            // Enqueue wins on a same-index collision: slot stays valid and
            // stores the new payload (no intermediate invalid state).
            if (enq_fire) begin
                entries[insert_idx]   <= enq_req_s;
                req_valid[insert_idx] <= 1'b1;
            end
        end
    end

endmodule