# Setup timing closure: failed

Setup timing remains violated after 3 backend attempts; latest WNS -0.019 ns. See timing_closure/timing_closure_report.md for critical-path recommendations.

Run: `target_230MHz`

# Timing Closure Recommendation Report

Target: 4.348 ns (230.000 MHz)
WNS: -0.019
TNS: None
Violating paths: 1
Worst category: synthesized memory/flop-array read mux
Worst path: service_addr_q_reg[0]/Q -> rsp_rdata_q_reg[17]/D

## Ranked Recommendations

### 1. synthesized memory/flop-array read mux (RTL advisory)
Slack: -0.019 ns
Path group: clk
Startpoint: `service_addr_q_reg[0]/Q`
Endpoint: `rsp_rdata_q_reg[17]/D`
Dominant cells: sky130_fd_sc_hd__dfxtp_1, sky130_fd_sc_hd__inv_2, sky130_fd_sc_hd__nor2_1, sky130_fd_sc_hd__buf_4, sky130_fd_sc_hd__buf_6, sky130_fd_sc_hd__buf_8, sky130_fd_sc_hd__and2_4, sky130_fd_sc_hd__buf_12
- Consider registering the memory read address or read data and documenting the added response latency.
- If this is a flop-array standing in for memory, consider replacing it with a proper SRAM/register-file implementation or banking the read path.
- Keep any patch as a human-reviewed RTL proposal because it can change read latency and memory semantics.
Human review required for: pipeline insertion, response latency changes, memory read/write semantic changes

RTL behavior is never changed by this analyzer. RTL-facing output is advisory only.
