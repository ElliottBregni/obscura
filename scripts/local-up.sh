#!/bin/bash
# =============================================================================
# local-up.sh — One-command Obscura local K8s setup
# =============================================================================
# Creates a kind cluster, builds the Docker image, deploys the full stack.
# Usage: ./scripts/local-up.sh
# =============================================================================
set -euo pipefail

CLUSTER_NAME="obscura"
NAMESPACE="obscura"
IMAGE_NAME="obscura-sdk:local"

echo "=== Obscura Local Development Setup ==="
echo ""

# 1. Create kind cluster (skip if already exists)
if kind get clusters 2>/dev/null | grep -q "^${CLUSTER_NAME}$"; then
    echo "[1/8] Kind cluster '${CLUSTER_NAME}' already exists, skipping..."
else
    echo "[1/8] Creating kind cluster..."
    kind create cluster --config infra/k8s/kind-config.yaml --name "${CLUSTER_NAME}"
fi

# 2. Build Docker image
echo "[2/8] Building Docker image..."
docker build -t "${IMAGE_NAME}" .

# 3. Load image into kind
echo "[3/8] Loading image into kind cluster..."
kind load docker-image "${IMAGE_NAME}" --name "${CLUSTER_NAME}"

# 4. Create namespace
echo "[4/8] Creating namespace..."
kubectl apply -f infra/k8s/namespace.yaml

# 5. Deploy OTel observability stack
echo "[5/8] Deploying observability stack (OTel Collector, Jaeger, Prometheus, Grafana)..."
kubectl apply -f infra/k8s/otel-stack.yaml -n "${NAMESPACE}"

# 6. Install Helm chart (includes Zitadel subchart)
echo "[6/8] Installing Obscura Helm chart..."
helm dependency update helm/obscura/ 2>/dev/null || true
helm upgrade --install obscura helm/obscura/ \
    -n "${NAMESPACE}" \
    -f helm/obscura/values-local.yaml \
    --wait --timeout 180s

# 7. Wait for pods to be ready
echo "[7/8] Waiting for pods to be ready..."
kubectl wait --for=condition=ready pod \
    -l app.kubernetes.io/name=obscura \
    -n "${NAMESPACE}" \
    --timeout=180s 2>/dev/null || echo "  (obscura pod not ready yet, continuing...)"

kubectl wait --for=condition=ready pod \
    -l app.kubernetes.io/name=otel-collector \
    -n "${NAMESPACE}" \
    --timeout=120s 2>/dev/null || echo "  (otel-collector not ready yet, continuing...)"

# 8. Bootstrap Zitadel (if setup script exists)
echo "[8/8] Bootstrapping Zitadel..."
if [ -f "infra/zitadel/setup.py" ]; then
    python infra/zitadel/setup.py --k8s || echo "  (Zitadel bootstrap skipped or failed)"
else
    echo "  (infra/zitadel/setup.py not found, skipping Zitadel bootstrap)"
fi

echo ""
echo "=== Obscura is running! ==="
echo ""
echo "  API:        http://localhost:8080"
echo "  Jaeger:     http://localhost:16686"
echo "  Grafana:    http://localhost:3000  (admin/obscura)"
echo "  Prometheus: http://localhost:9090"
echo ""
echo "  kubectl:    kubectl -n ${NAMESPACE} get pods"
echo "  logs:       kubectl -n ${NAMESPACE} logs -f -l app.kubernetes.io/name=obscura"
echo "  teardown:   ./scripts/local-down.sh"
echo ""
