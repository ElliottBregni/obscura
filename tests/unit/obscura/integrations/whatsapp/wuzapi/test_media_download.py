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

from obscura.integrations.messaging.media_store import (
    mimetype_to_extension,
    sanitize_filename_stem,
    save_inbound_media,
)
from obscura.integrations.whatsapp.wuzapi.adapter import (
    _extract_downloadable_media,
)
from obscura.integrations.whatsapp.wuzapi.service import (
    _download_and_save_media,
)


# ---------------------------------------------------------------------------
# Adapter: _extract_downloadable_media
# ---------------------------------------------------------------------------


def test_extract_image_payload_full_fields() -> None:
    """All download metadata fields propagate through, plus the
    ``marker`` field so the service can rewrite the text marker."""
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
    assert payload["marker"] == "[image]"
    assert payload["url"] == "https://mmg.whatsapp.net/v/t62.7118-24/abc"
    assert payload["mimetype"] == "image/jpeg"
    assert payload["direct_path"] == "/v/t62.7118-24/abc"
    assert payload["media_key"] == "BASE64KEY=="
    assert payload["file_enc_sha256"] == "ENC256=="
    assert payload["file_sha256"] == "PLAIN256=="
    assert payload["file_length"] == 12345


def test_extract_video_payload() -> None:
    """Video variants use the same download wire shape with kind='video'."""
    msg: dict[str, Any] = {
        "videoMessage": {
            "url": "https://example.com/clip",
            "mimetype": "video/mp4",
            "fileLength": 5000000,
        },
    }
    payload = _extract_downloadable_media(msg)
    assert payload is not None
    assert payload["kind"] == "video"
    assert payload["marker"] == "[video]"
    assert payload["mimetype"] == "video/mp4"


def test_extract_document_payload() -> None:
    """Document variant resolves to kind='document'."""
    msg: dict[str, Any] = {
        "documentMessage": {
            "url": "https://example.com/doc",
            "mimetype": "application/pdf",
        },
    }
    payload = _extract_downloadable_media(msg)
    assert payload is not None
    assert payload["kind"] == "document"
    assert payload["marker"] == "[document]"
    assert payload["mimetype"] == "application/pdf"


def test_extract_audio_payload() -> None:
    """Voice notes (audioMessage) resolve to kind='audio' with the
    'voice note' marker the adapter put in the text."""
    msg: dict[str, Any] = {
        "audioMessage": {
            "url": "https://example.com/voice",
            "mimetype": "audio/ogg",
            "ptt": True,
        },
    }
    payload = _extract_downloadable_media(msg)
    assert payload is not None
    assert payload["kind"] == "audio"
    assert payload["marker"] == "[voice note]"


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
        # Images
        ("image/jpeg", ".jpg"),
        ("image/png", ".png"),
        ("image/webp", ".webp"),
        ("image/gif", ".gif"),
        ("image/heic", ".heic"),
        # Videos
        ("video/mp4", ".mp4"),
        ("video/quicktime", ".mov"),
        ("video/webm", ".webm"),
        # Audio
        ("audio/ogg", ".ogg"),
        ("audio/mpeg", ".mp3"),
        ("audio/mp4", ".m4a"),
        # Documents
        ("application/pdf", ".pdf"),
        ("application/msword", ".doc"),
        ("text/plain", ".txt"),
        ("text/csv", ".csv"),
        # Fallback + tolerance for charset suffixes
        ("", ".bin"),
        ("application/octet-stream", ".bin"),
        ("image/jpeg; charset=binary", ".jpg"),
        ("video/mp4;codecs=avc1.4d401e", ".mp4"),
    ],
)
def test_mimetype_to_extension(mimetype: str, expected: str) -> None:
    """Comprehensive coverage across all four media kinds + parameter
    tolerance for codec / charset suffixes."""
    assert mimetype_to_extension(mimetype) == expected


def test_sanitize_filename_stem_keeps_safe_chars() -> None:
    assert sanitize_filename_stem("3EB0_abc-DEF123") == "3EB0_abc-DEF123"


def test_sanitize_filename_stem_strips_unsafe() -> None:
    assert sanitize_filename_stem("foo/bar.baz?qux=1") == "foobarbazqux1"


def test_sanitize_filename_stem_empty_falls_back() -> None:
    """Fallback is 'media' (generic, since this helper is shared across
    image/video/document/audio)."""
    assert sanitize_filename_stem("") == "media"
    assert sanitize_filename_stem("///") == "media"


def test_sanitize_filename_stem_blocks_traversal() -> None:
    """Untrusted message IDs containing path separators or '..' can't
    escape the per-platform inbound directory."""
    assert ".." not in sanitize_filename_stem("../../etc/passwd")
    assert "/" not in sanitize_filename_stem("foo/bar/baz")


# ---------------------------------------------------------------------------
# Service: _download_and_save_media
# ---------------------------------------------------------------------------


@pytest.fixture
def _obscura_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point resolve_obscura_home at a temp dir so file writes are isolated.

    Patches at the importing module's namespace (media_store) since
    that's where ``from obscura.core.paths import resolve_obscura_home``
    bound the name.
    """
    monkeypatch.setattr(
        "obscura.integrations.messaging.media_store.resolve_obscura_home",
        lambda: tmp_path,
    )
    return tmp_path


def _stub_client_with_all_downloaders(image_bytes: bytes = b"") -> Any:
    """MagicMock with the four download_* methods as AsyncMocks.

    Reset / override per test to assert which one(s) got called.
    """
    client: Any = MagicMock()
    client.download_image = AsyncMock(return_value=image_bytes)
    client.download_video = AsyncMock(return_value=b"")
    client.download_document = AsyncMock(return_value=b"")
    client.download_audio = AsyncMock(return_value=b"")
    return client


@pytest.mark.unit
@pytest.mark.asyncio
async def test_download_and_save_image_writes_bytes(_obscura_home: Path) -> None:
    """Happy path: image dispatcher calls download_image, bytes land in
    the shared media_inbound/whatsapp/ subdir."""
    fake_bytes = b"\xff\xd8\xff\xe0fake jpeg bytes"
    client = _stub_client_with_all_downloaders(fake_bytes)

    payload = {
        "kind": "image",
        "marker": "[image]",
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
    # Lives under the shared media_inbound/<platform>/ root, not the
    # legacy whatsapp_inbound/ path.
    assert "media_inbound/whatsapp/" in saved
    assert "3EB0abc123" in saved
    client.download_image.assert_awaited_once()
    client.download_video.assert_not_awaited()
    client.download_document.assert_not_awaited()
    client.download_audio.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_download_and_save_video_dispatches_correctly(
    _obscura_home: Path,
) -> None:
    """Video kind → download_video; mp4 mimetype → .mp4 extension."""
    client = _stub_client_with_all_downloaders()
    client.download_video = AsyncMock(return_value=b"video bytes")

    payload = {
        "kind": "video",
        "marker": "[video]",
        "url": "https://example.com/v",
        "mimetype": "video/mp4",
    }
    saved = await _download_and_save_media(client, payload, "vid1")
    assert saved is not None
    assert saved.endswith(".mp4")
    client.download_video.assert_awaited_once()
    client.download_image.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_download_and_save_document_dispatches_correctly(
    _obscura_home: Path,
) -> None:
    """Document kind → download_document; PDF mimetype → .pdf extension."""
    client = _stub_client_with_all_downloaders()
    client.download_document = AsyncMock(return_value=b"%PDF-1.4 fake")

    payload = {
        "kind": "document",
        "marker": "[document]",
        "url": "https://example.com/d",
        "mimetype": "application/pdf",
    }
    saved = await _download_and_save_media(client, payload, "doc1")
    assert saved is not None
    assert saved.endswith(".pdf")
    client.download_document.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_download_and_save_audio_dispatches_correctly(
    _obscura_home: Path,
) -> None:
    """Audio kind → download_audio; ogg mimetype → .ogg extension."""
    client = _stub_client_with_all_downloaders()
    client.download_audio = AsyncMock(return_value=b"OggS audio")

    payload = {
        "kind": "audio",
        "marker": "[voice note]",
        "url": "https://example.com/a",
        "mimetype": "audio/ogg",
    }
    saved = await _download_and_save_media(client, payload, "aud1")
    assert saved is not None
    assert saved.endswith(".ogg")
    client.download_audio.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_download_and_save_returns_none_on_download_error(
    _obscura_home: Path,
) -> None:
    """Download failure (wuzapi error, network blip, etc) returns None
    so the caller can fall back to the synthesized text marker."""
    client = _stub_client_with_all_downloaders()
    client.download_image = AsyncMock(side_effect=RuntimeError("wuzapi down"))

    payload = {
        "kind": "image",
        "url": "https://example.com/x",
        "mimetype": "image/jpeg",
    }
    saved = await _download_and_save_media(client, payload, "abc123")
    assert saved is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_download_and_save_unknown_kind_returns_none(
    _obscura_home: Path,
) -> None:
    """Unknown kind returns None without calling any downloader — no
    mis-routing if a new variant slips through."""
    client = _stub_client_with_all_downloaders()

    payload = {"kind": "sticker", "url": "https://example.com/s"}
    saved = await _download_and_save_media(client, payload, "s1")
    assert saved is None
    client.download_image.assert_not_awaited()
    client.download_video.assert_not_awaited()
    client.download_document.assert_not_awaited()
    client.download_audio.assert_not_awaited()


# ---------------------------------------------------------------------------
# Shared save_inbound_media — usable by any messaging adapter
# ---------------------------------------------------------------------------


def test_save_inbound_media_creates_per_platform_subdir(_obscura_home: Path) -> None:
    """Each platform gets its own subdirectory under media_inbound/.
    Lets you back up / sweep one platform without affecting others."""
    path = save_inbound_media(
        platform="whatsapp",
        message_id="abc",
        data=b"hello",
        mimetype="image/jpeg",
    )
    assert path is not None
    p = Path(path)
    assert p.parent.name == "whatsapp"
    assert p.parent.parent.name == "media_inbound"
    assert p.read_bytes() == b"hello"


def test_save_inbound_media_different_platforms_isolated(
    _obscura_home: Path,
) -> None:
    """Two adapters saving with the same message_id don't collide
    because they end up in different per-platform subdirs."""
    wa_path = save_inbound_media(
        platform="whatsapp",
        message_id="dup",
        data=b"WA",
        mimetype="image/jpeg",
    )
    im_path = save_inbound_media(
        platform="imessage",
        message_id="dup",
        data=b"IM",
        mimetype="image/jpeg",
    )
    assert wa_path is not None and im_path is not None
    assert wa_path != im_path
    assert Path(wa_path).read_bytes() == b"WA"
    assert Path(im_path).read_bytes() == b"IM"


def test_save_inbound_media_empty_data_returns_none(_obscura_home: Path) -> None:
    """Don't create empty files — empty data = bug upstream, return None
    and let the caller fall back to the marker."""
    result = save_inbound_media(
        platform="whatsapp",
        message_id="abc",
        data=b"",
        mimetype="image/jpeg",
    )
    assert result is None


def test_save_inbound_media_unknown_mimetype_uses_bin_ext(
    _obscura_home: Path,
) -> None:
    """Unknown mimetype → .bin extension. File is still saved and
    usable; the agent's tools just won't have a hint about the format."""
    path = save_inbound_media(
        platform="whatsapp",
        message_id="abc",
        data=b"some bytes",
        mimetype="application/x-unknown-format",
    )
    assert path is not None
    assert path.endswith(".bin")
