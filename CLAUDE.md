# Skill Iterator

## What This Is

A tool for iteratively developing and evaluating "skills" — system prompts that make Claude Code reliably perform specific coding tasks. Think of it as a test harness for prompt engineering, but against real projects instead of toy examples.

## Why This Exists

Claude Code is capable, but getting it to consistently produce code that meets specific standards (right testing framework, correct file locations, proper patterns, compiles cleanly) requires careful prompt engineering. The problem is:

1. **You can't evaluate a prompt without running it.** A system prompt that sounds good might produce code that uses XCTest instead of Swift Testing, puts files in the wrong directory, or doesn't compile.

2. **You can't improve a prompt without fast iteration.** Manually running Claude, inspecting output, tweaking the prompt, reverting changes, and re-running is tedious. Each cycle takes minutes of manual work.

3. **"It looks right" isn't good enough.** Eyeballing output doesn't catch subtle issues. You need automated checks — does the file contain the right imports? Does it compile? Does it cover all the methods?

4. **Evaluation criteria are hard to write upfront.** You often don't know what "good" looks like until you see the first attempt. The criteria emerge from reviewing actual output, not from abstract spec-writing.

This tool solves all four problems with a single interactive loop: run the skill, review the output, describe what's wrong in plain English, and let the tool convert that into automated checks for subsequent runs.

## The Core Insight

The feedback-to-expectations pipeline is the key design decision. Instead of asking users to write `FileExpectation(content_contains=["@Test"])` by hand, they just say "tests should use @Test not XCTest" and a separate Claude call converts that into structured checks. This keeps the human interface natural while producing deterministic, repeatable evaluations.

## Architecture

### Three layers, each independently useful

```
claude_gym.py     — Drives the `claude` CLI via subprocess. Handles streaming JSON
                    events, file snapshots, diff computation, timeout watchdogs.
                    Zero third-party dependencies.

evaluator.py      — Defines expectation types (FileExpectation, CommandExpectation,
                    SyntaxExpectation) and verification logic. Can evaluate any
                    project state against structured criteria.

run_skill.py      — Interactive loop that wires the above together with a
                    feedback-to-expectations derivation step.
run_demo.py       — Non-interactive demo that runs a predefined task (calculator)
                    through the evaluator. Useful for verifying the harness works.
```

### Data flow

```
User input (skill, prompt, project dir)
  → ClaudeGym.send_prompt() runs Claude in the real project
    → TurnResult with file_diffs, response, cost
      → User reviews output, gives freeform feedback
        → derive_expectations() calls claude -p to produce JSON
          → FileExpectation / CommandExpectation objects
            → Revert changes, re-run, auto-evaluate
              → Pass/fail report → more feedback → repeat
```

### Key types

- **`ClaudeGym`** — Subprocess wrapper for `claude --print --output-format stream-json`. Snapshots the working directory before/after each prompt to compute file diffs. Supports custom `work_dir`, `system_prompt`, streaming callbacks.

- **`TurnResult`** — One prompt-response cycle: response text, file diffs, tool uses, cost, duration, error status.

- **`FileExpectation`** — Check a file for existence, content substrings, regex matches, excluded strings, line counts.

- **`CommandExpectation`** — Run a command and check return code / stdout contents.

- **`CheckResult`** — Pass/fail for a single expectation, with human-readable message.

## How run_skill.py Works

### Run 1 (exploratory)
No expectations yet. Claude runs against the real project. You see what it produces and describe what's good or bad.

### Feedback → Expectations
Your freeform feedback ("should use @Test, cover all public methods, compile with swift build --build-tests") gets sent to a separate `claude -p` call that outputs structured JSON expectations.

### Run 2+ (evaluated)
Previous changes are reverted (created files deleted, modified files git-checkout'd). Claude runs fresh. Output is auto-evaluated against the expectations. You see a pass/fail report and can refine further.

### Safety
- Requires a clean git working tree (no uncommitted changes)
- Blocks dangerous directories (/, ~, the evaluator's own repo)
- Reverts use targeted file deletion + `git checkout`, never `git clean`
- Shows the git branch before starting

## Working on This Project

### Dependencies
None. Python standard library only. Requires `claude` CLI on PATH (`npm install -g @anthropic-ai/claude-code`).

### Running
```bash
python3 run_skill.py     # Interactive skill evaluation loop
python3 run_demo.py      # Non-interactive demo (calculator task)
```

### Design principles
- **No temp directories.** Skills should be evaluated against real projects with real build systems, real dependencies, real file structures. Git provides the safety net.
- **No changes to lower layers for upper-layer features.** `run_skill.py` works entirely through the public interfaces of `claude_gym.py` and `evaluator.py`. If you need new verification logic, add it to `evaluator.py`. If you need new Claude interaction patterns, add them to `claude_gym.py`.
- **Zero third-party dependencies.** Everything uses Python stdlib + the `claude` CLI. This keeps the tool portable and avoids version conflicts.
- **Human-readable output.** Every step prints what it's doing. No silent failures, no hidden state.
