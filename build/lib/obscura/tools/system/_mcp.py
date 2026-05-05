"""External MCP server discovery + orphan cleanup tools."""

from __future__ import annotations

import json
from typing import Any

from obscura.core.tool_context import current_tool_context
from obscura.core.tools import tool
from obscura.integrations.mcp.config_loader import discover_mcp_servers
from obscura.integrations.mcp.process_cleanup import cleanup_orphans, detect_orphans
from obscura.tools.system._policy import Policy


class Mcp:
    """External MCP server discovery + orphan cleanup tool namespace."""

    # ------------------------------------------------------------------
    # MCP discovery status — surface external MCP server health
    # ------------------------------------------------------------------

    @staticmethod
    @tool(
        "mcp_discovery_status",
        (
            "Report the outcome of the most recent external MCP server discovery "
            "for the active backend. Use this when external MCP tools "
            "(`mcp__<server>__<tool>`) seem missing or are timing out — the "
            "report says which servers came up, which failed, and why."
        ),
        {
            "type": "object",
            "properties": {},
        },
    )
    async def mcp_discovery_status() -> str:

        ctx = current_tool_context()
        report = ctx.mcp_discovery_report if ctx is not None else None
        if report is None:
            return json.dumps(
                {
                    "ok": True,
                    "configured": False,
                    "detail": (
                        "No MCP discovery has run for this session — either no "
                        "external MCP servers are configured or the backend "
                        "hasn't been started yet."
                    ),
                },
            )
        return json.dumps(report.to_dict())

    # ------------------------------------------------------------------
    # MCP orphan cleanup — reap leaked subprocess from past sessions
    # ------------------------------------------------------------------

    @staticmethod
    @tool(
        "mcp_cleanup_orphans",
        (
            "Find and reap orphaned external MCP server subprocess that previous "
            "sessions left behind (Claude SDK sometimes doesn't reap its stdio "
            "MCP servers when a session ends mid-flight). With dry_run=true "
            "(default) just lists matches without killing. With dry_run=false "
            "sends SIGTERM and falls back to SIGKILL after a grace period. "
            "Single matches are usually the active session's subprocess and "
            "should NOT be killed; this tool conservatively skips servers that "
            "only show one match unless force=true."
        ),
        {
            "type": "object",
            "properties": {
                "dry_run": {
                    "type": "boolean",
                    "description": "List matches without killing (default true).",
                },
                "force": {
                    "type": "boolean",
                    "description": (
                        "Also kill servers with only one match (the active session "
                        "loses its tool transport — only use after restarting). "
                        "Default false."
                    ),
                },
            },
        },
    )
    async def mcp_cleanup_orphans(dry_run: bool = True, force: bool = False) -> str:

        ctx = current_tool_context()
        report = ctx.mcp_discovery_report if ctx is not None else None
        if report is None:
            return Policy.json_error(
                "no_discovery_report",
                detail=(
                    "No MCP discovery report on this session — can't tell which "
                    "MCP server commands to scan for. Configure mcp_servers and "
                    "start the backend first."
                ),
            )

        # Reconstruct server configs from the discovery report — we don't have
        # the raw mcp_servers list here. Use server names + transport from the
        # report and look them up via configured paths if possible.
        server_names = [s.server_name for s in report.statuses]
        if not server_names:
            return json.dumps(
                {
                    "ok": True,
                    "dry_run": dry_run,
                    "scanned": [],
                    "killed": [],
                    "failed": [],
                },
            )

        # We need the original commands. They're not in the report — rescan via
        # the user-facing MCP config files.

        discovered = discover_mcp_servers()
        by_name = {s.name: s for s in discovered}
        server_dicts: list[dict[str, Any]] = []
        for name in server_names:
            d = by_name.get(name)
            if d is None or not d.command:
                continue
            server_dicts.append({"name": d.name, "command": d.command})

        orphans = detect_orphans(server_dicts)
        pids_to_kill: list[int] = []
        scan_summary: list[dict[str, Any]] = []
        for name, procs in orphans.items():
            if len(procs) <= 1 and not force:
                scan_summary.append(
                    {
                        "server": name,
                        "match_count": len(procs),
                        "skipped": "only one match (likely active session)",
                    },
                )
                continue
            # Keep the youngest? We don't have age data; conservatively kill all
            # but the highest PID (typically the most recent fork).
            candidates = sorted(procs, key=lambda p: p.pid)
            if force:
                kill_set = candidates
            else:
                kill_set = candidates[:-1]  # all but the last
            scan_summary.append(
                {
                    "server": name,
                    "match_count": len(procs),
                    "to_kill": [p.pid for p in kill_set],
                    "kept": ([candidates[-1].pid] if not force and candidates else []),
                },
            )
            pids_to_kill.extend(p.pid for p in kill_set)

        if dry_run:
            return json.dumps(
                {
                    "ok": True,
                    "dry_run": True,
                    "force": force,
                    "scanned": scan_summary,
                    "would_kill": pids_to_kill,
                },
            )

        result = cleanup_orphans(pids_to_kill)
        return json.dumps(
            {
                "ok": True,
                "dry_run": False,
                "force": force,
                "scanned": scan_summary,
                "killed": list(result.killed),
                "failed": list(result.failed),
            },
        )
