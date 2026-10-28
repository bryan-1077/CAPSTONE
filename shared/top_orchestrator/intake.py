"""Lossless request intake shared by Standard and Experimental frontends.

Interpretation, approval and generation belong to adapters. Clock advice makes
a model call only when explicitly requested with 'discuss'.
"""
import json
from pathlib import Path

from .frequency import DEFAULT_TARGET_MHZ, positive_mhz, ask_advisor
from .timing_model import FUNCTIONAL_TIMING, SUMMARY

FLOW_LABELS = {"expand-spec": "Standard", "specification": "Experimental"}


def collect_intake(flow: str | None, source: Path | None, target_mhz: float | None,
                   output: Path, *, allow_advisor=True) -> Path:
    """Confirm and save a request; optional clock advice requires explicit opt-in."""
    if flow is None:
        print("Choose a frontend:\n  1. Standard — configure supported controller features."
              "\n  2. Experimental — generate a specification and microarchitecture.")
        while True:
            choice = input("Frontend [1]: ").strip().lower()
            if choice in ("", "1", "standard", "expand-spec"):
                flow = "expand-spec"
                break
            if choice in ("2", "experimental", "specification"):
                flow = "specification"
                break
            print("Choose Standard (1) or Experimental (2).")
    if flow not in FLOW_LABELS:
        raise ValueError("Unsupported frontend flow")
    if output.exists():
        raise FileExistsError(f"Intake output already exists: {output}")
    if source is not None:
        source = source.expanduser().resolve()
        description = source.read_bytes().decode("utf-8")
    else:
        if flow == "expand-spec":
            print("Describe a DDR4 controller: speed 2400/3200; banks 1/2/4; scheduler"
                  " simple/round_robin; open_page/close_page; refresh, tFAW, tRRD."
                  " Existing slowest/medium/fastest presets apply during interpretation.")
        else:
            print("Describe what you want to build and any requirements that matter."
                  " Leave implementation choices open; planning will propose them for review."
                  " Optional details: interfaces, buffering, ordering, reset and timing.")
        print("Enter multiple lines; finish with a line containing only a period (.).")
        lines = []
        while True:
            try:
                line = input("> " if not lines else "... ")
            except EOFError:
                break
            if line == ".":
                break
            lines.append(line)
        description = "\n".join(lines)
    if not description.strip():
        raise ValueError("Describe the design before preparing a request")
    frequency_source = "command line" if target_mhz is not None else "user"
    conversation = []
    if target_mhz is None:
        while True:
            value = input(f"Target controller MHz [{DEFAULT_TARGET_MHZ:g}]"
                          + (", or 'discuss' (model call)" if allow_advisor else "") + ": ").strip()
            if value.lower() == "discuss" and allow_advisor:
                conversation.append({"role": "user", "content":
                                     "Help me choose a controller clock for this request."})
                try:
                    advice = ask_advisor(json.dumps({"original_description": description}), conversation)
                    conversation.append({"role": "assistant", "content": advice})
                    print(advice)
                except ValueError as exc:
                    print(exc)
                continue
            try:
                target_mhz = positive_mhz(value or str(DEFAULT_TARGET_MHZ))
                frequency_source = "user" if value else "configured default"
                break
            except ValueError as exc:
                print(exc)
    else:
        target_mhz = positive_mhz(str(target_mhz))
    record = {
        "schema_version": 1, "frontend_flow": flow,
        "frontend_label": FLOW_LABELS[flow], "original_text": description,
        "source_files": [str(source)] if source else [],
        "target_mhz": target_mhz, "target_mhz_source": frequency_source,
        "status": "confirmed_request", "design_approved": False,
        "frequency_conversation": conversation,
        "timing_model": dict(FUNCTIONAL_TIMING),
    }
    print(f"\n{FLOW_LABELS[flow]} ({flow}) request:\n{description}")
    print(f"Target controller clock: {target_mhz:g} MHz (not DDR transfer rate).")
    print(SUMMARY)
    print("This confirms the submitted request; design review and approval occur later.")
    answer = input("Save this request? [y/N, e to edit]: ").strip().lower()
    if answer in ("e", "edit"):
        return collect_intake(flow, None, None, output, allow_advisor=allow_advisor)
    if answer not in ("y", "yes"):
        raise ValueError("Request not confirmed; no intake written")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(record, indent=2, ensure_ascii=False) + "\n")
    return output
