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

# Registry aliases for --registry flag
REGISTRY_ALIASES: dict[str, str] = {
    "mcp.so": "mcpso",
    "mcpso": "mcpso",
    "mcpservers": "mcpservers",
    "mcpservers.org": "mcpservers",
    "official": "official",
    "registry.modelcontextprotocol.io": "official",
}


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


class MCPSoCatalogProvider:
    """Catalog provider backed by mcp.so (default registry).

    Tries the JSON API first (/api/servers), falls back to HTML scraping.
    Supports pagination via page parameter.
    """

    def __init__(
        self,
        base_url: str = "https://mcp.so",
        timeout_seconds: float = 12.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    # ------------------------------------------------------------------
    # JSON API path (Next.js / REST)
    # ------------------------------------------------------------------

    def _try_api(self, page: int, per_page: int) -> list[dict[str, Any]] | None:
        """Try JSON API endpoints. Returns list of raw items or None."""
        offset = (page - 1) * per_page
        candidates = [
            f"{self.base_url}/api/servers?page={page}&limit={per_page}",
            f"{self.base_url}/api/servers?offset={offset}&limit={per_page}",
            f"{self.base_url}/api/mcp/servers?page={page}&pageSize={per_page}",
            f"{self.base_url}/api/plugins?page={page}&limit={per_page}",
        ]
        for url in candidates:
            try:
                req = urllib.request.Request(
                    url,
                    headers={
                        "Accept": "application/json",
                        "User-Agent": "obscura/1.0",
                    },
                )
                with urllib.request.urlopen(req, timeout=self.timeout_seconds) as r:
                    ct = r.headers.get("content-type", "")
                    if "json" not in ct:
                        continue
                    payload = json.loads(r.read().decode("utf-8", errors="replace"))
                    if isinstance(payload, list):
                        return cast(list[dict[str, Any]], payload)
                    if isinstance(payload, dict):
                        for key in ("servers", "data", "items", "results", "plugins"):
                            val = payload.get(key)
                            if isinstance(val, list):
                                return cast(list[dict[str, Any]], val)
            except Exception:
                continue
        return None

    # ------------------------------------------------------------------
    # HTML scrape fallback
    # ------------------------------------------------------------------

    def _fetch_html_page(self, page: int) -> str:
        url = self.base_url if page <= 1 else f"{self.base_url}?page={page}"
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 obscura/1.0"}
        )
        with urllib.request.urlopen(req, timeout=self.timeout_seconds) as r:
            return r.read().decode("utf-8", errors="replace")

    @staticmethod
    def _parse_html(html_text: str) -> list[tuple[str, str, str]]:
        """Return (slug, name, url) tuples from mcp.so HTML."""
        results: list[tuple[str, str, str]] = []
        seen: set[str] = set()
        _SKIP = frozenset(
            {"all", "search", "browse", "category", "tags", "new", "top", "about"}
        )
        pattern = re.compile(
            r'href="(?P<path>/(?:servers?|mcp)/(?P<slug>[^"/?#]+))"[^>]*>(?P<label>.*?)</a>',
            flags=re.IGNORECASE | re.DOTALL,
        )
        for m in pattern.finditer(html_text):
            slug = m.group("slug").strip("/")
            path = m.group("path")
            if not slug or slug in seen or len(slug) < 2 or slug in _SKIP:
                continue
            raw_label = m.group("label")
            label = html.unescape(re.sub(r"<[^>]+>", "", raw_label)).strip()
            if not label or len(label) < 2:
                label = slug.replace("-", " ").title()
            url = f"https://mcp.so{path}"
            results.append((slug, label, url))
            seen.add(slug)
        return results

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch_top(self, limit: int = 500, page: int = 1) -> list[MCPCatalogEntry]:
        """Fetch up to *limit* entries starting from *page*."""
        if limit <= 0:
            return []

        entries: list[MCPCatalogEntry] = []
        per_page = min(limit, 50)
        current_page = page

        while len(entries) < limit:
            raw_items = self._try_api(current_page, per_page)
            if raw_items is not None:
                if not raw_items:
                    break
                for item in raw_items:
                    if not isinstance(item, dict):
                        continue
                    item_d = cast(dict[str, Any], item)
                    slug = str(
                        item_d.get("slug")
                        or item_d.get("name")
                        or item_d.get("id")
                        or ""
                    ).strip()
                    if not slug:
                        continue
                    label = str(
                        item_d.get("title")
                        or item_d.get("displayName")
                        or item_d.get("name")
                        or slug
                    ).strip()
                    url = str(
                        item_d.get("url")
                        or item_d.get("homepage")
                        or item_d.get("repository")
                        or ""
                    )
                    if not url:
                        url = f"{self.base_url}/servers/{slug}"
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
            else:
                # HTML fallback
                try:
                    page_html = self._fetch_html_page(current_page)
                    parsed = self._parse_html(page_html)
                    if not parsed:
                        break
                    for slug, label, url in parsed:
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
                except Exception:
                    break

            current_page += 1

        return entries[:limit]


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


def get_provider_for_registry(
    registry: str,
) -> MCPSoCatalogProvider | MCPServersOrgCatalogProvider | MCPRegistryAPICatalogProvider:
    """Return the appropriate catalog provider for a registry name/alias/URL."""
    alias = REGISTRY_ALIASES.get(registry.lower(), registry.lower())
    if alias == "mcpso":
        return MCPSoCatalogProvider()
    if alias == "mcpservers":
        return MCPServersOrgCatalogProvider()
    if alias == "official":
        return MCPRegistryAPICatalogProvider()
    # Unknown — treat as custom base URL for MCPSoCatalogProvider
    return MCPSoCatalogProvider(base_url=registry.rstrip("/"))


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
