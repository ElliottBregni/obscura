#!/usr/bin/env python3
"""
infra/zitadel/setup.py -- Bootstrap Zitadel for the Obscura platform.

Creates the "Obscura" project, roles, a default admin service user with
a machine key, and prints the JWKS URI / audience for configuration.

Usage::

    # After `docker compose up -d` and Zitadel is healthy:
    python infra/zitadel/setup.py

    # Get a test JWT for curl-based testing:
    python infra/zitadel/setup.py --get-token

    # In-cluster bootstrap (reads token from mounted secret):
    python infra/zitadel/setup.py --k8s
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

import httpx


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_ZITADEL_URL = "http://localhost:8080"
DEFAULT_PROJECT_NAME = "Obscura"
DEFAULT_ROLES = [
    "admin",
    "agent:copilot",
    "agent:claude",
    "agent:read",
    "sync:write",
    "sessions:manage",
]
DEFAULT_SERVICE_USER = "obscura-admin"

# Zitadel's first-instance admin credentials (from docker-compose.yml)
DEFAULT_ADMIN_USER = "admin@zitadel.localhost"
DEFAULT_ADMIN_PASS = "Password1!"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _wait_for_zitadel(base_url: str, timeout: int = 120) -> None:
    """Block until Zitadel's health endpoint returns 200."""
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient(timeout=5) as client:
        while time.monotonic() < deadline:
            try:
                resp = await client.get(f"{base_url}/debug/ready")
                if resp.status_code == 200:
                    print(f"Zitadel is ready at {base_url}")
                    return
            except httpx.ConnectError:
                pass
            await asyncio.sleep(2)
    raise TimeoutError(f"Zitadel not ready after {timeout}s")


async def _get_admin_token(base_url: str, username: str, password: str) -> str:
    """Authenticate as the first-instance human admin and return a bearer token.

    Uses Zitadel's session + OIDC auth flow to obtain a PAT-like token.
    Falls back to using the username as a PAT if it looks like one already.
    """
    # If the caller passes an existing token, use it directly
    if username.startswith("ey") or len(username) > 100:
        return username

    # Use the auth API to get an initial admin token via password grant
    async with httpx.AsyncClient(base_url=base_url, timeout=30) as client:
        # First, try using the Zitadel service user API to get a token
        # via client credentials if available
        # Otherwise, use the OIDC password grant (Zitadel first-instance flow)
        resp = await client.post(
            "/oauth/v2/token",
            data={
                "grant_type": "password",
                "username": username,
                "password": password,
                "scope": "openid urn:zitadel:iam:org:project:id:zitadel:aud",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        if resp.status_code == 200:
            data = resp.json()
            return data["access_token"]

        # Fall back: get PAT from the admin login API (Zitadel v2)
        resp = await client.post(
            "/v2/sessions",
            json={
                "checks": {
                    "user": {"loginName": username},
                    "password": {"password": password},
                },
            },
        )
        if resp.status_code in (200, 201):
            data = resp.json()
            session_token = data.get("sessionToken", "")
            if session_token:
                return session_token

        raise RuntimeError(
            f"Could not authenticate as {username}: "
            f"status={resp.status_code} body={resp.text}"
        )


async def _bootstrap(
    base_url: str,
    admin_token: str,
    project_name: str,
    roles: list[str],
    service_user_name: str,
) -> dict:
    """Run the full bootstrap using the sdk.auth.zitadel module."""
    # Import here so this script can also run standalone without the SDK installed
    try:
        from sdk.auth.zitadel import bootstrap
        return await bootstrap(
            base_url,
            admin_token,
            project_name=project_name,
            roles=roles,
            service_user_name=service_user_name,
        )
    except ImportError:
        # Inline bootstrap for environments without the SDK
        return await _inline_bootstrap(
            base_url, admin_token, project_name, roles, service_user_name,
        )


async def _inline_bootstrap(
    base_url: str,
    admin_token: str,
    project_name: str,
    roles: list[str],
    service_user_name: str,
) -> dict:
    """Standalone bootstrap that does not depend on the SDK package."""
    headers = {
        "Authorization": f"Bearer {admin_token}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=30) as c:
        # 1. Find or create project
        resp = await c.post("/management/v1/projects/_search", json={"query": {}})
        resp.raise_for_status()
        projects = resp.json().get("result", [])
        project_id = None
        for p in projects:
            if p.get("name") == project_name:
                project_id = p["id"]
                break

        if not project_id:
            resp = await c.post("/management/v1/projects", json={"name": project_name})
            resp.raise_for_status()
            project_id = resp.json()["id"]
            print(f"Created project '{project_name}' -> {project_id}")
        else:
            print(f"Project '{project_name}' already exists -> {project_id}")

        # 2. Create roles
        for role in roles:
            try:
                await c.post(
                    f"/management/v1/projects/{project_id}/roles",
                    json={"roleKey": role, "displayName": role},
                )
                print(f"  Added role: {role}")
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 409:
                    print(f"  Role exists: {role}")
                else:
                    raise

        # 3. Create machine user
        user_id = ""
        try:
            resp = await c.post(
                "/v2/users/machine",
                json={
                    "userName": service_user_name,
                    "name": "Obscura Admin Service Account",
                    "description": f"Bootstrap service user: {service_user_name}",
                },
            )
            resp.raise_for_status()
            user_id = resp.json().get("userId", "")
            print(f"Created service user '{service_user_name}' -> {user_id}")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 409:
                print(f"Service user '{service_user_name}' already exists")
            else:
                raise

        # 4. Create machine key + grant
        machine_key = {}
        if user_id:
            resp = await c.post(
                f"/management/v1/users/{user_id}/grants",
                json={"projectId": project_id, "roleKeys": ["admin"]},
            )
            resp.raise_for_status()

            resp = await c.post(
                f"/management/v1/users/{user_id}/keys",
                json={"type": "KEY_TYPE_JSON"},
            )
            resp.raise_for_status()
            machine_key = resp.json()
            print(f"Generated machine key (id={machine_key.get('keyId', '?')})")

        # 5. OIDC metadata
        resp = await c.get("/.well-known/openid-configuration")
        resp.raise_for_status()
        oidc = resp.json()

        return {
            "project_id": project_id,
            "user_id": user_id,
            "machine_key": machine_key,
            "jwks_uri": oidc.get("jwks_uri", f"{base_url}/.well-known/jwks.json"),
            "audience": project_id,
            "issuer": oidc.get("issuer", base_url),
        }


async def _get_test_token(base_url: str, machine_key: dict) -> str:
    """Exchange a machine key for a JWT (for curl testing)."""
    # Decode the base64-encoded key details if present
    key_details = machine_key.get("keyDetails", "")
    if key_details:
        import base64
        decoded = base64.b64decode(key_details).decode()
        key_data = json.loads(decoded)
    else:
        key_data = machine_key

    # Build a JWT assertion signed with the machine key
    from jose import jwt as jose_jwt
    import time as _time

    now = int(_time.time())
    claims = {
        "iss": key_data.get("userId", ""),
        "sub": key_data.get("userId", ""),
        "aud": base_url,
        "iat": now,
        "exp": now + 3600,
    }

    private_key = key_data.get("key", "")
    assertion = jose_jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": key_data.get("keyId", "")})

    # Exchange for an access token
    async with httpx.AsyncClient(base_url=base_url, timeout=30) as c:
        resp = await c.post(
            "/oauth/v2/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "scope": "openid urn:zitadel:iam:org:project:id:zitadel:aud",
                "assertion": assertion,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        return resp.json()["access_token"]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Bootstrap Zitadel for the Obscura platform.",
    )
    p.add_argument(
        "--url",
        default=os.environ.get("ZITADEL_URL", DEFAULT_ZITADEL_URL),
        help=f"Zitadel base URL (default: {DEFAULT_ZITADEL_URL})",
    )
    p.add_argument(
        "--admin-user",
        default=os.environ.get("ZITADEL_ADMIN_USER", DEFAULT_ADMIN_USER),
        help="Admin username for initial auth",
    )
    p.add_argument(
        "--admin-pass",
        default=os.environ.get("ZITADEL_ADMIN_PASS", DEFAULT_ADMIN_PASS),
        help="Admin password for initial auth",
    )
    p.add_argument(
        "--admin-token",
        default=os.environ.get("ZITADEL_ADMIN_TOKEN", ""),
        help="Use an existing admin token instead of username/password",
    )
    p.add_argument(
        "--project",
        default=DEFAULT_PROJECT_NAME,
        help=f"Project name (default: {DEFAULT_PROJECT_NAME})",
    )
    p.add_argument(
        "--service-user",
        default=DEFAULT_SERVICE_USER,
        help=f"Service user name (default: {DEFAULT_SERVICE_USER})",
    )
    p.add_argument(
        "--get-token",
        action="store_true",
        help="After bootstrap, exchange the machine key for a JWT and print it",
    )
    p.add_argument(
        "--k8s",
        action="store_true",
        help="Read admin token from /var/run/secrets/zitadel/admin-token",
    )
    p.add_argument(
        "--wait",
        action="store_true",
        default=True,
        help="Wait for Zitadel to be ready before bootstrapping (default: true)",
    )
    p.add_argument(
        "--no-wait",
        dest="wait",
        action="store_false",
        help="Skip waiting for Zitadel readiness",
    )
    p.add_argument(
        "--output",
        default=None,
        help="Write bootstrap result to a JSON file",
    )
    return p


async def async_main(args: argparse.Namespace) -> int:
    base_url = args.url

    # Wait for Zitadel
    if args.wait:
        try:
            await _wait_for_zitadel(base_url)
        except TimeoutError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1

    # Resolve admin token
    if args.k8s:
        token_path = "/var/run/secrets/zitadel/admin-token"
        if not os.path.exists(token_path):
            print(f"Error: {token_path} not found", file=sys.stderr)
            return 1
        admin_token = open(token_path).read().strip()
    elif args.admin_token:
        admin_token = args.admin_token
    else:
        print(f"Authenticating as {args.admin_user}...")
        try:
            admin_token = await _get_admin_token(
                base_url, args.admin_user, args.admin_pass,
            )
        except Exception as e:
            print(f"Error getting admin token: {e}", file=sys.stderr)
            return 1

    # Bootstrap
    print(f"\nBootstrapping project '{args.project}'...")
    try:
        result = await _bootstrap(
            base_url,
            admin_token,
            args.project,
            DEFAULT_ROLES,
            args.service_user,
        )
    except Exception as e:
        print(f"Bootstrap failed: {e}", file=sys.stderr)
        return 1

    # Output
    print("\n--- Bootstrap Result ---")
    print(f"  Project ID:  {result['project_id']}")
    print(f"  User ID:     {result.get('user_id', 'N/A')}")
    print(f"  JWKS URI:    {result['jwks_uri']}")
    print(f"  Audience:    {result['audience']}")
    print(f"  Issuer:      {result['issuer']}")

    if result.get("machine_key"):
        print(f"  Machine Key: (key_id={result['machine_key'].get('keyId', '?')})")

    # Write to file
    if args.output:
        with open(args.output, "w") as f:
            json.dump(result, f, indent=2)
        print(f"\nResult written to {args.output}")

    # Get test token
    if args.get_token and result.get("machine_key"):
        print("\nExchanging machine key for JWT...")
        try:
            token = await _get_test_token(base_url, result["machine_key"])
            print(f"\nTest JWT:\n{token}")
        except Exception as e:
            print(f"Error getting test token: {e}", file=sys.stderr)

    # Print env vars for easy configuration
    print("\n--- Environment Variables ---")
    print(f"export OBSCURA_AUTH_ISSUER={result['issuer']}")
    print(f"export OBSCURA_AUTH_JWKS_URI={result['jwks_uri']}")
    print(f"export OBSCURA_AUTH_AUDIENCE={result['audience']}")

    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return asyncio.run(async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
