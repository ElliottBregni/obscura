# Performance primitives

Cross-project operational defaults. These move latency, accuracy, and token
cost in the right direction regardless of the task.

## Parallelize what's independent
- Multiple read-only tool calls with no data dependency → send in **one
  message**, not one after another. Saves a full round-trip per call.
- Sequential dependency (call B uses output of A) → keep them serial.

## Right tool, right job
- File edits → `edit_text_file` / `write_text_file`. Never `bash cat <<EOF`
  or `sed -i`. The dedicated tools show diffs and apply atomically.
- File reads → `read_text_file`. Never `bash cat`.
- Code search → `grep_files`. Never `bash grep`.
- Find by name → `find_files`. Never `bash find`.
- Shell-only operations (running tests, git, builds) → `run_command`.

## Discover before guessing
- Unknown tool name? → `tool_search` first, never invent a name.
- Unknown @command or /command? → `list_commands` first.
- Unknown agent? → `list_agents` first.

## Sigil shortcuts
- See `@<name>` in content → call `run_at_command(name, args)`. Body is a
  sub-prompt to follow.
- See `/<name>` in content → call `run_slash_command(name, args)`. REPL only.
- `$skill` and `!agent` mid-prompt are pre-expanded — read the
  `[skill:…]` / `[agent:…]` blocks instead of re-resolving.

## Read once, work many
- Don't re-read a file in the same turn — keep its content in working memory.
- Don't re-grep for the same symbol — the result is in your context.
- The lazy loaders (`LazyCommandLoader`, `LazySkillLoader`) are mtime-aware;
  no manual cache invalidation needed.

## Bounded exploration
- 3 failed attempts at the same approach → stop, summarize what failed, ask.
- Loops in tool calls (same call, same args, expecting a different result)
  → break out, the answer isn't there.
- TIMEOUT result → retry **once**, then report.

## Don't fabricate
- Tool names, file paths, line numbers, APIs, error messages — if you
  haven't seen it in this turn, look it up. Never invent.
- "Permission denied" / "approval required" UI is not a thing in obscura —
  don't claim it.

## Memory — lazy by default
Memory is on-demand. The agent decides when to read, write, or prune —
nothing runs eagerly. Tools:
- `recall_memory(query, namespace?, top_k?)` — keyword (FTS5) search.
  Cheap. Phrase as keywords. **First pick** for recall.
- `recall_semantic(query, namespace?, top_k?)` — vector similarity.
  Pays an embedding round-trip. Use only when keyword recall misses
  conceptually-related content.
- `remember_memory(content, namespace?, metadata?)` — persist a note.
- `list_memory_namespaces` — discover what's been remembered.
- `/memory forget <id>` — delete a single memory by id (slash command).

The project's `OBSCURA.md` may declare namespaces in its frontmatter
(`remember:` key) — that's the **what** for this specific project.
The directives below are the **when**, applied universally.

### When to RECALL
- The user references something from a prior session ("like we did
  before", "remember when…", "the X I mentioned").
- A new task touches an area you've worked on before — surface prior
  decisions before duplicating effort.
- You're about to make an architectural / design choice — check whether
  it was already decided.
- You're stuck after 1-2 attempts — look for related prior context.

Don't recall on every turn. Recall when there's a specific reason.

### When to SAVE
- User states a durable preference or goal ("I prefer X", "from now
  on…", "always do Y").
- A meaningful task completes — save the decision and 1-line rationale,
  not the play-by-play.
- You dug for a fact that future-you will want again ("the auth flow
  uses session middleware in module foo").
- A surprise / gotcha / non-obvious workaround surfaces.
- The project's `OBSCURA.md` `remember:` frontmatter pattern matches.

Skip saving for: status updates, in-progress work, info already in
code/git/OBSCURA.md, anything trivial.

### The `user:*` namespace — eager-loaded at session start
The **only** namespace prefix that's loaded into the system prompt at
boot. Use sub-namespaces to scope:
- `user:profile` — durable identity (name, role, location, expertise).
  Complements the existing `user_profile.md` file; that file is for the
  user to edit, this namespace is for things they reveal in conversation.
- `user:prefs` — communication style, formatting preferences,
  decision-making style.
- `user:goals` — long-running personal goals that span projects (career,
  learning, side projects).
- `user:context` — background facts (current employer, ongoing projects,
  people they work with).

**Save to `user:*` whenever the user says something durable about
themselves** — even mid-task. Example triggers: "I'm currently working
on…", "my background is…", "I usually…", "remind me later that…",
"I prefer…". Future sessions will see these on the next boot without
the agent having to call `recall_memory`.

### When to FORGET
- A memory contradicts current reality (renamed file, reverted decision,
  superseded design).
- The user explicitly says "ignore that earlier note" or "forget X".
- Duplicate / near-duplicate of a more accurate memory.

Use `/memory forget <id>` (the id comes from `recall_memory` output).
Don't bulk-forget — each one should be a deliberate call.
