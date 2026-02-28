#!/usr/bin/env python3
"""Print GitHub CLI OAuth token from hosts.yml.

This allows containerized services to inherit host GitHub OAuth state
without requiring `gh` to be installed in the container.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


def extract_token(hosts_yml: str) -> str | None:
    # Matches:
    # github.com:
    #   oauth_token: ghp_xxx
    m = re.search(r"(?m)^\s*oauth_token:\s*(\S+)\s*$", hosts_yml)
    if not m:
        return None
    token = m.group(1).strip().strip("'\"")
    return token or None


def main() -> int:
    path = Path("/home/obscura/.config/gh/hosts.yml")
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return 1

    token = extract_token(content)
    if not token:
        return 1
    print(token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
