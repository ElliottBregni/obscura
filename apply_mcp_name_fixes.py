#!/usr/bin/env python3
"""Apply three MCP name field fixes."""

# Fix 1: types.py - add name field to MCPConnectionConfig
path1 = '/Users/elliottbregni/dev/obscura-main/obscura/integrations/mcp/types.py'
with open(path1, 'r') as f:
    c1 = f.read()

old1 = '    timeout: float = 30.0\n\n\n@dataclass\nclass MCPServerInfo:'
new1 = '    timeout: float = 30.0\n    name: str = ""  # Human-readable server name used as session/tool prefix\n\n\n@dataclass\nclass MCPServerInfo:'

if old1 in c1:
    c1 = c1.replace(old1, new1, 1)
    with open(path1, 'w') as f:
        f.write(c1)
    print('Fix 1 applied: types.py - name field added to MCPConnectionConfig')
else:
    print('Fix 1 FAILED: old text not found in types.py')

# Fix 2: mcp_backend.py - use config.name for session naming
path2 = '/Users/elliottbregni/dev/obscura-main/obscura/providers/mcp_backend.py'
with open(path2, 'r') as f:
    c2 = f.read()

old2 = '            session_name = f"mcp_server_{i}"'
new2 = '            session_name = config.name if config.name else f"mcp_server_{i}"'

if old2 in c2:
    c2 = c2.replace(old2, new2, 1)
    with open(path2, 'w') as f:
        f.write(c2)
    print('Fix 2 applied: mcp_backend.py - session_name uses config.name')
else:
    print('Fix 2 FAILED: old text not found in mcp_backend.py')

# Fix 3: client/__init__.py - pass name through when building MCPConnectionConfig
path3 = '/Users/elliottbregni/dev/obscura-main/obscura/core/client/__init__.py'
with open(path3, 'r') as f:
    c3 = f.read()

old3 = (
    '                    MCPConnectionConfig(\n'
    '                        transport=transport,\n'
    '                        command=server.get("command"),\n'
    '                        args=server.get("args", []),\n'
    '                        url=server.get("url"),\n'
    '                        env=server.get("env", {}),\n'
    '                    )'
)
new3 = (
    '                    MCPConnectionConfig(\n'
    '                        transport=transport,\n'
    '                        command=server.get("command"),\n'
    '                        args=server.get("args", []),\n'
    '                        url=server.get("url"),\n'
    '                        env=server.get("env", {}),\n'
    '                        name=server.get("name", ""),\n'
    '                    )'
)

if old3 in c3:
    c3 = c3.replace(old3, new3, 1)
    with open(path3, 'w') as f:
        f.write(c3)
    print('Fix 3 applied: client/__init__.py - name passed to MCPConnectionConfig')
else:
    print('Fix 3 FAILED: old text not found in client/__init__.py')
