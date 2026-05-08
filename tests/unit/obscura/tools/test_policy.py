"""Unit tests for obscura.tools.system._policy.Policy.

All methods are pure (no I/O). Test strategy:
  - env vars manipulated via monkeypatch
  - filesystem checks use tmp_path
  - SSRF guard uses OBSCURA_ALLOW_PRIVATE_URLS to bypass DNS lookups in CI
"""

from __future__ import annotations

from pathlib import Path

import pytest

from obscura.tools.system._policy import Policy

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# env_flag
# ---------------------------------------------------------------------------


def test_env_flag_missing_returns_default_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MY_FLAG", raising=False)
    assert Policy.env_flag("MY_FLAG") is False


def test_env_flag_missing_returns_custom_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MY_FLAG", raising=False)
    assert Policy.env_flag("MY_FLAG", default=True) is True


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "YES", "on", "ON"])
def test_env_flag_truthy_values(monkeypatch: pytest.MonkeyPatch, val: str) -> None:
    monkeypatch.setenv("MY_FLAG", val)
    assert Policy.env_flag("MY_FLAG") is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", ""])
def test_env_flag_falsy_values(monkeypatch: pytest.MonkeyPatch, val: str) -> None:
    monkeypatch.setenv("MY_FLAG", val)
    assert Policy.env_flag("MY_FLAG") is False


# ---------------------------------------------------------------------------
# normalize_list
# ---------------------------------------------------------------------------


def test_normalize_list_splits_by_comma() -> None:
    result = Policy.normalize_list("a, b, c")
    assert result == {"a", "b", "c"}


def test_normalize_list_filters_empty_segments() -> None:
    result = Policy.normalize_list(",, a ,, b,,")
    assert result == {"a", "b"}


def test_normalize_list_empty_string_returns_empty_set() -> None:
    assert Policy.normalize_list("") == set()


# ---------------------------------------------------------------------------
# string_key_dict
# ---------------------------------------------------------------------------


def test_string_key_dict_converts_int_keys() -> None:
    result = Policy.string_key_dict({1: "a", 2: "b"})
    assert result == {"1": "a", "2": "b"}


def test_string_key_dict_non_dict_returns_none() -> None:
    assert Policy.string_key_dict(["a", "b"]) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# resolve_base_dir
# ---------------------------------------------------------------------------


def test_resolve_base_dir_no_env_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OBSCURA_SYSTEM_TOOLS_BASE_DIR", raising=False)
    assert Policy.resolve_base_dir() is None


def test_resolve_base_dir_with_env_returns_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OBSCURA_SYSTEM_TOOLS_BASE_DIR", str(tmp_path))
    result = Policy.resolve_base_dir()
    assert result == tmp_path


# ---------------------------------------------------------------------------
# is_cwd_allowed
# ---------------------------------------------------------------------------


def test_is_cwd_allowed_no_base_dir_always_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OBSCURA_SYSTEM_TOOLS_BASE_DIR", raising=False)
    assert Policy.is_cwd_allowed("/any/path") is True


def test_is_cwd_allowed_empty_cwd_is_true(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OBSCURA_SYSTEM_TOOLS_BASE_DIR", str(tmp_path))
    assert Policy.is_cwd_allowed("") is True


def test_is_cwd_allowed_inside_base_true(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OBSCURA_SYSTEM_TOOLS_BASE_DIR", str(tmp_path))
    subdir = tmp_path / "project"
    subdir.mkdir()
    assert Policy.is_cwd_allowed(str(subdir)) is True


def test_is_cwd_allowed_outside_base_false(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OBSCURA_SYSTEM_TOOLS_BASE_DIR", str(tmp_path / "sandbox"))
    assert Policy.is_cwd_allowed("/usr/local/bin") is False


# ---------------------------------------------------------------------------
# resolve_path
# ---------------------------------------------------------------------------


def test_resolve_path_absolute_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OBSCURA_TOOLS_RELATIVE_TO_CWD", raising=False)
    result = Policy.resolve_path("/tmp/foo.txt")
    # /tmp may be a symlink on macOS (/private/tmp); compare resolved paths
    assert result == Path("/tmp/foo.txt").resolve()


def test_resolve_path_relative_to_cwd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OBSCURA_TOOLS_RELATIVE_TO_CWD", "1")
    monkeypatch.chdir(tmp_path)
    result = Policy.resolve_path("foo.txt")
    assert result == (tmp_path / "foo.txt").resolve()


# ---------------------------------------------------------------------------
# is_path_allowed
# ---------------------------------------------------------------------------


def test_is_path_allowed_no_base_always_true(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("OBSCURA_SYSTEM_TOOLS_BASE_DIR", raising=False)
    assert Policy.is_path_allowed(tmp_path / "anywhere.txt") is True


def test_is_path_allowed_inside_base_true(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OBSCURA_SYSTEM_TOOLS_BASE_DIR", str(tmp_path))
    assert Policy.is_path_allowed(tmp_path / "file.txt") is True


def test_is_path_allowed_outside_base_false(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OBSCURA_SYSTEM_TOOLS_BASE_DIR", str(tmp_path / "sandbox"))
    assert Policy.is_path_allowed(Path("/etc/passwd")) is False


def test_is_path_allowed_runtime_dir_overrides_base(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    extra = tmp_path / "extra"
    extra.mkdir()

    monkeypatch.setenv("OBSCURA_SYSTEM_TOOLS_BASE_DIR", str(sandbox))
    # Register extra dir — save and restore runtime_allowed_dirs between tests
    original = Policy.runtime_allowed_dirs.copy()
    try:
        Policy.add_allowed_dir(str(extra))
        assert Policy.is_path_allowed(extra / "ok.txt") is True
    finally:
        Policy.runtime_allowed_dirs[:] = original


# ---------------------------------------------------------------------------
# validate_url — scheme guard (no DNS needed)
# ---------------------------------------------------------------------------


def test_validate_url_allows_private_bypass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OBSCURA_ALLOW_PRIVATE_URLS", "true")
    # Should return URL unchanged when bypass env var is set
    result = Policy.validate_url("http://127.0.0.1/admin")
    assert result == "http://127.0.0.1/admin"


def test_validate_url_rejects_ftp_scheme() -> None:
    with pytest.raises(ValueError, match="scheme"):
        Policy.validate_url("ftp://example.com/file")


def test_validate_url_rejects_no_hostname() -> None:
    with pytest.raises(ValueError):
        Policy.validate_url("https://")


# ---------------------------------------------------------------------------
# json_error
# ---------------------------------------------------------------------------


def test_json_error_basic_shape() -> None:
    import json

    result = json.loads(Policy.json_error("some_error"))
    assert result["ok"] is False
    assert result["error"] == "some_error"
    assert result["exit_code"] == -1


def test_json_error_path_not_allowed_adds_hint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import json

    monkeypatch.setenv("OBSCURA_SYSTEM_TOOLS_BASE_DIR", str(tmp_path))
    result = json.loads(Policy.json_error("path_not_allowed"))
    assert "hint" in result


def test_json_error_extra_kwargs_included() -> None:
    import json

    result = json.loads(Policy.json_error("foo", path="/some/path"))
    assert result["path"] == "/some/path"
