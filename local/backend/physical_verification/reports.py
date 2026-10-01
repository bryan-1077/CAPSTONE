"""Missing or ambiguous physical verification reports cannot pass."""
import re


def violation_count(text, kind):
    pattern = (r"^\s*#?\s*Total\s+Violations\s*:\s*(\d+)\s*(?:Viols?\.?|Violations?\.?)?\s*$"
               if kind == "drc" else
               r"^\s*#?\s*Total number of process antenna violations\s*[:=]\s*(\d+)\s*$")
    if kind not in {"drc", "antenna"}:
        raise ValueError("Unknown physical report kind.")
    if re.search(r"\*\*ERROR|^\s*ERROR\s*:", text, re.M | re.I):
        raise ValueError(f"{kind} verification reported a tool error.")
    counts = re.findall(pattern, text, re.M | re.I)
    if len(counts) != 1:
        raise ValueError(f"{kind} report needs exactly one explicit violation total.")
    return int(counts[0])


def analyze_physical_reports(drc, antenna):
    return {
        "drc_count": violation_count(drc, "drc"),
        "antenna_count": violation_count(antenna, "antenna"),
        "filler_conflicts": sorted(set(re.findall(r"Blockage of Cell\s+(FILL[A-Za-z0-9_]*)", drc))),
        "drc_report": drc[:24000], "antenna_report": antenna[:24000],
    }
