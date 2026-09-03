// GENERATED VIA DUAL-LLM FLOW
module ddr4_request_queue #(
    parameter int DEPTH = 4
) (
    input  logic                      clk,
    input  logic                      rst_n,
    input  logic                      deq_en,
    input  logic [((DEPTH <= 1) ? 1 : $clog2(DEPTH))-1:0] sel_idx,
    input  logic [50:0]               enq_req,
    input  logic                      enq_valid,
    output logic                      enq_ready,
    output logic [DEPTH*51-1:0]       req_array,
    output logic [DEPTH-1:0]          req_valid
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

    request_t           req_mem[DEPTH];
    logic   [DEPTH-1:0] req_valid_q;

    logic   [DEPTH-1:0] free_slots;
    logic   [SEL_WIDTH-1:0] first_free_idx;
    logic   [SEL_WIDTH-1:0] insert_idx;
    logic                  has_free_slot;
    logic                  queue_full;
    logic                  reuse_slot;

    request_t enq_req_struct;
    always_comb begin
        enq_req_struct.bank     = enq_req[50:49];
        enq_req_struct.row      = enq_req[48:39];
        enq_req_struct.col      = enq_req[38:33];
        enq_req_struct.is_write = enq_req[32];
        enq_req_struct.wdata    = enq_req[31:0];
    end

    // Free slot calculation
    always_comb begin
        for (int i = 0; i < DEPTH; i++) begin
            free_slots[i] = ~req_valid_q[i];
        end
    end

    // Find first free slot
    always_comb begin
        first_free_idx = '0;
        has_free_slot = 1'b0;
        for (int j = 0; j < DEPTH; j++) begin
            if (free_slots[j] && !has_free_slot) begin
                first_free_idx = SEL_WIDTH'(j);
                has_free_slot = 1'b1;
            end
        end
    end

    // Enqueue insert index
    always_comb begin
        if (has_free_slot) begin
            insert_idx = first_free_idx;
        end else begin
            insert_idx = sel_idx;
        end
    end

    // Queue full logic
    always_comb begin
        queue_full = 1'b1;
        for (int k = 0; k < DEPTH; k++) begin
            if (!req_valid_q[k]) queue_full = 1'b0;
        end
    end

    // Reuse slot calculation
    always_comb begin
        reuse_slot = deq_en && enq_valid && (insert_idx == sel_idx);
    end

    // enq_ready logic
    always_comb begin
        enq_ready = has_free_slot || (queue_full && deq_en);
    end

    // Sequential logic for req_mem and req_valid_q
    always_ff @(posedge clk) begin
        if (!rst_n) begin
            for (int i = 0; i < DEPTH; i++) begin
                req_mem[i] <= '0;
                req_valid_q[i] <= 1'b0;
            end
        end else begin
            for (int l = 0; l < DEPTH; l++) begin
                // Dequeue clear guarded by reuse_slot
                if (deq_en && (SEL_WIDTH'(l) == sel_idx) && !reuse_slot) begin
                    req_valid_q[l] <= 1'b0;
                end
                // Enqueue write ONLY if allowed: gate with enq_ready
                if (enq_valid && enq_ready && (SEL_WIDTH'(l) == insert_idx)) begin
                    req_mem[l] <= enq_req_struct;
                    req_valid_q[l] <= 1'b1;
                end
            end
        end
    end

    // Pack req_mem to req_array output
    always_comb begin
        for (int m = 0; m < DEPTH; m++) begin
            req_array[m*REQUEST_WIDTH +: REQUEST_WIDTH] = {
                req_mem[m].bank,
                req_mem[m].row,
                req_mem[m].col,
                req_mem[m].is_write,
                req_mem[m].wdata
            };
        end
        req_valid = req_valid_q;
    end

endmodule