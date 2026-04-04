"""Tests for startup health checks."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from obscura.core.health import collect_startup_health

if TYPE_CHECKING:
    import pytest


class TestCheckVectorMemory:
    def test_healthy_qdrant_not_reported(self) -> None:
        """When Qdrant is requested and running, no check is returned."""
        store = MagicMock()
        store.backend = MagicMock()
        type(store.backend).__name__ = "QdrantBackend"
        checks = collect_startup_health(vector_store=store)
        assert checks == []

    def test_qdrant_fallback_to_sqlite(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When Qdrant was requested but SQLite is used, report degraded."""
        monkeypatch.setenv("OBSCURA_VECTOR_BACKEND", "qdrant")
        store = MagicMock()
        store.backend = MagicMock()
        type(store.backend).__name__ = "SQLiteBackend"
        checks = collect_startup_health(vector_store=store)
        assert len(checks) == 1
        assert checks[0].status == "degraded"
        assert "SQLite fallback" in checks[0].message

    def test_sqlite_requested_not_reported(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When SQLite is explicitly requested, no degradation is reported."""
        monkeypatch.setenv("OBSCURA_VECTOR_BACKEND", "sqlite")
        store = MagicMock()
        store.backend = MagicMock()
        type(store.backend).__name__ = "SQLiteBackend"
        checks = collect_startup_health(vector_store=store)
        assert checks == []

    def test_vector_store_none_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When vector memory is intentionally disabled, no check returned."""
        monkeypatch.setenv("OBSCURA_VECTOR_MEMORY", "off")
        checks = collect_startup_health(vector_store=None)
        assert checks == []

    def test_vector_store_none_failed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When vector memory fails to init (not disabled), report unavailable."""
        monkeypatch.delenv("OBSCURA_VECTOR_MEMORY", raising=False)
        checks = collect_startup_health(vector_store=None)
        assert len(checks) == 1
        assert checks[0].status == "unavailable"
        assert "failed to initialize" in checks[0].message


class TestCheckSkippedTools:
    def test_no_skipped_tools(self) -> None:
        checks = collect_startup_health(vector_store=MagicMock(), skipped_tools=[])
        assert checks == []

    def test_grouped_by_provider(self) -> None:
        """Skipped tools from the same provider are grouped into one check."""
        skipped = [
            ("msgraph.mail.list", "obscura.tools.providers.msgraph:MSGraphProvider"),
            ("msgraph.mail.send", "obscura.tools.providers.msgraph:MSGraphProvider"),
            (
                "msgraph.calendar.events.list",
                "obscura.tools.providers.msgraph:MSGraphProvider",
            ),
        ]
        store = MagicMock()
        store.backend = MagicMock()
        type(store.backend).__name__ = "QdrantBackend"
        checks = collect_startup_health(vector_store=store, skipped_tools=skipped)
        assert len(checks) == 1
        assert checks[0].status == "degraded"
        assert "3 msgraph tools skipped" in checks[0].message

    def test_multiple_providers(self) -> None:
        """Different providers produce separate health checks."""
        skipped = [
            ("msgraph.mail.list", "obscura.tools.providers.msgraph:MSGraphProvider"),
            ("gws.mail.list", "obscura.tools.providers.gws:GWSProvider"),
        ]
        store = MagicMock()
        store.backend = MagicMock()
        type(store.backend).__name__ = "QdrantBackend"
        checks = collect_startup_health(vector_store=store, skipped_tools=skipped)
        assert len(checks) == 2
        providers = {c.name for c in checks}
        assert "tools:msgraph" in providers
        assert "tools:gws" in providers

    def test_singular_tool_word(self) -> None:
        """Single skipped tool uses singular 'tool'."""
        skipped = [
            ("msgraph.mail.list", "obscura.tools.providers.msgraph:MSGraphProvider"),
        ]
        store = MagicMock()
        store.backend = MagicMock()
        type(store.backend).__name__ = "QdrantBackend"
        checks = collect_startup_health(vector_store=store, skipped_tools=skipped)
        assert "1 msgraph tool skipped" in checks[0].message


class TestCollectStartupHealth:
    def test_empty_when_healthy(self) -> None:
        """Healthy system returns no checks."""
        store = MagicMock()
        store.backend = MagicMock()
        type(store.backend).__name__ = "QdrantBackend"
        checks = collect_startup_health(vector_store=store, skipped_tools=[])
        assert checks == []

    def test_combines_vector_and_tools(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Both vector memory and tool issues are reported together."""
        monkeypatch.setenv("OBSCURA_VECTOR_BACKEND", "qdrant")
        store = MagicMock()
        store.backend = MagicMock()
        type(store.backend).__name__ = "SQLiteBackend"
        skipped = [
            ("msgraph.mail.list", "obscura.tools.providers.msgraph:MSGraphProvider"),
        ]
        checks = collect_startup_health(vector_store=store, skipped_tools=skipped)
        assert len(checks) == 2
        assert checks[0].name == "vector_memory"
        assert checks[1].name == "tools:msgraph"
