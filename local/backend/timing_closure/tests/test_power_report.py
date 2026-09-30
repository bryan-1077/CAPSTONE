import unittest

from timing_closure.constraints import parse_total_power


class PowerReportTests(unittest.TestCase):
    def test_innovus_section_heading_and_watt_header(self):
        report = """* Power Units = 1W
Total Power
-------------------------------
Total Internal Power: 0.02356090 70.4637%
Total Switching Power: 0.00987600 29.5362%
Total Leakage Power: 0.00000003 0.0001%
Total Power: 0.03343694
-------------------------------
"""
        self.assertAlmostEqual(parse_total_power(report), 0.03343694)

    def test_heading_alone_is_not_a_measurement(self):
        with self.assertRaises(ValueError):
            parse_total_power("Power Units = 1W\nTotal Power\n---")

    def test_heading_does_not_hide_malformed_measurements(self):
        for value in ("", "nan W", "unknown", "2 bananas", "0 W", "-1 W"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_total_power("Total Power\nTotal Power: " + value)

    def test_multiple_sections_preserve_worst_power(self):
        self.assertEqual(parse_total_power(
            "Power Units = 1W\nTotal Power\nTotal Power: 1\n"
            "Total Power\nTotal Power: 2.1"
        ), 2.1)


if __name__ == "__main__":
    unittest.main()
