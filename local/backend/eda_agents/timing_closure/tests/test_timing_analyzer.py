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
