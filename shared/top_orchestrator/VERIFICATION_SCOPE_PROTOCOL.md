# Module verification scope protocol, version 1

The frontend/orchestrator implementation is in [verification_scope.py](verification_scope.py).
Deployed consumer support has not yet been verified. Single-module transport is
now implemented behind explicit `--scope-plan`, including capability discovery
before agents. Without that flag the existing module path remains unchanged.
This does not alter Standard verification. Do not retry a paid legacy module
command expecting it to use this plan automatically.

## What is implemented on our side

- Authenticated source inventory and drafts with every scope initially unresolved.
- Explicit routing review and deterministic plan digest, bound to the full
  snapshot, RTL revision, module and original lowered YAML hash.
- Preservation of full rich source objects and legacy invariants, implementation
  constraints and forbidden-pattern records. Legacy records use stable
  `yaml.FIELD.INDEX` IDs; they are not silently discarded or implicitly simulated.
- Exact local-contract facet inventory, explicit checks, source contributions,
  environment assumptions and method validation. Behavioral facets require
  simulation; a packing check cannot alone certify the full local contract.
- Detection of multi-owner source requirements/obligations from authenticated
  ownership data. These cannot be marked fully verified by isolated module
  simulation. Metadata/structural checks can still inspect the complete package.
- A portable request bundle with the original isolated YAML/SV/context snapshot
  unchanged and the reviewed execution plan beside it.
- Offline result-body validation with method-specific artifact evidence and
  explicit pending system dispositions. Remote invocation authentication is a
  separate required integration layer, not supplied by this body validator.
- Single-module live transport with frozen plan/request uploads, network-disabled
  capability discovery, consumer-code hash checks, invocation/archive binding
  and local scoped-result validation. Only the matching capability can run;
  unsupported consumers stop without a legacy fallback.

Scope is a reviewed decision, never a keyword-based classification of prose.
Scope review is distinct from native architecture approval. Approving a routing
does not approve changed RTL/contracts or establish consumer compatibility.

## Source dispositions and check methods

Every source record has exactly one reviewed assignment and a nonempty reason:

| Source scope | Required checks and accepted outcome |
| --- | --- |
| `metadata` | Explicit metadata checks; no fabricated HDL comparison |
| `structural` | Explicit structural checks such as interface/ownership inspection |
| `module` | Explicit required local checks; may combine structural evidence and simulation |
| `system` | No local certifying check IDs; disposition must remain `unverified` |

Each executable check has `id`, `scope`, `method`, `description`, `source_ids`,
`contract_facets`, and an explicit `assumptions` list (possibly empty). Check
scope/method pairs are `metadata/metadata`, `structural/structural`, and
`module/simulation`. Check IDs are unique within the module plan.

`source_ids` can include global rules to document a local **contribution**.
Such a check cannot change that global source's disposition to passed. All
certifying source-to-check links must agree with the check's source references.
The local `behavior.contract` record must require the complete declared facet
set. Its `invariants`/`verification` facets establish reference integrity, not
the full truth of the referenced system behavior. These remain separate records.

The validator checks declared coverage structure, not whether arbitrary test
code truly implements its descriptions. Verification must audit the actual
comparisons, independently model expected behavior and validate assumptions.
Attaching all facet labels to a trivial comparison is not adequate evidence.

## Plan construction and portable inputs

These commands are local, do not make model calls and do not modify the mailbox:

```bash
python -m shared.top_orchestrator.verification_scope MAILBOX draft --module MODULE > routing.json
# Author/review the explicit routing; a draft cannot build an accepted plan.
python -m shared.top_orchestrator.verification_scope MAILBOX build --routing routing.json > execution_plan.json
python -m shared.top_orchestrator.verification_scope MAILBOX validate --plan-file execution_plan.json
python -m shared.top_orchestrator.verification_scope MAILBOX bundle --plan-file execution_plan.json --output NEW_BUNDLE
```

The routing schema is `capstone.module_scope_routing`, version 1. Review requires
`status=approved`, a reviewer and rationale. It binds exact `identity`,
`required_contract_facets`, and `composition_sources` from the draft. All source
IDs must be assigned; unresolved scopes prevent building the plan. The resulting
plan schema is `capstone.module_execution_plan`, version 1.

The bundle contains:

```text
scope_request.json       capstone.module_execution_request, version 1
execution_plan.json      complete authenticated sources and reviewed assignments
input/snapshot.json     unchanged native isolated-module snapshot
input/source_context.json
input/specs/MODULE.yaml
input/rtl/MODULE.sv
```

`scope_request.json` binds plan digest, plan file hash, module snapshot hash and
parent/module identity. Its input/plan paths are relative to bundle root. The
required consumer capability is **`capstone.module_execution_plan.v1`**.
The old `SPECIFICATION_CATALOG_VERSION=1` marker is not this capability.

## Live consumer discovery and invocation

The uploaded wrapper/probe use Python 3.6-compatible syntax and the standard
library. In a bounded child process, the probe disables Python socket connects
before running the deployed pipeline with `--scope-capabilities`. That endpoint
must return one JSON document on stdout, without starting agents:

```json
{
  "schema": "capstone.module_execution_capabilities",
  "schema_version": 1,
  "capabilities": ["capstone.module_execution_plan.v1"],
  "request_flag": "--execution-request",
  "results_flag": "--execution-results",
  "consumer_files": ["run_pipeline.py", "requirement_verification.py"]
}
```

The file list above illustrates the format. Declare every relevant consumer
Python source, including the actual pipeline filename and agent/helpers used by
this protocol, relative to the pipeline directory. Files must be regular `.py`
files within that directory, without symlinks, duplicates or path escapes.
The wrapper records their hashes and checks that they remain unchanged during
discovery and execution. This authenticates the declared inventory; completeness
of that declaration remains part of consumer review. External tool/dependency
versions remain separate environment evidence.

After successful discovery, agent execution uses the existing module CLI plus:

```text
PYTHON PIPELINE INPUT/rtl INPUT/specs --jobs JOBS \
  --execution-request INVOCATION/scope_request.json \
  --execution-results INPUT/verification_reports/module_execution_results.json
```

These paths are supplied as absolute arguments. The request's relative `input`
and `execution_plan.json` paths resolve from the request's parent directory.
The consumer must write its scoped result at the requested location, use paths
relative to `INPUT/verification_reports` for artifact evidence, and save exactly
one normal `*/summary.json` beneath that report root. The normal summary must
identify the submitted RTL/spec directories and report successful completion of
required Agent 1/2/3 stages **within module scope**. Deferred system sources
belong in the scoped body's pending dispositions, not fabricated module passes.

The wrapper checks unchanged inputs, sidecars and declared consumer code and
archives the invocation reports. The client verifies invocation/snapshot,
archive digest, result-file digest/body, plan/capability and parent/module
identities, source dispositions, test records and artifact hashes. The normal
pipeline summary and process exit must also pass before the scoped acceptance
records `remote_invocation_authentication=checked_by_validation_adapter`.

```bash
# Local preview: no SSH, prompts, writes or agents.
python -m shared.top_orchestrator validation --mailbox MAILBOX --module-probe MODULE --scope-plan execution_plan.json --preflight-only --plan
# User-operated SSH/Slurm discovery; no verification agents.
python -m shared.top_orchestrator validation --mailbox MAILBOX --module-probe MODULE --scope-plan execution_plan.json --preflight-only
# User-operated paid agent execution, after discovery passes.
python -m shared.top_orchestrator validation --mailbox MAILBOX --module-probe MODULE --scope-plan execution_plan.json
```

Capability discovery failure, non-JSON output, network use in the probe or
missing CLI support stops before agent execution. Preflight receipts contain no
pipeline execution or behavioral acceptance. Execution records use
`verification_scope=module_checks_only`; system/backend readiness stays false.

Plan digest uses SHA-256 over JSON excluding `plan_sha256`, with sorted keys,
separators `(',', ':')`, `ensure_ascii=true`, UTF-8 and no NaN. Use the emitted digest rather than
hashing pretty-printed plan bytes; the request separately hashes exact plan-file
bytes. Identity fields are `snapshot_sha256`, `rtl_revision`, `module_name`,
and `specification_sha256`.

## Results consumed by our validator

The result-body schema is `capstone.module_execution_results`, version 1. It
must copy exact `identity`, `plan_sha256`, and `consumer_capability`, and declare
`scope=module_checks_only`, `backend_ready=false`.

`checks` is a list covering every planned check exactly once. Each row contains:

- `id`, `status=passed`, and the exact planned `method`.
- `test_ids`: simulation IDs for runtime checks, otherwise an explicit list
  which can be empty.
- `evidence`: nonempty list of `{ "path": "relative/file", "sha256": "..." }`.

`executed_tests` is a list with unique test IDs. Each referenced simulation test
must have `method=simulation`, `status=passed`, `execution_completed=true`,
`execution_succeeded=true`, and nonempty artifact `evidence`. Its evidence must
be included in the referencing check's checked artifacts. Test IDs cannot be
unresolved labels. Artifact files must exist under the supplied evidence root;
path escapes, symlinks, missing/changed files and absent evidence are rejected.

`source_results` maps every preserved source ID to exactly:

```json
{"status": "passed", "check_ids": ["THE_ASSIGNED_REQUIRED_CHECKS"]}
```

For every deferred system source it must instead be:

```json
{"status": "unverified", "check_ids": []}
```

Missing, duplicate or invented checks/source IDs, failed/unexecuted checks,
scope/method mismatches, stale identity/digest and claimed system passes reject
local acceptance. Unsupported local work must remain incomplete; the validator
does not accept `not_applicable` as a shortcut to a module pass.

```bash
python -m shared.top_orchestrator.verification_scope MAILBOX accept --plan-file execution_plan.json --results consumer_results.json --evidence-root REPORT_DIRECTORY
```

A successful body check emits `capstone.module_scope_acceptance`, version 1,
`status=module_checks_passed`, the explicit deferred source list,
`system_verification=not_accepted`, `backend_ready=false`. It also declares that
remote invocation authentication is not evaluated by this offline validator.
This is not an existing deployed module receipt or a system readiness receipt.

## Real host-adapter handoff for the verification owner

The [frontend-reviewed example routing](examples/host_adapter_scope_routing.json)
is pinned to the existing `20261027T233850Z_47d4ee60/revision_001` publication.
It is our concrete proposed scope assignment, not a claim of verification-owner
agreement or executed checks. It preserves 30 records, defines seven checks
(one metadata, one structural, five local simulation), and defers 17 records to
system verification. The seven checks cover package identity, adapter structure,
admission, packing, response forwarding, reset gating and simultaneous/stateless
behavior. Environment assumptions explicitly identify upstream queue/memory work.

The generated real bundle is `/tmp/capstone-host-scoped-consumer-bundle-20261028`.
The original published YAML/SV were not edited. Other module/revision routings
must be authored and reviewed separately; this example is not a generic heuristic
to classify arbitrary modules or future snapshots.

Verification should implement capability discovery and consume this request
before generating tests. It must not add unrouted legacy simulation work on top
of the plan, manufacture PASS for deferred work, or guess scope from source text.
If a normative field/check cannot be routed, stop incomplete before paid repairs
and report the missing plan support. Preserve the existing audit guards.

Single-module capability negotiation, upload, invocation/archive authentication
and result-body validation are now implemented and tested against a synthetic
consumer. Actual deployed acceptance still requires the verification owner's
matching endpoint/flags and user-operated live runs. Scoped batch collection
and failed-run repair mapping remain separate work; legacy batch/repair paths
explicitly reject scoped inputs rather than dropping plan evidence or running
an unsupported batch. The older all-source simulation path remains available
without `--scope-plan`. System obligations and backend readiness remain blocked
in either path.

Validation: 12 scope-protocol tests passed; the complete orchestrator regression
suite passed 190 tests with four existing fixture-dependent skips. Tests use
explicit synthetic result evidence and do not claim real consumer execution.
The real published host-adapter bundle also passed digest-binding checks.
The live-transport regression suite subsequently passed 200 tests with four
existing skips, including ten transport tests against a synthetic scoped
consumer. This does not establish deployed capability or real DUT acceptance.
