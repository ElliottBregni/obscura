---
name: crawl-diagrams
description: Generate Mermaid diagrams from source code via copilot subprocess. Reads a file, sends it to copilot -p, returns clean Mermaid text with diagrams split and fences stripped.
---
[[Travelers]]
# Crawl Diagrams

Generate Mermaid diagram code from a source file using `copilot -p`.

## When to Use

- Generating architecture/flow diagrams from code
- Creating visual documentation for a source file
- Feeding into SVG rendering or Obsidian markdown

## Usage

Pass a file path. The skill reads it, sends it to copilot, and returns clean Mermaid.

```
crawl-diagrams path/to/file.py
```

## What It Does

1. Reads the source file
2. Calls `copilot -p` with a strict Mermaid-only prompt
3. Strips markdown fences and leading prose from output
4. Splits multiple diagrams on diagram-type headers
5. Returns each diagram as a separate clean block

## Subprocess

```python
import subprocess, re
from copilot_models import guard_automation

def generate_mermaid(file_path: str) -> list[str]:
    """Read a file and return a list of clean Mermaid diagram strings."""
    with open(file_path, encoding="utf-8") as f:
        code = f.read()

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

    model_id = guard_automation("copilot_batch_diagrammer")
    result = subprocess.run(
        ["copilot", "-p", prompt, "--model", model_id],
        capture_output=True,
        text=True,
        check=False,
    )

    raw = (result.stdout or "").strip()
    if not raw:
        return []

    # Strip markdown fences
    lines = raw.splitlines()
    cleaned = []
    for line in lines:
        if line.strip().startswith("```"):
            continue
        cleaned.append(line)
    text = "\n".join(cleaned).strip()

    # Trim leading prose before first diagram header
    STARTERS = re.compile(
        r"^(?:graph|flowchart|sequenceDiagram|classDiagram|"
        r"stateDiagram|erDiagram|journey|gantt|pie|mindmap|timeline)\b",
        re.IGNORECASE,
    )
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if STARTERS.match(line.strip()):
            text = "\n".join(lines[i:]).strip()
            break

    # Split on diagram headers
    lines = text.splitlines()
    diagrams = []
    current = []
    for line in lines:
        if STARTERS.match(line.strip()) and current:
            d = "\n".join(current).strip()
            if d:
                diagrams.append(d)
            current = [line]
        else:
            current.append(line)
    if current:
        d = "\n".join(current).strip()
        if d:
            diagrams.append(d)

    return diagrams
```

## Example Output

Given a Python file with two classes, returns:

```
['classDiagram\n    class UserService {\n        +create_user()\n        +delete_user()\n    }',
 'flowchart TD\n    A[Request] --> B[Validate]\n    B --> C[Save]']
```

Each string is a standalone valid Mermaid diagram ready for rendering.
