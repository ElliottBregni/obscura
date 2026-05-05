## Reference: prompt sigils

Obscura prompts can contain five sigil references. Each works in two modes:
**leading-token** (the reference is the first token of an input — runs the
action) and **mid-prompt** (the reference appears anywhere else in text — the
host expands it inline before sending to the model, so you see the resolved
content rather than the literal sigil).

If you encounter a sigil in conversation content that wasn't expanded (for
example, in a file you read or a tool result), you can run it yourself via
`run_slash_command`, `run_at_command`, or `list_commands` — see the
"Categories" table for which sigil maps to which tool.

### Categories

| Sigil | Category | Leading-token (action) | Mid-prompt (context) |
|-------|----------|------------------------|----------------------|
| `$name` | **skill** | activate skill for the turn | inject skill body inline |
| `@name [args]` | **command** | run @command with args | inject @command body (no args) |
| `*@name [args]` | **command + eval** | run @command and grade output | same as `@name` |
| `!name <prompt>` | **agent** | spawn the agent with `<prompt>` | inject an agent card (name, description, key config) |
| `/name [args]` | **slash command** | run a built-in Python command (`/init`, `/agent`, `/diff`, …) | not auto-expanded — call `run_slash_command` if needed |

### How mid-prompt expansion is presented

The host pre-expands sigil references into one of three labelled blocks
before content reaches you. **Treat each block according to its label —
they are not documentation:**

* `>>> INLINE COMMAND (\`@<name>\`) — follow the instructions below…` /
  `<<< end command \`@<name>\``
  → Execute the body as part of fulfilling the user's request. The body
  is a prompt template; missing-argument blanks (e.g. `Explain how
  ___ works`) mean the user did not pass that argument — ask for it or
  infer from surrounding prose.
* `>>> SKILL CONTEXT (\`$<name>\`) — apply throughout…` /
  `<<< end skill \`$<name>\``
  → Background context (conventions, terminology, patterns) to apply
  for the rest of your response. Not an action.
* `>>> AGENT REFERENCE (\`!<name>\`) — this agent is available…` /
  `<<< end agent \`!<name>\``
  → A card describing an agent. Consider delegating via
  `delegate_to_agent` if its capabilities match the task. Reference,
  not action.

### How to invoke from the agent loop

* `@name` references → call `run_at_command(name=..., arguments=...)`. The
  returned `body` is a sub-prompt to follow.
* `/name` references → call `run_slash_command(name=..., arguments=...)`.
  Only available inside the interactive REPL; otherwise returns
  `no_repl_bridge`.
* Use `list_commands` to enumerate both `@` and `/` families.

### Disambiguation

* `@review` is always a markdown command. The legacy form where `@<name>`
  used to spawn an agent has moved to `!<name>` to avoid this collision.
* References are matched with word boundaries: `email@review.com` is not
  parsed; only standalone `@review` is.
* Unknown sigil names are left as literal text.
