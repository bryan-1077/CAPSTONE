# Setup timing closure: failed

Timing/area/power closure failed after 3 backend attempts and 3 AI recovery attempts. Best eligible attempt: build_GDSII_250MHz_02. See logs/timing_closure/timing_closure_report.md.

Run: `target_250MHz`

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
Slack: -0.203 ns
Path group: clk
Startpoint: `service_addr_q_reg[2]/Q`
Endpoint: `rsp_rdata_q_reg[25]/D`
Dominant cells: sky130_fd_sc_hd__dfxtp_2, sky130_fd_sc_hd__nor2_1, sky130_fd_sc_hd__buf_4, sky130_fd_sc_hd__clkbuf_1, sky130_fd_sc_hd__a221oi_1, sky130_fd_sc_hd__o22ai_1, sky130_fd_sc_hd__nand4_1, sky130_fd_sc_hd__a22oi_1
- Consider registering the memory read address or read data and documenting the added response latency.
- If this is a flop-array standing in for memory, consider replacing it with a proper SRAM/register-file implementation or banking the read path.
- Keep any patch as a human-reviewed RTL proposal because it can change read latency and memory semantics.
Human review required for: pipeline insertion, response latency changes, memory read/write semantic changes

### 3. synthesized memory/flop-array read mux (RTL advisory)
Slack: -0.196 ns
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
Slack: -0.169 ns
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
Slack: -0.075 ns
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

Attempt 1 (preset): violated; WNS -0.802; measurements {'die_area_mm2': 0.153512856, 'total_power_w': 0.04219701, 'max_die_area_mm2': 8.0, 'max_power_w': 2.0, 'within_limits': True, 'power_basis': 'Innovus report_power; activity and analysis conditions inherited from the design'}

Attempt 2 (preset): violated; WNS -0.254; measurements {'die_area_mm2': 0.1668525248, 'total_power_w': 0.04221258, 'max_die_area_mm2': 8.0, 'max_power_w': 2.0, 'within_limits': True, 'power_basis': 'Innovus report_power; activity and analysis conditions inherited from the design'}

Attempt 3 (preset): violated; WNS -0.323; measurements {'die_area_mm2': 0.1824975296, 'total_power_w': 0.04285022, 'max_die_area_mm2': 8.0, 'max_power_w': 2.0, 'within_limits': True, 'power_basis': 'Innovus report_power; activity and analysis conditions inherited from the design'}

Attempt 4 (ai_resize): hold_violated; WNS -0.26; measurements {'die_area_mm2': 0.1668525248, 'total_power_w': 0.04222306, 'max_die_area_mm2': 8.0, 'max_power_w': 2.0, 'within_limits': True, 'power_basis': 'Innovus report_power; activity and analysis conditions inherited from the design'}
AI rationale: Best measured checkpoint is build_GDSII_250MHz_02: setup WNS=-0.254ns, hold has small violations down to -0.041ns on fast input-to-flop paths. Limits are very safe (area 0.1669mm^2 << 4mm^2, power 0.042W << 2W). The dominant setup critical paths share a large late-stage a22oi cell delay (~0.998–1.147ns) (e.g., g183318__7098, g183325__9315, g183314__2802, g183329__1666). A single targeted upsize of the worst of these (g183325__9315 at 1.147ns) is a low-risk, evidence-based experiment. Avoid upsizing earlier common driver g189594/FE_OFC218_n_38 as they feed many endpoints and may worsen holds/clock power. Hold fixes should be left to the tool during hold optimization; upsizing a late data-path gate typically does not worsen hold and may slightly help by increasing min delay upstream loading effects handled by the optimizer.

Attempt 5 (ai_resize): hold_violated; WNS -0.255; measurements {'die_area_mm2': 0.1668525248, 'total_power_w': 0.0422219, 'max_die_area_mm2': 8.0, 'max_power_w': 2.0, 'within_limits': True, 'power_basis': 'Innovus report_power; activity and analysis conditions inherited from the design'}
AI rationale: Best eligible checkpoint is build_GDSII_250MHz_02 (setup WNS=-0.254ns; hold WNS=-0.041ns; area=0.1669mm^2, power=0.0422W well within limits). Prior experiment (attempt 4) upsizing g183325__9315 to a22oi_4 slightly worsened setup (WNS -0.260) and did not improve hold, so we avoid repeating it. Current worst setup paths (1/2/3/8/9/10 shown) consistently have very large delay concentrated in late-stage a22oi cells near the endpoints: g183318__7098 (0.998ns, already a22oi_2), g183305__2398 (0.859ns), g183314__2802 (1.053ns), g183329__1666 (1.131ns). A small, targeted next experiment is to upsize the highest-delay a22oi_1 instances that appear on multiple violated paths (g183314__2802 and g183329__1666) by one step to a22oi_2 to reduce delay without the more aggressive a22oi_4 jump that previously failed on a similar cell. Hold violations are on separate fast input->flop paths (a22o_1) not in resize_choices, so we must not worsen hold by touching shared early logic; these endpoint-near resizes are least likely to impact those holds and Innovus hold optimization can still insert delay cells if needed.

Attempt 6 (ai_resize): hold_violated; WNS -0.228; measurements {'die_area_mm2': 0.1668525248, 'total_power_w': 0.04222205, 'max_die_area_mm2': 8.0, 'max_power_w': 2.0, 'within_limits': True, 'power_basis': 'Innovus report_power; activity and analysis conditions inherited from the design'}
AI rationale: Using best measured eligible checkpoint build_GDSII_250MHz_02 (setup WNS=-0.254ns; hold WNS=-0.041ns; area=0.1669mm^2 and power=0.0422W well within 4mm^2/2W limits). Previous resizes were counterproductive: (attempt 4) g183325__9315->a22oi_4 worsened setup to -0.260ns; (attempt 5) g183329__1666 and g183314__2802 ->a22oi_2 did not improve setup (WNS -0.255ns). Avoid repeating those. The dominant setup slack is still driven by very large late-stage a22oi delays near endpoints (e.g., g183318__7098 at ~0.998ns is already a22oi_2). Next lowest-risk lever is to upsize g183318__7098 itself to a22oi_4, since it is the last big contributor on the worst setup path (Path 1) and not previously tried. Avoid touching early shared drivers (g189594/FE_OFC218_n_38/clock buffers) due to broad fanout and potential hold/power impact; hold violations are on separate fast input->flop paths not in resize_choices, so we should not make them harder by global speeding.
