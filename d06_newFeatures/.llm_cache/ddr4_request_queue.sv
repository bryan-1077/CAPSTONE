// GENERATED VIA DUAL-LLM FLOW
module ddr4_request_queue #(
    parameter int DEPTH = 4,
    parameter int SEL_WIDTH = (DEPTH <= 1) ? 1 : $clog2(DEPTH)
) (
    input  logic              clk,
    input  logic              rst_n,
    input  logic              deq_en,
    input  logic              enq_valid,
    input  logic [51-1:0]     enq_req,
    input  logic [SEL_WIDTH-1:0] sel_idx,
    output logic              enq_ready,
    output logic [DEPTH*51-1:0] req_array,
    output logic [DEPTH-1:0]    req_valid
);

    typedef struct packed {
        logic [1:0]   bank;
        logic [9:0]   row;
        logic [5:0]   col;
        logic         is_write;
        logic [31:0]  wdata;
    } request_t;

    localparam int REQUEST_WIDTH = 51;

    // Internal storage
    request_t req_mem[DEPTH];
    logic     req_valid_reg[DEPTH];

    // Internal signals
    logic [SEL_WIDTH-1:0] insert_idx;
    logic                 queue_full;
    logic                 slot_free[DEPTH];
    logic                 reuse_slot;

    // Output assignment: packed req_array
    always_comb begin
        for (int j = 0; j < DEPTH; j++) begin
            req_array[j*REQUEST_WIDTH +: REQUEST_WIDTH] = REQUEST_WIDTH'(req_mem[j]);
        end
    end

    // Output assignment: req_valid
    always_comb begin
        for (int k = 0; k < DEPTH; k++) begin
            req_valid[k] = req_valid_reg[k];
        end
    end

    // Find the insert index (lowest free slot, or sel_idx if full & deq)
    always_comb begin
        queue_full = 1'b1;
        insert_idx = '0;
        for (int m = 0; m < DEPTH; m++) begin
            slot_free[m] = ~req_valid_reg[m];
            if (slot_free[m] && queue_full) begin
                insert_idx = SEL_WIDTH'(m);
                queue_full = 1'b0;
            end
        end
        if (queue_full && deq_en) begin
            insert_idx = sel_idx;
        end
    end

    // reuse_slot detection
    always_comb begin
        reuse_slot = deq_en && enq_valid && (insert_idx == sel_idx);
    end

    // enq_ready generation
    always_comb begin
        logic any_free;
        any_free = 1'b0;
        for (int n = 0; n < DEPTH; n++) begin
            any_free |= ~req_valid_reg[n];
        end
        enq_ready = any_free || (queue_full && deq_en);
    end

    // Queue update logic
    always_ff @(posedge clk) begin
        if (!rst_n) begin
            for (int p = 0; p < DEPTH; p++) begin
                req_mem[p]      <= '0;
                req_valid_reg[p] <= 1'b0;
            end
        end else begin
            for (int q = 0; q < DEPTH; q++) begin
                if (enq_valid && (insert_idx == SEL_WIDTH'(q))) begin
                    req_mem[q]      <= enq_req;
                    req_valid_reg[q] <= 1'b1;
                end else if (deq_en && (sel_idx == SEL_WIDTH'(q)) && !reuse_slot) begin
                    req_valid_reg[q] <= 1'b0;
                end
            end
        end
    end
endmodule