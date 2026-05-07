"""conftest.py for obscura/tools unit tests.

Breaks the circular import that arises when importing `obscura.tools.providers.*`
submodules directly:

  obscura.tools.providers.__init__
    → obscura.tools.system (→ _ui → obscura.agent.interaction)
    → obscura.agent.__init__ (→ agents.py)
    → obscura.tools.providers (partially initialized → ImportError)

Pre-importing `obscura.agent.agents` resolves the cycle by ensuring
`obscura.agent` is fully initialized before `obscura.tools.providers`
triggers it as a side-effect.
"""
from __future__ import annotations

import obscura.agent.agents as _  # noqa: F401  — break circular import
