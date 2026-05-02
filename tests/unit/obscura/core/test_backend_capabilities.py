"""Optional backend capability protocols.

``ToolRouterCapable`` and ``ConfirmationCapable`` are runtime-checkable
protocols that gate calls to backend-specific methods. They replace
``hasattr(...)`` probes with type-safe ``isinstance(...)`` narrowing.
"""

from __future__ import annotations

from typing import Any

from obscura.core.types import ConfirmationCapable, ToolRouterCapable


class _RouterBackend:
    """Backend that opts into ToolRouterCapable."""

    def set_tool_router(self, router: Any) -> None:  # noqa: ANN401  protocol shape
        self.router = router


class _ConfirmBackend:
    """Backend that opts into ConfirmationCapable."""

    def enable_confirmation(self, confirm_fn: Any) -> None:  # noqa: ANN401  protocol shape
        self.confirm_fn = confirm_fn


class _BareBackend:
    """Backend with neither capability."""


def test_isinstance_narrows_to_router_capable() -> None:
    backend = _RouterBackend()
    assert isinstance(backend, ToolRouterCapable)
    # Narrowing lets the call site invoke the method without hasattr probes.
    if isinstance(backend, ToolRouterCapable):
        backend.set_tool_router("router-X")
    assert backend.router == "router-X"


def test_isinstance_rejects_backend_missing_method() -> None:
    assert not isinstance(_BareBackend(), ToolRouterCapable)
    assert not isinstance(_BareBackend(), ConfirmationCapable)


def test_isinstance_narrows_to_confirmation_capable() -> None:
    backend = _ConfirmBackend()
    assert isinstance(backend, ConfirmationCapable)
    if isinstance(backend, ConfirmationCapable):
        backend.enable_confirmation(lambda name, inp: True)
    assert backend.confirm_fn("any", {}) is True


def test_protocols_are_independent() -> None:
    """A backend can implement one capability without the other."""
    assert isinstance(_RouterBackend(), ToolRouterCapable)
    assert not isinstance(_RouterBackend(), ConfirmationCapable)

    assert isinstance(_ConfirmBackend(), ConfirmationCapable)
    assert not isinstance(_ConfirmBackend(), ToolRouterCapable)
