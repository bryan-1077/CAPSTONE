# DDR4 Controller RTL Handoff

## Design Description
`ddr4_controller_top` is a generated top-level DDR4 controller wrapper that combines the selected scheduler policy, 4 bank integration path(s), and any enabled optional timing modules into one integration point.

Generated page policy: `open_page`.

## Compile Instructions
Compile the package from the `rtl_output/` directory using the generated filelist:

```sh
iverilog -g2012 -s ddr4_controller_top -f filelist.f
```

## Top Module
- `ddr4_controller_top`

## IO Description
- `clk` (input): Controller clock.
- `rst_n` (input): Active-low synchronous reset.
- `txn_valid` (input): Presents a request; acceptance occurs on a rising edge with txn_valid and txn_ready high. Hold payload stable while stalled.
- `txn_is_write` (input): Transaction type selector: 0=READ, 1=WRITE.
- `txn_addr` (input): Address for the minimal banked storage model (4 bits).
- `txn_wdata` (input): Write data for accepted WRITE transactions (32 bits).
- `txn_bank` (input): Selects one of 4 banks for the single incoming transaction stream.
- `txn_ready` (output): Queue acceptance readiness; supports a new request every ready/valid cycle, including consecutive cycles.
- `cmd_ready` (output): Legacy execution/timing readiness for the incoming address; not the host acceptance handshake.
- `rsp_valid` (output): One-cycle pulse at read service completion; host enqueue and execution dispatch are separate events.
- `rsp_rdata` (output): Read response data returned from the selected banked storage (32 bits).

## Feature Summary
- Instantiates 4 reusable ddr4_bank_top integration block(s) to package the per-bank FSM chain.
- Buffers up to four pending requests and selects queued requests with row-hit priority; bank selection is stored with each request.
- Maps txn_is_write into bank-local cmd_type values so READ and WRITE remain visible through the control path.
- Implements a small banked storage model in the controller wrapper so accepted WRITEs store data and accepted READs return stored data.
- Keeps the serviced row open after completion so later accesses can reuse it.
- Accepts host traffic with txn_valid/txn_ready independently of execution; cmd_ready remains execution-readiness visibility.
- Instantiates the refresh controller and gives refresh requests fixed priority in the scheduler path.
- Gates transaction issue with shared controller-level tFAW and tRRD activation-spacing checks.
- Returns read data on rsp_rdata with a fixed 1-cycle rsp_valid pulse and no write response.

## Generated Modules
- 11 modules across 11 SystemVerilog files
- `ddr4_bank_activate_fsm`
- `ddr4_bank_bank_sequencer`
- `ddr4_bank_precharge_fsm`
- `ddr4_bank_tRAS_fsm`
- `ddr4_bank_top`
- `ddr4_controller_top`
- `ddr4_refresh_refresh_controller`
- `ddr4_request_queue`
- `ddr4_scheduler_scheduler`
- `ddr4_tFAW_tFAW_tracker`
- `ddr4_tRRD_simple_tRRD`

## Known Simplifications
- The wrapper exposes one transaction stream with explicit bank selection; queue selection can reorder requests, and same-address ordering is not yet guaranteed.
- READ and WRITE share the same simplified bank sequencing structure; cmd_type preserves direction semantics without changing the bank FSM structure.
- Read responses follow service completion; queue wait and row-dependent service add latency after host acceptance.
- WRITE transactions update the minimal banked storage model and do not produce a response payload.
- Page policy is modeled at the wrapper level: open_page controls whether a serviced row remains open after completion.
- Refresh remains a simplified top-level event source and is not modeled as a detailed per-bank flow.
- tFAW and tRRD remain controller-global gates even when multiple banks are instantiated.
