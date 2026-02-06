#!/usr/bin/env python3
import subprocess
import sys

print("🧪 Running Multi-Agent Symlink Tests via Python")
print("=" * 60)
print()

try:
    # Make script executable
    subprocess.run(['chmod', '+x', '/Users/bregnie/FV-Copilot/test-multi-agent-sync.sh'], check=True)
    
    # Run the test script
    result = subprocess.run(
        ['/Users/bregnie/FV-Copilot/test-multi-agent-sync.sh'],
        cwd='/Users/bregnie/FV-Copilot',
        capture_output=True,
        text=True,
        timeout=60
    )
    
    print(result.stdout)
    if result.stderr:
        print("STDERR:", result.stderr, file=sys.stderr)
    
    sys.exit(result.returncode)
    
except subprocess.TimeoutExpired:
    print("❌ Test timed out after 60 seconds")
    sys.exit(1)
except Exception as e:
    print(f"❌ Error running tests: {e}")
    sys.exit(1)
