# Skill Iterator

A tool for iteratively developing and evaluating **skills** — system prompts that make Claude Code reliably perform specific coding tasks. Run your skill against a real project, review the output, describe what's wrong in plain English, and the tool automatically derives evaluation checks *and* proposes targeted updates to the skill itself.

## Prerequisites

- **Python 3.10+**
- **Claude Code CLI** (or a compatible wrapper) installed and on PATH:
  ```bash
  npm install -g @anthropic-ai/claude-code
  ```
- No third-party Python dependencies — stdlib only.

## Quick Start

```bash
git clone <this-repo>
cd SkillIteratorClaude

# Interactive skill evaluation loop
python3 run_skill.py

# Non-interactive demo (builds a calculator, evaluates it)
python3 run_demo.py
```

On first run, a setup wizard will ask which CLI command to use (default: `claude`) and run a quick smoke test. Configuration is saved to `~/.skilliterator/config.json`. Re-run the wizard anytime with `--setup`.

## How It Works

The evaluator runs a tight feedback loop:

```
 1. Select a skill (system prompt)
 2. Give it a task prompt and a target project directory
 3. Claude runs against the real project using your skill
 4. You review the output in a browser-based diff UI
 5. You describe what's wrong in plain English
 6. The tool derives automated checks (expectations) from your feedback
 7. The tool proposes targeted updates to the skill itself
 8. Changes are reverted, and Claude re-runs with the improved skill
 9. Output is auto-evaluated against the checks — repeat until green
```

Each iteration improves *both* the evaluation criteria and the skill, converging on a prompt that reliably produces correct output.

## Usage

### Creating a Skill

A skill is a system prompt — plain text or Markdown that tells Claude how to perform a task. You can provide one in two ways:

**Option A: Skill files (recommended for reuse)**

Create a directory under `~/.claude/skills/` with a `SKILL.md` file:

```
~/.claude/skills/
  swift-unit-test-writer/
    SKILL.md
  api-endpoint-generator/
    SKILL.md
```

The `SKILL.md` file can optionally include YAML frontmatter:

```markdown
---
name: Swift Unit Test Writer
description: Writes unit tests using Swift Testing framework
---

You are a test-writing assistant. When given a Swift source file, generate
comprehensive unit tests for all public methods.

Rules:
- Use the Swift Testing framework (@Test macro, #expect)
- DO NOT use XCTest
- Place test files in the Tests/ directory mirroring the source structure
- Every public method must have at least one test
- Include edge cases for optional parameters
...
```

The evaluator discovers these automatically and presents them as a numbered menu.

**Option B: Enter manually**

If no skill files exist (or you choose "Custom"), you'll be prompted to type the skill directly. End with a blank line.

### Running the Evaluator

```bash
python3 run_skill.py
```

You'll be prompted for:

1. **Skill** — select from discovered skills or enter a custom one
2. **Task prompt** — what Claude should do (e.g., "Write unit tests for Sources/Models/User.swift")
3. **Project directory** — absolute path to a git repo with a clean working tree
4. **Interactive mode** — whether you can interact with Claude during the run (answer questions, approve plans)

#### Run 1: Exploratory

The first run has no expectations. Claude executes the task, and you see what it produces. A browser window opens with a diff review UI where you can:

- **Click** a line number to comment on a single line
- **Drag** across line numbers to select and comment on a range
- Write **overall feedback** in the text area at the bottom
- Press **Submit Feedback** (or Ctrl+Enter) to continue
- Press **Skip** (or Esc) to re-run without feedback

#### Feedback Processing

After you submit feedback, two things happen in sequence:

**1. Expectations are derived.** Your freeform feedback is sent to Claude, which converts it into structured JSON checks:

```
Derived expectations:
  [+] File Tests/UserTests.swift exists
      Contains: @Test, import Testing
      Not contains: XCTest
  [+] Command swift build --build-tests returns 0
```

You're asked to accept or reject these.

**2. A skill update is proposed.** The same feedback (plus any evaluation results from prior runs) is used to generate targeted edits to the skill. You see a colorized diff:

```diff
--- current skill
+++ proposed skill
@@ -5,6 +5,8 @@
 Rules:
 - Use the Swift Testing framework (@Test macro, #expect)
 - DO NOT use XCTest
+- DO NOT import XCTest — use `import Testing` exclusively
+- Each test function must be annotated with @Test, not `func testXxx()`
```

Accept or reject the changes. If accepted, the updated skill is used on the next run.

#### Run 2+: Evaluated

Previous changes are reverted (created files deleted, modified files restored via `git checkout`). Claude runs fresh with the (potentially updated) skill. Output is auto-evaluated:

```
Evaluation:
  [+] Found: '@Test' in Tests/UserTests.swift
  [+] Excluded: 'XCTest' in Tests/UserTests.swift
  [-] FAIL: `swift build --build-tests` returned 1 (expected 0)
      error: missing import for 'Testing'

Results: 2/3 checks passed
```

You review, give more feedback, and the loop continues.

### Eval Mode (--eval)

Skip the exploratory run when you already have output to evaluate:

```bash
python3 run_skill.py --eval
```

This is for when Claude has already run and you have uncommitted changes in the target project. The evaluator:

1. Computes diffs from the dirty working tree
2. Opens the diff review UI for your feedback
3. Derives expectations and proposes skill updates
4. Reverts, re-runs Claude with the updated skill, and evaluates

Useful when you've been manually testing a skill and want to formalize the evaluation.

### Setup Wizard (--setup)

Re-run the CLI agent configuration wizard:

```bash
python3 run_skill.py --setup
```

The wizard runs automatically on first use (when no config file exists). Use `--setup` to reconfigure the command, extra args, or re-test connectivity. See [CLI Agent Configuration](#cli-agent-configuration) for details.

### Interactive Mode

When prompted "Interactive mode? (y/n)", choosing `y` gives Claude full terminal access — it can ask you questions, propose plans for your approval, and use its full interactive feature set. File diffs are still computed from before/after snapshots.

Choose `n` (default) for fully headless runs where Claude operates autonomously.

## Diff Review UI

The browser-based diff viewer provides a GitHub-style review experience:

- **Syntax-highlighted diffs** via diff2html, with dark theme
- **Inline commenting** — click a line number to comment on that line
- **Range selection** — click and drag across line numbers to comment on multiple lines. The selected range highlights as you drag, and the comment form shows the range (e.g., "L5-12").
- **Overall feedback** — free-text area for general observations
- **Keyboard shortcuts** — Ctrl+Enter to submit, Esc to skip, Ctrl+Enter inside a comment to save it
- **Collapsible file sections** — click file headers to expand/collapse
- **Comment count badges** — per-file count of inline comments

If the browser can't be opened, the evaluator falls back to terminal input.

## Expectation Types

Expectations are the automated checks derived from your feedback:

### FileExpectation

Checks a file's existence and contents. Supports both exact paths and glob patterns.

| Field | Description |
|---|---|
| `path` | Relative path from project root (mutually exclusive with `path_pattern`) |
| `path_pattern` | Glob pattern, e.g. `"Tests/**/*.swift"` (supports `**`, `*`, `?`) |
| `should_exist` | Whether the file should exist (default: true) |
| `content_contains` | Exact substrings that must be present |
| `content_not_contains` | Exact substrings that must be absent |
| `content_matches` | Python regex patterns to match |
| `min_lines` / `max_lines` | Line count bounds |
| `min_matching_files` | Minimum files matching `path_pattern` (default: 1) |

When `path_pattern` is used, the evaluator finds all matching files and runs the content checks against each one. This is useful for feedback like "all test files should import Testing" — it becomes a single expectation that checks every file matching `Tests/**/*.swift`.

### CommandExpectation

Runs a command and checks the result.

| Field | Description |
|---|---|
| `command` | Command as a list of strings (e.g., `["swift", "build"]`) |
| `returncode` | Expected exit code (default: 0) |
| `stdout_contains` | Substrings expected in stdout |
| `stdout_not_contains` | Substrings that must NOT appear in stdout |
| `stderr_contains` | Substrings expected in stderr |
| `stderr_not_contains` | Substrings that must NOT appear in stderr |
| `timeout` | Seconds before timeout (default: 30) |

Stderr checks are useful for catching compiler warnings or deprecation notices. For example, "no warnings during build" becomes a `stderr_not_contains` check.

### DiffExpectation

Constraints on what files were changed during the run.

| Field | Description |
|---|---|
| `allowed_statuses` | Restrict diffs to these statuses (e.g., `["added"]` for new files only) |
| `allowed_path_patterns` | Every changed file must match at least one pattern |
| `disallowed_path_patterns` | No changed file may match any of these patterns |
| `min_files_changed` / `max_files_changed` | Bounds on how many files were changed |
| `must_include_paths` | Specific paths that must appear in the diffs |

DiffExpectations are useful for scoping feedback like "should only add new files under Tests/", "must not modify any source files", or "should change exactly 2 files". They check the actual file diffs from the Claude run, not the project state.

### SyntaxExpectation

Validates syntax (currently Python only via `ast.parse`).

| Field | Description |
|---|---|
| `path` | File to check |
| `language` | `"python"` (only supported option) |

You don't write these by hand — they're generated from your plain-English feedback. But understanding what's available helps you give effective feedback (e.g., "the code should compile" becomes a CommandExpectation; "don't use XCTest" becomes a content_not_contains check; "should only add files under Tests/" becomes a DiffExpectation).

## Architecture

```
config.py         CLI agent configuration — AgentConfig dataclass, config
                  file I/O (~/.skilliterator/config.json), flag overrides,
                  env var handling, smoke test, and setup wizard.

claude_gym.py     Drives the CLI agent via subprocess. Handles streaming
                  JSON events, file snapshots, diff computation, timeout
                  watchdogs. Uses AgentConfig for command building.

evaluator.py      Defines expectation types (FileExpectation, CommandExpectation,
                  DiffExpectation, SyntaxExpectation) and verification logic.

diff_server.py    Local HTTP server + browser UI for reviewing diffs with
                  inline commenting. Returns structured feedback.

run_skill.py      Interactive loop wiring the above together with
                  feedback-to-expectations and feedback-to-skill-update
                  derivation steps.

run_demo.py       Non-interactive demo that runs a predefined calculator
                  task through the evaluator.
```

## CLI Agent Configuration

The tool supports custom CLI agents — company-internal wrappers around Claude that use the same flags and stream-json output format but are invoked via a different command or require extra flags.

Configuration is stored in `~/.skilliterator/config.json`:

```json
{
  "command": "claude",
  "extra_args": [],
  "flag_overrides": {},
  "env_vars": {},
  "nesting_guard_vars": ["CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"]
}
```

| Field | Purpose | Example |
|---|---|---|
| `command` | CLI binary name or path | `"acme-claude"` |
| `extra_args` | Args inserted after the command, before other flags | `["--team", "eng"]` |
| `flag_overrides` | Rename flags (string value) or suppress them (`null`) | `{"--verbose": null}` |
| `env_vars` | Extra environment variables for the subprocess | `{"AUTH_TOKEN": "..."}` |
| `nesting_guard_vars` | Env vars to strip to prevent nesting detection | defaults shown above |

### Setup Wizard

The wizard runs on first use (no config file) or when you pass `--setup`:

```
$ python3 run_skill.py --setup
==================================================
  Skill Iterator — First-Run Setup
==================================================

CLI command [claude]: acme-claude
  Found: /usr/local/bin/acme-claude

Extra args (e.g. --team eng) [none]: --team eng

[Running smoke test...]
  Smoke test passed.

Config saved to ~/.skilliterator/config.json
```

### Flag Overrides

Flag overrides let you rename or suppress individual CLI flags:

- **Rename:** `{"--model": "--acme-model"}` — uses `--acme-model` wherever `--model` would appear
- **Suppress:** `{"--verbose": null}` — omits `--verbose` from all commands
- **Unset flags pass through unchanged** — any flag not in `flag_overrides` is used as-is

This is useful when a wrapper CLI uses different flag names or doesn't support certain flags.

## Safety

The evaluator takes precautions to avoid damaging your project:

- **Clean working tree required.** Won't start if there are uncommitted changes (ensures safe reverts).
- **Blocked directories.** Refuses to run in `/`, `~`, or the evaluator's own repository.
- **Targeted reverts.** Created files are deleted individually; modified files are restored with `git checkout`. Never uses `git clean -f` or `git reset --hard`.
- **Branch visibility.** Shows the current git branch at startup so you know where changes will land.
- **Claude runs with `bypassPermissions`.** The subprocess has full file access within the project directory, but git provides the safety net.

## Tips for Effective Feedback

- **Be specific about patterns.** "Use @Test not XCTest" is better than "use the right framework."
- **Mention file paths.** "Tests should go in Tests/ModelTests/" helps derive accurate FileExpectations.
- **Include build commands.** "Should compile with `swift build --build-tests`" becomes a CommandExpectation.
- **Say what's wrong AND what's right.** "The structure is good but it's missing edge case tests" prevents the skill update from breaking what works.
- **Use the inline comments.** Pointing at specific lines gives the skill updater precise context about what to fix.
