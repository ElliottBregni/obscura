import sys
import types
import asyncio


async def test_build_prompt_session(monkeypatch):
    """Ensure build_prompt_session constructs a session without importing the real prompt UI."""
    # Inject a lightweight fake prompt module to avoid heavy UI imports
    mod = types.ModuleType("obscura.cli.prompt")

    async def animate_spinner(ss):
        # quick no-op spinner for test
        await asyncio.sleep(0)

    def create_prompt_session(completions, streaming_status=None, prompt_status=None, at_command_names=None, dollar_skill_names=None, hud_provider=None):
        return "FAKE_SESSION"

    mod.animate_spinner = animate_spinner
    mod.create_prompt_session = create_prompt_session

    monkeypatch.setitem(sys.modules, "obscura.cli.prompt", mod)

    from obscura.cli.repl import build_prompt_session

    class Ctx:
        pass

    ctx = Ctx()
    ctx.discover_at_commands = lambda: ["@cmd"]
    ctx.discover_dollar_skills = lambda: ["$skill"]
    ctx._prompt_status = "PROMPT_STATUS"

    ss = types.SimpleNamespace(active=False)

    session, prompt_status, spinner_task = build_prompt_session(ctx, ss)

    assert session == "FAKE_SESSION"
    assert prompt_status == "PROMPT_STATUS"

    # Await the spinner task to ensure it completes and doesn't leak
    await spinner_task
    assert spinner_task.done()
