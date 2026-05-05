# Projects/

Project-scoped notes and working documents. Human-authored; agent-readable for
context injection.

## Convention

```
Projects/
├── README.md              # this file
└── <project-name>/
    ├── overview.md        # project brief (agents will read this for context)
    ├── decisions/         # ADRs scoped to this project
    │   └── *.md
    └── notes/             # working notes
        └── *.md
```

## overview.md convention

Keep `overview.md` in each project directory up-to-date. Agents use it as a
context document when working on tasks related to that project. Include:
- What the project is and why it exists
- Current status
- Key decisions already made
- Open questions

## Linking

Link project notes to daily notes and agent logs using `[[wikilinks]]` — Obsidian
will track the graph and Obscura will use it for context retrieval.
