"""obscura.data.goals — goal-board repository.

Phase 4 of the data-layer migration. This phase **wraps** the existing
:class:`obscura.kairos.goals.GoalBoard` rather than moving 450 lines of
working code — Protocol + factory live here, the markdown-file
implementation stays in ``obscura/kairos/goals.py`` for now. Future
cleanup will fold the implementation into ``obscura/data/goals/markdown.py``
and convert the legacy module into a re-export shim.

Public API:

* :class:`Goal` — value type (re-exported from ``obscura.kairos.goals``)
* :class:`GoalRepo` — Protocol every backend implements
* :func:`get_goal_repo` — factory; markdown-file backend today
"""

from __future__ import annotations

from obscura.data.goals.factory import (
    get_goal_repo as get_goal_repo,
)
from obscura.data.goals.protocol import (
    Goal as Goal,
)
from obscura.data.goals.protocol import (
    GoalRepo as GoalRepo,
)
