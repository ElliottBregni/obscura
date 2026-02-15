"""Tests for crawlers.py — Mermaid diagram crawler."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from crawlers import (
    FileEntry,
    CrawlResult,
    crawl_repo,
    split_mermaid_diagrams,
    generate_index,
    build_parser,
    main,
    _strip_markdown_fences,
    _trim_leading_prose,
    _build_stub_markdown,
    _render_svg_with_kroki,
    _render_svg_with_mmdc,
    _render_single_diagram,
    DiagramCrawlerAgent,
    MERMAID_PROMPT,
    SKIP_DIRS,
    SKIP_FILES,
    SKIP_FILENAMES,
    DEFAULT_EXTENSIONS,
    DEFAULT_WORKERS,
    MAX_FILE_SIZE,
    MAX_COPILOT_CONCURRENT,
    MAX_KROKI_CONCURRENT,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_message(text: str) -> MagicMock:
    """Create a mock Message with a .text attribute."""
    msg = MagicMock()
    msg.text = text
    return msg


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_repo(tmp_path):
    """Create a sample repository structure for testing."""
    repo = tmp_path / "my_repo"
    repo.mkdir()

    # Python files
    (repo / "main.py").write_text("def main():\n    print('hello')\n")
    (repo / "utils.py").write_text("def add(a, b):\n    return a + b\n")

    # Nested directory
    sub = repo / "sub"
    sub.mkdir()
    (sub / "helper.py").write_text("class Helper:\n    pass\n")

    # Files that should be skipped
    (repo / "README.md").write_text("# My Repo\n")
    (repo / "AGENTS.md").write_text("# Agents\n")
    (repo / ".DS_Store").write_bytes(b"\x00")
    (repo / "big_file.py").write_text("x" * 200_000)  # Over MAX_FILE_SIZE
    (repo / "empty.py").write_text("")  # Empty

    # Skip directory
    node_modules = repo / "node_modules"
    node_modules.mkdir()
    (node_modules / "lib.js").write_text("module.exports = {}")

    # Hidden directory
    hidden = repo / ".hidden"
    hidden.mkdir()
    (hidden / "secret.py").write_text("SECRET = 42")

    # Non-matching extension
    (repo / "notes.txt").write_text("just notes")

    return repo


@pytest.fixture
def file_entry(tmp_path):
    """Create a FileEntry for testing."""
    code_file = tmp_path / "sample.py"
    code_file.write_text("def greet():\n    return 'hello'\n")
    return FileEntry(
        repo_relative=Path("sample.py"),
        absolute=code_file,
        extension=".py",
        size=code_file.stat().st_size,
    )


# ---------------------------------------------------------------------------
# split_mermaid_diagrams
# ---------------------------------------------------------------------------

class TestSplitMermaidDiagrams:
    def test_single_graph(self):
        text = "graph TD\n    A --> B\n    B --> C"
        result = split_mermaid_diagrams(text)
        assert len(result) == 1
        assert result[0] == "graph TD\n    A --> B\n    B --> C"

    def test_single_flowchart(self):
        text = "flowchart LR\n    Start --> End"
        result = split_mermaid_diagrams(text)
        assert len(result) == 1
        assert "flowchart LR" in result[0]

    def test_two_graphs_glued(self):
        text = (
            "graph TD\n"
            "    A --> B\n"
            "graph LR\n"
            "    C --> D"
        )
        result = split_mermaid_diagrams(text)
        assert len(result) == 2
        assert "A --> B" in result[0]
        assert "C --> D" in result[1]

    def test_three_mixed_diagrams(self):
        text = (
            "graph TD\n"
            "    A --> B\n"
            "sequenceDiagram\n"
            "    Alice->>Bob: Hello\n"
            "classDiagram\n"
            "    class Animal"
        )
        result = split_mermaid_diagrams(text)
        assert len(result) == 3
        assert result[0].startswith("graph TD")
        assert result[1].startswith("sequenceDiagram")
        assert result[2].startswith("classDiagram")

    def test_strips_markdown_fences(self):
        text = "```mermaid\ngraph TD\n    A --> B\n```"
        result = split_mermaid_diagrams(text)
        assert len(result) == 1
        assert "```" not in result[0]
        assert "graph TD" in result[0]

    def test_strips_multiple_fences(self):
        text = (
            "```mermaid\n"
            "graph TD\n"
            "    A --> B\n"
            "```\n"
            "```mermaid\n"
            "flowchart LR\n"
            "    C --> D\n"
            "```"
        )
        result = split_mermaid_diagrams(text)
        assert len(result) == 2

    def test_trims_leading_prose(self):
        text = (
            "Here is the Mermaid diagram:\n"
            "\n"
            "graph TD\n"
            "    A --> B"
        )
        result = split_mermaid_diagrams(text)
        assert len(result) == 1
        assert result[0].startswith("graph TD")

    def test_empty_input(self):
        assert split_mermaid_diagrams("") == []
        assert split_mermaid_diagrams("   ") == []

    def test_no_valid_diagram(self):
        text = "This is just some text with no diagram."
        result = split_mermaid_diagrams(text)
        # Returns the text as-is since there's no diagram header to trim to
        assert len(result) == 1

    def test_all_diagram_types(self):
        types = [
            "graph TD\n    A-->B",
            "flowchart LR\n    A-->B",
            "sequenceDiagram\n    A->>B: Hi",
            "classDiagram\n    class A",
            "stateDiagram\n    [*] --> A",
            "erDiagram\n    A ||--o{ B : has",
            "journey\n    title My",
            "gantt\n    title G",
            "pie\n    title P",
            "mindmap\n    root",
            "timeline\n    title T",
        ]
        combined = "\n".join(types)
        result = split_mermaid_diagrams(combined)
        assert len(result) == 11

    def test_preserves_indentation(self):
        text = "graph TD\n    A --> B\n    B --> C\n        subgraph sub\n        D --> E\n        end"
        result = split_mermaid_diagrams(text)
        assert len(result) == 1
        assert "    A --> B" in result[0]
        assert "        subgraph sub" in result[0]

    def test_case_insensitive_detection(self):
        text = "Graph TD\n    A --> B\nFlowchart LR\n    C --> D"
        result = split_mermaid_diagrams(text)
        assert len(result) == 2

    def test_prose_with_fences_and_multiple_diagrams(self):
        text = (
            "Here are the diagrams:\n"
            "```\n"
            "graph TD\n"
            "    A --> B\n"
            "```\n"
            "And another:\n"
            "```mermaid\n"
            "flowchart LR\n"
            "    C --> D\n"
            "```"
        )
        result = split_mermaid_diagrams(text)
        assert len(result) == 2
        assert "graph TD" in result[0]
        assert "flowchart LR" in result[1]


# ---------------------------------------------------------------------------
# _strip_markdown_fences
# ---------------------------------------------------------------------------

class TestStripMarkdownFences:
    def test_strips_mermaid_fences(self):
        text = "```mermaid\ngraph TD\n    A-->B\n```"
        assert "```" not in _strip_markdown_fences(text)
        assert "graph TD" in _strip_markdown_fences(text)

    def test_strips_plain_fences(self):
        text = "```\ngraph TD\n    A-->B\n```"
        assert "```" not in _strip_markdown_fences(text)

    def test_no_fences_unchanged(self):
        text = "graph TD\n    A-->B"
        assert _strip_markdown_fences(text) == text

    def test_empty_string(self):
        assert _strip_markdown_fences("") == ""


# ---------------------------------------------------------------------------
# _trim_leading_prose
# ---------------------------------------------------------------------------

class TestTrimLeadingProse:
    def test_trims_prose(self):
        text = "Here is a diagram:\n\ngraph TD\n    A-->B"
        result = _trim_leading_prose(text)
        assert result.startswith("graph TD")

    def test_no_prose_unchanged(self):
        text = "graph TD\n    A-->B"
        assert _trim_leading_prose(text) == text

    def test_no_diagram_header(self):
        text = "Just some text"
        assert _trim_leading_prose(text) == text

    def test_multiple_lines_of_prose(self):
        text = "Line 1\nLine 2\nLine 3\nflowchart LR\n    A-->B"
        result = _trim_leading_prose(text)
        assert result.startswith("flowchart LR")


# ---------------------------------------------------------------------------
# crawl_repo
# ---------------------------------------------------------------------------

class TestCrawlRepo:
    def test_finds_python_files(self, sample_repo):
        result = crawl_repo(sample_repo, extensions={".py"})
        names = {e.repo_relative.name for e in result.files}
        assert "main.py" in names
        assert "utils.py" in names
        assert "helper.py" in names

    def test_skips_node_modules(self, sample_repo):
        result = crawl_repo(sample_repo, extensions={".py", ".js"})
        names = {e.repo_relative.name for e in result.files}
        assert "lib.js" not in names
        assert result.skipped_dirs > 0

    def test_skips_hidden_dirs(self, sample_repo):
        result = crawl_repo(sample_repo, extensions={".py"})
        names = {e.repo_relative.name for e in result.files}
        assert "secret.py" not in names

    def test_skips_ds_store(self, sample_repo):
        result = crawl_repo(sample_repo, extensions={".py"})
        names = {e.repo_relative.name for e in result.files}
        assert ".DS_Store" not in names

    def test_skips_readme(self, sample_repo):
        result = crawl_repo(sample_repo, extensions={".py", ".md"})
        names = {e.repo_relative.name for e in result.files}
        assert "README.md" not in names
        assert "AGENTS.md" not in names

    def test_skips_oversized_files(self, sample_repo):
        result = crawl_repo(sample_repo, extensions={".py"})
        names = {e.repo_relative.name for e in result.files}
        assert "big_file.py" not in names
        assert result.skipped_size > 0

    def test_skips_empty_files(self, sample_repo):
        result = crawl_repo(sample_repo, extensions={".py"})
        names = {e.repo_relative.name for e in result.files}
        assert "empty.py" not in names

    def test_skips_wrong_extension(self, sample_repo):
        result = crawl_repo(sample_repo, extensions={".py"})
        names = {e.repo_relative.name for e in result.files}
        assert "notes.txt" not in names
        assert result.skipped_ext > 0

    def test_custom_extensions(self, sample_repo):
        result = crawl_repo(sample_repo, extensions={".txt"})
        names = {e.repo_relative.name for e in result.files}
        assert "notes.txt" in names
        assert "main.py" not in names

    def test_custom_max_size(self, sample_repo):
        result = crawl_repo(sample_repo, extensions={".py"}, max_size=10)
        # All .py files except empty should be skipped as too large
        assert result.skipped_size > 0

    def test_nonexistent_repo(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            crawl_repo(tmp_path / "nonexistent")

    def test_repo_relative_paths(self, sample_repo):
        result = crawl_repo(sample_repo, extensions={".py"})
        for entry in result.files:
            assert not entry.repo_relative.is_absolute()
            assert entry.absolute.is_absolute()

    def test_file_entry_fields(self, sample_repo):
        result = crawl_repo(sample_repo, extensions={".py"})
        for entry in result.files:
            assert entry.size > 0
            assert entry.extension == ".py"
            assert entry.absolute.exists()

    def test_total_discovered(self, sample_repo):
        result = crawl_repo(sample_repo, extensions={".py"})
        assert result.total_discovered > len(result.files)

    def test_repo_name(self, sample_repo):
        result = crawl_repo(sample_repo)
        assert result.repo_name == "my_repo"


# ---------------------------------------------------------------------------
# _render_svg_with_kroki
# ---------------------------------------------------------------------------

class TestRenderSvgWithKroki:
    @patch("crawlers.urllib.request.urlopen")
    def test_returns_svg(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'<svg xmlns="http://www.w3.org/2000/svg">test</svg>'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = _render_svg_with_kroki("graph TD\n    A-->B")
        assert "<svg" in result

    @patch("crawlers.urllib.request.urlopen")
    def test_uses_post(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'<svg xmlns="http://www.w3.org/2000/svg">test</svg>'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        _render_svg_with_kroki("graph TD\n    A-->B")
        req = mock_urlopen.call_args[0][0]
        assert req.method == "POST"
        assert req.get_header("Content-type") == "text/plain; charset=utf-8"

    @patch("crawlers.urllib.request.urlopen")
    def test_raises_on_no_svg(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"not svg"
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        with pytest.raises(RuntimeError, match="did not return SVG"):
            _render_svg_with_kroki("graph TD\n    A-->B")


# ---------------------------------------------------------------------------
# _render_svg_with_mmdc
# ---------------------------------------------------------------------------

class TestRenderSvgWithMmdc:
    @patch("crawlers.subprocess.run")
    def test_returns_none_when_mmdc_not_found(self, mock_run):
        mock_run.side_effect = FileNotFoundError
        result = _render_svg_with_mmdc("graph TD\n    A-->B")
        assert result is None


# ---------------------------------------------------------------------------
# _render_single_diagram
# ---------------------------------------------------------------------------

class TestRenderSingleDiagram:
    @patch("crawlers._render_svg_with_mmdc")
    @patch("crawlers._render_svg_with_kroki")
    def test_prefers_mmdc(self, mock_kroki, mock_mmdc):
        mock_mmdc.return_value = "<svg>mmdc</svg>"
        result = _render_single_diagram("graph TD\n    A-->B")
        assert result == "<svg>mmdc</svg>"
        mock_kroki.assert_not_called()

    @patch("crawlers._render_svg_with_mmdc")
    @patch("crawlers._render_svg_with_kroki")
    def test_falls_back_to_kroki(self, mock_kroki, mock_mmdc):
        mock_mmdc.return_value = None
        mock_kroki.return_value = "<svg>kroki</svg>"
        result = _render_single_diagram("graph TD\n    A-->B")
        assert result == "<svg>kroki</svg>"

    @patch("crawlers._render_svg_with_mmdc")
    @patch("crawlers._render_svg_with_kroki")
    def test_returns_none_on_all_failure(self, mock_kroki, mock_mmdc):
        mock_mmdc.return_value = None
        mock_kroki.side_effect = RuntimeError("Kroki failed")
        result = _render_single_diagram("graph TD\n    A-->B")
        assert result is None


# ---------------------------------------------------------------------------
# _build_stub_markdown
# ---------------------------------------------------------------------------

class TestBuildStubMarkdown:
    @patch("crawlers._render_single_diagram")
    def test_single_diagram(self, mock_render, file_entry):
        mock_render.return_value = '<svg xmlns="http://www.w3.org/2000/svg">test</svg>'

        md = _build_stub_markdown(file_entry, "graph TD\n    A --> B")

        assert "# Diagram: sample.py" in md
        assert "```mermaid" in md
        assert "graph TD" in md
        assert "A --> B" in md
        assert "```" in md
        assert '<svg xmlns="http://www.w3.org/2000/svg">test</svg>' in md
        assert "Auto-generated by Obscura crawlers" in md

    @patch("crawlers._render_single_diagram")
    def test_multiple_diagrams(self, mock_render, file_entry):
        mock_render.return_value = '<svg>test</svg>'

        md = _build_stub_markdown(
            file_entry,
            "graph TD\n    A --> B\nflowchart LR\n    C --> D",
        )

        assert "## Diagram 1" in md
        assert "## Diagram 2" in md
        assert md.count("```mermaid") == 2
        assert md.count("<svg>test</svg>") == 2

    @patch("crawlers._render_single_diagram")
    def test_render_failure_includes_note(self, mock_render, file_entry):
        mock_render.return_value = None

        md = _build_stub_markdown(file_entry, "graph TD\n    A --> B")

        assert "```mermaid" in md
        assert "graph TD" in md
        assert "SVG rendering failed" in md

    def test_no_valid_diagrams(self, file_entry):
        md = _build_stub_markdown(file_entry, "")

        assert "No valid Mermaid diagrams" in md

    @patch("crawlers._render_single_diagram")
    def test_svg_not_fenced(self, mock_render, file_entry):
        svg_content = '<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>'
        mock_render.return_value = svg_content

        md = _build_stub_markdown(file_entry, "graph TD\n    A --> B")

        # SVG should appear as raw inline, not inside a code fence
        lines = md.splitlines()
        for i, line in enumerate(lines):
            if "<svg" in line:
                assert i == 0 or "```" not in lines[i - 1]
                if i + 1 < len(lines):
                    assert "```" not in lines[i + 1] or "mermaid" in lines[i + 1]

    @patch("crawlers._render_single_diagram")
    def test_single_diagram_uses_mermaid_header(self, mock_render, file_entry):
        """When there's only one diagram, section header should be 'Mermaid' not 'Diagram 1'."""
        mock_render.return_value = '<svg>ok</svg>'

        md = _build_stub_markdown(file_entry, "graph TD\n    A --> B")

        assert "## Mermaid" in md
        assert "## Diagram 1" not in md

    @patch("crawlers._render_single_diagram")
    def test_fenced_copilot_output(self, mock_render, file_entry):
        """Copilot wrapping output in fences should still work."""
        mock_render.return_value = '<svg>ok</svg>'

        md = _build_stub_markdown(file_entry, "```mermaid\ngraph TD\n    A --> B\n```")
        assert "```mermaid" in md
        assert "graph TD" in md
        assert "<svg>ok</svg>" in md

    @patch("crawlers._render_single_diagram")
    def test_multiple_with_partial_failure(self, mock_render, file_entry):
        """If one diagram fails to render, others should still have SVG."""
        call_count = 0

        def render_side_effect(mermaid):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return None  # First diagram fails
            return '<svg>ok</svg>'

        mock_render.side_effect = render_side_effect

        md = _build_stub_markdown(
            file_entry,
            "graph TD\n    A --> B\nflowchart LR\n    C --> D",
        )

        assert "SVG rendering failed" in md
        assert "<svg>ok</svg>" in md
        assert md.count("```mermaid") == 2


# ---------------------------------------------------------------------------
# generate_index
# ---------------------------------------------------------------------------

class TestGenerateIndex:
    def test_basic_index(self, sample_repo):
        result = crawl_repo(sample_repo, extensions={".py"})
        index = generate_index(result)

        assert "crawlers Index: my_repo" in index
        assert "Auto-generated" in index
        assert "main.py" in index
        assert "utils.py" in index
        assert "helper.py" in index

    def test_index_has_stats(self, sample_repo):
        result = crawl_repo(sample_repo, extensions={".py"})
        index = generate_index(result)

        assert "Source files" in index
        assert "Skipped (junk)" in index
        assert "Skipped (too large)" in index
        assert "Skipped (wrong ext)" in index
        assert "Directories pruned" in index

    def test_index_has_directory_sections(self, sample_repo):
        result = crawl_repo(sample_repo, extensions={".py"})
        index = generate_index(result)

        assert "(root)" in index
        assert "sub" in index

    def test_index_has_file_links(self, sample_repo):
        result = crawl_repo(sample_repo, extensions={".py"})
        index = generate_index(result)

        assert "[main.py]" in index
        assert ".md)" in index  # Link targets end in .md)

    def test_index_skipped_count(self, sample_repo):
        result = crawl_repo(sample_repo, extensions={".py"})
        index = generate_index(result)
        total_skipped = result.skipped_files + result.skipped_size + result.skipped_ext
        assert f"Skipped: {total_skipped}" in index


# ---------------------------------------------------------------------------
# DiagramCrawlerAgent
# ---------------------------------------------------------------------------

class TestDiagramCrawlerAgent:
    @pytest.mark.asyncio
    async def test_analyze_crawls_repo(self, sample_repo, tmp_path):
        client = MagicMock()
        agent = DiagramCrawlerAgent(
            client, sample_repo, tmp_path / "out", extensions={".py"},
        )
        from sdk._types import AgentContext, AgentPhase
        ctx = AgentContext(phase=AgentPhase.ANALYZE)

        await agent.analyze(ctx)

        assert isinstance(ctx.analysis, CrawlResult)
        names = {e.repo_relative.name for e in ctx.analysis.files}
        assert "main.py" in names
        assert "utils.py" in names

    @pytest.mark.asyncio
    async def test_plan_filters_existing(self, sample_repo, tmp_path):
        client = MagicMock()
        output_dir = tmp_path / "out"
        agent = DiagramCrawlerAgent(
            client, sample_repo, output_dir, extensions={".py"},
        )
        from sdk._types import AgentContext, AgentPhase
        ctx = AgentContext(phase=AgentPhase.PLAN)

        # Analyze first
        await agent.analyze(ctx)

        # Pre-create one output file
        result: CrawlResult = ctx.analysis
        existing = output_dir / result.repo_name / "main.py.md"
        existing.parent.mkdir(parents=True, exist_ok=True)
        existing.write_text("# Already exists")

        await agent.plan(ctx)

        planned_names = {e.repo_relative.name for e in ctx.plan}
        assert "main.py" not in planned_names
        assert "utils.py" in planned_names

    @pytest.mark.asyncio
    async def test_execute_calls_sdk(self, sample_repo, tmp_path):
        client = MagicMock()
        client.send = AsyncMock(return_value=_make_message("graph TD\n    A --> B"))

        agent = DiagramCrawlerAgent(
            client, sample_repo, tmp_path / "out",
            extensions={".py"}, max_concurrent=2,
        )
        from sdk._types import AgentContext, AgentPhase
        ctx = AgentContext(phase=AgentPhase.ANALYZE)

        await agent.analyze(ctx)
        await agent.plan(ctx)

        with patch("crawlers._render_single_diagram", return_value="<svg>ok</svg>"):
            await agent.execute(ctx)

        assert len(ctx.results) > 0
        # Each result is a (FileEntry, content) tuple
        for entry, content in ctx.results:
            assert isinstance(entry, FileEntry)
            if content is not None:
                assert "```mermaid" in content

    @pytest.mark.asyncio
    async def test_execute_dry_run_skips(self, sample_repo, tmp_path):
        client = MagicMock()
        agent = DiagramCrawlerAgent(
            client, sample_repo, tmp_path / "out",
            extensions={".py"}, dry_run=True,
        )
        from sdk._types import AgentContext, AgentPhase
        ctx = AgentContext(phase=AgentPhase.ANALYZE)

        await agent.analyze(ctx)
        await agent.plan(ctx)
        await agent.execute(ctx)

        assert ctx.results == []
        # SDK should never be called
        assert not hasattr(client, 'send') or not client.send.called

    @pytest.mark.asyncio
    async def test_respond_writes_files(self, sample_repo, tmp_path):
        client = MagicMock()
        output_dir = tmp_path / "out"
        agent = DiagramCrawlerAgent(
            client, sample_repo, output_dir, extensions={".py"},
        )
        from sdk._types import AgentContext, AgentPhase
        ctx = AgentContext(phase=AgentPhase.ANALYZE)

        # Simulate analyze
        await agent.analyze(ctx)
        result: CrawlResult = ctx.analysis

        # Simulate execute results
        ctx.results = [(entry, f"# Diagram: {entry.repo_relative}") for entry in result.files]

        await agent.respond(ctx)

        assert ctx.response > 0
        # Check files written
        index_path = output_dir / result.repo_name / "INDEX.md"
        assert index_path.exists()
        main_md = output_dir / result.repo_name / "main.py.md"
        assert main_md.exists()

    @pytest.mark.asyncio
    async def test_respond_dry_run(self, sample_repo, tmp_path):
        client = MagicMock()
        output_dir = tmp_path / "out"
        agent = DiagramCrawlerAgent(
            client, sample_repo, output_dir,
            extensions={".py"}, dry_run=True,
        )
        from sdk._types import AgentContext, AgentPhase
        ctx = AgentContext(phase=AgentPhase.ANALYZE)
        await agent.analyze(ctx)

        await agent.respond(ctx)

        assert ctx.response == 0
        assert not (output_dir / ctx.analysis.repo_name).exists()

    @pytest.mark.asyncio
    async def test_execute_handles_sdk_error(self, sample_repo, tmp_path):
        """If the SDK raises, the entry should get None content, not crash."""
        client = MagicMock()
        client.send = AsyncMock(side_effect=RuntimeError("SDK error"))

        agent = DiagramCrawlerAgent(
            client, sample_repo, tmp_path / "out",
            extensions={".py"}, max_concurrent=2,
        )
        from sdk._types import AgentContext, AgentPhase
        ctx = AgentContext(phase=AgentPhase.ANALYZE)

        await agent.analyze(ctx)
        await agent.plan(ctx)
        await agent.execute(ctx)

        # Should not crash — entries get None content
        for entry, content in ctx.results:
            assert content is None

    @pytest.mark.asyncio
    async def test_full_run(self, sample_repo, tmp_path):
        """Full APER run through agent.run()."""
        client = MagicMock()
        client.send = AsyncMock(return_value=_make_message("graph TD\n    A --> B"))

        output_dir = tmp_path / "out"
        agent = DiagramCrawlerAgent(
            client, sample_repo, output_dir,
            extensions={".py"}, max_concurrent=2,
        )

        with patch("crawlers._render_single_diagram", return_value="<svg>ok</svg>"):
            written = await agent.run()

        assert written > 0
        index_path = output_dir / "my_repo" / "INDEX.md"
        assert index_path.exists()

    @pytest.mark.asyncio
    async def test_semaphore_limits(self, sample_repo, tmp_path):
        """Max concurrent SDK calls should be capped by semaphore."""
        active = 0
        peak = 0
        lock = asyncio.Lock()

        async def slow_send(prompt):
            nonlocal active, peak
            async with lock:
                active += 1
                if active > peak:
                    peak = active
            await asyncio.sleep(0.02)
            async with lock:
                active -= 1
            return _make_message("graph TD\n    A --> B")

        client = MagicMock()
        client.send = slow_send

        agent = DiagramCrawlerAgent(
            client, sample_repo, tmp_path / "out",
            extensions={".py"}, max_concurrent=1,
        )

        from sdk._types import AgentContext, AgentPhase
        ctx = AgentContext(phase=AgentPhase.ANALYZE)
        await agent.analyze(ctx)
        await agent.plan(ctx)

        with patch("crawlers._render_single_diagram", return_value="<svg>ok</svg>"):
            await agent.execute(ctx)

        # With max_concurrent=1, peak should be exactly 1
        assert peak <= 1, f"Expected max 1 concurrent, but peak was {peak}"


# ---------------------------------------------------------------------------
# CLI / build_parser
# ---------------------------------------------------------------------------

class TestCLI:
    def test_build_parser_required_repo(self):
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([])

    def test_build_parser_defaults(self):
        parser = build_parser()
        args = parser.parse_args(["--repo", "/tmp/test"])
        assert args.repo == Path("/tmp/test")
        assert args.output is None
        assert args.extensions is None
        assert args.max_size == MAX_FILE_SIZE
        assert args.workers == DEFAULT_WORKERS
        assert args.dry_run is False
        assert args.stats is False

    def test_build_parser_all_args(self):
        parser = build_parser()
        args = parser.parse_args([
            "--repo", "/tmp/test",
            "--output", "/tmp/out",
            "--extensions", ".py", ".js",
            "--max-size", "50000",
            "--workers", "8",
            "--dry-run",
            "--stats",
        ])
        assert args.repo == Path("/tmp/test")
        assert args.output == Path("/tmp/out")
        assert args.extensions == [".py", ".js"]
        assert args.max_size == 50000
        assert args.workers == 8
        assert args.dry_run is True
        assert args.stats is True

    def test_main_dry_run(self, sample_repo, capsys):
        ret = main(["--repo", str(sample_repo), "--dry-run"])
        assert ret == 0
        captured = capsys.readouterr()
        assert "dry-run" in captured.out

    def test_main_nonexistent_repo(self):
        ret = main(["--repo", "/nonexistent/path/xyzzy"])
        assert ret == 1

    def test_main_stats(self, sample_repo, capsys):
        ret = main(["--repo", str(sample_repo), "--stats", "--dry-run"])
        assert ret == 0
        captured = capsys.readouterr()
        assert "Crawl Stats" in captured.out

    def test_main_custom_extensions(self, sample_repo, capsys):
        ret = main(["--repo", str(sample_repo), "--extensions", "py", "js", "--dry-run"])
        assert ret == 0


# ---------------------------------------------------------------------------
# Constants / configuration
# ---------------------------------------------------------------------------

class TestConstants:
    def test_default_extensions_are_code(self):
        for ext in DEFAULT_EXTENSIONS:
            assert ext.startswith(".")
        # Should not include .md by default
        assert ".md" not in DEFAULT_EXTENSIONS

    def test_skip_dirs_includes_common(self):
        assert "node_modules" in SKIP_DIRS
        assert ".git" in SKIP_DIRS
        assert "__pycache__" in SKIP_DIRS

    def test_skip_filenames_includes_docs(self):
        assert "README.md" in SKIP_FILENAMES
        assert "AGENTS.md" in SKIP_FILENAMES

    def test_skip_files_includes_lock_files(self):
        assert "package-lock.json" in SKIP_FILES
        assert "yarn.lock" in SKIP_FILES

    def test_mermaid_prompt_exists(self):
        assert len(MERMAID_PROMPT) > 0
        assert "Mermaid" in MERMAID_PROMPT

    def test_constants_exist(self):
        assert MAX_COPILOT_CONCURRENT > 0
        assert MAX_KROKI_CONCURRENT > 0


# ---------------------------------------------------------------------------
# Edge cases / integration
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_split_diagram_with_subgraph(self):
        """Subgraph lines should not trigger a split."""
        text = (
            "graph TD\n"
            "    subgraph cluster\n"
            "        A --> B\n"
            "    end\n"
            "    C --> D"
        )
        result = split_mermaid_diagrams(text)
        assert len(result) == 1

    def test_split_preserves_empty_lines(self):
        text = "graph TD\n\n    A --> B\n\n    B --> C"
        result = split_mermaid_diagrams(text)
        assert len(result) == 1
        assert "\n\n" in result[0]

    def test_split_graph_keyword_in_node_label(self):
        """'graph' inside a node label (indented) should not trigger a split."""
        text = (
            "graph TD\n"
            "    A[\"graph visualization\"] --> B\n"
            "    B --> C"
        )
        result = split_mermaid_diagrams(text)
        assert len(result) == 1

    def test_crawl_repo_symlinks(self, tmp_path):
        """Symlinks should be handled without crashing."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "real.py").write_text("x = 1\n")
        try:
            (repo / "link.py").symlink_to(repo / "real.py")
        except OSError:
            pytest.skip("Symlinks not supported")

        result = crawl_repo(repo, extensions={".py"})
        names = {e.repo_relative.name for e in result.files}
        assert "real.py" in names

    def test_crawl_empty_repo(self, tmp_path):
        """Empty repo should return empty result without error."""
        repo = tmp_path / "empty_repo"
        repo.mkdir()
        result = crawl_repo(repo, extensions={".py"})
        assert len(result.files) == 0
        assert result.repo_name == "empty_repo"
