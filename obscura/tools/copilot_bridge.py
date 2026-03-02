"""
Copilot CLI Bridge Tools for Obscura
Provides view, edit, grep, glob functionality using Obscura's existing tools
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from obscura.core.types import ToolSpec

if TYPE_CHECKING:
    from obscura.auth.models import AuthenticatedUser


def view_file_impl(path: str, start_line: int = 1, end_line: int = -1) -> str:
    """View file with line numbers (Copilot CLI 'view' equivalent)."""
    import os
    
    if not os.path.exists(path):
        return f"Error: Path does not exist: {path}"
    
    if os.path.isdir(path):
        # Directory listing
        import subprocess
        result = subprocess.run(
            ["ls", "-la", path],
            capture_output=True,
            text=True
        )
        return result.stdout
    
    # File viewing with line numbers
    with open(path, 'r') as f:
        lines = f.readlines()
    
    if end_line == -1:
        end_line = len(lines)
    
    numbered_lines = []
    for i, line in enumerate(lines[start_line-1:end_line], start=start_line):
        numbered_lines.append(f"{i}. {line.rstrip()}")
    
    return "\n".join(numbered_lines)


def edit_file_impl(path: str, old_str: str, new_str: str) -> str:
    """Surgical string replacement in file (Copilot CLI 'edit' equivalent)."""
    import os
    
    if not os.path.exists(path):
        return f"Error: File does not exist: {path}"
    
    with open(path, 'r') as f:
        content = f.read()
    
    # Check if old_str exists exactly once
    count = content.count(old_str)
    if count == 0:
        return f"Error: old_str not found in {path}"
    if count > 1:
        return f"Error: old_str appears {count} times in {path}, must be unique"
    
    # Replace
    new_content = content.replace(old_str, new_str, 1)
    
    with open(path, 'w') as f:
        f.write(new_content)
    
    return f"File {path} updated successfully"


def grep_impl(pattern: str, path: str = ".", recursive: bool = True, 
              case_insensitive: bool = False, show_line_numbers: bool = True) -> str:
    """Search for pattern in files (Copilot CLI 'grep' equivalent using ripgrep)."""
    import subprocess
    
    cmd = ["rg"]
    
    if case_insensitive:
        cmd.append("-i")
    if show_line_numbers:
        cmd.append("-n")
    if not recursive:
        cmd.append("--max-depth=1")
    
    cmd.extend([pattern, path])
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    if result.returncode != 0 and result.returncode != 1:
        return f"Error: {result.stderr}"
    
    return result.stdout if result.stdout else "No matches found"


def glob_impl(pattern: str, path: str = ".") -> str:
    """Find files matching pattern (Copilot CLI 'glob' equivalent)."""
    import subprocess
    
    # Use fd or find
    result = subprocess.run(
        ["fd", pattern, path],
        capture_output=True,
        text=True
    )
    
    if result.returncode != 0:
        # Fallback to find
        result = subprocess.run(
            ["find", path, "-name", pattern],
            capture_output=True,
            text=True
        )
    
    return result.stdout if result.stdout else "No files found"


def report_intent_impl(intent: str) -> str:
    """Record the agent's current intent. No-op acknowledgement."""
    import json
    return json.dumps({"ok": True, "intent": intent})


def make_copilot_bridge_tool_specs(user: AuthenticatedUser) -> list[ToolSpec]:
    """Create Copilot CLI bridge tool specs."""
    
    return [
        ToolSpec(
            name="view",
            description="View file or directory with line numbers",
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to file or directory"
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "Start line number (default: 1)",
                        "default": 1
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "End line number (default: -1 for end of file)",
                        "default": -1
                    }
                },
                "required": ["path"]
            },
            handler=lambda path, start_line=1, end_line=-1: view_file_impl(path, start_line, end_line)
        ),
        
        ToolSpec(
            name="edit",
            description="Surgical string replacement in file (must match exactly once)",
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to file to edit"
                    },
                    "old_str": {
                        "type": "string",
                        "description": "Exact string to replace (must be unique)"
                    },
                    "new_str": {
                        "type": "string",
                        "description": "Replacement string"
                    }
                },
                "required": ["path", "old_str", "new_str"]
            },
            handler=lambda path, old_str, new_str: edit_file_impl(path, old_str, new_str)
        ),
        
        ToolSpec(
            name="grep",
            description="Search for pattern in files using ripgrep",
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Pattern to search for"
                    },
                    "path": {
                        "type": "string",
                        "description": "Path to search in (default: current dir)",
                        "default": "."
                    },
                    "case_insensitive": {
                        "type": "boolean",
                        "description": "Case insensitive search",
                        "default": False
                    },
                    "show_line_numbers": {
                        "type": "boolean",
                        "description": "Show line numbers",
                        "default": True
                    }
                },
                "required": ["pattern"]
            },
            handler=lambda pattern, path=".", case_insensitive=False, show_line_numbers=True: 
                grep_impl(pattern, path, True, case_insensitive, show_line_numbers)
        ),
        
        ToolSpec(
            name="glob",
            description="Find files matching glob pattern",
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Glob pattern (e.g., '*.py', '**/*.md')"
                    },
                    "path": {
                        "type": "string",
                        "description": "Base path to search from",
                        "default": "."
                    }
                },
                "required": ["pattern"]
            },
            handler=lambda pattern, path=".": glob_impl(pattern, path)
        )
    ]
