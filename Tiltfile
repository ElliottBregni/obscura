# =============================================================================
# Tiltfile — Obscura live-reload development loop
# =============================================================================
# Usage: tilt up
# Prereqs: kind cluster running, Tilt installed

# Build the Obscura SDK image with live-update for fast iteration
docker_build(
    'obscura-sdk',
    '.',
    live_update=[
        sync('sdk/', '/app/sdk/'),
        sync('sync.py', '/app/sync.py'),
        sync('copilot_models.py', '/app/copilot_models.py'),
        run('pip install -e .', trigger=['pyproject.toml']),
    ],
)

# Deploy Obscura via Helm with local overrides
k8s_yaml(helm(
    'helm/obscura/',
    values=['helm/obscura/values-local.yaml'],
))

# Deploy the OTel observability stack
k8s_yaml('infra/k8s/otel-stack.yaml')

# Port-forward all key services
k8s_resource('obscura', port_forwards=['8080:8080'])
k8s_resource('jaeger', port_forwards=['16686:16686'])
k8s_resource('grafana', port_forwards=['3000:3000'])
k8s_resource('prometheus', port_forwards=['9090:9090'])
k8s_resource('zitadel', port_forwards=['8081:8080'])
