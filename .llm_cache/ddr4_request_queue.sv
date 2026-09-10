// GENERATED VIA DUAL-LLM FLOW
module ddr4_request_queue #(
  parameter int DEPTH = 4
) (
  input  logic clk,
  input  logic rst_n,
  input  logic enq_valid,
  input  logic [50:0] enq_req,
  input  logic deq_en,
  input  logic [(DEPTH <= 1) ? 0 : $clog2(DEPTH)-1:0] sel_idx,
  output logic enq_ready,
  output logic [DEPTH*51-1:0] req_array,
  output logic [DEPTH-1:0] req_valid
);

  // request_t definition: packed struct
  typedef struct packed {
    logic [1:0]   bank;
    logic [9:0]   row;
    logic [5:0]   col;
    logic         is_write;
    logic [31:0] wdata;
  } request_t;
  
  localparam int REQUEST_WIDTH = 51;
  localparam int SEL_WIDTH = (DEPTH <= 1) ? 1 : $clog2(DEPTH);

  // Internal queue storage
  request_t      req_storage    [DEPTH];
  logic          req_valid_q    [DEPTH];

  // Index discovery
  logic [SEL_WIDTH-1:0] first_free_idx;
  logic [SEL_WIDTH-1:0] insert_idx;
  logic                 have_free;

  // reuse_slot detection
  logic reuse_slot;

  // Combinational: Find first free slot
  always_comb begin
    have_free      = 0;
    first_free_idx = '0;
    for (int free_i = 0; free_i < DEPTH; free_i++) begin
      if (req_valid_q[free_i] == 0 && have_free == 0) begin
        first_free_idx = SEL_WIDTH'(free_i);
        have_free      = 1;
      end
    end
  end

  // Combinational: Determine insert_idx
  always_comb begin
    if (have_free)
      insert_idx = first_free_idx;
    else
      insert_idx = sel_idx;
  end

  // reuse_slot: same index enqueue + dequeue
  always_comb begin
    reuse_slot = (deq_en && enq_valid && (insert_idx == sel_idx));
  end

  // enq_ready: any free slot OR same-cycle deq will free sel_idx
  always_comb begin
    enq_ready = have_free || (deq_en && req_valid_q[sel_idx]);
  end

  // Sequential: Queue storage and validity, explicit next-state ordering
  always_ff @(posedge clk) begin
    if (!rst_n) begin
      for (int reset_i = 0; reset_i < DEPTH; reset_i++) begin
        req_valid_q[reset_i]   <= 0;
        req_storage [reset_i]  <= '0;
      end
    end else begin
      // Dequeue: clear validity unless suppressed by reuse_slot
      if (deq_en && !reuse_slot) begin
        req_valid_q[sel_idx] <= 0;
      end

      // Enqueue: insert to insert_idx
      if (enq_valid) begin
        req_storage [insert_idx] <= request_t'(enq_req);
        req_valid_q [insert_idx] <= 1;
      end
    end
  end

  // Output: req_valid
  always_comb begin
    for (int v_i = 0; v_i < DEPTH; v_i++) begin
      req_valid[v_i] = req_valid_q[v_i];
    end
  end

  // Output: req_array as packed vector
  always_comb begin
    for (int a_i = 0; a_i < DEPTH; a_i++) begin
      req_array[REQUEST_WIDTH*a_i +: REQUEST_WIDTH] = REQUEST_WIDTH'(req_storage[a_i]);
    end
  end

endmodule