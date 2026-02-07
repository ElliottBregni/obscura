"""Tests for crawlers.py — Mermaid diagram crawler."""

from __future__ import annotations

import textwrap
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from crawlers import (
    FileEntry,
    CrawlResult,
    crawl_repo,
    split_mermaid_diagrams,
    generate_stub,
    generate_index,
    write_output,
    build_parser,
    main,
    _strip_markdown_fences,
    _trim_leading_prose,
    _process_entry,
    _init_semaphores,
    _run_copilot_for_mermaid,
    _render_svg_with_kroki,
    _render_svg_with_mmdc,
    _render_single_diagram,
    SKIP_DIRS,
    SKIP_FILES,
    SKIP_FILENAMES,
    DEFAULT_EXTENSIONS,
    DEFAULT_WORKERS,
    MAX_FILE_SIZE,
    MAX_COPILOT_CONCURRENT,
    MAX_KROKI_CONCURRENT,
    COPILOT_ALIAS,
)


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
# _run_copilot_for_mermaid
# ---------------------------------------------------------------------------

class TestRunCopilotForMermaid:
    @patch("crawlers.subprocess.run")
    def test_returns_stdout(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="graph TD\n    A --> B",
            stderr="",
            returncode=0,
        )
        result = _run_copilot_for_mermaid("def foo(): pass")
        assert result == "graph TD\n    A --> B"

    @patch("crawlers.subprocess.run")
    def test_raises_on_empty_output(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="",
            stderr="some error",
            returncode=1,
        )
        with pytest.raises(RuntimeError, match="empty output"):
            _run_copilot_for_mermaid("def foo(): pass")

    @patch("crawlers.subprocess.run")
    def test_calls_copilot_with_p_flag(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="graph TD\n    A-->B",
            stderr="",
            returncode=0,
        )
        _run_copilot_for_mermaid("code")
        args = mock_run.call_args
        cmd = args[0][0]
        assert cmd[0] == "copilot"
        assert cmd[1] == "-p"

    @patch("crawlers.subprocess.run")
    def test_uses_model_from_alias_system(self, mock_run):
        """Model ID must come from copilot_models, not be hardcoded."""
        from copilot_models import get_model_id
        mock_run.return_value = MagicMock(
            stdout="graph TD\n    A-->B",
            stderr="",
            returncode=0,
        )
        _run_copilot_for_mermaid("code")
        cmd = mock_run.call_args[0][0]
        assert "--model" in cmd
        model_idx = cmd.index("--model") + 1
        expected = get_model_id(COPILOT_ALIAS)
        assert cmd[model_idx] == expected


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
# generate_stub
# ---------------------------------------------------------------------------

class TestGenerateStub:
    @patch("crawlers._render_single_diagram")
    @patch("crawlers._run_copilot_for_mermaid")
    def test_single_diagram(self, mock_copilot, mock_render, file_entry):
        mock_copilot.return_value = "graph TD\n    A --> B"
        mock_render.return_value = '<svg xmlns="http://www.w3.org/2000/svg">test</svg>'

        md = generate_stub(file_entry)

        assert "# Diagram: sample.py" in md
        assert "```mermaid" in md
        assert "graph TD" in md
        assert "A --> B" in md
        assert "```" in md
        assert '<svg xmlns="http://www.w3.org/2000/svg">test</svg>' in md
        assert "Auto-generated by Obscura crawlers" in md

    @patch("crawlers._render_single_diagram")
    @patch("crawlers._run_copilot_for_mermaid")
    def test_multiple_diagrams(self, mock_copilot, mock_render, file_entry):
        mock_copilot.return_value = "graph TD\n    A --> B\nflowchart LR\n    C --> D"
        mock_render.return_value = '<svg>test</svg>'

        md = generate_stub(file_entry)

        assert "## Diagram 1" in md
        assert "## Diagram 2" in md
        assert md.count("```mermaid") == 2
        assert md.count("<svg>test</svg>") == 2

    @patch("crawlers._render_single_diagram")
    @patch("crawlers._run_copilot_for_mermaid")
    def test_render_failure_includes_note(self, mock_copilot, mock_render, file_entry):
        mock_copilot.return_value = "graph TD\n    A --> B"
        mock_render.return_value = None

        md = generate_stub(file_entry)

        assert "```mermaid" in md
        assert "graph TD" in md
        assert "SVG rendering failed" in md

    @patch("crawlers._run_copilot_for_mermaid")
    def test_no_valid_diagrams(self, mock_copilot, file_entry):
        mock_copilot.return_value = ""

        md = generate_stub(file_entry)

        assert "No valid Mermaid diagrams" in md

    @patch("crawlers._render_single_diagram")
    @patch("crawlers._run_copilot_for_mermaid")
    def test_svg_not_fenced(self, mock_copilot, mock_render, file_entry):
        mock_copilot.return_value = "graph TD\n    A --> B"
        svg_content = '<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>'
        mock_render.return_value = svg_content

        md = generate_stub(file_entry)

        # SVG should appear as raw inline, not inside a code fence
        lines = md.splitlines()
        for i, line in enumerate(lines):
            if "<svg" in line:
                # Check the line before is not a fence opener
                assert i == 0 or "```" not in lines[i - 1]
                # Check the line after (if exists) is not a fence closer
                if i + 1 < len(lines):
                    assert "```" not in lines[i + 1] or "mermaid" in lines[i + 1]

    @patch("crawlers._render_single_diagram")
    @patch("crawlers._run_copilot_for_mermaid")
    def test_single_diagram_uses_mermaid_header(self, mock_copilot, mock_render, file_entry):
        """When there's only one diagram, section header should be 'Mermaid' not 'Diagram 1'."""
        mock_copilot.return_value = "graph TD\n    A --> B"
        mock_render.return_value = '<svg>ok</svg>'

        md = generate_stub(file_entry)

        assert "## Mermaid" in md
        assert "## Diagram 1" not in md


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
# write_output
# ---------------------------------------------------------------------------

class TestWriteOutput:
    @patch("crawlers.generate_stub")
    def test_writes_files(self, mock_stub, sample_repo, tmp_path):
        mock_stub.return_value = "# Diagram\n\nstub content"
        result = crawl_repo(sample_repo, extensions={".py"})
        output_dir = tmp_path / "output"

        written = write_output(result, output_dir, workers=2)

        assert written > 0
        index_path = output_dir / "my_repo" / "INDEX.md"
        assert index_path.exists()

        # Check at least one diagram file was written
        main_md = output_dir / "my_repo" / "main.py.md"
        assert main_md.exists()
        assert main_md.read_text() == "# Diagram\n\nstub content"

    @patch("crawlers.generate_stub")
    def test_skips_existing_files(self, mock_stub, sample_repo, tmp_path):
        mock_stub.return_value = "# New content"
        result = crawl_repo(sample_repo, extensions={".py"})
        output_dir = tmp_path / "output"

        # Pre-create a file
        existing = output_dir / "my_repo" / "main.py.md"
        existing.parent.mkdir(parents=True, exist_ok=True)
        existing.write_text("# Existing content")

        write_output(result, output_dir, workers=2)

        # Should NOT be overwritten
        assert existing.read_text() == "# Existing content"

    def test_dry_run_writes_nothing(self, sample_repo, tmp_path):
        result = crawl_repo(sample_repo, extensions={".py"})
        output_dir = tmp_path / "output"

        written = write_output(result, output_dir, dry_run=True)

        assert written == 0
        assert not (output_dir / "my_repo").exists()

    @patch("crawlers.generate_stub")
    def test_continues_on_failure(self, mock_stub, sample_repo, tmp_path):
        import threading
        call_count_lock = threading.Lock()
        call_count = [0]

        def stub_side_effect(entry):
            with call_count_lock:
                call_count[0] += 1
                n = call_count[0]
            if n == 1:
                raise RuntimeError("Simulated failure")
            return "# OK"

        mock_stub.side_effect = stub_side_effect
        result = crawl_repo(sample_repo, extensions={".py"})
        output_dir = tmp_path / "output"

        written = write_output(result, output_dir, workers=2)

        # Should have written some files despite the failure
        assert written >= 1  # At least INDEX.md

    @patch("crawlers.generate_stub")
    def test_creates_nested_dirs(self, mock_stub, sample_repo, tmp_path):
        mock_stub.return_value = "# Diagram"
        result = crawl_repo(sample_repo, extensions={".py"})
        output_dir = tmp_path / "output"

        write_output(result, output_dir, workers=2)

        helper_md = output_dir / "my_repo" / "sub" / "helper.py.md"
        assert helper_md.exists()

    @patch("crawlers.generate_stub")
    def test_workers_1_sequential(self, mock_stub, sample_repo, tmp_path):
        """workers=1 should still work (sequential fallback)."""
        mock_stub.return_value = "# Diagram"
        result = crawl_repo(sample_repo, extensions={".py"})
        output_dir = tmp_path / "output"

        written = write_output(result, output_dir, workers=1)
        assert written > 0

    @patch("crawlers.generate_stub")
    def test_parallel_actually_runs_concurrently(self, mock_stub, sample_repo, tmp_path):
        """With workers>1, tasks should overlap (not strictly sequential)."""
        import threading
        active = [0]
        peak = [0]
        lock = threading.Lock()

        def slow_stub(entry):
            with lock:
                active[0] += 1
                if active[0] > peak[0]:
                    peak[0] = active[0]
            time.sleep(0.05)
            with lock:
                active[0] -= 1
            return "# Diagram"

        mock_stub.side_effect = slow_stub
        result = crawl_repo(sample_repo, extensions={".py"})
        output_dir = tmp_path / "output"

        write_output(result, output_dir, workers=3)

        # With 3 files and 3 workers, peak concurrency should be > 1
        # (unless the machine is extremely slow, but 50ms sleep makes this reliable)
        assert peak[0] >= 2, f"Expected concurrent execution, but peak was {peak[0]}"

    @patch("crawlers.generate_stub")
    def test_all_files_written_with_threading(self, mock_stub, sample_repo, tmp_path):
        """Every file should be written regardless of thread scheduling."""
        mock_stub.return_value = "# Diagram"
        result = crawl_repo(sample_repo, extensions={".py"})
        output_dir = tmp_path / "output"

        written = write_output(result, output_dir, workers=4)

        # All files + INDEX.md
        expected = len(result.files) + 1
        assert written == expected

    @patch("crawlers.generate_stub")
    def test_no_files_to_process(self, mock_stub, sample_repo, tmp_path):
        """When all files already exist, should only write INDEX.md."""
        mock_stub.return_value = "# Diagram"
        result = crawl_repo(sample_repo, extensions={".py"})
        output_dir = tmp_path / "output"

        # Pre-create all files
        for entry in result.files:
            md_path = output_dir / "my_repo" / f"{entry.repo_relative}.md"
            md_path.parent.mkdir(parents=True, exist_ok=True)
            md_path.write_text("# Existing")

        written = write_output(result, output_dir, workers=2)

        assert written == 1  # Only INDEX.md
        mock_stub.assert_not_called()


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

    @patch("crawlers.write_output")
    def test_main_dry_run(self, mock_write, sample_repo, capsys):
        mock_write.return_value = 0
        ret = main(["--repo", str(sample_repo), "--dry-run"])
        assert ret == 0
        mock_write.assert_called_once()
        _, kwargs = mock_write.call_args
        assert kwargs.get("dry_run") is True or mock_write.call_args[0][2] is True

    def test_main_nonexistent_repo(self):
        ret = main(["--repo", "/nonexistent/path/xyzzy"])
        assert ret == 1

    @patch("crawlers.write_output")
    def test_main_stats(self, mock_write, sample_repo, capsys):
        mock_write.return_value = 0
        ret = main(["--repo", str(sample_repo), "--stats", "--dry-run"])
        assert ret == 0
        captured = capsys.readouterr()
        assert "Crawl Stats" in captured.out

    @patch("crawlers.write_output")
    def test_main_custom_extensions(self, mock_write, sample_repo):
        mock_write.return_value = 0
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

    @patch("crawlers._render_single_diagram")
    @patch("crawlers._run_copilot_for_mermaid")
    def test_generate_stub_with_fenced_copilot_output(self, mock_copilot, mock_render, file_entry):
        """Copilot wrapping output in fences should still work."""
        mock_copilot.return_value = "```mermaid\ngraph TD\n    A --> B\n```"
        mock_render.return_value = '<svg>ok</svg>'

        md = generate_stub(file_entry)
        assert "```mermaid" in md
        assert "graph TD" in md
        assert "<svg>ok</svg>" in md

    @patch("crawlers._render_single_diagram")
    @patch("crawlers._run_copilot_for_mermaid")
    def test_generate_stub_multiple_with_partial_failure(self, mock_copilot, mock_render, file_entry):
        """If one diagram fails to render, others should still have SVG."""
        mock_copilot.return_value = "graph TD\n    A --> B\nflowchart LR\n    C --> D"

        call_count = 0

        def render_side_effect(mermaid):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return None  # First diagram fails
            return '<svg>ok</svg>'

        mock_render.side_effect = render_side_effect

        md = generate_stub(file_entry)

        assert "SVG rendering failed" in md
        assert "<svg>ok</svg>" in md
        assert md.count("```mermaid") == 2


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

class TestRateLimiting:
    def test_init_semaphores(self):
        """_init_semaphores should create working semaphores."""
        import crawlers
        _init_semaphores(copilot_limit=3, kroki_limit=2)
        assert crawlers._copilot_sem is not None
        assert crawlers._kroki_sem is not None
        # Clean up
        crawlers._copilot_sem = None
        crawlers._kroki_sem = None

    def test_constants_exist(self):
        assert MAX_COPILOT_CONCURRENT > 0
        assert MAX_KROKI_CONCURRENT > 0

    @patch("crawlers.subprocess.run")
    def test_copilot_respects_semaphore(self, mock_run):
        """Copilot calls should acquire/release the semaphore."""
        import crawlers
        mock_run.return_value = MagicMock(
            stdout="graph TD\n    A-->B", stderr="", returncode=0,
        )

        _init_semaphores(copilot_limit=2, kroki_limit=2)
        try:
            # Should work fine — semaphore has capacity
            result = _run_copilot_for_mermaid("code")
            assert "graph TD" in result
        finally:
            crawlers._copilot_sem = None
            crawlers._kroki_sem = None

    @patch("crawlers.urllib.request.urlopen")
    def test_kroki_respects_semaphore(self, mock_urlopen):
        """Kroki calls should acquire/release the semaphore."""
        import crawlers
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'<svg>ok</svg>'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        _init_semaphores(copilot_limit=2, kroki_limit=2)
        try:
            result = _render_svg_with_kroki("graph TD\n    A-->B")
            assert "<svg>" in result
        finally:
            crawlers._copilot_sem = None
            crawlers._kroki_sem = None

    @patch("crawlers.subprocess.run")
    def test_copilot_semaphore_limits_concurrency(self, mock_run):
        """Even with many threads, only N copilot calls should run at once."""
        import crawlers
        import threading

        active = [0]
        peak = [0]
        lock = threading.Lock()

        def slow_copilot(*args, **kwargs):
            with lock:
                active[0] += 1
                if active[0] > peak[0]:
                    peak[0] = active[0]
            time.sleep(0.05)
            with lock:
                active[0] -= 1
            return MagicMock(stdout="graph TD\n    A-->B", stderr="", returncode=0)

        mock_run.side_effect = slow_copilot

        _init_semaphores(copilot_limit=2, kroki_limit=2)
        try:
            threads = []
            for _ in range(10):
                t = threading.Thread(target=_run_copilot_for_mermaid, args=("code",))
                threads.append(t)
                t.start()
            for t in threads:
                t.join()

            assert peak[0] <= 2, f"Expected max 2 concurrent, but peak was {peak[0]}"
        finally:
            crawlers._copilot_sem = None
            crawlers._kroki_sem = None

    def test_no_semaphore_still_works(self):
        """When semaphores are None (not initialized), functions should still work."""
        import crawlers
        crawlers._copilot_sem = None
        crawlers._kroki_sem = None

        with patch("crawlers.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout="graph TD\n    A-->B", stderr="", returncode=0,
            )
            result = _run_copilot_for_mermaid("code")
            assert "graph TD" in result

    @patch("crawlers.generate_stub")
    def test_write_output_inits_semaphores(self, mock_stub, sample_repo, tmp_path):
        """write_output should initialize semaphores before processing."""
        import crawlers
        crawlers._copilot_sem = None
        crawlers._kroki_sem = None

        mock_stub.return_value = "# Diagram"
        result = crawl_repo(sample_repo, extensions={".py"})
        output_dir = tmp_path / "output"

        write_output(result, output_dir, workers=100)

        # Semaphores should have been initialized
        assert crawlers._copilot_sem is not None
        assert crawlers._kroki_sem is not None
        # Clean up
        crawlers._copilot_sem = None
        crawlers._kroki_sem = None
