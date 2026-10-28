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
    counts = [int(value) for value in re.findall(pattern, text, re.M | re.I)]
    clean_pattern = (r"^\s*No DRC violations were found\s*\.?\s*$"
                     if kind == "drc" else
                     r"^\s*No Violations Found\s*\.?\s*$")
    if re.search(clean_pattern, text, re.M | re.I):
        counts.append(0)
    if not counts:
        raise ValueError(f"{kind} report contains no recognized violation total or clean result.")
    if len(set(counts)) != 1:
        raise ValueError(f"{kind} report contains conflicting violation totals or clean results.")
    return counts[0]


def analyze_physical_reports(drc, antenna):
    return {
        "drc_count": violation_count(drc, "drc"),
        "antenna_count": violation_count(antenna, "antenna"),
        "filler_conflicts": sorted(set(re.findall(r"Blockage of Cell\s+(FILL[A-Za-z0-9_]*)", drc))),
        "drc_report": drc[:24000], "antenna_report": antenna[:24000],
    }
