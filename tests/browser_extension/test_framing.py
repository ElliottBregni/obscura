"""Test native-messaging framing (encode/decode round-trip)."""

from __future__ import annotations

from typing import Any

from .conftest import decode_frame, encode_frame


class TestFraming:
    def test_round_trip_simple(self) -> None:
        msg: dict[str, Any] = {"type": "ping", "id": "abc"}
        raw = encode_frame(msg)
        decoded, remaining = decode_frame(raw)
        assert decoded == msg
        assert remaining == b""

    def test_round_trip_unicode(self):
        msg = {"type": "chunk", "text": "hello \U0001f30d caf\u00e9"}
        raw = encode_frame(msg)
        decoded, _ = decode_frame(raw)
        assert decoded == msg

    def test_round_trip_empty(self) -> None:
        msg: dict[str, Any] = {}
        raw = encode_frame(msg)
        decoded, _ = decode_frame(raw)
        assert decoded == msg

    def test_decode_partial_header(self):
        raw = b"\x05\x00"  # only 2 bytes of header
        decoded, remaining = decode_frame(raw)
        assert decoded is None
        assert remaining == raw

    def test_decode_partial_payload(self):
        msg = {"type": "test"}
        raw = encode_frame(msg)
        partial = raw[:6]  # header + partial payload
        decoded, remaining = decode_frame(partial)
        assert decoded is None
        assert remaining == partial

    def test_multiple_frames(self):
        msg1 = {"type": "a"}
        msg2 = {"type": "b", "data": 42}
        raw = encode_frame(msg1) + encode_frame(msg2)

        d1, rest = decode_frame(raw)
        assert d1 == msg1
        d2, rest = decode_frame(rest)
        assert d2 == msg2
        assert rest == b""

    def test_large_payload(self):
        msg = {"type": "chunk", "text": "x" * 100_000}
        raw = encode_frame(msg)
        decoded, _ = decode_frame(raw)
        assert decoded == msg
