"""Tests for evaluator.py — expectation verification and glob matching."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from evaluator import (
    CheckResult,
    ClaudeEvaluator,
    CommandExpectation,
    DiffExpectation,
    FileExpectation,
    SyntaxExpectation,
    _glob_match,
)
from claude_gym import ClaudeGym, FileDiff


class TestGlobMatch(unittest.TestCase):
    """Test the _glob_match function."""

    def test_exact_match(self):
        self.assertTrue(_glob_match("foo.py", "foo.py"))

    def test_star_matches_single_segment(self):
        self.assertTrue(_glob_match("foo.py", "*.py"))
        self.assertFalse(_glob_match("dir/foo.py", "*.py"))

    def test_double_star_matches_zero_dirs(self):
        self.assertTrue(_glob_match("foo.py", "**/*.py"))

    def test_double_star_matches_multiple_dirs(self):
        self.assertTrue(_glob_match("a/b/c/foo.py", "**/*.py"))

    def test_double_star_at_end(self):
        self.assertTrue(_glob_match("src/a/b.py", "src/**"))

    def test_question_mark_single_char(self):
        self.assertTrue(_glob_match("test_a.py", "test_?.py"))
        self.assertFalse(_glob_match("test_ab.py", "test_?.py"))

    def test_no_match(self):
        self.assertFalse(_glob_match("foo.js", "*.py"))

    def test_nested_glob(self):
        self.assertTrue(_glob_match("Tests/Unit/FooTests.swift", "Tests/**/*.swift"))


class TestFileExpectationVerification(unittest.TestCase):
    """Test _verify_file_expectations with real temp directories."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        # Create a test file
        test_file = Path(self.tmpdir) / "hello.py"
        test_file.write_text("import os\n\ndef hello():\n    return 'hi'\n")
        # Create a nested file
        nested_dir = Path(self.tmpdir) / "src"
        nested_dir.mkdir()
        (nested_dir / "util.py").write_text("# utility\ndef util(): pass\n")
        # Init git so ClaudeGym works
        os.system(f"cd {self.tmpdir} && git init -q && git add -A && git commit -q -m init")
        self.gym = ClaudeGym(work_dir=self.tmpdir)
        self.evaluator = ClaudeEvaluator()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_content_contains_pass(self):
        exp = FileExpectation(path="hello.py", content_contains=["def hello"])
        results = self.evaluator._verify_file_expectations(self.gym, [exp])
        self.assertTrue(all(r.passed for r in results))

    def test_content_contains_fail(self):
        exp = FileExpectation(path="hello.py", content_contains=["def goodbye"])
        results = self.evaluator._verify_file_expectations(self.gym, [exp])
        self.assertFalse(all(r.passed for r in results))

    def test_content_not_contains_pass(self):
        exp = FileExpectation(path="hello.py", content_not_contains=["import sys"])
        results = self.evaluator._verify_file_expectations(self.gym, [exp])
        self.assertTrue(all(r.passed for r in results))

    def test_content_not_contains_fail(self):
        exp = FileExpectation(path="hello.py", content_not_contains=["import os"])
        results = self.evaluator._verify_file_expectations(self.gym, [exp])
        self.assertFalse(all(r.passed for r in results))

    def test_regex_match_pass(self):
        exp = FileExpectation(path="hello.py", content_matches=[r"def \w+\(\):"])
        results = self.evaluator._verify_file_expectations(self.gym, [exp])
        self.assertTrue(all(r.passed for r in results))

    def test_regex_match_fail(self):
        exp = FileExpectation(path="hello.py", content_matches=[r"class \w+:"])
        results = self.evaluator._verify_file_expectations(self.gym, [exp])
        self.assertFalse(all(r.passed for r in results))

    def test_should_not_exist_pass(self):
        exp = FileExpectation(path="nonexistent.py", should_exist=False)
        results = self.evaluator._verify_file_expectations(self.gym, [exp])
        self.assertTrue(all(r.passed for r in results))

    def test_should_not_exist_fail(self):
        exp = FileExpectation(path="hello.py", should_exist=False)
        results = self.evaluator._verify_file_expectations(self.gym, [exp])
        self.assertFalse(all(r.passed for r in results))

    def test_should_exist_fail(self):
        exp = FileExpectation(path="missing.py", should_exist=True)
        results = self.evaluator._verify_file_expectations(self.gym, [exp])
        self.assertFalse(all(r.passed for r in results))

    def test_min_lines(self):
        exp = FileExpectation(path="hello.py", min_lines=2)
        results = self.evaluator._verify_file_expectations(self.gym, [exp])
        self.assertTrue(all(r.passed for r in results))

    def test_min_lines_fail(self):
        exp = FileExpectation(path="hello.py", min_lines=100)
        results = self.evaluator._verify_file_expectations(self.gym, [exp])
        self.assertFalse(all(r.passed for r in results))

    def test_max_lines(self):
        exp = FileExpectation(path="hello.py", max_lines=100)
        results = self.evaluator._verify_file_expectations(self.gym, [exp])
        self.assertTrue(all(r.passed for r in results))

    def test_max_lines_fail(self):
        exp = FileExpectation(path="hello.py", max_lines=1)
        results = self.evaluator._verify_file_expectations(self.gym, [exp])
        self.assertFalse(all(r.passed for r in results))

    def test_path_pattern_glob(self):
        exp = FileExpectation(path_pattern="**/*.py", min_matching_files=2)
        results = self.evaluator._verify_file_expectations(self.gym, [exp])
        # Should find hello.py and src/util.py
        pattern_result = results[0]
        self.assertTrue(pattern_result.passed)

    def test_path_pattern_no_match(self):
        exp = FileExpectation(path_pattern="**/*.swift", min_matching_files=1)
        results = self.evaluator._verify_file_expectations(self.gym, [exp])
        self.assertFalse(results[0].passed)


class TestCommandExpectationVerification(unittest.TestCase):
    """Test _verify_command_expectations."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        os.system(f"cd {self.tmpdir} && git init -q && git add -A && git commit -q --allow-empty -m init")
        self.gym = ClaudeGym(work_dir=self.tmpdir)
        self.evaluator = ClaudeEvaluator()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_returncode_pass(self):
        exp = CommandExpectation(command=["echo", "hello"], returncode=0)
        results = self.evaluator._verify_command_expectations(self.gym, [exp])
        self.assertTrue(results[0].passed)

    def test_returncode_fail(self):
        exp = CommandExpectation(command=["false"], returncode=0)
        results = self.evaluator._verify_command_expectations(self.gym, [exp])
        self.assertFalse(results[0].passed)

    def test_stdout_contains_pass(self):
        exp = CommandExpectation(command=["echo", "world"], stdout_contains=["world"])
        results = self.evaluator._verify_command_expectations(self.gym, [exp])
        # Check: rc + stdout_contains
        self.assertTrue(all(r.passed for r in results))

    def test_stdout_not_contains_pass(self):
        exp = CommandExpectation(command=["echo", "hello"], stdout_not_contains=["goodbye"])
        results = self.evaluator._verify_command_expectations(self.gym, [exp])
        self.assertTrue(all(r.passed for r in results))

    def test_stderr_contains(self):
        exp = CommandExpectation(
            command=["python3", "-c", "import sys; sys.stderr.write('err_msg')"],
            stderr_contains=["err_msg"],
        )
        results = self.evaluator._verify_command_expectations(self.gym, [exp])
        stderr_check = [r for r in results if "stderr contains" in r.target]
        self.assertTrue(all(r.passed for r in stderr_check))

    def test_timeout(self):
        exp = CommandExpectation(command=["sleep", "10"], timeout=1)
        results = self.evaluator._verify_command_expectations(self.gym, [exp])
        self.assertFalse(results[0].passed)
        self.assertIn("timed out", results[0].message)

    def test_missing_command(self):
        exp = CommandExpectation(command=["nonexistent_cmd_xyz"])
        results = self.evaluator._verify_command_expectations(self.gym, [exp])
        self.assertFalse(results[0].passed)
        self.assertIn("not found", results[0].message)


class TestDiffExpectationVerification(unittest.TestCase):
    """Test _verify_diff_expectations."""

    def setUp(self):
        self.evaluator = ClaudeEvaluator()

    def _make_diffs(self, items: list[tuple[str, str]]) -> list[FileDiff]:
        return [
            FileDiff(path=p, status=s, unified_diff="", before_hash=None, after_hash=None)
            for p, s in items
        ]

    def test_allowed_statuses_pass(self):
        diffs = self._make_diffs([("a.py", "added"), ("b.py", "added")])
        exp = DiffExpectation(allowed_statuses=["added"])
        results = self.evaluator._verify_diff_expectations(diffs, [exp])
        self.assertTrue(all(r.passed for r in results))

    def test_allowed_statuses_fail(self):
        diffs = self._make_diffs([("a.py", "added"), ("b.py", "modified")])
        exp = DiffExpectation(allowed_statuses=["added"])
        results = self.evaluator._verify_diff_expectations(diffs, [exp])
        # One should fail
        self.assertFalse(all(r.passed for r in results))

    def test_path_patterns_pass(self):
        diffs = self._make_diffs([("Tests/FooTest.swift", "added")])
        exp = DiffExpectation(allowed_path_patterns=["Tests/**/*.swift"])
        results = self.evaluator._verify_diff_expectations(diffs, [exp])
        self.assertTrue(all(r.passed for r in results))

    def test_disallowed_path_patterns(self):
        diffs = self._make_diffs([("src/main.py", "modified")])
        exp = DiffExpectation(disallowed_path_patterns=["src/**"])
        results = self.evaluator._verify_diff_expectations(diffs, [exp])
        self.assertFalse(all(r.passed for r in results))

    def test_must_include_paths(self):
        diffs = self._make_diffs([("a.py", "added")])
        exp = DiffExpectation(must_include_paths=["a.py", "b.py"])
        results = self.evaluator._verify_diff_expectations(diffs, [exp])
        passed_targets = {r.target for r in results if r.passed}
        failed_targets = {r.target for r in results if not r.passed}
        self.assertIn("must include a.py", passed_targets)
        self.assertIn("must include b.py", failed_targets)

    def test_file_count_bounds(self):
        diffs = self._make_diffs([("a.py", "added"), ("b.py", "added")])
        exp = DiffExpectation(min_files_changed=1, max_files_changed=3)
        results = self.evaluator._verify_diff_expectations(diffs, [exp])
        self.assertTrue(all(r.passed for r in results))

    def test_file_count_over_max(self):
        diffs = self._make_diffs([("a.py", "added"), ("b.py", "added"), ("c.py", "added")])
        exp = DiffExpectation(max_files_changed=2)
        results = self.evaluator._verify_diff_expectations(diffs, [exp])
        self.assertFalse(all(r.passed for r in results))


class TestSyntaxExpectation(unittest.TestCase):
    """Test syntax checking."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        os.system(f"cd {self.tmpdir} && git init -q && git commit -q --allow-empty -m init")
        self.gym = ClaudeGym(work_dir=self.tmpdir)
        self.evaluator = ClaudeEvaluator()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_valid_python(self):
        (Path(self.tmpdir) / "good.py").write_text("def f():\n    return 1\n")
        exp = SyntaxExpectation(path="good.py", language="python")
        results = self.evaluator._verify_syntax_expectations(self.gym, [exp])
        self.assertTrue(results[0].passed)

    def test_invalid_python(self):
        (Path(self.tmpdir) / "bad.py").write_text("def f(\n    return 1\n")
        exp = SyntaxExpectation(path="bad.py", language="python")
        results = self.evaluator._verify_syntax_expectations(self.gym, [exp])
        self.assertFalse(results[0].passed)

    def test_missing_file(self):
        exp = SyntaxExpectation(path="nope.py", language="python")
        results = self.evaluator._verify_syntax_expectations(self.gym, [exp])
        self.assertFalse(results[0].passed)

    def test_unsupported_language(self):
        (Path(self.tmpdir) / "file.rs").write_text("fn main() {}")
        exp = SyntaxExpectation(path="file.rs", language="rust")
        results = self.evaluator._verify_syntax_expectations(self.gym, [exp])
        self.assertFalse(results[0].passed)
        self.assertIn("Unsupported", results[0].message)


class TestExpectationValidation(unittest.TestCase):
    """Test validate() methods on expectation dataclasses."""

    def test_file_exp_valid(self):
        exp = FileExpectation(path="foo.py")
        self.assertEqual(exp.validate(), [])

    def test_file_exp_no_path(self):
        exp = FileExpectation()
        errors = exp.validate()
        self.assertTrue(any("must set either" in e for e in errors))

    def test_file_exp_both_paths(self):
        exp = FileExpectation(path="foo.py", path_pattern="*.py")
        errors = exp.validate()
        self.assertTrue(any("mutually exclusive" in e for e in errors))

    def test_file_exp_bad_regex(self):
        exp = FileExpectation(path="foo.py", content_matches=["[invalid"])
        errors = exp.validate()
        self.assertTrue(any("invalid regex" in e for e in errors))

    def test_cmd_exp_valid(self):
        exp = CommandExpectation(command=["echo", "hi"])
        self.assertEqual(exp.validate(), [])

    def test_cmd_exp_empty_command(self):
        exp = CommandExpectation(command=[])
        errors = exp.validate()
        self.assertTrue(any("empty" in e for e in errors))

    def test_cmd_exp_bad_timeout(self):
        exp = CommandExpectation(command=["echo"], timeout=0)
        errors = exp.validate()
        self.assertTrue(any("timeout" in e for e in errors))

    def test_diff_exp_valid(self):
        exp = DiffExpectation(allowed_statuses=["added", "modified"])
        self.assertEqual(exp.validate(), [])

    def test_diff_exp_invalid_status(self):
        exp = DiffExpectation(allowed_statuses=["added", "bogus"])
        errors = exp.validate()
        self.assertTrue(any("invalid status" in e for e in errors))


if __name__ == "__main__":
    unittest.main()
