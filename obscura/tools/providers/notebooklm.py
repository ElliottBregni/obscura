"""NotebookLM provider — uses notebooklm_mcp Python API directly.

This provider imports ``NotebookLMClient`` from the ``notebooklm_mcp``
package (installed as part of the ``notebooklm-mcp`` pip package) and
calls it directly — no subprocess or CLI binary required.

Auth is loaded from the cached token file written by ``notebooklm-mcp-auth``,
or from the ``NOTEBOOKLM_COOKIES`` / ``NOTEBOOKLM_CSRF_TOKEN`` /
``NOTEBOOKLM_SESSION_ID`` environment variables.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_client: Any = None


def _ensure_venv_path() -> None:
    """Inject the obscura venv site-packages into sys.path if needed.

    The running Obscura process may use a different Python interpreter than
    the one used to install dependencies into the obscura venv.  This
    function checks ``~/.obscura/venv/`` and prepends its ``site-packages``
    to ``sys.path`` so that ``notebooklm_mcp`` can be imported regardless
    of which interpreter is active.
    """
    venv_dir = Path(os.environ.get("OBSCURA_HOME", Path.home() / ".obscura")) / "venv"
    if venv_dir.is_dir():
        for sp in venv_dir.glob("lib/python*/site-packages"):
            sp_str = str(sp)
            if sp_str not in sys.path:
                sys.path.insert(0, sp_str)
                logger.debug("notebooklm: injected venv path %s", sp_str)

def _get_client() -> Any:
    global _client
    if _client is not None:
        return _client

    # Ensure the project venv is on sys.path before importing
    _ensure_venv_path()

    try:
        from notebooklm_mcp.api_client import (
            NotebookLMClient,
            extract_cookies_from_chrome_export,
        )
        from notebooklm_mcp.auth import load_cached_tokens

        cookie_header = os.environ.get("NOTEBOOKLM_COOKIES", "")
        csrf_token = os.environ.get("NOTEBOOKLM_CSRF_TOKEN", "")
        session_id = os.environ.get("NOTEBOOKLM_SESSION_ID", "")

        if cookie_header:
            cookies = extract_cookies_from_chrome_export(cookie_header)
        else:
            cached = load_cached_tokens()
            if cached:
                cookies = cached.cookies
                csrf_token = csrf_token or cached.csrf_token
                session_id = session_id or cached.session_id
            else:
                raise ValueError(
                    "No NotebookLM auth found. Run notebooklm-mcp-auth "
                    "to authenticate, or set NOTEBOOKLM_COOKIES env var."
                )

        _client = NotebookLMClient(
            cookies=cookies,
            csrf_token=csrf_token,
            session_id=session_id,
        )
        return _client
    except ImportError:
        raise RuntimeError(
            "notebooklm_mcp package not found. Install with:\n"
            "  uv pip install notebooklm-mcp\n"
            "  (or: pip install notebooklm-mcp)"
        )


# ---------------------------------------------------------------------------
# Notebook handlers
# ---------------------------------------------------------------------------


async def _handler_create_notebook(**kwargs: Any) -> dict[str, Any]:
    title = kwargs.get("title", "")
    try:
        client = _get_client()
        nb = client.create_notebook(title=title)
        if nb is None:
            return {"error": "Failed to create notebook"}
        return {"notebook_id": nb.id, "title": nb.title}
    except Exception as e:
        return {"error": str(e)}


async def _handler_list_notebooks(**kwargs: Any) -> dict[str, Any]:
    try:
        client = _get_client()
        notebooks = client.list_notebooks()
        return {
            "notebooks": [
                {"id": nb.id, "title": nb.title} for nb in notebooks
            ],
            "count": len(notebooks),
        }
    except Exception as e:
        return {"error": str(e)}


async def _handler_get_notebook(**kwargs: Any) -> dict[str, Any]:
    notebook_id = kwargs.get("notebook_id", "")
    if not notebook_id:
        return {"error": "notebook_id is required"}
    try:
        client = _get_client()
        result = client.get_notebook(notebook_id)
        if result is None:
            return {"error": f"Notebook {notebook_id} not found"}
        return result if isinstance(result, dict) else {"data": result}
    except Exception as e:
        return {"error": str(e)}


async def _handler_delete_notebook(**kwargs: Any) -> dict[str, Any]:
    notebook_id = kwargs.get("notebook_id", "")
    if not notebook_id:
        return {"error": "notebook_id is required"}
    try:
        client = _get_client()
        ok = client.delete_notebook(notebook_id)
        return {"deleted": ok, "notebook_id": notebook_id}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Source handlers
# ---------------------------------------------------------------------------


async def _handler_add_source(**kwargs: Any) -> dict[str, Any]:
    notebook_id = kwargs.get("notebook_id", "")
    if not notebook_id:
        return {"error": "notebook_id is required"}
    url = kwargs.get("url", "")
    text = kwargs.get("text", "")
    title = kwargs.get("title", "Pasted Text")
    try:
        client = _get_client()
        if url:
            result = client.add_url_source(notebook_id, url)
        elif text:
            result = client.add_text_source(notebook_id, text, title=title)
        else:
            return {"error": "Either url or text is required"}
        if result is None:
            return {"error": "Failed to add source"}
        return result if isinstance(result, dict) else {"data": result}
    except Exception as e:
        return {"error": str(e)}


async def _handler_list_sources(**kwargs: Any) -> dict[str, Any]:
    notebook_id = kwargs.get("notebook_id", "")
    if not notebook_id:
        return {"error": "notebook_id is required"}
    try:
        client = _get_client()
        sources = client.get_notebook_sources_with_types(notebook_id)
        return {"sources": sources, "count": len(sources)}
    except Exception as e:
        return {"error": str(e)}


async def _handler_get_source(**kwargs: Any) -> dict[str, Any]:
    source_id = kwargs.get("source_id", "")
    if not source_id:
        return {"error": "source_id is required"}
    try:
        client = _get_client()
        result = client.get_source_fulltext(source_id)
        return result if isinstance(result, dict) else {"data": result}
    except Exception as e:
        return {"error": str(e)}


async def _handler_delete_source(**kwargs: Any) -> dict[str, Any]:
    source_id = kwargs.get("source_id", "")
    if not source_id:
        return {"error": "source_id is required"}
    try:
        client = _get_client()
        ok = client.delete_source(source_id)
        return {"deleted": ok, "source_id": source_id}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Note handlers (maps to query/conversation API)
# ---------------------------------------------------------------------------


async def _handler_create_note(**kwargs: Any) -> dict[str, Any]:
    notebook_id = kwargs.get("notebook_id", "")
    query_text = kwargs.get("query", "") or kwargs.get("text", "")
    if not notebook_id:
        return {"error": "notebook_id is required"}
    if not query_text:
        return {"error": "query (or text) is required"}
    source_ids = kwargs.get("source_ids")
    try:
        client = _get_client()
        result = client.query(
            notebook_id,
            query_text,
            source_ids=source_ids,
        )
        if result is None:
            return {"error": "Failed to create note"}
        return result if isinstance(result, dict) else {"data": result}
    except Exception as e:
        return {"error": str(e)}


async def _handler_list_notes(**kwargs: Any) -> dict[str, Any]:
    notebook_id = kwargs.get("notebook_id", "")
    if not notebook_id:
        return {"error": "notebook_id is required"}
    try:
        client = _get_client()
        summary = client.get_notebook_summary(notebook_id)
        return summary if isinstance(summary, dict) else {"data": summary}
    except Exception as e:
        return {"error": str(e)}


async def _handler_get_note(**kwargs: Any) -> dict[str, Any]:
    conversation_id = kwargs.get("conversation_id", "")
    if not conversation_id:
        return {"error": "conversation_id is required"}
    try:
        client = _get_client()
        history = client.get_conversation_history(conversation_id)
        if history is None:
            return {"error": f"Conversation {conversation_id} not found"}
        return {"messages": history, "count": len(history)}
    except Exception as e:
        return {"error": str(e)}


async def _handler_delete_note(**kwargs: Any) -> dict[str, Any]:
    conversation_id = kwargs.get("conversation_id", "")
    if not conversation_id:
        return {"error": "conversation_id is required"}
    try:
        client = _get_client()
        ok = client.delete_conversation(conversation_id)
        return {"deleted": ok, "conversation_id": conversation_id}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Audio overview handlers
# ---------------------------------------------------------------------------


async def _handler_generate_audio_overview(**kwargs: Any) -> dict[str, Any]:
    notebook_id = kwargs.get("notebook_id", "")
    if not notebook_id:
        return {"error": "notebook_id is required"}
    try:
        client = _get_client()
        result = client.generate_audio_overview(notebook_id)
        return result if isinstance(result, dict) else {"data": result}
    except Exception as e:
        return {"error": str(e)}


async def _handler_get_audio_overview(**kwargs: Any) -> dict[str, Any]:
    notebook_id = kwargs.get("notebook_id", "")
    if not notebook_id:
        return {"error": "notebook_id is required"}
    try:
        client = _get_client()
        result = client.get_audio_overview(notebook_id)
        return result if isinstance(result, dict) else {"data": result}
    except Exception as e:
        return {"error": str(e)}
