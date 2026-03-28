# Plan: Obscura CLI Manpage

## Goal
Create a proper Unix manpage (`obscura.1`) that documents the full CLI — main flags, subcommands, REPL slash commands, backends, modes, and environment.

## Steps

### 1. Create `docs/obscura.1` (troff/groff format)
Standard manpage sections:
- **NAME** — `obscura` one-liner
- **SYNOPSIS** — `obscura [OPTIONS] [PROMPT]` and subcommand forms
- **DESCRIPTION** — What Obscura is (multi-backend AI agent runtime)
- **OPTIONS** — All top-level flags (`-b`, `-m`, `-s`, `--session`, `--continue`, `--max-turns`, `--tools`, `--confirm`, `--no-default-prompt`, `-w`, `--log-level`, `--supervise`)
- **SUBCOMMANDS** — `init`, `workspace list/inspect/load`, `template list/inspect`
- **REPL COMMANDS** — All `/slash` commands grouped by category:
  - Help & Navigation (`/help`, `/quit`, `/clear`)
  - Chat & Backend (`/backend`, `/model`, `/system`, `/tools`, `/confirm`)
  - Modes & Planning (`/mode`, `/plan`, `/approve`, `/reject`)
  - Review (`/diff`, `/context`, `/thinking`, `/compact`, `/cat`)
  - Session (`/session`)
  - Agents (`/agent`, `/delegate`, `/fleet`, `/swarm`, `/attention`)
  - Discovery & Tools (`/discover`, `/search-tools`)
  - MCP & Plugins (`/mcp`, `/plugin`, `/capability`)
  - Memory (`/memory`)
  - A2A (`/a2a`)
  - Monitoring (`/heartbeat`, `/status`, `/running`, `/kill`, `/audit`, `/health`, `/broker`, `/tail-trace`, `/replay`, `/policies`)
  - Workspace (`/init`, `/inspect`, `/pack`)
- **BACKENDS** — copilot, claude, codex descriptions
- **MODES** — ask, plan, code
- **ENVIRONMENT** — `OBSCURA_HOME`, plugin venv, specs dir, events DB
- **FILES** — `~/.obscura/specs/`, `~/.obscura/events.db`, `~/.obscura/venv/`, `~/.obscura/policies/`
- **EXIT STATUS** — 0 success, 1 error
- **EXAMPLES** — Common usage patterns
- **SEE ALSO** — Links to project

### 2. Add install target
Add a `Makefile` target or note in CLAUDE.md for installing the manpage:
```
install-man: docs/obscura.1
	install -d $(DESTDIR)$(PREFIX)/share/man/man1
	install -m 644 docs/obscura.1 $(DESTDIR)$(PREFIX)/share/man/man1/
```

### 3. Verify
- Run `man ./docs/obscura.1` to verify rendering
- Check groff syntax with `groff -man -Tascii docs/obscura.1 > /dev/null`

## Files Created/Modified
- **NEW**: `docs/obscura.1` — the manpage
- **MODIFIED**: None (optional Makefile target can be added later)
