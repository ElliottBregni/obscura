# ${name}

**Type:** ${type} vault
**Managed by:** vault-gen + Obscura

## Purpose

This is a `${type}` vault managed by vault-gen. It is:
- Readable by Obsidian as a vault (open the repo root as your vault folder)
- Pluggable into Obscura via `vault-gen link`
- Programmatically accessible via the `_access` module

## Access layer

The `_access/` directory is a stdlib-only Python module. Use it to read/write
this repo programmatically from any Python process:

```python
from _access import RepoAccess

repo = RepoAccess("/path/to/${name}")

# File I/O
content = repo.read("some/note.md")
repo.write("some/note.md", content, commit_msg="feat: update note")

# Search
hits = repo.search("query string")          # list[{file, line, text}]

# Git
log   = repo.history(n=10)                  # list[{hash, author, date, subject}]
delta = repo.diff("HEAD~1", "HEAD")
files = repo.list_files("**/*.md")

# Sync (requires remote)
repo.sync()

# Link to an Obscura instance
repo.link_obsura("/path/to/obscura")
```

## Conventions

- All notes are Markdown.
- YAML frontmatter is optional but encouraged for structured metadata.
- Commits should be atomic and well-messaged — they are the audit trail.
- See README.md for directory structure and type-specific conventions.
