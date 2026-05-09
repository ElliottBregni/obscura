"""Reveal-cursor pacing — shared between bordered REPL and TUI.

The streaming text from a backend arrives in arbitrary chunks: a
single token, a whole sentence, a 2 KB paragraph. Rendering each
chunk straight to the screen makes the assistant look choppy and
also makes long lines flash in atomically. Both surfaces want a
visible "typing" feel, so they keep the full streamed buffer behind
the scenes and advance a reveal cursor with a per-frame jittered
burst.

This module owns the pacing math so the bordered REPL
(``ModernRenderer._frame_loop``) and TUI (``app._reveal_tick``)
agree on what "smooth" means. The defaults are tuned for ~30 FPS and
read like organic typing without falling arbitrarily far behind
when a 2 KB chunk lands at once.
"""

from __future__ import annotations

import random


# Bounds for the per-frame jitter applied to the reveal cursor. ±30%
# of base breaks up the rigid "snap" of a fixed-burst reveal so
# streaming text feels more like organic typing. Floor at 1
# char/frame so a low jitter roll never stalls the cursor; ceiling is
# enforced by ``min(full_len, ...)`` at the call site.
REVEAL_JITTER_LOW = 0.7
REVEAL_JITTER_HIGH = 1.3


def compute_reveal_burst(*, backlog: int, base: int) -> int:
    """How many chars to reveal this frame.

    Pure helper — no IO, no hidden state. Backlog-aware: when the
    buffer is far ahead of the reveal cursor, scale the burst up so
    we don't fall arbitrarily far behind. Then jitter ±30% so the
    visible reveal rate isn't perfectly uniform.

    Returns at least 1 (so frames never stall while there's backlog)
    and is capped at the backlog by the caller's ``min(full_len, ...)``.
    """
    if backlog <= 0:
        return 0
    burst = base
    if backlog > 200:
        burst = max(burst, backlog // 4)
    elif backlog > 80:
        burst = max(burst, backlog // 6)
    jitter = random.uniform(REVEAL_JITTER_LOW, REVEAL_JITTER_HIGH)
    return max(1, int(burst * jitter))
