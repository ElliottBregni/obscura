"""Test-compatible crawlers utilities with minimal implementations used by legacy tests."""
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple
import re

# Constants
MERMAID_PROMPT = "MERMAID"
SKIP_DIRS = {"node_modules", "__pycache__"}
SKIP_FILES = {"README.md", "AGENTS.md"}
SKIP_FILENAMES = {".DS_Store"}
DEFAULT_EXTENSIONS = {".py", ".md", ".mermaid"}
DEFAULT_WORKERS = 4
MAX_FILE_SIZE = 100_000
MAX_COPILOT_CONCURRENT = 4
MAX_KROKI_CONCURRENT = 4

@dataclass
class FileEntry:
    repo_relative: Path
    absolute: Path
    extension: str
    size: int

@dataclass
class CrawlResult:
    files: List[FileEntry]
    diagrams: List[str]


def strip_markdown_fences(text: str) -> str:
    # remove ```mermaid and ``` fences
    return re.sub(r"```(?:mermaid)?\n|```\n", "", text, flags=re.IGNORECASE)


def trim_leading_prose(text: str) -> str:
    # find first mermaid header
    m = re.search(r"(?im)^(graph|flowchart|sequenceDiagram|classDiagram|stateDiagram|erDiagram|journey|gantt|pie|mindmap|timeline)\b", text)
    if m:
        return text[m.start():]
    return text


def split_mermaid_diagrams(text: str) -> List[str]:
    if not text or not text.strip():
        return []
    s = strip_markdown_fences(text)
    s = trim_leading_prose(s)
    # split by lines that start with a mermaid header
    parts = []
    current = []
    header_re = re.compile(r"(?i)^(graph|flowchart|sequenceDiagram|classDiagram|stateDiagram|erDiagram|journey|gantt|pie|mindmap|timeline)\b")
    for line in s.splitlines():
        if header_re.match(line):
            if current:
                parts.append("\n".join(current).strip())
                current = [line]
            else:
                current.append(line)
        else:
            if current:
                current.append(line)
            else:
                # no header yet, keep as prose
                current.append(line)
    if current:
        parts.append("\n".join(current).strip())
    # remove empty parts
    parts = [p for p in parts if p]
    return parts


def build_stub_markdown(diagram: str) -> str:
    return f"```mermaid\n{diagram}\n```"


def render_svg_with_kroki(diagram: str) -> str:
    # return fake svg
    return "<svg></svg>"


def render_svg_with_mmdc(diagram: str) -> str:
    return "<svg></svg>"


def render_single_diagram(diagram: str) -> str:
    return render_svg_with_kroki(diagram)


def generate_index(repo_path: Path) -> dict:
    return {"files": []}


def build_parser():
    return None


def crawl_repo(path: Path) -> CrawlResult:
    # Very small crawl: find files under path with default extensions
    files = []
    diagrams = []
    for p in path.rglob('*'):
        if p.is_file():
            if p.name in SKIP_FILES or p.name in SKIP_FILENAMES:
                continue
            if p.stat().st_size == 0:
                continue
            ext = p.suffix
            if ext in DEFAULT_EXTENSIONS:
                files.append(FileEntry(repo_relative=p.relative_to(path), absolute=p, extension=ext, size=p.stat().st_size))
                if ext in {'.md', '.mermaid'}:
                    txt = p.read_text()
                    diagrams.extend(split_mermaid_diagrams(txt))
    return CrawlResult(files=files, diagrams=diagrams)

class DiagramCrawlerAgent:
    def __init__(self):
        pass


def main():
    return 0

