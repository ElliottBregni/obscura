"""Integration test: image paste/drop forwarding wiring on the host side.

The side panel (`packages/browser-extension/src/sidepanel/sidepanel.js`)
attaches dropped/pasted images to a ``send`` frame as ``context.images =
[data-url, ...]``. The host extracts those images, hands them to
``ObscuraSession.send(images=...)``, and consumes them on the first turn
only. The end-to-end pathway is asserted by:

* `tests/browser_extension/test_browser_tools.py` for ToolSpec wiring.
* This test verifies the `ObscuraSession.send()` API contract — the
  multimodal kwargs are present on the public surface so the host's
  ``_handle_send`` (which is private to the host script) has somewhere
  to plumb context.images / context.attached_files into.
"""

from __future__ import annotations

import inspect

import pytest

from obscura.cli.session import ObscuraSession

pytestmark = pytest.mark.integration


def test_session_send_accepts_images_and_attached_files() -> None:
    sig = inspect.signature(ObscuraSession.send)
    params = sig.parameters
    assert "images" in params, (
        "ObscuraSession.send must accept an `images` kwarg so the host can "
        "forward context.images from the panel"
    )
    assert "attached_files" in params, (
        "ObscuraSession.send must accept an `attached_files` kwarg so the "
        "host can forward context.attached_files from the panel"
    )
    # Defaults must be None — the terminal REPL never passes either kwarg.
    assert params["images"].default is None
    assert params["attached_files"].default is None
