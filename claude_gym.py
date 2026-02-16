"""
ClaudeGym — Execution harness for driving the `claude` CLI programmatically.

Uses `claude --print --output-format stream-json --verbose` for structured
JSON events with zero third-party dependencies. Multi-turn conversations
use `--resume <session_id>`.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass
class FileSnapshot:
    path: str
    content: str | None  # None if binary
    sha256: str
    size: int
    mtime: float


@dataclass
class FileDiff:
    path: str
    status: str  # "added", "modified", "deleted"
    unified_diff: str
    before_hash: str | None
    after_hash: str | None


@dataclass
class TurnResult:
    prompt: str
    result_text: str
    session_id: str | None
    num_turns: int
    cost_usd: float
    duration: float
    is_error: bool
    raw_events: list[dict]
    tool_uses: list[dict]
    file_diffs: list[FileDiff]


@dataclass
class ConversationLog:
    turns: list[TurnResult] = field(default_factory=list)

    @property
    def total_cost(self) -> float:
        return sum(t.cost_usd for t in self.turns)

    @property
    def total_duration(self) -> float:
        return sum(t.duration for t in self.turns)

    @property
    def total_num_turns(self) -> int:
        return sum(t.num_turns for t in self.turns)


HIDDEN_DIRS = {".git", ".claude", "__pycache__", ".mypy_cache", ".pytest_cache"}


class ClaudeGym:
    """Drives the `claude` CLI via subprocess with structured JSON streaming."""

    def __init__(
        self,
        work_dir: str | Path | None = None,
        debug_mode: bool = False,
        model: str | None = None,
        max_turns: int = 10,
        max_budget_usd: float | None = None,
        permission_mode: str = "bypassPermissions",
        system_prompt: str | None = None,
        allowed_tools: list[str] | None = None,
        stream_callback: Callable[[dict], None] | None = None,
        interactive: bool = False,
    ):
        self._owns_work_dir = work_dir is None
        if work_dir is None:
            self._work_dir = Path(tempfile.mkdtemp(prefix="claude_gym_"))
        else:
            self._work_dir = Path(work_dir)
            self._work_dir.mkdir(parents=True, exist_ok=True)

        self.debug_mode = debug_mode
        self.model = model
        self.max_turns = max_turns
        self.max_budget_usd = max_budget_usd
        self.permission_mode = permission_mode
        self.system_prompt = system_prompt
        self.allowed_tools = allowed_tools
        self.stream_callback = stream_callback
        self.interactive = interactive

        self._session_id: str | None = None
        self.conversation_log = ConversationLog()

    @property
    def work_dir(self) -> Path:
        return self._work_dir

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.teardown()
        return False

    def _build_command(self, prompt: str, resume_session: str | None = None) -> list[str]:
        if self.interactive:
            return self._build_interactive_command(prompt, resume_session)

        cmd = [
            "claude",
            "-p", prompt,
            "--output-format", "stream-json",
            "--verbose",
            "--max-turns", str(self.max_turns),
        ]

        if self.permission_mode:
            cmd.extend(["--permission-mode", self.permission_mode])

        if self.debug_mode or self.stream_callback:
            cmd.append("--include-partial-messages")

        if resume_session:
            cmd.extend(["--resume", resume_session])

        if self.model:
            cmd.extend(["--model", self.model])

        if self.max_budget_usd is not None:
            cmd.extend(["--max-budget-usd", str(self.max_budget_usd)])

        if self.system_prompt:
            cmd.extend(["--system-prompt", self.system_prompt])

        if self.allowed_tools:
            cmd.extend(["--allowedTools"] + self.allowed_tools)

        return cmd

    def _build_interactive_command(self, prompt: str, resume_session: str | None = None) -> list[str]:
        cmd = ["claude"]

        if resume_session:
            cmd.extend(["--resume", resume_session])

        if self.model:
            cmd.extend(["--model", self.model])

        # No --max-turns in interactive mode: the user controls the session
        if self.max_budget_usd is not None:
            cmd.extend(["--max-budget-usd", str(self.max_budget_usd)])

        if self.system_prompt:
            cmd.extend(["--system-prompt", self.system_prompt])

        if self.allowed_tools:
            cmd.extend(["--allowedTools"] + self.allowed_tools)

        # Prompt goes as positional argument at the end
        cmd.append(prompt)

        return cmd

    def _build_env(self) -> dict[str, str]:
        env = os.environ.copy()
        # Nesting guard: claude refuses to start if it detects it's inside
        # another claude session via these env vars.
        env.pop("CLAUDECODE", None)
        env.pop("CLAUDE_CODE_ENTRYPOINT", None)
        return env

    def _snapshot_directory(self) -> dict[str, FileSnapshot]:
        snapshots: dict[str, FileSnapshot] = {}
        work = self._work_dir

        for root, dirs, files in os.walk(work):
            # Filter out hidden directories in-place
            dirs[:] = [d for d in dirs if d not in HIDDEN_DIRS and not d.startswith(".")]

            for fname in files:
                if fname.startswith("."):
                    continue
                fpath = Path(root) / fname
                rel = str(fpath.relative_to(work))
                try:
                    stat = fpath.stat()
                    raw = fpath.read_bytes()
                    sha = hashlib.sha256(raw).hexdigest()
                    try:
                        content = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        content = None
                    snapshots[rel] = FileSnapshot(
                        path=rel,
                        content=content,
                        sha256=sha,
                        size=stat.st_size,
                        mtime=stat.st_mtime,
                    )
                except (OSError, PermissionError):
                    continue

        return snapshots

    def _compute_diffs(
        self,
        before: dict[str, FileSnapshot],
        after: dict[str, FileSnapshot],
    ) -> list[FileDiff]:
        diffs: list[FileDiff] = []

        # Added files
        for path in sorted(set(after) - set(before)):
            snap = after[path]
            content = snap.content or "<binary>"
            diff_text = "\n".join(
                difflib.unified_diff(
                    [], content.splitlines(),
                    fromfile="/dev/null", tofile=path, lineterm=""
                )
            )
            diffs.append(FileDiff(
                path=path, status="added",
                unified_diff=diff_text,
                before_hash=None, after_hash=snap.sha256,
            ))

        # Deleted files
        for path in sorted(set(before) - set(after)):
            snap = before[path]
            content = snap.content or "<binary>"
            diff_text = "\n".join(
                difflib.unified_diff(
                    content.splitlines(), [],
                    fromfile=path, tofile="/dev/null", lineterm=""
                )
            )
            diffs.append(FileDiff(
                path=path, status="deleted",
                unified_diff=diff_text,
                before_hash=snap.sha256, after_hash=None,
            ))

        # Modified files
        for path in sorted(set(before) & set(after)):
            b, a = before[path], after[path]
            if b.sha256 != a.sha256:
                b_lines = (b.content or "<binary>").splitlines()
                a_lines = (a.content or "<binary>").splitlines()
                diff_text = "\n".join(
                    difflib.unified_diff(
                        b_lines, a_lines,
                        fromfile=f"a/{path}", tofile=f"b/{path}", lineterm=""
                    )
                )
                diffs.append(FileDiff(
                    path=path, status="modified",
                    unified_diff=diff_text,
                    before_hash=b.sha256, after_hash=a.sha256,
                ))

        return diffs

    def _debug_print_event(self, event: dict) -> None:
        etype = event.get("type")
        if etype == "content_block_delta":
            delta = event.get("delta", {})
            if delta.get("type") == "text_delta":
                text = delta.get("text", "")
                print(text, end="", file=sys.stderr, flush=True)
        elif etype == "content_block_start":
            cb = event.get("content_block", {})
            if cb.get("type") == "tool_use":
                name = cb.get("name", "unknown")
                print(f"\n[TOOL: {name}]", file=sys.stderr, flush=True)
        elif etype == "result":
            cost = event.get("cost_usd", 0)
            turns = event.get("num_turns", 0)
            session = event.get("session_id", "")
            print(
                f"\n--- Result: {turns} turn(s), ${cost:.4f}, session={session} ---",
                file=sys.stderr, flush=True,
            )

    def _parse_stream_events(self, process: subprocess.Popen) -> tuple[list[dict], dict | None]:
        events: list[dict] = []
        result_event: dict | None = None

        for raw_line in process.stdout:
            line = raw_line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            events.append(event)

            if self.debug_mode:
                self._debug_print_event(event)
            if self.stream_callback:
                self.stream_callback(event)

            if event.get("type") == "result":
                result_event = event

        return events, result_event

    def send_prompt(self, prompt: str, timeout: int = 300) -> TurnResult:
        """Send a prompt to claude and return structured results."""
        if self.interactive:
            return self._send_prompt_interactive(prompt)

        t0 = time.time()

        # Snapshot before
        before = self._snapshot_directory()

        # Build and run command
        cmd = self._build_command(prompt, resume_session=self._session_id)
        if self.debug_mode:
            print(f"\n>>> Prompt: {prompt[:120]}...", file=sys.stderr, flush=True)
            print(f">>> Command: {' '.join(cmd[:8])}...", file=sys.stderr, flush=True)

        process = subprocess.Popen(
            cmd,
            cwd=str(self._work_dir),
            env=self._build_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        # Timeout watchdog
        timed_out = threading.Event()

        def _watchdog():
            if not timed_out.wait(timeout):
                return
            try:
                process.send_signal(signal.SIGTERM)
                process.wait(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                except ProcessLookupError:
                    pass

        watchdog = threading.Thread(target=_watchdog, daemon=True)
        watchdog.start()

        # Parse events
        events, result_event = self._parse_stream_events(process)
        process.wait()
        timed_out.set()  # Cancel watchdog

        duration = time.time() - t0

        # Extract info from result event
        session_id = None
        num_turns = 0
        cost_usd = 0.0
        result_text = ""
        is_error = process.returncode != 0

        if result_event:
            session_id = result_event.get("session_id")
            num_turns = result_event.get("num_turns", 0)
            cost_usd = result_event.get("cost_usd", 0.0)
            result_text = result_event.get("result", "")
            is_error = result_event.get("is_error", is_error)
            if session_id:
                self._session_id = session_id

        # Extract tool uses from events
        tool_uses = []
        for ev in events:
            if ev.get("type") == "content_block_start":
                cb = ev.get("content_block", {})
                if cb.get("type") == "tool_use":
                    tool_uses.append({
                        "name": cb.get("name"),
                        "id": cb.get("id"),
                    })

        # Snapshot after and compute diffs
        after = self._snapshot_directory()
        file_diffs = self._compute_diffs(before, after)

        turn = TurnResult(
            prompt=prompt,
            result_text=result_text,
            session_id=session_id,
            num_turns=num_turns,
            cost_usd=cost_usd,
            duration=duration,
            is_error=is_error,
            raw_events=events,
            tool_uses=tool_uses,
            file_diffs=file_diffs,
        )
        self.conversation_log.turns.append(turn)
        return turn

    def _send_prompt_interactive(self, prompt: str) -> TurnResult:
        """Run claude interactively, letting it own the terminal.

        The user can interact with Claude directly (answer questions,
        approve plan mode, etc.). File diffs are computed from before/after
        snapshots. Structured event data (cost, turns) is not available.
        """
        t0 = time.time()

        before = self._snapshot_directory()

        cmd = self._build_command(prompt, resume_session=self._session_id)
        if self.debug_mode:
            print(f"\n>>> Interactive mode", file=sys.stderr, flush=True)
            print(f">>> Command: {' '.join(cmd[:8])}...", file=sys.stderr, flush=True)

        # Let Claude own stdin/stdout/stderr for full interactivity
        process = subprocess.Popen(
            cmd,
            cwd=str(self._work_dir),
            env=self._build_env(),
        )
        process.wait()

        duration = time.time() - t0

        after = self._snapshot_directory()
        file_diffs = self._compute_diffs(before, after)

        turn = TurnResult(
            prompt=prompt,
            result_text="(interactive session)",
            session_id=None,
            num_turns=0,
            cost_usd=0.0,
            duration=duration,
            is_error=process.returncode != 0,
            raw_events=[],
            tool_uses=[],
            file_diffs=file_diffs,
        )
        self.conversation_log.turns.append(turn)
        return turn

    def get_clean_log(self) -> str:
        """Format conversation as human-readable text."""
        lines: list[str] = []
        lines.append("=" * 60)
        lines.append("CONVERSATION LOG")
        lines.append("=" * 60)

        for i, turn in enumerate(self.conversation_log.turns, 1):
            lines.append(f"\n--- Turn {i} ---")
            lines.append(f"Prompt: {turn.prompt}")
            lines.append(f"Response: {turn.result_text[:500]}{'...' if len(turn.result_text) > 500 else ''}")

            if turn.tool_uses:
                tools = ", ".join(t["name"] for t in turn.tool_uses)
                lines.append(f"Tools used: {tools}")

            if turn.file_diffs:
                changes = ", ".join(f"{d.path} ({d.status})" for d in turn.file_diffs)
                lines.append(f"Files changed: {changes}")

            lines.append(f"Cost: ${turn.cost_usd:.4f} | Duration: {turn.duration:.1f}s | Turns: {turn.num_turns}")
            if turn.is_error:
                lines.append("*** ERROR ***")

        lines.append(f"\n{'=' * 60}")
        lines.append(f"Totals: ${self.conversation_log.total_cost:.4f} | "
                      f"{self.conversation_log.total_duration:.1f}s | "
                      f"{self.conversation_log.total_num_turns} turn(s)")
        lines.append("=" * 60)
        return "\n".join(lines)

    def get_file_diffs(self) -> list[FileDiff]:
        """Get file diffs from the latest turn."""
        if self.conversation_log.turns:
            return self.conversation_log.turns[-1].file_diffs
        return []

    def get_all_file_diffs(self) -> list[FileDiff]:
        """Get file diffs across all turns."""
        all_diffs: list[FileDiff] = []
        for turn in self.conversation_log.turns:
            all_diffs.extend(turn.file_diffs)
        return all_diffs

    def get_file_content(self, relative_path: str) -> str | None:
        """Read a file from work_dir by relative path."""
        fpath = self._work_dir / relative_path
        try:
            return fpath.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    def list_files(self) -> list[str]:
        """List all non-hidden files in work_dir."""
        files: list[str] = []
        for root, dirs, fnames in os.walk(self._work_dir):
            dirs[:] = [d for d in dirs if d not in HIDDEN_DIRS and not d.startswith(".")]
            for fname in fnames:
                if not fname.startswith("."):
                    rel = str(Path(root, fname).relative_to(self._work_dir))
                    files.append(rel)
        return sorted(files)

    def teardown(self) -> None:
        """Clean up work directory if we created it."""
        if self._owns_work_dir and self._work_dir.exists():
            import shutil
            shutil.rmtree(self._work_dir, ignore_errors=True)
