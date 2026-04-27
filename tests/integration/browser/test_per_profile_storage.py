"""Integration test: per-profile storage scoping end-to-end.

The browser-extension architecture roadmap (item 4.1 in
``packages/browser-extension/ARCHITECTURE.md``) calls out that two Chrome
profiles running obscura on the same machine today share ``events.db`` and
can corrupt session ids. The remediation is a per-profile root:

* ``profile_id`` on ``SessionConfig``.
* ``_resolve_profile_home`` in ``obscura/cli/session.py`` returning
  ``~/.obscura/profiles/<id>/`` when set, falling back to
  ``~/.obscura/`` when ``profile_id is None``.
* ``ObscuraSession.create()`` stores ``events.db`` under that path.

This pathway is currently unimplemented. The skip below captures the test
shape so the next person to add per-profile scoping flips it on.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


@pytest.mark.skip(
    reason=(
        "TODO: per-profile storage scoping not implemented yet. "
        "obscura/cli/session.py:SessionConfig has no `profile_id` field; "
        "ObscuraSession.create() unconditionally writes "
        "`SQLiteEventStore(resolve_obscura_home() / 'events.db')`. "
        "Once SessionConfig grows `profile_id: str | None = None` and "
        "create() routes through a `_resolve_profile_home` helper, "
        "unskip and assert: "
        "(a) profile_id='alice' creates events.db under "
        "~/.obscura/profiles/alice/; "
        "(b) profile_id='bob' uses ~/.obscura/profiles/bob/; "
        "(c) profile_id=None falls back to legacy ~/.obscura/events.db. "
        "Drive a real ObscuraSession.create() with a mocked backend "
        "(see tests/integration/test_cli_integration.py for the pattern)."
    )
)
@pytest.mark.asyncio
async def test_profile_id_scopes_events_db_path() -> None:
    """Placeholder for the per-profile storage scoping integration test."""
    msg = "per-profile scoping not implemented in SessionConfig yet"
    raise NotImplementedError(msg)
