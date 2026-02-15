#!/usr/bin/env python3
"""
crawlers — Codebase diagram crawler for Obscura.

Walks a target repository, reads each source file, generates Mermaid diagrams
via the copilot-sdk (ObscuraClient), renders each diagram to SVG, and writes
Markdown files that embed both fenced Mermaid and inline <svg> tags.

The output mirrors the repo's directory structure:

    <output>/crawlers/{repo_name}/src/auth.py.md
    <output>/crawlers/{repo_name}/src/models/user.py.md
    <output>/crawlers/{repo_name}/INDEX.md

Usage:
    python3 crawlers.py --repo ~/git/MyRepo
    python3 crawlers.py --repo ~/git/MyRepo --output ./crawlers
    python3 crawlers.py --repo ~/git/MyRepo --dry-run
    python3 crawlers.py --repo ~/git/MyRepo --extensions .py .ts .js
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import subprocess
import sys
import tempfile
import urllib.request

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, override
from urllib.error import HTTPError

from sdk.internal.types import AgentContext
from sdk.agent.agent import BaseAgent


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_EXTENSIONS: set[str] = {
    ".py", ".js", ".ts", ".jsx", ".tsx",
    ".java", ".go", ".rs", ".rb", ".php",
    ".c", ".cpp", ".h", ".hpp", ".cs",
    ".swift", ".kt", ".scala", ".sh",
    ".yml", ".yaml", ".toml",
}

SKIP_DIRS: set[str] = {
    ".git", ".github", ".claude", ".cursor",
    "node_modules", "__pycache__", ".pytest_cache",
    ".venv", "venv", "env", ".env",
    ".tox", ".mypy_cache", ".ruff_cache",
    "dist", "build", "target", "out",
    ".next", ".nuxt", ".svelte-kit",
    "vendor", "Pods", ".gradle",
    "coverage", "htmlcov", ".nyc_output",
    ".idea", ".vscode",
}

SKIP_FILES: set[str] = {
    ".DS_Store", "Thumbs.db",
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "Pipfile.lock", "poetry.lock",
    "Cargo.lock", "go.sum",
}

# Skip doc/non-code files that produce garbage Mermaid
SKIP_FILENAMES: set[str] = {
    "README.md", "readme.md", "AGENTS.md",
    "CHANGELOG.md", "CONTRIBUTING.md", "LICENSE",
    "LICENSE.md", "LICENSE.txt",
}

MAX_FILE_SIZE = 100_000  # 100KB — skip generated/minified files
DEFAULT_WORKERS = 4      # Concurrency for async tasks
MAX_COPILOT_CONCURRENT = 8   # Max concurrent SDK calls
MAX_KROKI_CONCURRENT = 6     # Max concurrent Kroki HTTP requests
COPILOT_ALIAS = "copilot_batch_diagrammer"  # Alias for copilot model selection

# Diagram type headers that start a valid Mermaid diagram
DIAGRAM_STARTERS = (
    "graph", "flowchart", "sequenceDiagram", "classDiagram",
    "stateDiagram", "erDiagram", "journey", "gantt",
    "pie", "mindmap", "timeline",
)

# Regex to detect a line that starts a new Mermaid diagram
_DIAGRAM_START_RE = re.compile(
    r"^(?:" + "|".join(DIAGRAM_STARTERS) + r")\b",
    re.IGNORECASE,
)

# Mermaid prompt sent to copilot
MERMAID_PROMPT = (
    "Generate Mermaid diagrams for the following source code.\n"
    "Rules:\n"
    "- Output ONLY valid Mermaid diagram code, nothing else.\n"
    "- Do NOT wrap output in markdown fences (no ```).\n"
    "- Do NOT include any explanatory text, comments, or prose.\n"
    "- If generating multiple diagrams, each must start with a valid "
    "Mermaid diagram type keyword (graph, flowchart, sequenceDiagram, "
    "classDiagram, stateDiagram, erDiagram, journey, gantt, pie, "
    "mindmap, timeline).\n"
    "- Each diagram must be syntactically complete and valid.\n\n"
)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class FileEntry:
    """A source file discovered in the target repo."""
    repo_relative: Path
    absolute: Path
    extension: str
    size: int


@dataclass
class CrawlResult:
    """Result of crawling a repository."""
    repo_name: str
    repo_path: Path
    files: list[FileEntry] = field(default_factory=lambda: list[FileEntry]())
    skipped_dirs: int = 0
    skipped_files: int = 0
    skipped_size: int = 0
    skipped_ext: int = 0

    @property
    def total_discovered(self) -> int:
        return len(self.files) + self.skipped_files + self.skipped_size + self.skipped_ext


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

def crawl_repo(
    repo_path: Path,
    extensions: set[str] | None = None,
    skip_dirs: set[str] | None = None,
    max_size: int = MAX_FILE_SIZE,
) -> CrawlResult:
    """Walk a repository and discover all source files."""
    repo_path = repo_path.resolve()
    if not repo_path.is_dir():
        raise FileNotFoundError(f"Repository not found: {repo_path}")

    exts = extensions or DEFAULT_EXTENSIONS
    dirs_to_skip = skip_dirs or SKIP_DIRS
    result = CrawlResult(repo_name=repo_path.name, repo_path=repo_path)

    for root, dirs, files in os.walk(repo_path, topdown=True):
        before = len(dirs)
        dirs[:] = [d for d in dirs if d not in dirs_to_skip and not d.startswith(".")]
        result.skipped_dirs += before - len(dirs)

        for fname in sorted(files):
            fpath = Path(root) / fname

            if fname in SKIP_FILES or fname in SKIP_FILENAMES:
                result.skipped_files += 1
                continue

            ext = fpath.suffix.lower()
            if ext not in exts:
                result.skipped_ext += 1
                continue

            try:
                size = fpath.stat().st_size
            except OSError:
                result.skipped_files += 1
                continue

            if size > max_size:
                result.skipped_size += 1
                continue

            if size == 0:
                result.skipped_files += 1
                continue

            result.files.append(FileEntry(
                repo_relative=fpath.relative_to(repo_path),
                absolute=fpath,
                extension=ext,
                size=size,
            ))

    return result


# ---------------------------------------------------------------------------
# Mermaid helpers
# ---------------------------------------------------------------------------

def _strip_markdown_fences(text: str) -> str:
    """Remove ```mermaid ... ``` fences that copilot sometimes adds."""
    lines = text.splitlines()
    out: list[str] = []
    inside_fence = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            inside_fence = not inside_fence
            continue
        out.append(line)
    return "\n".join(out).strip()

def _trim_leading_prose(text: str) -> str:
    """Trim any leading non-Mermaid prose before the first diagram header."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if _DIAGRAM_START_RE.match(line.strip()):
            return "\n".join(lines[i:]).strip()
    return text


def split_mermaid_diagrams(text: str) -> list[str]:
    """
    Split a block of text that may contain multiple Mermaid diagrams
    glued together into individual diagram strings.

    Splits on lines that start with a valid diagram type keyword.
    Returns a list of individual diagram strings (at least one).
    """
    text = _strip_markdown_fences(text)
    text = _trim_leading_prose(text)

    if not text.strip():
        return []

    lines = text.splitlines()
    diagrams: list[str] = []
    current: list[str] = []

    for line in lines:
        if _DIAGRAM_START_RE.match(line.strip()) and current:
            diagram = "\n".join(current).strip()
            if diagram:
                diagrams.append(diagram)
            current = [line]
        else:
            current.append(line)

    if current:
        diagram = "\n".join(current).strip()
        if diagram:
            diagrams.append(diagram)

    return diagrams


# ---------------------------------------------------------------------------
# SVG rendering (sync — wrapped in asyncio.to_thread by the agent)
# ---------------------------------------------------------------------------

def _render_svg_with_mmdc(mermaid: str) -> str | None:
    """Render SVG using Mermaid CLI (mmdc). Returns SVG text or None."""
    try:
        subprocess.run(
            ["mmdc", "--version"],
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        return None

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        in_path = td_path / "diagram.mmd"
        out_path = td_path / "diagram.svg"

        in_path.write_text(mermaid, encoding="utf-8")

        proc = subprocess.run(
            ["mmdc", "-i", str(in_path), "-o", str(out_path), "-b", "transparent"],
            capture_output=True, text=True, check=False,
        )
        if proc.returncode != 0 or not out_path.exists():
            return None

        return out_path.read_text(encoding="utf-8").strip()


def _render_svg_with_kroki(mermaid: str) -> str:
    """Render SVG via Kroki using POST (avoids URL length limits)."""
    url = "https://kroki.io/mermaid/svg"
    data = mermaid.encode("utf-8")

    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "text/plain; charset=utf-8",
            "User-Agent": "obscura-crawlers/2.0",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            svg = resp.read().decode("utf-8", errors="replace")
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Kroki HTTP {e.code}: {e.reason}\n{body[:500]}") from e

    if "<svg" not in svg:
        raise RuntimeError("Kroki did not return SVG.")
    return svg.strip()


def _render_single_diagram(mermaid: str) -> str | None:
    """
    Try to render a single Mermaid diagram to SVG.
    Returns SVG string or None on failure.
    """
    svg = _render_svg_with_mmdc(mermaid)
    if svg is not None:
        return svg
    try:
        return _render_svg_with_kroki(mermaid)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Stub/Markdown generation (pure function, no I/O)
# ---------------------------------------------------------------------------

def _build_stub_markdown(entry: FileEntry, raw_mermaid: str) -> str:
    """Build the Markdown output from raw Mermaid text for a single file."""
    diagrams = split_mermaid_diagrams(raw_mermaid)

    if not diagrams:
        return (
            f"# Diagram: {entry.repo_relative}\n\n"
            "> Auto-generated by Obscura crawlers\n\n"
            "> No valid Mermaid diagrams were generated for this file.\n"
        )

    parts: list[str] = [
        f"# Diagram: {entry.repo_relative}\n",
        "",
        "> Auto-generated by Obscura crawlers",
        "",
    ]

    for i, diagram in enumerate(diagrams, 1):
        label = f"Diagram {i}" if len(diagrams) > 1 else "Mermaid"

        parts.append(f"## {label}")
        parts.append("")
        parts.append("```mermaid")
        parts.append(diagram)
        parts.append("```")
        parts.append("")

        svg = _render_single_diagram(diagram)
        if svg is not None:
            parts.append("### SVG")
            parts.append("")
            parts.append(svg)
        else:
            parts.append("> SVG rendering failed for this diagram.")
        parts.append("")

    return "\n".join(parts)


# Legacy sync wrapper — kept for backward compatibility in tests
def generate_stub(entry: FileEntry) -> str:
    """Generate Markdown for a single file (sync, uses subprocess).

    Deprecated: use DiagramCrawlerAgent for async SDK-based generation.
    """
    # Import here to avoid hard dependency when using the agent path
    from copilot_models import guard_automation

    code = Path(entry.absolute).read_text(encoding="utf-8")
    prompt = MERMAID_PROMPT + code
    model_id = guard_automation(COPILOT_ALIAS)

    result = subprocess.run(
        ["copilot", "-p", prompt, "--model", model_id],
        capture_output=True, text=True, check=False,
    )
    raw_mermaid = (result.stdout or "").strip()
    if not raw_mermaid:
        stderr = (result.stderr or "").strip()
        raise RuntimeError(f"Copilot returned empty output. stderr:\n{stderr}")

    return _build_stub_markdown(entry, raw_mermaid)


# ---------------------------------------------------------------------------
# Index generation
# ---------------------------------------------------------------------------

def generate_index(result: CrawlResult) -> str:
    """Generate the INDEX.md for a crawled repository."""
    lines: list[str] = [
        f"# crawlers Index: {result.repo_name}\n",
        "\n",
        "> Auto-generated by Obscura crawlers\n",
        ">\n",
        f"> Files: {len(result.files)} | "
        f"Skipped: {result.skipped_files + result.skipped_size + result.skipped_ext}\n",
        "\n",
    ]

    dirs: dict[str, list[FileEntry]] = {}
    for entry in result.files:
        parent = str(entry.repo_relative.parent)
        if parent == ".":
            parent = "(root)"
        dirs.setdefault(parent, []).append(entry)

    for dirname in sorted(dirs):
        lines.append(f"## {dirname}\n\n")
        for entry in sorted(dirs[dirname], key=lambda e: e.repo_relative.name):
            md_name = f"{entry.repo_relative}.md"
            lines.append(f"- [{entry.repo_relative.name}]({md_name})")
            lines.append(f"  ({entry.size:,} bytes)\n")
        lines.append("\n")

    lines.extend([
        "---\n",
        "\n",
        "## Stats\n",
        "\n",
        "| Metric | Count |\n",
        "|--------|-------|\n",
        f"| Source files | {len(result.files)} |\n",
        f"| Skipped (junk) | {result.skipped_files} |\n",
        f"| Skipped (too large) | {result.skipped_size} |\n",
        f"| Skipped (wrong ext) | {result.skipped_ext} |\n",
        f"| Directories pruned | {result.skipped_dirs} |\n",
    ])

    return "".join(lines)


# ---------------------------------------------------------------------------
# DiagramCrawlerAgent — APER agent using copilot-sdk
# ---------------------------------------------------------------------------

class DiagramCrawlerAgent(BaseAgent):
    """APER agent for crawling a repo and generating Mermaid diagrams.

    Uses ObscuraClient (copilot-sdk) instead of subprocess calls.

    - **Analyze**: Walk the repo, discover source files.
    - **Plan**: Filter to files that need processing (not already generated).
    - **Execute**: Generate Mermaid via SDK + render SVGs concurrently.
    - **Respond**: Write Markdown files to disk + generate index.
    """

    def __init__(
        self,
        client: Any,  # ObscuraClient
        repo_path: Path,
        output_dir: Path,
        *,
        extensions: set[str] | None = None,
        max_size: int = MAX_FILE_SIZE,
        max_concurrent: int = MAX_COPILOT_CONCURRENT,
        kroki_concurrent: int = MAX_KROKI_CONCURRENT,
        dry_run: bool = False,
    ) -> None:
        super().__init__(client, name="diagram_crawler")
        self._repo_path = repo_path
        self._output_dir = output_dir
        self._extensions = extensions
        self._max_size = max_size
        self._copilot_sem = asyncio.Semaphore(max_concurrent)
        self._kroki_sem = asyncio.Semaphore(kroki_concurrent)
        self._dry_run = dry_run

    @override
    async def analyze(self, ctx: AgentContext) -> None:
        """Walk the repo, discover source files."""
        ctx.analysis = crawl_repo(
            self._repo_path,
            extensions=self._extensions,
            max_size=self._max_size,
        )

    @override
    async def plan(self, ctx: AgentContext) -> None:
        """Determine which files need processing."""
        result: CrawlResult = ctx.analysis
        out = self._output_dir / result.repo_name
        to_process: list[FileEntry] = []
        for entry in result.files:
            md_path = out / f"{entry.repo_relative}.md"
            if not md_path.exists():
                to_process.append(entry)
        ctx.plan = to_process

    @override
    async def execute(self, ctx: AgentContext) -> None:
        """Generate diagrams for all planned files via SDK."""
        to_process: list[FileEntry] = ctx.plan

        if self._dry_run or not to_process:
            ctx.results = []
            return

        total = len(to_process)
        counter = 0

        async def process_one(entry: FileEntry) -> tuple[FileEntry, str | None]:
            nonlocal counter
            try:
                code = entry.absolute.read_text(encoding="utf-8")
                prompt = MERMAID_PROMPT + code

                print(f"  → sending {entry.repo_relative} ...", flush=True)
                async with self._copilot_sem:
                    response = await self._client.send(prompt)

                raw_mermaid = response.text.strip()
                if not raw_mermaid:
                    raise RuntimeError("SDK returned empty output")

                # Render SVGs in thread pool (sync subprocess/HTTP calls)
                content = await asyncio.to_thread(
                    _build_stub_markdown, entry, raw_mermaid,
                )

                counter += 1
                print(f"  ✓ [{counter}/{total}] {entry.repo_relative}", flush=True)
                return (entry, content)
            except asyncio.TimeoutError:
                counter += 1
                print(f"  ✗ [{counter}/{total}] {entry.repo_relative}: timeout", file=sys.stderr, flush=True)
                return (entry, None)
            except Exception as e:
                counter += 1
                print(f"  ✗ [{counter}/{total}] {entry.repo_relative}: {e}", file=sys.stderr, flush=True)
                return (entry, None)

        print(f"  Processing {total} files via copilot-sdk (max {self._copilot_sem._value} concurrent)...")
        ctx.results = await asyncio.gather(*[process_one(e) for e in to_process])

    @override
    async def respond(self, ctx: AgentContext) -> None:
        """Write all results to disk and generate index."""
        result: CrawlResult = ctx.analysis
        out = self._output_dir / result.repo_name
        written = 0

        if self._dry_run:
            print(f"\n[dry-run] Would write to: {out}/")
            print(f"[dry-run] {len(result.files)} diagram files + INDEX.md")
            for entry in result.files[:20]:
                print(f"  {entry.repo_relative}.md")
            if len(result.files) > 20:
                print(f"  ... and {len(result.files) - 20} more")
            ctx.response = 0
            return

        for entry, content in ctx.results:
            if content is None:
                continue
            md_path = out / f"{entry.repo_relative}.md"
            md_path.parent.mkdir(parents=True, exist_ok=True)
            md_path.write_text(content, encoding="utf-8")
            written += 1

        # Always write/update the index
        index_path = out / "INDEX.md"
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(generate_index(result), encoding="utf-8")
        written += 1

        ctx.response = written


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="crawlers",
        description="Crawl a codebase and generate Mermaid+SVG diagram markdown for Obsidian.",
    )
    p.add_argument("--repo", required=True, type=Path,
                    help="Path to the target repository to crawl.")
    p.add_argument("--output", type=Path, default=None,
                    help="Output directory. Defaults to ./crawlers next to this file.")
    p.add_argument("--extensions", nargs="+", default=None,
                    help="File extensions to include (e.g. .py .ts .js).")
    p.add_argument("--max-size", type=int, default=MAX_FILE_SIZE,
                    help=f"Max file size in bytes (default: {MAX_FILE_SIZE:,}).")
    p.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                    help=f"Max concurrent SDK calls (default: {DEFAULT_WORKERS}).")
    p.add_argument("--dry-run", action="store_true",
                    help="Show what would be crawled without writing files.")
    p.add_argument("--stats", action="store_true",
                    help="Print crawl statistics.")
    return p


async def async_main(argv: list[str] | None = None) -> int:
    """Async entry point — runs the DiagramCrawlerAgent."""
    parser = build_parser()
    args = parser.parse_args(argv)

    repo_path = args.repo.expanduser().resolve()
    if not repo_path.is_dir():
        print(f"Error: {repo_path} is not a directory.", file=sys.stderr)
        return 1

    if args.output:
        output_dir = args.output.expanduser().resolve()
    else:
        vault = Path(__file__).resolve().parent
        output_dir = vault / "crawlers"

    extensions = None
    if args.extensions:
        extensions = {e if e.startswith(".") else f".{e}" for e in args.extensions}

    print(f"Crawling: {repo_path}")

    # Dry-run and stats don't need the SDK
    if args.dry_run or args.stats:
        result = crawl_repo(repo_path, extensions=extensions, max_size=args.max_size)
        print(f"Found {len(result.files)} source files in {result.repo_name}")

        if args.stats or args.dry_run:
            print("\n--- Crawl Stats ---")
            print(f"  Source files:      {len(result.files)}")
            print(f"  Skipped (junk):    {result.skipped_files}")
            print(f"  Skipped (large):   {result.skipped_size}")
            print(f"  Skipped (ext):     {result.skipped_ext}")
            print(f"  Dirs pruned:       {result.skipped_dirs}")

            ext_counts: dict[str, int] = {}
            for entry in result.files:
                ext_counts[entry.extension] = ext_counts.get(entry.extension, 0) + 1
            if ext_counts:
                print("\n--- By Extension ---")
                for ext, count in sorted(ext_counts.items(), key=lambda x: -x[1]):
                    print(f"  {ext:10s} {count}")

        if args.dry_run:
            out = output_dir / result.repo_name
            print(f"\n[dry-run] Would write to: {out}/")
            print(f"[dry-run] {len(result.files)} diagram files + INDEX.md")
            for entry in result.files[:20]:
                print(f"  {entry.repo_relative}.md")
            if len(result.files) > 20:
                print(f"  ... and {len(result.files) - 20} more")
            return 0

    # Full run — use the SDK
    from sdk.client import ObscuraClient

    async with ObscuraClient(
        "copilot",
        model_alias=COPILOT_ALIAS,
        automation_safe=True,
    ) as client:
        agent = DiagramCrawlerAgent(
            client=client,
            repo_path=repo_path,
            output_dir=output_dir,
            extensions=extensions,
            max_size=args.max_size,
            max_concurrent=min(args.workers, MAX_COPILOT_CONCURRENT),
        )
        written = await agent.run()

    print(f"\nWrote {written} files to: {output_dir / repo_path.name}/")
    print(f"Open in Obsidian: {output_dir / repo_path.name / 'INDEX.md'}")

    return 0


def main(argv: list[str] | None = None) -> int:
    """Sync entry point — wraps async_main."""
    return asyncio.run(async_main(argv))


# Public testing aliases (placed at end to avoid forward refs)
strip_markdown_fences = _strip_markdown_fences
trim_leading_prose = _trim_leading_prose
build_stub_markdown = _build_stub_markdown
render_svg_with_kroki = _render_svg_with_kroki
render_svg_with_mmdc = _render_svg_with_mmdc
render_single_diagram = _render_single_diagram


if __name__ == "__main__":
    raise SystemExit(main())
