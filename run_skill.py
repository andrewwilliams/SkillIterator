#!/usr/bin/env python3
"""
Interactive Skill Evaluator — define a skill, test it on a real project,
collect freeform feedback, derive evaluation criteria, and iterate.

Usage:
    python3 run_skill.py          # Normal: exploratory first run
    python3 run_skill.py --eval   # Skip to evaluation: provide expectations upfront
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
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
SKILLS_DIR = Path.home() / ".claude" / "skills"


def discover_skills() -> list[dict[str, str]]:
    """Scan ~/.claude/skills/ for SKILL.md files and parse their frontmatter."""
    skills: list[dict[str, str]] = []
    if not SKILLS_DIR.is_dir():
        return skills
    for skill_dir in sorted(SKILLS_DIR.iterdir()):
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.is_file():
            continue
        content = skill_file.read_text()
        # Parse YAML frontmatter (between --- delimiters)
        fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
        name = skill_dir.name
        description = ""
        if fm_match:
            for line in fm_match.group(1).splitlines():
                if line.startswith("name:"):
                    name = line.split(":", 1)[1].strip()
                elif line.startswith("description:"):
                    description = line.split(":", 1)[1].strip()
        skills.append({
            "name": name,
            "description": description,
            "content": content,
            "path": str(skill_file),
        })
    return skills


def select_skill(skills: list[dict[str, str]]) -> str:
    """Present available skills and return the chosen skill content."""
    while True:
        if skills:
            print("Available skills:")
            for i, s in enumerate(skills, 1):
                desc = f" — {s['description'][:70]}" if s["description"] else ""
                print(f"  {i}. {s['name']}{desc}")
            print(f"  {len(skills) + 1}. Custom (enter manually)")
            choice = input(f"\nSelect skill [1-{len(skills) + 1}]: ").strip()
            if choice.isdigit():
                idx = int(choice)
                if 1 <= idx <= len(skills):
                    selected = skills[idx - 1]
                    print(f"\nLoaded: {selected['name']}")
                    return selected["content"]
                elif idx == len(skills) + 1:
                    # Fall through to manual entry
                    pass
                else:
                    print(f"Invalid choice. Enter a number from 1 to {len(skills) + 1}.\n")
                    continue
            else:
                print(f"Invalid choice. Enter a number from 1 to {len(skills) + 1}.\n")
                continue

        # Manual entry (either no skills found or user chose Custom)
        skill = get_multiline_input("\nSkill (system prompt — end with blank line):")
        if skill.strip():
            return skill
        print("Error: skill cannot be empty.\n")


def check_prerequisites() -> str | None:
    """Return an error message if prerequisites aren't met, else None."""
    if not shutil.which("claude"):
        return "'claude' CLI not found on PATH. Install it first."
    return None


def validate_project_dir(project_dir: Path, *, skip_clean_check: bool = False) -> str | None:
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
    if not skip_clean_check:
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


def collect_and_derive_expectations(
    feedback: str, project_dir: Path, task_prompt: str
) -> tuple[list[FileExpectation], list[CommandExpectation]]:
    """Derive expectations from feedback, show them, and prompt for acceptance."""
    print("\n[Deriving expectations from feedback...]")
    file_exps, cmd_exps = derive_expectations(
        feedback, str(project_dir), task_prompt
    )

    if not file_exps and not cmd_exps:
        print("Could not derive expectations.")
        return [], []

    show_expectations(file_exps, cmd_exps)

    accept = input("\nAccept? (y/n): ").strip().lower()
    if accept != "y":
        print("Expectations rejected.")
        return [], []

    return file_exps, cmd_exps


def derive_skill_update(
    current_skill: str,
    feedback: str,
    task_prompt: str,
    checks: list[CheckResult] | None = None,
) -> str | None:
    """Call Claude to propose a revised skill based on feedback and evaluation results."""
    eval_context = ""
    if checks:
        results = []
        for c in checks:
            icon = "PASS" if c.passed else "FAIL"
            results.append(f"  [{icon}] {c.message}")
            if c.details and not c.passed:
                for line in c.details.splitlines()[:2]:
                    results.append(f"         {line}")
        eval_context = "\n\nAutomated evaluation results:\n" + "\n".join(results)

    revision_prompt = f"""You are revising a "skill" — a system prompt that instructs Claude Code to perform a specific coding task. The skill was tested and the output had issues.

Current skill:
---
{current_skill}
---

Task that was given to Claude:
{task_prompt}

User feedback on Claude's output:
{feedback}{eval_context}

Revise the skill to address the feedback. Guidelines:
- Make TARGETED changes — only modify/add what's needed to fix the reported issues.
- Preserve all instructions that are working correctly.
- Be specific and prescriptive (e.g., "Always use @Test macro, never use XCTest" not "use the correct testing framework").
- When Claude used wrong patterns, add explicit "DO: ... / DO NOT: ..." rules.
- Don't add unnecessary verbosity or redundant instructions.
- Preserve any YAML frontmatter (--- delimited) exactly as-is.

Return ONLY the complete revised skill text. No explanation, no markdown fences, no preamble."""

    try:
        proc = subprocess.run(
            ["claude", "-p", revision_prompt],
            capture_output=True,
            text=True,
            timeout=90,
            env=_build_clean_env(),
        )

        revised = proc.stdout.strip()
        if not revised:
            return None

        # Strip markdown fences if Claude wrapped them
        if revised.startswith("```"):
            lines = revised.split("\n")
            lines = [l for l in lines if not l.startswith("```")]
            revised = "\n".join(lines)

        # If unchanged, return None
        if revised.strip() == current_skill.strip():
            return None

        return revised

    except subprocess.TimeoutExpired:
        print("\nError: skill revision call timed out.")
        return None
    except Exception as e:
        print(f"\nError during skill revision: {e}")
        return None


def show_skill_diff(old_skill: str, new_skill: str) -> None:
    """Display a unified diff of old vs new skill."""
    old_lines = old_skill.splitlines(keepends=True)
    new_lines = new_skill.splitlines(keepends=True)
    diff = list(difflib.unified_diff(
        old_lines, new_lines,
        fromfile="current skill", tofile="proposed skill",
    ))
    if diff:
        for line in diff:
            # Colorize: green for additions, red for removals
            if line.startswith("+") and not line.startswith("+++"):
                print(f"\033[32m{line}\033[0m", end="")
            elif line.startswith("-") and not line.startswith("---"):
                print(f"\033[31m{line}\033[0m", end="")
            elif line.startswith("@@"):
                print(f"\033[36m{line}\033[0m", end="")
            else:
                print(line, end="")
        print()  # Ensure trailing newline
    else:
        print("(no visible changes)")


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
    parser = argparse.ArgumentParser(description="Interactive Skill Evaluator")
    parser.add_argument("--eval", action="store_true",
                        help="Skip exploratory run — provide expectations upfront")
    args = parser.parse_args()

    print("=== Skill Evaluator ===\n")

    # --- Prerequisites ---
    prereq_err = check_prerequisites()
    if prereq_err:
        print(f"Error: {prereq_err}")
        return 1

    # --- Collect inputs ---
    skills = discover_skills()
    skill = select_skill(skills)

    while True:
        task_prompt = get_multiline_input("\nTask prompt (end with blank line):")
        if task_prompt.strip():
            break
        print("Error: task prompt cannot be empty.\n")

    while True:
        project_dir_str = input("\nProject directory: ").strip()
        if not project_dir_str:
            print("Error: please enter a directory path.\n")
            continue
        project_dir = Path(os.path.expanduser(project_dir_str)).resolve()
        if not project_dir.is_dir():
            print(f"Error: {project_dir} is not a directory.\n")
            continue
        dir_err = validate_project_dir(project_dir, skip_clean_check=args.eval)
        if dir_err:
            print(f"Error: {dir_err}\n")
            continue
        break

    interactive_input = input("\nInteractive mode? (y/n) [n]: ").strip().lower()
    interactive = interactive_input == "y"
    if interactive:
        print("Claude will run interactively — you can answer questions and approve plans.")
    else:
        print("Claude will run headless (non-interactive).")

    branch = get_git_branch(project_dir)
    print(f"\nProject: {project_dir}")
    print(f"Branch:  {branch}")

    # --- Pre-loop expectations (--eval mode) ---
    file_exps: list[FileExpectation] = []
    cmd_exps: list[CommandExpectation] = []

    if args.eval:
        # Compute diffs from the dirty working tree using ClaudeGym's snapshot/diff logic
        gym = ClaudeGym(work_dir=project_dir)
        after = gym._snapshot_directory()
        # Stash changes to get the clean baseline, then restore
        subprocess.run(["git", "stash", "--include-untracked"], cwd=str(project_dir),
                        capture_output=True, timeout=30)
        before = gym._snapshot_directory()
        subprocess.run(["git", "stash", "pop"], cwd=str(project_dir),
                        capture_output=True, timeout=30)
        eval_diffs = gym._compute_diffs(before, after)

        if eval_diffs:
            show_file_changes(eval_diffs)
            from diff_server import present_diff_for_review
            feedback = present_diff_for_review(eval_diffs)
        else:
            print("\nNo uncommitted changes found.")
            feedback = get_multiline_input("\nFeedback (or 'done'):")

        if feedback.strip().lower() != "done" and feedback.strip():
            file_exps, cmd_exps = collect_and_derive_expectations(
                feedback, project_dir, task_prompt
            )

    # --- Loop state ---
    run_number = 0
    created_files: list[str] = []
    modified_files: list[str] = []

    # Track existing dirty-tree changes so Run 1 can revert them in --eval mode
    if args.eval and eval_diffs:
        for diff in eval_diffs:
            if diff.status == "added":
                created_files.append(diff.path)
            elif diff.status == "modified":
                modified_files.append(diff.path)

    while True:
        run_number += 1
        has_expectations = bool(file_exps or cmd_exps)

        # Revert previous changes before re-running (including --eval dirty tree on Run 1)
        if run_number > 1 or (args.eval and (created_files or modified_files)):
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
            debug_mode=not interactive,
            interactive=interactive,
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
        if not interactive:
            print(f"\nResponse: {turn.result_text[:500]}{'...' if len(turn.result_text) > 500 else ''}")
            print(f"Cost: ${turn.cost_usd:.4f} | Duration: {turn.duration:.1f}s | Turns: {turn.num_turns}")
        else:
            print(f"\nSession duration: {turn.duration:.1f}s")

        if turn.is_error:
            print("*** ERROR in Claude response ***")

        # Auto-evaluate if we have expectations
        checks: list[CheckResult] = []
        if has_expectations:
            checks = run_evaluation(project_dir, file_exps, cmd_exps)
            print_evaluation(checks)

        # Collect feedback via web diff review UI or terminal
        if turn.file_diffs:
            from diff_server import present_diff_for_review
            feedback = present_diff_for_review(turn.file_diffs)
        else:
            feedback = get_multiline_input("\nFeedback (or 'done'):")

        if feedback.strip().lower() == "done":
            print("\nDone.")
            break

        if not feedback.strip():
            print("No feedback provided, running again with current settings...")
            continue

        # Derive expectations from feedback
        new_file_exps, new_cmd_exps = collect_and_derive_expectations(
            feedback, project_dir, task_prompt
        )

        if new_file_exps or new_cmd_exps:
            file_exps = new_file_exps
            cmd_exps = new_cmd_exps
        else:
            print("Running again with existing expectations...")

        # Propose skill update based on feedback
        print("\n[Proposing skill update...]")
        revised_skill = derive_skill_update(
            skill, feedback, task_prompt, checks or None
        )
        if revised_skill:
            print("\nProposed skill changes:")
            show_skill_diff(skill, revised_skill)
            accept = input("\nApply skill update? (y/n): ").strip().lower()
            if accept == "y":
                skill = revised_skill
                print("Skill updated for next run.")
            else:
                print("Skill unchanged.")
        else:
            print("No skill changes proposed.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
