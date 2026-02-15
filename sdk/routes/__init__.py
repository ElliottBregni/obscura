"""
sdk.routes -- FastAPI route modules for the Obscura API.
"""

from sdk.routes.admin import router as admin_router
from sdk.routes.capabilities import router as capabilities_router
from sdk.routes.agent_groups import router as agent_groups_router
from sdk.routes.agents import router as agents_router
from sdk.routes.health import router as health_router
from sdk.routes.heartbeat import router as heartbeat_router
from sdk.routes.heartbeat import ws_router as heartbeat_ws_router
from sdk.routes.memory import router as memory_router
from sdk.routes.send import router as send_router
from sdk.routes.sessions import router as sessions_router
from sdk.routes.sync import router as sync_router
from sdk.routes.vector_memory import router as vector_memory_router
from sdk.routes.websockets import router as websockets_router
from sdk.routes.webhooks import router as webhooks_router
from sdk.routes.workflows import router as workflows_router

all_routers = [
    health_router,
    send_router,
    sessions_router,
    sync_router,
    memory_router,
    vector_memory_router,
    agents_router,
    agent_groups_router,
    websockets_router,
    workflows_router,
    webhooks_router,
    admin_router,
    heartbeat_router,
    heartbeat_ws_router,
    capabilities_router,
]
