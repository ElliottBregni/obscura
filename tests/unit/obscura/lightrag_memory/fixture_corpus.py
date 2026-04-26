"""Static corpus of representative memories — used by multiple test files."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Chunk:
    key: str
    namespace: str
    text: str
    memory_type: str


CORPUS: list[Chunk] = [
    Chunk(
        "user_lang_python", "default", "User primarily codes in Python 3.13.", "fact"
    ),
    Chunk(
        "user_lang_typescript", "default", "User maintains a TypeScript web UI.", "fact"
    ),
    Chunk("user_editor_neovim", "default", "User's editor is Neovim with LSP.", "fact"),
    Chunk("user_shell_zsh", "default", "User's shell is zsh.", "fact"),
    Chunk(
        "user_os_macos", "default", "User runs macOS Sequoia on a MacBook Pro.", "fact"
    ),
    Chunk("user_pkg_uv", "default", "User uses uv to manage Python deps.", "fact"),
    Chunk(
        "conv_summary_2026_04_01",
        "default",
        "Discussed LightRAG integration plan over a 2-hour session.",
        "summary",
    ),
    Chunk(
        "conv_summary_2026_04_15",
        "default",
        "Reviewed Qdrant migration path. User prefers local mode.",
        "summary",
    ),
    Chunk(
        "conv_summary_2026_04_20",
        "default",
        "Walked through decay config tuning. Settled on per-type profiles.",
        "summary",
    ),
    Chunk(
        "conv_summary_2026_04_22",
        "default",
        "Sketched test plan for hybrid scoring.",
        "summary",
    ),
    Chunk(
        "turn_001",
        "default",
        "User asked: how do I add an extra to pyproject?",
        "episode",
    ),
    Chunk(
        "turn_002",
        "default",
        "Assistant explained PEP 621 optional-dependencies.",
        "episode",
    ),
    Chunk(
        "turn_003",
        "default",
        "User asked: where does qdrant store data locally?",
        "episode",
    ),
    Chunk("turn_004", "default", "Assistant answered ~/.obscura/qdrant/.", "episode"),
    Chunk("turn_005", "default", "User confirmed and moved on.", "episode"),
    Chunk(
        "note_test_idiom",
        "default",
        "Use BackendConfig + SQLiteBackend in tmp_path for vector tests.",
        "general",
    ),
    Chunk(
        "note_async_test",
        "default",
        "Pytest is configured for asyncio_mode=auto; just use async def test_*.",
        "general",
    ),
    Chunk(
        "note_lint",
        "default",
        "Run `make lint` and `make typecheck` before opening a PR.",
        "general",
    ),
    Chunk(
        "pref_no_emoji",
        "default",
        "User prefers no emojis in generated docs.",
        "preference",
    ),
    Chunk("pref_concise", "default", "User wants concise responses.", "preference"),
]


def by_type(memory_type: str) -> list[Chunk]:
    return [c for c in CORPUS if c.memory_type == memory_type]


def by_key(key: str) -> Chunk:
    return next(c for c in CORPUS if c.key == key)
