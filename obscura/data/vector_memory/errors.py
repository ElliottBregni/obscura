"""Structured exception hierarchy for the vector-memory repository.

Callers should catch :class:`VectorMemoryError` to handle any vector-store
failure uniformly, then narrow on subclasses when they need to
distinguish "backend unreachable" from "feature disabled" from "bad
input."
"""

from __future__ import annotations


class VectorMemoryError(RuntimeError):
    """Base class — every vector-memory failure inherits from this."""


class VectorMemoryDisabled(VectorMemoryError):
    """Raised when vector memory is explicitly disabled.

    Set ``OBSCURA_VECTOR_MEMORY=on`` to re-enable. Distinct from
    :class:`VectorBackendUnavailable` so callers can degrade gracefully
    without surfacing a misleading "backend down" message.
    """


class VectorBackendUnavailable(VectorMemoryError):
    """The configured backend can't be reached after retry.

    Raised by the factory when:
      * Qdrant default + ``QdrantClient`` init fails with no
        ``OBSCURA_VECTOR_BACKEND`` opt-in to a fallback
      * pgvector / sqlite-vss explicitly selected but the backing pool
        / file is unreachable

    Includes the chosen backend name and underlying cause so the user
    can fix the config without grepping logs.
    """

    def __init__(self, backend: str, cause: Exception | str) -> None:
        super().__init__(f"vector backend {backend!r} unavailable: {cause}")
        self.backend = backend
        self.cause = cause


class VectorPayloadError(VectorMemoryError):
    """Bad input to upsert / search / filter — schema or shape problem."""


class VectorRetryExhausted(VectorMemoryError):
    """Transient operation failed after all retry attempts.

    Wraps the last underlying exception. Typically raised on Qdrant
    network blips that didn't recover within the configured budget.
    """

    def __init__(self, op: str, attempts: int, cause: Exception) -> None:
        super().__init__(
            f"vector op {op!r} failed after {attempts} attempts: {cause}",
        )
        self.op = op
        self.attempts = attempts
        self.cause = cause
