"""Bounded hold experiments when an AI decision overlooks legal data sinks."""
import json
import math
import re

from agents.openai_timing_closure import validate_plan


def plan_signature(source_outdir, actions):
    return json.dumps([source_outdir, sorted(
        json.dumps({key: value for key, value in action.items() if key != "reason"},
                   sort_keys=True)
        for action in actions
    )])


def legal_hold_targets(hold_report, buffers):
    """Intersect negative-slack endpoints with the executor's verified choices."""
    targets = {}
    for path in hold_report.paths:
        if (path.slack is None or not math.isfinite(path.slack) or path.slack >= 0
                or path.endpoint not in buffers):
            continue
        pin = path.endpoint
        choice = buffers[pin]
        if (not re.fullmatch(r"[A-Za-z0-9_./\[\]-]+/D", pin)
                or not re.fullmatch(r"sky130_fd_sc_hd__df\w+_\d+", choice.get("from_cell", ""))):
            continue
        cells = sorted({cell for cell in choice.get("allowed_cells", [])
                        if re.fullmatch(r"sky130_fd_sc_hd__buf_\d+", cell)},
                       key=lambda cell: (int(cell.rsplit("_", 1)[1]), cell))
        if cells:
            targets[pin] = {"slack_ns": min(path.slack, targets.get(pin, {}).get("slack_ns", 0)),
                            "cells": cells}
    return dict(sorted(targets.items(), key=lambda item: (item[1]["slack_ns"], item[0])))


def fallback_hold_plan(targets, buffers, source_outdir, tried_plans):
    """Try at most four sinks per experiment; never repeat a measured plan."""
    items = list(targets.items())
    for offset in range(0, len(items), 4):
        batch = items[offset:offset + 4]
        for size_index in range(max(len(target["cells"]) for _, target in batch)):
            actions = [{
                "operation": "insert_buffer", "pin": pin,
                "cell": target["cells"][min(size_index, len(target["cells"]) - 1)],
                "reason": f"Measured hold slack {target['slack_ns']:.3f} ns at {pin}; add local data delay.",
            } for pin, target in batch]
            if plan_signature(source_outdir, actions) in tried_plans:
                continue
            return validate_plan({
                "summary": (
                    f"Deterministic hold fallback from {source_outdir}: the AI returned no actions, "
                    f"but {len(targets)} failing hold endpoints have verified buffer choices. "
                    f"Try non-inverting buffers at {len(actions)} data sinks. "
                    "This is an experiment, not a guarantee of closure; remeasure setup, hold, "
                    "DRC, antenna, area, and power before accepting the result."
                ),
                "actions": actions,
            }, {}, buffers)
    return None
