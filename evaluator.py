"""
ClaudeEvaluator — Task definitions, verification checks, and result reporting.

Defines tasks with expected outcomes (file contents, syntax validity, command
outputs) and runs them through ClaudeGym, producing structured pass/fail reports.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_gym import ClaudeGym, TurnResult


@dataclass
class FileExpectation:
    path: str
    should_exist: bool = True
    content_contains: list[str] = field(default_factory=list)
    content_matches: list[str] = field(default_factory=list)  # regex patterns
    content_not_contains: list[str] = field(default_factory=list)
    min_lines: int | None = None
    max_lines: int | None = None


@dataclass
class CommandExpectation:
    command: list[str]
    stdout_contains: list[str] = field(default_factory=list)
    returncode: int = 0
    timeout: int = 30


@dataclass
class SyntaxExpectation:
    path: str
    language: str = "python"  # currently only "python" supported


@dataclass
class TaskDefinition:
    name: str
    description: str
    prompt: str
    follow_up_prompts: list[str] = field(default_factory=list)
    file_expectations: list[FileExpectation] = field(default_factory=list)
    command_expectations: list[CommandExpectation] = field(default_factory=list)
    syntax_expectations: list[SyntaxExpectation] = field(default_factory=list)
    max_turns: int = 10
    timeout: int = 300
    setup_files: dict[str, str] = field(default_factory=dict)  # path -> content


@dataclass
class CheckResult:
    check_type: str  # "file", "syntax", "command"
    target: str
    passed: bool
    message: str
    details: str = ""


@dataclass
class TaskResult:
    task: TaskDefinition
    checks: list[CheckResult]
    turns: list[TurnResult]
    passed: bool
    total_cost: float
    clean_log: str
    error: str | None = None

    @property
    def pass_rate(self) -> float:
        if not self.checks:
            return 0.0
        return sum(1 for c in self.checks if c.passed) / len(self.checks)

    def summary(self) -> str:
        passed_count = sum(1 for c in self.checks if c.passed)
        total = len(self.checks)
        status = "PASS" if self.passed else "FAIL"
        return (
            f"[{status}] {self.task.name}: "
            f"{passed_count}/{total} checks passed, "
            f"${self.total_cost:.4f}"
        )


class ClaudeEvaluator:
    """Runs tasks through ClaudeGym and verifies outcomes."""

    def __init__(self, debug_mode: bool = False, model: str | None = None):
        self.debug_mode = debug_mode
        self.model = model

    def run_task(self, task: TaskDefinition) -> TaskResult:
        """Execute a task and verify all expectations."""
        gym = ClaudeGym(
            debug_mode=self.debug_mode,
            model=self.model,
            max_turns=task.max_turns,
        )

        turns: list[TurnResult] = []
        error: str | None = None

        try:
            # Write setup files
            for rel_path, content in task.setup_files.items():
                fpath = gym.work_dir / rel_path
                fpath.parent.mkdir(parents=True, exist_ok=True)
                fpath.write_text(content, encoding="utf-8")

            # Send main prompt
            turn = gym.send_prompt(task.prompt, timeout=task.timeout)
            turns.append(turn)

            if turn.is_error:
                error = f"Main prompt returned error: {turn.result_text[:200]}"

            # Send follow-up prompts (auto-resumes via session_id)
            for follow_up in task.follow_up_prompts:
                turn = gym.send_prompt(follow_up, timeout=task.timeout)
                turns.append(turn)
                if turn.is_error:
                    error = f"Follow-up returned error: {turn.result_text[:200]}"

            # Run verification checks
            checks: list[CheckResult] = []
            checks.extend(self._verify_file_expectations(gym, task.file_expectations))
            checks.extend(self._verify_syntax_expectations(gym, task.syntax_expectations))
            checks.extend(self._verify_command_expectations(gym, task.command_expectations))

            all_passed = all(c.passed for c in checks) and error is None
            clean_log = gym.get_clean_log()

            return TaskResult(
                task=task,
                checks=checks,
                turns=turns,
                passed=all_passed,
                total_cost=gym.conversation_log.total_cost,
                clean_log=clean_log,
                error=error,
            )

        except Exception as e:
            return TaskResult(
                task=task,
                checks=[],
                turns=turns,
                passed=False,
                total_cost=gym.conversation_log.total_cost,
                clean_log=gym.get_clean_log(),
                error=str(e),
            )

        finally:
            gym.teardown()

    def _verify_file_expectations(
        self, gym: ClaudeGym, expectations: list[FileExpectation]
    ) -> list[CheckResult]:
        results: list[CheckResult] = []

        for exp in expectations:
            # Check existence
            content = gym.get_file_content(exp.path)
            exists = content is not None

            if exp.should_exist and not exists:
                results.append(CheckResult(
                    check_type="file", target=exp.path, passed=False,
                    message=f"File {exp.path} should exist but was not found",
                ))
                continue
            elif not exp.should_exist and exists:
                results.append(CheckResult(
                    check_type="file", target=exp.path, passed=False,
                    message=f"File {exp.path} should not exist but was found",
                ))
                continue
            elif not exp.should_exist and not exists:
                results.append(CheckResult(
                    check_type="file", target=exp.path, passed=True,
                    message=f"File {exp.path} correctly does not exist",
                ))
                continue

            # File exists and should — run content checks
            # Substring checks
            for substring in exp.content_contains:
                found = substring in content
                results.append(CheckResult(
                    check_type="file", target=f"{exp.path} contains '{substring}'",
                    passed=found,
                    message=f"{'Found' if found else 'Missing'}: '{substring}' in {exp.path}",
                ))

            # Regex checks
            for pattern in exp.content_matches:
                matched = bool(re.search(pattern, content))
                results.append(CheckResult(
                    check_type="file", target=f"{exp.path} matches /{pattern}/",
                    passed=matched,
                    message=f"{'Matched' if matched else 'No match'}: /{pattern}/ in {exp.path}",
                ))

            # Not-contains checks
            for substring in exp.content_not_contains:
                absent = substring not in content
                results.append(CheckResult(
                    check_type="file", target=f"{exp.path} excludes '{substring}'",
                    passed=absent,
                    message=f"{'Excluded' if absent else 'Found (unexpected)'}: '{substring}' in {exp.path}",
                ))

            # Line count checks
            line_count = len(content.splitlines())
            if exp.min_lines is not None:
                ok = line_count >= exp.min_lines
                results.append(CheckResult(
                    check_type="file",
                    target=f"{exp.path} min_lines={exp.min_lines}",
                    passed=ok,
                    message=f"{exp.path}: {line_count} lines (min {exp.min_lines})",
                ))
            if exp.max_lines is not None:
                ok = line_count <= exp.max_lines
                results.append(CheckResult(
                    check_type="file",
                    target=f"{exp.path} max_lines={exp.max_lines}",
                    passed=ok,
                    message=f"{exp.path}: {line_count} lines (max {exp.max_lines})",
                ))

        return results

    def _verify_syntax_expectations(
        self, gym: ClaudeGym, expectations: list[SyntaxExpectation]
    ) -> list[CheckResult]:
        results: list[CheckResult] = []

        for exp in expectations:
            content = gym.get_file_content(exp.path)
            if content is None:
                results.append(CheckResult(
                    check_type="syntax", target=exp.path, passed=False,
                    message=f"Cannot check syntax: {exp.path} not found",
                ))
                continue

            if exp.language == "python":
                try:
                    ast.parse(content, filename=exp.path)
                    results.append(CheckResult(
                        check_type="syntax", target=exp.path, passed=True,
                        message=f"Python syntax valid: {exp.path}",
                    ))
                except SyntaxError as e:
                    results.append(CheckResult(
                        check_type="syntax", target=exp.path, passed=False,
                        message=f"Python syntax error in {exp.path}: {e}",
                        details=str(e),
                    ))
            else:
                results.append(CheckResult(
                    check_type="syntax", target=exp.path, passed=False,
                    message=f"Unsupported syntax check language: {exp.language}",
                ))

        return results

    def _verify_command_expectations(
        self, gym: ClaudeGym, expectations: list[CommandExpectation]
    ) -> list[CheckResult]:
        results: list[CheckResult] = []

        for exp in expectations:
            cmd_str = " ".join(exp.command)
            try:
                proc = subprocess.run(
                    exp.command,
                    cwd=str(gym.work_dir),
                    capture_output=True,
                    text=True,
                    timeout=exp.timeout,
                )

                # Return code check
                rc_ok = proc.returncode == exp.returncode
                results.append(CheckResult(
                    check_type="command",
                    target=f"{cmd_str} (rc={exp.returncode})",
                    passed=rc_ok,
                    message=f"{'OK' if rc_ok else 'FAIL'}: `{cmd_str}` returned {proc.returncode} (expected {exp.returncode})",
                    details=proc.stderr[:300] if not rc_ok else "",
                ))

                # Stdout substring checks
                for substring in exp.stdout_contains:
                    found = substring in proc.stdout
                    results.append(CheckResult(
                        check_type="command",
                        target=f"{cmd_str} stdout contains '{substring}'",
                        passed=found,
                        message=f"{'Found' if found else 'Missing'}: '{substring}' in stdout of `{cmd_str}`",
                        details=f"stdout: {proc.stdout[:300]}" if not found else "",
                    ))

            except subprocess.TimeoutExpired:
                results.append(CheckResult(
                    check_type="command", target=cmd_str, passed=False,
                    message=f"Command timed out after {exp.timeout}s: `{cmd_str}`",
                ))
            except FileNotFoundError:
                results.append(CheckResult(
                    check_type="command", target=cmd_str, passed=False,
                    message=f"Command not found: `{cmd_str}`",
                ))

        return results

    def run_suite(self, tasks: list[TaskDefinition]) -> list[TaskResult]:
        """Run multiple tasks sequentially."""
        results: list[TaskResult] = []
        for task in tasks:
            if self.debug_mode:
                print(f"\n{'='*60}", file=sys.stderr)
                print(f"TASK: {task.name}", file=sys.stderr)
                print(f"{'='*60}", file=sys.stderr)
            result = self.run_task(task)
            results.append(result)
        return results

    @staticmethod
    def print_report(results: list[TaskResult]) -> None:
        """Print a formatted pass/fail report."""
        print("\n" + "=" * 60)
        print("EVALUATION REPORT")
        print("=" * 60)

        total_passed = 0
        total_tasks = len(results)
        total_cost = 0.0

        for result in results:
            total_cost += result.total_cost
            status = "PASS" if result.passed else "FAIL"
            print(f"\n[{status}] {result.task.name}")
            print(f"  Description: {result.task.description}")
            print(f"  Cost: ${result.total_cost:.4f}")

            if result.error:
                print(f"  Error: {result.error}")

            if result.passed:
                total_passed += 1

            for check in result.checks:
                icon = "+" if check.passed else "-"
                print(f"  [{icon}] {check.message}")
                if check.details and not check.passed:
                    for line in check.details.splitlines()[:5]:
                        print(f"      {line}")

        print(f"\n{'='*60}")
        print(f"Results: {total_passed}/{total_tasks} tasks passed")
        print(f"Total cost: ${total_cost:.4f}")
        print("=" * 60)
