import time
import importlib


def test_cli_import_is_fast():
    """Smoke test: obscura.cli should import quickly at CI collection time."""
    t0 = time.perf_counter()
    importlib.import_module("obscura.cli")
    dur_ms = (time.perf_counter() - t0) * 1000
    # 500ms threshold is conservative across CI machines
    assert dur_ms < 500, f"obscura.cli import too slow: {dur_ms:.0f}ms"
