#!/bin/bash
# =============================================================================
# local-down.sh — Tear down the Obscura kind cluster
# =============================================================================
# Usage: ./scripts/local-down.sh
# =============================================================================
set -euo pipefail

CLUSTER_NAME="obscura"

echo "=== Tearing down Obscura local cluster ==="

if kind get clusters 2>/dev/null | grep -q "^${CLUSTER_NAME}$"; then
    kind delete cluster --name "${CLUSTER_NAME}"
    echo "Cluster '${CLUSTER_NAME}' deleted."
else
    echo "Cluster '${CLUSTER_NAME}' not found, nothing to do."
fi
