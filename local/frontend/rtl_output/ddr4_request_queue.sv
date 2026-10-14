`timescale 1ns/1ps
// GENERATED VIA DUAL-LLM FLOW
module ddr4_request_queue #(
    parameter int DEPTH = 4
) (
    input  logic                                                clk,
    input  logic                                                rst_n,
    input  logic                                                enq_valid,
    input  logic [50:0]                                         enq_req,
    output logic                                                enq_ready,
    input  logic                                                deq_en,
    input  logic [(((DEPTH <= 1) ? 1 : $clog2(DEPTH)) - 1):0]   sel_idx,
    output logic [DEPTH*51-1:0]                                 req_array,
    output logic [DEPTH-1:0]                                    req_valid
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

    request_t                entries [DEPTH-1:0];
    request_t                enq_req_unpacked;

    logic                    any_free;
    logic [SEL_WIDTH-1:0]    first_free_idx;
    logic [SEL_WIDTH-1:0]    insert_idx;
    logic                    reuse_slot;
    logic                    do_enq;

    // Unpack packed vector port into request_t (widths match: 51 bits)
    always_comb begin
        enq_req_unpacked = enq_req;
    end

    // Lowest-index free slot detection
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

    // Insert index, readiness, same-cycle reuse detection
    // sel_idx is SEL_WIDTH wide by construction of its port declaration.
    always_comb begin
        insert_idx = any_free ? first_free_idx : SEL_WIDTH'(sel_idx);
        enq_ready  = any_free || deq_en;
        do_enq     = enq_valid && enq_ready;
        reuse_slot = deq_en && enq_valid && (insert_idx == SEL_WIDTH'(sel_idx));
    end

    // Storage with explicit next-state ordering: enqueue wins on same index,
    // and reuse_slot suppresses the dequeue clear entirely.
    always_ff @(posedge clk) begin
        if (!rst_n) begin
            req_valid <= '0;
            for (int j = 0; j < DEPTH; j++) begin
                entries[j] <= '0;
            end
        end else begin
            if (deq_en && !reuse_slot) begin
                req_valid[sel_idx] <= 1'b0;
            end
            if (do_enq) begin
                entries[insert_idx]   <= enq_req_unpacked;
                req_valid[insert_idx] <= 1'b1;
            end
        end
    end

    // Pack stored entries back into packed vector output
    always_comb begin
        req_array = '0;
        for (int k = 0; k < DEPTH; k++) begin
            req_array[k*REQUEST_WIDTH +: REQUEST_WIDTH] = entries[k];
        end
    end

endmodule