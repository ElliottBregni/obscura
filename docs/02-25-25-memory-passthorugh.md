Here’s a clean Markdown file you can drop directly into your repo.

========================================
FILE: docs/mcp_passthrough.md
=============================

# MCP Design for Passthrough Mode

## Overview

Passthrough mode allows Obscura to execute a vendor CLI directly while still integrating with Obscura’s memory, sessions, and context systems.

To make passthrough mode meaningful (not just stdout capture), Obscura exposes a set of MCP (Model Context Protocol) servers that vendor agents can call.

These MCP servers provide controlled access to:

* Memory
* Sessions
* Files
* Execution (optional, gated)

Passthrough mode only benefits from MCP if the vendor CLI supports tool calls, plugins, or MCP integration.

---

# Design Principles

1. Minimal surface area first.
2. Separate transcript storage from curated memory.
3. Make session boundaries explicit.
4. All write operations must be permission-gated.
5. Keep MCP tools deterministic and testable.
6. Version MCP schemas to allow future evolution.

---

# Required MCP Servers (Minimum Viable Integration)

## 1. obscura.memory

This is mandatory for meaningful passthrough integration.

### Purpose

Provide retrieval and persistence of contextual knowledge across sessions and agents.

### Tools

#### memory.search

Search memory entries.

Inputs:

* query: string
* session_id?: string
* agent_id?: string
* limit?: integer

Returns:

* list of MemoryItem objects

#### memory.get_session

Retrieve transcript for a session.

Inputs:

* session_id: string

Returns:

* structured transcript

#### memory.append

Append a message to session transcript.

Inputs:

* session_id: string
* role: "user" | "assistant" | "system"
* content: string
* metadata?: object

Returns:

* success status

#### memory.upsert

Persist structured knowledge.

Inputs:

* entity_type: string
* entity_id: string
* data: object
* tags?: list[string]

Returns:

* success status

---

## 2. obscura.sessions

### Purpose

Provide stable session identifiers independent of vendor CLI semantics.

### Tools

#### sessions.create

Create a new session.

Inputs:

* agent_id?: string
* backend?: string
* metadata?: object

Returns:

* session_id

#### sessions.list

List sessions.

Inputs:

* agent_id?: string
* limit?: integer

Returns:

* list of session metadata

#### sessions.set_active

Set active session.

Inputs:

* session_id: string

Returns:

* success status

#### sessions.close

Close a session.

Inputs:

* session_id: string

Returns:

* success status

---

## 3. obscura.files (Read-Only Initial Scope)

### Purpose

Allow vendor agents to inspect local project files safely.

### Tools

#### files.read

Read file content.

Inputs:

* path: string
* start_line?: integer
* end_line?: integer

Returns:

* file content

#### files.search

Search files by pattern or content.

Inputs:

* query: string
* glob?: string
* limit?: integer

Returns:

* list of matches

Write operations should be gated behind explicit approval.

---

# Optional MCP Servers (Advanced Use Cases)

## obscura.exec

High-risk, high-value capability.

### Tools

#### exec.run

Execute shell command.

Inputs:

* command: string
* cwd?: string
* timeout_s?: integer

Returns:

* stdout
* stderr
* exit_code

All execution tools must require explicit confirmation.

---

## obscura.context

### Purpose

Build token-budget-aware context bundles.

### Tools

#### context.pack

Generate context pack.

Inputs:

* prompt: string
* session_id?: string
* agent_id?: string
* budget_tokens?: integer

Returns:

* structured context bundle

---

# Passthrough Integration Modes

## Mode A: Native MCP-Supported CLI

If vendor CLI supports MCP:

* Vendor agent calls obscura.memory and obscura.files directly.
* Memory integration is dynamic and bidirectional.
* Tool lifecycle remains vendor-owned.

## Mode B: Memory Injection Fallback

If vendor CLI does not support MCP:

* Obscura injects memory into system prompt or temp file.
* Transcript captured after run.
* Memory updated post-hoc.

This mode provides limited integration.

---

# Transcript vs Memory Separation

Transcript:

* Raw chronological interaction.
* Stored via memory.append or transcript tool.

Curated Memory:

* Structured entities.
* Summaries, decisions, persistent knowledge.
* Stored via memory.upsert.

These must remain separate to prevent noise pollution.

---

# Security and Safety Constraints

* All write operations require explicit confirmation.
* Execution tools must be gated.
* File writes disabled by default.
* Session isolation enforced.
* Audit logs maintained for all MCP calls.

---

# Capability Requirements

Passthrough mode with meaningful integration requires:

* Vendor CLI support for MCP, plugins, or tool calling.
* Ability to inject system prompts or context files.
* Structured output or tool call events.

Without these, passthrough remains transcript-only.

---

# Minimal Viable Implementation

To make passthrough useful without overbuilding:

Implement:

* obscura.memory
* obscura.sessions
* obscura.files.read
* obscura.files.search

Defer execution and context packing until needed.

---

# Summary

MCP in passthrough mode allows vendor agents to:

* Query and update Obscura memory
* Operate within Obscura session boundaries
* Access project files safely

The goal is not to replace vendor runtimes, but to extend them with Obscura’s persistent memory and context engine while maintaining architectural boundaries.


Yeah, it can work. Not “magic fairy dust works,” but “engineering works if you don’t lie to yourself about the constraints.”

## What has to be true for MCP passthrough to actually work

### 1) The vendor CLI must be able to call tools (MCP or equivalent)

If the CLI can’t call MCP servers (or plugins/tools in any form), then your MCP work does **nothing** during the run. In that case, passthrough mode is limited to:

* **pre-inject memory** into a prompt/system file (one-way)
* **capture transcript** afterward (post-hoc memory update)

That’s still useful, just not interactive.

### 2) Your MCP tools must map to real agent needs

The set you wrote (memory + sessions + files) is the right core. If you build those cleanly, you get:

* “agent can pull context on demand”
* “agent can persist decisions”
* “agent can read repo files”

That’s enough for real workflows.

### 3) You need clean session identity and storage

If the vendor CLI uses its own session IDs, you need a stable Obscura session ID that persists across runs, and you need to store:

* transcript
* memory entities
* metadata about which vendor CLI run produced what

Otherwise you get mismatched context and “why is it remembering the wrong thing” bugs.

### 4) You need to handle tool gating

If you add `exec.run` or file writes, you *must* gate it.
Otherwise some agent will eventually do something “creative” in your repo.

If you keep it read-only + memory at first, risk is manageable.

---

## The big gotchas (where humans usually faceplant)

### Gotcha A: “Passthrough” can’t guarantee unified tool semantics

Even if vendor CLI supports MCP, you will not get the same structured tool lifecycle you get in your owned runtime. The vendor owns the loop.

So set expectations:

* Owned mode: full unified tool lifecycle + events
* Passthrough: best-effort integration via MCP

### Gotcha B: Output parsing is a trap

Don’t rely on parsing stdout to infer tool calls, finish reasons, usage, etc.
Treat passthrough transcripts as *transcripts*, not structured events.

### Gotcha C: Memory pollution

If you let an agent write memory freely, it will dump garbage.
Fix: separate:

* `memory.append` (always allowed, transcript log)
* `memory.upsert` (gated or heavily filtered)

A solid default is: allow append, restrict upsert to specific entity types or require confirmation.

---

## The version of this that definitely works (recommended)

Ship passthrough in two tiers:

### Tier 1: Transcript passthrough (works everywhere)

* spawn vendor CLI
* capture transcript
* store transcript into session
* optionally create a summary afterward using your owned backend

This is reliable, universal, and still valuable.

### Tier 2: MCP-enhanced passthrough (works when supported)

* run vendor CLI with MCP enabled
* expose `obscura.memory`, `obscura.sessions`, `obscura.files` as MCP tools
* vendor agent can fetch context live and record decisions

This gives you the “real integration” path without breaking Tier 1.

---

## What I’d do first (so you don’t waste a week)

1. Implement `obscura.memory` + `obscura.sessions` as MCP.
2. Add read-only `obscura.files.read/search`.
3. Build passthrough CLI that:

   * always captures transcript
   * always persists transcript
   * optionally runs a post-run “summarize and extract decisions” step using owned mode
4. Only then think about `exec.run` and write tools.

---

## Verdict

Plausible, and it’ll work **as long as you define passthrough honestly**:

* Always works for transcript capture + memory update after the run
* Interactive memory access during the run only works if the vendor CLI supports MCP/tooling

If you implement it this way, you get the best of both worlds without turning your “clean architecture” into a haunted house.
