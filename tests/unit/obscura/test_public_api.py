"""Tests for the public ``obscura`` import surface.

The contract:

- ``import obscura`` MUST be cheap. No SDK chains (anthropic, openai, qdrant,
  psycopg, asyncpg, playwright, tiktoken) get pulled in transitively.
- The eager surface (types, protocols, ``ToolRegistry``, ``tool``,
  ``AuthenticatedUser``, ``ToolContext``) is available without triggering
  any lazy resolution.
- The lazy surface (``ObscuraClient``, ``BaseAgent``, ``OpenClawBridge``,
  the OpenClaw request types, ``AuthConfig``, ``ContextLoader``,
  handlers) loads on first attribute access via PEP 562 ``__getattr__``.
- Unknown attributes raise ``AttributeError`` (PEP 562 contract).

These tests run each import in a fresh ``subprocess`` to defeat
``sys.modules`` caching. Without that, an earlier test in the suite that
already pulled qdrant would make the contract un-checkable.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

# Modules whose presence in ``sys.modules`` after ``import obscura`` would
# defeat the lazy-loading contract. These all pull non-trivial dependency
# trees and are runtime-only — no caller importing the public surface
# should pay for them.
_FORBIDDEN_PREFIXES: tuple[str, ...] = (
    "anthropic",
    "openai",
    "qdrant_client",
    "psycopg",
    "psycopg2",
    "asyncpg",
    "playwright",
    "tiktoken",
    "sentence_transformers",
    "transformers",
    # Internal modules that themselves pull the SDKs above.
    "obscura.providers.openai",
    "obscura.providers.claude",
    "obscura.providers.copilot",
    "obscura.providers.localllm",
    "obscura.providers.codex",
    "obscura.providers.moonshot",
    "obscura.vector_memory.backends.qdrant_backend",
    "obscura.core.client",
    "obscura.core.agent_loop",
    "obscura.agent.agent",
)


def _run_in_subprocess(script: str) -> tuple[int, str, str]:
    """Run *script* in a fresh Python interpreter. Returns (rc, stdout, stderr)."""
    proc = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_import_obscura_does_not_pull_heavy_sdks() -> None:
    """``import obscura`` must not transitively import SDK chains."""
    rc, out, err = _run_in_subprocess(
        f"""
        import sys

        before = set(sys.modules)
        import obscura  # noqa: F401

        forbidden = {_FORBIDDEN_PREFIXES!r}
        leaked = sorted(
            m for m in sys.modules
            if m not in before and any(m == f or m.startswith(f + ".") for f in forbidden)
        )
        if leaked:
            print("LEAKED:", leaked)
            sys.exit(1)
        print("OK")
        """
    )
    assert rc == 0, (
        f"forbidden imports leaked into `import obscura`:\nstdout={out}\nstderr={err}"
    )


def test_eager_names_resolve_without_triggering_lazy_loads() -> None:
    """The light eager surface must be available without lazy resolution.

    This indirectly verifies the eager imports at the top of ``obscura/__init__``
    don't pull SDK chains: if they did, the previous test would have failed.
    """
    rc, out, err = _run_in_subprocess(
        """
        import obscura

        # Types
        assert obscura.Backend is not None
        assert obscura.BackendProtocol is not None
        assert obscura.Message is not None
        assert obscura.StreamChunk is not None
        assert obscura.ToolSpec is not None

        # Tools
        assert obscura.ToolRegistry is not None
        assert callable(obscura.tool)

        # Tool context
        assert obscura.ToolContext is not None
        assert callable(obscura.current_tool_context)
        assert callable(obscura.bind_tool_context)

        # Auth user model
        assert obscura.AuthenticatedUser is not None

        # Lazy error class is eagerly importable (so callers can `except`).
        assert issubclass(obscura.MissingExtraError, ImportError)
        print("OK")
        """
    )
    assert rc == 0, f"eager surface broken:\nstdout={out}\nstderr={err}"


def test_lazy_attribute_access_resolves() -> None:
    """Attribute access on a registered lazy name must succeed and return
    the underlying object."""
    import obscura

    client_cls = obscura.ObscuraClient  # triggers lazy load
    assert client_cls is not None
    assert client_cls.__name__ == "ObscuraClient"


def test_unknown_attribute_raises_attribute_error() -> None:
    """PEP 562 ``__getattr__`` must raise ``AttributeError`` for unknown names."""
    import obscura

    with pytest.raises(AttributeError, match="DefinitelyNotAName"):
        _ = obscura.DefinitelyNotAName  # type: ignore[attr-defined]


def test_known_names_matches_registry() -> None:
    """The lazy registry's ``known_names`` includes every entry callers expect.

    Updates to ``_LAZY`` should be reflected here as a concrete contract,
    not just an internal detail.
    """
    from obscura.lazy import known_names

    expected = {
        "AgentLoop",
        "AgentLoopV2",
        "AgentLoopV2Config",
        "AuthConfig",
        "BackendRoutingPolicy",
        "BaseAgent",
        "ContextLoader",
        "MemoryWriteRequest",
        "ObscuraClient",
        "ObscuraConfig",
        "OpenClawBridge",
        "OpenClawBridgeConfig",
        "RequestHandler",
        "RequestMetadata",
        "RunAgentRequest",
        "SemanticSearchRequest",
        "SimpleHandler",
        "SpawnAgentRequest",
        "WorkflowRunRequest",
        "is_v2_enabled",
        "make_agent_loop",
    }
    assert expected.issubset(set(known_names())), (
        f"missing lazy names: {expected - set(known_names())}"
    )
