"""The event reader's output-limit refusal reports sizes, never source text (Kata 6sc9)."""

import unittest

from tools.review_events import event_reader as reader
from tools.review_events import review_events as contract


class OutputLimitDiagnosticTests(unittest.TestCase):
    def test_output_limit_diagnostic_reports_character_counts_without_source_text(self):
        output = "private-source-marker " + "\u00e9" * 20
        self.assertEqual(reader.bounded(output, len(output)), output)
        with self.assertRaises(contract.ReviewError) as refused:
            reader.bounded(output, len(output) - 1)
        message = str(refused.exception)
        self.assertIn(f"requires {len(output)} characters", message)
        self.assertIn(f"limit is {len(output) - 1}", message)
        self.assertIn("no fields were truncated", message)
        self.assertIn("outer client/tool response limit may still apply", message)
        self.assertNotIn("private-source-marker", message)
        self.assertNotIn("\u00e9", message)

    def test_a_non_integer_or_out_of_range_limit_is_still_refused(self):
        for bad in (0, 1000001, True, "5"):
            with self.assertRaises(contract.ReviewError):
                reader.bounded("x", bad)


if __name__ == "__main__":
    unittest.main()
