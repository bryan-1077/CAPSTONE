# LLM microarchitecture flow

The architecture stage makes design decisions before module RTL generation. An
LLM proposes module decomposition, responsibilities, behavioral contracts and
connections. A deterministic compiler validates and lowers that proposal. Feature
templates are reusable blocks; custom module specs do not require registration in
`GENERATED_INPUT_SPECS` or `SUPPORTED_FEATURES`.

```text
Requirements + constraints + reusable library/timing profiles
    → LLM architecture proposal
    → graph/module-IR validation (up to three proposal attempts)
    → architecture.yaml + review rationale and verification obligations
    → resolved module specs → existing IR/RTL routes
    → structural wrapper + exact filelist → integration lint
```

The flow is opt-in while controller-level equivalence is established. The legacy
config expansion and specialized controller wrapper remain available. Architecture
builds bypass that wrapper: all functional glue must be described in a module,
and the manifest supplies all inter-module wiring.

## Propose and review

From the repository root:

```sh
python3 local/frontend/run_flow.py local/frontend/configs/user_input.yaml \
  --microarch-only
```

This uses `TAMUS_AI_CHAT_API_KEY` and the project's existing TAMU chat endpoint.
`--microarch-model` selects the architecture model; its default comes from
`DEFAULT_MODEL` in `generate_microarch.py`, shared by both entry points.
The standalone `generate_microarch.py` also creates a proposal and module YAMLs without RTL calls;
its model flag is `--model`. Neither command requires a new SDK.

Default proposal: `local/frontend/microarch/proposals/architecture.yaml`, with a
Markdown review beside it. Proposal-only runs now also emit concrete module YAMLs
under `local/frontend/microarch/proposals/module_specs/<manifest-hash>/` and an
`architecture.modules.json` verification index beside the proposal. This does not
generate RTL, a wrapper, or module IR. Custom specs and reusable templates use the
same resolved module format as the RTL build path. Each YAML includes behavior,
ports, constraints/invariants, timing substitutions and `verification_requirements`.
The index maps each instance and role to its YAML path and owned requirements;
paths are relative to the index directory. Repeated instances share a definition.
The Markdown report links each YAML, and run_flow prints the directory/index. Inspect ownership, interfaces, assumptions, limitations,
and verification obligations before spending time on module RTL generation.

For architecture exploration, provide a YAML mapping containing constraints and
a `requirements` list of strings. Unlike the legacy text extractor, this entry
point does not restrict requests to existing schedulers, bank counts or feature
names. `--requirements-file` adds an entire text file as another requirement.
The proposal must preserve each requirement's ID and exact text, identify owners,
and describe a verification scenario. This checks traceability, not whether the
behavioral design actually satisfies the requirement. Config constraints are
included verbatim in the prompt/provenance; their semantic satisfaction still
needs review and behavioral tests.

Legacy controller configs (including `configs/user_input.yaml`) automatically
attach the versioned `current_controller_v1` baseline from
`controller_baseline.py`. This preserves the existing top name, host ports/widths,
4-bit address mapping, queue admission/ownership, functional banked memory and
read responses, and existing tRCD/tRAS/tRP timing protection. It also specifies
four waiting requests plus one separately stored active request, ownership through any PRE/ACT setup, and separates candidate permission
from accepted ACT updates. These are behavioral requirements, not fixed module
names or template choices. Controller proposals MUST have separately owned
`request_queue`, `scheduler`, `command_engine`, `timing` and `memory` roles. Declare
`roles` on each module; optional roles are `host_adapter`, `row_state`, `refresh`.
Mandatory roles cannot share a module instance or definition. Each role must have
functional connections to other modules; clock/reset-only dummy modules do not
satisfy decomposition. Related baseline requirement owners must include their
functional role owner. The compiler attaches `design_kind: memory_controller` to
controller proposals; small standalone block examples may still use one module.
Profile/host-interface and exact requirement coverage are checked;
behavioral satisfaction still needs RTL tests.

The compiler attaches the baseline contract to saved proposals and rechecks it
when building directly from a manifest. Missing/changed baseline requirements and
renamed host ports fail validation. Proposals from before this contract must be
regenerated; `--cache` correctly rejects the old prompt/compiler fingerprint.
To explore an intentionally incompatible design, a request can explicitly set
`microarchitecture: {baseline: none}`. A generic requirements-only request has no
legacy baseline by default; `baseline: current_controller` opts into it.

```yaml
design_name: ddr4_controller
memory:
  protocol: DDR4
  speed: '3200'
topology:
  banks: 4
requirements:
  - Use queued requests and preserve the current host acceptance contract.
  - Explore shared versus per-bank control and explain the selected decomposition.
```

`start_flow.sh --microarch-only --input path/to/request.yaml` is the equivalent
entry point from `local/frontend/`. This bypasses the old constrained text
configuration extractor. `--microarch` without `--input` uses the existing config.
The existing `--interactive` prompts belong to the legacy flow; use
`--microarch-only` and then a separate manifest build for architecture review.

## Generate from a manifest

```sh
python3 local/frontend/run_flow.py \
  local/frontend/microarch/proposals/architecture.yaml
```

Builds default to `local/frontend/microarch/builds/<manifest-hash>/`; use
`--build-dir` to choose an isolated directory. Builds contain the resolved specs,
IR, RTL, `architecture.yaml`, `filelist.f`, `build_status.json`, lint evidence,
and `verification_plan.json`. The existing controller RTL is not regenerated by
this path. A new FSM follows the existing deterministic FSM emitter; a new
datapath follows dual-LLM generation/review. Known reusable timing blocks retain
their specialized deterministic emitters.

`--cache` on a requirements/config input requires an existing proposal matching
the request, library, prompt/compiler and model. It never falls back to a live
call. Architecture builds keep reviewed RTL caches separate from legacy caches,
keyed by resolved module spec content. `--cache` on a manifest skips proposal
creation and uses those module caches; deterministic modules still regenerate.

## Offline example

The checked-in timing example is a small compiler smoke test, not a controller:

```sh
python3 local/frontend/run_flow.py \
  local/frontend/microarch/examples/timing_request.yaml --microarch \
  --microarch-response local/frontend/microarch/examples/timing_proposal.json \
  --architecture-output /tmp/capstone-proposal.yaml \
  --build-dir /tmp/capstone-build

python3 -m unittest discover -s local/frontend -p test_microarchitecture.py -v
```

A replay file must contain JSON with the same manifest shape as an LLM response.
It passes the same validation and requirement-coverage checks. Replay proves the
compiler/generation path; it does not prove a live model produces good proposals.

## Manifest contract and current compiler limits

`architecture_version: 1` requires `design_name`, `profile`, `rationale`,
`top_ports`, `modules`, `connections`, and `requirements`. Optional `assumptions`
and `limitations` are string lists. The generator supplies `provenance` containing
the input snapshot, model, fingerprint and repair diagnostics.

Each module has an `instance`, a `responsibility`, controller `roles` when applicable, and exactly one source:

- `reuse`: `feature`, `submodule`, optional `design_name`; timing and behavior are
  resolved from the existing library/profile. Interface/behavior overrides need
  a custom spec instead.
- `spec`: a concrete existing module spec with `design_name`, `design_type`,
  `clock`, `reset`, `ports`, and FSM state/output definitions or datapath `behavior`.
  Explicit clock/reset names are implicit input ports. Do not list them again
  in custom `ports`. The compiler normalizes existing library clock/reset ports.

Connections use `from: instance.port` and `to: instance.port`, with `top.port`
for host ports. Top inputs and child outputs are sources; child inputs and top
outputs are sinks. Source fanout is supported. Every sink needs exactly one
source; widths must match. Top inputs must be used. Optional child outputs may
be left open with a top-level `unused_outputs` entry such as
`{endpoint: u_tfaw.act_count, reason: Debug count is not used}`. The wrapper emits
an explicit `.act_count()` connection; dummy consumers are not required. Repeated instances
may share an identical module spec. Different definitions cannot share a module
name. Special names triggering existing timing/scheduler rewrites are reserved
for their matching reusable templates.

Custom datapath specs require `combinational_dependencies`, mapping every output
to its current-cycle input dependencies. For example `ready: [valid]` declares a
combinational path; `data: []` declares no current-cycle input dependency. Include
transitive paths through internal expressions. FSM dependencies are inferred from
output expressions; reusable blocks use the dependency models in
`architecture_dependencies.py`. The compiler rejects cycles across wiring and
these declared paths. Incorrect model declarations can still hide bugs, so RTL
lint and behavioral tests remain necessary. The existing tFAW template's prospective
permission outputs depend on `act_pulse`; directly gating that accepted pulse with
those outputs is detected as feedback.

Current lowering supports one top-level clock/reset source, consistent reset
polarity, fixed positive integer port widths, input/output ports, and full-port
connections. Synchronous and asynchronous assertion styles can coexist on the
same reset source/polarity. Constants, slices, expressions on connections, parameter overrides,
instance arrays and inout ports are unsupported. Unknown architectural fields
fail rather than being ignored. Glue logic belongs in a module spec. New IR
constructs should be added explicitly when a useful proposal needs them.

Structural/dependency checks cannot establish same-address ordering, fairness,
command legality, refresh correctness, or absence of undeclared/internal
combinational loops. RTL
lint is a subsequent check. Arbitrary architectures do not use the specialized
legacy controller testbench. Verification obligations remain `planned_not_executed`
and build status records behavioral verification as `not_run`; runnable controller
scenario generation is a later integration task.

## Validation recorded — 2026-10-08

Offline regression covers graph errors, custom module lowering, reusable timing
resolution, implicit clock normalization, shared definitions/fanout, requirement
coverage, proposal repairs, stale-cache rejection, and failure without replacing
a valid proposal. End-to-end tests generate/lint reusable timing and custom FSM
builds, replay the architecture cache, and run a Verilator timing simulation.

The user subsequently ran a live TAMU proposal successfully: the saved provenance
shows `protected.gpt-5.4` and two validation repairs before success. Review found
missing baseline timing/data behavior and declared behavior implying feedback.
The strengthened baseline/dependency checks have offline regression coverage;
a new live proposal under those checks and controller-level behavioral equivalence
remain to establish before adopting this path as the default.

Regression coverage also includes baseline attachment and preservation, host-port
repair feedback, explicit unused outputs, mixed reset assertion styles, and
combinational-loop rejection with valid registered feedback allowed.

## Modular controller and specification-export checks — 2026-10-09

The reviewed Terra proposal grouped everything into one core; controller validation
now rejects that decomposition. Regenerate it with `--microarch-only` to obtain
separate module contracts. Baseline capacity is explicitly four WAITING requests
plus one separately stored active request: waiting dequeue at dispatch and active
retirement at final service are distinct events. The prior proposal is kept for
inspection and is no longer accepted by the strengthened contract.

Regressions cover role separation, failed proposal preservation, concrete module
YAML export, requirement/role index contents, and proposal-only CLI output without
RTL or module IR. Module verification itself remains a subsequent task.
