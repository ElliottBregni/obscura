"""Adapter registry/factory for message platforms."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

AdapterBuilder = Callable[..., Any]


def _build_imessage_adapter(*, contacts: list[str], account_id: str = "default") -> Any:
    from obscura.integrations.imessage import IMessageAdapter

    return IMessageAdapter(contacts, account_id=account_id)


_ADAPTER_BUILDERS: dict[str, AdapterBuilder] = {
    "imessage": _build_imessage_adapter,
}


def register_adapter(platform: str, builder: AdapterBuilder) -> None:
    """Register a platform adapter builder at runtime."""
    _ADAPTER_BUILDERS[platform.strip().lower()] = builder


def get_adapter(*, platform: str, contacts: list[str], account_id: str = "default") -> Any:
    """Construct an adapter for the requested platform."""
    key = platform.strip().lower()
    builder = _ADAPTER_BUILDERS.get(key)
    if builder is None:
        raise ValueError(
            f"Unknown messaging platform '{platform}'. "
            f"Registered: {', '.join(sorted(_ADAPTER_BUILDERS))}"
        )
    return builder(contacts=contacts, account_id=account_id)
