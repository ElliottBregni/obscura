"""Default @command templates scaffolded by ``/init``."""

from __future__ import annotations

REVIEW = """\
---
description: Code review a file or diff with actionable feedback
argument-hint: "[file-or-path]"
allowed-tools: Read, Grep, Glob, Bash
---

Perform a thorough code review of $ARGUMENTS.

## If $ARGUMENTS is a file path:
1. Read the file
2. Review for: bugs, security issues, performance problems, readability, and adherence to project conventions

## If $ARGUMENTS is empty:
1. Run `git diff` to get the current unstaged changes
2. Review the diff

## Output format:

For each issue found:
```
**[SEVERITY]** file:line — Short description
> code snippet
Suggestion: what to change and why
```

Severities: CRITICAL (bugs/security), WARNING (perf/logic), STYLE (conventions/readability)

End with a summary: X issues found (N critical, N warning, N style).
If the code looks good, say so briefly.
"""

EXPLAIN = """\
---
description: Explain how a function, class, or module works
argument-hint: "[symbol-or-file]"
allowed-tools: Read, Grep, Glob
---

Explain how $ARGUMENTS works in this codebase.

1. Find $ARGUMENTS in the codebase (function, class, module, or file path)
2. Read the relevant code and its callers/callees
3. Explain:
   - **What it does** — one sentence
   - **How it works** — step-by-step walkthrough of the logic
   - **Key dependencies** — what it imports/calls
   - **Who calls it** — upstream callers and how they use it
   - **Edge cases** — notable error handling or boundary conditions

Keep it concise. Use code snippets only when they clarify the explanation.
"""

REFACTOR = """\
---
description: Suggest refactoring improvements for a file or function
argument-hint: "[file-or-symbol]"
allowed-tools: Read, Grep, Glob
---

Analyze $ARGUMENTS for refactoring opportunities.

1. Read the code for $ARGUMENTS
2. Identify its callers and dependents
3. Look for:
   - Duplicated logic that could be extracted
   - Functions doing too many things (single responsibility)
   - Overly complex conditionals or nesting
   - Missing or misused abstractions
   - Dead code or unnecessary indirection
   - Type safety improvements

## Output format:

For each opportunity:
```
### Refactor: [short name]
**Impact:** [low/medium/high]
**What:** [description]
**Before:** [code snippet]
**After:** [suggested code]
**Why:** [benefit]
```

Prioritize by impact. Only suggest changes that provide clear value.
"""

TEST = """\
---
description: Generate tests for a function, class, or module
argument-hint: "[symbol-or-file]"
allowed-tools: Read, Grep, Glob, Bash
---

Generate comprehensive tests for $ARGUMENTS.

1. Read the code for $ARGUMENTS
2. Find existing test patterns in the project (check tests/ directory structure, conftest fixtures, test style)
3. Generate tests covering:
   - Happy path / normal operation
   - Edge cases and boundary values
   - Error handling paths
   - Any async behavior (use project's asyncio_mode = "auto" convention)

## Rules:
- Match the existing test style and conventions in this project
- Use pytest (not unittest)
- Use existing conftest fixtures when available
- Use `from __future__ import annotations` at the top
- Don't mock what you can construct directly
- Name tests descriptively: `test_<what>_<condition>_<expected>`

## Output:
Write the test file. If tests already exist for this code, suggest additions rather than replacing them.
"""

DEBUG = """\
---
description: Investigate and diagnose a bug or error
argument-hint: "[error-message-or-description]"
allowed-tools: Read, Grep, Glob, Bash
---

Investigate the following issue: $ARGUMENTS

## Process:

1. **Search for context** — grep for error messages, function names, or keywords from the description
2. **Trace the execution path** — read the relevant code and follow the call chain
3. **Identify the root cause** — not just where it fails, but why
4. **Check recent changes** — run `git log --oneline -20` and `git diff` to see if recent changes could be related
5. **Propose a fix** — specific code change with explanation

## Output format:

```
## Root Cause
[One paragraph explaining what's wrong and why]

## Evidence
[Key code snippets and log output that confirm the diagnosis]

## Fix
[Specific code change needed]

## Verification
[How to confirm the fix works]
```
"""

DIFF = """\
---
description: Summarize current git changes with context
argument-hint: "[branch-or-ref]"
allowed-tools: Bash, Read, Grep
---

Summarize the current changes in this repo.

## If $ARGUMENTS is provided:
Run `git diff $ARGUMENTS` to compare against that ref.

## If $ARGUMENTS is empty:
Run `git diff` for unstaged changes and `git diff --cached` for staged changes.

## Output format:

For each changed file:
- **file.py** — [one-line summary of what changed and why]

Then a brief overall summary: what this changeset accomplishes as a whole.
Flag any concerns (breaking changes, missing tests, TODOs left behind).
"""

ADD_TOOL = """\
---
description: Scaffold a new system tool with the @tool decorator pattern
argument-hint: "[tool-name] [description]"
allowed-tools: Read, Grep, Glob, Bash
---

Create a new system tool named "$1" in the Obscura codebase.

## Steps:

1. Read `obscura/tools/system/__init__.py` to understand the registration pattern
2. Read one existing tool file for the `@tool` decorator pattern
3. Create the new tool following the project conventions
4. Register the tool in `obscura/tools/system/__init__.py` by importing it
5. Run `ruff check` on the new file
"""

ADD_PROVIDER = """\
---
description: Scaffold a new LLM backend provider
argument-hint: "[provider-name]"
allowed-tools: Read, Grep, Glob, Bash
---

Scaffold a new LLM backend provider named "$1" for Obscura.

## Steps:

1. Read `obscura/providers/claude.py` as the reference implementation
2. Read `obscura/core/types.py` for `BackendProtocol` and `Backend` enum
3. Create `obscura/providers/$1.py` implementing the protocol
4. Add the provider to the `Backend` enum
5. Wire it into provider selection in `obscura/core/client.py`
6. Run `ruff check` and `pyright` on the new file
"""

ADD_HOOK = """\
---
description: Create a new lifecycle hook (before/after on AgentEventKind)
argument-hint: "<event-kind> <before|after>"
allowed-tools: Read, Grep, Glob
---

Create a new hook for the "$1" event kind ($2 phase) in Obscura.

## Steps:

1. Read `obscura/core/hooks.py` for the HookRegistry and decorator pattern
2. Read `obscura/core/types.py` for AgentEventKind to verify "$1" is valid
3. Read `obscura/core/lifecycle.py` for hook factory examples
4. Create the hook following the before/after pattern
5. Suggest where to register it
"""

ADD_PLUGIN = """\
---
description: Scaffold a new plugin manifest and tool handler
argument-hint: "[plugin-name]"
allowed-tools: Read, Grep, Glob, Bash
---

Scaffold a new Obscura plugin named "$1".

## Steps:

1. Read an existing plugin manifest in `obscura/plugins/builtins/` for the TOML format
2. Create the manifest TOML with capabilities, tools, and bootstrap deps
3. Create the tool handler function
4. Run `ruff check` on the new files
"""

TRACE = """\
---
description: Trace an execution path through the codebase
argument-hint: "[entry-point-or-function]"
allowed-tools: Read, Grep, Glob
---

Trace the full execution path starting from $ARGUMENTS.

1. Find $ARGUMENTS in the codebase
2. Follow the call chain step by step, reading each function
3. Document the flow as a call tree with file:line references
4. Note async boundaries, hook invocations, error handling, and state mutations

Keep the trace focused on the main path, not every branch.
"""

IMPACT = """\
---
description: Assess blast radius before changing a symbol
argument-hint: "[function-or-class-name]"
allowed-tools: Read, Grep, Glob, Bash
---

Assess the impact of modifying $ARGUMENTS.

1. Find all definitions of $ARGUMENTS
2. Find all direct callers (d=1 — WILL BREAK)
3. Find indirect callers two levels deep (d=2 — LIKELY AFFECTED)
4. Check test coverage in tests/

## Output format:

```
## Symbol: $ARGUMENTS
**Defined in:** file.py:line

## Direct Dependents (d=1)
- caller() [file.py:line] — how it uses the symbol

## Indirect Dependents (d=2)
- upstream() -> caller() -> symbol

## Test Coverage
- test_file.py::test_name

## Risk: [LOW | MEDIUM | HIGH | CRITICAL]
```
"""

NEW_COMMAND = """\
---
description: Create a new @command
argument-hint: "[name] [what-it-does]"
allowed-tools: Read, Grep, Glob, Bash
---

Create a new @command named "$1".

The command should: $2

## Steps:

1. Read `~/.obscura/commands/README.md` (or `docs/COMMANDS.md` if in the Obscura repo) to understand the command format
2. Read 2-3 existing commands from `~/.obscura/commands/` to match the style
3. Design the command:
   - Write a clear `description` (one line, under 60 chars, shown as preview)
   - Decide if it needs `argument-hint`, `allowed-tools`, or `model` override
   - Write the prompt body with `$ARGUMENTS` / `$1` / `$2` substitution where needed
   - Include conditional behavior if the command should work with or without args
   - Specify a structured output format so results are consistent

4. Write the command file to `~/.obscura/commands/$1.md`
5. Verify the frontmatter parses correctly:
   - `argument-hint` values with `[` or `|` must be quoted
   - `description` should be concise

## Quality checklist:
- Description clearly says what the command does
- Uses $ARGUMENTS (not hardcoded values)
- Has a defined output format
- allowed-tools is set appropriately (don't grant Bash if not needed)
- model override set if the task is simple (haiku) or complex (opus)
"""

NEW_SKILL = """\
---
description: Create a new $skill
argument-hint: "[name] [what-context-it-provides]"
allowed-tools: Read, Grep, Glob, Bash
---

Create a new $skill named "$1".

The skill should provide context about: $2

## Steps:

1. Read `docs/COMMANDS.md` (the $Skills section) to understand the skill format
2. Read 1-2 existing skills from `~/.obscura/skills/` or `~/.claude/skills/` to match the style
3. Design the skill:
   - Skills are **context injections** — background knowledge, not instructions
   - Include: conventions, patterns, anti-patterns, terminology, key decisions
   - Do NOT include step-by-step instructions (that's what @commands are for)
   - Keep it focused — one domain covered well

4. Create the skill directory and file:
   ```
   ~/.obscura/skills/$1/SKILL.md
   ```

5. Write the SKILL.md with frontmatter:
   ```markdown
   ---
   name: $1
   description: [one-line description of what context this provides]
   ---

   [skill content here]
   ```

## Good skill content:
- Project-specific conventions ("we use X pattern for Y")
- Domain terminology and definitions
- Key architectural decisions and rationale
- Common patterns and anti-patterns
- References to important files or modules

## Bad skill content:
- Step-by-step instructions (use @commands instead)
- Entire file contents copy-pasted
- Generic knowledge the LLM already has
- Stale information that changes frequently
"""

# Map of filename -> content for init scaffolding
DEFAULT_COMMANDS: dict[str, str] = {
    "review.md": REVIEW,
    "explain.md": EXPLAIN,
    "refactor.md": REFACTOR,
    "test.md": TEST,
    "debug.md": DEBUG,
    "diff.md": DIFF,
    "add-tool.md": ADD_TOOL,
    "add-provider.md": ADD_PROVIDER,
    "add-hook.md": ADD_HOOK,
    "add-plugin.md": ADD_PLUGIN,
    "trace.md": TRACE,
    "impact.md": IMPACT,
    "new-command.md": NEW_COMMAND,
    "new-skill.md": NEW_SKILL,
}
