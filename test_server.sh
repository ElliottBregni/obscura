#!/bin/bash
# Test if server picks up env vars

echo "Testing env var passing..."

# Export vars explicitly
export OBSCURA_AUTH_ENABLED=false
export OTEL_ENABLED=false
export OBSCURA_LOG_LEVEL=DEBUG

echo "OBSCURA_AUTH_ENABLED=$OBSCURA_AUTH_ENABLED"
echo "OTEL_ENABLED=$OTEL_ENABLED"

# Test Python can see them
python3 -c "import os; print(f'Auth: {os.environ.get(\"OBSCURA_AUTH_ENABLED\", \"NOT SET\")}')"

# Now run server with explicit env
exec env OBSCURA_AUTH_ENABLED=false OTEL_ENABLED=false \
    python3 -m uvicorn sdk.server:create_app --factory --port 8080 --host 0.0.0.0
