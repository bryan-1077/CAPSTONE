// GENERATED VIA DUAL-LLM FLOW
module ddr4_controller_top_spec_waiting_queue (
    input  logic        clk,
    output logic        enqueue_ready,
    input  logic [50:0] enqueue_request,
    input  logic        enqueue_valid,
    output logic [50:0] head_request,
    input  logic        head_take,
    output logic        head_valid,
    input  logic        rst_n
);

    // ------------------------------------------------------------------
    // Local parameters
    // ------------------------------------------------------------------
    localparam int REQUEST_WIDTH = 51;
    localparam int DEPTH         = 4;                                 // WAITING_CAPACITY
    localparam int SEL_WIDTH     = (DEPTH <= 1) ? 1 : $clog2(DEPTH);
    localparam int CNT_WIDTH     = $clog2(DEPTH + 1);

    localparam logic [REQUEST_WIDTH-1:0] REQUEST_ZERO = {REQUEST_WIDTH{1'b0}};
    localparam logic [CNT_WIDTH-1:0]     CNT_ZERO     = {CNT_WIDTH{1'b0}};
    localparam logic [CNT_WIDTH-1:0]     CNT_ONE      = CNT_WIDTH'(1);
    localparam logic [CNT_WIDTH-1:0]     CNT_DEPTH    = CNT_WIDTH'(DEPTH);

    // ------------------------------------------------------------------
    // Owned state: FIFO-ordered waiting entries and registered occupancy
    // ------------------------------------------------------------------
    logic [REQUEST_WIDTH-1:0] entries [DEPTH];
    logic [CNT_WIDTH-1:0]     occupancy;

    // ------------------------------------------------------------------
    // Helper combinational signals
    // ------------------------------------------------------------------
    logic                     queue_not_empty;
    logic                     space_available;
    logic                     dispatch_event;
    logic                     admission_event;
    logic [SEL_WIDTH-1:0]     insert_idx;
    logic [CNT_WIDTH-1:0]     next_occupancy;

    always_comb begin
        queue_not_empty = (occupancy != CNT_ZERO);
        space_available = (occupancy < CNT_DEPTH);
    end

    // Outputs: reset immediately gates enqueue_ready and head_valid low.
    always_comb begin
        head_valid = rst_n && queue_not_empty;
    end

    always_comb begin
        if (rst_n && queue_not_empty) begin
            head_request = entries[0];
        end else begin
            head_request = REQUEST_ZERO;
        end
    end

    // Dispatch removes only the pre-edge head; head_take alone is not an event.
    always_comb begin
        dispatch_event = rst_n && queue_not_empty && head_take;
    end

    // Ready when pre-edge occupancy is below capacity or a valid pre-edge head
    // dispatches on this edge (full-queue simultaneous dispatch/admission).
    always_comb begin
        enqueue_ready = rst_n && (space_available || (queue_not_empty && head_take));
    end

    always_comb begin
        admission_event = rst_n && enqueue_valid && enqueue_ready;
    end

    // Insertion position after any same-edge head removal.
    always_comb begin
        if (dispatch_event) begin
            insert_idx = SEL_WIDTH'(occupancy - CNT_ONE);
        end else begin
            insert_idx = SEL_WIDTH'(occupancy);
        end
    end

    always_comb begin
        next_occupancy = occupancy;
        if (admission_event && !dispatch_event) begin
            next_occupancy = occupancy + CNT_ONE;
        end else if (!admission_event && dispatch_event) begin
            next_occupancy = occupancy - CNT_ONE;
        end
    end

    // ------------------------------------------------------------------
    // Sequential state update (synchronous active-low reset, reset wins)
    // ------------------------------------------------------------------
    always_ff @(posedge clk) begin
        if (!rst_n) begin
            for (int i = 0; i < DEPTH; i++) begin
                entries[i] <= REQUEST_ZERO;
            end
            occupancy <= CNT_ZERO;
        end else begin
            if (dispatch_event) begin
                for (int j = 0; j < DEPTH - 1; j++) begin
                    entries[j] <= entries[j+1];
                end
                entries[DEPTH-1] <= REQUEST_ZERO;
            end
            if (admission_event) begin
                entries[insert_idx] <= enqueue_request;
            end
            occupancy <= next_occupancy;
        end
    end

endmodule