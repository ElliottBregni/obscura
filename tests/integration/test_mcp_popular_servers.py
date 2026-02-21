"""Integration tests for popular MCP server config discovery."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

import pytest

from demos.mcp.run_generic_mcp_agent import add_server
from sdk.mcp.config_loader import build_runtime_server_configs, discover_mcp_servers


@dataclass(frozen=True)
class PopularServerCase:
    name: str
    transport: str
    command: str
    args: tuple[str, ...]
    url: str
    env_key: str | None = None


POPULAR_SERVER_CASES: tuple[PopularServerCase, ...] = (
    PopularServerCase("github", "stdio", "npx", ("-y", "@modelcontextprotocol/server-github"), "", "GITHUB_PERSONAL_ACCESS_TOKEN"),
    PopularServerCase("jira", "stdio", "npx", ("-y", "jira-mcp"), "", "JIRA_API_TOKEN"),
    PopularServerCase("auth0", "stdio", "npx", ("-y", "@auth0/auth0-mcp-server", "run"), "", None),
    PopularServerCase("postman", "stdio", "npx", ("-y", "@postman/postman-mcp-server"), "", "POSTMAN_API_KEY"),
    PopularServerCase("playwright", "stdio", "npx", ("-y", "@playwright/mcp@latest"), "", None),
    PopularServerCase("filesystem", "stdio", "npx", ("-y", "@modelcontextprotocol/server-filesystem", "/tmp"), "", None),
    PopularServerCase("slack", "stdio", "npx", ("-y", "@modelcontextprotocol/server-slack"), "", "SLACK_BOT_TOKEN"),
    PopularServerCase("notion", "stdio", "npx", ("-y", "@notionhq/notion-mcp-server"), "", "NOTION_API_KEY"),
    PopularServerCase("linear", "stdio", "npx", ("-y", "linear-mcp"), "", "LINEAR_API_KEY"),
    PopularServerCase("stripe", "stdio", "npx", ("-y", "stripe-mcp"), "", "STRIPE_API_KEY"),
    PopularServerCase("sentry", "stdio", "npx", ("-y", "sentry-mcp"), "", "SENTRY_AUTH_TOKEN"),
    PopularServerCase("gitlab", "stdio", "npx", ("-y", "gitlab-mcp"), "", "GITLAB_TOKEN"),
    PopularServerCase("confluence", "stdio", "npx", ("-y", "confluence-mcp"), "", "CONFLUENCE_API_TOKEN"),
    PopularServerCase("atlassian", "stdio", "npx", ("-y", "atlassian-mcp"), "", "ATLASSIAN_API_TOKEN"),
    PopularServerCase("jenkins", "stdio", "npx", ("-y", "jenkins-mcp"), "", "JENKINS_TOKEN"),
    PopularServerCase("docker", "stdio", "npx", ("-y", "docker-mcp"), "", None),
    PopularServerCase("kubernetes", "stdio", "npx", ("-y", "kubernetes-mcp"), "", None),
    PopularServerCase("aws", "stdio", "npx", ("-y", "aws-mcp"), "", "AWS_ACCESS_KEY_ID"),
    PopularServerCase("gcp", "stdio", "npx", ("-y", "gcp-mcp"), "", "GOOGLE_APPLICATION_CREDENTIALS"),
    PopularServerCase("azure", "stdio", "npx", ("-y", "azure-mcp"), "", "AZURE_OPENAI_API_KEY"),
)


_SPECIAL_PACKAGE_BY_NAME: dict[str, str] = {
    "github": "@modelcontextprotocol/server-github",
    "jira": "jira-mcp",
    "auth0": "@auth0/auth0-mcp-server",
    "postman": "@postman/postman-mcp-server",
    "playwright": "@playwright/mcp@latest",
    "filesystem": "@modelcontextprotocol/server-filesystem",
    "supabase": "@supabase/mcp-server",
    "cloudflare": "@cloudflare/mcp-server-cloudflare",
    "keycloak": "keycloak-mcp",
    "google-workspace": "google_workspace_mcp",
}

_ENV_KEY_BY_NAME: dict[str, str] = {
    "github": "GITHUB_PERSONAL_ACCESS_TOKEN",
    "jira": "JIRA_API_TOKEN",
    "postman": "POSTMAN_API_KEY",
    "supabase": "SUPABASE_ACCESS_TOKEN",
    "slack": "SLACK_BOT_TOKEN",
    "notion": "NOTION_API_KEY",
    "linear": "LINEAR_API_KEY",
    "stripe": "STRIPE_API_KEY",
    "sentry": "SENTRY_AUTH_TOKEN",
    "gitlab": "GITLAB_TOKEN",
    "confluence": "CONFLUENCE_API_TOKEN",
    "atlassian": "ATLASSIAN_API_TOKEN",
    "jenkins": "JENKINS_TOKEN",
    "aws": "AWS_ACCESS_KEY_ID",
    "gcp": "GOOGLE_APPLICATION_CREDENTIALS",
    "azure": "AZURE_OPENAI_API_KEY",
    "cloudflare": "CLOUDFLARE_API_TOKEN",
    "keycloak": "KEYCLOAK_TOKEN",
}


def _build_catalog_case(name: str) -> PopularServerCase:
    package = _SPECIAL_PACKAGE_BY_NAME.get(name, f"{name}-mcp")
    env_key = _ENV_KEY_BY_NAME.get(name)
    args = ("-y", package)
    if name == "auth0":
        args = ("-y", package, "run")
    if name == "filesystem":
        args = ("-y", package, "/tmp")
    return PopularServerCase(
        name=name,
        transport="stdio",
        command="npx",
        args=args,
        url="",
        env_key=env_key,
    )


TOP_100_GITHUB_MCP_SERVER_NAMES: tuple[str, ...] = (
    "github",
    "jira",
    "auth0",
    "postman",
    "playwright",
    "filesystem",
    "supabase",
    "slack",
    "notion",
    "linear",
    "stripe",
    "sentry",
    "gitlab",
    "confluence",
    "atlassian",
    "jenkins",
    "docker",
    "kubernetes",
    "aws",
    "gcp",
    "azure",
    "cloudflare",
    "keycloak",
    "google-workspace",
    "google-calendar",
    "gmail",
    "google-drive",
    "google-sheets",
    "google-docs",
    "todoist",
    "obsidian",
    "apple-notes",
    "apple-books",
    "youtube",
    "spotify",
    "tiktok",
    "bluesky",
    "shopify",
    "mercadolibre",
    "openlibrary",
    "semgrep",
    "osv",
    "vulert",
    "wazuh",
    "sonarqube",
    "jmeter",
    "argocd",
    "tree-sitter",
    "mem0",
    "sandbox",
    "openapi-schema-explorer",
    "openrpc",
    "octocode",
    "globalping",
    "gitkraken",
    "maven-tools",
    "defang",
    "vegalite",
    "echarts",
    "mermaid",
    "chart",
    "unified-diff",
    "rube",
    "pipedream",
    "zapier",
    "mcpjungle",
    "toolhive",
    "mcp-get",
    "mxcp",
    "yamcp",
    "remote-mcp",
    "chronulus",
    "llamacloud",
    "huggingface-spaces",
    "openai-compatible-chat",
    "perplexity",
    "arxiv",
    "paperswithcode",
    "tavily",
    "search1api",
    "searxng",
    "firecrawl",
    "exa",
    "skyvern",
    "browser-control",
    "apify-actors",
    "rag-web-browser",
    "cloudflare-workers",
    "cloudflare-r2",
    "cloudflare-d1",
    "tinybird",
    "make",
    "taskade",
    "netwrix",
    "entra-id",
    "coreflux-mqtt",
    "bagel",
    "keboola",
    "mcp-proxy",
    "mcp-gateway",
)

TOP_100_GITHUB_MCP_SERVER_CASES: tuple[PopularServerCase, ...] = tuple(
    _build_catalog_case(name) for name in TOP_100_GITHUB_MCP_SERVER_NAMES
)


def _write_single_server_config(path: Path, case: PopularServerCase) -> None:
    env: dict[str, str] = {}
    if case.env_key is not None:
        env[case.env_key] = "${" + case.env_key + "}"
    payload: dict[str, Any] = {
        "mcpServers": {
            case.name: {
                "transport": case.transport,
                "command": case.command,
                "args": list(case.args),
                "url": case.url,
                "env": env,
                "tools": [],
            }
        }
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _write_popular_config(path: Path) -> None:
    payload: dict[str, Any] = {
        "mcpServers": {
            "github": {
                "transport": "stdio",
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-github"],
                "env": {
                    "GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_PERSONAL_ACCESS_TOKEN}"
                },
                "tools": [],
            },
            "jira": {
                "transport": "stdio",
                "command": "npx",
                "args": ["-y", "jira-mcp"],
                "env": {
                    "JIRA_BASE_URL": "${JIRA_BASE_URL}",
                    "JIRA_USER_EMAIL": "${JIRA_USER_EMAIL}",
                    "JIRA_API_TOKEN": "${JIRA_API_TOKEN}",
                },
                "tools": [],
            },
            "auth0": {
                "transport": "stdio",
                "command": "npx",
                "args": ["-y", "@auth0/auth0-mcp-server", "run"],
                "env": {"DEBUG": "auth0-mcp"},
                "tools": [],
            },
            "postman": {
                "transport": "stdio",
                "command": "npx",
                "args": ["-y", "@postman/postman-mcp-server"],
                "env": {"POSTMAN_API_KEY": "${POSTMAN_API_KEY}"},
                "tools": [],
            },
            "playwright": {
                "transport": "stdio",
                "command": "npx",
                "args": ["-y", "@playwright/mcp@latest"],
                "env": {},
                "tools": ["browser_navigate", "browser_snapshot"],
            },
            "filesystem": {
                "transport": "stdio",
                "command": "npx",
                "args": [
                    "-y",
                    "@modelcontextprotocol/server-filesystem",
                    "/tmp",
                ],
                "env": {},
                "tools": ["read_file", "write_file"],
            },
        }
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


@pytest.mark.integration
def test_popular_server_discovery_and_selection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "mcp-config.json"
    _write_popular_config(config_path)

    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "ghp_test")
    monkeypatch.setenv("JIRA_BASE_URL", "https://example.atlassian.net")
    monkeypatch.setenv("JIRA_USER_EMAIL", "bot@example.com")
    monkeypatch.setenv("POSTMAN_API_KEY", "pm_test")
    # Intentionally omit JIRA_API_TOKEN to validate missing-env reporting.

    discovered = discover_mcp_servers(config_path, resolve_env=True)
    names = {server.name for server in discovered}
    assert {"github", "jira", "auth0", "postman", "playwright", "filesystem"} <= names

    jira = next(server for server in discovered if server.name == "jira")
    assert jira.missing_env == ("JIRA_API_TOKEN",)

    selected = build_runtime_server_configs(
        discovered,
        selected_names=["github", "jira", "auth0", "playwright"],
    )
    assert len(selected) == 4
    selected_names = {
        str(server.get("command", "")) + ":" + " ".join(server.get("args", []))
        for server in selected
    }
    assert "npx:-y @modelcontextprotocol/server-github" in selected_names
    assert "npx:-y jira-mcp" in selected_names
    assert "npx:-y @auth0/auth0-mcp-server run" in selected_names
    assert "npx:-y @playwright/mcp@latest" in selected_names


@pytest.mark.integration
def test_demo_add_server_round_trips_with_core_loader(tmp_path: Path) -> None:
    config_path = tmp_path / "mcp-config.json"

    add_server(
        path=config_path,
        name="github",
        transport="stdio",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-github"),
        url="",
        env={"GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_PERSONAL_ACCESS_TOKEN}"},
    )
    add_server(
        path=config_path,
        name="jira",
        transport="stdio",
        command="npx",
        args=("-y", "jira-mcp"),
        url="",
        env={
            "JIRA_BASE_URL": "${JIRA_BASE_URL}",
            "JIRA_USER_EMAIL": "${JIRA_USER_EMAIL}",
            "JIRA_API_TOKEN": "${JIRA_API_TOKEN}",
        },
    )
    add_server(
        path=config_path,
        name="auth0",
        transport="stdio",
        command="npx",
        args=("-y", "@auth0/auth0-mcp-server", "run"),
        url="",
        env={"DEBUG": "auth0-mcp"},
    )
    add_server(
        path=config_path,
        name="postman",
        transport="stdio",
        command="npx",
        args=("-y", "@postman/postman-mcp-server"),
        url="",
        env={"POSTMAN_API_KEY": "${POSTMAN_API_KEY}"},
    )

    discovered = discover_mcp_servers(config_path, resolve_env=False)
    assert len(discovered) == 4
    runtime_servers = build_runtime_server_configs(discovered)
    assert len(runtime_servers) == 4

    commands = [str(server["command"]) for server in runtime_servers]
    assert "npx" in commands


@pytest.mark.integration
@pytest.mark.parametrize(
    "case",
    POPULAR_SERVER_CASES,
    ids=[case.name for case in POPULAR_SERVER_CASES],
)
def test_popular_single_server_round_trip(case: PopularServerCase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / f"{case.name}-mcp-config.json"
    _write_single_server_config(config_path, case)

    if case.env_key is not None:
        monkeypatch.setenv(case.env_key, f"value-for-{case.name}")

    discovered = discover_mcp_servers(config_path, resolve_env=True)
    assert len(discovered) == 1
    server = discovered[0]
    assert server.name == case.name
    assert server.transport.value == case.transport
    assert server.command.endswith(case.command)
    assert server.args == case.args

    if case.env_key is not None:
        assert server.env[case.env_key] == f"value-for-{case.name}"
        assert case.env_key not in server.missing_env

    runtime = build_runtime_server_configs(discovered, selected_names=[case.name])
    assert len(runtime) == 1
    runtime_server = runtime[0]
    assert runtime_server["transport"] == case.transport
    if case.transport == "stdio":
        assert str(runtime_server["command"]).endswith(case.command)
        assert runtime_server["args"] == list(case.args)


@pytest.mark.integration
def test_top_100_catalog_has_expected_size_and_supabase() -> None:
    assert len(TOP_100_GITHUB_MCP_SERVER_CASES) == 100
    names = {case.name for case in TOP_100_GITHUB_MCP_SERVER_CASES}
    assert "supabase" in names


@pytest.mark.integration
@pytest.mark.parametrize(
    "case",
    TOP_100_GITHUB_MCP_SERVER_CASES,
    ids=[f"top100-{case.name}" for case in TOP_100_GITHUB_MCP_SERVER_CASES],
)
def test_top_100_github_mcp_catalog_round_trip(
    case: PopularServerCase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / f"top100-{case.name}.json"
    _write_single_server_config(config_path, case)

    if case.env_key is not None:
        monkeypatch.setenv(case.env_key, f"value-for-{case.name}")

    discovered = discover_mcp_servers(config_path, resolve_env=True)
    assert len(discovered) == 1
    server = discovered[0]
    assert server.name == case.name
    assert server.transport.value == "stdio"
    assert server.command == "npx"

    runtime_servers = build_runtime_server_configs(discovered, selected_names=[case.name])
    assert len(runtime_servers) == 1
    runtime_server = runtime_servers[0]
    assert runtime_server["transport"] == "stdio"
    assert runtime_server["command"] == "npx"
