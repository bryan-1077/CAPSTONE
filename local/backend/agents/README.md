# Agents Overview

This directory holds the OpenAI-backed review and edit agents used by the EDA flow.

- `openai_prep_review.py`: Runs the optional AI-assisted prep-stage review and merges that feedback with deterministic prep checks.
- `openai_prep_edit.py`: Builds prep-stage edit plans for bounded fixes to generated prep artifacts such as filelists, Tcl, SDC, metadata, and approved synthesis-top adaptations.
- `openai_netlist_review.py`: Runs the optional AI-assisted netlist-stage review and merges it with deterministic Genus/log classification results.
- `openai_netlist_edit.py`: Builds netlist-stage edit plans for bounded synthesis-oriented fixes when ownership remains in the netlist stage.
- `openai_rtl_failure_triage.py`: Recommends returning RTL to frontend when evidence shows poor or inefficient logic design (`poor_logic_design`). Keeps timing, congestion, routing, and SDC issues with backend unless the evidence identifies an RTL design weakness. Lint, syntax, generic code, and FSM lockup diagnostics alone do not trigger frontend handoff.
- `openai_mapped_review.py`: Runs the optional AI-assisted mapped-stage review and merges it with deterministic mapped log and artifact checks.
- `openai_mapped_edit.py`: Builds mapped-stage edit plans for bounded mapped-flow fixes when ownership remains in the mapped stage.
- `openai_gdsii_review.py`: Runs the optional AI-assisted GDSII-stage review and merges it with deterministic GDSII log and artifact checks.
- `openai_gdsii_edit.py`: Builds GDSII-stage edit plans for bounded export-flow fixes when ownership remains in the GDSII stage.
- `openai_timing_closure.py`: Chooses targeted gate/buffer resizes after timing presets fail. Uses the best eligible checkpoint, available cells, and measured experiment history, with fixed 4 mm² die-area and 2 W total-power limits. Pass/fail remains enforced by fresh tool reports.

Notes:

- Reviewers judge pass/fail and classify issues. Deterministic review always runs first.
- Editors only propose or apply bounded changes; they do not decide ownership.
- General AI review and edit are optional and additive. If their backend is unavailable, over budget, or returns non-JSON output, the flow falls back to deterministic behavior. Timing recovery is different: if its AI planner is unavailable or invalid, closure stops with an explicit error and preserves the best measured checkpoint.
