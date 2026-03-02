import re

with open('/Users/elliottbregni/dev/obscura-main/obscura/cli/__init__.py', 'r', encoding='utf-8') as f:
    content = f.read()

HEARTBEAT = (
    'async def _cli_heartbeat(\n'
    '    start_time: float,\n'
    '    got_output: asyncio.Event,\n'
    '    *,\n'
    '    first_ping: float = 120.0,\n'
    '    interval: float = 30.0,\n'
    ') -> None:\n'
    '    """Print keepalive dots if the agent takes longer than `first_ping` seconds\n'
    '    to produce any output. After the first ping, repeats every `interval` s."""\n'
    '    try:\n'
    '        await asyncio.wait_for(got_output.wait(), timeout=first_ping)\n'
    '    except asyncio.TimeoutError:\n'
    '        pass\n'
    '    else:\n'
    '        return  # output arrived \u2014 nothing to do\n'
    '\n'
    '    # First ping\n'
    '    elapsed = time.time() - start_time\n'
    '    console.print(\n'
    '        f"[dim]  \u29ff still working\u2026 ({elapsed:.0f}s)[/]",\n'
    '        highlight=False,\n'
    '    )\n'
    '\n'
    '    # Repeat every `interval` seconds until output arrives\n'
    '    while True:\n'
    '        try:\n'
    '            await asyncio.wait_for(got_output.wait(), timeout=interval)\n'
    '        except asyncio.TimeoutError:\n'
    '            elapsed = time.time() - start_time\n'
    '            console.print(\n'
    '                f"[dim]  \u29ff still working\u2026 ({elapsed:.0f}s)[/]",\n'
    '                highlight=False,\n'
    '            )\n'
    '        else:\n'
    '            return\n'
    '\n'
    '\n'
)

# Step 1
T1 = 'async def send_message('
assert T1 in content
content = content.replace(T1, HEARTBEAT + T1, 1)
print('Step 1 OK')

# Step 2
OLD2 = ('    async def _stream_with_retry(attempt: int = 0) -> list[str]:\n'
        '        _buf: list[str] = []\n'
        '        _s = ctx.client.run_loop(')
NEW2 = ('    _hb_start = time.time()\n'
        '    _got_output: asyncio.Event = asyncio.Event()\n'
        '    _hb_task: asyncio.Task[None] = asyncio.create_task(\n'
        '        _cli_heartbeat(_hb_start, _got_output)\n'
        '    )\n'
        '\n'
        '    async def _stream_with_retry(attempt: int = 0) -> list[str]:\n'
        '        _buf: list[str] = []\n'
        '        _s = ctx.client.run_loop(')
assert OLD2 in content
content = content.replace(OLD2, NEW2, 1)
print('Step 2 OK')

# Step 3
OLD3 = ('                if event.kind == AgentEventKind.TEXT_DELTA:\n'
        '                    _buf.append(event.text)')
NEW3 = ('                if event.kind == AgentEventKind.TEXT_DELTA:\n'
        '                    _got_output.set()\n'
        '                    _buf.append(event.text)')
assert OLD3 in content
content = content.replace(OLD3, NEW3, 1)
print('Step 3 OK')

# Step 4
OLD4 = ('    try:\n'
        '        accumulated = await _stream_with_retry()\n'
        '    except KeyboardInterrupt:\n'
        '        pass\n'
        '    finally:\n'
        '        renderer.finish()')
NEW4 = ('    try:\n'
        '        accumulated = await _stream_with_retry()\n'
        '    except KeyboardInterrupt:\n'
        '        pass\n'
        '    finally:\n'
        '        _got_output.set()  # unblock heartbeat so it exits cleanly\n'
        '        _hb_task.cancel()\n'
        '        try:\n'
        '            await _hb_task\n'
        '        except asyncio.CancelledError:\n'
        '            pass\n'
        '        renderer.finish()')
assert OLD4 in content
content = content.replace(OLD4, NEW4, 1)
print('Step 4 OK')

with open('/Users/elliottbregni/dev/obscura-main/obscura/cli/__init__.py', 'w', encoding='utf-8') as f:
    f.write(content)
print('All changes written.')
