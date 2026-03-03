from __future__ import annotations

import logging

from obscura.core.reasoning_output import ReasoningOutputBase


class _Harness(ReasoningOutputBase):
    pass


def test_record_reasoning_delta_logs(caplog) -> None:
    h = _Harness()
    with caplog.at_level(logging.INFO, logger="obscura.reasoning"):
        h.record_reasoning_delta(
            text="step-by-step",
            backend="openai",
            model="gpt-5",
            turn=1,
        )
    assert "backend=openai model=gpt-5 turn=1 reasoning=step-by-step" in caplog.text
