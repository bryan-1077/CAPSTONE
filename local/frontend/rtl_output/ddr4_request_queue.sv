`timescale 1ns/1ps
// GENERATED VIA DUAL-LLM FLOW
module ddr4_request_queue #(
    parameter int DEPTH = 4
) (
    input  logic                                              clk,
    input  logic                                              rst_n,
    input  logic                                              enq_valid,
    input  logic [50:0]                                       enq_req,
    output logic                                              enq_ready,
    input  logic                                              deq_en,
    input  logic [((DEPTH <= 1) ? 1 : $clog2(DEPTH))-1:0]     sel_idx,
    output logic [(DEPTH*51)-1:0]                             req_array,
    output logic [DEPTH-1:0]                                  req_valid
);

    localparam int REQUEST_WIDTH = 51;
    localparam int SEL_WIDTH     = (DEPTH <= 1) ? 1 : $clog2(DEPTH);

    typedef struct packed {
        logic [1:0]  bank;
        logic [9:0]  row;
        logic [5:0]  col;
        logic        is_write;
        logic [31:0] wdata;
    } request_t;

    // Storage
    request_t                  entries [DEPTH-1:0];

    // Unpacked input payload
    request_t                  enq_req_s;

    // Control
    logic                      any_free;
    logic [SEL_WIDTH-1:0]      first_free_idx;
    logic [SEL_WIDTH-1:0]      insert_idx;
    logic                      reuse_slot;
    logic                      do_enq;

    // Unpack packed vector port into request_t (widths match: 51 bits)
    always_comb begin
        enq_req_s = enq_req;
    end

    // Free-slot detection and lowest-index free slot selection
    always_comb begin
        any_free       = |(~req_valid);
        first_free_idx = '0;
        for (int i = DEPTH-1; i >= 0; i--) begin
            if (!req_valid[i]) begin
                first_free_idx = SEL_WIDTH'(i);
            end
        end
    end

    // Insert index, readiness, same-cycle reuse detection
    always_comb begin
        insert_idx = any_free ? first_free_idx : sel_idx;
        enq_ready  = any_free || deq_en;
        do_enq     = enq_valid && enq_ready;
        reuse_slot = deq_en && enq_valid && (insert_idx == sel_idx);
    end

    // Sequential state: entries and req_valid
    always_ff @(posedge clk) begin
        if (!rst_n) begin
            req_valid <= {DEPTH{1'b0}};
            for (int k = 0; k < DEPTH; k++) begin
                entries[k] <= '0;
            end
        end else begin
            // Dequeue clear, suppressed when the slot is reused in the same cycle
            if (deq_en && !reuse_slot) begin
                req_valid[sel_idx] <= 1'b0;
            end
            // Enqueue wins on same-index conflict: slot stays valid, stores new payload
            if (do_enq) begin
                entries[insert_idx]   <= enq_req_s;
                req_valid[insert_idx] <= 1'b1;
            end
        end
    end

    // Repack stored entries into packed output vector
    always_comb begin
        req_array = '0;
        for (int j = 0; j < DEPTH; j++) begin
            req_array[j*REQUEST_WIDTH +: REQUEST_WIDTH] = entries[j];
        end
    end

endmodule