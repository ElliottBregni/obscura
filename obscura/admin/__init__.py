"""Administrative operations that cross-cut subsystems.

Lives outside ``obscura/auth`` because these operations act on data
produced by many modules (memory, vector memory, event log, kairos,
notifications, audit) and coordinating them from any one of those
modules creates circular dependencies and unclear ownership.

Right now the only entry point is user-data deletion — a SOC2 C1 / P-series
control. Future additions (bulk export, retention purge, legal hold)
belong here for the same reason.
"""

from __future__ import annotations

from obscura.admin.deletion import (
    DeletionError,
    DeletionReceipt,
    delete_user_data,
)

__all__ = [
    "DeletionError",
    "DeletionReceipt",
    "delete_user_data",
]
