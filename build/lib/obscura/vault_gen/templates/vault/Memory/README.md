# Memory/

Vector memory snapshots surfaced as Markdown notes.

These files are written by Obscura's memory system and are treat-as-read-mostly
from Obsidian. Editing them is fine — changes will be reflected in the next
memory reconciliation.

## Convention

```
Memory/
├── README.md          # this file
├── <topic>.md         # memory snapshot for a topic
└── index.md           # auto-generated index (if enabled in Obscura config)
```

## Format

Each memory snapshot has YAML frontmatter with metadata:

```yaml
---
topic: example-topic
updated: 2026-04-09T12:00:00Z
source: obscura-memory
embedding_model: text-embedding-3-small
---
```

## Notes

- Memory files are regenerated on each sync — local edits may be overwritten.
- To preserve a note permanently, move it to `Projects/` or link from a daily note.
