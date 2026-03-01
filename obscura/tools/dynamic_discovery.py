"""Dynamic tool discovery and auto-installation for popular MCP servers.

Enables Obscura agents to automatically discover, rank, and install the most
popular tool capabilities from MCP registries.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from obscura.integrations.mcp.catalog import (
    MCPCatalogEntry,
    MCPRegistryAPICatalogProvider,
    MCPServersOrgCatalogProvider,
)

logger = logging.getLogger(__name__)


@dataclass
class ToolCapability:
    """Represents a discoverable tool capability."""

    name: str
    slug: str
    category: str
    popularity_rank: int
    npm_package: str | None = None
    description: str = ""
    installation_command: list[str] | None = None


class DynamicToolDiscovery:
    """Discovers and ranks popular tool capabilities from multiple sources."""

    def __init__(self) -> None:
        self.registry_provider = MCPRegistryAPICatalogProvider()
        self.mcpservers_provider = MCPServersOrgCatalogProvider()

    def discover_popular(
        self, limit: int = 50, min_rank: int | None = None
    ) -> list[ToolCapability]:
        """Discover top N popular tool capabilities."""
        capabilities: list[ToolCapability] = []

        # Try official registry first
        try:
            entries = self.registry_provider.fetch_top(limit)
            capabilities.extend(self._convert_entries(entries, "registry"))
            logger.info("Discovered %d tools from MCP Registry", len(entries))
        except Exception as e:
            logger.warning("Failed to fetch from MCP Registry: %s", e)

        # Fallback to mcpservers.org
        if len(capabilities) < limit:
            try:
                needed = limit - len(capabilities)
                entries = self.mcpservers_provider.fetch_top(needed)
                capabilities.extend(self._convert_entries(entries, "community"))
                logger.info("Discovered %d tools from MCPServers.org", len(entries))
            except Exception as e:
                logger.warning("Failed to fetch from MCPServers.org: %s", e)

        # Filter by rank if specified
        if min_rank is not None:
            capabilities = [c for c in capabilities if c.popularity_rank <= min_rank]

        # Sort by popularity
        capabilities.sort(key=lambda c: c.popularity_rank)

        return capabilities[:limit]

    def discover_by_category(
        self, category: str, limit: int = 20
    ) -> list[ToolCapability]:
        """Discover tools by category (e.g., 'filesystem', 'database', 'web')."""
        # Fetch from cache or get fresh data (avoid pagination issues)
        try:
            all_capabilities = self.discover_popular(limit=50)
        except Exception:
            all_capabilities = []

        # Simple keyword matching for categories
        category_keywords = {
            "filesystem": ["file", "fs", "directory", "path"],
            "database": ["db", "sql", "postgres", "mysql", "sqlite", "mongo"],
            "web": ["http", "fetch", "browser", "puppeteer", "scrape"],
            "git": ["git", "github", "gitlab", "version"],
            "communication": ["slack", "discord", "email", "sms"],
            "ai": ["openai", "anthropic", "llm", "gpt", "claude"],
            "cloud": ["aws", "gcp", "azure", "s3", "lambda"],
            "search": ["search", "query", "index", "elastic"],
        }

        keywords = category_keywords.get(category.lower(), [category.lower()])
        filtered = []

        for cap in all_capabilities:
            name_lower = cap.name.lower()
            slug_lower = cap.slug.lower()
            if any(kw in name_lower or kw in slug_lower for kw in keywords):
                filtered.append(cap)

        return filtered[:limit]

    @staticmethod
    def _convert_entries(
        entries: list[MCPCatalogEntry], source: str
    ) -> list[ToolCapability]:
        """Convert catalog entries to tool capabilities."""
        capabilities = []
        for entry in entries:
            # Extract npm package name if possible
            npm_package = None
            if "-mcp" in entry.slug or "mcp-" in entry.slug:
                npm_package = entry.slug
            elif entry.slug.startswith("@"):
                npm_package = entry.slug
            else:
                npm_package = f"{entry.slug}-mcp"

            # Infer category from name/slug
            category = DynamicToolDiscovery._infer_category(entry.slug, entry.name)

            capabilities.append(
                ToolCapability(
                    name=entry.name,
                    slug=entry.slug,
                    category=category,
                    popularity_rank=entry.rank,
                    npm_package=npm_package,
                    installation_command=["npx", "-y", npm_package]
                    if npm_package
                    else None,
                )
            )
        return capabilities

    @staticmethod
    def _infer_category(slug: str, name: str) -> str:
        """Infer category from slug and name."""
        text = f"{slug} {name}".lower()

        if any(kw in text for kw in ["file", "fs", "directory"]):
            return "filesystem"
        if any(kw in text for kw in ["git", "github", "gitlab"]):
            return "git"
        if any(kw in text for kw in ["db", "sql", "postgres", "sqlite"]):
            return "database"
        if any(kw in text for kw in ["http", "fetch", "web", "browser"]):
            return "web"
        if any(kw in text for kw in ["slack", "discord", "email"]):
            return "communication"
        if any(kw in text for kw in ["aws", "gcp", "azure", "cloud"]):
            return "cloud"
        if any(kw in text for kw in ["search", "elastic", "index"]):
            return "search"
        if any(kw in text for kw in ["ai", "llm", "gpt", "claude"]):
            return "ai"

        return "general"


class AutoInstallToolProvider:
    """Tool provider that auto-installs popular MCP servers on demand."""

    def __init__(
        self,
        auto_install_top_n: int = 10,
        categories: list[str] | None = None,
        config_path: Path | None = None,
    ) -> None:
        self.discovery = DynamicToolDiscovery()
        self.auto_install_top_n = auto_install_top_n
        self.categories = categories or []
        self.config_path = config_path or Path.home() / ".obscura" / "auto-mcp.json"

    def generate_config(self) -> dict[str, Any]:
        """Generate MCP config with popular servers."""
        capabilities: list[ToolCapability] = []

        # Get top N popular
        if self.auto_install_top_n > 0:
            capabilities.extend(self.discovery.discover_popular(self.auto_install_top_n))

        # Get by categories
        for category in self.categories:
            category_caps = self.discovery.discover_by_category(category, limit=5)
            capabilities.extend(category_caps)

        # Deduplicate by slug
        seen_slugs = set()
        unique_caps = []
        for cap in capabilities:
            if cap.slug not in seen_slugs:
                unique_caps.append(cap)
                seen_slugs.add(cap.slug)

        # Build MCP config
        mcp_servers = {}
        for cap in unique_caps:
            if cap.installation_command:
                mcp_servers[cap.slug] = {
                    "command": cap.installation_command[0],
                    "args": cap.installation_command[1:],
                    "env": {},
                    "tools": [],
                    "description": f"{cap.name} (rank: {cap.popularity_rank}, category: {cap.category})",
                }

        return {"mcpServers": mcp_servers}

    def save_config(self) -> Path:
        """Save auto-generated config to disk."""
        config = self.generate_config()
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        logger.info("Saved auto-generated MCP config to %s", self.config_path)
        return self.config_path


def cli_discover_tools(
    limit: int = 50, category: str | None = None, output: str | None = None
) -> None:
    """CLI helper to discover and optionally save popular tools."""
    discovery = DynamicToolDiscovery()

    if category:
        print(f"🔍 Discovering {category} tools...")
        capabilities = discovery.discover_by_category(category, limit)
    else:
        print(f"🔍 Discovering top {limit} popular tools...")
        capabilities = discovery.discover_popular(limit)

    print(f"\n✅ Found {len(capabilities)} tools:\n")
    print(f"{'Rank':<6} {'Category':<15} {'Name':<40} {'Package'}")
    print("-" * 100)

    for cap in capabilities:
        pkg = cap.npm_package or "N/A"
        print(f"{cap.popularity_rank:<6} {cap.category:<15} {cap.name:<40} {pkg}")

    if output:
        provider = AutoInstallToolProvider(auto_install_top_n=0)
        provider.categories = [category] if category else []
        path = provider.save_config()
        print(f"\n💾 Saved config to: {path}")


if __name__ == "__main__":
    import sys

    args = sys.argv[1:]
    limit = int(args[0]) if args and args[0].isdigit() else 20
    category = args[1] if len(args) > 1 else None
    output = args[2] if len(args) > 2 else None

    cli_discover_tools(limit, category, output)
