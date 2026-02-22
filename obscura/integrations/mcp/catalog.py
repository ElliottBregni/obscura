"""MCP server catalog providers and utilities."""

from __future__ import annotations

import html
import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast


@dataclass(frozen=True)
class MCPCatalogEntry:
    """Single MCP catalog entry."""

    name: str
    slug: str
    url: str
    rank: int


class MCPCatalogProvider(Protocol):
    """Contract for MCP catalog providers."""

    def fetch_top(self, limit: int = 500) -> list[MCPCatalogEntry]:
        """Fetch top N catalog entries."""
        ...


class MCPServersOrgCatalogProvider:
    """Catalog provider backed by mcpservers.org listing pages."""

    def __init__(
        self,
        base_url: str = "https://mcpservers.org",
        listing_path: str = "/all",
        page_size: int = 30,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.listing_path = listing_path
        self.page_size = page_size
        self.timeout_seconds = timeout_seconds

    def _listing_url(self, page: int) -> str:
        if page <= 1:
            return f"{self.base_url}{self.listing_path}"
        return f"{self.base_url}{self.listing_path}/{page}"

    def _fetch_page(self, page: int) -> str:
        url = self._listing_url(page)
        with urllib.request.urlopen(url, timeout=self.timeout_seconds) as response:
            raw = response.read()
        return raw.decode("utf-8", errors="replace")

    @staticmethod
    def _parse_entries(html_text: str) -> list[tuple[str, str]]:
        pattern = re.compile(
            r'href="/servers/(?P<slug>[^"]+)"[^>]*>(?P<label>.*?)</a>',
            flags=re.IGNORECASE | re.DOTALL,
        )
        entries: list[tuple[str, str]] = []
        seen: set[str] = set()
        for match in pattern.finditer(html_text):
            slug = match.group("slug").strip("/")
            if not slug or slug in seen:
                continue
            raw_label = match.group("label")
            label = html.unescape(re.sub(r"<[^>]+>", "", raw_label)).strip()
            if not label:
                label = slug.replace("-", " ")
            entries.append((slug, label))
            seen.add(slug)
        return entries

    def fetch_top(self, limit: int = 500) -> list[MCPCatalogEntry]:
        if limit <= 0:
            return []
        entries: list[MCPCatalogEntry] = []
        page = 1
        while len(entries) < limit:
            page_html = self._fetch_page(page)
            parsed = self._parse_entries(page_html)
            if not parsed:
                break
            for slug, label in parsed:
                entries.append(
                    MCPCatalogEntry(
                        name=label,
                        slug=slug,
                        url=urllib.parse.urljoin(self.base_url, f"/servers/{slug}"),
                        rank=len(entries) + 1,
                    )
                )
                if len(entries) >= limit:
                    break
            page += 1
        return entries


class MCPRegistryAPICatalogProvider:
    """Catalog provider backed by the official MCP Registry API."""

    def __init__(
        self,
        base_url: str = "https://registry.modelcontextprotocol.io",
        endpoint: str = "/v0.1/servers",
        timeout_seconds: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.endpoint = endpoint
        self.timeout_seconds = timeout_seconds

    def _fetch_page(self, cursor: str | None = None) -> dict[str, Any]:
        url = urllib.parse.urljoin(self.base_url, self.endpoint)
        if cursor:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}cursor={urllib.parse.quote(cursor)}"
        with urllib.request.urlopen(url, timeout=self.timeout_seconds) as response:
            payload = response.read().decode("utf-8", errors="replace")
        data = json.loads(payload)
        if isinstance(data, list):
            return {"servers": data, "nextCursor": None}
        if not isinstance(data, dict):
            return {"servers": [], "nextCursor": None}
        return cast(dict[str, Any], data)

    @staticmethod
    def _parse_name(item: dict[str, Any]) -> tuple[str, str]:
        slug = str(
            item.get("name")
            or item.get("serverName")
            or item.get("id")
            or item.get("qualifiedName")
            or ""
        )
        label = str(item.get("title") or item.get("displayName") or slug)
        return slug, label

    def fetch_top(self, limit: int = 500) -> list[MCPCatalogEntry]:
        if limit <= 0:
            return []
        entries: list[MCPCatalogEntry] = []
        cursor: str | None = None
        while len(entries) < limit:
            page = self._fetch_page(cursor)
            raw_servers = page.get("servers", [])
            if not isinstance(raw_servers, list):
                break
            raw_servers_list = cast(list[Any], raw_servers)
            if not raw_servers_list:
                break
            for raw_item in raw_servers_list:
                if not isinstance(raw_item, dict):
                    continue
                item = cast(dict[str, Any], raw_item)
                slug, label = self._parse_name(item)
                if not slug:
                    continue
                url = str(
                    item.get("url")
                    or item.get("repository")
                    or item.get("homepage")
                    or ""
                )
                if not url:
                    safe_slug = urllib.parse.quote(slug, safe="")
                    url = urllib.parse.urljoin(self.base_url, f"/servers/{safe_slug}")
                entries.append(
                    MCPCatalogEntry(
                        name=label,
                        slug=slug,
                        url=url,
                        rank=len(entries) + 1,
                    )
                )
                if len(entries) >= limit:
                    break
            next_cursor_raw = page.get("nextCursor")
            cursor = str(next_cursor_raw) if next_cursor_raw else None
            if not cursor:
                break
        return entries

def catalog_entries_to_mcp_servers(
    entries: list[MCPCatalogEntry],
) -> dict[str, dict[str, object]]:
    """Map catalog entries to stdio MCP config entries."""
    servers: dict[str, dict[str, object]] = {}
    for entry in entries:
        package = f"{entry.slug}-mcp"
        servers[entry.slug] = {
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", package],
            "env": {},
            "tools": [],
        }
    return servers


def write_catalog_config(
    output_path: str | Path,
    entries: list[MCPCatalogEntry],
) -> Path:
    """Write catalog entries into mcp-config JSON format."""
    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {"mcpServers": catalog_entries_to_mcp_servers(entries)}
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return output
