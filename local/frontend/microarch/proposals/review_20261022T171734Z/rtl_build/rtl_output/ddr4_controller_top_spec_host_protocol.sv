// GENERATED VIA DUAL-LLM FLOW
module ddr4_controller_top_spec_host_protocol (
    input  logic        clk,
    input  logic        rst_n,
    input  logic        txn_valid,
    input  logic        txn_is_write,
    input  logic [3:0]  txn_addr,
    input  logic [1:0]  txn_bank,
    input  logic [31:0] txn_wdata,
    input  logic        queue_ready,
    input  logic        probe_ready,
    input  logic        memory_rsp_valid,
    input  logic [31:0] memory_rsp_data,
    output logic        txn_ready,
    output logic        cmd_ready,
    output logic        enqueue_valid,
    output logic [50:0] mapped_request,
    output logic        rsp_valid,
    output logic [31:0] rsp_rdata
);

    // -----------------------------------------------------------------------
    // Local parameters (packed request layout, widths)
    // -----------------------------------------------------------------------
    localparam int REQUEST_WIDTH        = 51;
    localparam int DATA_WIDTH           = 32;
    localparam int REQUEST_BANK_WIDTH   = 2;
    localparam int REQUEST_ROW_WIDTH    = 10;
    localparam int REQUEST_COLUMN_WIDTH = 6;

    // mapped_request field offsets:
    //   bank     [50:49]
    //   row      [48:39]
    //   column   [38:33]
    //   is_write [32]
    //   wdata    [31:0]
    localparam int REQ_BANK_LSB     = 49;
    localparam int REQ_ROW_LSB      = 39;
    localparam int REQ_COLUMN_LSB   = 33;
    localparam int REQ_IS_WRITE_LSB = 32;
    localparam int REQ_WDATA_LSB    = 0;

    // -----------------------------------------------------------------------
    // Host address mapping: row = txn_addr[3:2], column = txn_addr[1:0],
    // both zero-extended into the internal request field widths.
    // -----------------------------------------------------------------------
    logic [REQUEST_BANK_WIDTH-1:0]   mapped_bank;
    logic [REQUEST_ROW_WIDTH-1:0]    mapped_row;
    logic [REQUEST_COLUMN_WIDTH-1:0] mapped_column;
    logic                            mapped_is_write;
    logic [DATA_WIDTH-1:0]           mapped_wdata;

    always_comb begin
        mapped_bank     = txn_bank;
        mapped_row      = {{(REQUEST_ROW_WIDTH-2){1'b0}}, txn_addr[3:2]};
        mapped_column   = {{(REQUEST_COLUMN_WIDTH-2){1'b0}}, txn_addr[1:0]};
        mapped_is_write = txn_is_write;
        mapped_wdata    = txn_wdata;
    end

    // -----------------------------------------------------------------------
    // Packed mapped request (combinational from host payload fields only)
    // -----------------------------------------------------------------------
    always_comb begin
        mapped_request                                                         = {REQUEST_WIDTH{1'b0}};
        mapped_request[REQ_BANK_LSB     +: REQUEST_BANK_WIDTH]                 = mapped_bank;
        mapped_request[REQ_ROW_LSB      +: REQUEST_ROW_WIDTH]                  = mapped_row;
        mapped_request[REQ_COLUMN_LSB   +: REQUEST_COLUMN_WIDTH]               = mapped_column;
        mapped_request[REQ_IS_WRITE_LSB]                                       = mapped_is_write;
        mapped_request[REQ_WDATA_LSB    +: DATA_WIDTH]                         = mapped_wdata;
    end

    // -----------------------------------------------------------------------
    // Admission handshake: enqueue_valid represents txn_valid,
    // queue_ready supplies admission backpressure.
    // Reset immediately gates both low.
    // -----------------------------------------------------------------------
    always_comb begin
        enqueue_valid = rst_n ? txn_valid   : 1'b0;
        txn_ready     = rst_n ? queue_ready : 1'b0;
    end

    // -----------------------------------------------------------------------
    // Diagnostic probe: cmd_ready is probe_ready only, reset gated.
    // It has no effect on admission, storage, dispatch, history or timing.
    // -----------------------------------------------------------------------
    always_comb begin
        cmd_ready = rst_n ? probe_ready : 1'b0;
    end

    // -----------------------------------------------------------------------
    // Response pass-through of the memory-owned registered response stage.
    // No additional response register is introduced here.
    // -----------------------------------------------------------------------
    always_comb begin
        rsp_valid = rst_n ? memory_rsp_valid : 1'b0;
        rsp_rdata = rst_n ? memory_rsp_data  : {DATA_WIDTH{1'b0}};
    end

endmodule