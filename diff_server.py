"""
diff_server — Local web UI for reviewing file diffs with inline commenting.

Starts a temporary HTTP server, opens a browser with rich diff rendering
(via diff2html CDN), collects line-level and overall feedback, and returns
it as a formatted string suitable for derive_expectations().

Zero third-party Python dependencies. Uses only stdlib + CDN JS/CSS.
"""

from __future__ import annotations

import json
import socket
import threading
import webbrowser
from dataclasses import dataclass, field
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any

from claude_gym import FileDiff


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class LineComment:
    file_path: str
    start_line: int
    end_line: int
    comment: str


@dataclass
class DiffFeedback:
    line_comments: list[LineComment] = field(default_factory=list)
    overall_feedback: str = ""


# ---------------------------------------------------------------------------
# Feedback formatting
# ---------------------------------------------------------------------------

def _format_feedback(fb: DiffFeedback) -> str:
    """Serialize DiffFeedback into natural-language text for derive_expectations()."""
    parts: list[str] = []

    if fb.overall_feedback.strip():
        parts.append(f"Overall: {fb.overall_feedback.strip()}")

    for lc in fb.line_comments:
        if lc.start_line == lc.end_line:
            parts.append(f"On {lc.file_path} line {lc.start_line}:\n{lc.comment}")
        else:
            parts.append(f"On {lc.file_path} lines {lc.start_line}-{lc.end_line}:\n{lc.comment}")

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Terminal fallback
# ---------------------------------------------------------------------------

def _terminal_fallback(diffs: list[FileDiff]) -> str:
    """Collect feedback via terminal when browser UI is unavailable."""
    print("\n(Browser UI unavailable — falling back to terminal input)")
    print("Enter feedback (or 'done'). End with a blank line:")
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


# ---------------------------------------------------------------------------
# Port finder
# ---------------------------------------------------------------------------

def _find_free_port() -> int:
    """Bind to port 0 and return the OS-assigned port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# HTML page (embedded)
# ---------------------------------------------------------------------------

def _build_html(diffs: list[FileDiff]) -> str:
    """Build the complete HTML page with diff data injected."""
    diff_data = []
    for d in diffs:
        diff_data.append({
            "path": d.path,
            "status": d.status,
            "unified_diff": d.unified_diff,
        })

    diff_json = json.dumps(diff_data)

    return _HTML_TEMPLATE.replace("__DIFF_DATA_PLACEHOLDER__", diff_json)


_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Diff Review — Skill Evaluator</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/diff2html/3.4.48/bundles/css/diff2html.min.css">
<style>
:root {
  --bg: #1e1e2e;
  --surface: #262637;
  --border: #3a3a52;
  --text: #cdd6f4;
  --text-muted: #9399b2;
  --accent: #89b4fa;
  --accent-hover: #74c7ec;
  --green: #a6e3a1;
  --red: #f38ba8;
  --yellow: #f9e2af;
  --comment-bg: #313244;
}

* { box-sizing: border-box; margin: 0; padding: 0; }

body {
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, monospace;
  line-height: 1.5;
  padding: 0;
}

.header {
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  padding: 16px 24px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  position: sticky;
  top: 0;
  z-index: 100;
}

.header h1 {
  font-size: 18px;
  font-weight: 600;
}

.header .file-count {
  color: var(--text-muted);
  font-size: 14px;
}

.container { max-width: 1200px; margin: 0 auto; padding: 24px; }

/* File sections */
.file-section {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  margin-bottom: 16px;
  overflow: hidden;
}

.file-header {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 16px;
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  cursor: pointer;
  user-select: none;
}

.file-header:hover { background: var(--comment-bg); }

.file-header .badge {
  font-size: 11px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 4px;
  text-transform: uppercase;
}

.badge-added { background: var(--green); color: #1e1e2e; }
.badge-modified { background: var(--yellow); color: #1e1e2e; }
.badge-deleted { background: var(--red); color: #1e1e2e; }

.file-header .path {
  font-family: 'SF Mono', 'Fira Code', monospace;
  font-size: 14px;
  flex: 1;
}

.file-header .toggle {
  color: var(--text-muted);
  font-size: 12px;
  transition: transform 0.2s;
}

.file-section.collapsed .toggle { transform: rotate(-90deg); }
.file-section.collapsed .file-body { display: none; }

.file-body { position: relative; }

/* diff2html overrides for dark theme */
.d2h-wrapper { background: transparent !important; }
.d2h-file-wrapper { border: none !important; margin: 0 !important; border-radius: 0 !important; }
.d2h-file-header { display: none !important; }
.d2h-code-linenumber { background: var(--surface) !important; color: var(--text-muted) !important; border-color: var(--border) !important; cursor: pointer; }
.d2h-code-linenumber:hover { background: var(--comment-bg) !important; color: var(--accent) !important; }
.d2h-code-line { background: var(--bg) !important; color: var(--text) !important; }
.d2h-code-line-ctn { background: transparent !important; }
.d2h-del { background: rgba(243, 139, 168, 0.1) !important; }
.d2h-ins { background: rgba(166, 227, 161, 0.1) !important; }
.d2h-code-side-line { background: var(--bg) !important; }
.d2h-file-diff { overflow-x: auto; }
.d2h-code-line del { background: rgba(243, 139, 168, 0.3) !important; }
.d2h-code-line ins { background: rgba(166, 227, 161, 0.3) !important; }
table.d2h-diff-table { font-family: 'SF Mono', 'Fira Code', monospace; font-size: 13px; }

/* Fallback raw diff */
.raw-diff {
  padding: 16px;
  font-family: 'SF Mono', 'Fira Code', monospace;
  font-size: 13px;
  white-space: pre-wrap;
  word-break: break-all;
  overflow-x: auto;
}
.raw-diff .add { color: var(--green); }
.raw-diff .del { color: var(--red); }
.raw-diff .hunk { color: var(--accent); }

/* Inline comment form */
.inline-comment {
  background: var(--comment-bg);
  border-top: 1px solid var(--border);
  border-bottom: 1px solid var(--border);
  padding: 12px 16px;
  display: flex;
  gap: 8px;
  align-items: flex-start;
}

.inline-comment .line-ref {
  color: var(--accent);
  font-size: 12px;
  font-family: monospace;
  white-space: nowrap;
  padding-top: 6px;
}

.inline-comment textarea {
  flex: 1;
  background: var(--bg);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 8px 12px;
  font-size: 13px;
  font-family: inherit;
  resize: vertical;
  min-height: 60px;
}

.inline-comment textarea:focus { outline: none; border-color: var(--accent); }

.inline-comment .btn-sm {
  padding: 4px 12px;
  font-size: 12px;
  border-radius: 4px;
  border: none;
  cursor: pointer;
  background: var(--accent);
  color: #1e1e2e;
  font-weight: 600;
}

.inline-comment .btn-sm:hover { background: var(--accent-hover); }

.inline-comment .btn-cancel {
  background: transparent;
  color: var(--text-muted);
  border: 1px solid var(--border);
}

/* Saved comment display */
.saved-comment {
  background: var(--comment-bg);
  border-left: 3px solid var(--accent);
  padding: 8px 16px;
  margin: 0;
  font-size: 13px;
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
}

.saved-comment .comment-text { flex: 1; white-space: pre-wrap; }

.saved-comment .comment-meta {
  color: var(--text-muted);
  font-size: 11px;
  margin-bottom: 4px;
}

.saved-comment .btn-remove {
  color: var(--text-muted);
  background: none;
  border: none;
  cursor: pointer;
  font-size: 16px;
  padding: 0 4px;
}

.saved-comment .btn-remove:hover { color: var(--red); }

/* Overall feedback */
.feedback-section {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 20px;
  margin-top: 24px;
}

.feedback-section h2 {
  font-size: 16px;
  margin-bottom: 12px;
}

.feedback-section textarea {
  width: 100%;
  background: var(--bg);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 12px;
  font-size: 14px;
  font-family: inherit;
  resize: vertical;
  min-height: 100px;
}

.feedback-section textarea:focus { outline: none; border-color: var(--accent); }

/* Actions */
.actions {
  display: flex;
  gap: 12px;
  margin-top: 16px;
  justify-content: flex-end;
}

.btn {
  padding: 10px 24px;
  border-radius: 6px;
  border: none;
  cursor: pointer;
  font-size: 14px;
  font-weight: 600;
  transition: background 0.15s;
}

.btn-primary { background: var(--accent); color: #1e1e2e; }
.btn-primary:hover { background: var(--accent-hover); }
.btn-secondary { background: transparent; color: var(--text-muted); border: 1px solid var(--border); }
.btn-secondary:hover { background: var(--comment-bg); }

.kbd {
  font-size: 11px;
  color: var(--text-muted);
  margin-left: 6px;
}

/* Comment counter badge */
.comment-count {
  background: var(--accent);
  color: #1e1e2e;
  font-size: 11px;
  font-weight: 700;
  padding: 1px 6px;
  border-radius: 10px;
  margin-left: 8px;
  display: none;
}

.comment-count.visible { display: inline; }
</style>
</head>
<body>

<div class="header">
  <div>
    <h1>Diff Review</h1>
  </div>
  <div class="file-count" id="fileCount"></div>
</div>

<div class="container">
  <div id="diffContainer"></div>

  <div class="feedback-section">
    <h2>Overall Feedback</h2>
    <textarea id="overallFeedback" placeholder="Describe what should change overall (e.g., 'tests should use @Test, code should compile cleanly')..."></textarea>
    <div class="actions">
      <button class="btn btn-secondary" onclick="submitSkip()">Skip <span class="kbd">Esc</span></button>
      <button class="btn btn-primary" onclick="submitFeedback()">Submit Feedback <span class="kbd">Ctrl+Enter</span></button>
    </div>
  </div>
</div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/diff2html/3.4.48/bundles/js/diff2html-ui.min.js"></script>
<script>
// Diff data injected by Python server
window.__DIFF_DATA__ = __DIFF_DATA_PLACEHOLDER__;

const comments = []; // {file_path, start_line, end_line, comment}
let activeCommentForm = null;

function init() {
  const container = document.getElementById('diffContainer');
  const diffs = window.__DIFF_DATA__;

  document.getElementById('fileCount').textContent = diffs.length + ' file' + (diffs.length !== 1 ? 's' : '') + ' changed';

  diffs.forEach((d, idx) => {
    const section = document.createElement('div');
    section.className = 'file-section';
    section.dataset.path = d.path;

    const badgeClass = 'badge-' + d.status;

    section.innerHTML =
      '<div class="file-header" onclick="toggleSection(this.parentElement)">' +
        '<span class="badge ' + badgeClass + '">' + d.status + '</span>' +
        '<span class="path">' + escapeHtml(d.path) + '</span>' +
        '<span class="comment-count" id="cc-' + idx + '">0</span>' +
        '<span class="toggle">&#9660;</span>' +
      '</div>' +
      '<div class="file-body" id="body-' + idx + '"></div>';

    container.appendChild(section);

    const body = document.getElementById('body-' + idx);

    if (d.unified_diff) {
      renderDiff(body, d.unified_diff, d.path, idx);
    } else {
      body.innerHTML = '<div class="raw-diff">(no diff content)</div>';
    }
  });
}

function renderDiff(container, unifiedDiff, filePath, fileIdx) {
  // Try diff2html first
  if (window.Diff2HtmlUI) {
    try {
      const target = document.createElement('div');
      target.id = 'diff2html-' + fileIdx;
      container.appendChild(target);

      const diff2htmlUi = new Diff2HtmlUI(target, unifiedDiff, {
        drawFileList: false,
        matching: 'lines',
        outputFormat: 'line-by-line',
        highlight: false,
        renderNothingWhenEmpty: false,
      });
      diff2htmlUi.draw();

      // Add click handlers to line numbers
      setTimeout(function() { attachLineClickHandlers(target, filePath, fileIdx); }, 100);
      return;
    } catch (e) {
      console.warn('diff2html failed, falling back to raw:', e);
    }
  }

  // Fallback: raw diff with basic coloring
  renderRawDiff(container, unifiedDiff);
}

function renderRawDiff(container, unifiedDiff) {
  const pre = document.createElement('div');
  pre.className = 'raw-diff';
  const lines = unifiedDiff.split('\n');
  lines.forEach(function(line) {
    const span = document.createElement('span');
    if (line.startsWith('+') && !line.startsWith('+++')) {
      span.className = 'add';
    } else if (line.startsWith('-') && !line.startsWith('---')) {
      span.className = 'del';
    } else if (line.startsWith('@@')) {
      span.className = 'hunk';
    }
    span.textContent = line;
    pre.appendChild(span);
    pre.appendChild(document.createTextNode('\n'));
  });
  container.appendChild(pre);
}

function attachLineClickHandlers(target, filePath, fileIdx) {
  const lineNumbers = target.querySelectorAll('.d2h-code-linenumber');
  lineNumbers.forEach(function(ln) {
    ln.addEventListener('click', function(e) {
      e.stopPropagation();
      // Extract line number from the element
      const numEl = ln.querySelector('.line-num2') || ln.querySelector('.line-num1');
      let lineNum = 0;
      if (numEl) {
        lineNum = parseInt(numEl.textContent.trim(), 10) || 0;
      }
      if (lineNum > 0) {
        openCommentForm(ln.closest('tr'), filePath, lineNum, fileIdx);
      }
    });
  });
}

function openCommentForm(afterRow, filePath, lineNum, fileIdx) {
  // Remove any existing comment form
  closeCommentForm();

  const tr = document.createElement('tr');
  tr.className = 'inline-comment-row';
  const td = document.createElement('td');
  td.colSpan = 10;
  td.innerHTML =
    '<div class="inline-comment">' +
      '<span class="line-ref">L' + lineNum + '</span>' +
      '<textarea id="commentInput" placeholder="Add a comment about this line..." autofocus></textarea>' +
      '<div style="display:flex;flex-direction:column;gap:4px">' +
        '<button class="btn-sm" onclick="saveComment(\'' + escapeAttr(filePath) + '\',' + lineNum + ',' + fileIdx + ')">Add</button>' +
        '<button class="btn-sm btn-cancel" onclick="closeCommentForm()">Cancel</button>' +
      '</div>' +
    '</div>';
  tr.appendChild(td);

  if (afterRow && afterRow.parentNode) {
    afterRow.parentNode.insertBefore(tr, afterRow.nextSibling);
  }

  activeCommentForm = tr;

  const input = document.getElementById('commentInput');
  if (input) {
    input.focus();
    input.addEventListener('keydown', function(e) {
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
        saveComment(filePath, lineNum, fileIdx);
      } else if (e.key === 'Escape') {
        closeCommentForm();
      }
    });
  }
}

function closeCommentForm() {
  if (activeCommentForm) {
    activeCommentForm.remove();
    activeCommentForm = null;
  }
}

function saveComment(filePath, lineNum, fileIdx) {
  const input = document.getElementById('commentInput');
  if (!input || !input.value.trim()) return;

  comments.push({
    file_path: filePath,
    start_line: lineNum,
    end_line: lineNum,
    comment: input.value.trim()
  });

  // Show saved comment in place of the form
  const row = activeCommentForm;
  if (row) {
    const td = row.querySelector('td');
    td.innerHTML =
      '<div class="saved-comment">' +
        '<div>' +
          '<div class="comment-meta">Line ' + lineNum + '</div>' +
          '<div class="comment-text">' + escapeHtml(input.value.trim()) + '</div>' +
        '</div>' +
        '<button class="btn-remove" onclick="removeComment(this, ' + (comments.length - 1) + ', ' + fileIdx + ')">&times;</button>' +
      '</div>';
    row.className = 'saved-comment-row';
    activeCommentForm = null;
  }

  updateCommentCount(fileIdx);
}

function removeComment(btn, commentIdx, fileIdx) {
  comments.splice(commentIdx, 1);
  const row = btn.closest('tr');
  if (row) row.remove();
  updateCommentCount(fileIdx);
  // Re-index remaining remove buttons (simplified — works for typical usage)
}

function updateCommentCount(fileIdx) {
  const diffs = window.__DIFF_DATA__;
  const filePath = diffs[fileIdx].path;
  const count = comments.filter(function(c) { return c.file_path === filePath; }).length;
  const badge = document.getElementById('cc-' + fileIdx);
  if (badge) {
    badge.textContent = count;
    badge.className = 'comment-count' + (count > 0 ? ' visible' : '');
  }
}

function toggleSection(section) {
  section.classList.toggle('collapsed');
}

function escapeHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function escapeAttr(s) {
  return s.replace(/\\/g,'\\\\').replace(/'/g,"\\'");
}

function submitFeedback() {
  const overall = document.getElementById('overallFeedback').value;
  const payload = {
    line_comments: comments,
    overall_feedback: overall
  };

  fetch('/api/feedback', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  }).then(function() {
    document.body.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100vh;color:#a6e3a1;font-size:18px;">Feedback submitted. You can close this tab.</div>';
  }).catch(function(err) {
    alert('Failed to submit: ' + err);
  });
}

function submitSkip() {
  fetch('/api/cancel', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: '{}'
  }).then(function() {
    document.body.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100vh;color:#9399b2;font-size:18px;">Skipped. You can close this tab.</div>';
  });
}

// Keyboard shortcuts
document.addEventListener('keydown', function(e) {
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey) && document.activeElement.id !== 'commentInput') {
    submitFeedback();
  } else if (e.key === 'Escape' && !activeCommentForm) {
    submitSkip();
  }
});

// Beacon on tab close so server doesn't hang
window.addEventListener('beforeunload', function() {
  navigator.sendBeacon('/api/cancel', '{}');
});

init();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class _DiffReviewHandler(BaseHTTPRequestHandler):
    """Handles GET / (HTML page) and POST /api/feedback, /api/cancel."""

    # These are set on the class before the server starts
    html_content: str = ""
    feedback_result: dict[str, Any] | None = None
    feedback_event: threading.Event = threading.Event()
    cancelled: bool = False

    def do_GET(self) -> None:
        if self.path == "/" or self.path == "":
            self._send_response(200, "text/html", self.html_content.encode())
        else:
            self._send_response(404, "text/plain", b"Not found")

    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b""

        if self.path == "/api/feedback":
            try:
                data = json.loads(body) if body else {}
            except json.JSONDecodeError:
                data = {}
            _DiffReviewHandler.feedback_result = data
            _DiffReviewHandler.cancelled = False
            _DiffReviewHandler.feedback_event.set()
            self._send_response(200, "application/json", b'{"ok":true}')

        elif self.path == "/api/cancel":
            if not _DiffReviewHandler.feedback_event.is_set():
                _DiffReviewHandler.cancelled = True
                _DiffReviewHandler.feedback_event.set()
            self._send_response(200, "application/json", b'{"ok":true}')

        else:
            self._send_response(404, "text/plain", b"Not found")

    def _send_response(self, code: int, content_type: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        # Suppress request logging
        pass


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def present_diff_for_review(diffs: list[FileDiff], timeout: int = 600) -> str:
    """Start a local web server, open browser with diff review UI, and block
    until the user submits feedback or the timeout expires.

    Returns a formatted feedback string suitable for derive_expectations().
    Returns empty string if the user skips/cancels.
    """
    if not diffs:
        return ""

    # Reset handler state
    _DiffReviewHandler.feedback_result = None
    _DiffReviewHandler.feedback_event = threading.Event()
    _DiffReviewHandler.cancelled = False
    _DiffReviewHandler.html_content = _build_html(diffs)

    port = _find_free_port()
    server = HTTPServer(("127.0.0.1", port), _DiffReviewHandler)

    # Run server in daemon thread
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    url = f"http://127.0.0.1:{port}"
    print(f"\n[Diff review UI] Opening browser: {url}")

    try:
        webbrowser.open(url)
    except Exception:
        print(f"  Could not open browser automatically. Navigate to: {url}")

    print("  Waiting for feedback (submit in browser, or Ctrl+C for terminal fallback)...")

    try:
        got_feedback = _DiffReviewHandler.feedback_event.wait(timeout=timeout)
    except KeyboardInterrupt:
        print("\n  Interrupted.")
        server.shutdown()
        return _terminal_fallback(diffs)

    server.shutdown()

    if not got_feedback:
        print("\n  Timed out waiting for browser feedback.")
        return _terminal_fallback(diffs)

    if _DiffReviewHandler.cancelled:
        print("  Browser session cancelled.")
        return _terminal_fallback(diffs)

    # Parse the feedback
    data = _DiffReviewHandler.feedback_result or {}
    line_comments = []
    for lc in data.get("line_comments", []):
        line_comments.append(LineComment(
            file_path=lc.get("file_path", ""),
            start_line=lc.get("start_line", 0),
            end_line=lc.get("end_line", 0),
            comment=lc.get("comment", ""),
        ))

    fb = DiffFeedback(
        line_comments=line_comments,
        overall_feedback=data.get("overall_feedback", ""),
    )

    formatted = _format_feedback(fb)

    if formatted:
        print(f"\n  Feedback received ({len(line_comments)} inline comment(s))")
    else:
        print("  No feedback provided.")

    return formatted
