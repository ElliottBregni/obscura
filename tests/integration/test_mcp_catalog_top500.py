"""Integration tests for modular top-N MCP catalog provider."""

from __future__ import annotations

import contextlib
import http.server
import json
import os
import socket
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import override

import pytest

from obscura.integrations.mcp.catalog import MCPServersOrgCatalogProvider, write_catalog_config
from obscura.integrations.mcp.catalog import MCPRegistryAPICatalogProvider
from obscura.integrations.mcp.config_loader import discover_mcp_servers


@dataclass(frozen=True)
class _CatalogServer:
    host: str
    port: int
    thread: threading.Thread
    httpd: http.server.ThreadingHTTPServer

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


class _CatalogRequestHandler(http.server.BaseHTTPRequestHandler):
    pages: dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802
        body = self.pages.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        payload = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    @override
    def log_message(self, format: str, *args: object) -> None:
        return


def _render_page(slugs: list[str]) -> str:
    links = "\n".join(
        f'<a href="/servers/{slug}">{slug.replace("-", " ").title()}</a>'
        for slug in slugs
    )
    return f"<html><body>{links}</body></html>"


@contextlib.contextmanager
def _run_catalog_server(pages: dict[str, str]) -> Iterator[_CatalogServer]:
    _CatalogRequestHandler.pages = pages
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        host, port = probe.getsockname()
    httpd = http.server.ThreadingHTTPServer((str(host), int(port)), _CatalogRequestHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield _CatalogServer(host=str(host), port=int(port), thread=thread, httpd=httpd)
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2.0)


@pytest.mark.integration
def test_top_500_catalog_fetch_and_write_round_trip(tmp_path: Path) -> None:
    slugs = [f"server-{index:03d}" for index in range(1, 501)]
    pages: dict[str, str] = {}
    chunk_size = 30
    page_count = (len(slugs) + chunk_size - 1) // chunk_size
    for page in range(1, page_count + 1):
        start = (page - 1) * chunk_size
        end = start + chunk_size
        page_slugs = slugs[start:end]
        path = "/all" if page == 1 else f"/all/{page}"
        pages[path] = _render_page(page_slugs)

    with _run_catalog_server(pages) as server:
        provider = MCPServersOrgCatalogProvider(base_url=server.base_url)
        entries = provider.fetch_top(limit=500)

    assert len(entries) == 500
    assert entries[0].slug == "server-001"
    assert entries[-1].slug == "server-500"

    output_path = tmp_path / ".obscura" / "mcp" / "top500.json"
    write_catalog_config(output_path, entries)
    discovered = discover_mcp_servers(output_path)
    assert len(discovered) == 500
    names = {server.name for server in discovered}
    assert "server-001" in names
    assert "server-500" in names


@pytest.mark.integration
def test_registry_api_provider_fetches_paginated_top_500(tmp_path: Path) -> None:
    slugs = [f"api-server-{index:03d}" for index in range(1, 501)]
    page_size = 125
    pages: dict[str, str] = {}
    cursor = "c0"
    for page_index in range(4):
        start = page_index * page_size
        end = start + page_size
        page_slugs = slugs[start:end]
        next_cursor = f"c{page_index + 1}" if page_index < 3 else None
        body = {
            "servers": [{"name": slug, "displayName": slug.upper()} for slug in page_slugs],
            "nextCursor": next_cursor,
        }
        path = "/v0.1/servers" if page_index == 0 else f"/v0.1/servers?cursor={cursor}"
        pages[path] = json.dumps(body)
        cursor = next_cursor or ""

    with _run_catalog_server(pages) as server:
        provider = MCPRegistryAPICatalogProvider(base_url=server.base_url)
        entries = provider.fetch_top(limit=500)

    assert len(entries) == 500
    assert entries[0].slug == "api-server-001"
    assert entries[-1].slug == "api-server-500"
    output_path = tmp_path / ".obscura" / "mcp" / "registry-top500.json"
    write_catalog_config(output_path, entries)
    discovered = discover_mcp_servers(output_path)
    assert len(discovered) == 500


@pytest.mark.integration
def test_top_500_catalog_includes_supabase_from_source(tmp_path: Path) -> None:
    slugs = ["supabase"] + [f"other-{index:03d}" for index in range(1, 500)]
    pages: dict[str, str] = {}
    chunk_size = 30
    page_count = (len(slugs) + chunk_size - 1) // chunk_size
    for page in range(1, page_count + 1):
        start = (page - 1) * chunk_size
        end = start + chunk_size
        page_slugs = slugs[start:end]
        path = "/all" if page == 1 else f"/all/{page}"
        pages[path] = _render_page(page_slugs)

    with _run_catalog_server(pages) as server:
        provider = MCPServersOrgCatalogProvider(base_url=server.base_url)
        entries = provider.fetch_top(limit=500)

    assert len(entries) == 500
    assert entries[0].slug == "supabase"
    output_path = tmp_path / ".obscura" / "mcp" / "top500-with-supabase.json"
    write_catalog_config(output_path, entries)
    discovered = discover_mcp_servers(output_path)
    assert any(server.name == "supabase" for server in discovered)


@pytest.mark.integration
def test_live_mcpservers_org_top_500_optional() -> None:
    if not bool(int(os.environ.get("OBSCURA_RUN_LIVE_MCP_CATALOG", "0"))):
        pytest.skip("Set OBSCURA_RUN_LIVE_MCP_CATALOG=1 to run live mcpservers.org fetch")

    provider = MCPServersOrgCatalogProvider(base_url="https://mcpservers.org")
    entries = provider.fetch_top(limit=500)
    assert len(entries) == 500
    assert entries[0].slug != ""
