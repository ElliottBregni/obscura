# MCP Servers Configuration

This document describes the Model Context Protocol (MCP) servers configured for Obscura agents.

## Configured Servers

### Existing (Your Original Config)
1. **Postman** - API testing and collection management
2. **Jira** - Project management and issue tracking  
3. **Auth0** - Identity management and authentication

### Newly Added (Reference Implementations)
4. **Filesystem** - Secure file operations with access controls
   - Restricted to: `/Users/elliottbregni/dev`
   - Tools: read_file, write_file, list_directory, create_directory, etc.

5. **Git** - Repository operations
   - Tools: git_status, git_diff, git_log, git_show, search_commits, etc.
   - Works with any git repo accessible from the system

6. **Memory** - Knowledge graph persistent memory
   - Tools: create_entities, create_relations, search_nodes, open_nodes
   - Complements Obscura's built-in memory system

7. **Fetch** - Web content fetching
   - Tools: fetch (URL→clean markdown), get_page_metadata
   - Complements Obscura's web_fetch tool

8. **Sequential Thinking** - Structured problem-solving
   - Tools: dynamic thought sequences for complex reasoning
   - Helps agents break down multi-step problems

## Location

- **Config:** `/Users/elliottbregni/dev/obscura-main/config/mcp-config.json`
- **Servers:** `/Users/elliottbregni/.obscura/mcp-servers/`

## Usage in Obscura

Agents can access MCP tools when spawned with MCP configuration:

```python
# Via API
POST /api/v1/agents
{
  "name": "my-agent",
  "model": "copilot",
  "mcp": {
    "enabled": true,
    "server_names": ["filesystem", "git", "memory"]
  }
}
```

```bash
# Via CLI
obscura spawn --name my-agent --model copilot --mcp filesystem,git
```

## Why These Servers?

| Server | Purpose | Use Cases |
|--------|---------|-----------|
| **Filesystem** | File operations | Code editing, document management, config updates |
| **Git** | Version control | Code review, history search, diff analysis |
| **Memory** | Persistent storage | Knowledge accumulation across sessions |
| **Fetch** | Web scraping | Research, documentation fetching |
| **Sequential Thinking** | Reasoning | Complex problem decomposition |

## Security Notes

- **Filesystem MCP** is sandboxed to `/Users/elliottbregni/dev` only
- All MCP servers run as separate processes (stdio transport)
- Environment variables (API keys) are kept in your shell config

## Testing

Test MCP server availability:

```bash
# Test filesystem server
node ~/.obscura/mcp-servers/filesystem/dist/index.js /Users/elliottbregni/dev

# Test git server  
node ~/.obscura/mcp-servers/git/dist/index.js

# Test memory server
node ~/.obscura/mcp-servers/memory/dist/index.js
```

## Additional MCP Servers Available

See the [MCP Registry](https://registry.modelcontextprotocol.io/) for more servers:

- **Puppeteer** - Browser automation
- **PostgreSQL** - Database access
- **SQLite** - Local database
- **Slack** - Team messaging
- **GitHub** - Repository management (official)
- **Brave Search** - Web search (official)
- **Google Drive** - Cloud storage
- **Redis** - Key-value store

## References

- [Model Context Protocol](https://modelcontextprotocol.io/)
- [MCP Servers Repository](https://github.com/modelcontextprotocol/servers)
- [MCP TypeScript SDK](https://github.com/modelcontextprotocol/typescript-sdk)
