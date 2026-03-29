#!/usr/bin/env python3
"""Convert YAML config files to TOML.

Usage:
    python scripts/yaml_to_toml.py <path_or_glob> [--delete-yaml] [--dry-run]

Examples:
    python scripts/yaml_to_toml.py obscura/plugins/builtins/
    python scripts/yaml_to_toml.py .obscura/specs/ --delete-yaml
    python scripts/yaml_to_toml.py .obscura/agents.yaml --dry-run
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path
from typing import Any

import tomli_w
import yaml


def _strip_nulls(obj: Any) -> Any:
    """Recursively remove keys with None values (TOML has no null)."""
    if isinstance(obj, dict):
        return {k: _strip_nulls(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [_strip_nulls(item) for item in obj]
    return obj


def convert_file(yaml_path: Path, *, delete_yaml: bool = False, dry_run: bool = False) -> bool:
    """Convert a single YAML file to TOML.  Returns True on success."""
    toml_path = yaml_path.with_suffix(".toml")

    try:
        data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"  ERROR reading {yaml_path}: {exc}", file=sys.stderr)
        return False

    if not isinstance(data, dict):
        print(f"  SKIP {yaml_path}: not a mapping", file=sys.stderr)
        return False

    clean = _strip_nulls(data)

    try:
        toml_bytes = tomli_w.dumps(clean)
    except Exception as exc:
        print(f"  ERROR serialising {yaml_path}: {exc}", file=sys.stderr)
        return False

    # Validate round-trip
    try:
        roundtrip = tomllib.loads(toml_bytes)
        if roundtrip != clean:
            print(f"  WARNING {yaml_path}: round-trip mismatch (proceeding anyway)", file=sys.stderr)
    except Exception as exc:
        print(f"  ERROR round-trip validation for {yaml_path}: {exc}", file=sys.stderr)
        return False

    if dry_run:
        print(f"  DRY-RUN {yaml_path} -> {toml_path}")
        return True

    toml_path.write_text(toml_bytes, encoding="utf-8")
    print(f"  OK {yaml_path} -> {toml_path}")

    if delete_yaml:
        yaml_path.unlink()
        print(f"     deleted {yaml_path}")

    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert YAML configs to TOML")
    parser.add_argument("path", help="File or directory to convert")
    parser.add_argument("--delete-yaml", action="store_true", help="Delete YAML files after conversion")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done")
    args = parser.parse_args()

    target = Path(args.path)
    files: list[Path] = []

    if target.is_file():
        files = [target]
    elif target.is_dir():
        files = sorted(target.rglob("*.yaml")) + sorted(target.rglob("*.yml"))
    else:
        print(f"Not found: {target}", file=sys.stderr)
        sys.exit(1)

    if not files:
        print("No YAML files found.")
        return

    print(f"Converting {len(files)} file(s)...")
    ok = sum(1 for f in files if convert_file(f, delete_yaml=args.delete_yaml, dry_run=args.dry_run))
    print(f"\nDone: {ok}/{len(files)} converted.")


if __name__ == "__main__":
    main()
