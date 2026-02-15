#!/usr/bin/env python3
"""
Interactive Skill Evaluator — define a skill, test it on a real project,
collect freeform feedback, derive evaluation criteria, and iterate.

Usage:
    python3 run_skill.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from claude_gym import ClaudeGym, FileDiff
from evaluator import (
    CheckResult,
    ClaudeEvaluator,
    CommandExpectation,
    FileExpectation,
)


SCRIPT_DIR = Path(__file__).resolve().parent


def check_prerequisites() -> str | None:
    """Return an error message if prerequisites aren't met, else None."""
    if not shutil.which("claude"):
        return "'claude' CLI not found on PATH. Install it first."
    return None


def validate_project_dir(project_dir: Path) -> str | None:
    """Return an error message if the project dir is unsafe, else None."""
    # Block dangerous directories
    home = Path.home().resolve()
    dangerous = {Path("/").resolve(), home}
    if project_dir in dangerous:
        return f"Refusing to run in {project_dir} — too dangerous."

    # Don't run inside the evaluator's own repo
    if project_dir == SCRIPT_DIR or SCRIPT_DIR.is_relative_to(project_dir):
        return (
            f"Project dir ({project_dir}) contains the evaluator itself. "
            "Use a different project."
        )

    # Must be a git repo
    git_dir = project_dir / ".git"
    if not git_dir.is_dir():
        return f"{project_dir} is not a git repository. Only git-tracked projects are supported."

    # Working tree must be clean (no uncommitted changes)
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return f"Could not run git status: {e}"

    if status.stdout.strip():
        dirty_count = len(status.stdout.strip().splitlines())
        return (
            f"Working tree has {dirty_count} uncommitted change(s). "
            "Commit or stash them first so we can safely revert between runs.\n"
            "  Hint: git stash  OR  git commit -am 'wip'"
        )

    return None


def get_git_branch(project_dir: Path) -> str:
    """Return the current branch name, or 'detached HEAD' / 'unknown'."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=5,
        )
        branch = result.stdout.strip()
        return branch if branch else "unknown"
    except Exception:
        return "unknown"


def get_multiline_input(prompt: str) -> str:
    """Read multi-line input terminated by a blank line or EOF."""
    print(prompt)
    lines: list[str] = []
    while True:
        try:
            line = input("> ")
        except EOFError:
            break
        if line == "":
            break
        lines.append(line)
    return "\n".join(lines)


def show_file_changes(diffs: list[FileDiff]) -> None:
    """Print a summary of file changes."""
    if not diffs:
        print("\nNo files changed.")
        return
    print("\nFiles changed:")
    for d in diffs:
        symbol = {"added": "+", "modified": "~", "deleted": "-"}.get(d.status, "?")
        print(f"  {symbol} {d.path} ({d.status})")


def revert_changes(
    project_dir: Path, created_files: list[str], modified_files: list[str]
) -> None:
    """Revert changes: delete created files, git-checkout modified files."""
    for rel_path in created_files:
        fpath = project_dir / rel_path
        if fpath.exists():
            fpath.unlink()
            print(f"  Deleted: {rel_path}")
        # Remove empty parent dirs up to project_dir
        parent = fpath.parent
        while parent != project_dir:
            try:
                parent.rmdir()
                print(f"  Removed empty dir: {parent.relative_to(project_dir)}")
                parent = parent.parent
            except OSError:
                break

    if modified_files:
        try:
            subprocess.run(
                ["git", "checkout", "--"] + modified_files,
                cwd=str(project_dir),
                capture_output=True,
                text=True,
                check=True,
            )
            for f in modified_files:
                print(f"  Restored: {f}")
        except subprocess.CalledProcessError as e:
            print(f"  Warning: git checkout failed: {e.stderr.strip()}")


def _build_clean_env() -> dict[str, str]:
    """Build an env dict that strips Claude nesting-guard variables."""
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)
    return env


def derive_expectations(
    feedback: str, project_dir: str, task_prompt: str
) -> tuple[list[FileExpectation], list[CommandExpectation]]:
    """Call Claude to convert freeform feedback into structured expectations."""
    derivation_prompt = f"""You are converting user feedback about a coding task into structured JSON expectations.

The task was run in project directory: {project_dir}
The task prompt was:
{task_prompt}

The user's feedback on the result:
{feedback}

Convert this into a JSON object with two arrays:

{{
  "file_expectations": [
    {{
      "path": "relative/path/to/file.ext",
      "should_exist": true,
      "content_contains": ["string1", "string2"],
      "content_not_contains": ["bad_string"],
      "content_matches": [],
      "min_lines": null,
      "max_lines": null
    }}
  ],
  "command_expectations": [
    {{
      "command": ["swift", "build", "--build-tests"],
      "returncode": 0,
      "stdout_contains": [],
      "timeout": 60
    }}
  ]
}}

Rules:
- Infer file paths from the task prompt and feedback context.
- Only include fields relevant to the feedback.
- content_contains: exact substrings to find.
- content_not_contains: exact substrings that must be absent.
- content_matches: valid Python regex patterns.
- command: full command as a list of strings.
- Use reasonable timeouts (default 30s, build commands 120s).

Return ONLY the raw JSON object. No markdown fences, no explanation."""

    try:
        proc = subprocess.run(
            ["claude", "-p", derivation_prompt],
            capture_output=True,
            text=True,
            timeout=60,
            env=_build_clean_env(),
        )

        text = proc.stdout.strip()

        # Strip markdown fences if Claude wrapped them
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [l for l in lines if not l.startswith("```")]
            text = "\n".join(lines)

        data = json.loads(text)

        file_exps: list[FileExpectation] = []
        for fe in data.get("file_expectations", []):
            file_exps.append(
                FileExpectation(
                    path=fe["path"],
                    should_exist=fe.get("should_exist", True),
                    content_contains=fe.get("content_contains", []),
                    content_matches=fe.get("content_matches", []),
                    content_not_contains=fe.get("content_not_contains", []),
                    min_lines=fe.get("min_lines"),
                    max_lines=fe.get("max_lines"),
                )
            )

        cmd_exps: list[CommandExpectation] = []
        for ce in data.get("command_expectations", []):
            cmd_exps.append(
                CommandExpectation(
                    command=ce["command"],
                    returncode=ce.get("returncode", 0),
                    stdout_contains=ce.get("stdout_contains", []),
                    timeout=ce.get("timeout", 30),
                )
            )

        return file_exps, cmd_exps

    except subprocess.TimeoutExpired:
        print("\nError: derivation call timed out.")
        return [], []
    except (json.JSONDecodeError, KeyError) as e:
        print(f"\nError parsing derived expectations: {e}")
        if proc.stdout.strip():
            print(f"Raw output:\n{proc.stdout.strip()[:500]}")
        return [], []


def show_expectations(
    file_exps: list[FileExpectation], cmd_exps: list[CommandExpectation]
) -> None:
    """Display derived expectations for user review."""
    print("\nDerived expectations:")
    for fe in file_exps:
        exist_str = "exists" if fe.should_exist else "does not exist"
        print(f"  [+] File: {fe.path} {exist_str}")
        if fe.content_contains:
            print(f"      Contains: {', '.join(fe.content_contains)}")
        if fe.content_not_contains:
            print(f"      Not contains: {', '.join(fe.content_not_contains)}")
        if fe.content_matches:
            print(f"      Matches: {', '.join(fe.content_matches)}")
        if fe.min_lines is not None:
            print(f"      Min lines: {fe.min_lines}")
        if fe.max_lines is not None:
            print(f"      Max lines: {fe.max_lines}")
    for ce in cmd_exps:
        cmd_str = " ".join(ce.command)
        print(f"  [+] Command: {cmd_str} returns {ce.returncode}")
        if ce.stdout_contains:
            print(f"      Stdout contains: {', '.join(ce.stdout_contains)}")


def run_evaluation(
    project_dir: Path,
    file_exps: list[FileExpectation],
    cmd_exps: list[CommandExpectation],
) -> list[CheckResult]:
    """Run expectation checks against the current project state."""
    gym = ClaudeGym(work_dir=project_dir)
    evaluator = ClaudeEvaluator()
    checks: list[CheckResult] = []
    checks.extend(evaluator._verify_file_expectations(gym, file_exps))
    checks.extend(evaluator._verify_command_expectations(gym, cmd_exps))
    return checks


def print_evaluation(checks: list[CheckResult]) -> None:
    """Print pass/fail report."""
    print("\nEvaluation:")
    passed = 0
    for check in checks:
        icon = "+" if check.passed else "-"
        print(f"  [{icon}] {check.message}")
        if check.details and not check.passed:
            for line in check.details.splitlines()[:3]:
                print(f"      {line}")
        if check.passed:
            passed += 1
    total = len(checks)
    print(f"\nResults: {passed}/{total} checks passed")


def main() -> int:
    print("=== Skill Evaluator ===\n")

    # --- Prerequisites ---
    prereq_err = check_prerequisites()
    if prereq_err:
        print(f"Error: {prereq_err}")
        return 1

    # --- Collect inputs ---
    skill = get_multiline_input("Skill (system prompt — end with blank line):")
    if not skill.strip():
        print("Error: skill cannot be empty.")
        return 1

    task_prompt = get_multiline_input("\nTask prompt (end with blank line):")
    if not task_prompt.strip():
        print("Error: task prompt cannot be empty.")
        return 1

    project_dir_str = input("\nProject directory: ").strip()
    project_dir = Path(os.path.expanduser(project_dir_str)).resolve()
    if not project_dir.is_dir():
        print(f"Error: {project_dir} is not a directory.")
        return 1

    # --- Validate project directory ---
    dir_err = validate_project_dir(project_dir)
    if dir_err:
        print(f"Error: {dir_err}")
        return 1

    branch = get_git_branch(project_dir)
    print(f"\nProject: {project_dir}")
    print(f"Branch:  {branch}")

    # --- Loop state ---
    file_exps: list[FileExpectation] = []
    cmd_exps: list[CommandExpectation] = []
    run_number = 0
    created_files: list[str] = []
    modified_files: list[str] = []

    while True:
        run_number += 1
        has_expectations = bool(file_exps or cmd_exps)

        # Revert previous changes before re-running
        if run_number > 1:
            print("\n[Reverting previous changes...]")
            revert_changes(project_dir, created_files, modified_files)
            created_files = []
            modified_files = []

        label = "(evaluated)" if has_expectations else "(exploratory)"
        print(f"\n--- Run {run_number} {label} ---")

        # Run Claude in the real project
        gym = ClaudeGym(
            work_dir=project_dir,
            system_prompt=skill,
            debug_mode=True,
        )
        turn = gym.send_prompt(task_prompt)

        # Track changes for revert on next iteration
        for diff in turn.file_diffs:
            if diff.status == "added":
                created_files.append(diff.path)
            elif diff.status == "modified":
                modified_files.append(diff.path)

        # Show results
        show_file_changes(turn.file_diffs)
        print(f"\nResponse: {turn.result_text[:500]}{'...' if len(turn.result_text) > 500 else ''}")
        print(f"Cost: ${turn.cost_usd:.4f} | Duration: {turn.duration:.1f}s | Turns: {turn.num_turns}")

        if turn.is_error:
            print("*** ERROR in Claude response ***")

        # Auto-evaluate if we have expectations
        if has_expectations:
            checks = run_evaluation(project_dir, file_exps, cmd_exps)
            print_evaluation(checks)

        # Collect feedback
        feedback = get_multiline_input("\nFeedback (or 'done'):")
        if feedback.strip().lower() == "done":
            print("\nDone.")
            break

        if not feedback.strip():
            print("No feedback provided, running again with current settings...")
            continue

        # Derive expectations from feedback
        print("\n[Deriving expectations from feedback...]")
        new_file_exps, new_cmd_exps = derive_expectations(
            feedback, str(project_dir), task_prompt
        )

        if not new_file_exps and not new_cmd_exps:
            print("Could not derive expectations. Running again with existing ones...")
            continue

        file_exps = new_file_exps
        cmd_exps = new_cmd_exps

        show_expectations(file_exps, cmd_exps)

        accept = input("\nAccept? (y/n): ").strip().lower()
        if accept != "y":
            print("Expectations rejected. Provide new feedback on the next run.")
            file_exps = []
            cmd_exps = []

    return 0


if __name__ == "__main__":
    sys.exit(main())
