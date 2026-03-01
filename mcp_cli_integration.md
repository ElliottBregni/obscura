# MCP CLI Integration - Completion Checklist

## Status: 90% Complete

### ✅ Already Done
- MCP commands module created (obscura/cli/mcp_commands.py - 370 lines)
- Async wrapper created in commands.py (cmd_mcp function exists)
- 5 subcommands implemented: discover, list, select, env, install

### 🔧 Remaining Tasks (3 small fixes)

#### 1. Fix Import in mcp_commands.py (Line 8)
```python
# CHANGE THIS:
from typing import Any

# TO THIS:
from typing import Any, Callable
```

#### 2. Register in COMMANDS (commands.py ~line 1002)
```python
# ADD THIS LINE:
    "mcp": cmd_mcp,
```

#### 3. Register in COMPLETIONS (commands.py ~line 1028)
```python
# ADD THIS LINE:
    "mcp": ["discover", "list", "select", "env", "install"],
```

## Quick Integration Script

Save as `fix_mcp_integration.py`:

```python
from pathlib import Path

# 1. Fix import
mcp_cmds = Path("obscura/cli/mcp_commands.py")
content = mcp_cmds.read_text()
content = content.replace(
    "from typing import Any\n",
    "from typing import Any, Callable\n"
)
mcp_cmds.write_text(content)
print("✅ Fixed mcp_commands.py import")

# 2. Register command
cmds = Path("obscura/cli/commands.py")
content = cmds.read_text()
cmds.with_suffix(".py.backup").write_text(content)

# Add to COMMANDS
content = content.replace(
    '    "discover": cmd_discover,\n}',
    '    "discover": cmd_discover,\n    "mcp": cmd_mcp,\n}'
)

# Add to COMPLETIONS  
content = content.replace(
    '    "discover": ["web", "filesystem", "git", "database", "ai", "cloud", "search"],\n}',
    '    "discover": ["web", "filesystem", "git", "database", "ai", "cloud", "search"],\n    "mcp": ["discover", "list", "select", "env", "install"],\n}'
)

cmds.write_text(content)
print("✅ Registered /mcp command")
print("✅ Integration complete!")
```

Run with: `python3 fix_mcp_integration.py`
