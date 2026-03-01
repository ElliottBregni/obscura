from obscura.cli.render import export_transcript_markdown


def test_export_simple():
    history = [
        ("user", "› hello"),
        ("assistant", "• Hi there\nI can help."),
        ("user", "› run tests"),
        ("assistant", "• Running tests...\nAll good."),
    ]
    md = export_transcript_markdown(history)
    assert "# Conversation transcript" in md
    assert "### 1. User" in md
    assert "Hi there" in md
