#!/usr/bin/env python3
"""
Demo script — runs the Calculator task through ClaudeGym and evaluates results.

Usage:
    python3 run_demo.py
"""

import sys

from evaluator import (
    ClaudeEvaluator,
    CommandExpectation,
    FileExpectation,
    SyntaxExpectation,
    TaskDefinition,
)

CALCULATOR_TASK = TaskDefinition(
    name="Calculator",
    description="Create a calculator module with arithmetic operations and a CLI interface",
    prompt=(
        "Create a file called calculator.py that implements a calculator with the "
        "following requirements:\n"
        "1. Functions: add(a, b), subtract(a, b), multiply(a, b), divide(a, b)\n"
        "2. divide should raise ValueError on division by zero\n"
        "3. All functions should work with int and float arguments\n"
        "4. Include a CLI interface using argparse so it can be called as:\n"
        "   python3 calculator.py add 2 3\n"
        "   python3 calculator.py multiply 4 5\n"
        "5. The CLI should print just the numeric result to stdout\n"
        "6. Do not create any other files — just calculator.py"
    ),
    file_expectations=[
        FileExpectation(
            path="calculator.py",
            should_exist=True,
            content_contains=[
                "def add",
                "def subtract",
                "def multiply",
                "def divide",
            ],
            min_lines=15,
        ),
    ],
    syntax_expectations=[
        SyntaxExpectation(path="calculator.py", language="python"),
    ],
    command_expectations=[
        CommandExpectation(
            command=["python3", "calculator.py", "add", "2", "3"],
            stdout_contains=["5"],
            returncode=0,
        ),
        CommandExpectation(
            command=["python3", "calculator.py", "subtract", "10", "4"],
            stdout_contains=["6"],
            returncode=0,
        ),
        CommandExpectation(
            command=["python3", "calculator.py", "multiply", "4", "5"],
            stdout_contains=["20"],
            returncode=0,
        ),
        CommandExpectation(
            command=["python3", "calculator.py", "divide", "15", "3"],
            stdout_contains=["5"],
            returncode=0,
        ),
    ],
    max_turns=5,
    timeout=120,
)


def main() -> int:
    evaluator = ClaudeEvaluator(debug_mode=True)
    results = evaluator.run_suite([CALCULATOR_TASK])
    evaluator.print_report(results)

    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
