import math

import tempfile
import unittest
from pathlib import Path

from timing_closure.timing_analyzer import analyze_reports, parse_timing_report


FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures"


def test_ddr4_memory_mux_fixture():
    report = parse_timing_report(FIXTURE_DIR / "ddr4_postroute_memory_mux.rpt")
    analysis = analyze_reports([report], target_period=4.762)

    assert analysis["wns"] == -0.119
    assert abs(analysis["target_frequency_mhz"] - 210.0) < 0.01
    assert analysis["worst_startpoint"] == "service_addr_q_reg[0]/Q"
    assert analysis["worst_endpoint"] == "rsp_rdata_q_reg[17]/D"
    assert analysis["worst_category"] == "synthesized memory/flop-array read mux"
    assert analysis["ranked_recommendations"][0]["owner"] == "RTL advisory"


def test_countones_fixture():
    report = parse_timing_report(FIXTURE_DIR / "ddr4_mapped_countones.rpt")
    analysis = analyze_reports([report])

    assert analysis["worst_category"] == "counter/adduction tree"
    assert "u_tFAW_tracker_act_count_reg[1]/d" in analysis["worst_endpoint"]


def test_backend_route_skew_fixture():
    report = parse_timing_report(FIXTURE_DIR / "backend_route_skew.rpt")
    analysis = analyze_reports([report])

    assert analysis["wns"] == -0.184
    assert analysis["tns"] == -1.512
    assert analysis["violating_path_count"] == 12
    assert analysis["worst_category"] in {
        "wire/load dominated routed path",
        "clock skew dominated path",
        "high-fanout control path",
    }


def test_innovus_beginpoint_slack_time_fixture():
    report = parse_timing_report(FIXTURE_DIR / "innovus_beginpoint_slack_time.rpt")
    analysis = analyze_reports([report], target_period=4.762)

    assert analysis["wns"] == -0.119
    assert analysis["violating_path_count"] == 1
    assert analysis["worst_startpoint"] == "service_addr_q_reg[0]/Q"
    assert analysis["worst_endpoint"] == "rsp_rdata_q_reg[17]/D"
    assert analysis["worst_path_group"] == "clk"
    assert analysis["worst_required_time"] == 4.855
    assert analysis["worst_arrival_time"] == 4.974
    assert analysis["worst_slack"] == -0.119
    assert analysis["worst_category"] == "synthesized memory/flop-array read mux"
    assert report.clock_period == 5.000


class RoundedSlackTests(unittest.TestCase):
    def test_rounded_zero_and_tool_classification(self):
        cases = [("-0.000", "VIOLATED", 1), ("-0.000", "MET", 1),
                 ("0.000", "VIOLATED", 1), ("0.000", "MET", 0),
                 ("+0.000", "MET", 0), ("0.020", "MET", 0)]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "timing.rpt"
            for slack, marker, count in cases:
                with self.subTest(slack=slack, marker=marker):
                    path.write_text(f"Path 1: {marker} Hold Check with Pin sink/CLK\n"
                                    f"Beginpoint: input\nEndpoint: sink/D\nSlack Time {slack}\n")
                    report = parse_timing_report(path)
                    self.assertEqual(report.violating_path_count, count)
                    self.assertEqual(math.copysign(1, report.wns), -1 if slack.startswith("-") else 1)

    def test_violation_beyond_path_limit_overrides_clean_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "timing.rpt"
            path.write_text("Violating paths: 0\n"
                            "Path 1: MET Hold Check\nSlack Time 0.020\n"
                            "Path 2: VIOLATED Hold Check\nSlack Time -0.000\n")
            report = parse_timing_report(path, max_paths=1)
            self.assertEqual(report.violating_path_count, 1)
            self.assertEqual(len(report.paths), 1)
