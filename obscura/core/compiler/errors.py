"""obscura.core.compiler.errors — Error types for the compile pipeline."""

from __future__ import annotations


class CompileError(Exception):
    """Base class for compilation errors."""

    def __init__(self, message: str, *, source: str = "") -> None:
        self.source = source
        super().__init__(message)


class SpecLoadError(CompileError):
    """Failed to load or parse a spec file."""


class SpecValidationError(CompileError):
    """A spec file is structurally valid YAML but semantically invalid."""


class ResolutionError(CompileError):
    """Failed to resolve a reference (template extends, policy ref, etc.)."""


class MergeError(CompileError):
    """Conflict during merge that cannot be resolved automatically."""


class PluginFilterError(CompileError):
    """A requested plugin is not available or is excluded by policy."""
