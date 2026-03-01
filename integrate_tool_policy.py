#!/usr/bin/env python3
"""
Integration script for ToolPolicy into Copilot and Claude backends.

This script will:
1. Add ToolPolicy import to both backends
2. Add tool_policy parameter to __init__
3. Apply policy in build/config methods
4. Create backup files before modifying
"""

import re
from pathlib import Path
import shutil
from datetime import datetime

def backup_file(filepath: Path) -> Path:
    """Create a backup of the file."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = filepath.with_suffix(f".{timestamp}.backup")
    shutil.copy2(filepath, backup_path)
    print(f"✅ Backed up: {backup_path}")
    return backup_path

def integrate_copilot():
    """Integrate ToolPolicy into CopilotBackend."""
    filepath = Path("obscura/providers/copilot.py")
    content = filepath.read_text()
    
    # Backup first
    backup_file(filepath)
    
    # 1. Add import after other core imports
    import_pattern = r"(from obscura\.core\.tools import ToolRegistry)"
    import_replacement = r"\1\nfrom obscura.core.tool_policy import ToolPolicy"
    
    if "from obscura.core.tool_policy import ToolPolicy" not in content:
        content = re.sub(import_pattern, import_replacement, content)
        print("✅ Added ToolPolicy import to copilot.py")
    else:
        print("ℹ️  ToolPolicy import already exists in copilot.py")
    
    # 2. Add tool_policy parameter to __init__
    init_pattern = r"(streaming: bool = True,)\n(\s+)\) -> None:"
    init_replacement = r"\1\n\2tool_policy: ToolPolicy | None = None,\n\2) -> None:"
    
    if "tool_policy: ToolPolicy" not in content:
        content = re.sub(init_pattern, init_replacement, content)
        print("✅ Added tool_policy parameter to __init__")
    else:
        print("ℹ️  tool_policy parameter already exists")
    
    # 3. Initialize _tool_policy in __init__
    streaming_init_pattern = r"(self\._streaming = streaming)"
    streaming_init_replacement = r"\1\n        self._tool_policy = tool_policy or ToolPolicy.from_env()"
    
    if "self._tool_policy" not in content:
        content = re.sub(streaming_init_pattern, streaming_init_replacement, content)
        print("✅ Added _tool_policy initialization")
    else:
        print("ℹ️  _tool_policy already initialized")
    
    # 4. Apply policy in build_session_config
    config_pattern = r'(if self\._tools:\n\s+config\["tools"\] = self\._tools)(  # Backend translates ToolSpecs)?'
    config_replacement = r'\1\n            # Apply tool policy to restrict native tools\n            self._tool_policy.apply_to_copilot(config, self._tools)'
    
    if "apply_to_copilot" not in content:
        content = re.sub(config_pattern, config_replacement, content)
        print("✅ Added policy application to build_session_config")
    else:
        print("ℹ️  Policy application already exists")
    
    # Write back
    filepath.write_text(content)
    print(f"✅ Updated {filepath}\n")

def integrate_claude():
    """Integrate ToolPolicy into ClaudeBackend."""
    filepath = Path("obscura/providers/claude.py")
    content = filepath.read_text()
    
    # Backup first
    backup_file(filepath)
    
    # 1. Add import
    import_pattern = r"(from obscura\.core\.tools import ToolRegistry)"
    import_replacement = r"\1\nfrom obscura.core.tool_policy import ToolPolicy"
    
    if "from obscura.core.tool_policy import ToolPolicy" not in content:
        content = re.sub(import_pattern, import_replacement, content)
        print("✅ Added ToolPolicy import to claude.py")
    else:
        print("ℹ️  ToolPolicy import already exists in claude.py")
    
    # 2. Add tool_policy parameter to __init__
    # Claude has more parameters, so we need to be more careful
    init_pattern = r"(cwd: str \| None = None,)\n(\s+)\) -> None:"
    init_replacement = r"\1\n\2tool_policy: ToolPolicy | None = None,\n\2) -> None:"
    
    if "tool_policy: ToolPolicy" not in content:
        content = re.sub(init_pattern, init_replacement, content)
        print("✅ Added tool_policy parameter to __init__")
    else:
        print("ℹ️  tool_policy parameter already exists")
    
    # 3. Initialize _tool_policy
    cwd_init_pattern = r"(self\._cwd = cwd)"
    cwd_init_replacement = r"\1\n        self._tool_policy = tool_policy or ToolPolicy.from_env()"
    
    if "self._tool_policy" not in content:
        content = re.sub(cwd_init_pattern, cwd_init_replacement, content)
        print("✅ Added _tool_policy initialization")
    else:
        print("ℹ️  _tool_policy already initialized")
    
    # 4. Replace allowed_tools logic in _build_options
    allowed_tools_pattern = r'# Allowed tools: expose custom tools by MCP name\n\s+if self\._tools:\n\s+allowed = \[f"mcp__obscura_tools__{t\.name}" for t in self\._tools\]\n\s+opts\["allowed_tools"\] = allowed'
    allowed_tools_replacement = '# Apply tool policy to restrict tools\n        if self._tools:\n            self._tool_policy.apply_to_claude(opts, self._tools)'
    
    if "apply_to_claude" not in content:
        content = re.sub(allowed_tools_pattern, allowed_tools_replacement, content)
        print("✅ Replaced allowed_tools logic with policy application")
    else:
        print("ℹ️  Policy application already exists")
    
    # Write back
    filepath.write_text(content)
    print(f"✅ Updated {filepath}\n")

def verify_integration():
    """Verify the integration was successful."""
    print("\n🔍 Verifying integration...\n")
    
    errors = []
    
    # Check Copilot
    copilot_path = Path("obscura/providers/copilot.py")
    copilot_content = copilot_path.read_text()
    
    if "from obscura.core.tool_policy import ToolPolicy" not in copilot_content:
        errors.append("❌ Copilot: Missing ToolPolicy import")
    else:
        print("✅ Copilot: ToolPolicy imported")
    
    if "tool_policy: ToolPolicy | None = None" not in copilot_content:
        errors.append("❌ Copilot: Missing tool_policy parameter")
    else:
        print("✅ Copilot: tool_policy parameter added")
    
    if "self._tool_policy = tool_policy or ToolPolicy.from_env()" not in copilot_content:
        errors.append("❌ Copilot: Missing _tool_policy initialization")
    else:
        print("✅ Copilot: _tool_policy initialized")
    
    if "apply_to_copilot" not in copilot_content:
        errors.append("❌ Copilot: Missing policy application")
    else:
        print("✅ Copilot: Policy applied in build_session_config")
    
    # Check Claude
    claude_path = Path("obscura/providers/claude.py")
    claude_content = claude_path.read_text()
    
    if "from obscura.core.tool_policy import ToolPolicy" not in claude_content:
        errors.append("❌ Claude: Missing ToolPolicy import")
    else:
        print("✅ Claude: ToolPolicy imported")
    
    if "tool_policy: ToolPolicy | None = None" not in claude_content:
        errors.append("❌ Claude: Missing tool_policy parameter")
    else:
        print("✅ Claude: tool_policy parameter added")
    
    if "self._tool_policy = tool_policy or ToolPolicy.from_env()" not in claude_content:
        errors.append("❌ Claude: Missing _tool_policy initialization")
    else:
        print("✅ Claude: _tool_policy initialized")
    
    if "apply_to_claude" not in claude_content:
        errors.append("❌ Claude: Missing policy application")
    else:
        print("✅ Claude: Policy applied in _build_options")
    
    if errors:
        print("\n❌ Integration incomplete:")
        for error in errors:
            print(f"  {error}")
        return False
    else:
        print("\n✅ All integration checks passed!")
        return True

def main():
    print("🚀 Starting ToolPolicy Integration\n")
    print("="*60)
    
    # Integrate Copilot
    print("\n📦 Integrating into Copilot Backend...\n")
    integrate_copilot()
    
    # Integrate Claude
    print("📦 Integrating into Claude Backend...\n")
    integrate_claude()
    
    # Verify
    success = verify_integration()
    
    print("\n" + "="*60)
    if success:
        print("\n🎉 Integration Complete!")
        print("\n📝 Next steps:")
        print("   1. Review the changes in the backup files")
        print("   2. Run: python3 -c 'from obscura.providers.copilot import CopilotBackend'")
        print("   3. Run: python3 -c 'from obscura.providers.claude import ClaudeBackend'")
        print("   4. Run tests: pytest tests/unit/obscura/core/test_tool_policy.py")
        print("   5. Test with: export OBSCURA_ALLOW_NATIVE_TOOLS=false")
    else:
        print("\n⚠️  Integration needs manual fixes. Check errors above.")
    print()

if __name__ == "__main__":
    main()
