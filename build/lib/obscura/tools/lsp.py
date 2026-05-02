"""obscura.tools.lsp — Language Server Protocol tool.

Provides code navigation operations via LSP:
  - goToDefinition: Find where a symbol is defined
  - findReferences: Find all references to a symbol
  - hover: Get type/docs info for a symbol
  - documentSymbol: List all symbols in a file
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

from obscura.core.tools import tool

if TYPE_CHECKING:
    from obscura.core.types import ToolSpec

# Module-level LSP manager (set by REPL/runtime at startup).
_lsp_manager: Any = None


def set_lsp_manager(manager: Any) -> None:
    """Set the global LSP server manager."""
    global _lsp_manager
    _lsp_manager = manager


def _format_location(loc: dict[str, Any]) -> str:
    """Format an LSP Location as file:line:col."""
    uri = loc.get("uri", "")
    path = uri.replace("file://", "") if uri.startswith("file://") else uri
    rng = loc.get("range", {})
    start = rng.get("start", {})
    line = start.get("line", 0) + 1
    char = start.get("character", 0) + 1
    return f"{path}:{line}:{char}"


def _format_locations(locs: list[dict[str, Any]] | dict[str, Any] | None) -> list[str]:
    """Format a list of LSP Locations."""
    if locs is None:
        return []
    if isinstance(locs, dict):
        return [_format_location(locs)]
    return [_format_location(loc) for loc in locs]


@tool(
    "lsp",
    (
        "Code navigation via Language Server Protocol. Operations: "
        "goToDefinition, findReferences, hover, documentSymbol."
    ),
    {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["goToDefinition", "findReferences", "hover", "documentSymbol"],
                "description": "LSP operation to perform.",
            },
            "file_path": {
                "type": "string",
                "description": "Absolute path to the file.",
            },
            "line": {"type": "integer", "description": "1-based line number."},
            "character": {
                "type": "integer",
                "description": "1-based character offset.",
            },
        },
        "required": ["operation", "file_path"],
    },
)
async def lsp_tool(
    operation: str,
    file_path: str,
    line: int = 1,
    character: int = 1,
) -> str:
    if _lsp_manager is None:
        return json.dumps(
            {
                "ok": False,
                "error": "lsp_not_available",
                "detail": "LSP server manager not initialized",
            },
        )

    client = await _lsp_manager.get_client(file_path)
    if client is None:
        return json.dumps(
            {
                "ok": False,
                "error": "no_server",
                "detail": f"No LSP server available for {file_path}",
            },
        )

    try:
        if operation == "goToDefinition":
            result = await client.goto_definition(file_path, line, character)
            locations = _format_locations(result)
            return json.dumps(
                {
                    "ok": True,
                    "operation": operation,
                    "file_path": file_path,
                    "results": locations,
                    "count": len(locations),
                },
            )

        if operation == "findReferences":
            result = await client.find_references(file_path, line, character)
            locations = _format_locations(result)
            return json.dumps(
                {
                    "ok": True,
                    "operation": operation,
                    "file_path": file_path,
                    "results": locations,
                    "count": len(locations),
                },
            )

        if operation == "hover":
            result = await client.hover(file_path, line, character)
            content = ""
            if result and "contents" in result:
                contents = result["contents"]
                if isinstance(contents, str):
                    content = contents
                elif isinstance(contents, dict):
                    content = contents.get("value", str(contents))
                elif isinstance(contents, list):
                    content = "\n".join(
                        c.get("value", str(c)) if isinstance(c, dict) else str(c)
                        for c in contents
                    )
            return json.dumps(
                {
                    "ok": True,
                    "operation": operation,
                    "file_path": file_path,
                    "content": content,
                },
            )

        if operation == "documentSymbol":
            result = await client.document_symbols(file_path)
            symbols = []
            if result:
                for sym in result:
                    symbols.append(
                        {
                            "name": sym.get("name", ""),
                            "kind": sym.get("kind", 0),
                            "range": f"{sym.get('range', {}).get('start', {}).get('line', 0) + 1}",
                        },
                    )
            return json.dumps(
                {
                    "ok": True,
                    "operation": operation,
                    "file_path": file_path,
                    "symbols": symbols,
                    "count": len(symbols),
                },
            )

        return json.dumps(
            {"ok": False, "error": "unknown_operation", "detail": operation},
        )

    except Exception as exc:
        return json.dumps({"ok": False, "error": "lsp_error", "detail": str(exc)})


def get_lsp_tool_specs() -> list[ToolSpec]:
    """Return LSP tool specs for registration."""
    return [cast("ToolSpec", lsp_tool.spec)]
