#!/usr/bin/env python3
"""
crawlers — Codebase diagram crawler for Obscura.

Walks a target repository, reads each source file, and outputs a Mermaid
diagram Markdown file for every file into the vault's crawlers/ directory.

The output mirrors the repo's directory structure:

    vault/crawlers/{repo_name}/src/auth.py.md
    vault/crawlers/{repo_name}/src/models/user.py.md
    vault/crawlers/{repo_name}/INDEX.md

Usage:
    python3 crawlers.py --repo ~/git/MyRepo
    python3 crawlers.py --repo ~/git/MyRepo --output ./crawlers
    python3 crawlers.py --repo ~/git/MyRepo --dry-run
    python3 crawlers.py --repo ~/git/MyRepo --extensions .py .ts .js
"""

from __future__ import annotations

import argparse
import os
import sys
import subprocess
import tempfile
import textwrap
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from copilot_models import guard_automation


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_EXTENSIONS: set[str] = {
    ".py", ".js", ".ts", ".tsx", ".jsx",
    ".java", ".kt", ".go", ".rs", ".rb",
    ".c", ".cpp", ".h", ".hpp", ".cs",
    ".swift", ".m", ".mm",
    ".sql", ".graphql", ".gql",
    ".yaml", ".yml", ".json", ".toml",
    ".sh", ".bash", ".zsh",
    ".md",
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

MAX_FILE_SIZE = 100_000  # 100KB — skip generated/minified files
COPILOT_ALIAS = "copilot_batch_diagrammer"  # Alias for copilot model selection


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
    files: list[FileEntry] = field(default_factory=list)
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

            if fname in SKIP_FILES:
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
# Mermaid + SVG generation
# ---------------------------------------------------------------------------

def _run_copilot_for_mermaid(code: str) -> str:
    """
    Returns ONLY Mermaid code (no markdown fences).
    Assumes `copilot -p` prints model output to stdout.
    """
    prompt = (
        "Generate Mermaid diagrams for the following source code.\n"
        "Rules:\n"
        "- Output ONLY Mermaid code.\n"
        "- Split every method into subsequent graphs.\n"
        "- Do NOT use markdown fences.\n\n"
        f"{code}"
    )
    model_id = guard_automation(COPILOT_ALIAS)
    result = subprocess.run(
        ["copilot", "-p", prompt, "--model", model_id],
        capture_output=True,
        text=True,
        check=False,
    )
    print(result)

    mermaid = (result.stdout or "").strip()
    if not mermaid:
        stderr = (result.stderr or "").strip()
        raise RuntimeError(f"Copilot returned empty output. stderr:\n{stderr}")

    starters = (
        "graph", "flowchart", "sequenceDiagram", "classDiagram", "stateDiagram",
        "erDiagram", "journey", "gantt", "pie", "mindmap", "timeline"
    )

    # Trim any leading helpful prose if it appears
    if not mermaid.lstrip().startswith(starters):
        lines = mermaid.splitlines()
        for i, line in enumerate(lines):
            if line.strip().startswith(starters):
                mermaid = "\n".join(lines[i:]).strip()
                break

    return mermaid


def _render_svg_with_mmdc(mermaid: str) -> str | None:
    """
    Render SVG using Mermaid CLI (mmdc). Returns SVG text or None if mmdc not available/fails.
    """
    try:
        subprocess.run(["mmdc", "--version"], capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return None

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        in_path = td_path / "diagram.mmd"
        out_path = td_path / "diagram.svg"

        in_path.write_text(mermaid, encoding="utf-8")

        proc = subprocess.run(
            ["mmdc", "-i", str(in_path), "-o", str(out_path), "-b", "transparent"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0 or not out_path.exists():
            return None

        return out_path.read_text(encoding="utf-8").strip()


def _render_svg_with_kroki(mermaid: str) -> str:
    """
    Render SVG via Kroki (public service). No auth.
    """
    encoded = urllib.parse.quote(mermaid, safe="")
    url = f"https://kroki.io/mermaid/svg/{encoded}"

    req = urllib.request.Request(url, headers={"User-Agent": "obscura-crawlers/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        svg = resp.read().decode("utf-8", errors="replace")

    if "<svg" not in svg:
        raise RuntimeError("Kroki did not return SVG.")
    return svg.strip()


def generate_stub(entry: FileEntry) -> str:
    """
    Returns Markdown that contains:
    - Mermaid in a fenced code block
    - Inline <svg>...</svg> (NOT fenced) so Markdown preview renders it
    """
    code = Path(entry.absolute).read_text(encoding="utf-8")
    mermaid = _run_copilot_for_mermaid(code)

    svg = _render_svg_with_mmdc(mermaid)
    if svg is None:
        svg = _render_svg_with_kroki(mermaid)

    return textwrap.dedent(f"""\
    # Diagram: {entry.repo_relative}

    > Auto-generated by Obscura crawlers

    ## Mermaid
    ```mermaid
    {mermaid}
    ```

    ## SVG
    {svg}
    """)


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
# Output writing
# ---------------------------------------------------------------------------

def write_output(
    result: CrawlResult,
    output_dir: Path,
    dry_run: bool = False,
) -> int:
    """Write diagram Markdown files and index to the output directory."""
    out = output_dir / result.repo_name
    written = 0

    if dry_run:
        print(f"\n[dry-run] Would write to: {out}/")
        print(f"[dry-run] {len(result.files)} diagram files + INDEX.md")
        for entry in result.files[:20]:
            print(f"  {entry.repo_relative}.md")
        if len(result.files) > 20:
            print(f"  ... and {len(result.files) - 20} more")
        return 0

    for entry in result.files:
        md_path = out / f"{entry.repo_relative}.md"
        md_path.parent.mkdir(parents=True, exist_ok=True)

        # Don't overwrite existing files (may have agent-generated content)
        if md_path.exists():
            continue

        try:
            md_path.write_text(generate_stub(entry), encoding="utf-8")
            written += 1
        except Exception as e:
            # Keep going. One file shouldn't tank the whole crawl.
            print(f"[warn] Failed for {entry.repo_relative}: {e}", file=sys.stderr)

    index_path = out / "INDEX.md"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(generate_index(result), encoding="utf-8")
    written += 1

    return written


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="crawlers",
        description="Crawl a codebase and generate Mermaid+SVG diagram markdown for Obsidian.",
    )
    p.add_argument("--repo", required=True, type=Path, help="Path to the target repository to crawl.")
    p.add_argument("--output", type=Path, default=None, help="Output directory. Defaults to ./crawlers next to this file.")
    p.add_argument("--extensions", nargs="+", default=None, help="File extensions to include (e.g. .py .ts .js).")
    p.add_argument("--max-size", type=int, default=MAX_FILE_SIZE, help=f"Max file size in bytes (default: {MAX_FILE_SIZE:,}).")
    p.add_argument("--dry-run", action="store_true", help="Show what would be crawled without writing files.")
    p.add_argument("--stats", action="store_true", help="Print crawl statistics.")
    return p


def main(argv: list[str] | None = None) -> int:
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

    written = write_output(result, output_dir, dry_run=args.dry_run)

    if not args.dry_run:
        print(f"\nWrote {written} files to: {output_dir / result.repo_name}/")
        print(f"Open in Obsidian: {output_dir / result.repo_name / 'INDEX.md'}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
