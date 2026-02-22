"""
obscura.routes -- FastAPI route modules for the Obscura API.
"""

from obscura.routes.admin import router as admin_router
from obscura.routes.capabilities import router as capabilities_router
from obscura.routes.agent_groups import router as agent_groups_router
from obscura.routes.agents import router as agents_router
from obscura.routes.health import router as health_router
from obscura.routes.heartbeat import router as heartbeat_router
from obscura.routes.heartbeat import ws_router as heartbeat_ws_router
from obscura.routes.memory import router as memory_router
from obscura.routes.observe import router as observe_router
from obscura.routes.send import router as send_router
from obscura.routes.sessions import router as sessions_router
from obscura.routes.sync import router as sync_router
from obscura.routes.tool_approvals import router as tool_approvals_router
from obscura.routes.vector_memory import router as vector_memory_router
from obscura.routes.websockets import router as websockets_router
from obscura.routes.webhooks import router as webhooks_router
from obscura.routes.skills import router as skills_router
from obscura.routes.workflows import router as workflows_router

all_routers = [
    health_router,
    send_router,
    sessions_router,
    sync_router,
    tool_approvals_router,
    memory_router,
    observe_router,
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
    skills_router,
]
