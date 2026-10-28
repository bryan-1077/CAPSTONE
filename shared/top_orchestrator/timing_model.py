"""Versioned timing scope matching the established expand-spec implementation.

Carry the complete definition in fresh requests so planning, review and handoff
consumers can interpret it without importing orchestrator code.
"""
from copy import deepcopy

FUNCTIONAL_TIMING = {
    "id": "project-functional-controller-v1",
    "definition": (
        "Build a single-clock functional controller model. A DDR speed/profile name "
        "selects the project's timing dictionary; use its stored timing_cycles values "
        "directly as counts of controller clock edges. This applies to minimum delays, "
        "windows, refresh intervals and refresh recovery. The dictionary's "
        "clock_frequency_mhz and timing_ns explain the origin of those stored counts; "
        "they do not request another RTL clock domain. controller_clock.frequency_mhz "
        "is the RTL implementation/backend target and does not rescale the stored "
        "counts. For example, DDR4-3200 tRCD=22 means 22 controller cycles; at 200 MHz "
        "that is 110 ns. This model does not claim physical DDR4-3200 throughput, "
        "JEDEC wall-clock timing compliance, a PHY, CDC or an 8:1 clock conversion. "
        "Do not infer a controller/PHY frequency conflict solely from the profile "
        "and implementation target. Preserve all explicit user requirements: a "
        "request for physical DDR signaling, real link throughput, additional clocks "
        "or timing in absolute time must be evaluated separately; report an unsupported "
        "requirement or scope conflict when necessary rather than silently weakening it."
    ),
}

SUMMARY = ("Timing scope: functional controller; DDR profile counts are controller cycles. "
           "Target MHz sets implementation timing; physical DDR link timing is outside this model.")


def apply_timing_model(config, *, standard=False):
    """Add the default for new controller requests, preserving explicit scopes."""
    if not isinstance(config, dict):
        raise ValueError("Timing scope requires a configuration mapping")
    result = deepcopy(config)
    declared = result.get("timing_model")
    if "timing_model" in result and (not isinstance(declared, dict)
            or not isinstance(declared.get("id"), str) or not declared["id"].strip()):
        raise ValueError("timing_model must be a mapping with a nonempty id")
    if (isinstance(declared, dict) and declared.get("id") == FUNCTIONAL_TIMING["id"]
            and declared != FUNCTIONAL_TIMING):
        raise ValueError("Functional timing model definition differs from its versioned contract")
    if standard:
        if "timing_model" in result and result["timing_model"] != FUNCTIONAL_TIMING:
            raise ValueError("Standard supports only project-functional-controller-v1 timing")
        result["timing_model"] = deepcopy(FUNCTIONAL_TIMING)
    elif result.get("design_type") == "memory_controller" or result.get("design_kind") == "memory_controller":
        result.setdefault("timing_model", deepcopy(FUNCTIONAL_TIMING))
    return result
