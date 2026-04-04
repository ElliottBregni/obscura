"""Eval-driven tool router — selects relevant tools per model turn.

Replaces the naive ``filtered[:128]`` truncation with a four-layer
selection pipeline:

1. **Pinned** — core tools that must always be present
2. **Quality gate** — exclude tools below a minimum quality score
3. **Capability match** — keyword-match prompt against capability descriptions
4. **Score rank** — fill remaining budget from highest-quality tools

An optional fifth step reorders the selected set using
:class:`EvalMemory` context-aware recall signals.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from obscura.core.compiler.compiled import ToolRoutingConfig
    from obscura.core.tool_score_index import ToolScoreIndex
    from obscura.core.types import ToolSpec

logger = logging.getLogger(__name__)

# Per-backend hard limits on tool definitions in a single request.
BACKEND_TOOL_LIMITS: dict[str, int] = {
    "copilot": 128,
    "claude": 128,
    "openai": 128,
    "localllm": 256,
    "codex": 128,
    "moonshot": 64,
}

# Core tools that are always included regardless of routing.
DEFAULT_PINNED_TOOLS: frozenset[str] = frozenset(
    {
        "run_shell",
        "read_text_file",
        "write_text_file",
        "edit_text_file",
        "list_directory",
        "grep_files",
        "find_files",
        "git_status",
    },
)


# ---------------------------------------------------------------------------
# Routing result
# ---------------------------------------------------------------------------


@dataclass
class RoutingResult:
    """Output of a single routing decision."""

    tools: list[ToolSpec]
    pinned: list[str] = field(default_factory=list[str])
    capability_matched: list[str] = field(default_factory=list[str])
    score_ranked: list[str] = field(default_factory=list[str])
    dropped_count: int = 0
    quarantined_count: int = 0


# ---------------------------------------------------------------------------
# Lightweight keyword tokeniser
# ---------------------------------------------------------------------------


_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> set[str]:
    """Extract lowercase alphanumeric tokens from *text*."""
    return set(_TOKEN_RE.findall(text.lower()))


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


class ToolRouter:
    """Selects a relevant subset of tools for each model turn.

    Parameters
    ----------
    config : ToolRoutingConfig
        Frozen routing configuration from the compiled agent.
    score_index : ToolScoreIndex
        Live quality-score tracker fed by broker audit entries.
    eval_memory : object | None
        Optional :class:`EvalMemory` instance for context-aware recall.
    capability_descriptions : dict[str, str] | None
        ``{capability_id: description}`` for keyword matching.
    capability_tool_map : dict[str, list[str]] | None
        ``{capability_id: [tool_name, ...]}`` mapping capabilities to tools.
    default_grant_tools : set[str] | None
        Tool names from capabilities with ``default_grant=true``.
    quarantined_tools : set[str] | None
        Tool names quarantined at registration time.
    backend : str
        Backend name for looking up the hard tool limit.

    """

    def __init__(
        self,
        config: ToolRoutingConfig,
        score_index: ToolScoreIndex,
        eval_memory: Any | None = None,
        capability_descriptions: dict[str, str] | None = None,
        capability_tool_map: dict[str, list[str]] | None = None,
        default_grant_tools: set[str] | None = None,
        quarantined_tools: set[str] | None = None,
        backend: str = "copilot",
    ) -> None:
        self._config = config
        self._score_index = score_index
        self._eval_memory = eval_memory
        self._cap_descriptions = capability_descriptions or {}
        self._cap_tool_map = capability_tool_map or {}
        self._default_grant_tools = default_grant_tools or set()
        self._quarantined = quarantined_tools or set()
        self._backend = backend
        self._file_context: list[str] = []

    def set_file_context(self, file_paths: list[str]) -> None:
        """Update the file context for context-aware tool recall."""
        self._file_context = list(file_paths)

    # -- Factory -------------------------------------------------------------

    @classmethod
    def from_capability_index(
        cls,
        config: ToolRoutingConfig,
        score_index: ToolScoreIndex,
        capability_index: Any,
        *,
        eval_memory: Any | None = None,
        quarantined_tools: set[str] | None = None,
        backend: str = "copilot",
    ) -> ToolRouter:
        """Build a router from a live :class:`CapabilityIndex`.

        Extracts capability descriptions and tool maps directly from the
        index, so the router has full capability-matching data.
        """
        cap_descriptions: dict[str, str] = {}
        cap_tool_map: dict[str, list[str]] = {}
        default_grant_tools: set[str] = set()

        try:
            for cap in capability_index.list_all():
                cap_descriptions[cap.id] = cap.description
                cap_tool_map[cap.id] = list(cap.tools)
                if cap.default_grant:
                    default_grant_tools.update(cap.tools)
        except Exception:
            logger.debug("Failed to extract capability data from index", exc_info=True)

        return cls(
            config=config,
            score_index=score_index,
            eval_memory=eval_memory,
            capability_descriptions=cap_descriptions,
            capability_tool_map=cap_tool_map,
            default_grant_tools=default_grant_tools,
            quarantined_tools=quarantined_tools,
            backend=backend,
        )

    # -- Public API ----------------------------------------------------------

    def select(
        self,
        prompt: str,
        available_tools: list[ToolSpec],
        file_context: list[str] | None = None,
    ) -> RoutingResult:
        """Select the most relevant tools for *prompt*.

        Parameters
        ----------
        prompt : str
            The current user/system prompt text.
        available_tools : list[ToolSpec]
            Policy-filtered tool list (output of ``ToolPolicy.filter_tools``).
        file_context : list[str] | None
            Active file paths for EvalMemory context-aware recall.

        Returns
        -------
        RoutingResult
            Selected tools with per-tier attribution.

        """
        if not self._config.enabled:
            return RoutingResult(tools=available_tools)

        # Use stored file_context if caller doesn't provide one
        if file_context is None and self._file_context:
            file_context = self._file_context

        try:
            return self._do_select(prompt, available_tools, file_context)
        except Exception:
            logger.warning(
                "Tool routing failed — falling back to full list",
                exc_info=True,
            )
            hard_limit = BACKEND_TOOL_LIMITS.get(self._backend, 128)
            cap = min(self._config.max_tools, hard_limit)
            return RoutingResult(
                tools=available_tools[:cap],
                dropped_count=max(len(available_tools) - cap, 0),
            )

    # -- Internal ------------------------------------------------------------

    def _do_select(
        self,
        prompt: str,
        available_tools: list[ToolSpec],
        file_context: list[str] | None,
    ) -> RoutingResult:
        hard_limit = BACKEND_TOOL_LIMITS.get(self._backend, 128)
        max_tools = min(self._config.max_tools, hard_limit)
        tool_by_name: dict[str, ToolSpec] = {t.name: t for t in available_tools}

        # Track which tier each tool was selected from.
        selected: dict[str, str] = {}  # name → tier
        quarantined_count = 0

        # ----- Layer 1: Pinned ------------------------------------------------
        pinned_names = set(self._config.pinned_tools) | DEFAULT_PINNED_TOOLS
        if self._config.pin_default_capabilities:
            pinned_names |= self._default_grant_tools

        for name in pinned_names:
            if name in tool_by_name and name not in self._quarantined:
                selected[name] = "pinned"

        # ----- Layer 2: Quality gate ------------------------------------------
        if self._config.use_quality_scores:
            gated_out: set[str] = set()
            for t in available_tools:
                if t.name in selected or t.name in self._quarantined:
                    continue
                score = self._score_index.get_score(t.name)
                if score.quality_score < self._config.min_quality_score:
                    gated_out.add(t.name)
            # Also count quarantined as excluded
            quarantined_count = sum(
                1 for t in available_tools if t.name in self._quarantined
            )
        else:
            gated_out = set()

        # Pool of tools eligible for selection (not pinned, not gated, not quarantined).
        eligible = [
            t
            for t in available_tools
            if t.name not in selected
            and t.name not in gated_out
            and t.name not in self._quarantined
        ]

        # ----- Layer 3: Capability match --------------------------------------
        if len(selected) < max_tools:
            matched_caps = self._match_capabilities(prompt)
            for cap_id in matched_caps:
                for tool_name in self._cap_tool_map.get(cap_id, []):
                    if (
                        tool_name in tool_by_name
                        and tool_name not in selected
                        and tool_name not in gated_out
                        and tool_name not in self._quarantined
                    ):
                        selected[tool_name] = "capability"
                        if len(selected) >= max_tools:
                            break
                if len(selected) >= max_tools:
                    break

        # ----- Layer 4: Score rank (fill remaining budget) --------------------
        if len(selected) < max_tools:
            remaining = [t.name for t in eligible if t.name not in selected]
            ranked = self._score_index.ranked(remaining)
            for name in ranked:
                if name not in selected:
                    selected[name] = "score"
                    if len(selected) >= max_tools:
                        break

        # ----- Layer 5: Context adjust (reorder, don't add/remove) -----------
        ordered_names = list(selected.keys())
        if (
            self._config.use_context_recall
            and self._eval_memory is not None
            and file_context
        ):
            ordered_names = self._apply_context_recall(
                ordered_names,
                file_context,
            )

        # Build final result.
        result_tools = [tool_by_name[n] for n in ordered_names if n in tool_by_name]
        total_available = len(available_tools)

        return RoutingResult(
            tools=result_tools,
            pinned=[n for n, t in selected.items() if t == "pinned"],
            capability_matched=[n for n, t in selected.items() if t == "capability"],
            score_ranked=[n for n, t in selected.items() if t == "score"],
            dropped_count=total_available - len(result_tools),
            quarantined_count=quarantined_count,
        )

    # -- Capability matching -------------------------------------------------

    def _match_capabilities(self, prompt: str) -> list[str]:
        """Score capabilities against *prompt* using keyword overlap."""
        prompt_tokens = _tokenize(prompt)
        if not prompt_tokens:
            # No signal — include all known capabilities.
            return list(self._cap_descriptions.keys())

        scored: list[tuple[str, float]] = []
        for cap_id, desc in self._cap_descriptions.items():
            desc_tokens = _tokenize(desc)
            if not desc_tokens:
                continue
            overlap = len(prompt_tokens & desc_tokens) / len(desc_tokens)
            if overlap >= self._config.capability_match_threshold:
                scored.append((cap_id, overlap))

        scored.sort(key=lambda x: x[1], reverse=True)
        return [cap_id for cap_id, _ in scored]

    # -- Context recall ------------------------------------------------------

    def _apply_context_recall(
        self,
        tool_names: list[str],
        file_paths: list[str],
    ) -> list[str]:
        """Reorder *tool_names* using EvalMemory context signals.

        Tools with recent failures on the given files are moved to the end;
        tools with recent successes are moved to the front (after pinned).
        """
        if self._eval_memory is None:
            return tool_names

        try:
            warnings = self._eval_memory.recall_for_context(
                tool_names=tool_names,
                file_paths=file_paths,
                top_k=10,
            )
        except Exception:
            logger.debug(
                "EvalMemory recall failed — skipping context adjust",
                exc_info=True,
            )
            return tool_names

        if not warnings:
            return tool_names

        # Parse warning strings for tool names that have failures.
        failed_tools: set[str] = set()
        for warning in warnings:
            lower = warning.lower()
            for name in tool_names:
                if name.lower() in lower and "fail" in lower:
                    failed_tools.add(name)

        if not failed_tools:
            return tool_names

        # Move failed tools to the end.
        boosted = [n for n in tool_names if n not in failed_tools]
        penalised = [n for n in tool_names if n in failed_tools]
        return boosted + penalised


__all__ = [
    "BACKEND_TOOL_LIMITS",
    "DEFAULT_PINNED_TOOLS",
    "RoutingResult",
    "ToolRouter",
]
