"""
sdk.tui.themes -- Dark and light theme CSS definitions for the TUI.

Provides Textual CSS stylesheets for the Obscura TUI application.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Dark theme (default)
# ---------------------------------------------------------------------------

DARK_THEME = """\
/* ── Global ───────────────────────────────────────────────────── */
Screen {
    background: #1a1b26;
    color: #c0caf5;
}

/* ── Sidebar ──────────────────────────────────────────────────── */
#sidebar {
    width: 28;
    background: #16161e;
    border-right: solid #292e42;
    padding: 1;
}

#sidebar.hidden {
    display: none;
}

#sidebar .sidebar-title {
    text-style: bold;
    color: #7aa2f7;
    padding-bottom: 1;
}

#sidebar .sidebar-section {
    padding-top: 1;
    color: #565f89;
    text-style: bold;
}

#sidebar .sidebar-value {
    color: #a9b1d6;
    padding-left: 2;
}

#sidebar .mode-item {
    padding-left: 2;
    color: #565f89;
}

#sidebar .mode-item.active {
    color: #7aa2f7;
    text-style: bold;
}

/* ── Message List ─────────────────────────────────────────────── */
#message-list {
    height: 1fr;
    overflow-y: auto;
    padding: 0 1;
}

/* ── Message Bubbles ──────────────────────────────────────────── */
.message-bubble {
    margin: 1 0;
    padding: 1 2;
}

.message-bubble.user {
    background: #292e42;
    border: solid #3b4261;
    color: #c0caf5;
}

.message-bubble.assistant {
    background: #1e2030;
    border: solid #292e42;
    color: #c0caf5;
}

.message-bubble .role-label {
    text-style: bold;
    padding-bottom: 1;
}

.message-bubble .role-label.user {
    color: #9ece6a;
}

.message-bubble .role-label.assistant {
    color: #7aa2f7;
}

.message-bubble .error-text {
    color: #f7768e;
    text-style: bold;
}

/* ── Thinking Block ───────────────────────────────────────────── */
.thinking-block {
    margin: 0 0;
    padding: 0 1;
}

.thinking-block .thinking-header {
    color: #bb9af7;
    text-style: bold italic;
}

.thinking-block .thinking-content {
    color: #565f89;
    padding-left: 2;
}

.thinking-block.collapsed .thinking-content {
    display: none;
}

/* ── Tool Status ──────────────────────────────────────────────── */
.tool-status {
    margin: 0 2;
    padding: 0 1;
    height: auto;
}

.tool-status .tool-name {
    color: #e0af68;
    text-style: bold;
}

.tool-status .tool-spinner {
    color: #e0af68;
}

.tool-status .tool-result {
    color: #565f89;
    padding-left: 4;
}

.tool-status.complete .tool-name {
    color: #9ece6a;
}

.tool-status.error .tool-name {
    color: #f7768e;
}

/* ── Input Area ───────────────────────────────────────────────── */
#input-area {
    dock: bottom;
    height: auto;
    max-height: 10;
    min-height: 3;
    background: #16161e;
    border-top: solid #292e42;
    padding: 0 1;
}

#input-area .mode-prefix {
    color: #7aa2f7;
    text-style: bold;
    width: auto;
    padding: 1 0;
}

#input-area TextArea {
    background: #16161e;
    color: #c0caf5;
    border: none;
    height: auto;
    min-height: 1;
    max-height: 8;
}

#input-area TextArea:focus {
    border: none;
}

/* ── Status Bar ───────────────────────────────────────────────── */
#status-bar {
    dock: bottom;
    height: 1;
    background: #292e42;
    color: #565f89;
    padding: 0 1;
}

#status-bar .status-mode {
    text-style: bold;
    color: #7aa2f7;
}

#status-bar .status-model {
    color: #a9b1d6;
}

#status-bar .status-session {
    color: #565f89;
}

#status-bar .status-timing {
    color: #e0af68;
}

#status-bar .status-streaming {
    color: #9ece6a;
    text-style: bold;
}

/* ── Plan View ────────────────────────────────────────────────── */
.plan-view {
    padding: 1 2;
}

.plan-view .plan-title {
    text-style: bold;
    color: #7aa2f7;
    padding-bottom: 1;
}

.plan-step {
    padding: 0 0 0 2;
    height: auto;
}

.plan-step .step-number {
    color: #e0af68;
    text-style: bold;
}

.plan-step .step-text {
    color: #c0caf5;
}

.plan-step.approved .step-text {
    color: #9ece6a;
}

.plan-step.rejected .step-text {
    color: #f7768e;
    text-style: strike;
}

.plan-step.edited .step-text {
    color: #e0af68;
}

.plan-summary {
    padding: 1 2;
    color: #565f89;
}

/* ── Diff View ────────────────────────────────────────────────── */
.diff-view {
    padding: 0;
    overflow-y: auto;
}

.diff-view .diff-header {
    text-style: bold;
    color: #7aa2f7;
    padding: 0 1;
    background: #292e42;
}

.diff-view .diff-hunk-header {
    color: #bb9af7;
    padding: 0 1;
    background: #1e2030;
}

.diff-view .diff-line-add {
    color: #9ece6a;
    background: #1a2e1a;
}

.diff-view .diff-line-del {
    color: #f7768e;
    background: #2e1a1a;
}

.diff-view .diff-line-ctx {
    color: #565f89;
}

.diff-view .diff-gutter {
    color: #3b4261;
    width: 6;
}

.diff-view .hunk-selected {
    border-left: thick #7aa2f7;
}

/* ── File Tree ────────────────────────────────────────────────── */
.file-tree {
    padding: 0 1;
}

.file-tree .file-entry {
    color: #a9b1d6;
    padding-left: 1;
}

.file-tree .file-entry.accepted {
    color: #9ece6a;
}

.file-tree .file-entry.rejected {
    color: #f7768e;
}

.file-tree .file-entry.pending {
    color: #e0af68;
}

/* ── Scrollbar ────────────────────────────────────────────────── */
Scrollbar {
    background: #16161e;
}

ScrollbarSlider {
    color: #292e42;
}

ScrollbarSlider:hover {
    color: #3b4261;
}
"""

# ---------------------------------------------------------------------------
# Light theme
# ---------------------------------------------------------------------------

LIGHT_THEME = """\
/* ── Global ───────────────────────────────────────────────────── */
Screen {
    background: #f5f5f5;
    color: #343b58;
}

/* ── Sidebar ──────────────────────────────────────────────────── */
#sidebar {
    width: 28;
    background: #e8e8e8;
    border-right: solid #d0d0d0;
    padding: 1;
}

#sidebar.hidden {
    display: none;
}

#sidebar .sidebar-title {
    text-style: bold;
    color: #2e7de9;
    padding-bottom: 1;
}

#sidebar .sidebar-section {
    padding-top: 1;
    color: #8990a3;
    text-style: bold;
}

#sidebar .sidebar-value {
    color: #343b58;
    padding-left: 2;
}

#sidebar .mode-item {
    padding-left: 2;
    color: #8990a3;
}

#sidebar .mode-item.active {
    color: #2e7de9;
    text-style: bold;
}

/* ── Message List ─────────────────────────────────────────────── */
#message-list {
    height: 1fr;
    overflow-y: auto;
    padding: 0 1;
}

/* ── Message Bubbles ──────────────────────────────────────────── */
.message-bubble {
    margin: 1 0;
    padding: 1 2;
}

.message-bubble.user {
    background: #e0e5f0;
    border: solid #c8cdd8;
    color: #343b58;
}

.message-bubble.assistant {
    background: #ffffff;
    border: solid #d0d0d0;
    color: #343b58;
}

.message-bubble .role-label {
    text-style: bold;
    padding-bottom: 1;
}

.message-bubble .role-label.user {
    color: #587539;
}

.message-bubble .role-label.assistant {
    color: #2e7de9;
}

.message-bubble .error-text {
    color: #f52a65;
    text-style: bold;
}

/* ── Thinking Block ───────────────────────────────────────────── */
.thinking-block {
    margin: 0 0;
    padding: 0 1;
}

.thinking-block .thinking-header {
    color: #7847bd;
    text-style: bold italic;
}

.thinking-block .thinking-content {
    color: #8990a3;
    padding-left: 2;
}

.thinking-block.collapsed .thinking-content {
    display: none;
}

/* ── Tool Status ──────────────────────────────────────────────── */
.tool-status {
    margin: 0 2;
    padding: 0 1;
    height: auto;
}

.tool-status .tool-name {
    color: #8c6c3e;
    text-style: bold;
}

.tool-status .tool-result {
    color: #8990a3;
    padding-left: 4;
}

.tool-status.complete .tool-name {
    color: #587539;
}

.tool-status.error .tool-name {
    color: #f52a65;
}

/* ── Input Area ───────────────────────────────────────────────── */
#input-area {
    dock: bottom;
    height: auto;
    max-height: 10;
    min-height: 3;
    background: #e8e8e8;
    border-top: solid #d0d0d0;
    padding: 0 1;
}

#input-area .mode-prefix {
    color: #2e7de9;
    text-style: bold;
    width: auto;
    padding: 1 0;
}

#input-area TextArea {
    background: #e8e8e8;
    color: #343b58;
    border: none;
    height: auto;
    min-height: 1;
    max-height: 8;
}

/* ── Status Bar ───────────────────────────────────────────────── */
#status-bar {
    dock: bottom;
    height: 1;
    background: #d0d0d0;
    color: #8990a3;
    padding: 0 1;
}

#status-bar .status-mode {
    text-style: bold;
    color: #2e7de9;
}

#status-bar .status-model {
    color: #343b58;
}

#status-bar .status-timing {
    color: #8c6c3e;
}

#status-bar .status-streaming {
    color: #587539;
    text-style: bold;
}

/* ── Plan View ────────────────────────────────────────────────── */
.plan-view {
    padding: 1 2;
}

.plan-view .plan-title {
    text-style: bold;
    color: #2e7de9;
    padding-bottom: 1;
}

.plan-step {
    padding: 0 0 0 2;
    height: auto;
}

.plan-step .step-number {
    color: #8c6c3e;
    text-style: bold;
}

.plan-step .step-text {
    color: #343b58;
}

.plan-step.approved .step-text {
    color: #587539;
}

.plan-step.rejected .step-text {
    color: #f52a65;
    text-style: strike;
}

/* ── Diff View ────────────────────────────────────────────────── */
.diff-view .diff-line-add {
    color: #587539;
    background: #e0f0e0;
}

.diff-view .diff-line-del {
    color: #f52a65;
    background: #f0e0e0;
}

.diff-view .diff-line-ctx {
    color: #8990a3;
}

/* ── File Tree ────────────────────────────────────────────────── */
.file-tree .file-entry.accepted {
    color: #587539;
}

.file-tree .file-entry.rejected {
    color: #f52a65;
}

.file-tree .file-entry.pending {
    color: #8c6c3e;
}
"""


def get_theme_css(theme: str = "dark") -> str:
    """Return the CSS for the requested theme.

    Args:
        theme: Either 'dark' or 'light'.

    Returns:
        The full CSS stylesheet as a string.
    """
    if theme == "light":
        return LIGHT_THEME
    return DARK_THEME
