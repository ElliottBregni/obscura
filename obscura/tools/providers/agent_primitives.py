"""High-leverage internet/infra/data primitive handlers for plugin manifests."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from typing import Any

import httpx


async def _http_json(
    *,
    method: str,
    url: str,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    json_body: dict[str, Any] | list[Any] | None = None,
    data: dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(
                method=method.upper(),
                url=url,
                params=params,
                headers=headers,
                json=json_body,
                data=data,
            )
            content_type = resp.headers.get("content-type", "")
            payload: Any
            if "application/json" in content_type:
                payload = resp.json()
            else:
                payload = {"text": resp.text}
            return {
                "ok": resp.is_success,
                "status_code": resp.status_code,
                "url": str(resp.url),
                "data": payload,
            }
    except Exception as exc:
        return {"ok": False, "error": str(exc), "url": url}


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"Missing required env var: {name}")
    return value


def _run_cli(command: list[str]) -> dict[str, Any]:
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=60)
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "command": " ".join(shlex.quote(c) for c in command),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc), "command": command}


async def securitytrails_domain(domain: str, **_: Any) -> dict[str, Any]:
    key = _env("SECURITYTRAILS_API_KEY")
    return await _http_json(
        method="GET",
        url=f"https://api.securitytrails.com/v1/domain/{domain}",
        headers={"APIKEY": key},
    )


async def censys_search(query: str, per_page: int = 10, **_: Any) -> dict[str, Any]:
    import base64

    api_id = _env("CENSYS_API_ID")
    api_secret = _env("CENSYS_API_SECRET")
    auth = base64.b64encode(f"{api_id}:{api_secret}".encode("utf-8")).decode("utf-8")
    return await _http_json(
        method="POST",
        url="https://search.censys.io/api/v2/hosts/search",
        headers={
            "Accept": "application/json",
            "Authorization": f"Basic {auth}",
        },
        json_body={"q": query, "per_page": per_page},
        timeout=45.0,
    )


async def shodan_search(query: str, page: int = 1, **_: Any) -> dict[str, Any]:
    key = _env("SHODAN_API_KEY")
    return await _http_json(
        method="GET",
        url="https://api.shodan.io/shodan/host/search",
        params={"key": key, "query": query, "page": page},
    )


async def polygon_snapshot(symbol: str, **_: Any) -> dict[str, Any]:
    key = _env("POLYGON_API_KEY")
    return await _http_json(
        method="GET",
        url=f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{symbol}",
        params={"apiKey": key},
    )


async def sec_edgar_submissions(cik: str, **_: Any) -> dict[str, Any]:
    ua = os.environ.get("SEC_EDGAR_USER_AGENT", "obscura/1.0 (ops@local)")
    cik10 = cik.zfill(10)
    return await _http_json(
        method="GET",
        url=f"https://data.sec.gov/submissions/CIK{cik10}.json",
        headers={"User-Agent": ua, "Accept-Encoding": "gzip, deflate"},
    )


async def sentinelhub_process(payload: dict[str, Any], **_: Any) -> dict[str, Any]:
    token = _env("SENTINELHUB_ACCESS_TOKEN")
    return await _http_json(
        method="POST",
        url="https://services.sentinel-hub.com/api/v1/process",
        headers={"Authorization": f"Bearer {token}"},
        json_body=payload,
        timeout=60.0,
    )


async def marinetraffic_call(path: str, params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
    key = _env("MARINETRAFFIC_API_KEY")
    merged = dict(params or {})
    merged.setdefault("api_key", key)
    return await _http_json(
        method="GET",
        url=f"https://services.marinetraffic.com/api/{path.lstrip('/')}",
        params=merged,
    )


async def flightaware_call(path: str, params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
    key = _env("FLIGHTAWARE_API_KEY")
    return await _http_json(
        method="GET",
        url=f"https://aeroapi.flightaware.com/aeroapi/{path.lstrip('/')}",
        params=params or {},
        headers={"x-apikey": key},
    )


async def browserless_content(url: str, wait_until: str = "networkidle", **_: Any) -> dict[str, Any]:
    token = _env("BROWSERLESS_TOKEN")
    endpoint = os.environ.get("BROWSERLESS_URL", "https://chrome.browserless.io")
    return await _http_json(
        method="POST",
        url=f"{endpoint.rstrip('/')}/content",
        params={"token": token},
        json_body={"url": url, "waitUntil": wait_until},
        timeout=60.0,
    )


def playwright_version(**_: Any) -> dict[str, Any]:
    return _run_cli(["npx", "playwright", "--version"])


def rg_search(pattern: str, path: str = ".", **_: Any) -> dict[str, Any]:
    return _run_cli(["rg", "-n", pattern, path])


def jq_eval(filter_expr: str, input_json: str, **_: Any) -> dict[str, Any]:
    return _run_cli(["jq", filter_expr, input_json])


def fzf_filter(query: str, input_text: str, **_: Any) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            ["fzf", "--filter", query],
            input=input_text,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def fd_find(pattern: str, path: str = ".", **_: Any) -> dict[str, Any]:
    return _run_cli(["fd", pattern, path])


def duckdb_query(query: str, database: str = ":memory:", **_: Any) -> dict[str, Any]:
    try:
        import duckdb  # type: ignore

        conn = duckdb.connect(database=database)
        rows = conn.execute(query).fetchall()
        cols = [d[0] for d in (conn.description or [])]
        conn.close()
        return {"ok": True, "columns": cols, "rows": rows}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def datafusion_query(query: str, **_: Any) -> dict[str, Any]:
    try:
        from datafusion import SessionContext  # type: ignore

        ctx = SessionContext()
        df = ctx.sql(query)
        batches = df.collect()
        return {
            "ok": True,
            "rows": [str(b) for b in batches],
            "batch_count": len(batches),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def wikidata_sparql(query: str, **_: Any) -> dict[str, Any]:
    return await _http_json(
        method="GET",
        url="https://query.wikidata.org/sparql",
        params={"query": query, "format": "json"},
        headers={"Accept": "application/sparql-results+json"},
        timeout=45.0,
    )


async def openalex_search(search: str, per_page: int = 25, **_: Any) -> dict[str, Any]:
    return await _http_json(
        method="GET",
        url="https://api.openalex.org/works",
        params={"search": search, "per-page": per_page},
    )


async def github_graphql(query: str, variables: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
    token = _env("GITHUB_TOKEN")
    return await _http_json(
        method="POST",
        url="https://api.github.com/graphql",
        headers={"Authorization": f"Bearer {token}"},
        json_body={"query": query, "variables": variables or {}},
        timeout=45.0,
    )


def docker_ps(**_: Any) -> dict[str, Any]:
    return _run_cli(["docker", "ps", "--format", "{{json .}}"])


def kubectl_get(resource: str, namespace: str = "default", **_: Any) -> dict[str, Any]:
    return _run_cli(["kubectl", "get", resource, "-n", namespace, "-o", "json"])


async def prometheus_query(query: str, **_: Any) -> dict[str, Any]:
    base = os.environ.get("PROMETHEUS_URL", "http://127.0.0.1:9090")
    return await _http_json(
        method="GET",
        url=f"{base.rstrip('/')}/api/v1/query",
        params={"query": query},
    )


async def grafana_api(path: str = "/api/health", method: str = "GET", body: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
    base = os.environ.get("GRAFANA_URL", "http://127.0.0.1:3000")
    token = os.environ.get("GRAFANA_TOKEN", "").strip()
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return await _http_json(
        method=method,
        url=f"{base.rstrip('/')}/{path.lstrip('/')}",
        headers=headers,
        json_body=body,
    )


async def matrix_send(room_id: str, message: str, **_: Any) -> dict[str, Any]:
    base = _env("MATRIX_HOMESERVER_URL")
    token = _env("MATRIX_ACCESS_TOKEN")
    txn = "obscura-txn-1"
    return await _http_json(
        method="PUT",
        url=f"{base.rstrip('/')}/_matrix/client/v3/rooms/{room_id}/send/m.room.message/{txn}",
        headers={"Authorization": f"Bearer {token}"},
        json_body={"msgtype": "m.text", "body": message},
    )


def nats_publish(subject: str, message: str, server: str = "nats://127.0.0.1:4222", **_: Any) -> dict[str, Any]:
    return _run_cli(["nats", "--server", server, "pub", subject, message])


async def overpass_query(query: str, **_: Any) -> dict[str, Any]:
    return await _http_json(
        method="POST",
        url="https://overpass-api.de/api/interpreter",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={"data": query},
        timeout=60.0,
    )


async def healthcheck(**_: Any) -> dict[str, Any]:
    return {"ok": True, "provider": "agent_primitives"}


# ---------------------------------------------------------------------------
# Datadog
# ---------------------------------------------------------------------------

def _dd_headers() -> dict[str, str]:
    """Return Datadog auth headers using DATADOG_API_KEY and DATADOG_APP_KEY env vars."""
    return {
        "DD-API-KEY": _env("DATADOG_API_KEY"),
        "DD-APPLICATION-KEY": _env("DATADOG_APP_KEY"),
        "Content-Type": "application/json",
    }


def _dd_base() -> str:
    site = os.environ.get("DATADOG_SITE", "datadoghq.com").strip() or "datadoghq.com"
    return f"https://api.{site}"


async def datadog_list_logs(
    query: str,
    from_ts: str | None = None,
    to_ts: str | None = None,
    limit: int = 50,
    **_: Any,
) -> dict[str, Any]:
    import datetime

    now = datetime.datetime.utcnow()
    default_from = (now - datetime.timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ")
    default_to = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    body: dict[str, Any] = {
        "filter": {
            "query": query,
            "from": from_ts or default_from,
            "to": to_ts or default_to,
        },
        "page": {"limit": min(limit, 1000)},
    }
    return await _http_json(
        method="POST",
        url=f"{_dd_base()}/api/v2/logs/events/search",
        headers=_dd_headers(),
        json_body=body,
    )


async def datadog_query_metrics(
    query: str,
    from_ts: int | None = None,
    to_ts: int | None = None,
    **_: Any,
) -> dict[str, Any]:
    import time

    now = int(time.time())
    params: dict[str, Any] = {
        "query": query,
        "from": from_ts if from_ts is not None else now - 3600,
        "to": to_ts if to_ts is not None else now,
    }
    return await _http_json(
        method="GET",
        url=f"{_dd_base()}/api/v1/query",
        headers=_dd_headers(),
        params=params,
    )


async def datadog_list_traces(
    query: str,
    from_ts: str | None = None,
    to_ts: str | None = None,
    limit: int = 50,
    **_: Any,
) -> dict[str, Any]:
    import datetime

    now = datetime.datetime.utcnow()
    default_from = (now - datetime.timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ")
    default_to = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    body: dict[str, Any] = {
        "filter": {
            "query": query,
            "from": from_ts or default_from,
            "to": to_ts or default_to,
        },
        "page": {"limit": min(limit, 1000)},
    }
    return await _http_json(
        method="POST",
        url=f"{_dd_base()}/api/v2/spans/events/search",
        headers=_dd_headers(),
        json_body=body,
    )


async def datadog_get_monitors(query: str | None = None, **_: Any) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if query:
        params["query"] = query
    return await _http_json(
        method="GET",
        url=f"{_dd_base()}/api/v1/monitor",
        headers=_dd_headers(),
        params=params or None,
    )


async def datadog_list_dashboards(query: str | None = None, **_: Any) -> dict[str, Any]:
    result = await _http_json(
        method="GET",
        url=f"{_dd_base()}/api/v1/dashboard",
        headers=_dd_headers(),
    )
    # client-side filter by title substring if query given
    if query and result.get("ok") and isinstance(result.get("data"), dict):
        dashboards = result["data"].get("dashboards", [])
        result["data"]["dashboards"] = [
            d for d in dashboards if query.lower() in d.get("title", "").lower()
        ]
    return result


async def datadog_list_incidents(**_: Any) -> dict[str, Any]:
    return await _http_json(
        method="GET",
        url=f"{_dd_base()}/api/v2/incidents",
        headers=_dd_headers(),
        params={"include": "attachments"},
    )


async def datadog_get_incident(incident_id: str, **_: Any) -> dict[str, Any]:
    return await _http_json(
        method="GET",
        url=f"{_dd_base()}/api/v2/incidents/{incident_id}",
        headers=_dd_headers(),
    )


async def datadog_list_hosts(filter: str | None = None, **_: Any) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if filter:
        params["filter"] = filter
    return await _http_json(
        method="GET",
        url=f"{_dd_base()}/api/v1/hosts",
        headers=_dd_headers(),
        params=params or None,
    )


async def datadog_mute_host(
    hostname: str,
    end: int | None = None,
    message: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    body: dict[str, Any] = {}
    if end is not None:
        body["end"] = end
    if message:
        body["message"] = message
    return await _http_json(
        method="POST",
        url=f"{_dd_base()}/api/v1/host/{hostname}/mute",
        headers=_dd_headers(),
        json_body=body,
    )


async def datadog_unmute_host(hostname: str, **_: Any) -> dict[str, Any]:
    return await _http_json(
        method="POST",
        url=f"{_dd_base()}/api/v1/host/{hostname}/unmute",
        headers=_dd_headers(),
        json_body={},
    )


async def datadog_schedule_downtime(
    scope: str,
    start: int | None = None,
    end: int | None = None,
    message: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    import time

    body: dict[str, Any] = {
        "scope": scope,
        "start": start if start is not None else int(time.time()),
    }
    if end is not None:
        body["end"] = end
    if message:
        body["message"] = message
    return await _http_json(
        method="POST",
        url=f"{_dd_base()}/api/v1/downtime",
        headers=_dd_headers(),
        json_body=body,
    )


async def datadog_healthcheck(**_: Any) -> dict[str, Any]:
    """Validate Datadog credentials by hitting the validate endpoint."""
    return await _http_json(
        method="GET",
        url=f"{_dd_base()}/api/v1/validate",
        headers=_dd_headers(),
    )
