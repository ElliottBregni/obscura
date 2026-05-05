"""obscura.composition — surface-agnostic agent composition.

The composition layer extracts agent boot from the surfaces (REPL, REST
API, A2A, MCP-server) into a shared core + per-surface "extras" pipeline.
Each surface calls a `build_*_session()` function that returns a fully
wired `AgentSession`; surfaces never duplicate plugin loading, MCP
discovery, hook setup, or system-prompt composition.

Migration philosophy:

- **Building blocks** (in `composition/blocks/`) are functions
  `(session, config) -> None`. Each block reads config and may no-op
  silently when its feature is disabled. Hard failures raise; soft
  failures log+skip.
- **No lazy loading where logic can drift.** When a building block is
  extracted, the original callsite in the surface is *deleted*, not
  left as a fallback. `tests/composition/test_no_drift.py` enforces
  this with a callsite scan.
- **AgentSession is the only thing surfaces touch** post-build. Future
  refactors can absorb `ObscuraClient` into `AgentSession` without
  changing surface code.

Usage::

    from obscura.composition.repl import build_repl_session

    async with await build_repl_session(config) as session:
        async for event in session.stream_loop(prompt):
            ...
"""

from obscura.composition.session import (
    AgentSession,
    SessionConfig,
    Surface,
    new_session_id,
)

__all__ = [
    "AgentSession",
    "SessionConfig",
    "Surface",
    "new_session_id",
]
