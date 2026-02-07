#!/bin/bash
# E2E Test Runner with Temporary Server
# Usage: ./scripts/run-e2e-tests.sh

set -e

echo "🧪 Obscura E2E Test Runner"
echo "=========================="

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Export test configuration
export OBSCURA_URL=http://localhost:8080
export OBSCURA_AUTH_ENABLED=false
export OTEL_ENABLED=false

# Check if server is already running
if curl -s http://localhost:8080/health > /dev/null; then
    echo -e "${YELLOW}⚠️  Server already running on localhost:8080${NC}"
    echo "Using existing server (may need auth token)..."
else
    echo "🚀 Starting temporary server..."
    echo "   Auth: disabled"
    echo "   OTel: disabled"
    
    # Start server in background with explicit env vars
    env OBSCURA_AUTH_ENABLED=false OTEL_ENABLED=false \
        python -m uvicorn sdk.server:create_app --factory --port 8080 --host 0.0.0.0 &
    SERVER_PID=$!
    
    # Trap to kill server on exit
    trap "echo ''; echo 'Stopping server...'; kill $SERVER_PID 2>/dev/null || true" EXIT
    
    # Wait for server to be ready
    echo "⏳ Waiting for server to start..."
    for i in {1..30}; do
        if curl -s http://localhost:8080/health > /dev/null 2>&1; then
            echo -e "${GREEN}✓ Server ready${NC}"
            break
        fi
        sleep 1
        if [ $i -eq 30 ]; then
            echo -e "${RED}✗ Server failed to start${NC}"
            exit 1
        fi
    done
fi

echo ""
echo "🧪 Running E2E tests..."
echo ""

# Run tests without token (auth disabled)
export OBSCURA_TOKEN=""

# Run tests
if pytest tests/e2e/ -v --run-e2e "$@"; then
    echo ""
    echo -e "${GREEN}✓ All E2E tests passed!${NC}"
    exit 0
else
    echo ""
    echo -e "${RED}✗ Some tests failed${NC}"
    exit 1
fi
