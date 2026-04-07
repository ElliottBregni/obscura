import asyncio
import types


async def test_caffeinate_start_stop(monkeypatch):
    """Start and stop the /caffeinate command using the fallback keep-awake task."""
    from obscura.cli import commands_extra as cmds
    import importlib

    # Capture prints
    messages = []

    def fake_print_info(msg):
        messages.append(("info", msg))

    def fake_print_ok(msg):
        messages.append(("ok", msg))

    def fake_print_error(msg):
        messages.append(("err", msg))

    render = importlib.import_module("obscura.cli.render")
    monkeypatch.setattr(render, "print_info", fake_print_info)
    monkeypatch.setattr(render, "print_ok", fake_print_ok)
    monkeypatch.setattr(render, "print_error", fake_print_error)

    # Force fallback (no system caffeinate)
    import shutil

    monkeypatch.setattr(shutil, "which", lambda _p: None)

    ctx = types.SimpleNamespace()

    # Start caffeinate (fallback task)
    await cmds.cmd_caffeinate("start", ctx)
    assert hasattr(ctx, "_caffeinate") and ctx._caffeinate is not None

    # Status should report running
    await cmds.cmd_caffeinate("status", ctx)

    # Stop caffeinate
    await cmds.cmd_caffeinate("stop", ctx)
    assert getattr(ctx, "_caffeinate", None) is None
