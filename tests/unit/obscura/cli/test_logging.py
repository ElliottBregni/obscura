import importlib
import os
import logging


def reload_modules():
    # ensure render and logger pick up env vars
    import obscura.cli.render as render
    importlib.reload(render)
    import obscura.cli.logger as clog
    importlib.reload(clog)
    return render


def test_info_logs_to_console(monkeypatch, tmp_path):
    os.environ["OBSCURA_OUTPUT_MODE"] = "cli"
    render = reload_modules()
    # ensure verbose internals are enabled
    render.output.verbose = True

    printed = []

    def fake_print(*args, **kwargs):
        printed.append(" ".join(str(a) for a in args))

    monkeypatch.setattr(render.console, "print", fake_print)

    logger = logging.getLogger("obscura.testinfo")
    # child logger should propagate to 'obscura' parent handlers
    logger.info("hello user")

    assert any("hello user" in p for p in printed), "INFO log was not printed to console"


def test_debug_logs_to_output_buffer(monkeypatch):
    os.environ["OBSCURA_OUTPUT_MODE"] = "cli"
    render = reload_modules()
    render.output.verbose = True
    # clear any prior buffer
    render.output._buffer.clear()

    logger = logging.getLogger("obscura.testdebug")
    logger.debug("secret debug")

    buf = render.output.get_buffer()
    assert any("secret debug" in s for s in buf), f"DEBUG not found in buffer: {buf}"
