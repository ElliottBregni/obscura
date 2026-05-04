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


# TODO(phase 6): retype `ToolSpec.side_effects: str` (in `core/types.py`,
# owned by Team Agent) to use this enum. Defined here so Team Agent has
# something to import when their phase lands.
class SideEffects(StrEnum):
    NONE = "none"
    READ = "read"
    WRITE = "write"


# TODO(phase 6): retype `ToolChoice.mode: str` (in `core/types.py`,
# owned by Team Agent). Defined now to lock the canonical wire values.
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


__all__ = [
    "BashRisk",
    "ChangeKind",
    "HTTPMethod",
    "SideEffects",
    "ToolChoiceMode",
    "UIMode",
]
