import importlib
import sys
import builtins


def _reload_render():
    # Ensure a fresh import
    if "obscura.cli.render" in sys.modules:
        del sys.modules["obscura.cli.render"]
    return importlib.import_module("obscura.cli.render")


def test_defaults(monkeypatch):
    # Ensure env defaults (no capture prints)
    monkeypatch.delenv("OBSCURA_CAPTURE_PRINTS", raising=False)
    monkeypatch.delenv("OBSCURA_OUTPUT_MODE", raising=False)
    monkeypatch.delenv("OBSCURA_VERBOSE", raising=False)
    # reload config and render fresh
    if "obscura.config" in sys.modules:
        del sys.modules["obscura.config"]
    r = _reload_render()
    assert hasattr(r, "output")
    assert r.output.env == "cli"
    # Preserve previous default behavior: verbose internals enabled by default
    assert r.output.verbose is True


def test_capture_prints_wraps(monkeypatch):
    monkeypatch.setenv("OBSCURA_CAPTURE_PRINTS", "true")
    monkeypatch.setenv("OBSCURA_OUTPUT_MODE", "test-mode")
    # reload config and render to pick up env changes
    if "obscura.config" in sys.modules:
        del sys.modules["obscura.config"]
    if "obscura.cli.render" in sys.modules:
        del sys.modules["obscura.cli.render"]
    import obscura.config as config
    orig_print = builtins.print
    r = importlib.import_module("obscura.cli.render")
    try:
        assert builtins.print is not orig_print
        assert r.output.env == "test-mode"
        assert r.output.verbose == config.VERBOSE
    finally:
        builtins.print = orig_print
