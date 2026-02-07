#!/usr/bin/env python3
"""
crawlers — Codebase diagram crawler for Obscura.

Walks a target repository, reads each source file, generates Mermaid diagrams
via `copilot -p`, renders each diagram to SVG, and writes Markdown files that
embed both fenced Mermaid and inline <svg> tags.

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
import os
import re
import sys
import subprocess
import tempfile
import threading
import urllib.parse
import urllib.request

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from copilot_models import guard_automation


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
DEFAULT_WORKERS = 4      # Thread pool size for parallel stub generation
MAX_COPILOT_CONCURRENT = 8   # Max concurrent copilot subprocess calls
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
# Rate-limiting semaphores (initialized per-run, safe for threading)
# ---------------------------------------------------------------------------

_copilot_sem: threading.Semaphore | None = None
_kroki_sem: threading.Semaphore | None = None


def _init_semaphores(
    copilot_limit: int = MAX_COPILOT_CONCURRENT,
    kroki_limit: int = MAX_KROKI_CONCURRENT,
) -> None:
    """Initialize rate-limiting semaphores. Call once before threaded work."""
    global _copilot_sem, _kroki_sem
    _copilot_sem = threading.Semaphore(copilot_limit)
    _kroki_sem = threading.Semaphore(kroki_limit)


# ---------------------------------------------------------------------------
# Mermaid + SVG generation
# ---------------------------------------------------------------------------

def _run_copilot_for_mermaid(code: str) -> str:
    """
    Call copilot to generate Mermaid diagrams from source code.
    Returns the raw Mermaid text (stdout only, not CompletedProcess).
    """
    prompt = (
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
        f"{code}"
    )

    model_id = guard_automation(COPILOT_ALIAS)

    sem = _copilot_sem
    if sem is not None:
        sem.acquire()
    try:
        result = subprocess.run(
            ["copilot", "-p", prompt, "--model", model_id],
            capture_output=True,
            text=True,
            check=False,
            )
    finally:
        if sem is not None:
            sem.release()

    mermaid = (result.stdout or "").strip()
    if not mermaid:
        stderr = (result.stderr or "").strip()
        raise RuntimeError(f"Copilot returned empty output. stderr:\n{stderr}")

    return mermaid


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
            "User-Agent": "obscura-crawlers/1.0",
        },
    )

    sem = _kroki_sem
    if sem is not None:
        sem.acquire()
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            svg = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else ""
        raise RuntimeError(f"Kroki HTTP {e.code}: {e.reason}\n{body[:500]}") from e
    finally:
        if sem is not None:
            sem.release()

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
# Stub generation
# ---------------------------------------------------------------------------

def generate_stub(entry: FileEntry) -> str:
    """
    Generate Markdown for a single file:
    - One ```mermaid fence per diagram
    - One inline <svg> per diagram (NOT fenced)
    - On render failure, include the Mermaid with a failure note
    """
    code = Path(entry.absolute).read_text(encoding="utf-8")
    raw_mermaid = _run_copilot_for_mermaid(code)
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

def _process_entry(
    entry: FileEntry,
    out: Path,
    counter: list[int],
    total: int,
    lock: threading.Lock,
) -> bool:
    """
    Process a single FileEntry: generate stub and write markdown.
    Returns True if a file was written, False otherwise.
    Thread-safe: uses lock only for the counter print and dir creation.
    """
    md_path = out / f"{entry.repo_relative}.md"

    # Don't overwrite existing files (may have agent-generated content)
    if md_path.exists():
        return False

    try:
        content = generate_stub(entry)
    except Exception as e:
        with lock:
            print(f"[warn] Failed for {entry.repo_relative}: {e}", file=sys.stderr)
        return False

    # Ensure parent dirs exist (mkdir is safe to call concurrently with exist_ok)
    md_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        md_path.write_text(content, encoding="utf-8")
    except Exception as e:
        with lock:
            print(f"[warn] Write failed for {entry.repo_relative}: {e}", file=sys.stderr)
        return False

    with lock:
        counter[0] += 1
        done = counter[0]
        print(f"  [{done}/{total}] {entry.repo_relative}", flush=True)

    return True


def write_output(
    result: CrawlResult,
    output_dir: Path,
    dry_run: bool = False,
    workers: int = DEFAULT_WORKERS,
) -> int:
    """Write diagram Markdown files and index to the output directory.

    Uses a ThreadPoolExecutor to process files in parallel since the
    bottleneck is subprocess calls (copilot) and HTTP (Kroki), both I/O-bound.
    """
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

    # Filter to only entries that need processing
    to_process: list[FileEntry] = []
    for entry in result.files:
        md_path = out / f"{entry.repo_relative}.md"
        md_path.parent.mkdir(parents=True, exist_ok=True)
        if not md_path.exists():
            to_process.append(entry)

    total = len(to_process)
    if total == 0:
        print("  All files already exist, skipping generation.")
    else:
        _init_semaphores()
        effective_copilot = min(workers, MAX_COPILOT_CONCURRENT)
        effective_kroki = min(workers, MAX_KROKI_CONCURRENT)
        print(
            f"  Processing {total} files with {workers} workers "
            f"(copilot limit: {effective_copilot}, kroki limit: {effective_kroki})..."
        )

        lock = threading.Lock()
        counter = [0]  # mutable container for thread-safe counting

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_process_entry, entry, out, counter, total, lock): entry
                for entry in to_process
            }
            for future in as_completed(futures):
                if future.result():
                    written += 1

    # Always write/update the index
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
    p.add_argument("--repo", required=True, type=Path,
                    help="Path to the target repository to crawl.")
    p.add_argument("--output", type=Path, default=None,
                    help="Output directory. Defaults to ./crawlers next to this file.")
    p.add_argument("--extensions", nargs="+", default=None,
                    help="File extensions to include (e.g. .py .ts .js).")
    p.add_argument("--max-size", type=int, default=MAX_FILE_SIZE,
                    help=f"Max file size in bytes (default: {MAX_FILE_SIZE:,}).")
    p.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                    help=f"Number of parallel workers (default: {DEFAULT_WORKERS}).")
    p.add_argument("--dry-run", action="store_true",
                    help="Show what would be crawled without writing files.")
    p.add_argument("--stats", action="store_true",
                    help="Print crawl statistics.")
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

    written = write_output(result, output_dir, dry_run=args.dry_run, workers=args.workers)

    if not args.dry_run:
        print(f"\nWrote {written} files to: {output_dir / result.repo_name}/")
        print(f"Open in Obsidian: {output_dir / result.repo_name / 'INDEX.md'}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
