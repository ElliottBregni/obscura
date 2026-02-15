# MCP Configuration

## Your Personal Config

**Location**: `config/mcp-config.json` (symlinked to `~/.copilot/mcp-config.json`)

This is your **personal** MCP config with your API keys and settings. It's gitignored for security.

## Team Template

**Location**: `config/config/mcp-config.template.json`

This is a **template** committed to the repo for your team. It shows which MCP servers are recommended but doesn't contain secrets.

**To use the team template:**
```bash
# Copy template to your personal config
cp ~/FV-Copilot/config/config/mcp-config.template.json ~/.copilot/mcp-config.json

# Add your API keys
# Edit ~/.copilot/mcp-config.json and replace ${VARIABLE} with actual values
```

## Managing MCP Servers

### Via Copilot CLI (Recommended)
```bash
/mcp show                    # List servers
/mcp enable <server-name>    # Enable server
/mcp disable <server-name>   # Disable server
/mcp add <server-name>       # Add new server
```

### Manual Edit
Edit `~/.copilot/mcp-config.json` or `config/mcp-config.json` in Obsidian (they're the same file).

## Current Team Servers

See `config/config/mcp-config.template.json` for:
- Postman - API testing integration
- Jira - Issue tracking
- Auth0 - Authentication management

**Note**: Set environment variables for API keys before using these servers.
