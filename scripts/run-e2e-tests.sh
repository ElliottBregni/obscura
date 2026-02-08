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
export OBSCURA_PORT=8080
export OBSCURA_AUTH_ENABLED=false
export OTEL_ENABLED=false
export OBSCURA_LOG_LEVEL=WARNING
export OBSCURA_TOKEN=""

echo "Environment:"
echo "  OBSCURA_AUTH_ENABLED=$OBSCURA_AUTH_ENABLED"
echo "  OTEL_ENABLED=$OTEL_ENABLED"
echo ""

# Kill any existing server on port 8080
echo "Checking for existing server..."
lsof -ti:8080 | xargs kill 2>/dev/null || true
sleep 1

# Check if server is already running
if curl -s http://localhost:8080/health > /dev/null 2>&1; then
    echo -e "${YELLOW}⚠️  Server already running on localhost:8080${NC}"
    echo "Using existing server..."
else
    echo "🚀 Starting test server..."
    
    # Use uv if available, otherwise run with python directly
    if command -v uv >/dev/null 2>&1; then
        uv run python scripts/test_server.py &
    else
        python scripts/test_server.py &
    fi
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

# Run tests
if command -v uv >/dev/null 2>&1; then
    if uv run pytest tests/e2e/ -v --run-e2e "$@"; then
        echo ""
        echo -e "${GREEN}✓ All E2E tests passed!${NC}"
        exit 0
    else
        echo ""
        echo -e "${RED}✗ Some tests failed${NC}"
        exit 1
    fi
else
    if python -m pytest tests/e2e/ -v --run-e2e "$@"; then
        echo ""
        echo -e "${GREEN}✓ All E2E tests passed!${NC}"
        exit 0
    else
        echo ""
        echo -e "${RED}✗ Some tests failed${NC}"
        exit 1
    fi
fi
