from __future__ import annotations

import os
from pathlib import Path
from typing import Sequence

import msal

DEFAULT_SCOPES = (
    'https://graph.microsoft.com/Mail.Read',
    'https://graph.microsoft.com/Mail.Send',
    'https://graph.microsoft.com/Calendars.ReadWrite',
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
        self.client_id = client_id or os.environ.get('OBSCURA_MSGRAPH_CLIENT_ID', '')
        self.tenant_id = tenant_id or os.environ.get('OBSCURA_MSGRAPH_TENANT_ID', 'common')
        self.scopes = tuple(scopes or DEFAULT_SCOPES)
        home = Path(os.environ.get('OBSCURA_HOME', Path.home() / '.obscura'))
        self.cache_path = Path(cache_path or (home / 'msgraph_token.json'))
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache = msal.SerializableTokenCache()
        if self.cache_path.is_file():
            try:
                self._cache.deserialize(self.cache_path.read_text())
            except Exception:
                pass
        authority = 'https://login.microsoftonline.com/' + self.tenant_id
        self._app = msal.PublicClientApplication(
            self.client_id,
            authority=authority,
            token_cache=self._cache,
        )

    def _save_cache(self) -> None:
        try:
            self.cache_path.write_text(self._cache.serialize())
        except Exception:
            pass

    def acquire_token(self) -> str:
        if not self.client_id:
            raise ValueError('OBSCURA_MSGRAPH_CLIENT_ID is required')
        # Try silent first
        result = self._app.acquire_token_silent(list(self.scopes), account=None)
        if not result:
            flow = self._app.initiate_device_flow(scopes=list(self.scopes))
            if 'user_code' not in flow:
                raise RuntimeError('Failed to create device flow')
            # Device flow: print instructions to console; provider will capture
            print('msgraph_device_flow_verification_uri:', flow.get('verification_uri'))
            print('msgraph_device_flow_user_code:', flow.get('user_code'))
            result = self._app.acquire_token_by_device_flow(flow)
        if 'access_token' not in result:
            err = str(result.get('error'))
            desc = str(result.get('error_description'))
            raise RuntimeError('MSAL error: ' + err + ': ' + desc)
        self._save_cache()
        return result['access_token']
