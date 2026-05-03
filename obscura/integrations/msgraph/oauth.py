from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

_msal: Any
try:
    import msal  # pyright: ignore[reportMissingImports]

    _msal = msal
except ImportError:
    _msal = None

if TYPE_CHECKING:
    from collections.abc import Sequence

DEFAULT_SCOPES = (
    "https://graph.microsoft.com/Mail.Read",
    "https://graph.microsoft.com/Mail.Send",
    "https://graph.microsoft.com/Calendars.ReadWrite",
)


class GraphOAuth:
    def __init__(
        self,
        *,
        client_id: str | None = None,
        tenant_id: str | None = None,
        scopes: Sequence[str] | None = None,
        cache_path: str | None = None,
    ) -> None:
        self.client_id = client_id or os.environ.get("OBSCURA_MSGRAPH_CLIENT_ID", "")
        self.tenant_id = tenant_id or os.environ.get(
            "OBSCURA_MSGRAPH_TENANT_ID",
            "common",
        )
        self.scopes = tuple(scopes or DEFAULT_SCOPES)
        home = Path(os.environ.get("OBSCURA_HOME", Path.home() / ".obscura"))
        self.cache_path = Path(cache_path or (home / "msgraph_token.json"))
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        if _msal is None:
            msg = "msal is required for GraphOAuth (pip install msal)"
            raise ImportError(msg)
        self._cache: Any = _msal.SerializableTokenCache()
        if self.cache_path.is_file():
            with contextlib.suppress(Exception):
                self._cache.deserialize(self.cache_path.read_text())
        authority = "https://login.microsoftonline.com/" + self.tenant_id
        self._app: Any = _msal.PublicClientApplication(
            self.client_id,
            authority=authority,
            token_cache=self._cache,
        )

    def _save_cache(self) -> None:
        with contextlib.suppress(Exception):
            self.cache_path.write_text(cast(str, self._cache.serialize()))

    def acquire_token(self) -> str:
        if not self.client_id:
            msg = "OBSCURA_MSGRAPH_CLIENT_ID is required"
            raise ValueError(msg)
        # Try silent first
        result = cast(
            "dict[str, Any] | None",
            self._app.acquire_token_silent(list(self.scopes), account=None),
        )
        if not result:
            flow = cast(
                "dict[str, Any]",
                self._app.initiate_device_flow(scopes=list(self.scopes)),
            )
            if "user_code" not in flow:
                msg = "Failed to create device flow"
                raise RuntimeError(msg)
            # Device flow: print instructions to console; provider will capture
            result = cast(
                "dict[str, Any]",
                self._app.acquire_token_by_device_flow(flow),
            )
        if "access_token" not in result:
            err = str(result.get("error"))
            desc = str(result.get("error_description"))
            raise RuntimeError("MSAL error: " + err + ": " + desc)
        self._save_cache()
        return cast(str, result["access_token"])
