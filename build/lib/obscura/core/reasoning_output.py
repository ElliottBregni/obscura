"""Shared reasoning output/logging helpers."""

from __future__ import annotations

import logging


class ReasoningOutputBase:
    """Centralized reasoning output handling.

    Any loop/adapter that emits reasoning can inherit this and call
    ``record_reasoning_delta`` so logging behavior stays consistent.
    """

    _REASONING_LOG_MAX_CHARS = 2000

    def record_reasoning_delta(
        self,
        *,
        text: str,
        backend: str,
        model: str,
        turn: int,
    ) -> None:
        cleaned = (text or "").strip()
        if not cleaned:
            return
        if len(cleaned) > self._REASONING_LOG_MAX_CHARS:
            cleaned = cleaned[: self._REASONING_LOG_MAX_CHARS] + "..."

        reasoning_logger = logging.getLogger("obscura.reasoning")
        fallback_logger = logging.getLogger("obscura_reasoning")
        if reasoning_logger.level == logging.NOTSET:
            reasoning_logger.setLevel(logging.INFO)
        if fallback_logger.level == logging.NOTSET:
            fallback_logger.setLevel(logging.INFO)

        payload = "backend=%s model=%s turn=%s reasoning=%s"
        args = (backend, model or "unknown", turn, cleaned)
        reasoning_logger.info(payload, *args)
        # Keep a non-namespaced fallback so capture still works if the CLI logger
        # reconfigures the "obscura.*" hierarchy during tests/runtime reloads.
        fallback_logger.info(payload, *args)
