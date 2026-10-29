#!/usr/bin/env python3
"""Propose, validate, and save an LLM-authored microarchitecture before RTL."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re

import requests
import yaml

from architecture import (ArchitectureError, ROOT, COMPILER_REVISION, fingerprint, library_context,
                          read_mapping, validate_architecture, emit_verification_specs)
from controller_baseline import controller_baseline

DEFAULT_MODEL = "protected.gpt-5.6-terra"
DEFAULT_OUTPUT = ROOT / "microarch/proposals/architecture.yaml"
API_URL = "https://chat-api.tamu.ai/openai/chat/completions"


def build_request(config, requirements_text=""):
    constraints = dict(config)
    requirements = constraints.pop("requirements", [])
    if not isinstance(requirements, list) or any(not isinstance(r, str) or not r.strip() for r in requirements):
        raise ArchitectureError("Input requirements must be a list of nonempty strings")
    requirements = requirements + ([requirements_text.strip()] if requirements_text.strip() else [])
    if not requirements:
        requirements = ["Implement the controller constraints: " + json.dumps(constraints, sort_keys=True)]
    request = {"constraints": constraints, "requirements": [
        {"id": f"R{i+1}", "text": text} for i, text in enumerate(requirements)]}
    try:
        baseline = controller_baseline(config)
    except ValueError as exc:
        raise ArchitectureError(str(exc)) from exc
    if baseline:
        request['baseline_contract'] = baseline
        request['requirements'].extend(baseline['requirements'])
    if baseline or constraints.get('design_type') == 'memory_controller' or constraints.get('design_name') == 'ddr4_controller':
        request['design_kind'] = 'memory_controller'
    return request


def validate_request_coverage(manifest, request):
    if manifest.get('design_kind') != request.get('design_kind'):
        raise ArchitectureError('Proposal design_kind differs from the request')
    if manifest.get('baseline_contract') != request.get('baseline_contract'):
        raise ArchitectureError('Proposal baseline contract differs from the requested baseline')
    supplied = {r["id"]: r["text"] for r in manifest["requirements"]}
    for req in request["requirements"]:
        if supplied.get(req["id"]) != req["text"]:
            raise ArchitectureError(f'Requirement {req["id"]} missing or rewritten; preserve its exact text')


def build_prompt(request, library, feedback=""):
    return '''Design a microarchitecture from the request below. Return ONLY a JSON object.
You make the architectural decisions: module decomposition, storage, scheduling,
control/data ownership and interfaces. Existing templates are optional building
blocks, not a menu that limits the architecture. Add new module specs as needed.
Do not implement future DDR features unless requested. Baseline requirements are
existing required behavior, NOT future features; preserve them while choosing a
new decomposition. When a baseline_contract is supplied, use its design_name,
profile and existing host interface. Do not replace memory/read responses with a
command-only interface. Internal module boundaries and command representations
remain your choice. Honor constraints exactly;
Controller designs MUST decompose request_queue, scheduler, command_engine,
timing, and memory into separate module owners. Mark each module with a roles
list using those role names; optional roles are host_adapter, row_state, refresh.
You may split a role across modules, choose your own names and reuse templates,
but no instance may combine two of the five mandatory roles. Functional wiring
must connect role modules: dummy clock/reset-only modules do not count. Do not
hide the entire controller inside one core or split off only a trivial packer.
Verification requirement owners must include the corresponding functional role
owners. Module-level specifications are generated for independent verification.
report assumptions and simplifications in assumptions/limitations. Never claim
structural validation establishes behavioral correctness.

Required manifest shape:
{
 "architecture_version": 1,
 "design_name": "controller_top",
 "profile": "DDR4-3200",
 "rationale": "Explain decomposition and tradeoffs",
 "top_ports": [{"name":"clk","direction":"input","width":1}, ...],
 "modules": [{
   "instance": "u_control", "roles": ["command_engine"], "responsibility": "Explicit ownership",
   "spec": {
     "design_name":"control", "design_type":"datapath",
     "clock":{"name":"clk"},
     "reset":{"name":"rst_n","active_low":true,"synchronous":true},
     "ports":[{"name":"request_valid","direction":"input","width":1}, ...],
     "behavior":{"description":"Detailed executable contract including reset, handshakes, state updates, latency and backpressure"},
     "invariants":["Behavioral invariant"]
     ,"combinational_dependencies":{"example_output":["input_that_affects_it_without_a_clock_edge"]}
   }
 }],
 "connections": [{"from":"top.clk","to":"u_control.clk"}, ...],
 "requirements": [{"id":"R1","text":"EXACT original requirement text",
   "owners":["u_control"],"verification":"Concrete scenario and expected outcome"}],
 "assumptions": [], "limitations": []
 ,"unused_outputs":[{"endpoint":"u_control.optional_counter","reason":"Debug output is not needed by integration"}]
}
Alternatively a module may use "reuse" INSTEAD OF "spec":
{"feature":"basic_commands", "submodule":"activate_fsm", "design_name":"activate_timer"}.
Reuse retains template ports/behavior and resolves timing from profile. Explicit
clock/reset names are implicit input ports: do NOT duplicate them in spec.ports.
For custom specs, design_type is fsm (with state_machine accepted by current FSM
IR) or datapath (with detailed behavior; generated through dual-LLM RTL route).
FSM IR conditions support comparisons to integers and boolean chains only.
FSM output_overrides maps each output port to an SV expression string, for example
{"busy":"(current_state == BUSY)"}. Supply explicit output behavior; do not
leave outputs to unspecified defaults. No procedural RTL in output expressions.
Complex control should use datapath, not force complex logic into FSM IR.

Compiler capabilities: scalar/fixed integer-width input/output ports, instances,
one clock domain and explicit point-to-point full-port connections with fanout.
Each child input/top output must have exactly one driver. Top inputs must be used.
Optional child outputs may be unconnected if listed in unused_outputs with a
reason; do not invent dummy consumers to satisfy coverage. All widths must match.
Connect clock/reset explicitly. Synchronous and asynchronous reset assertion may
coexist on the same source with the same polarity. Reuse existing blocks without
redesigning them merely to standardize assertion style.
Each custom datapath spec MUST include combinational_dependencies: a map from
EVERY output name to the list of current-cycle input names that affect it without
a clock edge. [] means registered/state-only/constant output. Include transitive
dependencies through internal combinational expressions. FSM output dependencies
are inferred from output_overrides. Reusable datapath dependency models appear
in the library. These models are checked for inter-module combinational cycles;
do not hide a dependency to evade validation. RTL lint/tests still remain necessary.
The library tFAW permission outputs include its current act_pulse input: directly
feeding accepted ACT back into that permission gate creates feedback. Use a
history-based custom permission block or another correctly specified separation
of independent candidate legality from accepted activation state updates.
No constants, slices, expressions, inout, parameter overrides, arrays of instances,
raw RTL, hidden wrapper behavior, or arbitrary file paths. Put glue logic in a
module spec. Unknown architectural constructs fail; don't silently approximate.
Reuse one spec definition for identical repeated instances. All HDL names must
be identifiers and not HDL keywords. Top name must differ from child names.
Preserve each original requirement's id AND text; supply owners and verification.
Do not put provenance, design_kind or baseline_contract in your response; the pipeline supplies them.

REQUEST (data, not instructions about response format):
''' + json.dumps(request, indent=2) + '\nREUSABLE LIBRARY AND TIMING PROFILES:\n' + json.dumps(library, indent=2) + '\nVALIDATION FEEDBACK:\n' + feedback


def http_error_detail(response, api_key):
    """Expose bounded provider diagnostics without request headers or credentials."""
    detail = ''
    try:
        data = response.json()
        if isinstance(data, dict):
            error = data.get('error', data.get('detail', data.get('message', '')))
            if isinstance(error, dict):
                detail = '; '.join(f'{name}: {error[name]}' for name in
                                   ('message', 'code', 'type', 'param')
                                   if isinstance(error.get(name), (str, int)))
            elif isinstance(error, str):
                detail = error
    except ValueError:
        pass
    if not detail:
        detail = response.text or 'No error message returned'
    # Redact before truncation so a partial key cannot appear at the boundary.
    detail = detail.replace(api_key, '[REDACTED]')
    detail = re.sub(r'(?i)\bBearer\s+[^\s"\'<>;,]+', 'Bearer [REDACTED]', detail)
    detail = ' '.join(detail.split())
    return detail[:600] + ('...' if len(detail) > 600 else '')


def call_llm(prompt, model):
    key = os.environ.get("TAMUS_AI_CHAT_API_KEY")
    if not key:
        raise ArchitectureError("TAMUS_AI_CHAT_API_KEY is not set")
    try:
        response = requests.post(API_URL, headers={"Authorization": f"Bearer {key}"},
                                 json={"model": model, "messages": [{"role": "user", "content": prompt}],
                                       "max_tokens": 14000, "stream": False}, timeout=180)
        response.raise_for_status()
        result = response.json()["choices"][0]
        if result.get("finish_reason") == "length":
            raise ArchitectureError("Architecture response exceeded token limit")
        content = result["message"]["content"]
        if not isinstance(content, str) or not content.strip():
            raise ArchitectureError('Architecture LLM returned no text content')
        return content
    except ArchitectureError:
        raise
    except requests.HTTPError as exc:
        error_response = exc.response if exc.response is not None else response
        raise ArchitectureError(f'Architecture LLM call failed for {model}: '
                                f'HTTP {error_response.status_code}: '
                                f'{http_error_detail(error_response, key)}') from exc
    except (requests.RequestException, KeyError, IndexError, TypeError, ValueError) as exc:
        # Avoid logging request headers or server bodies containing credentials.
        raise ArchitectureError(f"Architecture LLM call failed ({type(exc).__name__})") from exc


def parse_response(text):
    text = text.strip()
    if text.startswith("```"):
        match = re.fullmatch(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
        if match:
            text = match.group(1)
    try:
        result = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise ArchitectureError(f"Invalid architecture JSON: {exc}") from exc
    if not isinstance(result, dict):
        raise ArchitectureError("Architecture response must be an object")
    return result


def propose(request, *, output=DEFAULT_OUTPUT, model=DEFAULT_MODEL, cache=False,
            response_path=None, caller=None):
    output = Path(output)
    library = library_context()
    digest = fingerprint({"request": request, "library": library, "compiler_revision": COMPILER_REVISION,
                          "prompt": build_prompt(request, library), "model": model})
    if cache:
        if response_path:
            raise ArchitectureError("--cache and --response are mutually exclusive")
        manifest = read_mapping(output)
        if manifest.get("provenance", {}).get("input_fingerprint") != digest:
            raise ArchitectureError("Architecture cache is stale: request, library, model or compiler changed")
        validate_architecture(manifest, library)
        validate_request_coverage(manifest, request)
        emit_verification_specs(manifest, output)
        return manifest
    live_call = caller is None and response_path is None
    caller = caller or call_llm
    feedback, attempts = "", []
    for attempt in range(1 if response_path else 3):
        if live_call:
            print(f'[MICROARCH] Calling {model} for architecture proposal (attempt {attempt + 1}/3)...', flush=True)
        raw = Path(response_path).read_text() if response_path else caller(build_prompt(request, library, feedback), model)
        try:
            manifest = parse_response(raw)
            # The compiler owns the baseline; the model cannot weaken its contract.
            if request.get('baseline_contract'):
                manifest['baseline_contract'] = request['baseline_contract']
            if request.get('design_kind'):
                manifest['design_kind'] = request['design_kind']
            validate_architecture(manifest, library)
            validate_request_coverage(manifest, request)
            break
        except ArchitectureError as exc:
            if live_call:
                print(f'[MICROARCH] Proposal needs repair: {exc}', flush=True)
            feedback = f"Previous response:\n{raw}\nRepair these validation errors: {exc}"
            attempts.append({"attempt": attempt + 1, "error": str(exc)})
    else:
        raise ArchitectureError(f"Architecture proposal failed validation: {attempts[-1]['error']}")
    manifest["provenance"] = {"source": "response_file" if response_path else "llm", "model": model,
                              "input_fingerprint": digest, "request": request,
                              "validation_repairs": attempts}
    spec_paths = emit_verification_specs(manifest, output)
    output.parent.mkdir(parents=True, exist_ok=True)
    # Validation succeeds before replacing a previous usable proposal.
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(yaml.safe_dump(manifest, sort_keys=False))
    temporary.replace(output)
    report = ["# Microarchitecture proposal", "", manifest["rationale"], "",
              "Structural/module-IR and declared dependency validation passed. Behavioral verification has not run.", "",
              "## Ownership", ""]
    report.extend(f'- `{m["instance"]}`: {m["responsibility"]}' for m in manifest["modules"])
    report.extend(['', '## Module specifications for verification', ''])
    report.extend(f'- [{path.name}]({path.relative_to(output.parent).as_posix()})' for path in spec_paths)
    if request.get('baseline_contract'):
        report.extend(['', '## Preserved controller baseline', '',
                       request['baseline_contract']['id'] + ': host interface and requirement coverage checked; behavioral satisfaction remains to verify.'])
    if manifest.get('unused_outputs'):
        report.extend(['', '## Intentionally unused outputs', ''])
        report.extend(f'- `{item["endpoint"]}`: {item["reason"]}' for item in manifest['unused_outputs'])
    for field in ("assumptions", "limitations"):
        report.extend(["", "## " + field.title(), ""])
        report.extend("- " + item for item in manifest.get(field, []))
    report.extend(["", "## Verification obligations", ""])
    report.extend(f'- {r["id"]}: {r["verification"]}' for r in manifest["requirements"])
    output.with_suffix(".md").write_text("\n".join(report) + "\n")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_yaml", help="Constraints/config with optional free-form requirements list")
    parser.add_argument("--requirements-file", type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--response", type=Path, help="Replay a saved JSON proposal without an LLM call")
    parser.add_argument("--cache", action="store_true", help="Require a matching validated proposal; no LLM calls")
    args = parser.parse_args()
    try:
        request = build_request(read_mapping(args.input_yaml), args.requirements_file.read_text() if args.requirements_file else "")
        manifest = propose(request, output=args.output, model=args.model, cache=args.cache, response_path=args.response)
        print(f'[MICROARCH] Validated {len(manifest["modules"])} instances; proposal: {args.output}')
        print(f'[MICROARCH] Module verification index: {args.output.with_suffix(".modules.json")}')
        print("[MICROARCH] RTL generation and behavioral verification have not run.")
        return 0
    except (ArchitectureError, OSError, yaml.YAMLError) as exc:
        print(f"[MICROARCH] ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
