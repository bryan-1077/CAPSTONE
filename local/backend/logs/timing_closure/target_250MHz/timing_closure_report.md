# Setup timing closure: passed

Setup timing passed: WNS 0.034 ns at 4.000 ns.

Run: `target_250MHz`

# Timing Closure Recommendation Report

Target: 4.000 ns (250.000 MHz)
WNS: 0.034
TNS: None
Violating paths: None
Worst category: synthesized memory/flop-array read mux
Worst path: service_addr_q_reg[2]/Q -> rsp_rdata_q_reg[9]/D

## Ranked Recommendations

### 1. synthesized memory/flop-array read mux (RTL advisory)
Slack: 0.034 ns
Path group: clk
Startpoint: `service_addr_q_reg[2]/Q`
Endpoint: `rsp_rdata_q_reg[9]/D`
Dominant cells: sky130_fd_sc_hd__dfxtp_2, sky130_fd_sc_hd__nor2_1, sky130_fd_sc_hd__buf_4, sky130_fd_sc_hd__a221oi_1, sky130_fd_sc_hd__o22ai_1, sky130_fd_sc_hd__nand4_1, sky130_fd_sc_hd__a22oi_2, sky130_fd_sc_hd__nand2_1
- Consider registering the memory read address or read data and documenting the added response latency.
- If this is a flop-array standing in for memory, consider replacing it with a proper SRAM/register-file implementation or banking the read path.
- Keep any patch as a human-reviewed RTL proposal because it can change read latency and memory semantics.
Human review required for: pipeline insertion, response latency changes, memory read/write semantic changes

### 2. synthesized memory/flop-array read mux (RTL advisory)
Slack: 0.042 ns
Path group: clk
Startpoint: `service_addr_q_reg[3]/Q`
Endpoint: `rsp_rdata_q_reg[31]/D`
Dominant cells: sky130_fd_sc_hd__dfxbp_2, sky130_fd_sc_hd__or2_2, sky130_fd_sc_hd__inv_2, sky130_fd_sc_hd__and2_4, sky130_fd_sc_hd__a221oi_1, sky130_fd_sc_hd__nand4_1, sky130_fd_sc_hd__a22oi_1, sky130_fd_sc_hd__nand2_1
- Consider registering the memory read address or read data and documenting the added response latency.
- If this is a flop-array standing in for memory, consider replacing it with a proper SRAM/register-file implementation or banking the read path.
- Keep any patch as a human-reviewed RTL proposal because it can change read latency and memory semantics.
Human review required for: pipeline insertion, response latency changes, memory read/write semantic changes

### 3. synthesized memory/flop-array read mux (RTL advisory)
Slack: 0.057 ns
Path group: clk
Startpoint: `service_addr_q_reg[0]/Q`
Endpoint: `rsp_rdata_q_reg[0]/D`
Dominant cells: sky130_fd_sc_hd__dfxtp_1, sky130_fd_sc_hd__inv_2, sky130_fd_sc_hd__nand2_2, sky130_fd_sc_hd__inv_6, sky130_fd_sc_hd__inv_8, sky130_fd_sc_hd__buf_2, sky130_fd_sc_hd__a22o_1, sky130_fd_sc_hd__a221oi_1
- Consider registering the memory read address or read data and documenting the added response latency.
- If this is a flop-array standing in for memory, consider replacing it with a proper SRAM/register-file implementation or banking the read path.
- Keep any patch as a human-reviewed RTL proposal because it can change read latency and memory semantics.
Human review required for: pipeline insertion, response latency changes, memory read/write semantic changes

### 4. synthesized memory/flop-array read mux (RTL advisory)
Slack: 0.064 ns
Path group: clk
Startpoint: `service_addr_q_reg[3]/Q`
Endpoint: `rsp_rdata_q_reg[28]/D`
Dominant cells: sky130_fd_sc_hd__dfxbp_2, sky130_fd_sc_hd__or2_2, sky130_fd_sc_hd__inv_2, sky130_fd_sc_hd__and2_4, sky130_fd_sc_hd__a221oi_1, sky130_fd_sc_hd__nand4_1, sky130_fd_sc_hd__a22oi_1, sky130_fd_sc_hd__nand2_1
- Consider registering the memory read address or read data and documenting the added response latency.
- If this is a flop-array standing in for memory, consider replacing it with a proper SRAM/register-file implementation or banking the read path.
- Keep any patch as a human-reviewed RTL proposal because it can change read latency and memory semantics.
Human review required for: pipeline insertion, response latency changes, memory read/write semantic changes

### 5. unknown (Investigate)
Slack: 0.058 ns
Path group: clk
Startpoint: `u_request_queue_req_mem_reg[1][bank][0]/Q`
Endpoint: `u_request_queue_req_mem_reg[1][wdata][3]/D`
Dominant cells: sky130_fd_sc_hd__dfxtp_4, sky130_fd_sc_hd__nor2_1, sky130_fd_sc_hd__a2bb2oi_1, sky130_fd_sc_hd__nand3_1, sky130_fd_sc_hd__nand2_1, sky130_fd_sc_hd__inv_1, sky130_fd_sc_hd__nand2_2, sky130_fd_sc_hd__nor2_2
- Collect full path, QoR, fanout, transition, capacitance, and congestion reports before applying changes.

### 6. unknown (Investigate)
Slack: 0.058 ns
Path group: clk
Startpoint: `u_request_queue_req_mem_reg[1][bank][0]/Q`
Endpoint: `u_request_queue_req_mem_reg[1][wdata][28]/D`
Dominant cells: sky130_fd_sc_hd__dfxtp_4, sky130_fd_sc_hd__nor2_1, sky130_fd_sc_hd__a2bb2oi_1, sky130_fd_sc_hd__nand3_1, sky130_fd_sc_hd__nand2_1, sky130_fd_sc_hd__inv_1, sky130_fd_sc_hd__nand2_2, sky130_fd_sc_hd__nor2_2
- Collect full path, QoR, fanout, transition, capacitance, and congestion reports before applying changes.

### 7. unknown (Investigate)
Slack: 0.061 ns
Path group: clk
Startpoint: `u_request_queue_req_mem_reg[1][bank][0]/Q`
Endpoint: `u_request_queue_req_mem_reg[1][wdata][24]/D`
Dominant cells: sky130_fd_sc_hd__dfxtp_4, sky130_fd_sc_hd__nor2_1, sky130_fd_sc_hd__a2bb2oi_1, sky130_fd_sc_hd__nand3_1, sky130_fd_sc_hd__nand2_1, sky130_fd_sc_hd__inv_1, sky130_fd_sc_hd__nand2_2, sky130_fd_sc_hd__nor2_2
- Collect full path, QoR, fanout, transition, capacitance, and congestion reports before applying changes.

### 8. unknown (Investigate)
Slack: 0.061 ns
Path group: clk
Startpoint: `u_request_queue_req_mem_reg[1][bank][0]/Q`
Endpoint: `u_request_queue_req_mem_reg[1][wdata][5]/D`
Dominant cells: sky130_fd_sc_hd__dfxtp_4, sky130_fd_sc_hd__nor2_1, sky130_fd_sc_hd__a2bb2oi_1, sky130_fd_sc_hd__nand3_1, sky130_fd_sc_hd__nand2_1, sky130_fd_sc_hd__inv_1, sky130_fd_sc_hd__nand2_2, sky130_fd_sc_hd__nor2_2
- Collect full path, QoR, fanout, transition, capacitance, and congestion reports before applying changes.

### 9. unknown (Investigate)
Slack: 0.062 ns
Path group: clk
Startpoint: `u_request_queue_req_mem_reg[1][bank][0]/Q`
Endpoint: `u_request_queue_req_mem_reg[1][wdata][23]/D`
Dominant cells: sky130_fd_sc_hd__dfxtp_4, sky130_fd_sc_hd__nor2_1, sky130_fd_sc_hd__a2bb2oi_1, sky130_fd_sc_hd__nand3_1, sky130_fd_sc_hd__nand2_1, sky130_fd_sc_hd__inv_1, sky130_fd_sc_hd__nand2_2, sky130_fd_sc_hd__nor2_2
- Collect full path, QoR, fanout, transition, capacitance, and congestion reports before applying changes.

### 10. unknown (Investigate)
Slack: 0.062 ns
Path group: clk
Startpoint: `u_request_queue_req_mem_reg[1][bank][0]/Q`
Endpoint: `u_request_queue_req_mem_reg[1][wdata][15]/D`
Dominant cells: sky130_fd_sc_hd__dfxtp_4, sky130_fd_sc_hd__nor2_1, sky130_fd_sc_hd__a2bb2oi_1, sky130_fd_sc_hd__nand3_1, sky130_fd_sc_hd__nand2_1, sky130_fd_sc_hd__inv_1, sky130_fd_sc_hd__nand2_2, sky130_fd_sc_hd__nor2_2
- Collect full path, QoR, fanout, transition, capacitance, and congestion reports before applying changes.

RTL behavior is never changed by this analyzer. RTL-facing output is advisory only.

Limits: die footprint <= 4 mm²; reported total power <= 2 W.

Attempt 1 (preset): violated; WNS -0.101; measurements {'die_area_mm2': 0.153512856, 'total_power_w': 0.04225733, 'max_die_area_mm2': 8.0, 'max_power_w': 2.0, 'within_limits': True, 'power_basis': 'Innovus report_power; activity and analysis conditions inherited from the design'}

Attempt 2 (preset): passed; WNS 0.034; measurements {'die_area_mm2': 0.1668525248, 'total_power_w': 0.04223104, 'max_die_area_mm2': 8.0, 'max_power_w': 2.0, 'within_limits': True, 'power_basis': 'Innovus report_power; activity and analysis conditions inherited from the design'}
