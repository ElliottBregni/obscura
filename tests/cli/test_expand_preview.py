import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROMPT_PATH = str(ROOT / 'obscura' / 'cli' / 'prompt.py')
RENDER_PATH = str(ROOT / 'obscura' / 'cli' / 'render.py')

spec2 = importlib.util.spec_from_file_location('obscura.cli.render', RENDER_PATH)
render = importlib.util.module_from_spec(spec2)
sys.modules['obscura.cli.render'] = render
spec2.loader.exec_module(render)

spec = importlib.util.spec_from_file_location('obscura.cli.prompt', PROMPT_PATH)
prompt = importlib.util.module_from_spec(spec)
sys.modules['obscura.cli.prompt'] = prompt
spec.loader.exec_module(prompt)

class FakeRenderer:
    def __init__(self, text):
        self._text = text
    def get_accumulated_text(self):
        return self._text


def test_expand_preview_outputs_markdown(capsys):
    fake = FakeRenderer("# Hello\n\nThis is *markdown* preview.")
    render.set_active_renderer(fake)
    try:
        prompt.expand_preview()
        captured = capsys.readouterr()
        assert "Hello" in captured.out
        assert "markdown" in captured.out
    finally:
        render.set_active_renderer(None)
