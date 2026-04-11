# tools/

Tool configuration files for agents in this workspace.

Each file configures a specific tool available to agents. Filenames are the tool
name; format depends on the tool type.

## Convention

```
tools/
├── README.md          # this file
├── web-search.toml    # example: web search tool config
└── code-exec.toml     # example: code execution tool config
```

## Example tool config

```toml
[tool]
name = "web-search"
version = "1"
enabled = true

[tool.config]
max_results = 10
safe_search = true
timeout_seconds = 30
```

## Notes

- Tools listed here must be registered in `plugins/registry.toml` to be usable.
- Tool configs are loaded by Obscura at workspace startup.
- Changes take effect on the next agent spawn, not mid-session.
