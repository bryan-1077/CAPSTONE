`timescale 1ns/1ps
module ddr4_tRRD_simple_tRRD (
    input logic clk,
    input logic rst_n,
    input logic act_pulse,
    output logic tRRD_block
);

    parameter int TRRD_CYCLES = 6;
    localparam int COUNTER_WIDTH = $clog2(TRRD_CYCLES + 1);

    logic [COUNTER_WIDTH-1:0] counter;

    localparam logic [COUNTER_WIDTH-1:0] TRRD_CYCLES_L = COUNTER_WIDTH'(TRRD_CYCLES);
    localparam logic [COUNTER_WIDTH-1:0] COUNTER_ZERO_L = COUNTER_WIDTH'(0);
    localparam logic [COUNTER_WIDTH-1:0] COUNTER_ONE_L = COUNTER_WIDTH'(1);

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            counter <= COUNTER_ZERO_L;
        end else if (act_pulse) begin
            counter <= TRRD_CYCLES_L - COUNTER_ONE_L;
        end else if (counter > COUNTER_ZERO_L) begin
            counter <= counter - COUNTER_ONE_L;
        end else begin
            counter <= COUNTER_ZERO_L;
        end
    end

    always_comb begin
        tRRD_block = (counter > COUNTER_ZERO_L);
    end

endmodule
