"""Native Python adapter — loads tools from dotted handler references."""

from __future__ import annotations

import importlib
import logging
from typing import Any

from obscura.plugins.models import PluginSpec

logger = logging.getLogger(__name__)


class NativeAdapter:
    """Adapter for native Python plugins (runtime_type == 'native').

    Resolves dotted handler paths like ``mypackage.tools:search_repo``
    into callable functions.
    """

    def can_handle(self, spec: PluginSpec) -> bool:
        return spec.runtime_type == "native"

    async def load(self, spec: PluginSpec, config: dict[str, Any]) -> dict[str, Any]:
        handlers: dict[str, Any] = {}
        for tool in spec.tools:
            if not tool.handler:
                continue
            try:
                handler = _resolve_handler(tool.handler)
                handlers[tool.name] = handler
                logger.debug("Resolved handler %s → %s", tool.name, tool.handler)
            except Exception as exc:
                logger.warning("Cannot resolve handler %s for tool %s: %s", tool.handler, tool.name, exc)
        return {"handlers": handlers}

    async def healthcheck(self, spec: PluginSpec) -> bool:
        if spec.healthcheck and spec.healthcheck.type == "callable":
            try:
                fn = _resolve_handler(spec.healthcheck.target)
                return bool(fn())
            except Exception:
                return False
        return True

    async def teardown(self, spec: PluginSpec) -> None:
        pass  # No persistent resources for native plugins


def _resolve_handler(ref: str) -> Any:
    """Resolve 'module.path:function_name' to a callable."""
    if ":" in ref:
        module_path, _, attr_name = ref.rpartition(":")
    else:
        module_path, _, attr_name = ref.rpartition(".")

    mod = importlib.import_module(module_path)
    return getattr(mod, attr_name)
