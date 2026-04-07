CLI Compatibility Shims — Purpose & Deprecation Plan

Context

During the cli API refactor, a small set of compatibility shims were introduced to keep obscura.cli cheap to import and to avoid breaking tests and downstream consumers that monkeypatch top-level symbols. These shims are intentionally small and documented here so maintainers can follow a safe removal path.

Current shims

- console: lightweight NullConsole exposed at obscura.cli.console so tests can monkeypatch console.print and capture warnings.
- confirm_prompt_async: top-level async wrapper that lazily delegates to the real prompt implementation.
- _FILE_WRITE_TOOLS: frozenset default of file-writing tool names used by /diff tracking.
- lazy bootstrap wrappers: thin wrappers in obscura/cli/__init__.py that delegate to obscura.cli.bootstrap at runtime.

Goals

1. Keep the shims available long enough for consumers/tests to migrate to the new public API.
2. Ensure IDEs, linters and CI do not depend on the shims as permanent public surface.
3. Remove the shims on a predictable, documented schedule with automated tests and migration guides.

Deprecation policy (proposed)

- Deprecation window: keep shims for one minor release cycle (e.g., 1.0 -> 1.1) after the refactor merges.
- Warning: Add runtime DeprecationWarning when a shim is imported or called. Tests must assert warnings where appropriate.
- Migration docs: publish examples showing the new preferred APIs (e.g., import obscura.cli.api.get_console() or use obscura.cli.bootstrap helpers).
- Codemod: prepare a small codemod (python script using lib2to3 or libcst) to patch common import sites in the codebase and examples.
- Removal: after the deprecation window and a successful CI period, open a removal PR that deletes shim code and updates references.

Testing & CI

- Add smoke tests that import obscura.cli in isolation to assert low import time and no heavy dependencies loaded during collection.
- Add tests that assert DeprecationWarning for shim imports, so downstream test suites can adapt.

Migration checklist for callers

- Replace obscura.cli.console usages with the new API (documented examples in the Migration section below).
- For code that monkeypatches console.print in tests, update to monkeypatch obscura.cli.api.get_console() where available.
- Replace direct uses of confirm_prompt_async with obscura.cli.prompt.confirm_prompt_async when running full REPL startup.

Migration examples (before -> after)

Before (testing code that monkeypatches top-level console):

    # tests/test_example.py
    import obscura.cli as cli

    def test_something(monkeypatch):
        printed = []
        monkeypatch.setattr(cli.console, "print", lambda *a, **k: printed.append(a))
        # ... exercise code that emits warnings via cli.console.print ...

After (preferred public API):

    # tests/test_example.py
    from obscura.cli.api import get_console

    def test_something(monkeypatch):
        console = get_console()
        printed = []
        monkeypatch.setattr(console, "print", lambda *a, **k: printed.append(a))
        # ... exercise code that emits warnings via get_console().print ...

Before (calling confirm shim):

    await obscura.cli.confirm_prompt_async("Proceed?")

After (explicit prompt module):

    from obscura.cli.prompt import confirm_prompt_async
    await confirm_prompt_async("Proceed?")

Exact timeline (example)

- T+0 (merge): keep shims in place and mark as deprecated via warnings.
- T+1 minor release (~4–8 weeks typical cadence): continue shims, collect telemetry/feedback from CI and callers.
- T+2 minor release: remove shims if migration coverage is sufficient and no major blockers remain.

Codemod stub (libcst)

A simple libcst-based codemod can be used to replace common top-level import sites. Example scaffold (save as tools/codemods/cli_shim_migration.py):

    # tools/codemods/cli_shim_migration.py
    import libcst as cst
    from libcst import matchers as m

    class ReplaceCLIShimImports(cst.CSTTransformer):
        def leave_ImportFrom(self, node: cst.ImportFrom, updated: cst.ImportFrom) -> cst.CSTNode:
            # Replace: from obscura import cli -> from obscura.cli.api import get_console
            if isinstance(node.module, cst.Attribute) and node.module.attr.value == "cli":
                # no-op scaffold; implement specific name mappings here
                return updated
            return updated

    if __name__ == "__main__":
        print("Scaffold codemod — implement mappings and run with libcst CLI")

CI smoke checks

- Import-time smoke: add a test that imports obscura.cli in isolation and asserts import returns quickly (e.g., < 200ms on CI machines). Example pytest snippet:

    import time
    import importlib

    def test_cli_import_is_fast():
        t0 = time.perf_counter()
        importlib.import_module("obscura.cli")
        dur = (time.perf_counter() - t0) * 1000
        assert dur < 200, f"obscura.cli import too slow: {dur:.0f}ms"

- Deprecation smoke: assert importing or calling deprecated shims emits DeprecationWarning:

    import warnings
    import importlib

    def test_deprecation_warning_on_shim_import():
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            mod = importlib.import_module("obscura.cli")
            # Accessing shim that should warn
            _ = getattr(mod, "console", None)
            assert any(isinstance(x.message, DeprecationWarning) for x in w)

Action items

- Add the CI smoke checks to tests/ci/ (or directly to tests/unit as agreed).
- Implement the codemod mappings and run on examples/docs before removal.
- Add DeprecationWarning emissions to shim entry points when ready.

Contact

For questions or to volunteer to run the codemod, ping the reviewer team on PR #12.
