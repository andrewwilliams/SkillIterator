"""Tests for config.py — loading, saving, flag resolution, command building."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config import (
    AgentConfig,
    build_base_command,
    build_env,
    load_config,
    resolve_flag,
    save_config,
)


class TestLoadConfig(unittest.TestCase):
    """Test config loading behavior."""

    def test_default_when_no_file(self):
        with patch("config.CONFIG_FILE", Path("/tmp/nonexistent_config_xyz.json")):
            config = load_config()
        self.assertEqual(config.command, "claude")
        self.assertEqual(config.extra_args, [])

    def test_valid_json(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"command": "myagent", "extra_args": ["--team", "eng"]}, f)
            tmp_path = f.name
        try:
            with patch("config.CONFIG_FILE", Path(tmp_path)):
                config = load_config()
            self.assertEqual(config.command, "myagent")
            self.assertEqual(config.extra_args, ["--team", "eng"])
        finally:
            os.unlink(tmp_path)

    def test_malformed_json_returns_defaults(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("not valid json{{{")
            tmp_path = f.name
        try:
            with patch("config.CONFIG_FILE", Path(tmp_path)):
                config = load_config()
            self.assertEqual(config.command, "claude")
        finally:
            os.unlink(tmp_path)


class TestSaveConfig(unittest.TestCase):
    """Test config saving."""

    def test_creates_dir_and_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / "subdir"
            config_file = config_dir / "config.json"
            with patch("config.CONFIG_DIR", config_dir), \
                 patch("config.CONFIG_FILE", config_file):
                save_config(AgentConfig(command="test_cmd"))
            self.assertTrue(config_file.exists())
            data = json.loads(config_file.read_text())
            self.assertEqual(data["command"], "test_cmd")

    def test_dir_permissions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / "perms"
            config_file = config_dir / "config.json"
            with patch("config.CONFIG_DIR", config_dir), \
                 patch("config.CONFIG_FILE", config_file):
                save_config(AgentConfig())
            mode = oct(os.stat(str(config_dir)).st_mode & 0o777)
            self.assertEqual(mode, "0o700")


class TestResolveFlag(unittest.TestCase):
    """Test flag resolution logic."""

    def test_no_override_returns_original(self):
        config = AgentConfig()
        self.assertEqual(resolve_flag(config, "-p"), "-p")

    def test_renamed_flag(self):
        config = AgentConfig(flag_overrides={"-p": "--prompt"})
        self.assertEqual(resolve_flag(config, "-p"), "--prompt")

    def test_suppressed_flag(self):
        config = AgentConfig(flag_overrides={"--verbose": None})
        self.assertIsNone(resolve_flag(config, "--verbose"))

    def test_unrelated_flags_unchanged(self):
        config = AgentConfig(flag_overrides={"-p": "--prompt"})
        self.assertEqual(resolve_flag(config, "--model"), "--model")


class TestBuildBaseCommand(unittest.TestCase):
    """Test command building."""

    def test_default(self):
        config = AgentConfig()
        cmd = build_base_command(config)
        self.assertEqual(cmd, ["claude"])

    def test_with_extra_args(self):
        config = AgentConfig(command="myagent", extra_args=["--team", "eng"])
        cmd = build_base_command(config)
        self.assertEqual(cmd, ["myagent", "--team", "eng"])


class TestBuildEnv(unittest.TestCase):
    """Test env building."""

    def test_strips_nesting_guard_vars(self):
        config = AgentConfig(nesting_guard_vars=["TEST_VAR"])
        with patch.dict(os.environ, {"TEST_VAR": "1", "OTHER": "2"}):
            env = build_env(config)
        self.assertNotIn("TEST_VAR", env)
        self.assertIn("OTHER", env)

    def test_merges_env_vars(self):
        config = AgentConfig(env_vars={"MY_KEY": "my_val"})
        env = build_env(config)
        self.assertEqual(env["MY_KEY"], "my_val")


if __name__ == "__main__":
    unittest.main()
