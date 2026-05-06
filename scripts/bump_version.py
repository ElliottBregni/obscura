#!/usr/bin/env python3
"""Compute and apply a semver bump from commit messages.

Reads commits since the last `vX.Y.Z` tag and looks for the keywords
`major`, `minor`, or `patch` (case-insensitive) in any commit message.
The highest precedence wins (major > minor > patch). If none of the
keywords appear, no bump happens and the script exits 0 with
`bump=none` written to GITHUB_OUTPUT (when running in CI).

Recognized keyword forms in a commit message (only the **first line**
is examined, and the keyword must be an explicit marker — plain
English words like "monkey-patch" do not trigger a bump):

    major: rewrite the agent loop      → major
    minor: add new tool                → minor
    patch: fix off-by-one              → patch
    [major] / [minor] / [patch] ...    → bracket form
    (major) / (minor) / (patch) ...    → paren form
    feat!: breaking change             → major (conventional-commits ! suffix)

When a bump is selected, the script rewrites:

    - pyproject.toml                          (top-level `version = "..."`)
    - packages/browser-extension/package.json (top-level "version")
    - packages/browser-extension/manifest.json(top-level "version")

Both `package.json` and `manifest.json` always track `pyproject.toml` —
if they have drifted, they're snapped to the new version.

Usage:

    python scripts/bump_version.py            # bump in place + write outputs
    python scripts/bump_version.py --dry-run  # report only, no writes
    python scripts/bump_version.py --since vX.Y.Z   # override base ref
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Literal

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
EXT_PACKAGE_JSON = REPO_ROOT / "packages" / "browser-extension" / "package.json"
EXT_MANIFEST_JSON = REPO_ROOT / "packages" / "browser-extension" / "manifest.json"

BumpKind = Literal["major", "minor", "patch", "none"]

_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_PYPROJECT_VERSION_RE = re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE)

# Only fire on explicit semver markers at, or near, the start of the
# subject line. "monkey-patch" or "stage minor refactor" must NOT trigger.
_KEYWORD_PREFIX_RE = re.compile(
    r"^\s*"                            # optional leading whitespace
    r"(?:\[|\()?"                      # optional [ or (
    r"(major|minor|patch)"             # the keyword
    r"(?:\]|\))?"                      # optional ] or )
    r"\s*[:\-]",                       # required ":" or "-" separator
    re.IGNORECASE,
)
_BREAKING_BANG_RE = re.compile(r"^[a-zA-Z]+(\([^)]+\))?!:")


def _run(cmd: list[str], *, check: bool = True) -> str:
    out = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, check=False)
    if check and out.returncode != 0:
        raise SystemExit(f"command failed: {' '.join(cmd)}\n{out.stderr}")
    return out.stdout.strip()


def last_version_tag() -> str | None:
    tags = _run(["git", "tag", "--sort=-version:refname"]).splitlines()
    for tag in tags:
        if _VERSION_RE.match(tag.lstrip("v")):
            return tag
    return None


def commits_since(ref: str | None) -> list[str]:
    if ref:
        log_range = f"{ref}..HEAD"
        out = _run(["git", "log", log_range, "--format=%B%x1f", "--no-merges"])
    else:
        out = _run(["git", "log", "--format=%B%x1f", "--no-merges"])
    return [m.strip() for m in out.split("\x1f") if m.strip()]


def classify(messages: list[str]) -> BumpKind:
    seen: set[str] = set()
    for msg in messages:
        first_line = msg.splitlines()[0] if msg else ""
        if _BREAKING_BANG_RE.match(first_line):
            seen.add("major")
        m = _KEYWORD_PREFIX_RE.match(first_line)
        if m:
            seen.add(m.group(1).lower())
    if "major" in seen:
        return "major"
    if "minor" in seen:
        return "minor"
    if "patch" in seen:
        return "patch"
    return "none"


def read_pyproject_version() -> str:
    text = PYPROJECT.read_text()
    m = _PYPROJECT_VERSION_RE.search(text)
    if not m:
        raise SystemExit("pyproject.toml: could not find top-level `version = \"...\"`")
    return m.group(1)


def bump_string(version: str, kind: BumpKind) -> str:
    m = _VERSION_RE.match(version)
    if not m:
        raise SystemExit(f"unrecognized version string: {version!r}")
    major, minor, patch = (int(m.group(i)) for i in (1, 2, 3))
    if kind == "major":
        return f"{major + 1}.0.0"
    if kind == "minor":
        return f"{major}.{minor + 1}.0"
    if kind == "patch":
        return f"{major}.{minor}.{patch + 1}"
    return version


def write_pyproject(new_version: str) -> None:
    text = PYPROJECT.read_text()
    new_text, count = _PYPROJECT_VERSION_RE.subn(
        f'version = "{new_version}"', text, count=1
    )
    if count != 1:
        raise SystemExit("pyproject.toml: failed to rewrite version line")
    PYPROJECT.write_text(new_text)


def write_json_version(path: Path, new_version: str) -> None:
    if not path.exists():
        return
    data = json.loads(path.read_text())
    data["version"] = new_version
    path.write_text(json.dumps(data, indent=2) + "\n")


def emit_github_output(**kv: str) -> None:
    out_path = os.environ.get("GITHUB_OUTPUT")
    if not out_path:
        return
    with open(out_path, "a", encoding="utf-8") as fh:
        for k, v in kv.items():
            fh.write(f"{k}={v}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="print the plan, don't write files")
    parser.add_argument("--since", help="override the base ref (default: last vX.Y.Z tag)")
    args = parser.parse_args()

    base = args.since if args.since is not None else last_version_tag()
    messages = commits_since(base)
    kind = classify(messages)
    current = read_pyproject_version()
    new_version = bump_string(current, kind)

    print(f"base ref:      {base or '(none)'}")
    print(f"commits read:  {len(messages)}")
    print(f"current:       {current}")
    print(f"bump:          {kind}")
    print(f"new:           {new_version}")

    emit_github_output(
        bump=kind,
        current=current,
        new_version=new_version,
        tag=f"v{new_version}",
    )

    if kind == "none":
        print("no bump keyword found; leaving files untouched")
        return 0

    if args.dry_run:
        print("--dry-run: not writing files")
        return 0

    write_pyproject(new_version)
    write_json_version(EXT_PACKAGE_JSON, new_version)
    write_json_version(EXT_MANIFEST_JSON, new_version)
    print(f"wrote pyproject.toml + browser-extension files at {new_version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
