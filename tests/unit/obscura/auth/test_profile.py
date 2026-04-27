"""Tests for obscura.auth.profile -- Supabase user_metadata.obscura_profile."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from obscura.auth import profile as profile_module


@pytest.fixture(autouse=True)
def _reset_singleton() -> Any:
    profile_module.reset()
    yield
    profile_module.reset()


@pytest.fixture(autouse=True)
def _isolate_machine_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Redirect machine.id to a tmp path so tests don't touch real ~/.obscura."""
    monkeypatch.setenv("OBSCURA_HOME", str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------------------
# Machine ID
# ---------------------------------------------------------------------------


class TestMachineId:
    def test_generates_on_first_call(self, _isolate_machine_id: Path) -> None:
        first = profile_module.get_or_create_machine_id()
        assert first
        # File now exists and contains the same id
        assert (_isolate_machine_id / "machine.id").read_text().strip() == first

    def test_stable_across_calls(self, _isolate_machine_id: Path) -> None:
        a = profile_module.get_or_create_machine_id()
        b = profile_module.get_or_create_machine_id()
        assert a == b

    def test_regenerates_when_file_empty(
        self,
        _isolate_machine_id: Path,
    ) -> None:
        path = _isolate_machine_id / "machine.id"
        path.write_text("   ")  # empty after strip

        new_id = profile_module.get_or_create_machine_id()

        assert new_id
        assert path.read_text().strip() == new_id

    def test_current_device_info_populates_fields(
        self,
        _isolate_machine_id: Path,
    ) -> None:
        info = profile_module.current_device_info()

        assert info.id == profile_module.get_or_create_machine_id()
        assert info.name  # defaults to hostname
        assert info.platform  # e.g. "darwin", "linux", "windows"
        assert info.first_seen == info.last_seen  # fresh entry

    def test_current_device_info_honours_name_override(
        self,
        _isolate_machine_id: Path,
    ) -> None:
        info = profile_module.current_device_info(name="my-laptop")

        assert info.name == "my-laptop"


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class TestModels:
    def test_profile_defaults(self) -> None:
        p = profile_module.ObscuraProfile()
        assert p.display_name is None
        assert p.feature_flags == []
        assert p.devices == []

    def test_profile_forbids_extra_fields(self) -> None:
        with pytest.raises(Exception):
            profile_module.ObscuraProfile.model_validate(
                {"display_name": "x", "bogus": "y"},
            )

    def test_device_info_roundtrip(self) -> None:
        dev = profile_module.DeviceInfo(
            id="uuid-1",
            name="laptop",
            platform="darwin",
            hostname="host",
            first_seen="2026-04-24T00:00:00Z",
            last_seen="2026-04-24T01:00:00Z",
        )
        parsed = profile_module.DeviceInfo.model_validate(dev.model_dump())
        assert parsed == dev


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class TestProfileClient:
    def _make_client(self) -> profile_module.ProfileClient:
        return profile_module.ProfileClient(
            api_url="https://sb.test",
            anon_key="anon-key",
            get_access_token=lambda: "session-tok",
        )

    @respx.mock
    def test_load_parses_existing_profile(self) -> None:
        respx.get("https://sb.test/auth/v1/user").mock(
            return_value=httpx.Response(
                200,
                json={
                    "email": "me@example.com",
                    "user_metadata": {
                        "obscura_profile": {
                            "display_name": "Elliott",
                            "default_backend": "claude",
                            "feature_flags": ["voice"],
                            "devices": [],
                        },
                    },
                },
            ),
        )
        profile = self._make_client().load()
        assert profile.display_name == "Elliott"
        assert profile.default_backend == "claude"
        assert profile.feature_flags == ["voice"]

    @respx.mock
    def test_load_returns_empty_when_profile_missing(self) -> None:
        respx.get("https://sb.test/auth/v1/user").mock(
            return_value=httpx.Response(200, json={"user_metadata": {}}),
        )
        profile = self._make_client().load()
        assert profile.display_name is None

    @respx.mock
    def test_load_falls_back_on_corrupt_profile(self) -> None:
        respx.get("https://sb.test/auth/v1/user").mock(
            return_value=httpx.Response(
                200,
                json={
                    "user_metadata": {
                        "obscura_profile": {
                            "display_name": "x",
                            "unknown_field": "boom",
                        },
                    },
                },
            ),
        )
        profile = self._make_client().load()
        # Corrupt profile → empty default, not a crash
        assert profile.display_name is None

    @respx.mock
    def test_update_merges_fields(self) -> None:
        respx.get("https://sb.test/auth/v1/user").mock(
            return_value=httpx.Response(
                200,
                json={
                    "user_metadata": {
                        "obscura_profile": {
                            "display_name": "Old",
                            "default_backend": "copilot",
                        },
                    },
                },
            ),
        )
        put = respx.put("https://sb.test/auth/v1/user").mock(
            return_value=httpx.Response(200, json={}),
        )

        updated = self._make_client().update(display_name="New")

        assert updated.display_name == "New"
        assert updated.default_backend == "copilot"  # preserved
        assert put.call_count == 1
        import json as _json

        body = _json.loads(put.calls[0].request.read())
        assert body["data"]["obscura_profile"]["display_name"] == "New"

    @respx.mock
    def test_update_rejects_unknown_fields(self) -> None:
        respx.get("https://sb.test/auth/v1/user").mock(
            return_value=httpx.Response(200, json={"user_metadata": {}}),
        )
        with pytest.raises(Exception):
            self._make_client().update(bogus="value")  # type: ignore[arg-type]

    @respx.mock
    def test_register_device_adds_current_machine(
        self,
        _isolate_machine_id: Path,
    ) -> None:
        respx.get("https://sb.test/auth/v1/user").mock(
            return_value=httpx.Response(200, json={"user_metadata": {}}),
        )
        put = respx.put("https://sb.test/auth/v1/user").mock(
            return_value=httpx.Response(200, json={}),
        )

        client = self._make_client()
        entry = client.register_device(name="test-machine")

        assert entry.name == "test-machine"
        assert entry.id == profile_module.get_or_create_machine_id()
        assert put.call_count == 1

        import json as _json

        body = _json.loads(put.calls[0].request.read())
        devices = body["data"]["obscura_profile"]["devices"]
        assert len(devices) == 1
        assert devices[0]["id"] == entry.id

    @respx.mock
    def test_register_device_is_idempotent(
        self,
        _isolate_machine_id: Path,
    ) -> None:
        """Registering twice updates last_seen, doesn't duplicate."""
        machine_id = profile_module.get_or_create_machine_id()
        existing = profile_module.DeviceInfo(
            id=machine_id,
            name="old-name",
            platform="linux",
            hostname="old-host",
            first_seen="2026-04-24T00:00:00+00:00",
            last_seen="2026-04-24T00:00:00+00:00",
        )
        respx.get("https://sb.test/auth/v1/user").mock(
            return_value=httpx.Response(
                200,
                json={
                    "user_metadata": {
                        "obscura_profile": {
                            "devices": [existing.model_dump(mode="json")],
                        },
                    },
                },
            ),
        )
        put = respx.put("https://sb.test/auth/v1/user").mock(
            return_value=httpx.Response(200, json={}),
        )

        client = self._make_client()
        # Register again without explicit name -- should keep old_name.
        refreshed = client.register_device()

        assert refreshed.id == machine_id
        # last_seen advanced
        assert refreshed.last_seen >= existing.last_seen

        import json as _json

        body = _json.loads(put.calls[0].request.read())
        devices = body["data"]["obscura_profile"]["devices"]
        assert len(devices) == 1, "should not duplicate entries for same machine"

    @respx.mock
    def test_remove_device_drops_entry(
        self,
        _isolate_machine_id: Path,
    ) -> None:
        target = profile_module.DeviceInfo(
            id="delete-me",
            name="stale",
            platform="linux",
            hostname="old",
            first_seen="2026-04-24T00:00:00+00:00",
            last_seen="2026-04-24T00:00:00+00:00",
        )
        keep = profile_module.DeviceInfo(
            id="keep-me",
            name="current",
            platform="darwin",
            hostname="new",
            first_seen="2026-04-24T00:00:00+00:00",
            last_seen="2026-04-24T00:00:00+00:00",
        )
        respx.get("https://sb.test/auth/v1/user").mock(
            return_value=httpx.Response(
                200,
                json={
                    "user_metadata": {
                        "obscura_profile": {
                            "devices": [
                                target.model_dump(mode="json"),
                                keep.model_dump(mode="json"),
                            ],
                        },
                    },
                },
            ),
        )
        put = respx.put("https://sb.test/auth/v1/user").mock(
            return_value=httpx.Response(200, json={}),
        )

        client = self._make_client()
        removed = client.remove_device("delete-me")

        assert removed is True

        import json as _json

        body = _json.loads(put.calls[0].request.read())
        devices = body["data"]["obscura_profile"]["devices"]
        assert len(devices) == 1
        assert devices[0]["id"] == "keep-me"

    @respx.mock
    def test_rename_device_refuses_empty(
        self,
        _isolate_machine_id: Path,
    ) -> None:
        client = self._make_client()
        with pytest.raises(profile_module.ProfileError, match="empty"):
            client.rename_device("   ")


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


class TestSingleton:
    def test_returns_none_when_supabase_not_configured(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from obscura.auth import secrets as _secrets

        for var in ("SUPABASE_URL", "SUPABASE_ANON_KEY"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setattr(_secrets, "_shell_env_snapshot", {})
        monkeypatch.setattr(_secrets, "_dotenv_loaded", True)
        monkeypatch.setattr(_secrets, "keyring_available", lambda: False)

        assert profile_module.get_client() is None
