"""Microsoft Graph provider — direct Graph API via OAuth (MSAL)."""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from obscura.integrations.msgraph import GraphOAuth, DEFAULT_SCOPES

logger = logging.getLogger(__name__)


async def MSGraphProvider(**kwargs: Any) -> dict[str, Any]:
    """
    Tools:
      - msgraph.mail.send(to: list[str]|str, subject: str, body_html|body_text)
      - msgraph.mail.list(folder='inbox', top=10)
      - msgraph.calendar.events.list(start=None, end=None, timezone='UTC', top=50)
      - msgraph.calendar.events.create(subject, start, end, attendees=[], body_html='', timezone='UTC')
    """
    tool_name = kwargs.get("_tool_name", "")

    oauth = GraphOAuth()
    token = oauth.acquire_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            if tool_name == "msgraph.mail.send":
                to = kwargs.get("to") or kwargs.get("recipients")
                if isinstance(to, str):
                    to = [to]
                subject = kwargs.get("subject", "")
                body_html = kwargs.get("body_html")
                body_text = kwargs.get("body_text")
                if not body_html and not body_text:
                    return {"error": "Provide body_html or body_text"}
                body = {
                    "contentType": "HTML" if body_html else "Text",
                    "content": body_html or body_text,
                }
                payload = {
                    "message": {
                        "subject": subject,
                        "body": body,
                        "toRecipients": [{"emailAddress": {"address": addr}} for addr in (to or [])],
                    },
                    "saveToSentItems": True,
                }
                resp = await client.post(
                    "https://graph.microsoft.com/v1.0/me/sendMail",
                    headers=headers,
                    content=json.dumps(payload),
                )
                if resp.status_code >= 300:
                    return {"error": f"Graph error {resp.status_code}", "details": resp.text}
                return {"ok": True}

            if tool_name == "msgraph.mail.list":
                folder = kwargs.get("folder", "inbox")
                top = int(kwargs.get("top", 10))
                url = f"https://graph.microsoft.com/v1.0/me/mailFolders/{folder}/messages?$top={top}"
                resp = await client.get(url, headers=headers)
                if resp.status_code >= 300:
                    return {"error": f"Graph error {resp.status_code}", "details": resp.text}
                data = resp.json()
                return {"messages": data.get("value", [])}

            if tool_name == "msgraph.calendar.events.list":
                params = []
                top = int(kwargs.get("top", 50))
                params.append(("$top", str(top)))
                if kwargs.get("start") and kwargs.get("end"):
                    start = kwargs["start"]
                    end = kwargs["end"]
                    tz = kwargs.get("timezone", "UTC")
                    params.extend([
                        ("startDateTime", start),
                        ("endDateTime", end),
                        ("Prefer", f"outlook.timezone=\"{tz}\""),
                    ])
                    url = "https://graph.microsoft.com/v1.0/me/calendarView"
                else:
                    url = "https://graph.microsoft.com/v1.0/me/events"
                resp = await client.get(url, headers=headers, params=params)
                if resp.status_code >= 300:
                    return {"error": f"Graph error {resp.status_code}", "details": resp.text}
                return resp.json()

            if tool_name == "msgraph.calendar.events.create":
                subject = kwargs.get("subject", "")
                start = kwargs.get("start")
                end = kwargs.get("end")
                tz = kwargs.get("timezone", "UTC")
                attendees = kwargs.get("attendees", [])
                body_html = kwargs.get("body_html", "")
                if not (start and end):
                    return {"error": "start and end are required (ISO 8601)"}
                payload = {
                    "subject": subject,
                    "body": {"contentType": "HTML", "content": body_html},
                    "start": {"dateTime": start, "timeZone": tz},
                    "end": {"dateTime": end, "timeZone": tz},
                    "attendees": [
                        {
                            "emailAddress": {"address": a if isinstance(a, str) else a.get("address")},
                            "type": "required",
                        }
                        for a in attendees
                    ],
                }
                resp = await client.post(
                    "https://graph.microsoft.com/v1.0/me/events",
                    headers=headers,
                    content=json.dumps(payload),
                )
                if resp.status_code >= 300:
                    return {"error": f"Graph error {resp.status_code}", "details": resp.text}
                return resp.json()

            # Fallback: allow raw request
            if tool_name == "msgraph.request":
                method = (kwargs.get("method") or "GET").upper()
                url = kwargs.get("url") or kwargs.get("endpoint")
                if not url:
                    return {"error": "url is required"}
                body = kwargs.get("body")
                resp = await client.request(method, url, headers=headers, json=body)
                try:
                    return resp.json()
                except Exception:
                    return {"status": resp.status_code, "body": resp.text}

            return {"error": f"Unknown tool: {tool_name}"}
        except Exception as e:
            return {"error": str(e)}
