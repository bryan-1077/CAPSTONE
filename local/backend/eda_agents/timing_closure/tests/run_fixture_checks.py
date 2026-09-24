#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
from timing_closure.timing_analyzer import analyze_reports, parse_timing_report


def main() -> int:
    fixture_dir = Path(__file__).resolve().parents[1] / "fixtures"
    reports = [parse_timing_report(path) for path in sorted(fixture_dir.glob("*.rpt"))]
    analysis = analyze_reports(reports, target_period=4.762)
    categories = {
        item["category"] for item in analysis.get("ranked_recommendations", [])
    }

    assert analysis["wns"] == -0.184
    assert analysis["tns"] == -1.512
    assert abs(analysis["target_frequency_mhz"] - 210.0) < 0.01
    assert analysis["worst_category"] == "clock skew dominated path"
    assert "synthesized memory/flop-array read mux" in categories
    assert "counter/adduction tree" in categories

    print("timing_closure fixture checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
