// Regression fixture from 20261109T193353Z_bb5024f81ea9.
// Only the sel_idx port range was repaired to use the guarded width.
module ddr4_request_queue #(
    parameter int DEPTH = 4
) (
    input  logic                        clk,
    input  logic                        rst_n,
    input  logic                        enq_valid,
    input  logic [50:0]                 enq_req,
    output logic                        enq_ready,
    input  logic                        deq_en,
    input  logic [((DEPTH <= 1) ? 1 : $clog2(DEPTH))-1:0]    sel_idx,
    output logic [DEPTH-1:0]            req_valid,
    output logic [DEPTH*51-1:0]         req_array
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

    // Unpacked view of the packed enqueue port
    request_t                 enq_req_s;

    // Stored state
    request_t                 entries_q [DEPTH];
    logic [DEPTH-1:0]         valid_q;

    // Next-state (combinational, not stored)
    request_t                 entries_d [DEPTH];
    logic [DEPTH-1:0]         valid_d;

    // Helper signals
    logic                     any_free;
    logic [SEL_WIDTH-1:0]     first_free_idx;
    logic [SEL_WIDTH-1:0]     insert_idx;
    logic                     reuse_slot;
    logic                     enq_fire;

    // Packed vector port -> request_t (widths match: 51 bits)
    assign enq_req_s = enq_req;

    // Lowest-index free slot
    always_comb begin
        any_free       = 1'b0;
        first_free_idx = '0;
        for (int i = DEPTH-1; i >= 0; i--) begin
            if (!valid_q[i]) begin
                any_free       = 1'b1;
                first_free_idx = SEL_WIDTH'(i);
            end
        end
    end

    // Insert index, readiness, reuse detection
    always_comb begin
        insert_idx = any_free ? first_free_idx : sel_idx;
        enq_ready  = any_free || deq_en;
        enq_fire   = enq_valid && enq_ready;
        reuse_slot = deq_en && enq_valid && (insert_idx == sel_idx);
    end

    // Explicit next-state ordering: dequeue clear first (suppressed on reuse),
    // then enqueue write wins on same index.
    always_comb begin
        valid_d = valid_q;
        for (int j = 0; j < DEPTH; j++) begin
            entries_d[j] = entries_q[j];
        end

        if (deq_en && !reuse_slot) begin
            valid_d[sel_idx] = 1'b0;
        end

        if (enq_fire) begin
            entries_d[insert_idx] = enq_req_s;
            valid_d[insert_idx]   = 1'b1;
        end
    end

    always_ff @(posedge clk) begin
        if (!rst_n) begin
            valid_q <= '0;
            for (int k = 0; k < DEPTH; k++) begin
                entries_q[k] <= '0;
            end
        end else begin
            valid_q <= valid_d;
            for (int m = 0; m < DEPTH; m++) begin
                entries_q[m] <= entries_d[m];
            end
        end
    end

    // Outputs
    always_comb begin
        req_valid = valid_q;
        req_array = '0;
        for (int n = 0; n < DEPTH; n++) begin
            req_array[n*REQUEST_WIDTH +: REQUEST_WIDTH] = entries_q[n];
        end
    end

endmodule
