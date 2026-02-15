"""Routes: vector / semantic memory."""

from __future__ import annotations

from datetime import UTC
from typing import Any, cast

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from sdk.auth.models import AuthenticatedUser
from sdk.auth.rbac import AGENT_READ_ROLES, require_any_role
from sdk.deps import audit

router = APIRouter(prefix="/api/v1", tags=["vector-memory"])


# NOTE: Specific routes MUST be registered before the catch-all
# ``{namespace}/{key}`` pattern, otherwise FastAPI matches the generic
# pattern first (e.g. namespace="search", key="routed").


@router.get("/vector-memory/search")
async def vector_memory_search(
    q: str,
    namespace: str | None = None,
    top_k: int = 5,
    memory_types: str | None = None,
    rerank: bool = False,
    recency_weight: float = 0.2,
    first_stage_k: int = 50,
    date_from: str | None = None,
    date_to: str | None = None,
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_READ_ROLES)),
) -> JSONResponse:
    """Semantic search over vector memories with optional reranking."""
    from datetime import datetime as dt

    from sdk.vector_memory import VectorMemoryStore

    store = VectorMemoryStore.for_user(user)

    memory_type_list: list[str] | None = (
        memory_types.split(",") if memory_types else None
    )
    date_range: tuple[dt, dt] | None = None
    if date_from and date_to:
        date_range = (dt.fromisoformat(date_from), dt.fromisoformat(date_to))
    elif date_from:
        date_range = (dt.fromisoformat(date_from), dt.now(UTC))
    elif date_to:
        date_range = (dt.min, dt.fromisoformat(date_to))

    if rerank:
        results = store.search_reranked(
            q,
            namespace=namespace,
            top_k=top_k,
            first_stage_k=first_stage_k,
            memory_types=memory_type_list,
            date_range=date_range,
            recency_weight=recency_weight,
        )
    else:
        results = store.search_similar(
            q,
            namespace=namespace,
            top_k=top_k,
            memory_types=memory_type_list,
            date_range=date_range,
        )

    return JSONResponse(
        content={
            "query": q,
            "results": [
                {
                    "namespace": r.key.namespace,
                    "key": r.key.key,
                    "text": r.text,
                    "score": r.score,
                    "final_score": r.final_score,
                    "memory_type": r.memory_type,
                    "metadata": r.metadata,
                }
                for r in results
            ],
            "count": len(results),
        }
    )


@router.post("/vector-memory/search/routed")
async def vector_memory_search_routed(
    body: dict[str, Any],
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_READ_ROLES)),
) -> JSONResponse:
    """Multi-query search with memory type routing and weighted merging."""
    from sdk.vector_memory import VectorMemoryStore
    from sdk.vector_memory.vector_memory_router import MemoryRouter, MemoryTypeQuery
    from sdk.vector_memory import MetadataFilter

    store = VectorMemoryStore.for_user(user)
    router_inst = MemoryRouter(store)

    query: str = body["query"]
    route_configs: list[dict[str, Any]] = body.get("routes", [])
    metadata_filters_body = cast(
        list[MetadataFilter] | None, body.get("metadata_filters")
    )
    routes: list[MemoryTypeQuery] = [
        MemoryTypeQuery(
            memory_type=r["memory_type"],
            weight=r.get("weight", 1.0),
            top_k=r.get("top_k", 10),
        )
        for r in route_configs
    ]

    final_top_k: int = body.get("final_top_k", 10)
    ns: str | None = body.get("namespace")
    result = router_inst.route_and_merge(
        query=query,
        routes=routes,
        final_top_k=final_top_k,
        namespace=ns,
        metadata_filters=metadata_filters_body,
    )

    return JSONResponse(
        content={
            "query": query,
            "results": [
                {
                    "namespace": r.key.namespace,
                    "key": r.key.key,
                    "text": r.text,
                    "score": r.score,
                    "final_score": r.final_score,
                    "memory_type": r.memory_type,
                    "metadata": r.metadata,
                }
                for r in result.entries
            ],
            "sources": result.sources,
            "count": len(result.entries),
        }
    )


# Catch-all route -- MUST be last to avoid shadowing specific routes above.
@router.post("/vector-memory/{namespace}/{key}")
async def vector_memory_set(
    namespace: str,
    key: str,
    body: dict[str, Any],
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_READ_ROLES)),
) -> JSONResponse:
    """Store text with semantic embedding for vector search."""
    from sdk.vector_memory import VectorMemoryStore

    store = VectorMemoryStore.for_user(user)
    text: str = body.get("text", "")
    metadata: dict[str, Any] = body.get("metadata", {})
    memory_type: str = body.get("memory_type", "general")
    store.set(
        key, text, metadata=metadata, namespace=namespace, memory_type=memory_type
    )
    audit("vector_memory.set", user, f"vector:{namespace}:{key}", "write", "success")
    return JSONResponse(
        content={"namespace": namespace, "key": key, "stored": True, "type": "vector"}
    )
