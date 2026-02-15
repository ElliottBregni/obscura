# pyright: reportMissingImports=false
"""
sdk.telemetry.metrics — Pre-defined metrics for the Obscura SDK.

All metrics are lazily created on first access so the module can be
imported without OTel installed.

Metrics
-------
========================================= ============= ================================
Metric                                    Type          Labels
========================================= ============= ================================
obscura_requests_total                    Counter       backend, method, status
obscura_request_duration_seconds          Histogram     backend, method
obscura_agent_runs_total                  Counter       agent_name, status
obscura_agent_phase_duration_seconds      Histogram     agent_name, phase
obscura_tool_calls_total                  Counter       tool_name, status
obscura_tool_duration_seconds             Histogram     tool_name
obscura_sync_operations_total             Counter       status
obscura_active_sessions                   UpDownCounter backend
obscura_stream_chunks_total              Counter       backend, chunk_kind
========================================= ============= ================================
"""

from __future__ import annotations

from typing import Any, Protocol, overload, runtime_checkable


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------

@runtime_checkable
class MetricInstrument(Protocol):
    """Structural type for an OTel-style metric instrument.

    Both ``add`` and ``record`` are provided so a single no-op type can
    stand in for counters *and* histograms.
    """

    def add(self, amount: float = 1, attributes: dict[str, str] | None = None) -> None: ...

    def record(self, amount: float, attributes: dict[str, str] | None = None) -> None: ...


class Meter(Protocol):
    """Structural type for an OTel-style Meter.

    Parameters are keyword-only to stay compatible with OTel regardless
    of its positional argument order.
    """

    def create_counter(self, name: str, *, unit: str = "", description: str = "") -> Any: ...

    def create_histogram(self, name: str, *, unit: str = "", description: str = "") -> Any: ...

    def create_up_down_counter(self, name: str, *, unit: str = "", description: str = "") -> Any: ...


def _get_meter() -> Meter | None:
    """Return an OTel meter, or None if OTel is unavailable."""
    try:
        from opentelemetry import metrics
        return metrics.get_meter("obscura-sdk")
    except ImportError:
        return None


class _LazyMetric:
    """Descriptor that lazily creates an OTel metric instrument."""

    def __init__(self, factory_name: str, metric_name: str, description: str, unit: str = "") -> None:
        self._factory_name = factory_name
        self._metric_name = metric_name
        self._description = description
        self._unit = unit
        self._attr = f"_lazy_{metric_name.replace('.', '_')}"

    def __set_name__(self, owner: type, name: str) -> None:
        self._attr = f"_lazy_{name}"

    @overload
    def __get__(self, obj: None, objtype: type) -> _LazyMetric: ...

    @overload
    def __get__(self, obj: object, objtype: type | None = None) -> MetricInstrument: ...

    def __get__(self, obj: object | None, objtype: type | None = None) -> _LazyMetric | MetricInstrument:
        if obj is None:
            return self

        cached: MetricInstrument | None = getattr(obj, self._attr, None)
        if cached is not None:
            return cached

        meter = _get_meter()
        instrument: MetricInstrument
        if meter is None:
            instrument = _NoOpInstrument()
        else:
            factory = getattr(meter, self._factory_name)
            kwargs: dict[str, Any] = {
                "name": self._metric_name,
                "description": self._description,
            }
            if self._unit:
                kwargs["unit"] = self._unit
            instrument = factory(**kwargs)

        object.__setattr__(obj, self._attr, instrument)
        return instrument


class ObscuraMetrics:
    """Container for all Obscura SDK metrics. Singleton access via ``get_metrics()``."""

    # -- Request metrics -------------------------------------------------------
    requests_total = _LazyMetric(
        "create_counter",
        "obscura_requests_total",
        "Total number of requests to LLM backends",
    )

    request_duration_seconds = _LazyMetric(
        "create_histogram",
        "obscura_request_duration_seconds",
        "Duration of requests to LLM backends",
        unit="s",
    )

    # -- Agent metrics ---------------------------------------------------------
    agent_runs_total = _LazyMetric(
        "create_counter",
        "obscura_agent_runs_total",
        "Total number of agent APER loop runs",
    )

    agent_phase_duration_seconds = _LazyMetric(
        "create_histogram",
        "obscura_agent_phase_duration_seconds",
        "Duration of individual agent phases",
        unit="s",
    )

    # -- Tool metrics ----------------------------------------------------------
    tool_calls_total = _LazyMetric(
        "create_counter",
        "obscura_tool_calls_total",
        "Total number of tool calls",
    )

    tool_duration_seconds = _LazyMetric(
        "create_histogram",
        "obscura_tool_duration_seconds",
        "Duration of tool executions",
        unit="s",
    )

    # -- Sync metrics ----------------------------------------------------------
    sync_operations_total = _LazyMetric(
        "create_counter",
        "obscura_sync_operations_total",
        "Total number of vault sync operations",
    )

    # -- Session metrics -------------------------------------------------------
    active_sessions = _LazyMetric(
        "create_up_down_counter",
        "obscura_active_sessions",
        "Number of currently active sessions",
    )

    # -- Stream metrics --------------------------------------------------------
    stream_chunks_total = _LazyMetric(
        "create_counter",
        "obscura_stream_chunks_total",
        "Total number of stream chunks emitted",
    )


# ---------------------------------------------------------------------------
# Singleton access
# ---------------------------------------------------------------------------

_instance: ObscuraMetrics | None = None


def get_metrics() -> ObscuraMetrics:
    """Return the singleton ObscuraMetrics instance."""
    global _instance
    if _instance is None:
        _instance = ObscuraMetrics()
    return _instance


# ---------------------------------------------------------------------------
# No-op fallback
# ---------------------------------------------------------------------------

class NoOpInstrument:
    """No-op metric instrument when OTel is unavailable.

    Structurally satisfies :class:`MetricInstrument`.
    """

    def add(self, amount: float = 1, attributes: dict[str, str] | None = None) -> None:
        pass

    def record(self, amount: float, attributes: dict[str, str] | None = None) -> None:
        pass


# Ensure structural compatibility at module level.
_noop_check: MetricInstrument = NoOpInstrument()


def get_noop_instrument() -> MetricInstrument:
    """Return a reusable no-op instrument (testing/observability)."""
    return _noop_check
