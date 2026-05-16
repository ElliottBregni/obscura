"""obscura.integrations.network_gateway — OpenAI-compatible network gateway.

Exposes Obscura agents over HTTP on port 18790 with:

* ``POST /v1/chat/completions`` — OpenAI-compatible chat completions
* ``GET  /v1/models``           — list Obscura backends as model objects
* A2A routers mounted at ``/a2a/``
* ``GET  /health``              — unauthenticated health probe
* ``GET  /.well-known/agent.json`` — A2A discovery (unauthenticated)

Entry point::

    uvicorn obscura.integrations.network_gateway.app:app --host 0.0.0.0 --port 18790

Or programmatically::

    from obscura.integrations.network_gateway.app import create_gateway_app
    from obscura.integrations.network_gateway.config import GatewayConfig

    app = create_gateway_app(GatewayConfig())

"""

from obscura.integrations.network_gateway.app import create_gateway_app
from obscura.integrations.network_gateway.config import GatewayConfig

__all__ = ["GatewayConfig", "create_gateway_app"]
