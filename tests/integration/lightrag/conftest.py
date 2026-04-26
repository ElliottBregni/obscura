"""Integration suite — real lightrag-hku, cassetted LLM calls."""

from __future__ import annotations

import os
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip every test in this directory unless `RUN_LR_INTEGRATION=1`."""
    if os.environ.get("RUN_LR_INTEGRATION") != "1":
        skip = pytest.mark.skip(reason="set RUN_LR_INTEGRATION=1 to run")
        for item in items:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def vcr_config() -> dict[str, Any]:
    """VCR config — match by method+URL, scrub auth headers."""
    return {
        "filter_headers": [
            ("authorization", "REDACTED"),
            ("x-api-key", "REDACTED"),
            ("openai-organization", "REDACTED"),
        ],
        "match_on": ["method", "scheme", "host", "port", "path", "query"],
        "record_mode": "none",
    }


@pytest.fixture(scope="session")
def cassette_dir() -> Path:
    return Path(__file__).parent / "cassettes"


@pytest.fixture
def real_lightrag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[Any, None, None]:
    """A real `LightRAG` instance pointed at a temp working_dir.

    OpenAI / embedding calls inside it are intercepted by the
    pytest-recording cassette via `@pytest.mark.vcr`.
    """
    monkeypatch.setenv("OBSCURA_LIGHTRAG", "on")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")

    from obscura.auth.models import AuthenticatedUser
    from obscura.lightrag_memory.adapter import LightRAGAdapter

    user = AuthenticatedUser(
        user_id="u-lr-integration",
        email="lr-int@test.com",
        roles=("admin",),
        org_id="org-1",
        token_type="user",
        raw_token="test",
    )
    adapter = LightRAGAdapter.for_user(user, embedding_fn=None)
    yield adapter
    adapter.close()
