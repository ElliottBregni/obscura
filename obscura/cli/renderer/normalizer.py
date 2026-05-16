"""obscura.cli.renderer.normalizer — AgentEvent → UiEvent pipeline.

The :class:`SignalNormalizer` is the only thing the renderer should
consume from. It owns:

    * adapter dispatch (per-tool / per-source refinement)
    * mode-aware visibility (``NORMAL`` vs ``DEBUG``)
    * dedup of repeated status events
    * collapsing long tool results in normal mode
    * correlation IDs linking tool_call ↔ tool_result
    * never-throw guarantee — malformed input becomes an error UiEvent

Streaming text deltas pass through unchanged: visual merging is the
renderer's job (it already buffers and flushes on turn boundaries).
The normalizer never blocks waiting for "the rest of a message".
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass

from obscura.cli.renderer.adapters import (
    EventAdapter,
    MCPToolEventAdapter,
    RuntimeEventAdapter,
    ShellToolEventAdapter,
)
from obscura.cli.renderer.ui_event import (
    DisplayMode,
    UiEvent,
    UiEventKind,
    UiEventSource,
    UiSeverity,
    UiVisibility,
)
from obscura.core.types import AgentEvent

__all__ = [
    "NormalizerConfig",
    "SignalNormalizer",
]

logger = logging.getLogger(__name__)


# Lines past this threshold collapse in NORMAL mode. Falls back to a
# byte-count check for very long single lines. Override via env to
# match user expectations across terminals of different heights.
_DEFAULT_COLLAPSE_LINES = int(
    os.environ.get("OBSCURA_TUI_COLLAPSE_LINES", "60") or "60",
)
_DEFAULT_COLLAPSE_BYTES = int(
    os.environ.get("OBSCURA_TUI_COLLAPSE_BYTES", "8000") or "8000",
)
# Window for status-event dedup. Identical (source, title, content)
# events arriving inside this window are dropped in NORMAL mode.
_DEFAULT_STATUS_DEDUP_WINDOW_S = float(
    os.environ.get("OBSCURA_TUI_STATUS_DEDUP_S", "1.5") or "1.5",
)


@dataclass
class NormalizerConfig:
    """Tunables for :class:`SignalNormalizer`.

    All fields default to env-driven values so deployments can tweak
    without touching code. The renderer creates one config per
    session.
    """

    collapse_lines: int = _DEFAULT_COLLAPSE_LINES
    collapse_bytes: int = _DEFAULT_COLLAPSE_BYTES
    status_dedup_window_s: float = _DEFAULT_STATUS_DEDUP_WINDOW_S


class SignalNormalizer:
    r"""Convert :class:`AgentEvent` instances into :class:`UiEvent`\ s.

    Stateful across calls (dedup window, correlation tracking) but
    cheap to construct — one per renderer instance.
    """

    def __init__(
        self,
        mode: DisplayMode = DisplayMode.NORMAL,
        *,
        config: NormalizerConfig | None = None,
        adapters: list[EventAdapter] | None = None,
    ) -> None:
        self._mode = mode
        self._config = config or NormalizerConfig()
        # Adapter chain: most-specific first; RuntimeEventAdapter is
        # the catch-all and runs last.
        self._adapters: list[EventAdapter] = adapters or [
            MCPToolEventAdapter(),
            ShellToolEventAdapter(),
            RuntimeEventAdapter(),
        ]
        # Dedup state: (source, title, content_hash) → monotonic_ts.
        self._last_status: dict[tuple[str, str, int], float] = {}
        # Cache of seen tool_use_id values so the renderer can verify
        # tool_call ↔ tool_result pairs in debug mode.
        self._open_tool_calls: set[str] = set()

    # ── public API ───────────────────────────────────────────────────────

    @property
    def mode(self) -> DisplayMode:
        return self._mode

    def set_mode(self, mode: DisplayMode) -> None:
        """Switch normal/debug mode at runtime.

        Drops dedup state on transition so debug → normal doesn't
        suppress events that were last emitted under debug.
        """
        if mode == self._mode:
            return
        self._mode = mode
        self._last_status.clear()

    def normalize(self, event: AgentEvent) -> list[UiEvent]:
        r"""Project ``event`` onto zero or more :class:`UiEvent`\ s.

        Always returns a list (possibly empty). Never raises.
        """
        try:
            adapter = self._pick_adapter(event)
            ui_events = list(adapter.adapt(event))
        except Exception:  # noqa: BLE001  — never let adapter errors poison the TUI
            logger.exception("adapter raised; emitting error UiEvent")
            return [
                UiEvent(
                    kind=UiEventKind.ERROR,
                    source=UiEventSource.RUNTIME,
                    title="signal_normalizer_error",
                    content=f"adapter failure on {getattr(event, 'kind', '?')}",
                    severity=UiSeverity.ERROR,
                    visibility=UiVisibility.NORMAL,
                )
            ]

        out: list[UiEvent] = []
        for ui in ui_events:
            adjusted = self._post_process(ui)
            if adjusted is None:
                continue
            out.append(adjusted)
            # Debug mirror for tool events — surfaces raw payload as a
            # DEBUG UiEvent so debug mode can render args/results fully
            # without reshaping the user-facing TOOL_CALL/TOOL_RESULT
            # event itself.
            if (
                self._mode == DisplayMode.DEBUG
                and adjusted.kind in (UiEventKind.TOOL_CALL, UiEventKind.TOOL_RESULT)
                and adjusted.raw is not None
            ):
                out.append(
                    UiEvent(
                        kind=UiEventKind.DEBUG,
                        source=adjusted.source,
                        title=f"raw {adjusted.kind.value}",
                        content=adjusted.raw,
                        tool_name=adjusted.tool_name,
                        correlation_id=adjusted.correlation_id,
                        provider=adjusted.provider,
                        metadata={**adjusted.metadata, "mirror_of": adjusted.id},
                        visibility=UiVisibility.DEBUG_ONLY,
                    )
                )
        return out

    # ── pipeline steps ───────────────────────────────────────────────────

    def _pick_adapter(self, event: AgentEvent) -> EventAdapter:
        for adapter in self._adapters:
            try:
                if adapter.handles(event):
                    return adapter
            except Exception:  # noqa: BLE001
                logger.debug(
                    "adapter %s.handles raised; skipping",
                    type(adapter).__name__,
                    exc_info=True,
                )
        # The last adapter in the chain (RuntimeEventAdapter) always
        # handles, so this is unreachable in practice. Defend anyway.
        return self._adapters[-1]

    def _post_process(self, ui: UiEvent) -> UiEvent | None:
        """Apply mode-aware policies — visibility, dedup, collapsing.

        Returns ``None`` to drop the event entirely (dedup hit) or
        a (possibly mutated) :class:`UiEvent` to forward.
        """
        # ── correlation tracking ──────────────────────────────────
        if ui.kind == UiEventKind.TOOL_CALL and ui.correlation_id:
            self._open_tool_calls.add(ui.correlation_id)
        elif ui.kind == UiEventKind.TOOL_RESULT and ui.correlation_id:
            # Match found — drop from open set. Unmatched results
            # remain visible; we don't drop them, but they're worth
            # tagging in metadata so debug mode can highlight them.
            if ui.correlation_id in self._open_tool_calls:
                self._open_tool_calls.discard(ui.correlation_id)
            else:
                ui.metadata = {**ui.metadata, "unmatched_call": True}

        # ── debug-mode raw exposure ───────────────────────────────
        if self._mode != DisplayMode.DEBUG:
            # Strip raw payloads in normal mode to keep memory + log
            # output lean. The original AgentEvent is not lost — the
            # event store still has it; this only affects renderer
            # state.
            ui.raw = None

        # ── collapse long tool results in normal mode ─────────────
        if (
            ui.kind == UiEventKind.TOOL_RESULT
            and self._mode == DisplayMode.NORMAL
            and self._needs_collapse(ui)
        ):
            ui.visibility = UiVisibility.COLLAPSED
            ui.metadata = {
                **ui.metadata,
                "collapsed": True,
                "original_size": _content_size(ui.content),
            }

        # ── dedup repeated status events ──────────────────────────
        if ui.kind == UiEventKind.STATUS:
            key = (
                str(ui.source.value),
                ui.title or "",
                hash(_content_to_str(ui.content)),
            )
            now = ui.monotonic_ts
            last = self._last_status.get(key)
            if (
                last is not None
                and now - last < self._config.status_dedup_window_s
                and self._mode == DisplayMode.NORMAL
            ):
                # Drop the duplicate. Debug mode keeps duplicates so
                # the trace is faithful.
                return None
            self._last_status[key] = now

        # Hidden events are dropped here so downstream consumers
        # (renderer, tests) can iterate without filtering. Debug-only
        # in normal mode is also dropped at this point.
        if not ui.is_visible(self._mode):
            return None

        return ui

    def _needs_collapse(self, ui: UiEvent) -> bool:
        size = _content_size(ui.content)
        if size > self._config.collapse_bytes:
            return True
        text = _content_to_str(ui.content)
        return text.count("\n") + 1 > self._config.collapse_lines


# ── helpers ─────────────────────────────────────────────────────────────


def _content_to_str(content: object) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return repr(content)


def _content_size(content: object) -> int:
    return len(_content_to_str(content))


def normalize_stream(
    events: Iterable[AgentEvent],
    *,
    mode: DisplayMode = DisplayMode.NORMAL,
) -> Iterable[UiEvent]:
    """Convenience: project an iterable of AgentEvents onto UiEvents.

    Useful for offline replay / event-store inspection where the
    caller doesn't need to share a normalizer instance with a live
    renderer.
    """
    norm = SignalNormalizer(mode=mode)
    for event in events:
        yield from norm.normalize(event)
