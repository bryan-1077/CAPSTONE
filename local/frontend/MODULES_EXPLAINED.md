> Top-Level Controller
# ddr4_controller_top:

This is the main integration point. It takes in transactions (bank, address, read/write), runs them through the scheduler and timing gates, routes them to the correct bank, and returns read data. It also contains the simple memory model used for demo storage.

> Scheduling / Control
# ddr4_scheduler_scheduler:

This decides whether to issue a refresh or a normal transaction each cycle. In simple mode, refresh has priority; in round_robin, it alternates fairly under contention. It does not enforce timing—only arbitration.

# ddr4_refresh_refresh_controller:

This periodically requests refresh operations to keep memory valid. It asserts ref_req, and the scheduler decides when to service it. For demo purposes, it acts like a background maintenance requester.

> Global Timing Constraints
# ddr4_tFAW_tFAW_tracker:

Tracks how many ACT (activate) commands occurred in a sliding time window. If too many occur (violating tFAW), it blocks further activations. This enforces a global power/timing constraint across all banks.

# ddr4_tRRD_simple_tRRD:

Ensures a minimum delay between ACT commands. If an ACT happens, it blocks the next one for a few cycles. This is another global timing constraint shared across banks.

> Bank-Level Control
# ddr4_bank_top:

This wraps everything needed to control a single memory bank. It receives commands (cmd_valid, cmd_type) and runs the internal FSM chain to execute them. Think of it as a self-contained “bank controller.”

# ddr4_bank_bank_sequencer:

This is the core control FSM that decides what operation happens next inside a bank. It sequences through activate → active → precharge based on incoming commands and timing completion signals. It also generates cmd_ready.

# ddr4_bank_activate_fsm:

Handles the ACTIVATE timing (tRCD). When a row is activated, this FSM waits the required number of cycles before signaling completion. It ensures you don’t access data too early.

# ddr4_bank_tRAS_fsm:

Handles how long a row must stay open (tRAS). It ensures the row remains active long enough before precharge. This enforces correct row lifecycle timing.

# ddr4_bank_precharge_fsm:

Handles precharging the bank (tRP). It ensures the bank is properly closed before the next activation. This resets the bank back to an idle-ready state.

> General Flow
txn → scheduler → timing gates → selected bank_top → memory model → response

> Testbench

## Corner Cases:

# 1. Control correctness
scheduler behavior
routing
handshake
# 2. Timing correctness
tRRD blocking
tFAW enforcement
backpressure
# 3. Functional correctness
read/write
memory storage
bank isolation