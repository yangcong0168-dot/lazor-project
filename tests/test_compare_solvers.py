"""Keep benchmark failures and censored times out of speedup claims."""

import unittest

from lazor.compare_solvers import summarize


class ComparisonSummaryTests(unittest.TestCase):
    def test_successes_use_median_total_times(self):
        records = [
            {"method": method, "status": "solved",
             "solution_verified": True, "elapsed_seconds": seconds}
            for method, seconds in (
                ("dfs", 9), ("dfs", 10), ("dfs", 11),
                ("smt", 1), ("smt", 2), ("smt", 6),
            )
        ]
        result = summarize(records, 110)
        self.assertEqual(result["median_time_ratio_dfs_over_smt"], 5)

    def test_timeouts_produce_only_a_conservative_lower_bound(self):
        records = [
            {"method": "dfs", "status": "timeout"},
            {"method": "smt", "status": "solved",
             "solution_verified": True, "elapsed_seconds": 1},
            {"method": "smt", "status": "solved",
             "solution_verified": True, "elapsed_seconds": 2},
        ]
        result = summarize(records, 110)
        self.assertEqual(result["conservative_time_ratio_lower_bound"], 55)
        self.assertNotIn("median_time_ratio_dfs_over_smt", result)
        self.assertIsNone(result["dfs"]["median_success_seconds"])

    def test_errors_unknown_and_unverified_answers_cannot_claim_speedup(self):
        for status in ("worker_error", "unknown", "no_solution", "solved"):
            with self.subTest(status=status):
                records = [
                    {"method": "dfs", "status": "timeout"},
                    {"method": "smt", "status": status,
                     "solution_verified": False, "elapsed_seconds": 1},
                ]
                result = summarize(records, 110)
                self.assertNotIn(
                    "conservative_time_ratio_lower_bound", result
                )
                self.assertNotIn("median_time_ratio_dfs_over_smt", result)

    def test_empty_and_partial_reports_do_not_claim_a_speedup(self):
        for records in ([], [{"method": "dfs", "status": "timeout"}]):
            result = summarize(records, 110)
            self.assertNotIn("conservative_time_ratio_lower_bound", result)
            self.assertNotIn("median_time_ratio_dfs_over_smt", result)


if __name__ == "__main__":
    unittest.main()
