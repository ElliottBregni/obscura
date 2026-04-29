"""Tests for vector_memory_bridge — injection mode + project scope.

The two knobs being verified:

* ``OBSCURA_MEMORY_INJECTION_MODE`` — ``first`` (default), ``every``,
  ``off``. Controls whether the cli REPL's per-turn pre-search runs.
  In ``first`` mode it should run once per session and stop, since
  conversation history covers continuations and the per-turn search
  was the source of cross-project noise.
* ``OBSCURA_MEMORY_PROJECT_SCOPE`` — when on, auto-saved memories are
  tagged with a stable ``project_key`` derived from git toplevel (or
  cwd) and the search functions drop entries whose ``project_key``
  doesn't match. Pre-tag entries are dropped — that's intentional;
  they're indistinct cross-project noise and TTL handles them.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from obscura.cli.vector_memory_bridge import (
    _filter_results_by_project,
    derive_project_key,
    get_memory_injection_mode,
    is_project_scope_enabled,
    search_relevant_context,
)


class TestInjectionMode:
    def test_default_is_first(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OBSCURA_MEMORY_INJECTION_MODE", raising=False)
        assert get_memory_injection_mode() == "first"

    @pytest.mark.parametrize(
        ("env", "expected"),
        [
            ("first", "first"),
            ("every", "every"),
            ("off", "off"),
            ("FIRST", "first"),  # case-insensitive
            ("Off", "off"),
        ],
    )
    def test_explicit_modes(
        self,
        monkeypatch: pytest.MonkeyPatch,
        env: str,
        expected: str,
    ) -> None:
        monkeypatch.setenv("OBSCURA_MEMORY_INJECTION_MODE", env)
        assert get_memory_injection_mode() == expected

    def test_unknown_value_falls_back_to_first(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OBSCURA_MEMORY_INJECTION_MODE", "weekly")
        assert get_memory_injection_mode() == "first"


class TestProjectScopeEnabled:
    def test_default_is_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OBSCURA_MEMORY_PROJECT_SCOPE", raising=False)
        assert is_project_scope_enabled() is True

    @pytest.mark.parametrize("val", ["off", "false", "0", "no", "OFF"])
    def test_explicit_off(self, monkeypatch: pytest.MonkeyPatch, val: str) -> None:
        monkeypatch.setenv("OBSCURA_MEMORY_PROJECT_SCOPE", val)
        assert is_project_scope_enabled() is False


class TestDeriveProjectKey:
    def test_returns_short_stable_string(self) -> None:
        key = derive_project_key()
        # 12-char SHA-1 prefix.
        assert isinstance(key, str)
        assert len(key) == 12

    def test_same_dir_same_key(self, tmp_path: Any) -> None:
        a = derive_project_key(tmp_path)
        b = derive_project_key(tmp_path)
        assert a == b

    def test_different_dirs_different_keys(self, tmp_path: Any) -> None:
        d1 = tmp_path / "proj_a"
        d2 = tmp_path / "proj_b"
        d1.mkdir()
        d2.mkdir()
        # Neither has a git root, so the keys derive from the absolute
        # paths and must differ.
        assert derive_project_key(d1) != derive_project_key(d2)

    def test_subdir_collapses_to_git_root(self, tmp_path: Any) -> None:
        """A subdirectory in a git repo must share the parent repo's
        project_key — that's the whole point of preferring the git
        toplevel over cwd."""
        repo = tmp_path / "repo"
        repo.mkdir()
        # Initialize a git repo.
        import subprocess

        subprocess.run(
            ["git", "init", "-q"], cwd=str(repo), check=True, timeout=5
        )
        subdir = repo / "deep" / "nested"
        subdir.mkdir(parents=True)

        root_key = derive_project_key(repo)
        sub_key = derive_project_key(subdir)
        assert root_key == sub_key


class TestFilterResultsByProject:
    def _result(self, project_key: str | None) -> Any:
        r = MagicMock()
        r.metadata = {"project_key": project_key} if project_key else {}
        return r

    def test_keeps_only_matching_project(self) -> None:
        results = [
            self._result("aaa"),
            self._result("bbb"),
            self._result("aaa"),
            self._result(None),  # untagged — must be dropped
        ]
        kept = _filter_results_by_project(results, "aaa")
        assert len(kept) == 2

    def test_no_metadata_means_dropped(self) -> None:
        """Pre-scope-tag entries (saved before the knob existed) get
        dropped. That's intentional — once project-scoping is on for a
        few sessions, the unscoped entries decay out via TTL."""
        r = MagicMock()
        r.metadata = None
        kept = _filter_results_by_project([r], "anything")
        assert kept == []

    def test_empty_input(self) -> None:
        assert _filter_results_by_project([], "x") == []


class TestSearchRelevantContextProjectFilter:
    """End-to-end: the search function fetches more than top_k when
    project filter is on so post-filter still has a fighting chance,
    and trims the final list to top_k."""

    def _hit(self, score: float, text: str, project_key: str | None) -> Any:
        h = MagicMock()
        h.score = score
        h.text = text
        h.metadata = {"project_key": project_key} if project_key else {}
        return h

    def test_filters_to_matching_project(self) -> None:
        store = MagicMock()
        store.search_reranked.return_value = [
            self._hit(0.9, "match A", "current"),
            self._hit(0.85, "noise A", "other"),
            self._hit(0.8, "match B", "current"),
            self._hit(0.7, "noise B", "other"),
        ]
        out = search_relevant_context(
            store, query="anything", top_k=3, project_key="current"
        )
        assert "match A" in out
        assert "match B" in out
        assert "noise A" not in out
        assert "noise B" not in out

    def test_overfetches_when_project_key_set(self) -> None:
        """We over-fetch (top_k * 4) so post-filter has results to keep."""
        store = MagicMock()
        store.search_reranked.return_value = []
        search_relevant_context(
            store, query="x", top_k=3, project_key="current"
        )
        call = store.search_reranked.call_args
        assert call.kwargs["top_k"] == 12  # 3 * 4

    def test_no_overfetch_when_project_key_unset(self) -> None:
        store = MagicMock()
        store.search_reranked.return_value = []
        search_relevant_context(store, query="x", top_k=3, project_key=None)
        call = store.search_reranked.call_args
        assert call.kwargs["top_k"] == 3

    def test_returns_empty_when_filter_drops_everything(self) -> None:
        store = MagicMock()
        store.search_reranked.return_value = [
            self._hit(0.9, "noise", "other"),
            self._hit(0.85, "more noise", "other"),
        ]
        out = search_relevant_context(
            store, query="x", top_k=3, project_key="current"
        )
        assert out == ""

    def test_unfiltered_path_passes_through(self) -> None:
        """When project_key is None, no filtering happens."""
        store = MagicMock()
        store.search_reranked.return_value = [
            self._hit(0.9, "result", None),  # no metadata
        ]
        out = search_relevant_context(store, query="x", top_k=3, project_key=None)
        assert "result" in out
