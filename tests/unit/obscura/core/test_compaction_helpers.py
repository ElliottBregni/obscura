"""Tests for compaction's duck-typed message/block helpers.

These read provider-shaped dicts and objects (Anthropic, OpenAI, etc.).
After the strict-typing pass the helpers return None for non-string IDs
rather than passing arbitrary values through — covered here so the
contract doesn't drift.
"""

from __future__ import annotations

from types import SimpleNamespace

from obscura.core.compaction import (
    _get_block_id,
    _get_block_text,
    _get_block_tool_use_id,
    _get_block_type,
    _get_content,
    _get_role,
    _rebuild_block_text,
    _rebuild_message,
)


def test_get_content_reads_dict_and_object_forms() -> None:
    assert _get_content({"content": "hi"}) == "hi"
    assert _get_content({"content": [{"type": "text"}]}) == [{"type": "text"}]
    assert _get_content(SimpleNamespace(content="obj")) == "obj"
    assert _get_content({}) == ""


def test_get_role_coerces_to_str() -> None:
    assert _get_role({"role": "user"}) == "user"
    assert _get_role(SimpleNamespace(role="assistant")) == "assistant"
    assert _get_role({}) == ""


def test_get_block_type_reads_dict_and_object_forms() -> None:
    assert _get_block_type({"type": "tool_use"}) == "tool_use"
    assert _get_block_type(SimpleNamespace(type="text")) == "text"
    assert _get_block_type({"text": "no type"}) == ""


def test_get_block_id_returns_none_for_non_string() -> None:
    """The strict-typing pass narrows the return: non-str ids become None."""
    assert _get_block_id({"id": "abc"}) == "abc"
    assert _get_block_id({"id": 123}) is None
    assert _get_block_id({"id": None}) is None
    assert _get_block_id({}) is None
    assert _get_block_id(SimpleNamespace(id="obj-id")) == "obj-id"
    assert _get_block_id(SimpleNamespace(id=42)) is None


def test_get_block_tool_use_id_returns_none_for_non_string() -> None:
    assert _get_block_tool_use_id({"tool_use_id": "abc"}) == "abc"
    assert _get_block_tool_use_id({"tool_use_id": 123}) is None
    assert _get_block_tool_use_id({}) is None
    assert _get_block_tool_use_id(SimpleNamespace(tool_use_id="obj")) == "obj"


def test_get_block_text_coerces() -> None:
    assert _get_block_text({"text": "hello"}) == "hello"
    assert _get_block_text({"text": ""}) == ""
    # When text is missing the helper falls back to ""
    assert _get_block_text({}) == ""


def test_rebuild_block_text_preserves_other_dict_fields() -> None:
    block = {"type": "text", "text": "old", "extra": "kept"}
    new = _rebuild_block_text(block, "new")
    assert new == {"type": "text", "text": "new", "extra": "kept"}
    # Original untouched
    assert block["text"] == "old"


def test_rebuild_block_text_uses_replace_for_namedtuple_like() -> None:
    """Objects exposing ``_replace`` (namedtuples, etc.) are updated structurally."""

    class FakeBlock:
        def __init__(self, type_: str, text: str) -> None:
            self.type = type_
            self.text = text

        def _replace(self, **kwargs: str) -> "FakeBlock":
            new = FakeBlock(self.type, self.text)
            for k, v in kwargs.items():
                setattr(new, k, v)
            return new

    block = FakeBlock("text", "old")
    new = _rebuild_block_text(block, "new")
    assert isinstance(new, FakeBlock)
    assert new.text == "new"
    assert block.text == "old"


def test_rebuild_message_preserves_dict_role_and_metadata() -> None:
    msg = {"role": "user", "content": "old", "ts": 1}
    new = _rebuild_message(msg, [{"type": "text", "text": "x"}])
    assert new == {"role": "user", "content": [{"type": "text", "text": "x"}], "ts": 1}
