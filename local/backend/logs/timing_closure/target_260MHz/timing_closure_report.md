# Setup timing closure: passed

Setup timing passed: WNS 0.008 ns at 3.846 ns.

Run: `target_260MHz`

# Timing Closure Recommendation Report

Target: 3.846 ns (260.000 MHz)
WNS: 0.008
TNS: None
Violating paths: None
Worst category: synthesized memory/flop-array read mux
Worst path: service_addr_q_reg[2]/Q -> rsp_rdata_q_reg[11]/D

## Ranked Recommendations

### 1. synthesized memory/flop-array read mux (RTL advisory)
Slack: 0.008 ns
Path group: clk
Startpoint: `service_addr_q_reg[2]/Q`
Endpoint: `rsp_rdata_q_reg[11]/D`
Dominant cells: sky130_fd_sc_hd__dfxtp_4, sky130_fd_sc_hd__and2_4, sky130_fd_sc_hd__inv_2, sky130_fd_sc_hd__a22o_1, sky130_fd_sc_hd__a221oi_1, sky130_fd_sc_hd__o22ai_1, sky130_fd_sc_hd__a221oi_2, sky130_fd_sc_hd__nand4_1
- Consider registering the memory read address or read data and documenting the added response latency.
- If this is a flop-array standing in for memory, consider replacing it with a proper SRAM/register-file implementation or banking the read path.
- Keep any patch as a human-reviewed RTL proposal because it can change read latency and memory semantics.
Human review required for: pipeline insertion, response latency changes, memory read/write semantic changes

### 2. synthesized memory/flop-array read mux (RTL advisory)
Slack: 0.02 ns
Path group: clk
Startpoint: `service_addr_q_reg[2]/Q`
Endpoint: `rsp_rdata_q_reg[0]/D`
Dominant cells: sky130_fd_sc_hd__dfxtp_4, sky130_fd_sc_hd__and2_4, sky130_fd_sc_hd__inv_2, sky130_fd_sc_hd__a22o_1, sky130_fd_sc_hd__a221oi_1, sky130_fd_sc_hd__o22ai_1, sky130_fd_sc_hd__a221oi_2, sky130_fd_sc_hd__nand4_1
- Consider registering the memory read address or read data and documenting the added response latency.
- If this is a flop-array standing in for memory, consider replacing it with a proper SRAM/register-file implementation or banking the read path.
- Keep any patch as a human-reviewed RTL proposal because it can change read latency and memory semantics.
Human review required for: pipeline insertion, response latency changes, memory read/write semantic changes

### 3. synthesized memory/flop-array read mux (RTL advisory)
Slack: 0.02 ns
Path group: clk
Startpoint: `service_addr_q_reg[2]/Q`
Endpoint: `rsp_rdata_q_reg[21]/D`
Dominant cells: sky130_fd_sc_hd__dfxtp_4, sky130_fd_sc_hd__and2_4, sky130_fd_sc_hd__inv_2, sky130_fd_sc_hd__a22o_1, sky130_fd_sc_hd__a221oi_1, sky130_fd_sc_hd__o22ai_1, sky130_fd_sc_hd__nand4_1, sky130_fd_sc_hd__a22oi_2
- Consider registering the memory read address or read data and documenting the added response latency.
- If this is a flop-array standing in for memory, consider replacing it with a proper SRAM/register-file implementation or banking the read path.
- Keep any patch as a human-reviewed RTL proposal because it can change read latency and memory semantics.
Human review required for: pipeline insertion, response latency changes, memory read/write semantic changes

### 4. synthesized memory/flop-array read mux (RTL advisory)
Slack: 0.03 ns
Path group: clk
Startpoint: `service_addr_q_reg[2]/Q`
Endpoint: `rsp_rdata_q_reg[14]/D`
Dominant cells: sky130_fd_sc_hd__dfxtp_4, sky130_fd_sc_hd__and2_4, sky130_fd_sc_hd__inv_2, sky130_fd_sc_hd__a22o_1, sky130_fd_sc_hd__a221oi_1, sky130_fd_sc_hd__o22ai_1, sky130_fd_sc_hd__a221oi_2, sky130_fd_sc_hd__nand4_2
- Consider registering the memory read address or read data and documenting the added response latency.
- If this is a flop-array standing in for memory, consider replacing it with a proper SRAM/register-file implementation or banking the read path.
- Keep any patch as a human-reviewed RTL proposal because it can change read latency and memory semantics.
Human review required for: pipeline insertion, response latency changes, memory read/write semantic changes

### 5. synthesized memory/flop-array read mux (RTL advisory)
Slack: 0.031 ns
Path group: clk
Startpoint: `service_addr_q_reg[2]/Q`
Endpoint: `rsp_rdata_q_reg[7]/D`
Dominant cells: sky130_fd_sc_hd__dfxtp_4, sky130_fd_sc_hd__nor2_1, sky130_fd_sc_hd__buf_12, sky130_fd_sc_hd__buf_6, sky130_fd_sc_hd__and2_4, sky130_fd_sc_hd__a221oi_2, sky130_fd_sc_hd__nand4_1, sky130_fd_sc_hd__a22oi_1
- Consider registering the memory read address or read data and documenting the added response latency.
- If this is a flop-array standing in for memory, consider replacing it with a proper SRAM/register-file implementation or banking the read path.
- Keep any patch as a human-reviewed RTL proposal because it can change read latency and memory semantics.
Human review required for: pipeline insertion, response latency changes, memory read/write semantic changes

### 6. synthesized memory/flop-array read mux (RTL advisory)
Slack: 0.039 ns
Path group: clk
Startpoint: `service_addr_q_reg[1]/Q`
Endpoint: `rsp_rdata_q_reg[1]/D`
Dominant cells: sky130_fd_sc_hd__dfxtp_1, sky130_fd_sc_hd__inv_2, sky130_fd_sc_hd__nand2_8, sky130_fd_sc_hd__buf_2, sky130_fd_sc_hd__o22ai_1, sky130_fd_sc_hd__a221oi_1, sky130_fd_sc_hd__nand4_1, sky130_fd_sc_hd__a22oi_2
- Consider registering the memory read address or read data and documenting the added response latency.
- If this is a flop-array standing in for memory, consider replacing it with a proper SRAM/register-file implementation or banking the read path.
- Keep any patch as a human-reviewed RTL proposal because it can change read latency and memory semantics.
Human review required for: pipeline insertion, response latency changes, memory read/write semantic changes

### 7. unknown (Investigate)
Slack: 0.02 ns
Path group: clk
Startpoint: `u_request_queue_req_mem_reg[3][bank][1]/Q`
Endpoint: `u_request_queue_req_mem_reg[1][bank][0]/D`
Dominant cells: sky130_fd_sc_hd__dfxbp_2, sky130_fd_sc_hd__nand2_1, sky130_fd_sc_hd__nor2_1, sky130_fd_sc_hd__nand4_2, sky130_fd_sc_hd__nand3_2, sky130_fd_sc_hd__and2_4, sky130_fd_sc_hd__o221ai_4, sky130_fd_sc_hd__nand3_4
- Collect full path, QoR, fanout, transition, capacitance, and congestion reports before applying changes.

### 8. unknown (Investigate)
Slack: 0.026 ns
Path group: clk
Startpoint: `u_request_queue_req_mem_reg[3][bank][1]/Q`
Endpoint: `u_request_queue_req_mem_reg[1][bank][1]/D`
Dominant cells: sky130_fd_sc_hd__dfxbp_2, sky130_fd_sc_hd__nand2_1, sky130_fd_sc_hd__nor2_1, sky130_fd_sc_hd__nand4_2, sky130_fd_sc_hd__nand3_2, sky130_fd_sc_hd__and2_4, sky130_fd_sc_hd__o221ai_4, sky130_fd_sc_hd__nand3_4
- Collect full path, QoR, fanout, transition, capacitance, and congestion reports before applying changes.

### 9. unknown (Investigate)
Slack: 0.037 ns
Path group: clk
Startpoint: `u_request_queue_req_mem_reg[3][bank][1]/Q`
Endpoint: `u_request_queue_req_mem_reg[1][row][0]/D`
Dominant cells: sky130_fd_sc_hd__dfxbp_2, sky130_fd_sc_hd__nand2_1, sky130_fd_sc_hd__nor2_1, sky130_fd_sc_hd__nand4_2, sky130_fd_sc_hd__nand3_2, sky130_fd_sc_hd__and2_4, sky130_fd_sc_hd__o221ai_4, sky130_fd_sc_hd__nand3_4
- Collect full path, QoR, fanout, transition, capacitance, and congestion reports before applying changes.

### 10. unknown (Investigate)
Slack: 0.039 ns
Path group: clk
Startpoint: `u_request_queue_req_mem_reg[3][bank][1]/Q`
Endpoint: `u_request_queue_req_mem_reg[1][row][1]/D`
Dominant cells: sky130_fd_sc_hd__dfxbp_2, sky130_fd_sc_hd__nand2_1, sky130_fd_sc_hd__nor2_1, sky130_fd_sc_hd__nand4_2, sky130_fd_sc_hd__nand3_2, sky130_fd_sc_hd__and2_4, sky130_fd_sc_hd__o221ai_4, sky130_fd_sc_hd__nand3_4
- Collect full path, QoR, fanout, transition, capacitance, and congestion reports before applying changes.

RTL behavior is never changed by this analyzer. RTL-facing output is advisory only.

Limits: die footprint <= 4 mm²; reported total power <= 2 W.

Attempt 1 (preset): passed; WNS 0.008; measurements {'die_area_mm2': 0.153512856, 'total_power_w': 0.04411068, 'max_die_area_mm2': 8.0, 'max_power_w': 2.0, 'within_limits': True, 'power_basis': 'Innovus report_power; activity and analysis conditions inherited from the design'}
