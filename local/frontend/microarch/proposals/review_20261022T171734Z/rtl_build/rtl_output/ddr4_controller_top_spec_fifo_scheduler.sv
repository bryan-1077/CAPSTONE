// GENERATED VIA DUAL-LLM FLOW
// fifo_scheduler: request_selection role for ddr4_controller_top_spec
// Stateless FIFO head selection with immediate reset gating of handshake outputs.
module ddr4_controller_top_spec_fifo_scheduler (
    input  logic        clk,
    input  logic        rst_n,
    input  logic        head_valid,
    input  logic [50:0] head_request,
    input  logic        dispatch_ready,
    output logic        dispatch_valid,
    output logic [50:0] dispatch_request,
    output logic        head_take
);

    // Packed request width (shared.types.request)
    localparam int REQUEST_WIDTH = 51;

    // Combinational selection: always expose the previously stored FIFO head.
    // dispatch_valid depends on head_valid (reset-gated per clock_reset.handshake_gating).
    logic selection_enabled;

    always_comb begin
        selection_enabled = rst_n;
    end

    // dispatch_request depends only on head_request; widths match exactly.
    always_comb begin
        dispatch_request = head_request;
    end

    // dispatch_valid reflects the stored FIFO head independently of dispatch_ready
    // and of any timing permission; reset forces it low immediately.
    always_comb begin
        dispatch_valid = selection_enabled && head_valid;
    end

    // head_take follows the dispatch handshake condition
    // (dispatch_valid && dispatch_ready); reset forces it low immediately.
    always_comb begin
        head_take = selection_enabled && head_valid && dispatch_ready;
    end

    // No owned state in this module: clk is unused by design (stateless selection).
    // Reference retained only through the declared port list; no dummy logic added.

endmodule