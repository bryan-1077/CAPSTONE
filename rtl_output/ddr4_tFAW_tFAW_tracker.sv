`timescale 1ns/1ps
module ddr4_tFAW_tFAW_tracker(
  input logic clk,
  input logic rst_n,
  input logic act_pulse,
  output logic [2:0] act_count,
  output logic tFAW_block,
  output logic tFAW_ok
);

  parameter int TFAW_CYCLES = 40;
  parameter int TFAW_LIMIT = 4;

  typedef logic [TFAW_CYCLES-1:0] shift_reg_t;
  localparam int NEXT_COUNT_WIDTH = $clog2(TFAW_CYCLES + 1);
  logic [NEXT_COUNT_WIDTH-1:0] next_count;
  shift_reg_t act_window;
  shift_reg_t next_window;

  localparam int ACT_COUNT_WIDTH = $bits(act_count);
  localparam logic [NEXT_COUNT_WIDTH-1:0] TFAW_LIMIT_L = NEXT_COUNT_WIDTH'(TFAW_LIMIT);
  localparam logic [ACT_COUNT_WIDTH-1:0] TFAW_LIMIT_ACT_COUNT_L = ACT_COUNT_WIDTH'(TFAW_LIMIT);
  localparam logic [ACT_COUNT_WIDTH-1:0] ACT_COUNT_ZERO_L = ACT_COUNT_WIDTH'(0);

  // Combinational logic to calculate next_window and next_count
  always_comb begin
    next_window = {act_window[TFAW_CYCLES-2:0], act_pulse};
    next_count = NEXT_COUNT_WIDTH'($countones(next_window));
    tFAW_block = (next_count >= TFAW_LIMIT_L);
    tFAW_ok = !tFAW_block;
  end

  // Sequential logic for act_window and act_count
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      act_window <= '0;
      act_count <= ACT_COUNT_ZERO_L;
    end else begin
      act_window <= next_window;
      act_count <= (next_count > TFAW_LIMIT_L)
          ? TFAW_LIMIT_ACT_COUNT_L
          : next_count[ACT_COUNT_WIDTH-1:0];
    end
  end

endmodule
