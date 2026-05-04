"""Deprecated import location — re-exports :mod:`obscura.runtime._default_skills`.

The implementation moved to :mod:`obscura.runtime` as part of the A2 surface
split. This shim keeps existing ``from obscura.core._default_skills import …``
callers working; new code should import from the new location directly.
"""

from __future__ import annotations

from obscura.runtime._default_skills import DEFAULT_SKILLS

__all__ = ["DEFAULT_SKILLS"]
