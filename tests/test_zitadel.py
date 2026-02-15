"""Tests for sdk.auth.zitadel — ZitadelClient and bootstrap."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import httpx

from sdk.auth.zitadel import ZitadelClient, bootstrap, DEFAULT_ROLES


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_response(json_data=None, status_code=200):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            message=f"HTTP {status_code}",
            request=MagicMock(),
            response=resp,
        )
    return resp


# ---------------------------------------------------------------------------
# ZitadelClient init and lifecycle
# ---------------------------------------------------------------------------

class TestZitadelClientInit:
    def test_init(self):
        client = ZitadelClient("https://z.example.com", "my-token")
        assert client._base_url == "https://z.example.com"
        assert client._token == "my-token"

    def test_init_strips_trailing_slash(self):
        client = ZitadelClient("https://z.example.com/", "tok")
        assert client._base_url == "https://z.example.com"


class TestZitadelClientContextManager:
    @pytest.mark.asyncio
    async def test_aenter_aexit(self):
        client = ZitadelClient("https://z.example.com", "tok")
        with patch.object(client, "close", new_callable=AsyncMock) as mock_close:
            async with client as c:
                assert c is client
            mock_close.assert_awaited_once()


# ---------------------------------------------------------------------------
# HTTP helpers (_post, _get, _put, _delete)
# ---------------------------------------------------------------------------

class TestZitadelClientHTTPHelpers:
    @pytest.mark.asyncio
    async def test_post(self):
        client = ZitadelClient("https://z.example.com", "tok")
        resp = _mock_response({"id": "123"})
        client._client = AsyncMock()
        client._client.post = AsyncMock(return_value=resp)

        result = await client._post("/projects", {"name": "Test"})
        assert result == {"id": "123"}
        client._client.post.assert_awaited_once_with(
            "/management/v1/projects", json={"name": "Test"}
        )

    @pytest.mark.asyncio
    async def test_post_no_body(self):
        client = ZitadelClient("https://z.example.com", "tok")
        resp = _mock_response({"ok": True})
        client._client = AsyncMock()
        client._client.post = AsyncMock(return_value=resp)

        result = await client._post("/action")
        assert result == {"ok": True}
        client._client.post.assert_awaited_once_with("/management/v1/action", json={})

    @pytest.mark.asyncio
    async def test_get(self):
        client = ZitadelClient("https://z.example.com", "tok")
        resp = _mock_response({"data": "val"})
        client._client = AsyncMock()
        client._client.get = AsyncMock(return_value=resp)

        result = await client._get("/orgs")
        assert result == {"data": "val"}

    @pytest.mark.asyncio
    async def test_put(self):
        client = ZitadelClient("https://z.example.com", "tok")
        resp = _mock_response({"updated": True})
        client._client = AsyncMock()
        client._client.put = AsyncMock(return_value=resp)

        result = await client._put("/orgs/1", {"name": "New"})
        assert result == {"updated": True}

    @pytest.mark.asyncio
    async def test_delete(self):
        client = ZitadelClient("https://z.example.com", "tok")
        resp = _mock_response({})
        client._client = AsyncMock()
        client._client.delete = AsyncMock(return_value=resp)

        result = await client._delete("/projects/1")
        assert result == {}


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

class TestZitadelClientProjects:
    @pytest.mark.asyncio
    async def test_create_project(self):
        client = ZitadelClient("https://z.example.com", "tok")
        with patch.object(client, "_post", new_callable=AsyncMock,
                          return_value={"id": "proj-1"}):
            pid = await client.create_project("Obscura")
        assert pid == "proj-1"

    @pytest.mark.asyncio
    async def test_list_projects(self):
        client = ZitadelClient("https://z.example.com", "tok")
        with patch.object(client, "_post", new_callable=AsyncMock,
                          return_value={"result": [{"id": "p1", "name": "Test"}]}):
            projects = await client.list_projects()
        assert len(projects) == 1
        assert projects[0]["name"] == "Test"

    @pytest.mark.asyncio
    async def test_list_projects_empty(self):
        client = ZitadelClient("https://z.example.com", "tok")
        with patch.object(client, "_post", new_callable=AsyncMock,
                          return_value={}):
            projects = await client.list_projects()
        assert projects == []

    @pytest.mark.asyncio
    async def test_find_project_by_name_found(self):
        client = ZitadelClient("https://z.example.com", "tok")
        with patch.object(client, "list_projects", new_callable=AsyncMock,
                          return_value=[{"name": "Obscura", "id": "p1"}]):
            pid = await client.find_project_by_name("Obscura")
        assert pid == "p1"

    @pytest.mark.asyncio
    async def test_find_project_by_name_not_found(self):
        client = ZitadelClient("https://z.example.com", "tok")
        with patch.object(client, "list_projects", new_callable=AsyncMock,
                          return_value=[{"name": "Other", "id": "p2"}]):
            pid = await client.find_project_by_name("Obscura")
        assert pid is None


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------

class TestZitadelClientRoles:
    @pytest.mark.asyncio
    async def test_add_project_role(self):
        client = ZitadelClient("https://z.example.com", "tok")
        with patch.object(client, "_post", new_callable=AsyncMock) as mock_post:
            await client.add_project_role("proj-1", "admin", "Administrator")
        mock_post.assert_awaited_once_with(
            "/projects/proj-1/roles",
            {"roleKey": "admin", "displayName": "Administrator"},
        )

    @pytest.mark.asyncio
    async def test_add_project_role_no_display_name(self):
        client = ZitadelClient("https://z.example.com", "tok")
        with patch.object(client, "_post", new_callable=AsyncMock) as mock_post:
            await client.add_project_role("proj-1", "agent:read")
        mock_post.assert_awaited_once_with(
            "/projects/proj-1/roles",
            {"roleKey": "agent:read", "displayName": "agent:read"},
        )

    @pytest.mark.asyncio
    async def test_bulk_add_roles(self):
        client = ZitadelClient("https://z.example.com", "tok")
        with patch.object(client, "add_project_role", new_callable=AsyncMock) as mock_add:
            await client.bulk_add_roles("proj-1", ["admin", "agent:read"])
        assert mock_add.await_count == 2

    @pytest.mark.asyncio
    async def test_bulk_add_roles_skips_409(self):
        client = ZitadelClient("https://z.example.com", "tok")

        call_count = 0

        async def fake_add(proj_id, role):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                resp = MagicMock()
                resp.status_code = 409
                raise httpx.HTTPStatusError("Conflict", request=MagicMock(), response=resp)

        with patch.object(client, "add_project_role", side_effect=fake_add):
            await client.bulk_add_roles("proj-1", ["admin", "agent:read"])
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_bulk_add_roles_raises_non_409(self):
        client = ZitadelClient("https://z.example.com", "tok")

        async def fail_add(proj_id, role):
            resp = MagicMock()
            resp.status_code = 500
            raise httpx.HTTPStatusError("Server Error", request=MagicMock(), response=resp)

        with patch.object(client, "add_project_role", side_effect=fail_add):
            with pytest.raises(httpx.HTTPStatusError):
                await client.bulk_add_roles("proj-1", ["admin"])


# ---------------------------------------------------------------------------
# API Applications
# ---------------------------------------------------------------------------

class TestZitadelClientAPIApps:
    @pytest.mark.asyncio
    async def test_create_api_app(self):
        client = ZitadelClient("https://z.example.com", "tok")
        with patch.object(client, "_post", new_callable=AsyncMock,
                          return_value={"clientId": "cid-1", "clientSecret": "sec"}):
            result = await client.create_api_app("proj-1", "my-app")
        assert result["clientId"] == "cid-1"


# ---------------------------------------------------------------------------
# Machine users
# ---------------------------------------------------------------------------

class TestZitadelClientMachineUsers:
    @pytest.mark.asyncio
    async def test_create_machine_user(self):
        client = ZitadelClient("https://z.example.com", "tok")
        resp = _mock_response({"userId": "u-1"})
        client._client = AsyncMock()
        client._client.post = AsyncMock(return_value=resp)

        uid = await client.create_machine_user("svc-user", "Service User")
        assert uid == "u-1"

    @pytest.mark.asyncio
    async def test_create_machine_key(self):
        client = ZitadelClient("https://z.example.com", "tok")
        resp = _mock_response({"keyId": "k-1", "keyDetails": "..."})
        client._client = AsyncMock()
        client._client.post = AsyncMock(return_value=resp)

        result = await client.create_machine_key("u-1")
        assert result["keyId"] == "k-1"


# ---------------------------------------------------------------------------
# Grants
# ---------------------------------------------------------------------------

class TestZitadelClientGrants:
    @pytest.mark.asyncio
    async def test_grant_project_roles(self):
        client = ZitadelClient("https://z.example.com", "tok")
        with patch.object(client, "_post", new_callable=AsyncMock) as mock_post:
            await client.grant_project_roles("proj-1", "u-1", ["admin"])
        mock_post.assert_awaited_once_with(
            "/projects/proj-1/members",
            {"userId": "u-1", "roles": ["admin"]},
        )

    @pytest.mark.asyncio
    async def test_add_user_grant(self):
        client = ZitadelClient("https://z.example.com", "tok")
        with patch.object(client, "_post", new_callable=AsyncMock,
                          return_value={"userGrantId": "g-1"}):
            gid = await client.add_user_grant("proj-1", "u-1", ["admin"])
        assert gid == "g-1"


# ---------------------------------------------------------------------------
# PAT and OIDC
# ---------------------------------------------------------------------------

class TestZitadelClientMisc:
    @pytest.mark.asyncio
    async def test_create_pat(self):
        client = ZitadelClient("https://z.example.com", "tok")
        resp = _mock_response({"token": "pat-abc"})
        client._client = AsyncMock()
        client._client.post = AsyncMock(return_value=resp)

        token = await client.create_pat("u-1")
        assert token == "pat-abc"

    @pytest.mark.asyncio
    async def test_get_oidc_config(self):
        client = ZitadelClient("https://z.example.com", "tok")
        resp = _mock_response({
            "issuer": "https://z.example.com",
            "jwks_uri": "https://z.example.com/.well-known/jwks.json",
        })
        client._client = AsyncMock()
        client._client.get = AsyncMock(return_value=resp)

        config = await client.get_oidc_config()
        assert config["issuer"] == "https://z.example.com"

    @pytest.mark.asyncio
    async def test_close(self):
        client = ZitadelClient("https://z.example.com", "tok")
        client._client = AsyncMock()
        await client.close()
        client._client.aclose.assert_awaited_once()


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

class TestBootstrap:
    @pytest.mark.asyncio
    async def test_bootstrap_new_project(self):
        with patch("sdk.auth.zitadel.ZitadelClient") as MockClient:
            mock_z = AsyncMock()
            MockClient.return_value = mock_z
            # Context manager
            mock_z.__aenter__ = AsyncMock(return_value=mock_z)
            mock_z.__aexit__ = AsyncMock(return_value=False)

            mock_z.find_project_by_name.return_value = None
            mock_z.create_project.return_value = "proj-new"
            mock_z.bulk_add_roles = AsyncMock()
            mock_z.create_machine_user.return_value = "u-new"
            mock_z.add_user_grant = AsyncMock(return_value="g-1")
            mock_z.create_machine_key.return_value = {"keyId": "k-1"}
            mock_z.get_oidc_config.return_value = {
                "issuer": "https://z.example.com",
                "jwks_uri": "https://z.example.com/.well-known/jwks.json",
            }

            result = await bootstrap("https://z.example.com", "admin-tok")

        assert result["project_id"] == "proj-new"
        assert result["user_id"] == "u-new"
        assert result["machine_key"]["keyId"] == "k-1"

    @pytest.mark.asyncio
    async def test_bootstrap_existing_project(self):
        with patch("sdk.auth.zitadel.ZitadelClient") as MockClient:
            mock_z = AsyncMock()
            MockClient.return_value = mock_z
            mock_z.__aenter__ = AsyncMock(return_value=mock_z)
            mock_z.__aexit__ = AsyncMock(return_value=False)

            mock_z.find_project_by_name.return_value = "proj-existing"
            mock_z.bulk_add_roles = AsyncMock()
            mock_z.create_machine_user.return_value = "u-new"
            mock_z.add_user_grant = AsyncMock(return_value="g-1")
            mock_z.create_machine_key.return_value = {"keyId": "k-1"}
            mock_z.get_oidc_config.return_value = {
                "issuer": "https://z.example.com",
                "jwks_uri": "https://z.example.com/.well-known/jwks.json",
            }

            result = await bootstrap("https://z.example.com", "admin-tok")

        assert result["project_id"] == "proj-existing"
        mock_z.create_project.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_bootstrap_user_already_exists(self):
        with patch("sdk.auth.zitadel.ZitadelClient") as MockClient:
            mock_z = AsyncMock()
            MockClient.return_value = mock_z
            mock_z.__aenter__ = AsyncMock(return_value=mock_z)
            mock_z.__aexit__ = AsyncMock(return_value=False)

            mock_z.find_project_by_name.return_value = "proj-1"
            mock_z.bulk_add_roles = AsyncMock()

            # Simulate 409 on user creation
            resp_409 = MagicMock()
            resp_409.status_code = 409
            mock_z.create_machine_user.side_effect = httpx.HTTPStatusError(
                "Conflict", request=MagicMock(), response=resp_409,
            )

            mock_z.get_oidc_config.return_value = {
                "issuer": "https://z.example.com",
                "jwks_uri": "https://z.example.com/.well-known/jwks.json",
            }

            result = await bootstrap("https://z.example.com", "admin-tok")

        assert result["user_id"] == ""
        assert result["machine_key"] == {}

    @pytest.mark.asyncio
    async def test_bootstrap_user_creation_non_409_error(self):
        with patch("sdk.auth.zitadel.ZitadelClient") as MockClient:
            mock_z = AsyncMock()
            MockClient.return_value = mock_z
            mock_z.__aenter__ = AsyncMock(return_value=mock_z)
            mock_z.__aexit__ = AsyncMock(return_value=False)

            mock_z.find_project_by_name.return_value = "proj-1"
            mock_z.bulk_add_roles = AsyncMock()

            resp_500 = MagicMock()
            resp_500.status_code = 500
            mock_z.create_machine_user.side_effect = httpx.HTTPStatusError(
                "Server Error", request=MagicMock(), response=resp_500,
            )

            with pytest.raises(httpx.HTTPStatusError):
                await bootstrap("https://z.example.com", "admin-tok")

    @pytest.mark.asyncio
    async def test_bootstrap_custom_roles(self):
        with patch("sdk.auth.zitadel.ZitadelClient") as MockClient:
            mock_z = AsyncMock()
            MockClient.return_value = mock_z
            mock_z.__aenter__ = AsyncMock(return_value=mock_z)
            mock_z.__aexit__ = AsyncMock(return_value=False)

            mock_z.find_project_by_name.return_value = "proj-1"
            mock_z.bulk_add_roles = AsyncMock()
            mock_z.create_machine_user.return_value = "u-1"
            mock_z.add_user_grant = AsyncMock(return_value="g-1")
            mock_z.create_machine_key.return_value = {}
            mock_z.get_oidc_config.return_value = {"issuer": "https://z.example.com"}

            custom_roles = ["custom:role1", "custom:role2"]
            await bootstrap("https://z.example.com", "admin-tok", roles=custom_roles)

        mock_z.bulk_add_roles.assert_awaited_once_with("proj-1", custom_roles)

    def test_default_roles(self):
        assert "admin" in DEFAULT_ROLES
        assert "agent:copilot" in DEFAULT_ROLES
        assert "agent:claude" in DEFAULT_ROLES
