#!/bin/bash
# Test script for multi-agent symlink fixes

set -e

VAULT_PATH="$HOME/FV-Copilot"
TEST_REPO="$HOME/git/FV-Platform-Main"

echo "🧪 Testing Multi-Agent Symlink Fixes"
echo "===================================="
echo ""

# Test 1: Agent-to-path mapping
echo "Test 1: Agent-to-path mapping"
echo "------------------------------"
source "$VAULT_PATH/watch-and-sync.sh"

echo -n "  copilot → "
get_agent_target_path "copilot"

echo -n "  claude → "
get_agent_target_path "claude"

echo -n "  cursor → "
get_agent_target_path "cursor"

echo -n "  custom → "
get_agent_target_path "custom"

echo ""

# Test 2: Check registered agents
echo "Test 2: Registered Agents"
echo "-------------------------"
get_registered_agents | sed 's/^/  - /'
echo ""

# Test 3: Clean up existing symlinks
echo "Test 3: Cleanup"
echo "---------------"
if [ -L "$TEST_REPO/.github" ]; then
    echo "  Removing existing .github symlink"
    rm "$TEST_REPO/.github"
fi

if [ -L "$TEST_REPO/.claude" ]; then
    echo "  Removing existing .claude symlink"
    rm "$TEST_REPO/.claude"
fi
echo "  ✓ Clean"
echo ""

# Test 4: Create copilot symlink
echo "Test 4: Create Copilot Symlink"
echo "-------------------------------"
cd "$VAULT_PATH"
./watch-and-sync.sh --repo "$TEST_REPO" --agent copilot --mode symlink
echo ""

# Verify copilot symlink
if [ -L "$TEST_REPO/.github" ]; then
    target=$(readlink "$TEST_REPO/.github")
    echo "  ✅ .github symlink created"
    echo "     Target: $target"
    
    if [ -e "$TEST_REPO/.github/copilot-instructions.md" ]; then
        echo "  ✅ copilot-instructions.md accessible via symlink"
    else
        echo "  ❌ copilot-instructions.md NOT accessible"
    fi
else
    echo "  ❌ .github symlink NOT created"
fi
echo ""

# Test 5: Create claude symlink
echo "Test 5: Create Claude Symlink"
echo "------------------------------"
./watch-and-sync.sh --repo "$TEST_REPO" --agent claude --mode symlink
echo ""

# Verify claude symlink
if [ -L "$TEST_REPO/.claude" ]; then
    target=$(readlink "$TEST_REPO/.claude")
    echo "  ✅ .claude symlink created"
    echo "     Target: $target"
    
    if [ -e "$TEST_REPO/.claude/copilot-instructions.md" ]; then
        echo "  ✅ copilot-instructions.md accessible via symlink"
    else
        echo "  ❌ copilot-instructions.md NOT accessible"
    fi
else
    echo "  ❌ .claude symlink NOT created"
fi
echo ""

# Test 6: Verify both symlinks coexist
echo "Test 6: Multi-Agent Coexistence"
echo "--------------------------------"
if [ -L "$TEST_REPO/.github" ] && [ -L "$TEST_REPO/.claude" ]; then
    echo "  ✅ Both .github and .claude symlinks exist"
    echo ""
    echo "  Structure:"
    ls -la "$TEST_REPO" | grep -E "^\s*(\.github|\.claude)" | sed 's/^/    /'
else
    echo "  ❌ Both symlinks do NOT exist"
    if [ -L "$TEST_REPO/.github" ]; then
        echo "     .github: EXISTS"
    else
        echo "     .github: MISSING"
    fi
    
    if [ -L "$TEST_REPO/.claude" ]; then
        echo "     .claude: EXISTS"
    else
        echo "     .claude: MISSING"
    fi
fi
echo ""

# Test 7: Broken symlink repair
echo "Test 7: Broken Symlink Repair"
echo "------------------------------"
# Create a broken symlink
rm "$TEST_REPO/.github" 2>/dev/null || true
ln -s "/nonexistent/path" "$TEST_REPO/.github"
echo "  Created broken symlink: .github → /nonexistent/path"

# Run sync again - should repair
./watch-and-sync.sh --repo "$TEST_REPO" --agent copilot --mode symlink
echo ""

# Verify repaired
if [ -L "$TEST_REPO/.github" ] && [ -e "$TEST_REPO/.github" ]; then
    echo "  ✅ Broken symlink repaired successfully"
else
    echo "  ❌ Broken symlink NOT repaired"
fi
echo ""

# Final summary
echo "=================================="
echo "🎯 Test Summary"
echo "=================================="
echo ""

PASS=0
FAIL=0

# Check .github
if [ -L "$TEST_REPO/.github" ] && [ -e "$TEST_REPO/.github" ]; then
    echo "✅ Copilot (.github) - PASS"
    PASS=$((PASS + 1))
else
    echo "❌ Copilot (.github) - FAIL"
    FAIL=$((FAIL + 1))
fi

# Check .claude
if [ -L "$TEST_REPO/.claude" ] && [ -e "$TEST_REPO/.claude" ]; then
    echo "✅ Claude (.claude) - PASS"
    PASS=$((PASS + 1))
else
    echo "❌ Claude (.claude) - FAIL"
    FAIL=$((FAIL + 1))
fi

# Check agent-to-path mapping
copilot_path=$(get_agent_target_path "copilot")
claude_path=$(get_agent_target_path "claude")

if [ "$copilot_path" = ".github" ] && [ "$claude_path" = ".claude" ]; then
    echo "✅ Agent-to-path mapping - PASS"
    PASS=$((PASS + 1))
else
    echo "❌ Agent-to-path mapping - FAIL"
    FAIL=$((FAIL + 1))
fi

echo ""
echo "Total: $PASS passed, $FAIL failed"

if [ $FAIL -eq 0 ]; then
    echo ""
    echo "🎉 ALL TESTS PASSED!"
    exit 0
else
    echo ""
    echo "⚠️  SOME TESTS FAILED"
    exit 1
fi
