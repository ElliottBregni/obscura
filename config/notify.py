from __future__ import annotations

import os
from pathlib import Path

DEFAULT_DB = f"sqlite:///{Path.home() / '.obscura' / 'notify.db'}"


def get_notify_db_url() -> str:
    return os.environ.get("NOTIFY_DATABASE_URL", DEFAULT_DB)
