"""Shim module for run_aper_10_agents used in tests.

Provides lightweight stubs to satisfy imports during unit/integration test
collection. These are NOT functional and will raise if executed.
"""
from __future__ import annotations

class WorkflowA2AService:
    def __init__(self, *args, **kwargs):
        # noop: placeholder for tests that only import the symbol
        pass


def run_workflow(*args, **kwargs):
    raise RuntimeError("demos.a2a.run_aper_10_agents.run_workflow is a test shim and should not be executed")

__all__ = ["WorkflowA2AService", "run_workflow"]
