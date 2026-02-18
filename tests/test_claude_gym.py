"""Tests for claude_gym.py — diff parsing, git diffing, command building."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from claude_gym import ClaudeGym, FileDiff
from config import AgentConfig


class TestParseGitDiffOutput(unittest.TestCase):
    """Test _parse_git_diff_output splitting logic."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        os.system(f"cd {self.tmpdir} && git init -q && git commit -q --allow-empty -m init")
        self.gym = ClaudeGym(work_dir=self.tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_single_file_diff(self):
        diff_text = (
            "diff --git a/foo.py b/foo.py\n"
            "index 1234..5678 100644\n"
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        )
        chunks = self.gym._parse_git_diff_output(diff_text)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0][0], "foo.py")

    def test_multiple_file_diffs(self):
        diff_text = (
            "diff --git a/a.py b/a.py\n"
            "--- a/a.py\n"
            "+++ b/a.py\n"
            "@@ -1 +1 @@\n"
            "-x\n"
            "+y\n"
            "diff --git a/b.py b/b.py\n"
            "--- a/b.py\n"
            "+++ b/b.py\n"
            "@@ -1 +1 @@\n"
            "-a\n"
            "+b\n"
        )
        chunks = self.gym._parse_git_diff_output(diff_text)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0][0], "a.py")
        self.assertEqual(chunks[1][0], "b.py")

    def test_empty_diff(self):
        chunks = self.gym._parse_git_diff_output("")
        self.assertEqual(len(chunks), 0)

    def test_path_with_spaces(self):
        diff_text = (
            "diff --git a/my file.py b/my file.py\n"
            "--- a/my file.py\n"
            "+++ b/my file.py\n"
            "@@ -1 +1 @@\n"
            "-x\n"
            "+y\n"
        )
        chunks = self.gym._parse_git_diff_output(diff_text)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0][0], "my file.py")


class TestGitComputeDiffs(unittest.TestCase):
    """Test _git_compute_diffs with real git repos."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.run = lambda *args: subprocess.run(
            args, cwd=self.tmpdir, capture_output=True, text=True, check=True,
        )
        self.run("git", "init", "-q")
        self.run("git", "commit", "-q", "--allow-empty", "-m", "init")
        self.gym = ClaudeGym(work_dir=self.tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _get_head(self):
        r = self.run("git", "rev-parse", "HEAD")
        return r.stdout.strip()

    def test_added_file(self):
        baseline = self._get_head()
        (Path(self.tmpdir) / "new.py").write_text("print('hello')\n")
        self.run("git", "add", "new.py")
        diffs = self.gym._git_compute_diffs(baseline)
        # Should appear either as tracked added or untracked added
        paths = {d.path for d in diffs}
        self.assertIn("new.py", paths)
        new_diff = next(d for d in diffs if d.path == "new.py")
        self.assertEqual(new_diff.status, "added")

    def test_modified_file(self):
        (Path(self.tmpdir) / "exist.py").write_text("original\n")
        self.run("git", "add", "exist.py")
        self.run("git", "commit", "-q", "-m", "add exist")
        baseline = self._get_head()
        (Path(self.tmpdir) / "exist.py").write_text("modified\n")
        diffs = self.gym._git_compute_diffs(baseline)
        exist_diff = next(d for d in diffs if d.path == "exist.py")
        self.assertEqual(exist_diff.status, "modified")

    def test_deleted_file(self):
        (Path(self.tmpdir) / "todelete.py").write_text("bye\n")
        self.run("git", "add", "todelete.py")
        self.run("git", "commit", "-q", "-m", "add todelete")
        baseline = self._get_head()
        os.remove(Path(self.tmpdir) / "todelete.py")
        self.run("git", "add", "-A")
        diffs = self.gym._git_compute_diffs(baseline)
        del_diff = next(d for d in diffs if d.path == "todelete.py")
        self.assertEqual(del_diff.status, "deleted")

    def test_untracked_file(self):
        baseline = self._get_head()
        (Path(self.tmpdir) / "untracked.py").write_text("data\n")
        diffs = self.gym._git_compute_diffs(baseline)
        untracked_diff = next(d for d in diffs if d.path == "untracked.py")
        self.assertEqual(untracked_diff.status, "added")

    def test_renamed_file(self):
        (Path(self.tmpdir) / "old_name.py").write_text("content here\n")
        self.run("git", "add", "old_name.py")
        self.run("git", "commit", "-q", "-m", "add old_name")
        baseline = self._get_head()
        self.run("git", "mv", "old_name.py", "new_name.py")
        diffs = self.gym._git_compute_diffs(baseline)
        renamed = next((d for d in diffs if d.path == "new_name.py"), None)
        self.assertIsNotNone(renamed)
        self.assertEqual(renamed.status, "renamed")
        self.assertEqual(renamed.old_path, "old_name.py")


class TestBuildCommand(unittest.TestCase):
    """Test _build_command output."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        os.system(f"cd {self.tmpdir} && git init -q && git commit -q --allow-empty -m init")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_default_flags(self):
        gym = ClaudeGym(work_dir=self.tmpdir)
        cmd = gym._build_command("test prompt")
        self.assertIn("-p", cmd)
        self.assertIn("test prompt", cmd)
        self.assertIn("--output-format", cmd)
        self.assertIn("stream-json", cmd)
        self.assertIn("--verbose", cmd)

    def test_model_flag(self):
        gym = ClaudeGym(work_dir=self.tmpdir, model="opus")
        cmd = gym._build_command("test")
        self.assertIn("--model", cmd)
        self.assertIn("opus", cmd)

    def test_flag_overrides(self):
        config = AgentConfig(flag_overrides={"-p": "--prompt"})
        gym = ClaudeGym(work_dir=self.tmpdir, agent_config=config)
        cmd = gym._build_command("test")
        self.assertIn("--prompt", cmd)
        self.assertNotIn("-p", cmd)

    def test_flag_suppression(self):
        config = AgentConfig(flag_overrides={"--verbose": None})
        gym = ClaudeGym(work_dir=self.tmpdir, agent_config=config)
        cmd = gym._build_command("test")
        self.assertNotIn("--verbose", cmd)

    def test_interactive_mode(self):
        gym = ClaudeGym(work_dir=self.tmpdir, interactive=True)
        cmd = gym._build_command("test prompt")
        # Interactive mode: no -p, no --output-format, prompt is positional
        self.assertNotIn("-p", cmd)
        self.assertNotIn("--output-format", cmd)
        self.assertEqual(cmd[-1], "test prompt")


if __name__ == "__main__":
    unittest.main()
