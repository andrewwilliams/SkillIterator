"""Tests for run_skill.py — validation, revert, path expansion."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from run_skill import get_multiline_input, revert_changes, validate_project_dir


class TestValidateProjectDir(unittest.TestCase):
    """Test validate_project_dir safety checks."""

    def test_blocks_root(self):
        err = validate_project_dir(Path("/").resolve())
        self.assertIsNotNone(err)
        self.assertIn("dangerous", err.lower())

    def test_blocks_home(self):
        err = validate_project_dir(Path.home().resolve())
        self.assertIsNotNone(err)
        self.assertIn("dangerous", err.lower())

    def test_blocks_script_dir(self):
        script_dir = Path(__file__).resolve().parent.parent
        err = validate_project_dir(script_dir)
        self.assertIsNotNone(err)
        self.assertIn("evaluator itself", err)

    def test_blocks_non_git_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            err = validate_project_dir(Path(tmpdir))
        self.assertIsNotNone(err)
        self.assertIn("not a git repository", err)

    def test_blocks_dirty_tree(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            subprocess.run(["git", "init", "-q"], cwd=tmpdir, check=True,
                           capture_output=True)
            subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "init"],
                           cwd=tmpdir, check=True, capture_output=True)
            (Path(tmpdir) / "dirty.txt").write_text("uncommitted\n")
            err = validate_project_dir(Path(tmpdir))
        self.assertIsNotNone(err)
        self.assertIn("uncommitted", err)

    def test_accepts_clean_repo(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            subprocess.run(["git", "init", "-q"], cwd=tmpdir, check=True,
                           capture_output=True)
            subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "init"],
                           cwd=tmpdir, check=True, capture_output=True)
            err = validate_project_dir(Path(tmpdir))
        self.assertIsNone(err)

    def test_skip_clean_check(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            subprocess.run(["git", "init", "-q"], cwd=tmpdir, check=True,
                           capture_output=True)
            subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "init"],
                           cwd=tmpdir, check=True, capture_output=True)
            (Path(tmpdir) / "dirty.txt").write_text("uncommitted\n")
            err = validate_project_dir(Path(tmpdir), skip_clean_check=True)
        self.assertIsNone(err)


class TestRevertChanges(unittest.TestCase):
    """Test revert_changes file deletion and git checkout."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        subprocess.run(["git", "init", "-q"], cwd=self.tmpdir, check=True,
                       capture_output=True)
        # Create and commit a base file
        (Path(self.tmpdir) / "base.txt").write_text("original\n")
        subprocess.run(["git", "add", "base.txt"], cwd=self.tmpdir, check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=self.tmpdir,
                       check=True, capture_output=True)
        self.project_dir = Path(self.tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_deletes_created_files(self):
        (self.project_dir / "new_file.py").write_text("created\n")
        self.assertTrue((self.project_dir / "new_file.py").exists())
        revert_changes(self.project_dir, ["new_file.py"], [])
        self.assertFalse((self.project_dir / "new_file.py").exists())

    def test_restores_modified_files(self):
        (self.project_dir / "base.txt").write_text("changed\n")
        self.assertEqual((self.project_dir / "base.txt").read_text(), "changed\n")
        revert_changes(self.project_dir, [], ["base.txt"])
        self.assertEqual((self.project_dir / "base.txt").read_text(), "original\n")

    def test_removes_empty_parent_dirs(self):
        nested = self.project_dir / "a" / "b"
        nested.mkdir(parents=True)
        (nested / "file.py").write_text("x\n")
        revert_changes(self.project_dir, ["a/b/file.py"], [])
        self.assertFalse(nested.exists())
        # Parent 'a' should also be removed since it's empty
        self.assertFalse((self.project_dir / "a").exists())


class TestPathExpansion(unittest.TestCase):
    """Test that path expansion handles ~, $HOME, and relative paths."""

    def test_tilde_expansion(self):
        expanded = Path(os.path.expandvars(os.path.expanduser("~/test"))).resolve()
        self.assertTrue(str(expanded).startswith(str(Path.home())))

    def test_env_var_expansion(self):
        with patch.dict(os.environ, {"MY_DIR": "/tmp/testdir"}):
            expanded = Path(os.path.expandvars("$MY_DIR/sub")).resolve()
        # On macOS, /tmp resolves to /private/tmp
        self.assertTrue(str(expanded).endswith("/testdir/sub"))


if __name__ == "__main__":
    unittest.main()
