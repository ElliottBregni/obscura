"""Tests for `_extract_text` — pulls human-readable text from a wuzapi
Message dict, including synthesized markers for media variants.

The motivating bug: user sent an image and the message was dropped at
the adapter level because `_extract_text` only knew about ``conversation``
and ``extendedTextMessage.text``. Image messages have a separate
``imageMessage`` payload with an optional ``caption`` — they don't fit
either of the text-only probes and fall through to "" → blank-text
drop.

After the fix, media variants produce a synthesized text marker that
the agent can recognize (``[image]``, ``[image] caption: ...``, etc).
"""

from __future__ import annotations

from typing import Any

import pytest

from obscura.integrations.whatsapp.wuzapi.adapter import _extract_text


# ---------------------------------------------------------------------------
# Existing text paths — regression guard
# ---------------------------------------------------------------------------


def test_conversation_plain_text() -> None:
    assert _extract_text({"conversation": "hello"}) == "hello"


def test_extended_text_message() -> None:
    msg: dict[str, Any] = {"extendedTextMessage": {"text": "reply with quote"}}
    assert _extract_text(msg) == "reply with quote"


def test_ephemeral_wrapper() -> None:
    msg: dict[str, Any] = {
        "ephemeralMessage": {"message": {"conversation": "disappearing"}},
    }
    assert _extract_text(msg) == "disappearing"


def test_no_text_returns_empty() -> None:
    """Genuinely empty / unknown variants still return "" so the adapter
    can drop them as blank-text."""
    assert _extract_text({"reactionMessage": {"text": "👍"}}) == ""


# ---------------------------------------------------------------------------
# Media variants — synthesized text markers
# ---------------------------------------------------------------------------


def test_image_no_caption() -> None:
    """The motivating bug: image-only message produced an empty text
    and was dropped at the adapter level. Now produces a marker."""
    msg: dict[str, Any] = {"imageMessage": {"mimetype": "image/jpeg"}}
    assert _extract_text(msg) == "[image]"


def test_image_with_caption() -> None:
    msg: dict[str, Any] = {
        "imageMessage": {
            "mimetype": "image/jpeg",
            "caption": "please look at this",
        },
    }
    assert _extract_text(msg) == "[image] caption: please look at this"


def test_image_caption_is_whitespace_only() -> None:
    """Whitespace-only caption should be ignored (no trailing 'caption: ')."""
    msg: dict[str, Any] = {"imageMessage": {"caption": "   "}}
    assert _extract_text(msg) == "[image]"


def test_video_with_caption() -> None:
    msg: dict[str, Any] = {
        "videoMessage": {"caption": "watch this clip"},
    }
    assert _extract_text(msg) == "[video] caption: watch this clip"


def test_voice_note() -> None:
    """Voice notes never have captions — just the marker."""
    assert _extract_text({"audioMessage": {"ptt": True}}) == "[voice note]"


def test_document_with_filename_and_caption() -> None:
    msg: dict[str, Any] = {
        "documentMessage": {
            "fileName": "invoice.pdf",
            "caption": "my receipt for August",
        },
    }
    result = _extract_text(msg)
    assert result == "[document] (invoice.pdf) caption: my receipt for August"


def test_document_filename_only() -> None:
    msg: dict[str, Any] = {"documentMessage": {"fileName": "report.docx"}}
    assert _extract_text(msg) == "[document] (report.docx)"


def test_sticker() -> None:
    """Stickers have no text; just the marker."""
    assert _extract_text({"stickerMessage": {}}) == "[sticker]"


def test_location() -> None:
    msg: dict[str, Any] = {
        "locationMessage": {
            "name": "Times Square",
            "address": "Manhattan, NY",
        },
    }
    result = _extract_text(msg)
    assert result == "[location] name: Times Square address: Manhattan, NY"


def test_contact_share() -> None:
    assert _extract_text({"contactMessage": {}}) == "[contact]"


def test_image_wrapped_in_ephemeral() -> None:
    """Disappearing image messages get the marker too (wrapper unwrap +
    media extraction compose correctly)."""
    msg: dict[str, Any] = {
        "ephemeralMessage": {
            "message": {"imageMessage": {"caption": "secret pic"}},
        },
    }
    assert _extract_text(msg) == "[image] caption: secret pic"


def test_image_wrapped_in_view_once() -> None:
    """ViewOnce media (one-tap-to-view) also synthesizes correctly."""
    msg: dict[str, Any] = {
        "viewOnceMessageV2": {"message": {"imageMessage": {}}},
    }
    assert _extract_text(msg) == "[image]"


def test_text_takes_precedence_over_media() -> None:
    """If a message has BOTH text and a media payload (unusual but
    possible), the text wins — that's what the user actually typed."""
    msg: dict[str, Any] = {
        "conversation": "explicit text",
        "imageMessage": {"caption": "ignored"},
    }
    assert _extract_text(msg) == "explicit text"


@pytest.mark.parametrize(
    "media_key,expected_label",
    [
        ("imageMessage", "image"),
        ("videoMessage", "video"),
        ("documentMessage", "document"),
        ("audioMessage", "voice note"),
        ("stickerMessage", "sticker"),
        ("locationMessage", "location"),
        ("contactMessage", "contact"),
        ("liveLocationMessage", "live location"),
    ],
)
def test_all_media_variants_produce_a_marker(
    media_key: str,
    expected_label: str,
) -> None:
    """Every media variant the adapter recognizes produces a non-empty
    marker so the message survives the blank-text filter."""
    msg: dict[str, Any] = {media_key: {}}
    result = _extract_text(msg)
    assert result == f"[{expected_label}]"
