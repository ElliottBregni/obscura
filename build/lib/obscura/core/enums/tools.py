"""Tool-domain enums.

Promotes the loose string literals scattered across `bash_classifier`,
`file_watcher`, `tools/system/_ui.py`, and `tools/memory_tools.py` into
typed `StrEnum`s, plus pre-declares the enums that `core/types.py` will
adopt in Phase 6 (`SideEffects`, `ToolChoiceMode`, `HTTPMethod`). The
`core/types.py` callers are not retyped here — Team Agent owns that
file. The enums simply exist so Phase 6 has somewhere to import from.
"""

from __future__ import annotations

from enum import StrEnum


class BashRisk(StrEnum):
    SAFE = "safe"
    NEEDS_REVIEW = "needs-review"
    DANGEROUS = "dangerous"


class ChangeKind(StrEnum):
    CREATED = "created"
    MODIFIED = "modified"
    DELETED = "deleted"


class UIMode(StrEnum):
    PERMISSION = "permission"
    NOTIFY = "notify"
    QUESTION = "question"
    MULTI_SELECT = "multi_select"


class SideEffects(StrEnum):
    NONE = "none"
    READ = "read"
    WRITE = "write"
    # ``MUTATING`` covers state-changing operations that don't fit
    # READ / WRITE — e.g. browser clicks, form submissions, navigation,
    # iMessage sends. Conservative: never speculated, always prompts
    # for confirmation. Used by browser tools and other UI-driving
    # integrations.
    MUTATING = "mutating"
    # Catch-all for tools whose effect set isn't yet classified. Treated
    # as MUTATING for safety (no speculation, prompts on confirmation).
    UNKNOWN = "unknown"


class ToolChoiceMode(StrEnum):
    AUTO = "auto"
    NONE = "none"
    REQUIRED = "required"
    FUNCTION = "function"


class HTTPMethod(StrEnum):
    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    DELETE = "DELETE"
    PATCH = "PATCH"
    HEAD = "HEAD"
    OPTIONS = "OPTIONS"


class ContentBlockKind(StrEnum):
    """Discriminator for the four message ContentBlock variants."""

    TEXT = "text"
    THINKING = "thinking"
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"


class CompilerSpecKind(StrEnum):
    """Discriminator for the five compiler spec types loaded from YAML."""

    TEMPLATE = "Template"
    AGENT = "Agent"
    POLICY = "Policy"
    PACK = "Pack"
    WORKSPACE = "Workspace"


__all__ = [
    "BashRisk",
    "ChangeKind",
    "CompilerSpecKind",
    "ContentBlockKind",
    "HTTPMethod",
    "SideEffects",
    "ToolChoiceMode",
    "UIMode",
]
