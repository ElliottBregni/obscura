# Dynamic Tool Discovery

Obscura can automatically discover and recommend popular MCP tool capabilities from community registries.

## Overview

The `DynamicToolDiscovery` system fetches trending MCP servers from:
- **MCP Registry API** (official)
- **MCPServers.org** (community catalog)

It ranks tools by popularity, categorizes them, and generates ready-to-use MCP configurations.

## Usage

### CLI Command

Inside the Obscura REPL:

```bash
# Discover top 20 popular tools
/discover 20

# Discover web-related tools
/discover web

# Discover database tools (limit 10)
/discover database 10

# Categories: web, filesystem, git, database, ai, cloud, search
```

### Programmatic API

```python
from obscura.tools.dynamic_discovery import DynamicToolDiscovery

discovery = DynamicToolDiscovery()

# Get top 50 popular tools
tools = discovery.discover_popular(limit=50)

for tool in tools:
    print(f"{tool.name} - {tool.category} (rank {tool.popularity_rank})")
    print(f"Install: {' '.join(tool.installation_command)}")

# Get tools by category
web_tools = discovery.discover_by_category("web", limit=20)
git_tools = discovery.discover_by_category("git", limit=10)
```

### Auto-Generate MCP Config

```python
from obscura.tools.dynamic_discovery import AutoInstallToolProvider

# Auto-generate config with top 15 tools + specific categories
provider = AutoInstallToolProvider(
    auto_install_top_n=15,
    categories=["web", "git", "database"]
)

# Save to ~/.obscura/auto-mcp.json
config_path = provider.save_config()

# Use in your agent spawn
agent = await runtime.spawn_agent(
    name="auto-agent",
    model="copilot",
    mcp={"enabled": True, "config_path": str(config_path)}
)
```

### CLI Script

```bash
# Discover and save config
python obscura/tools/dynamic_discovery.py 30 web ~/.obscura/my-tools.json

# Just discover
python obscura/tools/dynamic_discovery.py 50
```

## Tool Categories

| Category | Examples |
|----------|----------|
| **filesystem** | File operations, path manipulation, directory listing |
| **git** | Repository management, commits, branches, diffs |
| **web** | HTTP requests, web scraping, browser automation |
| **database** | SQL, NoSQL, schema inspection, queries |
| **communication** | Slack, Discord, email, messaging |
| **ai** | LLM APIs, embeddings, fine-tuning |
| **cloud** | AWS, GCP, Azure, serverless functions |
| **search** | Full-text search, indexing, Elasticsearch |

## How It Works

1. **Fetch** - Queries MCP registries for latest server list
2. **Rank** - Orders by popularity/downloads
3. **Categorize** - Infers category from name/description using keywords
4. **Generate** - Creates MCP config with `npx` commands

## Data Flow

```
MCPServers.org / Registry API
        ↓
  Fetch top N servers
        ↓
Parse metadata (name, slug, rank)
        ↓
  Infer category + npm package
        ↓
Generate ToolCapability objects
        ↓
    mcpServers config JSON
```

## Example Output

```
Rank   Category        Name                                     Package
----------------------------------------------------------------------------------------------------
1      ai              .NET Types Explorer                      v0v1kkk/dotnetmetadatamcpserver-mcp
2      ai              @blockrun/mcp                           blockrunai/blockrun-mcp
3      web             @mcp-fe/react-tools                     mcp-fe/mcp-fe
4      filesystem      @mcp-z/mcp-pdf                          github-com-kmalakoff-mcp-pdf
5      web             @shipsite/mcpofficial                   shipsite-sh-llm-mcp
6      ai              /vibe                                   vibecodinginc/vibe-mcp
7      web             12306-mcp                               Joooook/12306-mcp
8      database        302AI Sandbox MCP Server                302ai/302_sandbox_mcp-mcp
```

## Integration with Agents

Once discovered, tools are automatically available to agents:

```python
# Agent with auto-discovered tools
runtime = AgentRuntime(user)
await runtime.start()

# Dynamic discovery + install
discovery = DynamicToolDiscovery()
top_tools = discovery.discover_popular(20)

# Generate config
provider = AutoInstallToolProvider(auto_install_top_n=20)
config_path = provider.save_config()

# Spawn with auto-config
agent = await runtime.spawn_agent(
    name="smart-agent",
    model="copilot",
    mcp={"enabled": True, "config_path": str(config_path)}
)
```

## Limitations

- **Community-driven** - Tool quality varies
- **NPM-based** - Assumes `npx` availability
- **Network required** - Fetches from external registries
- **Rate limits** - Registries may rate-limit requests
- **Categorization** - Keyword-based, not always accurate

## Advanced: Custom Catalog Provider

Implement your own catalog source:

```python
from obscura.integrations.mcp.catalog import MCPCatalogProvider, MCPCatalogEntry

class InternalCatalogProvider:
    def fetch_top(self, limit: int) -> list[MCPCatalogEntry]:
        # Your internal tool registry
        return [
            MCPCatalogEntry(
                name="Internal File Server",
                slug="internal-fs",
                url="https://internal.com/fs-mcp",
                rank=1
            )
        ]

# Use in discovery
discovery = DynamicToolDiscovery()
discovery.mcpservers_provider = InternalCatalogProvider()
```

## Future Enhancements

- [ ] **Caching** - Cache popular tools locally
- [ ] **Ratings** - Integrate user ratings/reviews
- [ ] **Dependencies** - Detect tool dependencies
- [ ] **Health checks** - Verify tool availability
- [ ] **Auto-update** - Periodic refresh of tool list
- [ ] **Smart recommendations** - ML-based suggestions
- [ ] **Usage analytics** - Track most-used tools

## References

- [MCP Registry](https://registry.modelcontextprotocol.io/)
- [MCPServers.org](https://mcpservers.org/)
- [MCP Specification](https://modelcontextprotocol.io/)
