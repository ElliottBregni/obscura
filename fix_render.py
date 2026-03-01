import re

path = '/Users/elliottbregni/dev/obscura-main/obscura/cli/render.py'
content = open(path).read()
original = content

# 1. Add markup_escape import
content = content.replace(
    'from rich.markdown import Markdown\nfrom rich.panel import Panel',
    'from rich.markdown import Markdown\nfrom rich.markup import escape as markup_escape\nfrom rich.panel import Panel'
)

# 2. Strengthen _sanitize_text
old_fn = 'def _sanitize_text(s: str) -> str:\n    """Remove ANSI escape/control sequences from text for safe printing."""\n    if not s:\n        return ""\n    try:\n        cleaned = re.sub(r"\\x1B\\[[0-?]*[ -/]*[@-~]", "", s)\n        cleaned = re.sub(r"[\\x00-\\x08\\x0B\\x0C\\x0E-\\x1F\\x7F]+", "", cleaned)\n        return cleaned\n    except Exception:\n        return s'

new_fn = 'def _sanitize_text(s: str) -> str:\n    """Remove ANSI/escape sequences and control characters from text."""\n    if not s:\n        return ""\n    try:\n        # CSI sequences: ESC [ ... final-byte\n        cleaned = re.sub(r"\\x1B\\[[0-?]*[ -/]*[@-~]", "", s)\n        # OSC sequences: ESC ] ... (ST or BEL)\n        cleaned = re.sub(r"\\x1B\\][^\\x07\\x1B]*(?:\\x07|\\x1B\\\\)", "", cleaned)\n        # DCS / PM / APC / SOS sequences\n        cleaned = re.sub(r"\\x1B[PX^_][^\\x1B]*(?:\\x1B\\\\|$)", "", cleaned)\n        # Lone ESC + one char\n        cleaned = re.sub(r"\\x1B[@-Z\\\\-_]", "", cleaned)\n        # Bare ESC\n        cleaned = re.sub(r"\\x1B", "", cleaned)\n        # C0 controls (keep TAB \\x09, LF \\x0A, CR \\x0D)\n        cleaned = re.sub(r"[\\x00-\\x08\\x0B\\x0C\\x0E-\\x1F\\x7F]+", "", cleaned)\n        return cleaned\n    except Exception:\n        return s'

if old_fn in content:
    content = content.replace(old_fn, new_fn)
    print("OK: _sanitize_text updated")
else:
    print("WARN: _sanitize_text not found by exact match, trying flexible match")
    pattern = r'def _sanitize_text\(s: str\) -> str:.*?(?=\ndef |\nclass )'
    match = re.search(pattern, content, re.DOTALL)
    if match:
        content = content[:match.start()] + new_fn + content[match.end():]
        print("OK: _sanitize_text updated via regex")
    else:
        print("ERROR: could not find _sanitize_text")

# 3. Fix _show_tool_result error path: escape sanitized raw text
before = 'console.print(f"  [{ERROR_COLOR}]  {_sanitize_text(raw[:200])}[/]")'
after  = 'console.print(f"  [{ERROR_COLOR}]  {markup_escape(_sanitize_text(raw[:200]))}[/]")'
if before in content:
    content = content.replace(before, after)
    print("OK: _show_tool_result error path escaped")
else:
    print("WARN: _show_tool_result error path not found")

# 4. Fix _show_tool_result plain-text path: escape short
before = '            short = _sanitize_text(raw[:300])\n            console.print(f"  [dim {OK_COLOR}]  {short}[/]")'
after  = '            short = markup_escape(_sanitize_text(raw[:300]))\n            console.print(f"  [dim {OK_COLOR}]  {short}[/]")'
if before in content:
    content = content.replace(before, after)
    print("OK: _show_tool_result plain-text path escaped")
else:
    print("WARN: _show_tool_result plain-text path not found")

# 5. Fix _show_tool_call: escape name and sanitized_args
before = '        console.print(\n            f"\\n  [{TOOL_COLOR}]  {name}[/]  [dim]{sanitized_args}[/]"\n        )'
after  = '        console.print(\n            f"\\n  [{TOOL_COLOR}]  {markup_escape(name)}[/]  [dim]{markup_escape(sanitized_args)}[/]"\n        )'
if before in content:
    content = content.replace(before, after)
    print("OK: _show_tool_call escaped")
else:
    print("WARN: _show_tool_call not found")

# 6. Fix render_event TOOL_RESULT: escape snippet
before = '            snippet = _sanitize_text((event.tool_result or "")[:120])\n            style = ERROR_COLOR if event.is_error else "dim green"\n            prefix = "" if event.is_error else ""\n            console.print(f"  [{style}]{prefix} {snippet}[/]")'
after  = '            snippet = markup_escape(_sanitize_text((event.tool_result or "")[:120]))\n            style = ERROR_COLOR if event.is_error else "dim green"\n            prefix = "" if event.is_error else ""\n            console.print(f"  [{style}]{prefix} {snippet}[/]")'
if before in content:
    content = content.replace(before, after)
    print("OK: render_event TOOL_RESULT escaped")
else:
    print("WARN: render_event TOOL_RESULT not found")

# 7. Fix render_event TOOL_CALL: escape tool_name
before = '            console.print(\n                f"\\n  [{TOOL_COLOR}]  {_sanitize_text(event.tool_name)}[/]"\n            )'
after  = '            console.print(\n                f"\\n  [{TOOL_COLOR}]  {markup_escape(_sanitize_text(event.tool_name))}[/]"\n            )'
if before in content:
    content = content.replace(before, after)
    print("OK: render_event TOOL_CALL escaped")
else:
    print("WARN: render_event TOOL_CALL not found")

# 8. Fix render_event THINKING_DELTA: escape safe
before = '            safe = _sanitize_text(event.text)\n            console.print(f"[dim italic {THINKING_COLOR}]{safe}[/]", end="")'
after  = '            safe = markup_escape(_sanitize_text(event.text))\n            console.print(f"[dim italic {THINKING_COLOR}]{safe}[/]", end="")'
if before in content:
    content = content.replace(before, after)
    print("OK: render_event THINKING_DELTA escaped")
else:
    print("WARN: render_event THINKING_DELTA not found")

# 9. Fix render_agent_output: escape safe_text and agent_name
before = '    if output_ev.is_final:\n        console.print(f"  [bold {ACCENT}]{_sanitize_text(output_ev.agent_name)}:[/] {safe_text}")\n    else:\n        console.print(safe_text, end="")'
after  = '    if output_ev.is_final:\n        console.print(f"  [bold {ACCENT}]{markup_escape(_sanitize_text(output_ev.agent_name))}:[/] {markup_escape(safe_text)}")\n    else:\n        console.print(markup_escape(safe_text), end="")'
if before in content:
    content = content.replace(before, after)
    print("OK: render_agent_output escaped")
else:
    print("WARN: render_agent_output not found")

if content != original:
    open(path, 'w').write(content)
    print("File written successfully")
else:
    print("No changes were made")
