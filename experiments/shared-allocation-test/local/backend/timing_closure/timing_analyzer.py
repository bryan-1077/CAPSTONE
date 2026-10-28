#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gzip
import json
import re
import tarfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable


FLOAT_RE = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)"
DEFAULT_SWEEP_PERIODS = [5.000, 4.762, 4.700, 4.650]
DEFAULT_SWEEP_UTILIZATIONS = [0.50, 0.55, 0.60]
DEFAULT_SWEEP_CCOPT_SKEWS = [0.25, 0.18, 0.12]
DEFAULT_SWEEP_CCOPT_MAX_TRANSITIONS = [0.50, 0.35, 0.25]


@dataclass
class TimingPath:
    source_file: str
    startpoint: str = ""
    endpoint: str = ""
    path_group: str = ""
    required_time: float | None = None
    arrival_time: float | None = None
    slack: float | None = None
    clock_period: float | None = None
    dominant_cells: list[str] = field(default_factory=list)
    dominant_nets: list[str] = field(default_factory=list)
    category: str = "unknown"
    category_reasons: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)


@dataclass
class TimingReport:
    source_file: str
    tool: str = "unknown"
    wns: float | None = None
    tns: float | None = None
    violating_path_count: int | None = None
    clock_period: float | None = None
    paths: list[TimingPath] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        item = str(value).strip()
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _read_text(path: Path) -> str:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    return path.read_text(encoding="utf-8", errors="replace")


def _read_report_members(path: Path) -> list[tuple[str, str]]:
    if path.name.endswith(".tarpt.gz") or path.name.endswith(".tar.gz"):
        members: list[tuple[str, str]] = []
        try:
            with tarfile.open(path, "r:gz") as tf:
                for member in tf.getmembers():
                    if not member.isfile():
                        continue
                    extracted = tf.extractfile(member)
                    if extracted is None:
                        continue
                    raw = extracted.read()
                    members.append(
                        (
                            f"{path}:{member.name}",
                            raw.decode("utf-8", errors="replace"),
                        )
                    )
            return members
        except tarfile.TarError:
            return [(str(path), _read_text(path))]
    return [(str(path), _read_text(path))]


def _parse_float(text: str | None) -> float | None:
    if text is None:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _first_float(patterns: list[str], text: str) -> float | None:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
        if match:
            value = _parse_float(match.group("value"))
            if value is not None:
                return value
    return None


def _detect_tool(text: str, source_name: str) -> str:
    haystack = f"{source_name}\n{text[:8000]}".lower()
    if "innovus" in haystack or "nanoroute" in haystack or "postcts" in haystack:
        return "innovus"
    if "genus" in haystack or "synthesis solution" in haystack or "syn_map" in haystack:
        return "genus"
    return "unknown"


def _extract_clock_period(text: str) -> float | None:
    return _first_float(
        [
            rf"\bcreate_clock\b[^\n;]*\b-period\s+(?P<value>{FLOAT_RE})",
            rf"\bclock\s+period\s*[:=]\s*(?P<value>{FLOAT_RE})",
            rf"\(clock\s+\S+\)\s+capture\s+(?P<value>{FLOAT_RE})\b",
            rf"\bphase\s+shift\s+(?P<value>{FLOAT_RE})\b",
            rf"\brequired\s+time\s*[:=]?\s*(?P<value>{FLOAT_RE})",
        ],
        text,
    )


def _extract_wns(text: str) -> float | None:
    values: list[float] = []
    for pattern in [
        rf"\bWNS(?:\s*\(ns\))?\s*(?:Slack)?\s*[:=]?\s*\|?\s*(?P<value>{FLOAT_RE})",
        rf"^\s*\|\s*(?P<value>{FLOAT_RE})\s*\|\s*(?:{FLOAT_RE})\s*\|.*Pathgroup",
    ]:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE | re.MULTILINE):
            value = _parse_float(match.group("value"))
            if value is not None:
                values.append(value)
    slacks = _extract_slack_values(text)
    if slacks:
        values.append(min(slacks))
    return min(values) if values else None


def _extract_tns(text: str) -> float | None:
    return _first_float(
        [
            rf"\bTNS(?:\s*\(ns\))?\s*(?:Slack)?\s*[:=]?\s*\|?\s*(?P<value>{FLOAT_RE})",
            rf"^\s*\|\s*{FLOAT_RE}\s*\|\s*(?P<value>{FLOAT_RE})\s*\|.*Pathgroup",
        ],
        text,
    )


def _extract_violating_path_count(text: str) -> int | None:
    patterns = [
        r"\b(?:violating|violated)\s+paths?\s*[:=]\s*(?P<count>\d+)",
        r"\bnum(?:ber)?\s+of\s+violating\s+paths?\s*[:=]\s*(?P<count>\d+)",
        r"\bpaths?\s+violated\s*[:=]\s*(?P<count>\d+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return int(match.group("count"))
    negative_slacks = [value for value in _extract_slack_values(text) if value < 0]
    return len(negative_slacks) if negative_slacks else None


def _extract_slack_values(text: str) -> list[float]:
    values: list[float] = []
    patterns = [
        rf"\bslack(?:\s+time|\s*\([^)]+\))?\s*[:=]?\s*(?P<value>{FLOAT_RE})",
        rf"^\s*(?P<value>{FLOAT_RE})\s+slack\b",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE | re.MULTILINE):
            value = _parse_float(match.group("value"))
            if value is not None:
                values.append(value)
    return values


def _path_section_starts(text: str) -> list[int]:
    path_headers = [match.start() for match in re.finditer(r"(?im)^\s*Path\s+\d+\b", text)]
    if path_headers:
        return path_headers
    return [
        match.start()
        for match in re.finditer(r"(?im)^\s*(?:Start-?point|Startpoint|Beginpoint)\s*[:：]", text)
    ]


def _split_path_sections(text: str, max_paths: int) -> list[str]:
    starts = _path_section_starts(text)
    if not starts:
        return [text[:40000]]
    sections: list[str] = []
    for index, start in enumerate(starts[:max_paths]):
        end = starts[index + 1] if index + 1 < len(starts) else len(text)
        sections.append(text[start:end])
    return sections


def _extract_endpoint(patterns: list[str], section: str) -> str:
    for pattern in patterns:
        match = re.search(pattern, section, flags=re.IGNORECASE | re.MULTILINE)
        if match:
            return match.group("value").strip()
    return ""


def _clean_pin_name(value: str) -> str:
    cleaned = str(value).strip()
    cleaned = re.sub(
        r"\s+\([^)]+\)\s+(?:checked|triggered)\b.*$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"\s+(?:checked|triggered)\s+by\b.*$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    return cleaned.strip()


def _extract_cells(section: str) -> list[str]:
    cells: list[str] = []
    for match in re.finditer(
        r"\b((?:sky130|saed|nangate|tsmc|gf|asap7|sc)[A-Za-z0-9_.$/-]*__(?:[A-Za-z0-9_.$/-]+)|unmapped_[A-Za-z0-9_.$/-]+)\b",
        section,
        flags=re.IGNORECASE,
    ):
        cells.append(match.group(1))
    for match in re.finditer(r"\)\s+([A-Za-z0-9_.$/-]*(?:mux|aoi|oai|and|or|xor|xnor|nand|nor|buf|inv|df|dff)[A-Za-z0-9_.$/-]*)\s+", section, flags=re.IGNORECASE):
        cells.append(match.group(1))
    return _unique(cells)[:20]


def _extract_nets(section: str) -> list[str]:
    nets: list[str] = []
    for raw_line in section.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("-", "=", "*", "#", "|")):
            continue
        if re.search(r"\b(?:net|wire|fanout|cap|load|length)\b", line, flags=re.IGNORECASE):
            token = re.split(r"\s+", line)[0].strip()
            if "/" in token or "[" in token or "_" in token:
                nets.append(token)
    return _unique(nets)[:20]


def _classify_path(path: TimingPath, section: str) -> None:
    text = "\n".join(
        [
            section,
            path.startpoint,
            path.endpoint,
            " ".join(path.dominant_cells),
            " ".join(path.dominant_nets),
        ]
    ).lower()
    reasons: list[str] = []

    def has(pattern: str) -> bool:
        return bool(re.search(pattern, text, flags=re.IGNORECASE))

    if has(r"\b(input|output|inout)\b|(?:^|[/_.])(?:pad|gpio|io|iobuf)(?:[/_.]|$)") and (
        "input" in path.startpoint.lower()
        or "output" in path.endpoint.lower()
        or has(r"\bpath\s+group\b.*(?:in2reg|reg2out|in2out|io)")
    ):
        path.category = "IO timing path"
        reasons.append("Path appears to cross an IO timing group or top-level port.")
    elif has(r"skew|clock\s+latency|source\s+insertion|capture\s+insertion") and has(
        r"clock.*(?:domin|uncertainty)|skew\s*(?:>|:|-)?\s*(?:0\.[2-9]|\d)"
    ):
        path.category = "clock skew dominated path"
        reasons.append("Clock skew/latency terms appear prominent in the path section.")
    elif has(r"wire|net\s+delay|route|interconnect|load|capacitance|fanout") and has(
        r"(?:net|wire).*(?:domin|delay)|(?:high|large).*(?:cap|load|fanout)|route"
    ):
        path.category = "wire/load dominated routed path"
        reasons.append("Interconnect/load wording suggests routed delay dominates.")
    elif has(r"fanout|hfnet|high[-\s]?fanout") and has(r"enable|reset|rst|valid|ready|select|sel|control|clk_en"):
        path.category = "high-fanout control path"
        reasons.append("Control-like net names appear with fanout diagnostics.")
    elif has(r"\$countones|countones|popcount|population|wallace|csa|adder|add_|sum|carry|counter"):
        path.category = "counter/adduction tree"
        reasons.append("Path contains counter/addition/popcount-style logic.")
    elif has(r"\beq\b|neq|xnor|compare|comparator|==|!=|greater|less|gte|lte"):
        path.category = "comparator/equality tree"
        reasons.append("Path contains equality/comparator-style logic.")
    elif (
        has(r"mem|memory|ram|array") and has(r"mux|select|sel|addr|read|rdata|q_reg")
    ) or (
        has(r"addr") and has(r"rdata|read") and has(r"q_reg|reg\[")
    ):
        path.category = "synthesized memory/flop-array read mux"
        reasons.append("Memory/address/read-data names indicate a synthesized array read mux.")
    elif has(r"mux|select|sel|case|priority|onehot|arb|grant"):
        path.category = "wide mux/select tree"
        reasons.append("Mux/select/arbiter names dominate the path.")
    else:
        path.category = "unknown"
        reasons.append("No reusable timing-closure category matched with high confidence.")

    path.category_reasons = reasons


def _parse_path(section: str, source_name: str, fallback_period: float | None) -> TimingPath:
    path = TimingPath(source_file=source_name)
    path.startpoint = _extract_endpoint(
        [
            r"(?im)^\s*Start-?point\s*[:：]\s*(?P<value>.+?)\s*$",
            r"(?im)^\s*Start Point\s*[:：]\s*(?P<value>.+?)\s*$",
            r"(?im)^\s*Beginpoint\s*[:：]\s*(?P<value>.+?)\s*$",
        ],
        section,
    )
    path.startpoint = _clean_pin_name(path.startpoint)
    path.endpoint = _extract_endpoint(
        [
            r"(?im)^\s*End-?point\s*[:：]\s*(?P<value>.+?)\s*$",
            r"(?im)^\s*End Point\s*[:：]\s*(?P<value>.+?)\s*$",
        ],
        section,
    )
    path.endpoint = _clean_pin_name(path.endpoint)
    path.path_group = _extract_endpoint(
        [
            r"(?im)^\s*(?:Path\s+Groups?|path_group|Cost\s+Group)\s*[:：]\s*'?(?P<value>[^'\n]+)'?",
            r"(?im)\bpath_group\s+'?(?P<value>[^')\n]+)'?",
        ],
        section,
    ).strip("{} ")
    path.required_time = _first_float(
        [
            rf"\bdata\s+required\s+time\s+(?P<value>{FLOAT_RE})",
            rf"\brequired\s+time\s*[:=]?\s*(?P<value>{FLOAT_RE})",
        ],
        section,
    )
    path.arrival_time = _first_float(
        [
            rf"\bdata\s+arrival\s+time\s+(?P<value>{FLOAT_RE})",
            rf"\barrival\s+time\s*[:=]?\s*(?P<value>{FLOAT_RE})",
        ],
        section,
    )
    path.slack = _first_float(
        [
            rf"\bslack(?:\s+time|\s*\([^)]+\))?\s*[:=]?\s*(?P<value>{FLOAT_RE})",
            rf"^\s*(?P<value>{FLOAT_RE})\s+slack\b",
        ],
        section,
    )
    path.clock_period = _extract_clock_period(section) or fallback_period
    path.dominant_cells = _extract_cells(section)
    path.dominant_nets = _extract_nets(section)
    path.evidence = _unique(
        [
            line.strip()
            for line in section.splitlines()
            if re.search(
                r"service_addr|rsp_rdata|countones|WALLACE|mux|fanout|skew|slack|arrival|required|route|cap|load",
                line,
                flags=re.IGNORECASE,
            )
        ]
    )[:16]
    _classify_path(path, section)
    return path


def parse_timing_report(path: Path, *, max_paths: int = 10) -> TimingReport:
    members = _read_report_members(path)
    report = TimingReport(source_file=str(path))
    all_text = "\n".join(text for _, text in members)
    report.tool = _detect_tool(all_text, str(path))
    report.wns = _extract_wns(all_text)
    report.tns = _extract_tns(all_text)
    report.violating_path_count = _extract_violating_path_count(all_text)
    report.clock_period = _extract_clock_period(all_text)

    paths: list[TimingPath] = []
    for source_name, text in members:
        for section in _split_path_sections(text, max_paths=max_paths):
            parsed = _parse_path(section, source_name, report.clock_period)
            if parsed.startpoint or parsed.endpoint or parsed.slack is not None:
                paths.append(parsed)

    report.paths = sorted(
        paths,
        key=lambda item: item.slack if item.slack is not None else float("inf"),
    )[:max_paths]
    if report.wns is None and report.paths and report.paths[0].slack is not None:
        report.wns = report.paths[0].slack
    if report.violating_path_count is None and report.paths:
        report.violating_path_count = len(
            [path for path in report.paths if path.slack is not None and path.slack < 0]
        )
    if report.clock_period is None:
        periods = [path.clock_period for path in report.paths if path.clock_period is not None]
        report.clock_period = periods[0] if periods else None
    if not report.paths:
        report.warnings.append("No timing paths were parsed from this report.")
    return report


def _recommend_for_category(category: str) -> tuple[str, list[str], list[str]]:
    if category == "synthesized memory/flop-array read mux":
        return (
            "RTL advisory",
            [
                "Consider registering the memory read address or read data and documenting the added response latency.",
                "If this is a flop-array standing in for memory, consider replacing it with a proper SRAM/register-file implementation or banking the read path.",
                "Keep any patch as a human-reviewed RTL proposal because it can change read latency and memory semantics.",
            ],
            ["pipeline insertion", "response latency changes", "memory read/write semantic changes"],
        )
    if category == "wide mux/select tree":
        return (
            "RTL advisory",
            [
                "Consider pipelining or factoring the select tree, or predecoding select signals close to their consumers.",
                "Review valid/ready behavior before accepting any latency-changing fix.",
            ],
            ["pipeline insertion", "valid/ready protocol changes"],
        )
    if category == "counter/adduction tree":
        return (
            "RTL advisory",
            [
                "For $countones/popcount logic, consider incremental counting, a balanced registered reduction tree, or precomputed partial counts.",
                "Treat any change as a reviewed RTL proposal because it may alter state update timing.",
            ],
            ["pipeline insertion", "response latency changes"],
        )
    if category == "comparator/equality tree":
        return (
            "RTL advisory",
            [
                "Consider predecoding, registering comparison operands, or splitting equality checks into narrower staged compares.",
            ],
            ["pipeline insertion"],
        )
    if category == "high-fanout control path":
        return (
            "Backend first",
            [
                "Try buffering/replication, useful skew, placement grouping, and fanout-aware optimization before RTL changes.",
                "If backend fixes stall, propose RTL-local control replication for human review.",
            ],
            ["retiming across module boundaries"],
        )
    if category == "wire/load dominated routed path":
        return (
            "Backend",
            [
                "Try lower utilization, placement refinement, route optimization, buffering, and final post-route setup optimization.",
            ],
            [],
        )
    if category == "clock skew dominated path":
        return (
            "Backend",
            [
                "Tune CCOpt target skew/max transition and inspect source/capture insertion delay before source RTL changes.",
            ],
            [],
        )
    if category == "IO timing path":
        return (
            "Constraint/interface review",
            [
                "Review input/output delay SDC assumptions first; keep source changes advisory only unless the interface contract changes.",
            ],
            ["valid/ready protocol changes"],
        )
    return (
        "Investigate",
        [
            "Collect full path, QoR, fanout, transition, capacitance, and congestion reports before applying changes.",
        ],
        [],
    )


def analyze_reports(reports: list[TimingReport], *, target_period: float | None = None) -> dict:
    all_paths = [path for report in reports for path in report.paths]
    all_paths = sorted(
        all_paths,
        key=lambda item: item.slack if item.slack is not None else float("inf"),
    )
    worst = all_paths[0] if all_paths else None
    category_counts: dict[str, int] = {}
    for path in all_paths:
        category_counts[path.category] = category_counts.get(path.category, 0) + 1

    recommendations = []
    for rank, path in enumerate(all_paths[:10], start=1):
        owner, actions, review_flags = _recommend_for_category(path.category)
        slack = path.slack or 0.0
        score = max(0.0, -slack)
        if slack > 0:
            score = max(0.0, 0.2 - slack) * 0.1
        if path.category in {
            "synthesized memory/flop-array read mux",
            "counter/adduction tree",
            "wide mux/select tree",
        } and slack <= 0.2:
            score += 0.05
        recommendations.append(
            {
                "rank": rank,
                "priority_score": round(score, 4),
                "owner": owner,
                "category": path.category,
                "startpoint": path.startpoint,
                "endpoint": path.endpoint,
                "path_group": path.path_group,
                "slack": path.slack,
                "required_time": path.required_time,
                "arrival_time": path.arrival_time,
                "clock_period": path.clock_period,
                "dominant_cells": path.dominant_cells[:10],
                "dominant_nets": path.dominant_nets[:10],
                "actions": actions,
                "human_review_required_for": review_flags,
            }
        )

    wns_values = [report.wns for report in reports if report.wns is not None]
    tns_values = [report.tns for report in reports if report.tns is not None]
    parsed_periods = [report.clock_period for report in reports if report.clock_period is not None]
    effective_period = target_period or (parsed_periods[0] if parsed_periods else None)
    ranked_recommendations = sorted(
        recommendations,
        key=lambda item: item["priority_score"],
        reverse=True,
    )
    for rank, item in enumerate(ranked_recommendations, start=1):
        item["rank"] = rank

    return {
        "target_period": effective_period,
        "target_frequency_mhz": round(1000.0 / effective_period, 3) if effective_period else None,
        "wns": min(wns_values) if wns_values else None,
        "tns": min(tns_values) if tns_values else None,
        "violating_path_count": sum(
            report.violating_path_count or 0
            for report in reports
            if report.violating_path_count is not None
        )
        or None,
        "worst_startpoint": worst.startpoint if worst else "",
        "worst_endpoint": worst.endpoint if worst else "",
        "worst_path_group": worst.path_group if worst else "",
        "worst_required_time": worst.required_time if worst else None,
        "worst_arrival_time": worst.arrival_time if worst else None,
        "worst_slack": worst.slack if worst else None,
        "worst_category": worst.category if worst else "unknown",
        "category_counts": category_counts,
        "reports": [asdict(report) for report in reports],
        "ranked_recommendations": ranked_recommendations,
    }


def render_markdown(analysis: dict) -> str:
    lines = ["# Timing Closure Recommendation Report", ""]
    target = analysis.get("target_period")
    if target:
        lines.append(
            f"Target: {target:.3f} ns ({analysis.get('target_frequency_mhz'):.3f} MHz)"
        )
    lines.append(f"WNS: {analysis.get('wns')}")
    lines.append(f"TNS: {analysis.get('tns')}")
    lines.append(f"Violating paths: {analysis.get('violating_path_count')}")
    lines.append(f"Worst category: {analysis.get('worst_category')}")
    lines.append(
        "Worst path: {0} -> {1}".format(
            analysis.get("worst_startpoint") or "unknown",
            analysis.get("worst_endpoint") or "unknown",
        )
    )
    lines.append("")
    lines.append("## Ranked Recommendations")
    for item in analysis.get("ranked_recommendations", []):
        lines.append("")
        lines.append(f"### {item['rank']}. {item['category']} ({item['owner']})")
        lines.append(f"Slack: {item.get('slack')} ns")
        lines.append(f"Path group: {item.get('path_group') or 'unknown'}")
        lines.append(f"Startpoint: `{item.get('startpoint') or 'unknown'}`")
        lines.append(f"Endpoint: `{item.get('endpoint') or 'unknown'}`")
        if item.get("dominant_cells"):
            lines.append("Dominant cells: " + ", ".join(item["dominant_cells"][:8]))
        if item.get("dominant_nets"):
            lines.append("Dominant nets: " + ", ".join(item["dominant_nets"][:8]))
        for action in item.get("actions", []):
            lines.append(f"- {action}")
        if item.get("human_review_required_for"):
            lines.append(
                "Human review required for: "
                + ", ".join(item["human_review_required_for"])
            )
    lines.append("")
    lines.append(
        "RTL behavior is never changed by this analyzer. RTL-facing output is advisory only."
    )
    return "\n".join(lines) + "\n"


def emit_advisory_patch_proposals(analysis: dict, outdir: Path) -> list[str]:
    outdir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for item in analysis.get("ranked_recommendations", []):
        if item.get("owner") != "RTL advisory":
            continue
        path = outdir / "rtl_advisory_{0:02d}_{1}.md".format(
            int(item.get("rank", 0)),
            re.sub(r"[^A-Za-z0-9_]+", "_", str(item.get("category", "unknown"))).strip("_"),
        )
        lines = [
            "# RTL Advisory Patch Proposal",
            "",
            "This is not an applied patch. It is a timing-closure proposal for human RTL review.",
            "",
            f"Category: {item.get('category')}",
            f"Startpoint: `{item.get('startpoint') or 'unknown'}`",
            f"Endpoint: `{item.get('endpoint') or 'unknown'}`",
            f"Slack: {item.get('slack')} ns",
            "",
            "## Review Risks",
        ]
        for risk in item.get("human_review_required_for", []):
            lines.append(f"- {risk}")
        lines.extend(["", "## Proposed Direction"])
        for action in item.get("actions", []):
            lines.append(f"- {action}")
        lines.extend(
            [
                "",
                "## Patch Sketch",
                "",
                "No source edit is generated automatically. Create a reviewed RTL patch only after confirming protocol, latency, reset, and memory semantics.",
            ]
        )
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        written.append(str(path))
    return written


def build_sweep_plan(
    *,
    base_command: str,
    periods: list[float],
    utilizations: list[float],
    ccopt_target_skews: list[float],
    ccopt_target_max_transitions: list[float],
    include_final_postroute_setup_opt: bool,
) -> dict:
    runs = []
    run_id = 0
    for period in periods:
        for util in utilizations:
            for skew in ccopt_target_skews:
                for max_tran in ccopt_target_max_transitions:
                    run_id += 1
                    final_opt = " --final-postroute-setup-opt" if include_final_postroute_setup_opt else ""
                    runs.append(
                        {
                            "id": f"tc_{run_id:03d}",
                            "clock_period": period,
                            "utilization": util,
                            "ccopt_target_skew": skew,
                            "ccopt_target_max_transition": max_tran,
                            "final_postroute_setup_opt": include_final_postroute_setup_opt,
                            "command": (
                                f"{base_command} --clock-period {period:.3f} "
                                f"--utilization {util:.3f} "
                                f"--ccopt-target-skew {skew:.3f} "
                                f"--ccopt-target-max-transition {max_tran:.3f}"
                                f"{final_opt}"
                            ),
                        }
                    )
    return {"runs": runs, "run_count": len(runs)}


def _float_list(raw: str, defaults: list[float]) -> list[float]:
    if not raw:
        return defaults
    return [float(item.strip()) for item in raw.split(",") if item.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze Cadence Genus/Innovus timing reports.")
    subparsers = parser.add_subparsers(dest="command")

    analyze = subparsers.add_parser("analyze", help="Parse timing reports and emit recommendations.")
    analyze.add_argument("reports", nargs="+", type=Path)
    analyze.add_argument("--target-period", type=float, default=None)
    analyze.add_argument("--max-paths", type=int, default=10)
    analyze.add_argument("--format", choices=("json", "markdown"), default="markdown")
    analyze.add_argument("--out", type=Path, default=None)
    analyze.add_argument(
        "--advisory-patches-dir",
        type=Path,
        default=None,
        help="Write human-review-only RTL advisory proposal notes for RTL-facing recommendations.",
    )

    sweep = subparsers.add_parser("sweep-plan", help="Emit a safe tool-knob sweep matrix.")
    sweep.add_argument("--base-command", default="python3 run_innovus_GDSII_universal.py --mappeddir build_mapped_mc_lang")
    sweep.add_argument("--periods", default=",".join(f"{item:.3f}" for item in DEFAULT_SWEEP_PERIODS))
    sweep.add_argument("--utilizations", default=",".join(f"{item:.3f}" for item in DEFAULT_SWEEP_UTILIZATIONS))
    sweep.add_argument("--ccopt-target-skews", default=",".join(f"{item:.3f}" for item in DEFAULT_SWEEP_CCOPT_SKEWS))
    sweep.add_argument("--ccopt-target-max-transitions", default=",".join(f"{item:.3f}" for item in DEFAULT_SWEEP_CCOPT_MAX_TRANSITIONS))
    sweep.add_argument("--final-postroute-setup-opt", action="store_true")
    sweep.add_argument("--out", type=Path, default=None)

    args = parser.parse_args()
    if args.command == "analyze":
        reports = []
        for report_path in args.reports:
            reports.append(parse_timing_report(report_path, max_paths=max(1, args.max_paths)))
        analysis = analyze_reports(reports, target_period=args.target_period)
        if args.advisory_patches_dir:
            analysis["advisory_patch_proposals"] = emit_advisory_patch_proposals(
                analysis,
                args.advisory_patches_dir,
            )
        output = (
            json.dumps(analysis, indent=2)
            if args.format == "json"
            else render_markdown(analysis)
        )
    elif args.command == "sweep-plan":
        output = json.dumps(
            build_sweep_plan(
                base_command=args.base_command,
                periods=_float_list(args.periods, DEFAULT_SWEEP_PERIODS),
                utilizations=_float_list(args.utilizations, DEFAULT_SWEEP_UTILIZATIONS),
                ccopt_target_skews=_float_list(args.ccopt_target_skews, DEFAULT_SWEEP_CCOPT_SKEWS),
                ccopt_target_max_transitions=_float_list(
                    args.ccopt_target_max_transitions,
                    DEFAULT_SWEEP_CCOPT_MAX_TRANSITIONS,
                ),
                include_final_postroute_setup_opt=bool(args.final_postroute_setup_opt),
            ),
            indent=2,
        )
    else:
        parser.print_help()
        return 2

    if getattr(args, "out", None):
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(output, encoding="utf-8")
    else:
        print(output, end="" if output.endswith("\n") else "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
