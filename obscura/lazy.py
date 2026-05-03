"""obscura.lazy — single source of truth for lazy-loaded public-API names.

The package's ``obscura/__init__.py`` uses PEP 562 ``__getattr__`` to delegate
attribute lookup of un-eagerly-imported names to :func:`resolve` here. That
keeps ``import obscura`` cheap — no SDK chains, no qdrant, no psycopg — while
still exposing the full public API surface that callers expect.

To expose a new name, add an entry to ``_LAZY``. Each entry is keyed by the
public name and maps to ``(module_path, attr_name, extras)``:

- ``module_path`` — the dotted module to import on first access.
- ``attr_name`` — the attribute on that module to return.
- ``extras`` — optional tuple of extras names. If the import fails, the
  raised :class:`MissingExtraError` includes a hint pointing at the right
  ``uv pip install obscura[extra]`` invocation. Empty tuple means the
  module is always available (just lazy for startup speed).
"""

from __future__ import annotations

import importlib
from typing import Any

__all__ = ["MissingExtraError", "known_names", "resolve"]


class MissingExtraError(ImportError):
    """Raised when a lazy public-API name requires an optional extra
    not installed."""


# ---------------------------------------------------------------------------
# Registry: public-name → (module_path, attr_name, extras)
# ---------------------------------------------------------------------------

_LAZY: dict[str, tuple[str, str, tuple[str, ...]]] = {
    # --- Client / config ---
    "ObscuraClient": ("obscura.core.client", "ObscuraClient", ()),
    "ObscuraConfig": ("obscura.core.config", "ObscuraConfig", ()),
    # --- Auth ---
    "AuthConfig": ("obscura.core.auth", "AuthConfig", ()),
    # --- Context / handlers ---
    "ContextLoader": ("obscura.core.context", "ContextLoader", ()),
    "RequestHandler": ("obscura.core.handlers", "RequestHandler", ()),
    "SimpleHandler": ("obscura.core.handlers", "SimpleHandler", ()),
    # --- Agent base class ---
    "BaseAgent": ("obscura.agent.agent", "BaseAgent", ()),
    # --- OpenClaw bridge (pulls httpx) ---
    "OpenClawBridge": ("obscura.openclaw_bridge", "OpenClawBridge", ()),
    "OpenClawBridgeConfig": ("obscura.openclaw_bridge", "OpenClawBridgeConfig", ()),
    "BackendRoutingPolicy": (
        "obscura.openclaw_bridge",
        "BackendRoutingPolicy",
        (),
    ),
    "MemoryWriteRequest": ("obscura.openclaw_bridge", "MemoryWriteRequest", ()),
    "RequestMetadata": ("obscura.openclaw_bridge", "RequestMetadata", ()),
    "RunAgentRequest": ("obscura.openclaw_bridge", "RunAgentRequest", ()),
    "SemanticSearchRequest": (
        "obscura.openclaw_bridge",
        "SemanticSearchRequest",
        (),
    ),
    "SpawnAgentRequest": ("obscura.openclaw_bridge", "SpawnAgentRequest", ()),
    "WorkflowRunRequest": ("obscura.openclaw_bridge", "WorkflowRunRequest", ()),
}


def resolve(name: str) -> Any:
    """Look up a lazy public-API name and return the underlying attribute.

    Raises :class:`AttributeError` when *name* is not registered (matches the
    PEP 562 ``__getattr__`` contract), or :class:`MissingExtraError` when the
    target module fails to import and an optional extra would have provided
    it.
    """
    entry = _LAZY.get(name)
    if entry is None:
        msg = f"module 'obscura' has no attribute {name!r}"
        raise AttributeError(msg)

    module_path, attr_name, extras = entry
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        if extras:
            extras_hint = ",".join(extras)
            msg = (
                f"obscura.{name} requires the {extras_hint!r} extra. "
                f"Install with: uv pip install obscura[{extras_hint}]"
            )
            raise MissingExtraError(msg) from exc
        raise
    return getattr(module, attr_name)


def known_names() -> tuple[str, ...]:
    """Return all registered lazy names — useful for ``__all__`` enumeration
    and IDE autocomplete drivers that introspect this module."""
    return tuple(_LAZY)
