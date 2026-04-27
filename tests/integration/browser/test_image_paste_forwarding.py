"""Integration test: image paste/drop forwarding from panel through host.

The side panel (`packages/browser-extension/src/sidepanel/sidepanel.js`)
attaches dropped/pasted images to a ``send`` frame as ``context.images =
[data-url, ...]``. The host should:

1. Strip those images out of ``context`` before assembling the prompt
   (so they don't get injected as text via ``_assemble_prompt``).
2. Forward them to ``ObscuraSession.send(...)`` as an ``images=...`` kwarg.
3. Consume them only on the **first** turn — when the model issues a tool
   call and the host loops back to send the tool result, the images must
   not be replayed on the follow-up turn.

This pathway is currently unimplemented in the host. The skip below is a
placeholder so the test slot exists and the next person to add image
forwarding flips the skip into an assertion.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


@pytest.mark.skip(
    reason=(
        "TODO: host-side image-paste forwarding not implemented yet. "
        "obscura/cli/session.py:ObscuraSession.send() takes no `images` kwarg; "
        "packages/browser-extension/native-host/obscura_native_host.py:"
        "_handle_send / _assemble_prompt drop the `context.images` field on "
        "the floor. Once the host extracts `context.images` and pipes them "
        "through to session.send(images=..., consume_after=1), unskip this "
        "test and assert: "
        "(a) session.send is called once with images=[data-url, ...]; "
        "(b) the stub multimodal backend sees images on turn 1 only; "
        "(c) on a tool-result turn, no images are replayed."
    )
)
@pytest.mark.asyncio
async def test_image_paste_forwarded_first_turn_only() -> None:
    """Placeholder for the image-paste round-trip integration test."""
    msg = "image-paste forwarding not implemented in host yet"
    raise NotImplementedError(msg)
