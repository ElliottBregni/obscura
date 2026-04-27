"""Tests for ObscuraConfig layering: env > settings.json > defaults."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from obscura.core.config import (
    ObscuraConfig,
    _read_settings_runtime,
)

if TYPE_CHECKING:
    from pathlib import Path


def _write_settings(home: Path, runtime: dict | None, hooks: dict | None = None) -> None:
    home.mkdir(parents=True, exist_ok=True)
    payload: dict = {}
    if runtime is not None:
        payload["runtime"] = runtime
    if hooks is not None:
        payload["hooks"] = hooks
    (home / "settings.json").write_text(json.dumps(payload), encoding="utf-8")


class TestReadSettingsRuntime:
    def test_returns_empty_when_file_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OBSCURA_HOME", str(tmp_path))
        assert _read_settings_runtime() == {}

    def test_reads_runtime_section(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OBSCURA_HOME", str(tmp_path))
        _write_settings(tmp_path, {"log_level": "DEBUG", "rate_limit_rpm": 200})
        assert _read_settings_runtime() == {
            "log_level": "DEBUG",
            "rate_limit_rpm": 200,
        }

    def test_drops_unknown_keys(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OBSCURA_HOME", str(tmp_path))
        _write_settings(tmp_path, {"log_level": "INFO", "fake_field": "x"})
        assert _read_settings_runtime() == {"log_level": "INFO"}

    def test_drops_secret_keys(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Secrets must never load from settings.json."""
        monkeypatch.setenv("OBSCURA_HOME", str(tmp_path))
        _write_settings(
            tmp_path,
            {
                "supabase_jwt_secret": "leaked-via-settings",
                "capability_secret": "leaked-via-settings",
                "log_level": "WARN",
            },
        )
        result = _read_settings_runtime()
        assert "supabase_jwt_secret" not in result
        assert "capability_secret" not in result
        assert result == {"log_level": "WARN"}

    def test_malformed_json_returns_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OBSCURA_HOME", str(tmp_path))
        (tmp_path / "settings.json").write_text("not json{{{", encoding="utf-8")
        assert _read_settings_runtime() == {}

    def test_no_runtime_section_returns_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Hooks-only settings.json shouldn't break runtime loading."""
        monkeypatch.setenv("OBSCURA_HOME", str(tmp_path))
        _write_settings(tmp_path, runtime=None, hooks={"preToolUse": []})
        assert _read_settings_runtime() == {}


class TestObscuraConfigLoad:
    def test_load_with_no_settings_uses_defaults(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OBSCURA_HOME", str(tmp_path))
        # Clear runtime env vars so we see settings/default behavior
        for var in ("OBSCURA_LOG_LEVEL", "OBSCURA_RATE_LIMIT_RPM", "OBSCURA_HOST"):
            monkeypatch.delenv(var, raising=False)
        cfg = ObscuraConfig.load()
        assert cfg.log_level == "INFO"
        assert cfg.rate_limit_rpm == 100
        assert cfg.host == "0.0.0.0"

    def test_settings_json_overrides_defaults(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OBSCURA_HOME", str(tmp_path))
        for var in ("OBSCURA_LOG_LEVEL", "OBSCURA_RATE_LIMIT_RPM", "OBSCURA_HOST"):
            monkeypatch.delenv(var, raising=False)
        _write_settings(
            tmp_path,
            {"log_level": "DEBUG", "rate_limit_rpm": 250, "host": "127.0.0.1"},
        )
        cfg = ObscuraConfig.load()
        assert cfg.log_level == "DEBUG"
        assert cfg.rate_limit_rpm == 250
        assert cfg.host == "127.0.0.1"

    def test_env_overrides_settings_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OBSCURA_HOME", str(tmp_path))
        _write_settings(tmp_path, {"log_level": "DEBUG", "rate_limit_rpm": 250})
        monkeypatch.setenv("OBSCURA_LOG_LEVEL", "ERROR")
        monkeypatch.setenv("OBSCURA_RATE_LIMIT_RPM", "500")
        cfg = ObscuraConfig.load()
        assert cfg.log_level == "ERROR"
        assert cfg.rate_limit_rpm == 500

    def test_kairos_optout_settings_json_disable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """settings.json can flip an opt-out bool to false."""
        monkeypatch.setenv("OBSCURA_HOME", str(tmp_path))
        for var in ("OBSCURA_KAIROS", "OBSCURA_KAIROS_PROACTIVE"):
            monkeypatch.delenv(var, raising=False)
        _write_settings(tmp_path, {"kairos_enabled": False, "kairos_proactive": False})
        cfg = ObscuraConfig.load()
        assert cfg.kairos_enabled is False
        assert cfg.kairos_proactive is False

    def test_cache_optin_settings_json_enable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """settings.json can flip an opt-in bool to true."""
        monkeypatch.setenv("OBSCURA_HOME", str(tmp_path))
        monkeypatch.delenv("OBSCURA_CACHE_ENABLED", raising=False)
        _write_settings(tmp_path, {"cache_enabled": True})
        cfg = ObscuraConfig.load()
        assert cfg.cache_enabled is True

    def test_secrets_never_from_settings_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Even if settings.json contains secret-shaped keys, they are ignored."""
        monkeypatch.setenv("OBSCURA_HOME", str(tmp_path))
        for var in (
            "SUPABASE_JWT_SECRET",
            "OBSCURA_CAPABILITY_SECRET",
        ):
            monkeypatch.delenv(var, raising=False)
        _write_settings(
            tmp_path,
            {
                "supabase_jwt_secret": "should-be-ignored",
                "capability_secret": "should-be-ignored",
            },
        )
        cfg = ObscuraConfig.load()
        assert cfg.supabase_jwt_secret == ""
        assert cfg.capability_secret == ""
