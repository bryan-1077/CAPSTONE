# Physical verification agent

The backend runs this gate after every completed GDS implementation template
attempt, before the timing agent evaluates that attempt. This includes each
preset and each timing ECO/restore attempt, even when setup slack is negative.
Physical repair candidates also receive fresh verification within the physical
agent's own loop. Clean reports
require no AI request. Only a positive DRC count triggers the separate
`agents/openai_physical_verification.py` planner, with up to three repair attempts
by default.
When DRC is zero, the repair planner and implementation are skipped. Remaining
antenna violations still fail verification without launching the repair agent.

The same physical agent has a separate checkpoint-selection role when setup timing
still fails after the backend presets. It compares all preset geometry, hold, and
post-route setup reports and chooses a verified eligible source for the timing
resizer. This comparison can run on clean DRC builds; it does not launch physical
repair. Only clean DRC/antenna, passing hold, and area/power-compliant presets with
valid setup evidence are selectable. Its choice and rationale are stored under the
timing run's `physical_selection*.json` artifacts. A passing preset needs neither
selection nor resizing.

Each generated build exports `reports/<top>.geom.rpt` and
`reports/<top>.antenna.rpt` using explicit Innovus report paths. For this controller,
these are `ddr4_controller_top.geom.rpt` and `ddr4_controller_top.antenna.rpt`.
The agent derives the filename prefix from `mapped_resolved_top`, then
`mapped_top_module`, then `top_module`; it does not hardcode the controller name.
The parser accepts explicit numeric totals and Innovus's clean messages
(`No DRC violations were found` and `No Violations Found`). Repeated matching
totals are accepted; conflicting results are rejected.
Missing reports, missing/conflicting totals, tool
errors, or nonzero counts cannot pass. This is the Innovus physical verification
gate; it does not replace foundry signoff checks such as LVS or external DRC.

The agent chooses data-only recipes supported by current report evidence:

- `reroute`: restore the routed checkpoint, remove only the flow's `FILL`
  fillers, perform targeted ECO routing, then reinsert fillers and reroute.
- `refill_and_reroute`: for reported filler conflicts, remove the flow's fillers
  and perform general ECO routing before legal filler reinsertion and rerouting.
- `antenna_cleanup`: enable routing with the configured loaded PDK diode and
  allow up to five targeted diode insertion/cleanup passes.

The executor does not run AI-generated Tcl or modify RTL, constraints, PDK rules,
or violation reports. It restores `db/06_final.enc` into a newly reserved build;
existing builds remain available. Every candidate needs fresh zero-violation DRC
and antenna reports, passing hold timing, and measurements within the existing
area/power limits. Setup timing is measured at the requested clock for every
candidate. During template evaluation, an existing setup violation belongs to
the timing agent: physical repair may preserve or improve it, but may not worsen
it or introduce a setup failure into a passing template. Standalone physical
closure additionally requires passing setup timing. Candidates that regress
timing, exceed limits, or fail to reduce physical violations are rejected and the
source checkpoint is retained.
Unsupported plans, unavailable AI, repeated rejected plans, and exhausted budgets
reject the template with `gdsii=failed`, owned by `physical_verification`. The
timing loop can continue to its next template, but physically dirty templates
are excluded from its eligible recovery checkpoints. Timing measurements and
checkpoint selection use the repaired build when physical repair succeeds.
Upstream synthesis is not automatically rebuilt. Full backend completion still
requires both clean physical verification and passing setup timing.

The existing command enables the gate automatically:

```bash
python3 local/backend/app.py --target-mhz 260
```

Configuration loaded by `app.py`:

- `EDA_PHYSICAL_VERIFICATION_ENABLED`: defaults to `1`. Set to `0` only for
  explicit generation/timing-only experiments; returned status is `disabled`.
- `EDA_PHYSICAL_VERIFICATION_AI_ENABLED`: defaults to `1`. Setting `0` keeps
  deterministic verification mandatory but disables repair requests.
- `EDA_PHYSICAL_VERIFICATION_MAX_ATTEMPTS`: defaults to `3`; accepts 1–10.

State includes `physical_verification_status`, `physical_verification_analysis`,
`physical_verification_attempts`, and `physical_verification_report_dir`. Reports,
plans, API diagnostics, rejected candidate states, and terminal transcripts live
under `logs/physical_verification/<unique-run-id>/`, or the invocation's explicit
`--log-dir`. `physical_verification_status.json` at the parent summarizes the
latest run. The frontend adapter requires a pass when this gate is enabled.

Run local tests without SSH, model requests, or Cadence:

```bash
PYTHONPATH=local/backend python3 -m unittest discover -s local/backend/physical_verification/tests -v
```
