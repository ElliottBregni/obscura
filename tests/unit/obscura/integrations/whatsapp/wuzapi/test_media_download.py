"""Tests for inbound image download: adapter metadata extraction +
service-side download/save.

Two layers:
* ``_extract_downloadable_media`` (adapter) — pulls wuzapi-shaped
  download metadata from a whatsmeow Message payload, or returns None.
* ``_download_and_save_media`` (service) — calls wuzapi to fetch
  bytes, saves to ``~/.obscura/whatsapp_inbound/``, returns the path.

The integration: an inbound image message has its bytes downloaded and
saved, the agent gets a prompt like ``[image at /path/to/file.jpg]
caption: ...`` and can pick it up with file-read or vision tools.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from obscura.integrations.whatsapp.wuzapi.adapter import (
    _extract_downloadable_media,
)
from obscura.integrations.whatsapp.wuzapi.service import (
    _download_and_save_media,
    _mimetype_to_extension,
    _sanitize_filename_stem,
)


# ---------------------------------------------------------------------------
# Adapter: _extract_downloadable_media
# ---------------------------------------------------------------------------


def test_extract_image_payload_full_fields() -> None:
    """All download metadata fields propagate through."""
    msg: dict[str, Any] = {
        "imageMessage": {
            "url": "https://mmg.whatsapp.net/v/t62.7118-24/abc",
            "mimetype": "image/jpeg",
            "directPath": "/v/t62.7118-24/abc",
            "mediaKey": "BASE64KEY==",
            "fileEncSha256": "ENC256==",
            "fileSha256": "PLAIN256==",
            "fileLength": 12345,
        },
    }
    payload = _extract_downloadable_media(msg)
    assert payload is not None
    assert payload["kind"] == "image"
    assert payload["url"] == "https://mmg.whatsapp.net/v/t62.7118-24/abc"
    assert payload["mimetype"] == "image/jpeg"
    assert payload["direct_path"] == "/v/t62.7118-24/abc"
    assert payload["media_key"] == "BASE64KEY=="
    assert payload["file_enc_sha256"] == "ENC256=="
    assert payload["file_sha256"] == "PLAIN256=="
    assert payload["file_length"] == 12345


def test_extract_image_payload_missing_url_returns_none() -> None:
    """Without a URL, we can't download, so no payload is emitted."""
    msg: dict[str, Any] = {"imageMessage": {"mimetype": "image/jpeg"}}
    assert _extract_downloadable_media(msg) is None


def test_extract_image_payload_file_length_string() -> None:
    """fileLength can arrive as a string in some webhook payloads."""
    msg: dict[str, Any] = {
        "imageMessage": {"url": "https://example.com/x", "fileLength": "9999"},
    }
    payload = _extract_downloadable_media(msg)
    assert payload is not None
    assert payload["file_length"] == 9999


def test_extract_image_payload_text_message_returns_none() -> None:
    """Plain text messages have no media — no payload."""
    assert _extract_downloadable_media({"conversation": "hi"}) is None


def test_extract_image_payload_ephemeral_wrapper() -> None:
    """Disappearing image messages: the imageMessage lives under
    ephemeralMessage.message — should still be found."""
    msg: dict[str, Any] = {
        "ephemeralMessage": {
            "message": {
                "imageMessage": {
                    "url": "https://example.com/secret",
                    "mimetype": "image/png",
                },
            },
        },
    }
    payload = _extract_downloadable_media(msg)
    assert payload is not None
    assert payload["url"] == "https://example.com/secret"
    assert payload["mimetype"] == "image/png"


def test_extract_image_payload_view_once_wrapper() -> None:
    """viewOnceMessageV2 wrapping is handled too."""
    msg: dict[str, Any] = {
        "viewOnceMessageV2": {
            "message": {
                "imageMessage": {"url": "https://example.com/oneoff"},
            },
        },
    }
    payload = _extract_downloadable_media(msg)
    assert payload is not None
    assert payload["url"] == "https://example.com/oneoff"


# ---------------------------------------------------------------------------
# Service helpers: _mimetype_to_extension, _sanitize_filename_stem
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mimetype,expected",
    [
        ("image/jpeg", ".jpg"),
        ("image/jpg", ".jpg"),
        ("image/png", ".png"),
        ("image/webp", ".webp"),
        ("image/gif", ".gif"),
        ("", ".bin"),
        ("application/octet-stream", ".bin"),
    ],
)
def test_mimetype_to_extension(mimetype: str, expected: str) -> None:
    assert _mimetype_to_extension(mimetype) == expected


def test_sanitize_filename_stem_keeps_safe_chars() -> None:
    assert _sanitize_filename_stem("3EB0_abc-DEF123") == "3EB0_abc-DEF123"


def test_sanitize_filename_stem_strips_unsafe() -> None:
    assert _sanitize_filename_stem("foo/bar.baz?qux=1") == "foobarbazqux1"


def test_sanitize_filename_stem_empty_falls_back() -> None:
    assert _sanitize_filename_stem("") == "image"
    assert _sanitize_filename_stem("///") == "image"


# ---------------------------------------------------------------------------
# Service: _download_and_save_media
# ---------------------------------------------------------------------------


@pytest.fixture
def _obscura_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point resolve_obscura_home at a temp dir so file writes are isolated."""
    monkeypatch.setattr(
        "obscura.core.paths.resolve_obscura_home",
        lambda: tmp_path,
    )
    return tmp_path


@pytest.mark.unit
@pytest.mark.asyncio
async def test_download_and_save_writes_bytes(_obscura_home: Path) -> None:
    """Happy path: wuzapi returns bytes, helper writes them to disk and
    returns the path."""
    fake_bytes = b"\xff\xd8\xff\xe0fake jpeg bytes"
    client: Any = MagicMock()
    client.download_image = AsyncMock(return_value=fake_bytes)

    payload = {
        "kind": "image",
        "url": "https://example.com/x",
        "direct_path": "/v/t/x",
        "media_key": "KEY",
        "mimetype": "image/jpeg",
        "file_enc_sha256": "ENC",
        "file_sha256": "PLN",
        "file_length": 100,
    }
    saved = await _download_and_save_media(client, payload, "3EB0abc123")
    assert saved is not None
    assert Path(saved).read_bytes() == fake_bytes
    assert saved.endswith(".jpg")
    assert "whatsapp_inbound" in saved
    assert "3EB0abc123" in saved


@pytest.mark.unit
@pytest.mark.asyncio
async def test_download_and_save_returns_none_on_download_error(
    _obscura_home: Path,
) -> None:
    """Download failure (wuzapi error, network blip, etc) returns None
    so the caller can fall back to the synthesized text marker."""
    client: Any = MagicMock()
    client.download_image = AsyncMock(side_effect=RuntimeError("wuzapi down"))

    payload = {
        "kind": "image",
        "url": "https://example.com/x",
        "mimetype": "image/jpeg",
    }
    saved = await _download_and_save_media(client, payload, "abc123")
    assert saved is None
    # No file should have been created
    inbound = _obscura_home / "whatsapp_inbound"
    if inbound.exists():
        assert list(inbound.iterdir()) == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_download_and_save_returns_none_for_non_image_kind(
    _obscura_home: Path,
) -> None:
    """Only 'image' kind is plumbed today; doc/video/audio return None
    rather than mis-routing through the image endpoint."""
    client: Any = MagicMock()
    client.download_image = AsyncMock()  # should not be called

    payload = {"kind": "document", "url": "https://example.com/x"}
    saved = await _download_and_save_media(client, payload, "abc123")
    assert saved is None
    client.download_image.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_download_and_save_picks_extension_from_mimetype(
    _obscura_home: Path,
) -> None:
    """PNG mimetype → .png file. Verifies the helper plumbs mimetype
    through to extension selection."""
    client: Any = MagicMock()
    client.download_image = AsyncMock(return_value=b"PNG\x00\x00\x00data")

    payload = {
        "kind": "image",
        "url": "https://example.com/x",
        "mimetype": "image/png",
    }
    saved = await _download_and_save_media(client, payload, "xyz")
    assert saved is not None
    assert saved.endswith(".png")
