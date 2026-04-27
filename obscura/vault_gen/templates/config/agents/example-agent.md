---
name: example-agent
version: "1.0"
model: claude-opus-4-6
workspace: default
tools:
  - read
  - write
  - search
  - list_files
memory: ephemeral
tags:
  - example
  - starter
enabled: true
---

# example-agent

A starter agent manifest. Replace this with your actual agent definition.

## Purpose

Describe what this agent does, what triggers it, and what it produces.

## Inputs

- **trigger**: Describe what triggers this agent (e.g., a note in `Agents/inbox/`)
- **context**: What context it reads from the vault

## Outputs

- **writes to**: Where it surfaces output (e.g., `Agents/output/`)
- **commits**: Whether it auto-commits output

## Notes

- Keep manifests atomic — one agent per file
- Version the manifest when the behavior changes
- Disabled agents should set `enabled: false` (not be deleted)
