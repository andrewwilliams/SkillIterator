"""
AgentConfig — Configurable CLI agent support for Skill Iterator.

Stores per-user settings (command name, extra args, flag overrides, env vars)
in ~/.skilliterator/config.json. Includes a first-run setup wizard.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import log

CONFIG_DIR = Path.home() / ".skilliterator"
CONFIG_FILE = CONFIG_DIR / "config.json"


@dataclass
class AgentConfig:
    command: str = "claude"
    extra_args: list[str] = field(default_factory=list)
    flag_overrides: dict[str, str | None] = field(default_factory=dict)
    env_vars: dict[str, str] = field(default_factory=dict)
    nesting_guard_vars: list[str] = field(
        default_factory=lambda: ["CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"]
    )


def load_config() -> AgentConfig:
    """Read ~/.skilliterator/config.json. Returns defaults if missing or malformed."""
    if not CONFIG_FILE.is_file():
        return AgentConfig()
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return AgentConfig(
            command=data.get("command", "claude"),
            extra_args=data.get("extra_args", []),
            flag_overrides=data.get("flag_overrides", {}),
            env_vars=data.get("env_vars", {}),
            nesting_guard_vars=data.get(
                "nesting_guard_vars", ["CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"]
            ),
        )
    except (json.JSONDecodeError, TypeError, AttributeError) as e:
        log.warn(f"Malformed config at {CONFIG_FILE}: {e}. Using defaults.")
        return AgentConfig()


def save_config(config: AgentConfig) -> None:
    """Write config to ~/.skilliterator/config.json. Creates dir with mode 0o700."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(str(CONFIG_DIR), 0o700)
    data = {
        "command": config.command,
        "extra_args": config.extra_args,
        "flag_overrides": config.flag_overrides,
        "env_vars": config.env_vars,
        "nesting_guard_vars": config.nesting_guard_vars,
    }
    CONFIG_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def config_exists() -> bool:
    """Check if the config file exists."""
    return CONFIG_FILE.is_file()


def resolve_flag(config: AgentConfig, canonical_flag: str) -> str | None:
    """Look up a canonical flag in flag_overrides.

    Returns the override value, None if suppressed (value is null/None),
    or the original flag unchanged if no override exists.
    """
    if canonical_flag in config.flag_overrides:
        return config.flag_overrides[canonical_flag]
    return canonical_flag


def build_base_command(config: AgentConfig) -> list[str]:
    """Return [config.command] + config.extra_args."""
    return [config.command] + list(config.extra_args)


def build_env(config: AgentConfig) -> dict[str, str]:
    """Build subprocess env: strip nesting guard vars, merge env_vars."""
    env = os.environ.copy()
    for var in config.nesting_guard_vars:
        env.pop(var, None)
    env.update(config.env_vars)
    return env


def smoke_test(config: AgentConfig) -> tuple[bool, str]:
    """Run a quick test to verify the CLI agent works.

    Runs: <cmd> <extra_args> -p "Say ok" --output-format stream-json
    Checks exit code 0 and that output contains a JSON line with "type": "result".
    """
    cmd = build_base_command(config)

    p_flag = resolve_flag(config, "-p")
    if p_flag:
        cmd.extend([p_flag, "Say ok"])
    else:
        cmd.append("Say ok")

    of_flag = resolve_flag(config, "--output-format")
    if of_flag:
        cmd.extend([of_flag, "stream-json"])

    cmd_str = " ".join(cmd)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
            env=build_env(config),
        )
    except FileNotFoundError:
        return False, f"Command not found: {config.command} (ran: {cmd_str})"
    except subprocess.TimeoutExpired:
        return False, f"Smoke test timed out after 60s (ran: {cmd_str})"

    if proc.returncode != 0:
        stderr_snippet = proc.stderr.strip()[:200] if proc.stderr else "(no stderr)"
        return False, f"Exited with code {proc.returncode}: {stderr_snippet}"

    # Check for a "type": "result" JSON line
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
            if event.get("type") == "result":
                return True, "Smoke test passed."
        except json.JSONDecodeError:
            continue

    return False, "No result event found in output."


def run_setup_wizard() -> AgentConfig:
    """Interactive first-run setup wizard. Shows current values as defaults when editing."""
    existing = load_config() if config_exists() else AgentConfig()
    is_edit = config_exists()

    print("=" * 50)
    if is_edit:
        print("  Skill Iterator — Edit Configuration")
    else:
        print("  Skill Iterator — First-Run Setup")
    print("=" * 50)
    print()
    print("This wizard configures which CLI agent command to use.")
    print("Press Enter to keep the current value.\n")

    # 1. Command
    default_cmd = existing.command
    cmd_input = input(f"CLI command [{default_cmd}]: ").strip()
    command = cmd_input if cmd_input else default_cmd

    # 2. Check if command exists on PATH
    found = shutil.which(command)
    if found:
        print(f"  Found: {found}")
    else:
        print(f"  Warning: '{command}' not found on PATH.")
        proceed = input("  Continue anyway? (y/n) [y]: ").strip().lower()
        if proceed == "n":
            print("  Aborting setup.")
            return run_setup_wizard()  # restart

    # 3. Extra args
    default_args = " ".join(existing.extra_args) if existing.extra_args else "none"
    args_input = input(f"\nExtra args (e.g. --team eng) [{default_args}]: ").strip()
    if args_input:
        extra_args = shlex.split(args_input)
    elif is_edit:
        extra_args = list(existing.extra_args)
    else:
        extra_args = []

    config = AgentConfig(command=command, extra_args=extra_args)

    # 4. Smoke test
    print("\n[Running smoke test...]")
    passed, message = smoke_test(config)
    if passed:
        print(f"  {message}")
    else:
        print(f"  FAILED: {message}")
        choice = input("  (r)econfigure or (s)ave anyway? [r]: ").strip().lower()
        if choice != "s":
            return run_setup_wizard()  # restart

    # 5. Save
    save_config(config)
    print(f"\nConfig saved to {CONFIG_FILE}")
    return config
