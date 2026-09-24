"""Measured area/power limits. Unknown measurements never count as a pass."""
import math
import re

MAX_DIE_AREA_MM2 = 8.0
MAX_POWER_W = 2.0
NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
UNITS = {"W": 1.0, "mW": 1e-3, "uW": 1e-6, "nW": 1e-9, "pW": 1e-12}


def parse_total_power(text):
    """Read explicit total-power labels, preserving units and taking the worst view."""
    default_unit = re.search(r"Power\s+Units?\s*[:=]\s*(?:1\s*)?([munp]?W)\b", text, re.I)
    values = []
    for line in text.splitlines():
        match = re.fullmatch(
            rf"\s*Total\s+Power\s*(?:\(([munp]?W)\))?\s*(?:[:=]\s*|\s+)({NUMBER})\s*([munp]?W)?\s*(?:\([^)]*\))?\s*;?\s*",
            line, re.I,
        )
        if not match:
            if re.match(r"\s*Total\s+Power\b", line, re.I):
                raise ValueError("Unrecognized total-power measurement; limits cannot be verified.")
            continue
        unit = match[3] or match[1] or (default_unit[1] if default_unit else None)
        if unit not in UNITS:
            raise ValueError("Total power has missing or unsupported units.")
        value = float(match[2]) * UNITS[unit]
        if not math.isfinite(value) or value <= 0:
            raise ValueError("Total power must be a finite positive measurement.")
        values.append(value)
    if not values:
        raise ValueError("No explicit total-power measurement was found.")
    return max(values)


def evaluate_limits(area_text, power_text):
    match = re.search(rf"(?m)^die_area_mm2\s*[:=]\s*({NUMBER})\s*$", area_text)
    if not match:
        raise ValueError("Die footprint measurement is missing (cell area is not die area).")
    area = float(match[1])
    if not math.isfinite(area) or area <= 0:
        raise ValueError("Die area must be a finite positive measurement.")
    power = parse_total_power(power_text)
    return {
        "die_area_mm2": area,
        "total_power_w": power,
        "max_die_area_mm2": MAX_DIE_AREA_MM2,
        "max_power_w": MAX_POWER_W,
        "within_limits": area <= MAX_DIE_AREA_MM2 and power <= MAX_POWER_W,
        "power_basis": "Innovus report_power; activity and analysis conditions inherited from the design",
    }
