`timescale 1ns/1ps
// GENERATED VIA DUAL-LLM FLOW
module ddr4_request_queue #(
    parameter int DEPTH = 4
) (
    input  logic                                            clk,
    input  logic                                            rst_n,
    input  logic                                            enq_valid,
    input  logic [50:0]                                     enq_req,
    input  logic                                            deq_en,
    input  logic [(((DEPTH <= 1) ? 1 : $clog2(DEPTH))-1):0] sel_idx,
    output logic                                            enq_ready,
    output logic [(DEPTH*51)-1:0]                           req_array,
    output logic [DEPTH-1:0]                                req_valid
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

    request_t             entries [DEPTH];
    request_t             enq_req_s;
    logic                 any_free;
    logic [SEL_WIDTH-1:0] first_free_idx;
    logic [SEL_WIDTH-1:0] insert_idx;
    logic                 reuse_slot;

    // Unpack packed vector port into request_t before policy logic
    always_comb begin
        enq_req_s = request_t'(enq_req);
    end

    // Lowest-index free slot search (descending scan leaves lowest index)
    always_comb begin
        any_free       = 1'b0;
        first_free_idx = '0;
        for (int j = DEPTH - 1; j >= 0; j--) begin
            if (req_valid[j] == 1'b0) begin
                any_free       = 1'b1;
                first_free_idx = SEL_WIDTH'(j);
            end
        end
    end

    // Insert index, reuse detection, readiness
    always_comb begin
        insert_idx = any_free ? first_free_idx : SEL_WIDTH'(sel_idx);
        reuse_slot = deq_en && enq_valid && (insert_idx == SEL_WIDTH'(sel_idx));
        enq_ready  = any_free || deq_en;
    end

    // Pack stored entries onto output vector
    always_comb begin
        req_array = '0;
        for (int i = 0; i < DEPTH; i++) begin
            req_array[i*REQUEST_WIDTH +: REQUEST_WIDTH] = entries[i];
        end
    end

    // Sequential state: entries and req_valid
    always_ff @(posedge clk) begin
        if (!rst_n) begin
            req_valid <= '0;
            for (int k = 0; k < DEPTH; k++) begin
                entries[k] <= '0;
            end
        end else begin
            // Dequeue clear, explicitly suppressed on same-index enqueue reuse
            if (deq_en && !reuse_slot) begin
                req_valid[SEL_WIDTH'(sel_idx)] <= 1'b0;
            end
            // Enqueue write: wins the slot and keeps/sets validity
            if (enq_valid && enq_ready) begin
                entries[insert_idx]   <= enq_req_s;
                req_valid[insert_idx] <= 1'b1;
            end
        end
    end

endmodule