// GENERATED VIA DUAL-LLM FLOW
module ddr4_request_queue #(
    parameter int DEPTH = 4
) (
    input  logic clk,
    input  logic deq_en,
    output logic enq_ready,
    input  logic [50:0] enq_req,
    input  logic enq_valid,
    output logic [(DEPTH * 51)-1:0] req_array,
    output logic [DEPTH-1:0] req_valid,
    input  logic rst_n,
    input  logic [((DEPTH <= 1) ? 1 : $clog2(DEPTH))-1:0] sel_idx
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

    request_t enq_request;
    request_t req_data [0:DEPTH-1];
    request_t req_data_next [0:DEPTH-1];
    logic [DEPTH-1:0] req_valid_next;
    logic has_free_slot;
    logic [SEL_WIDTH-1:0] first_free_idx;
    logic [SEL_WIDTH-1:0] insert_idx;
    logic reuse_slot;

    always_comb begin
        enq_request = enq_req;
        has_free_slot = 1'b0;
        first_free_idx = '0;

        for (int i = 0; i < DEPTH; i++) begin
            if (!req_valid[i] && !has_free_slot) begin
                has_free_slot = 1'b1;
                first_free_idx = SEL_WIDTH'(i);
            end
        end

        enq_ready = has_free_slot || deq_en;
        insert_idx = first_free_idx;

        if (!has_free_slot && deq_en) begin
            insert_idx = sel_idx;
        end

        reuse_slot = deq_en && enq_valid && (insert_idx == sel_idx);

        req_data_next = req_data;
        req_valid_next = req_valid;

        if (deq_en && !reuse_slot) begin
            req_valid_next[sel_idx] = 1'b0;
        end

        if (enq_valid && enq_ready) begin
            req_data_next[insert_idx] = enq_request;
            req_valid_next[insert_idx] = 1'b1;
        end
    end

    always_ff @(posedge clk) begin
        if (!rst_n) begin
            req_valid <= '0;
            for (int j = 0; j < DEPTH; j++) begin
                req_data[j] <= '0;
            end
        end else begin
            req_valid <= req_valid_next;
            for (int j = 0; j < DEPTH; j++) begin
                req_data[j] <= req_data_next[j];
            end
        end
    end

    always_comb begin
        req_array = '0;
        for (int k = 0; k < DEPTH; k++) begin
            req_array[k * REQUEST_WIDTH +: REQUEST_WIDTH] = req_data[k];
        end
    end

endmodule