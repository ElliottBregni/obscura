import os
import warnings
import importlib
import pytest


@pytest.mark.skipif(
    os.getenv("OBSCURA_ENABLE_SHIM_DEPRECATION") is None,
    reason="Deprecation smoke test is opt-in via OBSCURA_ENABLE_SHIM_DEPRECATION",
)
def test_deprecation_warning_on_shim_import():
    """Optional smoke test: importing the shim should emit DeprecationWarning when enabled."""
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        mod = importlib.import_module("obscura.cli")
        # Access a known shim entrypoint
        _ = getattr(mod, "console", None)
        assert any(isinstance(x.message, DeprecationWarning) for x in w)
