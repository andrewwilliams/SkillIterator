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
import uuid
from pathlib import Path

import log
from claude_gym import ClaudeGym, FileDiff
from config import (
    AgentConfig,
    add_recent_project,
    build_base_command,
    build_env,
    config_exists,
    load_config,
    load_recent_projects,
    remove_recent_project,
    resolve_flag,
    run_setup_wizard,
)
from evaluator import (
    CheckResult,
    ClaudeEvaluator,
    CommandExpectation,
    DiffExpectation,
    FileExpectation,
)


SCRIPT_DIR = Path(__file__).resolve().parent
SKILLS_DIR = Path.home() / ".claude" / "skills"
SESSION_FILE = Path.home() / ".skilliterator" / "session.json"
MAX_INPUT_LINES = 500


def save_session(
    skill: str, task_prompt: str, project_dir: str,
    file_exps: list[FileExpectation], cmd_exps: list[CommandExpectation],
    diff_exps: list[DiffExpectation], run_number: int,
) -> None:
    """Persist session state so a crashed/interrupted run can be resumed."""
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "session_id": str(uuid.uuid4()),
        "skill": skill,
        "task_prompt": task_prompt,
        "project_dir": project_dir,
        "run_number": run_number,
        "file_expectations": [
            {k: v for k, v in fe.__dict__.items()} for fe in file_exps
        ],
        "command_expectations": [
            {k: v for k, v in ce.__dict__.items()} for ce in cmd_exps
        ],
        "diff_expectations": [
            {k: v for k, v in de.__dict__.items() if not k.startswith("_")} for de in diff_exps
        ],
    }
    SESSION_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def load_session() -> dict | None:
    """Load saved session if it exists."""
    if not SESSION_FILE.is_file():
        return None
    try:
        return json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def delete_session() -> None:
    """Remove saved session file."""
    try:
        SESSION_FILE.unlink(missing_ok=True)
    except OSError:
        pass


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


def check_prerequisites(config: AgentConfig | None = None) -> str | None:
    """Return an error message if prerequisites aren't met, else None."""
    command = config.command if config else "claude"
    if not shutil.which(command):
        return f"'{command}' CLI not found on PATH. Install it first."
    return None


def validate_project_dir(project_dir: Path, *, skip_clean_check: bool = False) -> str | None:
    """Return an error message if the project dir is unsafe, else None."""
    # Block dangerous directories
    home = Path.home().resolve()
    dangerous = {Path("/").resolve(), home}
    # Block system directories
    for sys_dir in ["/var", "/etc", "/usr", "/System", "/Library", "/Applications"]:
        p = Path(sys_dir)
        if p.exists():
            dangerous.add(p.resolve())
    # Block parent directories of home (e.g. /Users)
    for parent in home.parents:
        if parent != Path("/").resolve():
            dangerous.add(parent)
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
        if len(lines) >= MAX_INPUT_LINES:
            print(f"  (Input truncated at {MAX_INPUT_LINES} lines)")
            break
    return "\n".join(lines)


def show_file_changes(diffs: list[FileDiff]) -> None:
    """Print a summary of file changes."""
    if not diffs:
        print("\nNo files changed.")
        return
    print("\nFiles changed:")
    for d in diffs:
        symbol = {"added": "+", "modified": "~", "deleted": "-", "renamed": "R", "copied": "C", "type_changed": "T"}.get(d.status, "?")
        suffix = f" (from {d.old_path})" if d.old_path else ""
        print(f"  {symbol} {d.path} ({d.status}){suffix}")


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


def derive_expectations(
    feedback: str, project_dir: str, task_prompt: str,
    config: AgentConfig | None = None,
) -> tuple[list[FileExpectation], list[CommandExpectation], list[DiffExpectation]]:
    """Call Claude to convert freeform feedback into structured expectations."""
    derivation_prompt = f"""You are converting user feedback about a coding task into structured JSON expectations.

The task was run in project directory: {project_dir}
The task prompt was:
{task_prompt}

The user's feedback on the result:
{feedback}

Convert this into a JSON object with three arrays:

{{
  "file_expectations": [
    {{
      "path": "relative/path/to/file.ext",
      "path_pattern": "",
      "should_exist": true,
      "content_contains": ["string1", "string2"],
      "content_not_contains": ["bad_string"],
      "content_matches": [],
      "min_lines": null,
      "max_lines": null,
      "min_matching_files": null
    }}
  ],
  "command_expectations": [
    {{
      "command": ["swift", "build", "--build-tests"],
      "returncode": 0,
      "stdout_contains": [],
      "stdout_not_contains": [],
      "stderr_contains": [],
      "stderr_not_contains": [],
      "timeout": 60
    }}
  ],
  "diff_expectations": [
    {{
      "allowed_statuses": ["added"],
      "allowed_path_patterns": ["Tests/**/*.swift"],
      "disallowed_path_patterns": [],
      "min_files_changed": null,
      "max_files_changed": null,
      "must_include_paths": []
    }}
  ]
}}

Rules:
- Infer file paths from the task prompt and feedback context.
- Only include fields relevant to the feedback.
- "path" and "path_pattern" are mutually exclusive. Use "path" for a specific file, "path_pattern" for a glob pattern (supports **, *, ?). When using "path_pattern", set "min_matching_files" to the expected minimum number of matches (default 1 if omitted).
- content_contains: exact substrings to find.
- content_not_contains: exact substrings that must be absent.
- content_matches: valid Python regex patterns.
- command: full command as a list of strings.
- stdout_not_contains: substrings that must NOT appear in stdout.
- stderr_contains / stderr_not_contains: check stderr output (e.g. for compiler warnings).
- diff_expectations: constraints on what files were changed. Use when feedback says things like "should only add files", "should only change files under X/", "must not modify Y".
  - allowed_statuses: restrict diffs to these statuses (e.g. ["added", "modified"]).
  - allowed_path_patterns: every changed file must match at least one pattern.
  - disallowed_path_patterns: no changed file may match any of these patterns.
  - must_include_paths: specific paths that must appear in the diffs.
- Use reasonable timeouts (default 30s, build commands 120s).
- Omit arrays/fields that aren't relevant to the feedback.

Return ONLY the raw JSON object. No markdown fences, no explanation."""

    try:
        cfg = config or AgentConfig()
        cmd = list(build_base_command(cfg))
        p_flag = resolve_flag(cfg, "-p")
        if p_flag:
            cmd.extend([p_flag, derivation_prompt])
        else:
            cmd.append(derivation_prompt)

        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
            env=build_env(cfg),
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
                    path=fe.get("path", ""),
                    path_pattern=fe.get("path_pattern", ""),
                    should_exist=fe.get("should_exist", True),
                    content_contains=fe.get("content_contains", []),
                    content_matches=fe.get("content_matches", []),
                    content_not_contains=fe.get("content_not_contains", []),
                    min_lines=fe.get("min_lines"),
                    max_lines=fe.get("max_lines"),
                    min_matching_files=fe.get("min_matching_files"),
                )
            )

        cmd_exps: list[CommandExpectation] = []
        for ce in data.get("command_expectations", []):
            cmd_exps.append(
                CommandExpectation(
                    command=ce["command"],
                    returncode=ce.get("returncode", 0),
                    stdout_contains=ce.get("stdout_contains", []),
                    stdout_not_contains=ce.get("stdout_not_contains", []),
                    stderr_contains=ce.get("stderr_contains", []),
                    stderr_not_contains=ce.get("stderr_not_contains", []),
                    timeout=ce.get("timeout", 30),
                )
            )

        diff_exps: list[DiffExpectation] = []
        for de in data.get("diff_expectations", []):
            diff_exps.append(
                DiffExpectation(
                    allowed_statuses=de.get("allowed_statuses", []),
                    allowed_path_patterns=de.get("allowed_path_patterns", []),
                    disallowed_path_patterns=de.get("disallowed_path_patterns", []),
                    min_files_changed=de.get("min_files_changed"),
                    max_files_changed=de.get("max_files_changed"),
                    must_include_paths=de.get("must_include_paths", []),
                )
            )

        return file_exps, cmd_exps, diff_exps

    except subprocess.TimeoutExpired:
        print(f"\nError: derivation call timed out after 60s (prompt was {len(derivation_prompt)} chars).")
        return [], [], []
    except (json.JSONDecodeError, KeyError) as e:
        print(f"\nError parsing derived expectations: {e}")
        try:
            raw = proc.stdout.strip()  # type: ignore[possibly-undefined]
            if raw:
                print(f"Raw output (first 500 chars):\n{raw[:500]}")
        except NameError:
            pass  # proc was never assigned
        return [], [], []


def show_expectations(
    file_exps: list[FileExpectation],
    cmd_exps: list[CommandExpectation],
    diff_exps: list[DiffExpectation] | None = None,
) -> None:
    """Display derived expectations for user review."""
    print("\nDerived expectations:")
    for fe in file_exps:
        if fe.path_pattern:
            min_f = fe.min_matching_files if fe.min_matching_files is not None else 1
            print(f"  [+] File pattern: {fe.path_pattern} (>= {min_f} match(es))")
        else:
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
        if ce.stdout_not_contains:
            print(f"      Stdout excludes: {', '.join(ce.stdout_not_contains)}")
        if ce.stderr_contains:
            print(f"      Stderr contains: {', '.join(ce.stderr_contains)}")
        if ce.stderr_not_contains:
            print(f"      Stderr excludes: {', '.join(ce.stderr_not_contains)}")
    for de in (diff_exps or []):
        print(f"  [+] Diff constraint:")
        if de.allowed_statuses:
            print(f"      Allowed statuses: {', '.join(de.allowed_statuses)}")
        if de.allowed_path_patterns:
            print(f"      Allowed paths: {', '.join(de.allowed_path_patterns)}")
        if de.disallowed_path_patterns:
            print(f"      Disallowed paths: {', '.join(de.disallowed_path_patterns)}")
        if de.min_files_changed is not None:
            print(f"      Min files changed: {de.min_files_changed}")
        if de.max_files_changed is not None:
            print(f"      Max files changed: {de.max_files_changed}")
        if de.must_include_paths:
            print(f"      Must include: {', '.join(de.must_include_paths)}")


def _edit_expectations(
    file_exps: list[FileExpectation],
    cmd_exps: list[CommandExpectation],
    diff_exps: list[DiffExpectation],
) -> tuple[list[FileExpectation], list[CommandExpectation], list[DiffExpectation]]:
    """Let user delete individual expectations by number."""
    # Build a numbered list of all expectations
    items: list[tuple[str, str, int]] = []  # (type, label, index_in_list)
    for i, fe in enumerate(file_exps):
        label = f"File: {fe.path or fe.path_pattern}"
        items.append(("file", label, i))
    for i, ce in enumerate(cmd_exps):
        label = f"Command: {' '.join(ce.command)}"
        items.append(("cmd", label, i))
    for i, de in enumerate(diff_exps):
        label = f"Diff constraint"
        items.append(("diff", label, i))

    print("\nExpectations (enter numbers to remove, comma-separated):")
    for idx, (_, label, _) in enumerate(items, 1):
        print(f"  {idx}. {label}")

    to_remove = input("\nRemove (e.g. 1,3) or Enter to keep all: ").strip()
    if not to_remove:
        return file_exps, cmd_exps, diff_exps

    try:
        remove_indices = {int(x.strip()) - 1 for x in to_remove.split(",") if x.strip()}
    except ValueError:
        print("Invalid input, keeping all expectations.")
        return file_exps, cmd_exps, diff_exps

    # Track which indices to remove per type
    file_remove = set()
    cmd_remove = set()
    diff_remove = set()
    for idx in remove_indices:
        if 0 <= idx < len(items):
            typ, _, list_idx = items[idx]
            if typ == "file":
                file_remove.add(list_idx)
            elif typ == "cmd":
                cmd_remove.add(list_idx)
            elif typ == "diff":
                diff_remove.add(list_idx)

    new_file = [e for i, e in enumerate(file_exps) if i not in file_remove]
    new_cmd = [e for i, e in enumerate(cmd_exps) if i not in cmd_remove]
    new_diff = [e for i, e in enumerate(diff_exps) if i not in diff_remove]

    removed = len(file_remove) + len(cmd_remove) + len(diff_remove)
    print(f"  Removed {removed} expectation(s).")
    return new_file, new_cmd, new_diff


def collect_and_derive_expectations(
    feedback: str, project_dir: Path, task_prompt: str,
    config: AgentConfig | None = None,
) -> tuple[list[FileExpectation], list[CommandExpectation], list[DiffExpectation]]:
    """Derive expectations from feedback, show them, and prompt for acceptance."""
    print("\n[Deriving expectations from feedback...]")
    file_exps, cmd_exps, diff_exps = derive_expectations(
        feedback, str(project_dir), task_prompt, config=config,
    )

    if not file_exps and not cmd_exps and not diff_exps:
        print("Could not derive expectations.")
        return [], [], []

    # Validate derived expectations
    validation_errors: list[str] = []
    for fe in file_exps:
        validation_errors.extend(fe.validate())
    for ce in cmd_exps:
        validation_errors.extend(ce.validate())
    for de in diff_exps:
        validation_errors.extend(de.validate())
    if validation_errors:
        print("\nValidation warnings:")
        for err in validation_errors:
            print(f"  ! {err}")

    show_expectations(file_exps, cmd_exps, diff_exps)

    while True:
        choice = input("\n(a)ccept / (e)dit / (r)eject: ").strip().lower()
        if choice in ("a", "accept"):
            return file_exps, cmd_exps, diff_exps
        elif choice in ("r", "reject"):
            print("Expectations rejected.")
            return [], [], []
        elif choice in ("e", "edit"):
            file_exps, cmd_exps, diff_exps = _edit_expectations(file_exps, cmd_exps, diff_exps)
            if not file_exps and not cmd_exps and not diff_exps:
                print("All expectations removed.")
                return [], [], []
            show_expectations(file_exps, cmd_exps, diff_exps)
            # Loop back to accept/edit/reject
        else:
            print("Please enter 'a', 'e', or 'r'.")


def derive_skill_update(
    current_skill: str,
    feedback: str,
    task_prompt: str,
    checks: list[CheckResult] | None = None,
    config: AgentConfig | None = None,
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
        cfg = config or AgentConfig()
        cmd = list(build_base_command(cfg))
        p_flag = resolve_flag(cfg, "-p")
        if p_flag:
            cmd.extend([p_flag, revision_prompt])
        else:
            cmd.append(revision_prompt)

        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=90,
            env=build_env(cfg),
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
    diff_exps: list[DiffExpectation] | None = None,
    file_diffs: list[FileDiff] | None = None,
    config: AgentConfig | None = None,
) -> list[CheckResult]:
    """Run expectation checks against the current project state."""
    gym = ClaudeGym(work_dir=project_dir, agent_config=config)
    evaluator = ClaudeEvaluator(agent_config=config)
    checks: list[CheckResult] = []
    checks.extend(evaluator._verify_file_expectations(gym, file_exps))
    checks.extend(evaluator._verify_command_expectations(gym, cmd_exps))
    if diff_exps and file_diffs is not None:
        checks.extend(evaluator._verify_diff_expectations(file_diffs, diff_exps))
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
    parser.add_argument("--setup", action="store_true",
                        help="Re-run the CLI agent setup wizard")
    parser.add_argument("--verbose", action="store_true",
                        help="Enable verbose debug output")
    args = parser.parse_args()

    log.set_verbose(args.verbose)

    print("=== Skill Evaluator ===\n")

    # --- Config ---
    if args.setup or not config_exists():
        config = run_setup_wizard()
    else:
        config = load_config()

    # --- Prerequisites ---
    prereq_err = check_prerequisites(config)
    if prereq_err:
        print(f"Error: {prereq_err}")
        return 1

    # --- Check for resumable session ---
    saved = load_session()
    if saved and not args.eval:
        print(f"Found saved session (run {saved.get('run_number', '?')}):")
        print(f"  Skill:   {saved.get('skill', '?')[:80]}...")
        print(f"  Project: {saved.get('project_dir', '?')}")
        resume = input("\nResume? (y/n) [n]: ").strip().lower()
        if resume == "y":
            skill = saved["skill"]
            task_prompt = saved["task_prompt"]
            project_dir = Path(saved["project_dir"]).resolve()
            # Reconstruct expectations
            file_exps = [FileExpectation(**fe) for fe in saved.get("file_expectations", [])]
            cmd_exps = [CommandExpectation(**ce) for ce in saved.get("command_expectations", [])]
            diff_exps = [DiffExpectation(**de) for de in saved.get("diff_expectations", [])]
            run_number = saved.get("run_number", 0)

            dir_err = validate_project_dir(project_dir, skip_clean_check=True)
            if dir_err:
                print(f"Error: {dir_err}")
                delete_session()
                return 1

            interactive_input = input("\nInteractive mode? (y/n) [n]: ").strip().lower()
            interactive = interactive_input == "y"

            branch = get_git_branch(project_dir)
            print(f"\nProject: {project_dir}")
            print(f"Branch:  {branch}")

            # Jump to main loop
            created_files: list[str] = []
            modified_files: list[str] = []
            # Skip to iteration loop below
            return _run_loop(
                skill, task_prompt, project_dir, config, interactive,
                file_exps, cmd_exps, diff_exps, run_number,
                created_files, modified_files, args,
            )

    # --- Collect inputs ---
    skills = discover_skills()
    skill = select_skill(skills)

    while True:
        task_prompt = get_multiline_input("\nTask prompt (end with blank line):")
        if task_prompt.strip():
            break
        print("Error: task prompt cannot be empty.\n")

    recent_projects = load_recent_projects()
    while True:
        if recent_projects:
            print("\nRecent projects:")
            for i, p in enumerate(recent_projects, 1):
                label = p.replace(str(Path.home()), "~")
                print(f"  {i}) {label}")
            print()
            choice = input("Choose a number or enter a new path: ").strip()
        else:
            choice = input("\nProject directory: ").strip()

        if not choice:
            print("Error: please enter a directory path or number.\n")
            continue

        # Check if the user entered a number to select a recent project
        if choice.isdigit() and recent_projects:
            idx = int(choice) - 1
            if 0 <= idx < len(recent_projects):
                project_dir_str = recent_projects[idx]
            else:
                print(f"Error: choose 1–{len(recent_projects)} or enter a path.\n")
                continue
        else:
            project_dir_str = choice

        project_dir = Path(os.path.expandvars(os.path.expanduser(project_dir_str))).resolve()
        if not project_dir.is_dir():
            # If selected from recent list but no longer exists, offer to remove it
            if choice.isdigit() and recent_projects:
                print(f"Error: {project_dir} no longer exists.")
                remove_recent_project(str(project_dir))
                recent_projects = load_recent_projects()
                print("  (Removed from recent list.)\n")
            else:
                print(f"Error: {project_dir} is not a directory.\n")
            continue
        dir_err = validate_project_dir(project_dir, skip_clean_check=args.eval)
        if dir_err:
            print(f"Error: {dir_err}\n")
            continue
        add_recent_project(str(project_dir))
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
    diff_exps: list[DiffExpectation] = []

    if args.eval:
        # Compute diffs of the dirty working tree against HEAD
        gym = ClaudeGym(work_dir=project_dir, agent_config=config)
        eval_diffs = gym.compute_working_tree_diffs()

        if eval_diffs:
            show_file_changes(eval_diffs)
            from diff_server import present_diff_for_review
            feedback = present_diff_for_review(eval_diffs)
        else:
            print("\nNo uncommitted changes found.")
            feedback = get_multiline_input("\nFeedback (or 'done'):")

        if feedback.strip().lower() != "done" and feedback.strip():
            file_exps, cmd_exps, diff_exps = collect_and_derive_expectations(
                feedback, project_dir, task_prompt, config=config,
            )

            # Propose skill update based on feedback
            print("\n[Proposing skill update...]")
            revised_skill = derive_skill_update(
                skill, feedback, task_prompt, config=config,
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

    return _run_loop(
        skill, task_prompt, project_dir, config, interactive,
        file_exps, cmd_exps, diff_exps, run_number,
        created_files, modified_files, args,
    )


def _run_loop(
    skill: str, task_prompt: str, project_dir: Path,
    config: AgentConfig, interactive: bool,
    file_exps: list[FileExpectation], cmd_exps: list[CommandExpectation],
    diff_exps: list[DiffExpectation], run_number: int,
    created_files: list[str], modified_files: list[str],
    args: argparse.Namespace,
) -> int:
    """Main iteration loop. Extracted so both normal and resume paths can use it."""
    while True:
        run_number += 1
        has_expectations = bool(file_exps or cmd_exps or diff_exps)

        # Revert previous changes before re-running (including --eval dirty tree on Run 1)
        if run_number > 1 or (args.eval and (created_files or modified_files)):
            print("\n[Reverting previous changes...]")
            revert_changes(project_dir, created_files, modified_files)
            created_files = []
            modified_files = []
            # Verify revert succeeded
            try:
                status = subprocess.run(
                    ["git", "status", "--porcelain"],
                    cwd=str(project_dir), capture_output=True, text=True, timeout=10,
                )
                if status.stdout.strip():
                    dirty = len(status.stdout.strip().splitlines())
                    log.warn(f"Working tree still has {dirty} change(s) after revert. "
                             "Results may be inconsistent.")
            except Exception:
                pass  # Best-effort check

        label = "(evaluated)" if has_expectations else "(exploratory)"
        print(f"\n--- Run {run_number} {label} ---")

        # Save session state before running
        save_session(
            skill, task_prompt, str(project_dir),
            file_exps, cmd_exps, diff_exps, run_number,
        )

        # Run Claude in the real project
        gym = ClaudeGym(
            work_dir=project_dir,
            system_prompt=skill,
            debug_mode=not interactive,
            interactive=interactive,
            agent_config=config,
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
            checks = run_evaluation(
                project_dir, file_exps, cmd_exps,
                diff_exps=diff_exps, file_diffs=turn.file_diffs,
                config=config,
            )
            print_evaluation(checks)

        # Collect feedback via web diff review UI or terminal
        if turn.file_diffs:
            from diff_server import present_diff_for_review
            feedback = present_diff_for_review(turn.file_diffs)
        else:
            feedback = get_multiline_input("\nFeedback (or 'done'):")

        if feedback.strip().lower() == "done":
            print("\nDone.")
            delete_session()
            break

        if not feedback.strip():
            print("No feedback provided, running again with current settings...")
            continue

        # Derive expectations from feedback
        new_file_exps, new_cmd_exps, new_diff_exps = collect_and_derive_expectations(
            feedback, project_dir, task_prompt, config=config,
        )

        if new_file_exps or new_cmd_exps or new_diff_exps:
            file_exps = new_file_exps
            cmd_exps = new_cmd_exps
            diff_exps = new_diff_exps
        else:
            print("Running again with existing expectations...")

        # Propose skill update based on feedback
        print("\n[Proposing skill update...]")
        revised_skill = derive_skill_update(
            skill, feedback, task_prompt, checks or None, config=config,
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
