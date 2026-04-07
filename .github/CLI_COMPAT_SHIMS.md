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

Open questions

- Exact release cadence to use for the deprecation window (one minor or two minors?).
- Whether to gate shims behind an env flag temporarily (OBSCURA_CLI_COMPAT=1) for CI to opt-in.

Contact

For questions or to volunteer to run the codemod, ping the reviewer team on PR #12.
