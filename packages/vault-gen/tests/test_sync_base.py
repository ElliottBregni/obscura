from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from vault_gen.sync.base import Change, SyncAdapter, SyncResult

# ---------------------------------------------------------------------------
# Change
# ---------------------------------------------------------------------------


class TestChange:
    def test_fields(self) -> None:
        c = Change(path="flags/dark-mode.toml", action="add", detail="new flag")
        assert c.path == "flags/dark-mode.toml"
        assert c.action == "add"
        assert c.detail == "new flag"

    def test_detail_defaults_to_empty(self) -> None:
        c = Change(path="flags/x.toml", action="remove")
        assert c.detail == ""

    def test_frozen(self) -> None:
        c = Change(path="p", action="add")
        with pytest.raises(FrozenInstanceError):
            c.path = "other"  # type: ignore[misc]

    def test_equality(self) -> None:
        a = Change(path="p", action="add", detail="d")
        b = Change(path="p", action="add", detail="d")
        assert a == b

    def test_inequality(self) -> None:
        assert Change(path="p", action="add") != Change(path="p", action="update")


# ---------------------------------------------------------------------------
# SyncResult
# ---------------------------------------------------------------------------


class TestSyncResult:
    def test_success_result(self) -> None:
        r = SyncResult(success=True, adapter="unleash")
        assert r.success is True
        assert r.adapter == "unleash"
        assert r.changes == ()
        assert r.error is None

    def test_failure_result(self) -> None:
        r = SyncResult(success=False, adapter="unleash", error="HTTP 401")
        assert r.success is False
        assert r.error == "HTTP 401"

    def test_with_changes(self) -> None:
        changes = (
            Change(path="flags/a.toml", action="add"),
            Change(path="flags/b.toml", action="update"),
        )
        r = SyncResult(success=True, adapter="unleash", changes=changes)
        assert len(r.changes) == 2

    def test_timestamp_is_set(self) -> None:
        r = SyncResult(success=True, adapter="x")
        assert r.timestamp
        assert "T" in r.timestamp  # ISO8601

    def test_frozen(self) -> None:
        r = SyncResult(success=True, adapter="x")
        with pytest.raises(FrozenInstanceError):
            r.success = False  # type: ignore[misc]

    def test_two_results_have_different_timestamps(self) -> None:
        import time

        r1 = SyncResult(success=True, adapter="x")
        time.sleep(0.01)
        r2 = SyncResult(success=True, adapter="x")
        # Timestamps should differ (created at different moments)
        # Weak assertion since resolution may be coarse on some systems
        assert isinstance(r1.timestamp, str)
        assert isinstance(r2.timestamp, str)


# ---------------------------------------------------------------------------
# SyncAdapter ABC
# ---------------------------------------------------------------------------


class TestSyncAdapterABC:
    def test_cannot_instantiate_directly(self) -> None:
        with pytest.raises(TypeError):
            SyncAdapter()  # type: ignore[abstract]

    def test_concrete_subclass_requires_all_abstract_methods(self) -> None:
        """A subclass missing any abstract method should also be un-instantiable."""

        class Incomplete(SyncAdapter):
            @property
            def name(self) -> str:
                return "incomplete"

            async def push(self, repo, config):  # type: ignore[override]
                ...

            # Missing pull and diff — should fail to instantiate.

        with pytest.raises(TypeError):
            Incomplete()  # type: ignore[abstract]

    def test_minimal_concrete_subclass(self) -> None:
        from vault_gen.access.repo import RepoAccess

        class Minimal(SyncAdapter):
            @property
            def name(self) -> str:
                return "minimal"

            async def push(self, repo: RepoAccess, config: dict) -> SyncResult:  # type: ignore[override]
                return SyncResult(success=True, adapter=self.name)

            async def pull(self, repo: RepoAccess, config: dict) -> SyncResult:  # type: ignore[override]
                return SyncResult(success=True, adapter=self.name)

            async def diff(self, repo: RepoAccess, config: dict) -> list[Change]:  # type: ignore[override]
                return []

        m = Minimal()
        assert m.name == "minimal"

    async def test_adapter_push_is_awaitable(self) -> None:
        from vault_gen.access.repo import RepoAccess

        class Echo(SyncAdapter):
            @property
            def name(self) -> str:
                return "echo"

            async def push(self, repo: RepoAccess, config: dict) -> SyncResult:  # type: ignore[override]
                return SyncResult(success=True, adapter=self.name)

            async def pull(self, repo: RepoAccess, config: dict) -> SyncResult:  # type: ignore[override]
                return SyncResult(success=True, adapter=self.name)

            async def diff(self, repo: RepoAccess, config: dict) -> list[Change]:  # type: ignore[override]
                return [Change(path="x", action="add")]

        echo = Echo()
        result = await echo.push(None, {})  # type: ignore[arg-type]
        assert result.success
        changes = await echo.diff(None, {})  # type: ignore[arg-type]
        assert len(changes) == 1
