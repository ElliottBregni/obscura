"""Routes: key-value memory store."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from sdk.auth.models import AuthenticatedUser
from sdk.auth.rbac import AGENT_READ_ROLES, AGENT_WRITE_ROLES, require_any_role
from sdk.deps import audit

router = APIRouter(prefix="/api/v1", tags=["memory"])

# In-memory namespace config (lateral move from server.py)
_memory_namespaces: dict[str, dict] = {}


# -- list / search / stats ------------------------------------------------


@router.get("/memory")
async def memory_list(
    namespace: str | None = None,
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_READ_ROLES)),
) -> JSONResponse:
    """List all memory keys for the user."""
    from sdk.memory import MemoryStore
    store = MemoryStore.for_user(user)
    keys = store.list_keys(namespace=namespace)
    return JSONResponse(content={
        "keys": [{"namespace": k.namespace, "key": k.key} for k in keys],
        "count": len(keys),
    })


@router.get("/memory/search")
async def memory_search(
    q: str,
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_READ_ROLES)),
) -> JSONResponse:
    """Search memory keys and values."""
    from sdk.memory import MemoryStore
    store = MemoryStore.for_user(user)
    results = store.search(q)
    return JSONResponse(content={
        "results": [{"namespace": k.namespace, "key": k.key, "value": v} for k, v in results],
        "count": len(results),
    })


@router.get("/memory/stats")
async def memory_stats(
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_READ_ROLES)),
) -> JSONResponse:
    """Get memory usage statistics."""
    from sdk.memory import MemoryStore
    store = MemoryStore.for_user(user)
    stats = store.get_stats()
    return JSONResponse(content=stats)


# -- namespaces ------------------------------------------------------------


@router.get("/memory/namespaces")
async def memory_namespace_list(
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_READ_ROLES)),
) -> JSONResponse:
    """List all memory namespaces."""
    from sdk.memory import MemoryStore
    store = MemoryStore.for_user(user)
    keys = store.list_keys()
    namespaces = set(k.namespace for k in keys)

    for ns_id, ns_data in _memory_namespaces.items():
        if ns_data.get("created_by") == user.user_id:
            namespaces.add(ns_id)

    return JSONResponse(content={
        "namespaces": sorted(list(namespaces)),
        "count": len(namespaces),
    })


@router.post("/memory/namespaces")
async def memory_namespace_create(
    body: dict,
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_WRITE_ROLES)),
) -> JSONResponse:
    """Create a new memory namespace with configuration."""
    namespace_id = body.get("name", str(uuid.uuid4()))

    namespace = {
        "namespace_id": namespace_id,
        "description": body.get("description", ""),
        "ttl_days": body.get("ttl_days"),
        "created_by": user.user_id,
        "created_at": datetime.now(UTC).isoformat(),
    }

    _memory_namespaces[namespace_id] = namespace

    audit("memory.namespace.create", user, f"memory:ns:{namespace_id}", "create", "success")

    return JSONResponse(content=namespace)


@router.delete("/memory/namespaces/{namespace}")
async def memory_namespace_delete(
    namespace: str,
    delete_data: bool = False,
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_WRITE_ROLES)),
) -> JSONResponse:
    """Delete a memory namespace. Optionally delete all data in it."""
    from sdk.memory import MemoryStore

    if namespace in _memory_namespaces:
        del _memory_namespaces[namespace]

    deleted_keys = 0
    if delete_data:
        store = MemoryStore.for_user(user)
        keys = store.list_keys(namespace=namespace)
        for key in keys:
            store.delete(key.key, namespace=key.namespace)
            deleted_keys += 1

    audit("memory.namespace.delete", user, f"memory:ns:{namespace}", "delete", "success",
          deleted_keys=deleted_keys)

    return JSONResponse(content={
        "namespace": namespace,
        "deleted": True,
        "keys_deleted": deleted_keys,
    })


@router.get("/memory/namespaces/{namespace}/stats")
async def memory_namespace_stats(
    namespace: str,
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_READ_ROLES)),
) -> JSONResponse:
    """Get statistics for a specific namespace."""
    from sdk.memory import MemoryStore
    store = MemoryStore.for_user(user)
    keys = store.list_keys(namespace=namespace)

    total_size = 0
    for key in keys:
        value = store.get(key.key, namespace=key.namespace)
        if value:
            total_size += len(str(value).encode('utf-8'))

    return JSONResponse(content={
        "namespace": namespace,
        "key_count": len(keys),
        "total_size_bytes": total_size,
    })


# -- transactions ----------------------------------------------------------


@router.post("/memory/transaction")
async def memory_transaction(
    body: dict,
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_WRITE_ROLES)),
) -> JSONResponse:
    """Execute multiple memory operations atomically."""
    from sdk.memory import MemoryStore
    store = MemoryStore.for_user(user)

    operations = body.get("operations", [])
    if not operations:
        raise HTTPException(status_code=400, detail="No operations provided")

    results = []
    errors = []

    for idx, op in enumerate(operations):
        try:
            op_type = op.get("op")
            ns = op.get("namespace", "default")
            key = op.get("key")

            if op_type == "set":
                value = op.get("value")
                store.set(key, value, namespace=ns)
                results.append({"idx": idx, "op": "set", "status": "ok"})
            elif op_type == "get":
                value = store.get(key, namespace=ns)
                results.append({"idx": idx, "op": "get", "status": "ok", "value": value})
            elif op_type == "delete":
                deleted = store.delete(key, namespace=ns)
                results.append({"idx": idx, "op": "delete", "status": "ok", "deleted": deleted})
            else:
                errors.append({"idx": idx, "error": f"Unknown operation: {op_type}"})
        except Exception as e:
            errors.append({"idx": idx, "error": str(e)})

    return JSONResponse(content={
        "results": results,
        "errors": errors,
        "total_ops": len(operations),
        "successful": len(results),
    })


# -- import / export -------------------------------------------------------


@router.get("/memory/export")
async def memory_export(
    namespace: str | None = None,
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_READ_ROLES)),
) -> JSONResponse:
    """Export memory data as JSON."""
    from sdk.memory import MemoryStore
    store = MemoryStore.for_user(user)

    keys = store.list_keys(namespace=namespace)
    data: dict[str, dict] = {}

    for key in keys:
        value = store.get(key.key, namespace=key.namespace)
        if value is not None:
            ns = key.namespace
            if ns not in data:
                data[ns] = {}
            data[ns][key.key] = value

    return JSONResponse(content={
        "exported_at": datetime.now(UTC).isoformat(),
        "namespaces": list(data.keys()),
        "total_keys": sum(len(v) for v in data.values()),
        "data": data,
    })


@router.post("/memory/import")
async def memory_import(
    body: dict,
    overwrite: bool = True,
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_WRITE_ROLES)),
) -> JSONResponse:
    """Import memory data from JSON."""
    from sdk.memory import MemoryStore
    store = MemoryStore.for_user(user)

    import_data = body.get("data", {})
    if not import_data:
        raise HTTPException(status_code=400, detail="No data provided")

    imported = 0
    skipped = 0
    errors = []

    for ns, ks in import_data.items():
        for key, value in ks.items():
            try:
                if not overwrite:
                    existing = store.get(key, namespace=ns)
                    if existing is not None:
                        skipped += 1
                        continue
                store.set(key, value, namespace=ns)
                imported += 1
            except Exception as e:
                errors.append({"namespace": ns, "key": key, "error": str(e)})

    audit("memory.import", user, "memory", "import", "success",
          imported=imported, skipped=skipped)

    return JSONResponse(content={
        "imported": imported,
        "skipped": skipped,
        "errors": errors,
        "total": imported + skipped,
    })


# -- key-value (catch-all, must be after specific /memory/ routes) ---------


@router.get("/memory/{namespace}/{key}")
async def memory_get(
    namespace: str,
    key: str,
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_READ_ROLES)),
) -> JSONResponse:
    """Get a value from the user's memory store."""
    from sdk.memory import MemoryStore
    store = MemoryStore.for_user(user)
    value = store.get(key, namespace=namespace)
    if value is None:
        raise HTTPException(status_code=404, detail=f"Key '{namespace}:{key}' not found")
    return JSONResponse(content={"namespace": namespace, "key": key, "value": value})


@router.post("/memory/{namespace}/{key}")
async def memory_set(
    namespace: str,
    key: str,
    body: dict,
    ttl: int | None = None,
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_READ_ROLES)),
) -> JSONResponse:
    """Store a value in the user's memory store."""
    from sdk.memory import MemoryStore
    store = MemoryStore.for_user(user)
    value = body.get("value")
    ttl_delta = timedelta(seconds=ttl) if ttl else None
    store.set(key, value, namespace=namespace, ttl=ttl_delta)
    audit("memory.set", user, f"memory:{namespace}:{key}", "write", "success")
    return JSONResponse(content={"namespace": namespace, "key": key, "stored": True})


@router.delete("/memory/{namespace}/{key}")
async def memory_delete(
    namespace: str,
    key: str,
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_READ_ROLES)),
) -> JSONResponse:
    """Delete a key from the user's memory store."""
    from sdk.memory import MemoryStore
    store = MemoryStore.for_user(user)
    deleted = store.delete(key, namespace=namespace)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Key '{namespace}:{key}' not found")
    audit("memory.delete", user, f"memory:{namespace}:{key}", "delete", "success")
    return JSONResponse(content={"namespace": namespace, "key": key, "deleted": True})
