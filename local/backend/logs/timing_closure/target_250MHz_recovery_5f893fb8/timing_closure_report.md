# Setup timing closure: failed

AI repeated an already measured resize plan; stopping without rerunning it.

Run: `target_250MHz_recovery_5f893fb8`

# Timing Closure Recommendation Report

Target: 4.000 ns (250.000 MHz)
WNS: -0.228
TNS: None
Violating paths: 10
Worst category: synthesized memory/flop-array read mux
Worst path: service_addr_q_reg[2]/Q -> rsp_rdata_q_reg[16]/D

## Ranked Recommendations

### 1. synthesized memory/flop-array read mux (RTL advisory)
Slack: -0.228 ns
Path group: clk
Startpoint: `service_addr_q_reg[2]/Q`
Endpoint: `rsp_rdata_q_reg[16]/D`
Dominant cells: sky130_fd_sc_hd__dfxtp_2, sky130_fd_sc_hd__nor2_1, sky130_fd_sc_hd__buf_4, sky130_fd_sc_hd__inv_2, sky130_fd_sc_hd__inv_4, sky130_fd_sc_hd__buf_6, sky130_fd_sc_hd__a221oi_1, sky130_fd_sc_hd__o22ai_1
- Consider registering the memory read address or read data and documenting the added response latency.
- If this is a flop-array standing in for memory, consider replacing it with a proper SRAM/register-file implementation or banking the read path.
- Keep any patch as a human-reviewed RTL proposal because it can change read latency and memory semantics.
Human review required for: pipeline insertion, response latency changes, memory read/write semantic changes

### 2. synthesized memory/flop-array read mux (RTL advisory)
Slack: -0.216 ns
Path group: clk
Startpoint: `service_addr_q_reg[2]/Q`
Endpoint: `rsp_rdata_q_reg[25]/D`
Dominant cells: sky130_fd_sc_hd__dfxtp_2, sky130_fd_sc_hd__nor2_1, sky130_fd_sc_hd__buf_4, sky130_fd_sc_hd__clkbuf_1, sky130_fd_sc_hd__a221oi_1, sky130_fd_sc_hd__o22ai_1, sky130_fd_sc_hd__nand4_1, sky130_fd_sc_hd__a22oi_1
- Consider registering the memory read address or read data and documenting the added response latency.
- If this is a flop-array standing in for memory, consider replacing it with a proper SRAM/register-file implementation or banking the read path.
- Keep any patch as a human-reviewed RTL proposal because it can change read latency and memory semantics.
Human review required for: pipeline insertion, response latency changes, memory read/write semantic changes

### 3. synthesized memory/flop-array read mux (RTL advisory)
Slack: -0.19 ns
Path group: clk
Startpoint: `service_addr_q_reg[2]/Q`
Endpoint: `rsp_rdata_q_reg[27]/D`
Dominant cells: sky130_fd_sc_hd__dfxtp_2, sky130_fd_sc_hd__nor2_1, sky130_fd_sc_hd__buf_4, sky130_fd_sc_hd__inv_2, sky130_fd_sc_hd__inv_4, sky130_fd_sc_hd__and2_4, sky130_fd_sc_hd__clkbuf_1, sky130_fd_sc_hd__a221oi_1
- Consider registering the memory read address or read data and documenting the added response latency.
- If this is a flop-array standing in for memory, consider replacing it with a proper SRAM/register-file implementation or banking the read path.
- Keep any patch as a human-reviewed RTL proposal because it can change read latency and memory semantics.
Human review required for: pipeline insertion, response latency changes, memory read/write semantic changes

### 4. synthesized memory/flop-array read mux (RTL advisory)
Slack: -0.187 ns
Path group: clk
Startpoint: `service_addr_q_reg[2]/Q`
Endpoint: `rsp_rdata_q_reg[2]/D`
Dominant cells: sky130_fd_sc_hd__dfxtp_2, sky130_fd_sc_hd__nor2_1, sky130_fd_sc_hd__buf_4, sky130_fd_sc_hd__inv_2, sky130_fd_sc_hd__inv_4, sky130_fd_sc_hd__clkbuf_1, sky130_fd_sc_hd__and2_4, sky130_fd_sc_hd__a22oi_1
- Consider registering the memory read address or read data and documenting the added response latency.
- If this is a flop-array standing in for memory, consider replacing it with a proper SRAM/register-file implementation or banking the read path.
- Keep any patch as a human-reviewed RTL proposal because it can change read latency and memory semantics.
Human review required for: pipeline insertion, response latency changes, memory read/write semantic changes

### 5. synthesized memory/flop-array read mux (RTL advisory)
Slack: -0.17 ns
Path group: clk
Startpoint: `service_addr_q_reg[2]/Q`
Endpoint: `rsp_rdata_q_reg[7]/D`
Dominant cells: sky130_fd_sc_hd__dfxtp_2, sky130_fd_sc_hd__nor2_1, sky130_fd_sc_hd__buf_4, sky130_fd_sc_hd__clkbuf_1, sky130_fd_sc_hd__a221oi_1, sky130_fd_sc_hd__o22ai_1, sky130_fd_sc_hd__nand4_1, sky130_fd_sc_hd__a22oi_1
- Consider registering the memory read address or read data and documenting the added response latency.
- If this is a flop-array standing in for memory, consider replacing it with a proper SRAM/register-file implementation or banking the read path.
- Keep any patch as a human-reviewed RTL proposal because it can change read latency and memory semantics.
Human review required for: pipeline insertion, response latency changes, memory read/write semantic changes

### 6. synthesized memory/flop-array read mux (RTL advisory)
Slack: -0.143 ns
Path group: clk
Startpoint: `service_addr_q_reg[2]/Q`
Endpoint: `rsp_rdata_q_reg[13]/D`
Dominant cells: sky130_fd_sc_hd__dfxtp_2, sky130_fd_sc_hd__nor2_1, sky130_fd_sc_hd__buf_4, sky130_fd_sc_hd__inv_2, sky130_fd_sc_hd__inv_4, sky130_fd_sc_hd__clkbuf_1, sky130_fd_sc_hd__and2_4, sky130_fd_sc_hd__a221oi_1
- Consider registering the memory read address or read data and documenting the added response latency.
- If this is a flop-array standing in for memory, consider replacing it with a proper SRAM/register-file implementation or banking the read path.
- Keep any patch as a human-reviewed RTL proposal because it can change read latency and memory semantics.
Human review required for: pipeline insertion, response latency changes, memory read/write semantic changes

### 7. synthesized memory/flop-array read mux (RTL advisory)
Slack: -0.107 ns
Path group: clk
Startpoint: `service_addr_q_reg[2]/Q`
Endpoint: `rsp_rdata_q_reg[8]/D`
Dominant cells: sky130_fd_sc_hd__dfxtp_2, sky130_fd_sc_hd__nor2_1, sky130_fd_sc_hd__buf_4, sky130_fd_sc_hd__inv_2, sky130_fd_sc_hd__inv_4, sky130_fd_sc_hd__clkbuf_1, sky130_fd_sc_hd__and2_4, sky130_fd_sc_hd__a221oi_1
- Consider registering the memory read address or read data and documenting the added response latency.
- If this is a flop-array standing in for memory, consider replacing it with a proper SRAM/register-file implementation or banking the read path.
- Keep any patch as a human-reviewed RTL proposal because it can change read latency and memory semantics.
Human review required for: pipeline insertion, response latency changes, memory read/write semantic changes

### 8. synthesized memory/flop-array read mux (RTL advisory)
Slack: -0.089 ns
Path group: clk
Startpoint: `service_addr_q_reg[0]/Q`
Endpoint: `rsp_rdata_q_reg[29]/D`
Dominant cells: sky130_fd_sc_hd__dfxtp_1, sky130_fd_sc_hd__and2_4, sky130_fd_sc_hd__buf_6, sky130_fd_sc_hd__clkbuf_1, sky130_fd_sc_hd__a22o_1, sky130_fd_sc_hd__a221oi_1, sky130_fd_sc_hd__o22ai_1, sky130_fd_sc_hd__nand4_1
- Consider registering the memory read address or read data and documenting the added response latency.
- If this is a flop-array standing in for memory, consider replacing it with a proper SRAM/register-file implementation or banking the read path.
- Keep any patch as a human-reviewed RTL proposal because it can change read latency and memory semantics.
Human review required for: pipeline insertion, response latency changes, memory read/write semantic changes

### 9. synthesized memory/flop-array read mux (RTL advisory)
Slack: -0.082 ns
Path group: clk
Startpoint: `service_addr_q_reg[2]/Q`
Endpoint: `rsp_rdata_q_reg[5]/D`
Dominant cells: sky130_fd_sc_hd__dfxtp_2, sky130_fd_sc_hd__nor2_1, sky130_fd_sc_hd__buf_4, sky130_fd_sc_hd__inv_2, sky130_fd_sc_hd__inv_4, sky130_fd_sc_hd__clkbuf_1, sky130_fd_sc_hd__and2_4, sky130_fd_sc_hd__a221oi_1
- Consider registering the memory read address or read data and documenting the added response latency.
- If this is a flop-array standing in for memory, consider replacing it with a proper SRAM/register-file implementation or banking the read path.
- Keep any patch as a human-reviewed RTL proposal because it can change read latency and memory semantics.
Human review required for: pipeline insertion, response latency changes, memory read/write semantic changes

### 10. synthesized memory/flop-array read mux (RTL advisory)
Slack: -0.072 ns
Path group: clk
Startpoint: `service_addr_q_reg[2]/Q`
Endpoint: `rsp_rdata_q_reg[19]/D`
Dominant cells: sky130_fd_sc_hd__dfxtp_2, sky130_fd_sc_hd__nor2_1, sky130_fd_sc_hd__buf_4, sky130_fd_sc_hd__inv_2, sky130_fd_sc_hd__inv_4, sky130_fd_sc_hd__buf_6, sky130_fd_sc_hd__a221oi_1, sky130_fd_sc_hd__o22ai_1
- Consider registering the memory read address or read data and documenting the added response latency.
- If this is a flop-array standing in for memory, consider replacing it with a proper SRAM/register-file implementation or banking the read path.
- Keep any patch as a human-reviewed RTL proposal because it can change read latency and memory semantics.
Human review required for: pipeline insertion, response latency changes, memory read/write semantic changes

RTL behavior is never changed by this analyzer. RTL-facing output is advisory only.

Limits: die footprint <= 4 mm²; reported total power <= 2 W.

Attempt 1 (restore_best): violated; WNS -0.256; measurements {'die_area_mm2': 0.1668525248, 'total_power_w': 0.04221991, 'max_die_area_mm2': 8.0, 'max_power_w': 2.0, 'within_limits': True, 'power_basis': 'Innovus report_power; activity and analysis conditions inherited from the design'}

Attempt 2 (ai_resize): hold_violated; WNS -0.211; measurements {'die_area_mm2': 0.1668525248, 'total_power_w': 0.04222376, 'max_die_area_mm2': 8.0, 'max_power_w': 2.0, 'within_limits': True, 'power_basis': 'Innovus report_power; activity and analysis conditions inherited from the design'}
AI rationale: Post-route WNS -0.256 ns on multiple paths sharing the same early portion: service_addr_q_reg[2] -> g189594 (nor2_1) -> FE_OFC218_n_38 (buf_4). This fanout/common segment appears in Paths 1-8 and dominates improvement leverage. Current power/area are far below limits, so a small, targeted upsizing on this shared driver/buffer is a low-risk experiment. Avoid resizing path-unique late-stage a22oi cells first since they differ per endpoint and would require many changes for similar aggregate benefit.

Attempt 3 (ai_resize): hold_violated; WNS -0.228; measurements {'die_area_mm2': 0.1668525248, 'total_power_w': 0.04222295, 'max_die_area_mm2': 8.0, 'max_power_w': 2.0, 'within_limits': True, 'power_basis': 'Innovus report_power; activity and analysis conditions inherited from the design'}
AI rationale: Current best eligible checkpoint is attempt 1 (WNS -0.256 ns, within area/power). The only other measured experiment (attempt 2) improved setup slightly (to -0.211 ns) but introduced hold violations (hold WNS -0.042), so we must avoid repeating that plan. To gain setup margin without risking the same hold regression, target the latest, largest-delay common endpoint-stage in the worst path: g183318__7098 (a22oi_2) contributes ~0.995 ns on Path 1. Upsizing this cell should directly reduce the dominant late-stage delay on the worst path and similar endpoints that use the _a22oi_2 variant, with less impact on early/common launch-side timing that can aggravate hold across many paths. Keep the change minimal (single instance) to stay evidence-based and limit downstream ripple.
