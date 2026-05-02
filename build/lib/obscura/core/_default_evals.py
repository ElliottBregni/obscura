"""Default eval suites for built-in @commands."""

from __future__ import annotations

REVIEW_EVAL = """\
---
runs: 1
---

## Test: File with known patterns
input: obscura/core/paths.py
preferred-tools: read_text_file, grep_files, git_diff
criteria:
  - Identifies the file and reads it successfully
  - Uses CRITICAL/WARNING/STYLE severity format
  - Provides actionable suggestions (not just descriptions)
  - Ends with a summary count of issues

## Test: Empty diff
input:
preferred-tools: run_command, git_diff
criteria:
  - Runs git diff to check for changes
  - Handles the case where there are no changes gracefully
  - Does not fabricate issues
"""

EXPLAIN_EVAL = """\
---
runs: 1
---

## Test: Known function
input: parse_frontmatter
preferred-tools: grep_files, read_text_file, find_files
criteria:
  - Finds the function definition in the codebase
  - Explains what it does in one sentence
  - Describes the step-by-step logic
  - Identifies callers or usage
  - Mentions key dependencies

## Test: Module-level explanation
input: obscura/core/paths.py
preferred-tools: read_text_file
criteria:
  - Reads the file
  - Explains the module's purpose
  - Lists the key functions and what they do
"""

TEST_EVAL = """\
---
runs: 1
---

## Test: Generate tests for a utility
input: parse_frontmatter
preferred-tools: grep_files, read_text_file, find_files, write_text_file
criteria:
  - Finds the function in the codebase
  - Uses pytest (not unittest)
  - Includes happy path test
  - Includes edge case test
  - Uses from __future__ import annotations
  - Test names follow test_<what>_<condition>_<expected> pattern
"""

DEBUG_EVAL = """\
---
runs: 1
---

## Test: Investigate a symbol
input: LazyCommandLoader discover_commands
preferred-tools: grep_files, read_text_file, run_command, git_log
criteria:
  - Searches the codebase for relevant code
  - Reads the relevant function
  - Provides a Root Cause section
  - Provides an Evidence section
  - Provides a Fix section
  - Provides a Verification section
"""

IMPACT_EVAL = """\
---
runs: 1
---

## Test: Assess impact of a function
input: parse_frontmatter
preferred-tools: grep_files, read_text_file, find_files
criteria:
  - Finds the function definition with file and line
  - Lists direct dependents (d=1)
  - Lists indirect dependents (d=2)
  - Checks test coverage
  - Provides a risk level assessment
"""

TRACE_EVAL = """\
---
runs: 1
---

## Test: Trace a function
input: resolve_obscura_home
preferred-tools: grep_files, read_text_file
criteria:
  - Finds the function in the codebase
  - Follows the call chain with file:line references
  - Documents the flow as a tree or sequential list
  - Notes any conditional branches
"""

PYTIGHT_EVAL = """\
---
runs: 1
---

## Test: Lint a Python module
input: obscura/core/paths.py
preferred-tools: run_command, read_text_file, edit_text_file
criteria:
  - Runs ruff check on the target
  - Runs ruff format check on the target
  - Runs pyright on the target
  - Reports results in the structured output format
  - Attempts auto-fix for safe issues
  - Reports final PASS/FAIL status
"""

DEFAULT_EVALS: dict[str, str] = {
    "review": REVIEW_EVAL,
    "explain": EXPLAIN_EVAL,
    "test": TEST_EVAL,
    "debug": DEBUG_EVAL,
    "impact": IMPACT_EVAL,
    "trace": TRACE_EVAL,
    "pytight": PYTIGHT_EVAL,
}
