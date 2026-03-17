"""Discover and parse TOML-based eval suite specifications."""

from __future__ import annotations

import tomllib
from pathlib import Path

from obscura.core.paths import resolve_all_evals_dirs
from obscura.eval.specs import EvalSuiteSpec


def discover_eval_files(dirs: list[Path] | None = None) -> list[Path]:
    """Find all ``.toml`` eval files in the given or default directories.

    Directories are searched in merge order (global first, local last).
    """
    if dirs is None:
        dirs = resolve_all_evals_dirs()

    files: list[Path] = []
    for d in dirs:
        if not d.is_dir():
            continue
        files.extend(sorted(d.glob("*.toml")))
    return files


def load_eval_suite(path: Path) -> EvalSuiteSpec:
    """Parse a single TOML file into an ``EvalSuiteSpec``."""
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    return EvalSuiteSpec.model_validate(raw)


def load_all_eval_suites(
    dirs: list[Path] | None = None,
) -> list[EvalSuiteSpec]:
    """Discover and parse all eval suites from the default directories."""
    suites: list[EvalSuiteSpec] = []
    for path in discover_eval_files(dirs):
        suites.append(load_eval_suite(path))
    return suites
