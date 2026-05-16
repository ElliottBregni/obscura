"""obscura.integrations.messaging.media_store — shared inbound media storage.

A canonical, platform-agnostic place for messaging adapters (WhatsApp,
iMessage, Telegram, Signal, etc.) to save inbound media so the agent
can read it with its standard file/vision tools.

The pattern
-----------
1. Adapter receives an inbound message with attached media (image,
   video, document, audio).
2. Adapter downloads/extracts the bytes from its platform-specific
   source (wuzapi REST endpoint, iMessage attachment path, Telegram
   getFile API, ...).
3. Adapter calls :func:`save_inbound_media` with the bytes plus the
   message ID and mimetype.
4. Adapter receives back an absolute path and embeds it in the agent's
   prompt text as ``[<kind> at <path>] caption: ...``.

The agent then picks up the file via its existing tools — there's no
new "attachment-aware" code path. This deliberately keeps the
integration light: a single helper call, no shared dataclasses for
consumers to import, no plumbing through ``send_message`` signatures.

Storage layout
--------------
Files land at::

    ~/.obscura/media_inbound/<platform>/<sanitized_message_id>.<ext>

The per-platform subdirectory lets you sweep or back up one platform's
inbox without affecting others. Filenames are sanitized to alphanum +
dash + underscore so weird message IDs can't escape the directory.
Extensions are derived from the mimetype via :func:`mimetype_to_extension`.

There's intentionally no retention policy here — files persist until
the user deletes them. The agent often references the same image
across multiple turns ("look at the chart I sent earlier"), and
silently expiring files would break that. A separate scheduled-task
helper could clean up files older than N days if needed.
"""

from __future__ import annotations

import logging
from pathlib import Path

from obscura.core.paths import resolve_obscura_home

logger = logging.getLogger(__name__)


def media_inbound_dir() -> Path:
    """Root directory for inbound media. ``~/.obscura/media_inbound/``."""
    return resolve_obscura_home() / "media_inbound"


# Comprehensive mimetype → extension map. Adapters call this so file
# names match the actual content. Order doesn't matter (first match
# wins via the substring check below).
_MIMETYPE_EXTENSIONS: tuple[tuple[str, str], ...] = (
    # Images
    ("image/jpeg", ".jpg"),
    ("image/jpg", ".jpg"),
    ("image/png", ".png"),
    ("image/webp", ".webp"),
    ("image/gif", ".gif"),
    ("image/bmp", ".bmp"),
    ("image/tiff", ".tiff"),
    ("image/svg+xml", ".svg"),
    ("image/heic", ".heic"),
    # Videos
    ("video/mp4", ".mp4"),
    ("video/quicktime", ".mov"),
    ("video/webm", ".webm"),
    ("video/x-msvideo", ".avi"),
    ("video/mpeg", ".mpeg"),
    ("video/x-matroska", ".mkv"),
    # Audio
    ("audio/ogg", ".ogg"),
    ("audio/mpeg", ".mp3"),
    ("audio/mp4", ".m4a"),
    ("audio/aac", ".aac"),
    ("audio/wav", ".wav"),
    ("audio/x-wav", ".wav"),
    ("audio/flac", ".flac"),
    ("audio/opus", ".opus"),
    ("audio/x-m4a", ".m4a"),
    # Documents
    ("application/pdf", ".pdf"),
    ("application/msword", ".doc"),
    (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".docx",
    ),
    ("application/vnd.ms-excel", ".xls"),
    (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsx",
    ),
    ("application/vnd.ms-powerpoint", ".ppt"),
    (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".pptx",
    ),
    ("application/json", ".json"),
    ("application/xml", ".xml"),
    ("text/plain", ".txt"),
    ("text/csv", ".csv"),
    ("text/html", ".html"),
    ("text/markdown", ".md"),
    ("application/zip", ".zip"),
    ("application/x-tar", ".tar"),
    ("application/gzip", ".gz"),
)


def mimetype_to_extension(mimetype: str) -> str:
    """Map a mimetype to a file extension (including the leading dot).

    Falls back to ``.bin`` if the mimetype isn't recognized — the file
    is still saved and usable, just with a generic extension. Matching
    is case-insensitive and uses substring containment so codec
    variants (e.g. ``image/jpeg; charset=binary``) still resolve.
    """
    mt = mimetype.lower().strip()
    if not mt:
        return ".bin"
    # Try exact match first, then substring (handles parameters like
    # "image/jpeg; codecs=...").
    for prefix, ext in _MIMETYPE_EXTENSIONS:
        if mt == prefix or mt.startswith(prefix + ";"):
            return ext
    for prefix, ext in _MIMETYPE_EXTENSIONS:
        if prefix in mt:
            return ext
    return ".bin"


def sanitize_filename_stem(stem: str) -> str:
    """Strip everything except alphanum, dash, and underscore.

    Defaults to ``"media"`` if the result is empty. Used so an
    untrusted message ID (which could in theory contain ``/`` or
    ``..``) can't escape the per-platform subdirectory.
    """
    cleaned = "".join(c for c in stem if c.isalnum() or c in "-_")
    return cleaned or "media"


def save_inbound_media(
    platform: str,
    message_id: str,
    data: bytes,
    mimetype: str,
) -> str | None:
    """Save inbound media bytes to disk, return the absolute path.

    Args:
        platform: Lowercase platform identifier (``whatsapp``,
            ``imessage``, ``telegram``, etc.). Used as the
            subdirectory name under ``media_inbound/``.
        message_id: Unique identifier for the source message. Used
            (sanitized) as the filename stem so two messages with the
            same content don't collide.
        data: Raw decrypted bytes.
        mimetype: Source mimetype (e.g. ``image/jpeg``,
            ``application/pdf``). Determines the file extension.

    Returns the absolute path on success, ``None`` on any failure
    (unwriteable parent, empty data, etc.) so callers can gracefully
    fall back to a no-attachment marker.

    Per-platform subdirectory is created lazily; concurrent calls
    that need the same directory race harmlessly via ``mkdir(...,
    exist_ok=True)``.
    """
    if not data:
        return None
    safe_platform = sanitize_filename_stem(platform) or "unknown"
    safe_stem = sanitize_filename_stem(message_id)
    ext = mimetype_to_extension(mimetype)
    target_dir = media_inbound_dir() / safe_platform
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        logger.debug(
            "media_store: failed to create dir %s",
            target_dir,
            exc_info=True,
        )
        return None
    target_path = target_dir / f"{safe_stem}{ext}"
    try:
        target_path.write_bytes(data)
    except Exception:
        logger.debug(
            "media_store: failed to write %s",
            target_path,
            exc_info=True,
        )
        return None
    return str(target_path)


__all__ = [
    "media_inbound_dir",
    "mimetype_to_extension",
    "sanitize_filename_stem",
    "save_inbound_media",
]
