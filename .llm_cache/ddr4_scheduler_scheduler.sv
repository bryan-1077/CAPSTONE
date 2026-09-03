// GENERATED VIA DUAL-LLM FLOW
module ddr4_scheduler_scheduler (
    input  logic              clk,
    input  logic              rst_n,
    input  logic [3:0]        bank_active,
    input  logic [39:0]       bank_open_row,
    input  logic              cmd_ready,
    output logic              issue_ref,
    output logic              issue_txn,
    output logic              issue_valid,
    input  logic              ref_req,
    input  logic [203:0]      req_array,
    input  logic [3:0]        req_valid,
    output logic [1:0]        sel_idx,
    input  logic              timing_ok
);

  // ---- Parameters ----
  localparam int DEPTH        = 4;
  localparam int REQUEST_WIDTH = 51;
  localparam int SEL_WIDTH     = (DEPTH <= 1) ? 1 : $clog2(DEPTH);
  localparam int BANKS         = 4;

  // ---- Request type ----
  typedef struct packed {
    logic [1:0]   bank;
    logic [9:0]   row;
    logic [5:0]   col;
    logic         is_write;
    logic [31:0]  wdata;
  } request_t;

  // ---- Internal unpacked request array ----
  request_t req_entry    [DEPTH];
  logic     row_hit      [DEPTH];

  // ---- Unpack req_array into req_entry[] ----
  always_comb begin
    for (int u = 0; u < DEPTH; u++) begin
      req_entry[u] = request_t'(req_array[u*REQUEST_WIDTH +: REQUEST_WIDTH]);
    end
  end

  // ---- Compute row_hit[i] ----
  always_comb begin
    for (int v = 0; v < DEPTH; v++) begin
      logic [1:0] bank_idx;
      logic [9:0] open_row;
      bank_idx = req_entry[v].bank;
      open_row = bank_open_row[bank_idx*10 +: 10];
      row_hit[v] = req_valid[v] && bank_active[bank_idx] && (open_row == req_entry[v].row);
    end
  end

  // ---- Selection logic ----
  logic               next_lock_valid, lock_valid;
  logic [SEL_WIDTH-1:0] next_locked_idx, locked_idx;
  logic                 candidate_found;
  logic [SEL_WIDTH-1:0] candidate_idx;

  always_comb begin
    candidate_found = 1'b0;
    candidate_idx   = '0;
    // Priority: lowest row-hit
    for (int w = 0; w < DEPTH; w++) begin
      if (!candidate_found && row_hit[w]) begin
        candidate_found = 1'b1;
        candidate_idx = SEL_WIDTH'(w);
      end
    end
    // Fallback: lowest valid
    if (!candidate_found) begin
      for (int x = 0; x < DEPTH; x++) begin
        if (!candidate_found && req_valid[x]) begin
          candidate_found = 1'b1;
          candidate_idx = SEL_WIDTH'(x);
        end
      end
    end
  end

  // ---- Candidate signals ----
  logic candidate_valid;
  assign candidate_valid = candidate_found;

  logic candidate_success;
  assign candidate_success = candidate_valid && cmd_ready && timing_ok && !ref_req;
  logic candidate_blocked;
  assign candidate_blocked = candidate_valid && !ref_req && !candidate_success;

  // Selection/lock update logic
  always_comb begin
    // Default
    next_lock_valid = lock_valid;
    next_locked_idx = locked_idx;

    // When locked, remain locked until successful txn issue
    if (lock_valid) begin
      if (issue_valid && cmd_ready && timing_ok && !ref_req && !issue_ref) begin
        // Unlock after successful_issue
        next_lock_valid = 1'b0;
        next_locked_idx = '0;
      end
      // else: hold
    end else begin
      // Unlocked: can acquire lock if candidate_blocked
      if (candidate_blocked) begin
        next_lock_valid = 1'b1;
        next_locked_idx = candidate_idx;
      end
      // else: remain unlocked
    end
  end

  // Lock registers
  always_ff @(posedge clk) begin
    if (!rst_n) begin
      lock_valid   <= 1'b0;
      locked_idx   <= '0;
    end else begin
      lock_valid   <= next_lock_valid;
      locked_idx   <= next_locked_idx;
    end
  end

  // ---- sel_idx assignment ----
  always_comb begin
    if (lock_valid)
      sel_idx = locked_idx;
    else
      sel_idx = candidate_idx;
  end

  // ---- issue_valid assignment ----
  always_comb begin
    if (lock_valid) begin
      issue_valid = req_valid[locked_idx];
    end else begin
      issue_valid = candidate_valid && req_valid[candidate_idx];
    end
  end

  // ---- issue_ref/issue_txn ----
  assign issue_ref = ref_req && cmd_ready && timing_ok;
  assign issue_txn = issue_valid && cmd_ready && timing_ok && !ref_req && !issue_ref;

endmodule