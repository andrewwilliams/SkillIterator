"""Tests for diff_server.py — feedback formatting."""

import unittest

from diff_server import DiffFeedback, LineComment, _format_feedback


class TestFormatFeedback(unittest.TestCase):
    """Test _format_feedback serialization."""

    def test_overall_only(self):
        fb = DiffFeedback(overall_feedback="Tests should use @Test")
        result = _format_feedback(fb)
        self.assertEqual(result, "Overall: Tests should use @Test")

    def test_line_comments_only(self):
        fb = DiffFeedback(line_comments=[
            LineComment(file_path="foo.py", start_line=10, end_line=10, comment="Fix this"),
        ])
        result = _format_feedback(fb)
        self.assertIn("On foo.py line 10:", result)
        self.assertIn("Fix this", result)

    def test_range_comments(self):
        fb = DiffFeedback(line_comments=[
            LineComment(file_path="bar.py", start_line=5, end_line=12, comment="Refactor range"),
        ])
        result = _format_feedback(fb)
        self.assertIn("On bar.py lines 5-12:", result)

    def test_mixed_feedback(self):
        fb = DiffFeedback(
            overall_feedback="Looks good overall",
            line_comments=[
                LineComment(file_path="a.py", start_line=1, end_line=1, comment="Typo"),
            ],
        )
        result = _format_feedback(fb)
        self.assertIn("Overall: Looks good overall", result)
        self.assertIn("On a.py line 1:", result)
        # Overall comes first
        self.assertTrue(result.index("Overall") < result.index("On a.py"))

    def test_empty_feedback(self):
        fb = DiffFeedback()
        result = _format_feedback(fb)
        self.assertEqual(result, "")


if __name__ == "__main__":
    unittest.main()
